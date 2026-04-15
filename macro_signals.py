"""
macro_signals.py
────────────────
Macro-level drift signals from FRED, packaged as features for the backtest.

Two signals:

1. **HY OAS enhanced** — upgrades the existing binary-regime HY OAS flag to
   a 4-feature vector:
     (a) level (BAMLH0A0HYM2)
     (b) 13-week change
     (c) deviation from 6-month moving average
     (d) CCC-BB spread ratio (BAMLH0A3HYC / BAMLH0A1HYBB)
   Gilchrist & Zakrajšek (2012) showed HY OAS predicts 3-12mo equity returns.
   Mechanism: bond investors price corporate default risk before equity does.

2. **Margin debt** — FINRA monthly margin debt series, z-scored.
   NYU Stern (2016): peaks in margin debt precede avg -7.8% SPX over 12mo,
   71% hit rate. Mechanism: leverage cycle + forced selling on vol spikes.

Both signals output a `tilt_signal` in [-1, +1] suitable for drift tilting.
Sign convention: positive = bullish (spreads tightening, or margin low → rally ok)

Usage:
    python3 macro_signals.py build       # fetch all, compute features, save
    python3 macro_signals.py latest      # print current snapshot
"""

from __future__ import annotations

import argparse
import io
import logging
from pathlib import Path
from typing import Dict, Optional

import pandas as pd
import requests

logger = logging.getLogger("macro")

OUT_DIR = Path("public_pulse_data")
OUT_DIR.mkdir(exist_ok=True)
HY_CSV = OUT_DIR / "hy_oas.csv"
MD_CSV = OUT_DIR / "margin_debt.csv"

FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv"

FRED_SERIES = {
    "BAMLH0A0HYM2":  "ICE BofA US HY OAS",
    "BAMLH0A3HYC":   "ICE BofA CCC & lower US HY OAS",
    "BAMLH0A1HYBB":  "ICE BofA BB US HY OAS",
}


