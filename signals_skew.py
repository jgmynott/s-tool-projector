"""
signals_skew.py
───────────────
CBOE SKEW Index signal.

SKEW measures the price of far-OTM S&P 500 put options relative to ATM options.
Higher readings mean options traders are paying up for tail-risk protection —
i.e., they are pricing in a higher probability of a large down-move.

Two plausible interpretations:
  (a) "Fear gauge" — high SKEW is bearish crowd positioning → contrarian bullish
  (b) "Smart money warning" — high SKEW is institutional hedging → bearish
The backtest decides which direction (if either) actually predicts drift.

Data: yfinance ^SKEW, daily, 10+ years of history.

Signal:
  tilt_signal = z-score of SKEW vs 2-year rolling window, clipped to [-2, +2] / 2.
  Sign convention: POSITIVE = bullish. We emit (a) contrarian reading by default.
  Flip in the backtest config to test interpretation (b).

Usage:
    python3 signals_skew.py build      # fetch + cache
    python3 signals_skew.py latest     # print current reading
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger("skew")

OUT_DIR = Path("public_pulse_data")  # reuse existing macro-signals cache dir
OUT_DIR.mkdir(exist_ok=True)
CSV = OUT_DIR / "skew.csv"


def build() -> pd.DataFrame:
    logger.info("Fetching ^SKEW from yfinance...")
    hist = yf.Ticker("^SKEW").history(period="max", auto_adjust=False)
    df = hist[["Close"]].rename(columns={"Close": "skew"}).reset_index()
    df.columns = ["date", "skew"]
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    df = df.sort_values("date").reset_index(drop=True)

    # 2-year rolling z-score (504 trading days)
    roll = df["skew"].rolling(504, min_periods=60)
    df["z_2y"] = (df["skew"] - roll.mean()) / (roll.std() + 1e-9)

    # Contrarian convention: high SKEW (fear) → positive (bullish) tilt_signal.
    # Flip sign in the backtest configuration to test the smart-money interpretation.
    df["tilt_signal"] = (df["z_2y"].clip(-2, 2) / 2).fillna(0)

    df.to_csv(CSV, index=False)
    logger.info("SKEW: %d rows → %s", len(df), CSV.name)
    return df


def tilt_timeseries() -> Optional[pd.DataFrame]:
    if not CSV.exists():
        build()
    df = pd.read_csv(CSV, parse_dates=["date"])
    return df[["date", "tilt_signal", "skew", "z_2y"]]


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
        print(df.tail(5)[["date", "skew", "z_2y", "tilt_signal"]].to_string(index=False))
    elif args.cmd == "latest":
        df = tilt_timeseries()
        r = df.iloc[-1]
        print(f"SKEW {r.date.date()}: {r.skew:.2f}  z={r.z_2y:+.2f}  tilt={r.tilt_signal:+.3f}")


if __name__ == "__main__":
    main()
