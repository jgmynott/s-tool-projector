"""
Horizon scan: where does the model's edge actually live?

The production backtest only measures 12-month outcomes. Day-trading is
a totally different alpha hunt — but before scoping any intraday data
infrastructure, we should answer the cheap version of the question
first using daily Close-to-Close at progressively shorter horizons:

  - If lift collapses to ~1.0× at 1–5d but is meaningful at 60–252d,
    the edge is a multi-week phenomenon. Day trading is a different
    research project, not an extension of this one.
  - If lift survives or even strengthens at short horizons, intraday
    data is the next investment.

Inputs:
  upside_hunt_scored.csv — 35 walk-forward windows × ~2000 tickers/window
  data_cache/prices/<SYM>.csv — daily OHLCV per ticker

Output: research/horizon_scan_results.json
  Per (scorer, horizon, threshold): top-20 hit rate, baseline hit
  rate, lift, mean return, n. Plus a one-line "where does the alpha
  live?" summary.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("horizon_scan")

ROOT = Path(__file__).parent.parent
SCORED_CSV = ROOT / "upside_hunt_scored.csv"
PRICES_DIR = ROOT / "data_cache" / "prices"
OUT_JSON = ROOT / "research" / "horizon_scan_results.json"

HORIZONS_DAYS = [1, 3, 5, 10, 20, 60, 126, 252]
THRESHOLDS = [0.02, 0.05, 0.10, 0.25, 0.50, 1.00]
TOP_N = 20

# Scorers to compare. Production picks come out of ensemble_score; the
# others are kept for context / sanity.
SCORE_COLUMNS = ["ensemble_score", "nn_score", "H7_ewma_p90", "H4_composite"]


def _load_price_close(sym: str) -> pd.Series | None:
    path = PRICES_DIR / f"{sym}.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
        s = df["Close"].dropna().sort_index()
        return s if len(s) > 0 else None
    except Exception:
        return None


def _realized_at_horizons(close: pd.Series, as_of: pd.Timestamp,
                          horizons: list[int]) -> dict[int, float | None]:
    """Return {horizon_days: pct_return} for each horizon, or None if
    the lookup falls outside the price series."""
    out: dict[int, float | None] = {h: None for h in horizons}
    if close.empty:
        return out
    # Find the entry close: most recent index <= as_of (so picks made
    # mid-day get the prior close, matching how a tomorrow-open trader
    # would have seen the universe).
    entry_loc = close.index.searchsorted(as_of, side="right") - 1
    if entry_loc < 0:
        return out
    entry_price = float(close.iloc[entry_loc])
    if entry_price <= 0 or not np.isfinite(entry_price):
        return out
    n = len(close)
    for h in horizons:
        target_loc = entry_loc + h
        if target_loc >= n:
            continue  # not enough forward data — leave as None
        exit_price = float(close.iloc[target_loc])
        if exit_price > 0 and np.isfinite(exit_price):
            out[h] = (exit_price - entry_price) / entry_price
    return out


def _hit_rate(returns: np.ndarray, threshold: float) -> dict:
    if len(returns) == 0:
        return {"n": 0, "hits": 0, "rate": 0.0}
    hits = int((returns >= threshold).sum())
    return {"n": int(len(returns)), "hits": hits, "rate": hits / len(returns)}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit-windows", type=int, default=None,
                   help="Use only the most recent N windows (faster smoke test).")
    args = p.parse_args()

    log.info("loading %s", SCORED_CSV)
    df = pd.read_csv(SCORED_CSV, parse_dates=["as_of"])
    log.info("rows=%d windows=%d", len(df), df["as_of"].nunique())

    if args.limit_windows:
        windows = sorted(df["as_of"].unique())[-args.limit_windows:]
        df = df[df["as_of"].isin(windows)]
        log.info("limited to %d most recent windows", args.limit_windows)

    # Load all prices upfront — symbols are reused across windows.
    syms_needed = sorted(df["symbol"].unique())
    log.info("loading prices for %d unique symbols", len(syms_needed))
    t0 = time.time()
    prices: dict[str, pd.Series] = {}
    missing = 0
    for s in syms_needed:
        c = _load_price_close(s)
        if c is None:
            missing += 1
        else:
            prices[s] = c
    log.info("loaded %d / %d in %.1fs (missing=%d)",
             len(prices), len(syms_needed), time.time() - t0, missing)

    # Compute realized returns at every horizon for every pick.
    log.info("computing realized returns at %d horizons...", len(HORIZONS_DAYS))
    t0 = time.time()
    horizon_returns: dict[int, list] = {h: [] for h in HORIZONS_DAYS}
    # Index aligned with df rows so we can attach back to score columns.
    realized_arr: dict[int, np.ndarray] = {h: np.full(len(df), np.nan) for h in HORIZONS_DAYS}
    for i, row in enumerate(df.itertuples(index=False)):
        sym = row.symbol
        as_of = row.as_of
        if sym not in prices:
            continue
        rs = _realized_at_horizons(prices[sym], as_of, HORIZONS_DAYS)
        for h, r in rs.items():
            if r is not None:
                realized_arr[h][i] = r
        if (i + 1) % 10000 == 0:
            log.info("  computed %d/%d (%.1fs)", i + 1, len(df), time.time() - t0)
    log.info("realized return computation: %.1fs", time.time() - t0)

    # Coverage per horizon — short horizons should hit ~100% coverage,
    # long horizons get truncated by the end of the price series.
    for h in HORIZONS_DAYS:
        valid = np.isfinite(realized_arr[h]).sum()
        log.info("  horizon %4dd: %5d / %5d valid (%.1f%%)",
                 h, valid, len(df), 100 * valid / len(df))

    # Universe baseline at each (horizon, threshold). This is the
    # rate at which any randomly-selected pick from the universe
    # cleared each threshold.
    baseline: dict[int, dict] = {}
    for h in HORIZONS_DAYS:
        rets = realized_arr[h][np.isfinite(realized_arr[h])]
        baseline[h] = {}
        for t in THRESHOLDS:
            baseline[h][f"+{int(t*100)}%"] = _hit_rate(rets, t)

    # Per-scorer top-20-per-window stats at each (horizon, threshold).
    methods: dict[str, dict] = {}
    for score_col in SCORE_COLUMNS:
        if score_col not in df.columns:
            log.warning("score column %s not in CSV — skipping", score_col)
            continue
        log.info("scoring with %s", score_col)
        # Within each window, take top-N by this score column.
        top = (
            df.assign(_row=np.arange(len(df)))
              .sort_values(["as_of", score_col], ascending=[True, False])
              .groupby("as_of")
              .head(TOP_N)
        )
        top_idx = top["_row"].to_numpy()

        method_block: dict = {"n_picks": int(len(top_idx)), "horizons": {}}
        for h in HORIZONS_DAYS:
            ret_at_h = realized_arr[h][top_idx]
            ret_at_h = ret_at_h[np.isfinite(ret_at_h)]
            block = {
                "n": int(len(ret_at_h)),
                "mean_return": float(ret_at_h.mean()) if len(ret_at_h) else 0.0,
                "median_return": float(np.median(ret_at_h)) if len(ret_at_h) else 0.0,
                "thresholds": {},
            }
            for t in THRESHOLDS:
                hr = _hit_rate(ret_at_h, t)
                base = baseline[h][f"+{int(t*100)}%"]
                lift = (hr["rate"] / base["rate"]) if base["rate"] > 0 else None
                block["thresholds"][f"+{int(t*100)}%"] = {
                    **hr,
                    "baseline_rate": base["rate"],
                    "lift": lift,
                }
            method_block["horizons"][f"{h}d"] = block
        methods[score_col] = method_block

    # Where does the alpha live? Use the +10% threshold lift as the
    # canonical headline metric across horizons (high enough to show
    # selection, low enough to be measurable at short horizons).
    primary = "ensemble_score" if "ensemble_score" in methods else next(iter(methods))
    horizon_lifts = {}
    for h in HORIZONS_DAYS:
        cell = methods.get(primary, {}).get("horizons", {}).get(f"{h}d", {})
        lift = cell.get("thresholds", {}).get("+10%", {}).get("lift")
        horizon_lifts[h] = lift

    payload = {
        "generated_at": time.time(),
        "scored_csv": str(SCORED_CSV.relative_to(ROOT)),
        "n_rows": int(len(df)),
        "n_windows": int(df["as_of"].nunique()),
        "horizons_days": HORIZONS_DAYS,
        "thresholds": [f"+{int(t*100)}%" for t in THRESHOLDS],
        "primary_scorer": primary,
        "horizon_lift_at_+10%": {f"{h}d": horizon_lifts[h] for h in HORIZONS_DAYS},
        "baseline": baseline,
        "methods": methods,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2, default=str))
    log.info("wrote %s", OUT_JSON)

    # Print the "where does alpha live" headline so you can see the
    # answer without opening the JSON.
    print("\n=== Horizon scan: ensemble_score top-20, +10% threshold ===")
    print(f"{'horizon':>10}  {'top-20 hit':>12}  {'baseline':>10}  {'lift':>8}  {'mean ret':>10}")
    for h in HORIZONS_DAYS:
        cell = methods[primary]["horizons"][f"{h}d"]
        thr = cell["thresholds"]["+10%"]
        lift_s = f"{thr['lift']:.2f}x" if thr['lift'] else "—"
        print(f"{h:>8}d   {thr['rate']*100:>10.1f}%   {thr['baseline_rate']*100:>8.1f}%   {lift_s:>8}   {cell['mean_return']*100:>+8.1f}%")
    print()


if __name__ == "__main__":
    sys.exit(main() or 0)