def _fred(series_id: str) -> pd.DataFrame:
    r = requests.get(FRED, params={"id": series_id}, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = ["date", series_id]
    df["date"] = pd.to_datetime(df["date"])
    df[series_id] = pd.to_numeric(df[series_id], errors="coerce")
    return df


# ─────────────────────────────────────────────────────────────────────
# 1. HY OAS enhanced feature set
# ─────────────────────────────────────────────────────────────────────

def build_hy_oas() -> pd.DataFrame:
    """Compute 4-feature HY OAS vector + composite tilt_signal."""
    logger.info("Pulling HY OAS series...")
    hy    = _fred("BAMLH0A0HYM2")
    ccc   = _fred("BAMLH0A3HYC")
    bb    = _fred("BAMLH0A1HYBB")

    df = hy.merge(ccc, on="date", how="left").merge(bb, on="date", how="left").sort_values("date")

    # Feature (a): level
    df["hy_level"] = df["BAMLH0A0HYM2"]
    # Feature (b): 13-week change (≈65 business days)
    df["hy_chg_13w"] = df["hy_level"].diff(65)
    # Feature (c): deviation from 6-month (126-day) MA
    ma = df["hy_level"].rolling(126, min_periods=30).mean()
    df["hy_dev_6mo_ma"] = df["hy_level"] - ma
    # Feature (d): CCC-BB ratio (elevated ratio = stressed low-credit, bearish)
    df["ccc_bb_ratio"] = df["BAMLH0A3HYC"] / df["BAMLH0A1HYBB"]

    # Composite tilt: credit TIGHTENING is bullish (spreads narrowing, risk priced)
    # Use z-score of NEGATIVE 13w change (i.e., tightening = positive signal)
    # vs 2-year rolling distribution. Also penalize high CCC/BB ratio.
    roll_chg = df["hy_chg_13w"].rolling(504, min_periods=60)
    hy_z = (-(df["hy_chg_13w"] - roll_chg.mean()) / (roll_chg.std() + 1e-9))

    roll_ratio = df["ccc_bb_ratio"].rolling(504, min_periods=60)
    ratio_z = -((df["ccc_bb_ratio"] - roll_ratio.mean()) / (roll_ratio.std() + 1e-9))

    # Equal-weight combine, clamp
    df["tilt_signal"] = ((0.6 * hy_z + 0.4 * ratio_z).clip(-2, 2) / 2).fillna(0)

    df = df.dropna(subset=["hy_level"]).reset_index(drop=True)
    df.to_csv(HY_CSV, index=False)
    logger.info("HY OAS: %d rows → %s", len(df), HY_CSV.name)
    return df


# ─────────────────────────────────────────────────────────────────────
# 2. Margin debt
# ─────────────────────────────────────────────────────────────────────

def build_margin_debt() -> pd.DataFrame:
    """FINRA monthly margin-debt series, z-scored.

    FRED mirrors BOGZ1FL663067003Q (margin accts debit, quarterly). We use
    the more timely monthly FINRA series via stlouis FRED proxy if available.

    Simple implementation: z-score the margin debt level vs 5yr rolling.
    Signal convention: HIGH margin = LOW expected return (negative tilt).
    """
    logger.info("Pulling margin debt (BOGZ1FL663067003Q)...")
    # Use FRED's official quarterly margin loans series
    df = _fred("BOGZ1FL663067003Q")
    df = df.rename(columns={"BOGZ1FL663067003Q": "margin_debt"})

    # Z-score vs 5-year trailing (20 quarters)
    roll = df["margin_debt"].rolling(20, min_periods=8)
    df["z_5y"] = (df["margin_debt"] - roll.mean()) / (roll.std() + 1e-9)

    # Sign flip: high margin debt → bearish → negative tilt
    df["tilt_signal"] = (-df["z_5y"].clip(-2, 2) / 2).fillna(0)
    df = df.dropna(subset=["margin_debt"]).reset_index(drop=True)
    df.to_csv(MD_CSV, index=False)
    logger.info("Margin debt: %d rows → %s", len(df), MD_CSV.name)
    return df


# ─────────────────────────────────────────────────────────────────────
# Tilt time-series wrappers
# ─────────────────────────────────────────────────────────────────────

def hy_oas_tilt_timeseries() -> Optional[pd.DataFrame]:
    p = HY_CSV
    if not p.exists():
        build_hy_oas()
    df = pd.read_csv(p, parse_dates=["date"])
    return df[["date", "tilt_signal", "hy_level", "hy_chg_13w", "ccc_bb_ratio"]]


def margin_debt_tilt_timeseries() -> Optional[pd.DataFrame]:
    p = MD_CSV
    if not p.exists():
        build_margin_debt()
    df = pd.read_csv(p, parse_dates=["date"])
    return df[["date", "tilt_signal", "margin_debt", "z_5y"]]


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="Fetch + compute both signals")
    sub.add_parser("latest", help="Print latest snapshot")
    args = p.parse_args()

    if args.cmd == "build":
        hy = build_hy_oas()
        md = build_margin_debt()
        print("\n── HY OAS tail ──")
        print(hy.tail(5)[["date", "hy_level", "hy_chg_13w", "ccc_bb_ratio", "tilt_signal"]].to_string(index=False))
        print("\n── Margin debt tail ──")
        print(md.tail(5)[["date", "margin_debt", "z_5y", "tilt_signal"]].to_string(index=False))

    elif args.cmd == "latest":
        hy = hy_oas_tilt_timeseries()
        md = margin_debt_tilt_timeseries()
        if hy is not None and len(hy):
            r = hy.iloc[-1]
            print(f"HY OAS     {r.date.date()}: level={r.hy_level:.2f}  tilt={r.tilt_signal:+.3f}")
        if md is not None and len(md):
            r = md.iloc[-1]
            print(f"Margin dbt {r.date.date()}: ${r.margin_debt:,.0f}M  z={r.z_5y:+.2f}  tilt={r.tilt_signal:+.3f}")


if __name__ == "__main__":
    main()
