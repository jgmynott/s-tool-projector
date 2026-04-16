"""
signals_breadth.py
──────────────────
Market breadth signal — % of universe trading above its 200-day SMA.

Classic breadth measure: when most stocks trade above their 200-day moving
average, the market is in a broad uptrend; when few do, it's broadly weak.
Extreme readings in either direction can be either momentum (trend-following)
or mean-reverting (exhaustion) signals. Backtest decides which.

We compute this from our own price cache (`data_cache/prices/*.csv`, ~2,358
symbols × 5yr of history) rather than an external feed, so it exactly matches
the universe we're trading.

Signal:
  breadth_pct = share of cached symbols with close > 200d SMA on each day
  tilt_signal = -1 × z-score vs 2-year rolling, clipped to [-2, +2] / 2
    → mean-reversion convention: extreme high breadth is bearish contra
    Flip sign for momentum interpretation in the backtest.

Usage:
    python3 signals_breadth.py build      # compute + cache (reads data_cache/)
    python3 signals_breadth.py latest     # current reading
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("breadth")

PRICE_CACHE = Path("data_cache/prices")
OUT_DIR = Path("public_pulse_data")
OUT_DIR.mkdir(exist_ok=True)
CSV = OUT_DIR / "breadth.csv"

SMA_DAYS = 200
MIN_SYMBOLS_PER_DAY = 200  # skip days before universe coverage is dense enough


def _load_close(symbol: str) -> Optional[pd.Series]:
    p = PRICE_CACHE / f"{symbol}.csv"
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p, parse_dates=["Date"])
    except Exception:
        return None
    if "Close" not in df.columns or df.empty:
        return None
    s = df.set_index("Date")["Close"].astype(float).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    s.index = s.index.tz_localize(None) if s.index.tz is not None else s.index
    return s


def build() -> pd.DataFrame:
    files = sorted(PRICE_CACHE.glob("*.csv"))
    files = [f for f in files if not f.name.startswith("_")]
    logger.info("Scanning %d cached price files for breadth...", len(files))

    # Compute each symbol's "above 200d SMA" boolean series, stack into a panel.
    above_panel = {}
    for i, p in enumerate(files):
        sym = p.stem
        s = _load_close(sym)
        if s is None or len(s) < SMA_DAYS + 10:
            continue
        sma = s.rolling(SMA_DAYS, min_periods=SMA_DAYS).mean()
        above = (s > sma).astype("Int8")
        above.name = sym
        above_panel[sym] = above
        if (i + 1) % 500 == 0:
            logger.info("  processed %d/%d", i + 1, len(files))

    if not above_panel:
        raise RuntimeError("No usable price caches — run price_backfill.py first")

    panel = pd.DataFrame(above_panel).sort_index()
    # Per-day counts
    pct_above = panel.mean(axis=1, skipna=True)  # fraction in [0, 1]
    n_obs = panel.count(axis=1)
    pct_above = pct_above[n_obs >= MIN_SYMBOLS_PER_DAY]
    df = pd.DataFrame({"date": pct_above.index, "breadth_pct": pct_above.values,
                       "n_symbols": n_obs.reindex(pct_above.index).values})
    df = df.reset_index(drop=True)

    # 2yr rolling z-score (504 trading days)
    roll = df["breadth_pct"].rolling(504, min_periods=60)
    df["z_2y"] = (df["breadth_pct"] - roll.mean()) / (roll.std() + 1e-9)
    # Mean-reversion convention: high breadth → negative tilt (contrarian bearish)
    df["tilt_signal"] = (-df["z_2y"].clip(-2, 2) / 2).fillna(0)
    df.to_csv(CSV, index=False)
    logger.info("Breadth: %d rows (%d–%d symbols/day) → %s",
                len(df), int(df.n_symbols.min()), int(df.n_symbols.max()), CSV.name)
    return df


def tilt_timeseries() -> Optional[pd.DataFrame]:
    if not CSV.exists():
        build()
    df = pd.read_csv(CSV, parse_dates=["date"])
    return df[["date", "tilt_signal", "breadth_pct", "z_2y"]]


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    sub.add_parser("latest")
    args = p.parse_args()

    if args.cmd == "build":
        df = build()
        print(df.tail(5)[["date", "breadth_pct", "z_2y", "tilt_signal"]].to_string(index=False))
    elif args.cmd == "latest":
        df = tilt_timeseries()
        r = df.iloc[-1]
        print(f"Breadth {r.date.date()}: pct>200SMA={r.breadth_pct:.1%}  "
              f"z={r.z_2y:+.2f}  tilt={r.tilt_signal:+.3f}")


if __name__ == "__main__":
    main()
