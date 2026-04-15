"""
form4_insider.py
────────────────
Insider-buying signal from SEC Form 4 filings, via openinsider.com screener.

Academic foundation (Seyhun 1986, Lakonishok/Lee 2001, Cohen/Malloy/Pomorski 2012,
Dardas 2011): corporate insiders' open-market PURCHASES risk personal capital on
non-public information and have historically predicted positive 3-12 month
abnormal returns. Unlike crowd-sentiment, real capital commitment cannot be
cheaply faked by bots, so this signal should survive the 2023 regime change.

Data source: openinsider.com per-symbol screener, free, ~2003+ history.
  URL: http://openinsider.com/screener?s={SYMBOL}&xp=1&sortcol=0&cnt=500

openinsider pre-filters to "cluster buys" (sortcol=0 sorted by most significant)
and reports:
  - filing date, trade date, ticker, insider name, title
  - transaction type (P = open-market purchase, S = open-market sale, etc.)
  - share count, price, $ value
  - quality grade (A-D, a rough confidence score)

Signal construction:
  1. Filter to transaction type == 'P' (purchase)
  2. Sum $ volume of purchases per ticker per day
  3. Compute 60-day trailing cluster $-volume per ticker
  4. Compute 504-day (2yr) rolling z-score — standardises by firm size
  5. Clamp to [-1, +1] for tilt use

Usage:
    # Smoke test a few tickers
    python3 form4_insider.py test

    # Backfill the full 107-symbol universe (~30-60 min depending on response time)
    python3 form4_insider.py backfill --all

    # Query latest z-score snapshot
    python3 form4_insider.py latest AAPL
"""

from __future__ import annotations

import argparse
import io
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import requests

logger = logging.getLogger("form4")

OUT_DIR = Path("public_pulse_data")
OUT_DIR.mkdir(exist_ok=True)

UA = "S-Tool-Projector/1.0 (stool@s-tool.io)"


# ─────────────────────────────────────────────────────────────────────
# Fetch
# ─────────────────────────────────────────────────────────────────────

def fetch_symbol_filings(symbol: str, max_rows: int = 500) -> pd.DataFrame:
    """Scrape openinsider.com for all Form 4 filings on a symbol.

    Returns a DataFrame with columns: filing_date, trade_date, ticker, insider,
    title, tx_type, shares, price, value.

    openinsider's HTML is a clean <table> parsable via pandas.read_html.
    """
    url = (
        f"http://openinsider.com/screener?s={symbol.upper()}"
        f"&xp=1&sortcol=0&cnt={max_rows}"
    )
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
    except Exception as exc:
        logger.warning("%s: fetch failed — %s", symbol, exc)
        return pd.DataFrame()

    # openinsider exposes many small tables; the insider-trades one has
    # distinctive columns: Filing Date, Trade Date, Ticker, Insider Name,
    # Title, Trade Type, Price, Qty, Value. Pick it by column signature.
    try:
        tables = pd.read_html(io.StringIO(r.text))
    except ValueError:
        logger.warning("%s: no tables found", symbol)
        return pd.DataFrame()

    df = None
    for t in tables:
        cols_norm = [str(c).replace("\xa0", " ").strip().lower() for c in t.columns]
        if "trade date" in cols_norm and "trade type" in cols_norm and "value" in cols_norm:
            df = t.copy()
            df.columns = [str(c).replace("\xa0", " ").strip() for c in t.columns]
            break
    if df is None or df.empty:
        logger.warning("%s: no insider-trades table", symbol)
        return pd.DataFrame()

    # Typical columns: X, Filing Date, Trade Date, Ticker, Insider Name,
    # Title, Trade Type, Price, Qty, Owned, ΔOwn, Value
    col_map = {}
    for c in df.columns:
        low = c.lower()
        if "filing date" in low:        col_map[c] = "filing_date"
        elif "trade date" in low:       col_map[c] = "trade_date"
        elif c.lower() == "ticker":     col_map[c] = "ticker"
        elif "insider name" in low or low == "insider": col_map[c] = "insider"
        elif low == "title":            col_map[c] = "title"
        elif "trade type" in low:       col_map[c] = "tx_type"
        elif low == "price":            col_map[c] = "price"
        elif low == "qty":              col_map[c] = "shares"
        elif low == "value":            col_map[c] = "value"

    df = df.rename(columns=col_map)
    keep = ["filing_date", "trade_date", "ticker", "insider", "title",
            "tx_type", "price", "shares", "value"]
    keep = [c for c in keep if c in df.columns]
    if not keep:
        return pd.DataFrame()
    df = df[keep].copy()

    # Parse dates
    for dc in ("filing_date", "trade_date"):
        if dc in df.columns:
            df[dc] = pd.to_datetime(df[dc], errors="coerce")

    # Parse numeric $ value (strip $, commas, parens)
    if "value" in df.columns:
        df["value"] = (df["value"].astype(str)
                       .str.replace(r"[\$,\+]", "", regex=True)
                       .str.replace(r"\(([^)]+)\)", r"-\1", regex=True))
        df["value"] = pd.to_numeric(df["value"], errors="coerce")

    # Parse shares
    if "shares" in df.columns:
        df["shares"] = (df["shares"].astype(str)
                        .str.replace(",", "", regex=False)
                        .str.replace(r"\(([^)]+)\)", r"-\1", regex=True))
        df["shares"] = pd.to_numeric(df["shares"], errors="coerce")

    # Parse price
    if "price" in df.columns:
        df["price"] = (df["price"].astype(str)
                       .str.replace(r"[\$,]", "", regex=True))
        df["price"] = pd.to_numeric(df["price"], errors="coerce")

    # Keep only purchases (tx_type starts with "P" — Purchase)
    # Sales start with "S", grants with "A" (award), etc.
    if "tx_type" in df.columns:
        df["is_purchase"] = df["tx_type"].astype(str).str.strip().str.startswith("P")
    else:
        df["is_purchase"] = False

    return df


