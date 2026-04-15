"""
net_liquidity.py
────────────────
Net Liquidity signal for S-Tool Projector — a macro drift tilt derived from
Fed balance sheet mechanics.

Formula:
    net_liquidity = WALCL - WTREGEN - RRPONTSYD
    (total Fed assets) − (Treasury General Account) − (reverse repo)

Mechanism: post-2020, the residual private-sector USD balance is a first-order
driver of risk-asset prices at 1-6 month horizons. When Treasury drains TGA
(spends) or reserves rise, more dollars sit in the private sector → risk-on.
When TGA builds (tax season, post-debt-ceiling) or RRP climbs, liquidity
drains → risk-off.

References:
  - Perli (2025) — NY Fed speech on monetary policy implementation
  - Arbor Data Science / Bianco Research — net-liquidity frameworks
  - Empirically ~0.6 correlation between QoQ net-liq changes and SPX returns
    post-2020. Weaker pre-2020.

Usage:
    # Pull fresh FRED data, compute features, save CSV
    python3 net_liquidity.py build

    # Inspect latest values
    python3 net_liquidity.py latest

    # Compute signal for a given date
    python3 net_liquidity.py at 2024-06-15
"""

from __future__ import annotations

import argparse
import io
import logging
from pathlib import Path
from typing import Dict, Optional

import pandas as pd
import requests

logger = logging.getLogger("net_liq")

OUT_DIR = Path("public_pulse_data")   # reuse same cache dir as PP
OUT_CSV = OUT_DIR / "net_liquidity.csv"

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
SERIES_IDS = {
    "WALCL":     "Fed total assets (weekly, Wed)",
    "WTREGEN":   "Treasury General Account (weekly, Wed)",
    "RRPONTSYD": "Reverse repo (daily)",
}


# ─────────────────────────────────────────────────────────────────────
# Data fetching
# ─────────────────────────────────────────────────────────────────────

def _fetch_series(series_id: str) -> pd.DataFrame:
    """Pull a FRED series via public CSV endpoint. No key required."""
    r = requests.get(FRED_CSV, params={"id": series_id}, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = ["date", series_id]
    df["date"] = pd.to_datetime(df["date"])
    df[series_id] = pd.to_numeric(df[series_id], errors="coerce")
    return df


def build() -> pd.DataFrame:
    """Pull all 3 components, merge, compute features, write to CSV.

    Features produced:
      • net_liq                — WALCL - WTREGEN - RRPONTSYD (billions $)
      • net_liq_chg_13w        — 13-week change (standard horizon)
      • net_liq_chg_13w_pct    — 13-week change as % of level
      • net_liq_z_52w          — z-score of 13w change vs trailing 52w distribution
      • tilt_signal            — clamped to [-1, +1] for feeding into mu_tilt

    The tilt_signal is the "ready-to-use" feature. Positive = liquidity
    expanding → bullish drift tilt. Negative = draining → bearish.
    """
    logger.info("Pulling FRED series...")
    walcl = _fetch_series("WALCL")      # weekly Wed
    tga   = _fetch_series("WTREGEN")    # weekly Wed
    rrp   = _fetch_series("RRPONTSYD")  # daily (business days)

    # Merge on weekly Wed dates (WALCL and TGA already weekly; downsample RRP)
    df = walcl.merge(tga, on="date", how="outer").sort_values("date")
    # For each date in df, get the RRP value on or before that date
    rrp_ff = rrp.set_index("date").sort_index()
    df = df.merge(
        rrp_ff.resample("D").ffill().reset_index(),
        on="date", how="left",
    )
    df["RRPONTSYD"] = df["RRPONTSYD"].fillna(0)  # zero before RRP facility era

    # Core signal
    df["net_liq"] = df["WALCL"] - df["WTREGEN"] - df["RRPONTSYD"]

    # 13-week change (standard macro horizon)
    df["net_liq_chg_13w"] = df["net_liq"].diff(13)
    df["net_liq_chg_13w_pct"] = df["net_liq_chg_13w"] / df["net_liq"].shift(13) * 100

    # Z-score of the 13w change over trailing 52 weeks (standardises regime)
    roll = df["net_liq_chg_13w"].rolling(52, min_periods=20)
    df["net_liq_z_52w"] = (df["net_liq_chg_13w"] - roll.mean()) / roll.std()

    # Tilt signal: clamp z to [-2, 2], scale to [-1, 1]
    df["tilt_signal"] = (df["net_liq_z_52w"].clip(-2, 2) / 2).fillna(0)

    df = df.dropna(subset=["net_liq"]).reset_index(drop=True)

    OUT_DIR.mkdir(exist_ok=True)
    df.to_csv(OUT_CSV, index=False)
    logger.info("Wrote %d rows → %s", len(df), OUT_CSV)
    logger.info("Date range: %s → %s",
                df.date.min().date(), df.date.max().date())
    return df


def load() -> pd.DataFrame:
    if not OUT_CSV.exists():
        logger.warning("Cache empty; building fresh")
        return build()
    df = pd.read_csv(OUT_CSV, parse_dates=["date"])
    return df


def at_date(target: pd.Timestamp) -> Dict[str, float] | None:
    """Return the most recent net-liq snapshot at or before `target`."""
    df = load()
    sub = df[df.date <= target]
    if sub.empty:
        return None
    row = sub.iloc[-1]
    return {
        "date":               row.date.strftime("%Y-%m-%d"),
        "net_liq":            float(row.net_liq),
        "net_liq_chg_13w":    float(row.net_liq_chg_13w) if pd.notna(row.net_liq_chg_13w) else None,
        "net_liq_chg_13w_pct": float(row.net_liq_chg_13w_pct) if pd.notna(row.net_liq_chg_13w_pct) else None,
        "z_52w":              float(row.net_liq_z_52w) if pd.notna(row.net_liq_z_52w) else None,
        "tilt_signal":        float(row.tilt_signal),
    }


def tilt_timeseries() -> pd.DataFrame:
    """Return date × tilt_signal for joining into backtest."""
    df = load()
    return df[["date", "tilt_signal", "net_liq_z_52w", "net_liq_chg_13w_pct"]].copy()


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="Fetch + compute + cache")
    sub.add_parser("latest", help="Print latest snapshot")
    at_p = sub.add_parser("at", help="Snapshot at a given date")
    at_p.add_argument("date")
    args = p.parse_args()

    if args.cmd == "build":
        df = build()
        print(df.tail(10).to_string(index=False))
    elif args.cmd == "latest":
        s = at_date(pd.Timestamp.today())
        if s:
            print("Latest net liquidity:")
            for k, v in s.items():
                if v is None:
                    print(f"  {k:22s} —")
                elif isinstance(v, float):
                    print(f"  {k:22s} {v:,.1f}")
                else:
                    print(f"  {k:22s} {v}")
    elif args.cmd == "at":
        s = at_date(pd.Timestamp(args.date))
        if s: print(s)


if __name__ == "__main__":
    main()
