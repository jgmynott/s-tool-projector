"""Wave 1 honest audit — survivorship + liquidity + transaction costs.

Three corrections an investor will ask about, applied to the production
nn_score and ensemble_score rankings:

  A) Survivorship.
     If a ticker delisted between as_of and as_of + 1 year, our current
     realized_ret silently drops the row (NaN). Re-compute realized_ret
     using last-known price — captures bankruptcies and forced delists.

  B) Liquidity floor.
     Production already filters the asymmetric tier by avg_dollar_volume
     ≥ $500k/day but the three main tiers don't. Apply the same floor to
     every pick retroactively; requeue to the next pick to hold top-20
     count constant.

  C) Transaction costs.
     Subtract 1.5% round-trip (entry + exit commissions + slippage) from
     every realized_ret. Recompute hit rate + mean return at the new
     threshold (clear +100% NET of costs, not gross).

Publishes side-by-side: original vs each correction individually vs all
three combined. Writes runtime_data/wave1_honest_audit.json so the
track-record page can surface the honest numbers.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wave1")

SCORED_CSV = ROOT / "upside_hunt_scored.csv"
PRICES_DIR = ROOT / "data_cache" / "prices"
OUT = ROOT / "runtime_data" / "wave1_honest_audit.json"

TOP_N = 20
THRESHOLD = 1.0
TX_COST = 0.015                 # 1.5% round-trip
LIQUIDITY_FLOOR = 500_000       # $500k average daily dollar volume
DELIST_GAP_DAYS = 30            # gap between as_of+1yr and last known price
TODAY = pd.Timestamp(datetime.utcnow().date())

# ── Price-frame cache ──
_PRICE_CACHE: dict[str, pd.DataFrame | None] = {}


def load_prices(sym: str) -> pd.DataFrame | None:
    if sym in _PRICE_CACHE:
        return _PRICE_CACHE[sym]
    p = PRICES_DIR / f"{sym}.csv"
    if not p.exists():
        _PRICE_CACHE[sym] = None
        return None
    try:
        df = pd.read_csv(p, usecols=["Date", "Close", "Volume"])
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.sort_values("Date").set_index("Date")
        _PRICE_CACHE[sym] = df
        return df
    except Exception:
        _PRICE_CACHE[sym] = None
        return None


def closest_close(df: pd.DataFrame, target: pd.Timestamp,
                  tolerance_days: int = 10) -> tuple[float | None, pd.Timestamp | None]:
    """Close on or just before `target`, within tolerance days."""
    if df is None or df.empty:
        return None, None
    sub = df[df.index <= target]
    if sub.empty:
        return None, None
    last_date = sub.index[-1]
    if (target - last_date).days > tolerance_days:
        return None, None
    return float(sub.iloc[-1]["Close"]), last_date


def enrich_row(row: pd.Series) -> pd.Series:
    """Attach delist-aware realized_ret and liquidity flag to one row."""
    sym = str(row["symbol"]).upper()
    as_of = pd.Timestamp(row["as_of"])
    df = load_prices(sym)

    # Default: trust upstream realized_ret
    realized_surv = row["realized_ret"]
    delisted_mid_window = False
    avg_dollar_vol = 0.0

    if df is not None and not df.empty:
        target = as_of + pd.DateOffset(years=1)
        last_date = df.index[-1]
        # Delisted if the most recent price predates the intended exit by
        # more than DELIST_GAP_DAYS. Replace realized_ret with last-price-
        # over-entry (which is typically deeply negative for wipeouts).
        if last_date < target - pd.Timedelta(days=DELIST_GAP_DAYS):
            entry_close, _ = closest_close(df, as_of)
            if entry_close and entry_close > 0:
                realized_surv = float(df.iloc[-1]["Close"]) / entry_close - 1.0
                delisted_mid_window = True

        # Liquidity: avg (Close * Volume) over 20 trading days ending at as_of
        window = df.loc[:as_of].tail(20)
        if len(window) >= 5:
            dollar_vols = (window["Close"] * window["Volume"]).replace(
                [np.inf, -np.inf], np.nan
            ).dropna()
            if len(dollar_vols) > 0:
                avg_dollar_vol = float(dollar_vols.mean())

    row["realized_ret_surv"] = realized_surv
    row["delisted_mid_window"] = delisted_mid_window
    row["avg_dollar_vol"] = avg_dollar_vol
    return row


def pick_stats(df: pd.DataFrame, score_col: str,
                realized_col: str = "realized_ret",
                liquidity_floor: float = 0.0,
                tx_cost: float = 0.0,
                top_n: int = TOP_N) -> dict:
    """For each as_of, take top-n by score (after liquidity filter),
    compute hit + mean return using realized_col with tx_cost subtracted."""
    d = df.copy()
    if liquidity_floor > 0:
        d = d[d["avg_dollar_vol"] >= liquidity_floor]
    picks = (d.sort_values(["as_of", score_col], ascending=[True, False])
               .groupby("as_of").head(top_n))
    realized = picks[realized_col].astype(float) - tx_cost
    realized = realized.replace([np.inf, -np.inf], np.nan).dropna()
    n = len(realized)
    if n == 0:
        return {"n": 0}
    hits = int((realized >= THRESHOLD).sum())
    return {
        "n": n,
        "hit_100": round(hits / n, 4),
        "mean_return": round(realized.mean(), 4),
        "median_return": round(realized.median(), 4),
    }


def universe_baseline(df: pd.DataFrame, realized_col: str,
                       liquidity_floor: float = 0.0,
                       tx_cost: float = 0.0) -> float:
    d = df.copy()
    if liquidity_floor > 0:
        d = d[d["avg_dollar_vol"] >= liquidity_floor]
    realized = d[realized_col].astype(float) - tx_cost
    realized = realized.replace([np.inf, -np.inf], np.nan).dropna()
    if len(realized) == 0:
        return 0.0
    return float((realized >= THRESHOLD).mean())


def main() -> None:
    log.info("loading scored data…")
    df = pd.read_csv(SCORED_CSV)
    log.info("loaded %d rows", len(df))

    # Enrich: surv-aware realized_ret + liquidity metric. ~22k rows, each
    # needs one CSV open (cached) + ~3 lookups. Coarse ETA: ~3-5 min.
    t0 = time.time()
    n = len(df)
    enriched = []
    for i, row in df.iterrows():
        enriched.append(enrich_row(row))
        if (i + 1) % 2000 == 0:
            log.info("  progress %d/%d (%.0fs)", i + 1, n, time.time() - t0)
    out = pd.DataFrame(enriched)
    log.info("enriched in %.1fs", time.time() - t0)

    total = len(out)
    delisted = int(out["delisted_mid_window"].sum())
    illiquid = int((out["avg_dollar_vol"] < LIQUIDITY_FLOOR).sum())
    log.info("total=%d delisted_mid_window=%d (%.2f%%) illiquid=%d (%.2f%%)",
             total, delisted, 100 * delisted / total,
             illiquid, 100 * illiquid / total)

    # Compute all variants for the two production scorers.
    variants = {
        "A_published": dict(realized_col="realized_ret", liquidity_floor=0,     tx_cost=0.0),
        "B_survivorship": dict(realized_col="realized_ret_surv", liquidity_floor=0,     tx_cost=0.0),
        "C_liquidity":   dict(realized_col="realized_ret",        liquidity_floor=LIQUIDITY_FLOOR, tx_cost=0.0),
        "D_tx_cost":     dict(realized_col="realized_ret",        liquidity_floor=0,     tx_cost=TX_COST),
        "E_all_three":   dict(realized_col="realized_ret_surv",    liquidity_floor=LIQUIDITY_FLOOR, tx_cost=TX_COST),
    }

    report = {
        "generated_at": int(time.time()),
        "universe_rows": total,
        "delisted_mid_window_count": delisted,
        "delisted_mid_window_pct": round(100 * delisted / total, 3),
        "illiquid_count_below_500k": illiquid,
        "illiquid_pct": round(100 * illiquid / total, 3),
        "liquidity_floor_usd": LIQUIDITY_FLOOR,
        "tx_cost_roundtrip": TX_COST,
        "delist_gap_days": DELIST_GAP_DAYS,
        "top_n": TOP_N,
        "threshold": THRESHOLD,
        "scorers": {},
    }

    for scorer in ("nn_score", "ensemble_score"):
        scorer_block: dict = {"variants": {}}
        for v_name, kw in variants.items():
            picks = pick_stats(out, scorer, top_n=TOP_N, **kw)
            baseline = universe_baseline(out,
                                          realized_col=kw["realized_col"],
                                          liquidity_floor=kw["liquidity_floor"],
                                          tx_cost=kw["tx_cost"])
            picks["baseline_hit_100"] = round(baseline, 4)
            picks["lift"] = round(picks["hit_100"] / baseline, 3) \
                if baseline > 0 and picks.get("n") else None
            scorer_block["variants"][v_name] = picks
            log.info("%s %-15s n=%s hit=%s mean=%s baseline=%s lift=%s",
                     scorer, v_name, picks.get("n"), picks.get("hit_100"),
                     picks.get("mean_return"), round(baseline, 4),
                     picks.get("lift"))
        report["scorers"][scorer] = scorer_block

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2))
    log.info("wrote %s", OUT)


if __name__ == "__main__":
    main()
