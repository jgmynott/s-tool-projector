"""Historical price backfill to 2015-01-01 for every ticker we train on.

The data_cache/prices/ directory currently holds daily OHLCV back to
2021-04 at the earliest. Upside_hunt's walk-forward windows can only
go back as far as the oldest cached price + a small lookback buffer,
which is why the backtest is stuck in the 2022-05 → 2024-08 regime.

This script pulls yfinance history for every ticker in the training
set and MERGES older rows into the existing CSV — no re-download of
what we already have. Once done, upside_hunt.py can generate 2016 →
present windows, covering the 2018 Q4 drawdown, the 2020 COVID crash,
and the 2022 H1 tech bear.

Target ticker set: the 2,272 distinct symbols present in
upside_hunt_results.csv (i.e. the universe the NN already trains on).

Run safely in the background for ~2 hours. Progress prints every 50
symbols; resumable because we skip tickers whose CSV already reaches
the target start date.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(Path(__file__).parent / ".env")
except Exception:
    pass

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("price-backfill")

ROOT = Path(__file__).parent
PRICES_DIR = ROOT / "data_cache" / "prices"
DEFAULT_TARGET_START = "2015-01-01"
DEFAULT_SLEEP = 1.5    # polite spacing between yfinance calls


def load_universe(results_csv: Path) -> list[str]:
    df = pd.read_csv(results_csv, usecols=["symbol"])
    return sorted(df["symbol"].astype(str).str.upper().unique())


def existing_earliest(sym: str) -> date | None:
    """Return the earliest date already cached for this ticker, or None
    if we have no CSV at all."""
    p = PRICES_DIR / f"{sym}.csv"
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p, usecols=["Date"])
        if df.empty:
            return None
        first = pd.to_datetime(df["Date"]).min()
        return first.date()
    except Exception:
        return None


def fetch_range(sym: str, start: date, end: date) -> pd.DataFrame | None:
    """Pull daily OHLCV from yfinance. Returns DataFrame with
    Date,Open,High,Low,Close,Volume columns, or None."""
    import yfinance as yf
    try:
        df = yf.download(sym, start=start.isoformat(), end=end.isoformat(),
                         auto_adjust=False, progress=False, timeout=20,
                         threads=False)
    except Exception as e:
        log.debug("%s yf.download error: %s", sym, e)
        return None
    if df is None or df.empty:
        return None
    # Flatten MultiIndex columns that yfinance returns for single tickers.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.reset_index()
    # Normalise column names (yfinance sometimes returns "Adj Close" or a
    # mixture of spacing).
    rename = {}
    for c in df.columns:
        if str(c).lower().startswith("date"):
            rename[c] = "Date"
        elif str(c).lower() == "open":
            rename[c] = "Open"
        elif str(c).lower() == "high":
            rename[c] = "High"
        elif str(c).lower() == "low":
            rename[c] = "Low"
        elif str(c).lower() == "close":
            rename[c] = "Close"
        elif str(c).lower() == "volume":
            rename[c] = "Volume"
    df = df.rename(columns=rename)
    keep = [c for c in ["Date", "Open", "High", "Low", "Close", "Volume"] if c in df.columns]
    df = df[keep].copy()
    df["Date"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d")
    return df


def merge_and_save(sym: str, older: pd.DataFrame) -> int:
    """Prepend older rows to the existing CSV, de-duplicate by Date.
    Returns the count of net-new rows written."""
    p = PRICES_DIR / f"{sym}.csv"
    if p.exists():
        try:
            existing = pd.read_csv(p)
        except Exception:
            existing = pd.DataFrame()
    else:
        existing = pd.DataFrame()

    combined = pd.concat([older, existing], ignore_index=True)
    if combined.empty:
        return 0
    before = len(existing)
    combined["_d"] = pd.to_datetime(combined["Date"], errors="coerce")
    combined = combined.dropna(subset=["_d"])
    combined = combined.drop_duplicates(subset=["Date"], keep="last")
    combined = combined.sort_values("_d").drop(columns="_d").reset_index(drop=True)
    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    combined.to_csv(p, index=False)
    return len(combined) - before


def backfill(symbols: list[str], target_start: str,
              sleep_s: float, resume: bool = True) -> dict:
    target = date.fromisoformat(target_start)
    today = date.today()
    total = len(symbols)
    skipped = added = errors = empty = 0
    rows_added = 0
    t0 = time.time()

    for i, sym in enumerate(symbols):
        earliest = existing_earliest(sym)
        if resume and earliest is not None and earliest <= target:
            skipped += 1
            if (i + 1) % 100 == 0:
                log.info("[%d/%d] skipped(already back to %s): %d",
                         i + 1, total, target, skipped)
            continue

        # Fetch only what we're missing
        fetch_end = earliest if earliest else today
        try:
            df = fetch_range(sym, target, fetch_end)
            if df is None or df.empty:
                empty += 1
            else:
                n_added = merge_and_save(sym, df)
                rows_added += n_added
                added += 1
        except Exception as e:
            log.warning("%s failed: %s", sym, e)
            errors += 1

        if (i + 1) % 25 == 0:
            rate = (i + 1) / max(1e-6, (time.time() - t0))
            eta_min = (total - i - 1) / max(1e-6, rate) / 60.0
            log.info("[%d/%d] added=%d skipped=%d empty=%d err=%d  rate=%.1f/s  ETA %.0f min",
                     i + 1, total, added, skipped, empty, errors, rate, eta_min)

        time.sleep(sleep_s)

    summary = {
        "total_symbols": total,
        "added": added,
        "skipped_already_backfilled": skipped,
        "empty_response": empty,
        "errors": errors,
        "total_rows_added": rows_added,
        "elapsed_min": round((time.time() - t0) / 60.0, 1),
    }
    log.info("BACKFILL COMPLETE: %s", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-start", default=DEFAULT_TARGET_START,
                        help=f"earliest date to fetch (YYYY-MM-DD, default {DEFAULT_TARGET_START})")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP)
    parser.add_argument("--universe-source", default=str(ROOT / "upside_hunt_results.csv"),
                        help="CSV with a 'symbol' column")
    parser.add_argument("--symbols", nargs="+", default=None,
                        help="override universe — specific symbols only")
    parser.add_argument("--no-resume", action="store_true",
                        help="refetch even if existing CSV already covers target")
    parser.add_argument("--limit", type=int, default=None,
                        help="only process the first N symbols (for smoke tests)")
    args = parser.parse_args()

    if args.symbols:
        syms = [s.upper() for s in args.symbols]
    else:
        syms = load_universe(Path(args.universe_source))
        log.info("loaded %d symbols from %s", len(syms), args.universe_source)

    if args.limit:
        syms = syms[:args.limit]

    backfill(syms, target_start=args.target_start,
             sleep_s=args.sleep, resume=not args.no_resume)


if __name__ == "__main__":
    main()