# ─────────────────────────────────────────────────────────────────────
# Signal construction
# ─────────────────────────────────────────────────────────────────────

def build_symbol_signal(symbol: str, raw: pd.DataFrame) -> pd.DataFrame:
    """From raw filings, build the per-day signal time series.

    Output columns: date, buy_value_usd, buy_count, cluster_60d_usd,
                    z_504d, tilt_signal (clamped to [-1, +1])
    """
    if raw.empty or "trade_date" not in raw.columns:
        return pd.DataFrame()

    # Buys only, positive dollar value
    buys = raw[raw.is_purchase & (raw["value"].fillna(0) > 0)].copy()
    if buys.empty:
        return pd.DataFrame()

    # Aggregate per trade_date
    daily = (buys.groupby(buys["trade_date"].dt.normalize())
                  .agg(buy_value_usd=("value", "sum"),
                       buy_count=("value", "size"))
                  .reset_index()
                  .rename(columns={"trade_date": "date"}))
    daily = daily.sort_values("date").reset_index(drop=True)
    if daily.empty:
        return pd.DataFrame()

    # Build a full business-day index between first and last buy
    full_idx = pd.date_range(daily["date"].min(), daily["date"].max(), freq="B")
    df = pd.DataFrame({"date": full_idx}).merge(daily, on="date", how="left")
    df["buy_value_usd"] = df["buy_value_usd"].fillna(0)
    df["buy_count"] = df["buy_count"].fillna(0)

    # 60-trading-day rolling cluster $
    df["cluster_60d_usd"] = df["buy_value_usd"].rolling(60, min_periods=5).sum()

    # 504-day (2yr) z-score of the cluster metric — controls for firm size
    roll = df["cluster_60d_usd"].rolling(504, min_periods=60)
    df["z_504d"] = (df["cluster_60d_usd"] - roll.mean()) / (roll.std() + 1e-9)

    # Tilt signal: clamp z to [-2, +2], rescale to [-1, +1]
    df["tilt_signal"] = (df["z_504d"].clip(-2, 2) / 2).fillna(0)
    df["symbol"] = symbol.upper()
    return df


# ─────────────────────────────────────────────────────────────────────
# Batch backfill
# ─────────────────────────────────────────────────────────────────────

def _out_path(symbol: str) -> Path:
    return OUT_DIR / f"form4_{symbol.upper()}.csv"


