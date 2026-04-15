"""
price_backfill.py
─────────────────
One-shot bulk price-history fetcher for the full 2,609-ticker universe.
Uses yfinance (free, unlimited-ish) with batched downloads for speed.

  • 5yr daily adjusted closes per symbol → `data_cache/prices/<SYM>.csv`
  • Resumes: skips tickers whose parquet is younger than --max-age-hours
  • Batches of 50 via yf.download (one network round-trip per batch)
  • Logs failures to `data_cache/prices/_failures.csv`

Usage:
    python3 price_backfill.py                      # default: full universe, 5yr
    python3 price_backfill.py --years 2            # shorter history
    python3 price_backfill.py --symbols AAPL TSLA  # targeted
    python3 price_backfill.py --force              # re-fetch everything
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path
from typing import List

import pandas as pd
import yfinance as yf

logger = logging.getLogger("backfill")

OUT_DIR = Path("data_cache/prices")
OUT_DIR.mkdir(parents=True, exist_ok=True)
FAILURES_CSV = OUT_DIR / "_failures.csv"

BATCH_SIZE = 50
MIN_ROWS = 100  # reject tickers with < 100 daily bars


def _is_fresh(sym: str, max_age_hours: float) -> bool:
    p = OUT_DIR / f"{sym}.csv"
    if not p.exists():
        return False
    age_hr = (time.time() - p.stat().st_mtime) / 3600
    return age_hr < max_age_hours


def fetch_batch(symbols: List[str], start: pd.Timestamp, end: pd.Timestamp) -> dict:
    """yf.download on a batch; returns {sym: DataFrame or None}."""
    try:
        df = yf.download(
            tickers=" ".join(symbols),
            start=start, end=end,
            auto_adjust=True, progress=False,
            group_by="ticker", threads=True,
        )
    except Exception as e:
        logger.error("batch download failed (%d syms): %s", len(symbols), e)
        return {s: None for s in symbols}

    out = {}
    if len(symbols) == 1:
        s = symbols[0]
        out[s] = df if df is not None and len(df) >= MIN_ROWS else None
        return out
    # Multi-symbol: df has a MultiIndex columns (ticker, field)
    for s in symbols:
        try:
            sub = df[s].dropna(how="all")
        except (KeyError, AttributeError):
            out[s] = None
            continue
        out[s] = sub if len(sub) >= MIN_ROWS else None
    return out


def backfill(symbols: List[str], years: int, force: bool, max_age_hours: float) -> None:
    end = pd.Timestamp.today().normalize()
    start = end - pd.DateOffset(years=years)

    if not force:
        todo = [s for s in symbols if not _is_fresh(s, max_age_hours)]
        skipped = len(symbols) - len(todo)
        if skipped:
            logger.info("Skipping %d fresh symbols (<%gh old)", skipped, max_age_hours)
    else:
        todo = list(symbols)

    logger.info("Fetching %d symbols, %d years, batch=%d", len(todo), years, BATCH_SIZE)
    failures: List[dict] = []
    ok = 0
    t0 = time.time()
    for i in range(0, len(todo), BATCH_SIZE):
        batch = todo[i : i + BATCH_SIZE]
        results = fetch_batch(batch, start, end)
        for s, df in results.items():
            if df is None or df.empty or "Close" not in df.columns:
                failures.append({"symbol": s, "reason": "no_data_or_short"})
                continue
            df.to_csv(OUT_DIR / f"{s}.csv")
            ok += 1
        done = i + len(batch)
        elapsed = time.time() - t0
        rate = done / elapsed if elapsed else 0
        eta_min = (len(todo) - done) / rate / 60 if rate else 0
        logger.info("batch %d/%d — ok=%d fails=%d rate=%.1f/s ETA %.1fm",
                    done, len(todo), ok, len(failures), rate, eta_min)

    if failures:
        pd.DataFrame(failures).to_csv(FAILURES_CSV, index=False)
        logger.warning("%d failures logged → %s", len(failures), FAILURES_CSV)
    logger.info("DONE. ok=%d fails=%d total_time=%.1fm",
                ok, len(failures), (time.time() - t0) / 60)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="+", help="specific tickers (default: full universe)")
    p.add_argument("--years", type=int, default=5)
    p.add_argument("--force", action="store_true", help="re-fetch even if cached")
    p.add_argument("--max-age-hours", type=float, default=20.0,
                   help="skip symbols whose cache file is newer than this")
    args = p.parse_args()

    if args.symbols:
        symbols = args.symbols
    else:
        from universe import load_full_universe
        symbols = load_full_universe()
        logger.info("Loaded full universe: %d symbols", len(symbols))

    backfill(symbols, args.years, args.force, args.max_age_hours)


if __name__ == "__main__":
    main()
