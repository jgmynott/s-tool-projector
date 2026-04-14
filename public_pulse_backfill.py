"""
public_pulse_backfill.py
────────────────────────
Historical backfill for the Public Pulse signal.

For each symbol in the backtest universe, pull daily time-series from the
non-Reddit sources (Google Trends, Wikipedia, GDELT) — these have clean
historical APIs — then compose a daily Public Pulse score that can be joined
against price data during walk-forward backtest.

Michigan CSI (UMCSENT) is a single series — loaded once from FRED and broadcast
across all symbols and dates.

Broad Reddit historical backfill is DEFERRED (it would require an arctic-shift
archive scan across 5 subs × 107 symbols × 2–3 years = tens of hours with
FinBERT scoring). For v1 PP backtest, we rely on the 4 non-Reddit sources which
still cover the "leading indicator" thesis: search, pageviews, news tone,
consumer confidence.

Output:
    public_pulse_data/
      gt_{SYMBOL}_{START}_{END}.csv          — Google Trends daily
      wiki_{SYMBOL}_{START}_{END}.csv         — Wikipedia pageviews daily
      gdelt_{SYMBOL}_{START}_{END}.csv        — GDELT tone daily/15-min
      umcsent_{START}_{END}.csv                — Michigan CSI monthly
      public_pulse_combined_{START}_{END}.csv — merged, daily composite score

Usage:
    # Smoke-test (1 symbol, short window)
    python3 public_pulse_backfill.py --symbols AAPL --start 2024-01-01 --end 2024-06-01

    # Full (107 symbols, 3 years) — overnight job
    python3 public_pulse_backfill.py --all --start 2023-01-01 --end 2026-04-01

    # Resume-safe: skips symbols with existing output CSVs
    python3 public_pulse_backfill.py --all --start 2023-01-01 --end 2026-04-01 --resume
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

# Load env
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from public_pulse import (
    GoogleTrendsCollector,
    WikipediaCollector,
    GDELTCollector,
    TICKER_TERMS,
)
from data_providers import FREDProvider

logger = logging.getLogger("pp_backfill")

OUT_DIR = Path("public_pulse_data")
OUT_DIR.mkdir(exist_ok=True)

# Default universe = same 107 symbols as sentiment_backtest
# Pulled from the WSB sentiment run manifest.
DEFAULT_UNIVERSE = [
    "AAPL","ABBV","AMC","AMD","AMZN","ARKK","ARM","AVGO","BA","BAC",
    "BB","BBBY","BNTX","CAT","CLNE","CLOV","COIN","COST","CRM","CRWD",
    "CSCO","CVX","DIS","DKNG","DOCU","ENPH","ETSY","F","FCX","FDX",
    "FUBO","GE","GILD","GM","GME","GOOG","GOOGL","HD","HOOD","IBKR",
    "IBM","INTC","IWM","JNJ","JPM","KO","LCID","LULU","LYFT","MA",
    "MARA","MCD","META","MRK","MRNA","MSFT","MU","NFLX","NIO","NKE",
    "NVDA","ORCL","PEP","PFE","PINS","PLTR","PLUG","PYPL","QQQ","RBLX",
    "RIOT","RIVN","ROKU","SBUX","SHOP","SNAP","SNOW","SOFI","SPCE","SPY",
    "SQ","T","TDOC","TGT","TLT","TSLA","TSM","TWLO","TXN","U",
    "UBER","UNH","UPST","V","VZ","WBA","WFC","WMT","WYNN","X",
    "XLE","XLF","XLV","XOM","Z","ZM","ZS",
]


# ─────────────────────────────────────────────────────────────────────
# Per-source backfill
# ─────────────────────────────────────────────────────────────────────

def _out_path(source: str, symbol: str, start: str, end: str) -> Path:
    return OUT_DIR / f"{source}_{symbol}_{start}_{end}.csv"


def _already_done(source: str, symbol: str, start: str, end: str) -> bool:
    p = _out_path(source, symbol, start, end)
    return p.exists() and p.stat().st_size > 50  # not empty


def backfill_google_trends(symbol: str, start: str, end: str) -> int:
    """Pull Google Trends daily for a symbol. pytrends limits timeframe to ~9 months
    for daily granularity; we chunk the date range."""
    gt = GoogleTrendsCollector()
    if not gt.available:
        logger.warning("pytrends unavailable; skipping Google Trends")
        return 0

    all_rows: List[Dict] = []
    # Chunk into 180-day windows for daily resolution
    cur = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    while cur < end_dt:
        chunk_end = min(cur + timedelta(days=180), end_dt)
        rows = gt.historical(
            symbol,
            cur.strftime("%Y-%m-%d"),
            chunk_end.strftime("%Y-%m-%d"),
        )
        all_rows.extend(rows)
        # pytrends is rate-limited; 2s between chunks is conservative
        time.sleep(2.0)
        cur = chunk_end + timedelta(days=1)

    # Deduplicate by date (chunks can overlap by 1 day)
    seen = set()
    dedup = []
    for r in all_rows:
        if r["date"] not in seen:
            seen.add(r["date"])
            dedup.append(r)

    out = _out_path("gt", symbol, start, end)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "value"])
        w.writeheader()
        w.writerows(dedup)
    logger.info("gt %s: %d rows → %s", symbol, len(dedup), out.name)
    return len(dedup)


def backfill_wikipedia(symbol: str, start: str, end: str) -> int:
    wk = WikipediaCollector()
    rows = wk.historical(symbol, start, end)
    out = _out_path("wiki", symbol, start, end)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "value"])
        w.writeheader()
        w.writerows(rows)
    logger.info("wiki %s: %d rows → %s", symbol, len(rows), out.name)
    return len(rows)


def backfill_gdelt(symbol: str, start: str, end: str) -> int:
    """GDELT TimelineTone has a 5s rate limit; throttled inside the collector."""
    gd = GDELTCollector()
    # Chunk in 90-day windows to keep individual JSON blobs manageable
    all_rows: List[Dict] = []
    cur = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    while cur < end_dt:
        chunk_end = min(cur + timedelta(days=90), end_dt)
        rows = gd.historical(
            symbol,
            cur.strftime("%Y-%m-%d"),
            chunk_end.strftime("%Y-%m-%d"),
        )
        all_rows.extend(rows)
        # Collector throttle handles 5s wait automatically
        cur = chunk_end + timedelta(days=1)

    # Deduplicate + aggregate to daily
    df = pd.DataFrame(all_rows)
    if df.empty:
        logger.warning("gdelt %s: no data", symbol)
        out = _out_path("gdelt", symbol, start, end)
        out.write_text("date,value\n")
        return 0
    df = df.drop_duplicates(subset=["date"])
    out = _out_path("gdelt", symbol, start, end)
    df.to_csv(out, index=False)
    logger.info("gdelt %s: %d rows → %s", symbol, len(df), out.name)
    return len(df)


def backfill_michigan_csi(start: str, end: str) -> int:
    """Pull full UMCSENT series once — applies to all symbols."""
    out = OUT_DIR / f"umcsent_{start}_{end}.csv"
    if out.exists() and out.stat().st_size > 50:
        return 0  # already cached
    fred = FREDProvider()
    import requests
    # FRED CSV endpoint supports date filtering via cosd/coed params
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv"
    params = {"id": "UMCSENT", "cosd": start, "coed": end}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    lines = r.text.strip().split("\n")
    # CSV: observation_date,UMCSENT — drop placeholder rows
    out_rows = []
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) >= 2 and parts[1].strip() != ".":
            try:
                out_rows.append({"date": parts[0].strip(), "value": float(parts[1].strip())})
            except ValueError:
                continue
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "value"])
        w.writeheader()
        w.writerows(out_rows)
    logger.info("umcsent: %d rows → %s", len(out_rows), out.name)
    return len(out_rows)


# ─────────────────────────────────────────────────────────────────────
# Composition — build combined daily PP score
# ─────────────────────────────────────────────────────────────────────

PP_WEIGHTS = {
    "gt":         0.25,
    "wiki":       0.20,
    "gdelt":      0.22,
    # broad_reddit 0.18 deferred
    "csi":        0.15,
}


def _normalize_series(df: pd.DataFrame, window: int = 60) -> pd.DataFrame:
    """Normalize `value` column to z-ish score relative to trailing `window`-day mean.
    Returns a new df with `norm` column in [-1, +1].
    """
    if df.empty or "value" not in df.columns:
        return df.assign(norm=[])
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    # Use rolling mean as baseline; use rolling std for scale
    roll_mean = df["value"].rolling(window, min_periods=10).mean()
    roll_std = df["value"].rolling(window, min_periods=10).std()
    # Relative deviation, clipped to [-1, +1]
    rel = (df["value"] - roll_mean) / roll_mean.replace(0, float("nan"))
    df["norm"] = rel.clip(-1, 1).fillna(0)
    return df[["date", "value", "norm"]]


def _normalize_csi(df: pd.DataFrame, mean: float = 85.0, sd: float = 15.0) -> pd.DataFrame:
    """Michigan CSI uses a fixed long-run mean/sd (same as live PP)."""
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    z = (df["value"] - mean) / sd
    df["norm"] = (z / 2.0).clip(-1, 1)
    return df[["date", "value", "norm"]]


def build_combined(symbols: List[str], start: str, end: str) -> Path:
    """Merge per-source CSVs into daily per-symbol composite PP score.

    Output: public_pulse_combined_{start}_{end}.csv with columns:
        date, symbol, composite, gt_norm, wiki_norm, gdelt_norm, csi_norm, n_sources
    """
    # Load CSI once — applies to all symbols
    csi_path = OUT_DIR / f"umcsent_{start}_{end}.csv"
    csi_df = pd.read_csv(csi_path) if csi_path.exists() else pd.DataFrame()
    csi_df = _normalize_csi(csi_df) if not csi_df.empty else csi_df
    if not csi_df.empty:
        csi_df = csi_df.rename(columns={"value": "csi_raw", "norm": "csi_norm"}).drop_duplicates("date")

    combined_rows: List[Dict] = []
    for sym in symbols:
        frames = []
        for src, weight in [("gt", PP_WEIGHTS["gt"]),
                            ("wiki", PP_WEIGHTS["wiki"]),
                            ("gdelt", PP_WEIGHTS["gdelt"])]:
            p = _out_path(src, sym, start, end)
            if not p.exists() or p.stat().st_size < 50:
                continue
            df = pd.read_csv(p)
            if df.empty:
                continue
            df = _normalize_series(df)
            df = df.rename(columns={"norm": f"{src}_norm", "value": f"{src}_raw"})
            frames.append(df[["date", f"{src}_norm", f"{src}_raw"]])
        if not frames:
            continue
        # Merge frames on date
        merged = frames[0]
        for f in frames[1:]:
            merged = merged.merge(f, on="date", how="outer")
        # CSI merge (monthly — forward-fill)
        if not csi_df.empty:
            merged = merged.merge(csi_df[["date", "csi_norm", "csi_raw"]], on="date", how="left")
            merged["csi_norm"] = merged["csi_norm"].ffill()
            merged["csi_raw"] = merged["csi_raw"].ffill()

        merged = merged.sort_values("date")

        # Weighted composite of available norms
        def row_composite(row):
            num = 0.0; denom = 0.0; n = 0
            for src, col in [("gt", "gt_norm"), ("wiki", "wiki_norm"),
                             ("gdelt", "gdelt_norm"), ("csi", "csi_norm")]:
                v = row.get(col)
                if pd.notna(v):
                    num += v * PP_WEIGHTS[src]
                    denom += PP_WEIGHTS[src]
                    n += 1
            return pd.Series({"composite": num / denom if denom else None, "n_sources": n})

        comp = merged.apply(row_composite, axis=1)
        merged["composite"] = comp["composite"]
        merged["n_sources"] = comp["n_sources"]
        merged["symbol"] = sym
        combined_rows.append(merged)

    if not combined_rows:
        logger.warning("No per-source data to combine")
        out = OUT_DIR / f"public_pulse_combined_{start}_{end}.csv"
        out.write_text("date,symbol,composite,n_sources\n")
        return out

    combined = pd.concat(combined_rows, ignore_index=True)
    out = OUT_DIR / f"public_pulse_combined_{start}_{end}.csv"
    cols = ["date", "symbol", "composite", "n_sources",
            "gt_norm", "wiki_norm", "gdelt_norm", "csi_norm",
            "gt_raw", "wiki_raw", "gdelt_raw", "csi_raw"]
    cols = [c for c in cols if c in combined.columns]
    combined[cols].to_csv(out, index=False)
    logger.info("combined: %d symbol-days → %s", len(combined), out.name)
    return out


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Backfill Public Pulse historical data")
    p.add_argument("--symbols", nargs="+", help="Specific tickers to fetch")
    p.add_argument("--all", action="store_true", help="Use the 107-symbol universe")
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD")
    p.add_argument("--sources", nargs="+",
                   choices=["gt", "wiki", "gdelt", "csi", "combine"],
                   default=["gt", "wiki", "gdelt", "csi", "combine"],
                   help="Which sources to backfill")
    p.add_argument("--resume", action="store_true",
                   help="Skip symbol+source pairs already in public_pulse_data/")

    args = p.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    symbols = DEFAULT_UNIVERSE if args.all else (args.symbols or [])
    if not symbols and "combine" not in args.sources:
        print("Must pass --symbols or --all", file=sys.stderr)
        sys.exit(1)

    # ── CSI once (not per-symbol) ──
    if "csi" in args.sources:
        csi_path = OUT_DIR / f"umcsent_{args.start}_{args.end}.csv"
        if args.resume and csi_path.exists():
            logger.info("csi: cached, skip")
        else:
            backfill_michigan_csi(args.start, args.end)

    # ── Per-symbol sources ──
    for i, sym in enumerate(symbols):
        logger.info("── [%d/%d] %s ──", i + 1, len(symbols), sym)
        if "wiki" in args.sources:
            if args.resume and _already_done("wiki", sym, args.start, args.end):
                logger.info("wiki %s: cached, skip", sym)
            else:
                try: backfill_wikipedia(sym, args.start, args.end)
                except Exception as e: logger.error("wiki %s FAILED: %s", sym, e)
        if "gdelt" in args.sources:
            if args.resume and _already_done("gdelt", sym, args.start, args.end):
                logger.info("gdelt %s: cached, skip", sym)
            else:
                try: backfill_gdelt(sym, args.start, args.end)
                except Exception as e: logger.error("gdelt %s FAILED: %s", sym, e)
        if "gt" in args.sources:
            if args.resume and _already_done("gt", sym, args.start, args.end):
                logger.info("gt %s: cached, skip", sym)
            else:
                try: backfill_google_trends(sym, args.start, args.end)
                except Exception as e: logger.error("gt %s FAILED: %s", sym, e)

    # ── Combine ──
    if "combine" in args.sources:
        build_combined(symbols or DEFAULT_UNIVERSE, args.start, args.end)
        logger.info("✓ Done")


if __name__ == "__main__":
    main()