def backfill(symbols: List[str], throttle: float = 1.5,
             resume: bool = True) -> pd.DataFrame:
    """Fetch + build signal for each symbol, save CSV, return combined frame."""
    all_frames: List[pd.DataFrame] = []
    for i, sym in enumerate(symbols, 1):
        out = _out_path(sym)
        if resume and out.exists() and out.stat().st_size > 100:
            df = pd.read_csv(out, parse_dates=["date"])
            logger.info("[%d/%d] %s: cached (%d rows)", i, len(symbols), sym, len(df))
            all_frames.append(df)
            continue

        logger.info("[%d/%d] %s: fetching...", i, len(symbols), sym)
        raw = fetch_symbol_filings(sym)
        df = build_symbol_signal(sym, raw)
        if df.empty:
            logger.info("  %s: no buy filings found", sym)
            # Write empty placeholder so resume skips it
            out.write_text("date,symbol,buy_value_usd,buy_count,cluster_60d_usd,z_504d,tilt_signal\n")
            continue
        df.to_csv(out, index=False)
        logger.info("  %s: %d rows → %s", sym, len(df), out.name)
        all_frames.append(df)
        time.sleep(throttle)

    if not all_frames:
        return pd.DataFrame()
    combined = pd.concat(all_frames, ignore_index=True)
    combined_out = OUT_DIR / "form4_combined.csv"
    combined.to_csv(combined_out, index=False)
    logger.info("Combined: %d rows → %s", len(combined), combined_out.name)
    return combined


def load_combined() -> Optional[pd.DataFrame]:
    p = OUT_DIR / "form4_combined.csv"
    if not p.exists():
        return None
    return pd.read_csv(p, parse_dates=["date"])


def tilt_timeseries() -> Optional[pd.DataFrame]:
    """Convenience for backtester — combined (date, symbol, tilt_signal)."""
    df = load_combined()
    if df is None:
        return None
    return df[["date", "symbol", "tilt_signal", "z_504d", "cluster_60d_usd"]].copy()


def at_date(symbol: str, target: pd.Timestamp) -> Optional[Dict]:
    p = _out_path(symbol)
    if not p.exists():
        return None
    df = pd.read_csv(p, parse_dates=["date"])
    sub = df[df.date <= target]
    if sub.empty:
        return None
    r = sub.iloc[-1]
    return {
        "date":             r["date"].strftime("%Y-%m-%d"),
        "cluster_60d_usd":  float(r["cluster_60d_usd"]) if pd.notna(r["cluster_60d_usd"]) else None,
        "z_504d":           float(r["z_504d"]) if pd.notna(r["z_504d"]) else None,
        "tilt_signal":      float(r["tilt_signal"]) if pd.notna(r["tilt_signal"]) else None,
    }


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("test", help="Smoke-test with AAPL + 2 others")

    b = sub.add_parser("backfill", help="Fetch for many symbols")
    b.add_argument("--symbols", nargs="+")
    b.add_argument("--all", action="store_true",
                   help="Use the 107-symbol DEFAULT_UNIVERSE")
    b.add_argument("--throttle", type=float, default=1.5,
                   help="Seconds between requests (default 1.5)")
    b.add_argument("--no-resume", action="store_true")

    lt = sub.add_parser("latest", help="Print latest z-score for a symbol")
    lt.add_argument("symbol")

    args = p.parse_args()

    if args.cmd == "test":
        for sym in ["AAPL", "TSLA", "GME"]:
            print(f"\n── {sym} ──")
            raw = fetch_symbol_filings(sym)
            df = build_symbol_signal(sym, raw)
            print(f"raw rows: {len(raw)}, signal rows: {len(df)}")
            if not df.empty:
                print(df.tail(5).to_string(index=False))

    elif args.cmd == "backfill":
        if args.all:
            from public_pulse_backfill import DEFAULT_UNIVERSE
            symbols = DEFAULT_UNIVERSE
        elif args.symbols:
            symbols = [s.upper() for s in args.symbols]
        else:
            print("Must pass --symbols or --all")
            return
        backfill(symbols, throttle=args.throttle, resume=not args.no_resume)

    elif args.cmd == "latest":
        s = at_date(args.symbol, pd.Timestamp.today())
        print(s)


if __name__ == "__main__":
    main()
