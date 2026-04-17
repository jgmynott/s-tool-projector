"""Price-derived features — 2026-04-17.

S-88 end-of-day plan item #4: 52w-high distance + sector/market-relative
momentum. Pure local computation from data_cache/prices/ CSVs. No API,
no new data source — just features we never tested as NN inputs.

For each (symbol, as_of) window in upside_hunt_results.csv, compute:
  - hi_52w_dist: (price_at_as_of - 52w_max) / 52w_max  (≤0; 0 = new high)
  - mom_30d, mom_60d, mom_90d: trailing returns
  - mom_rel_spy_30d: 30-day return minus SPY 30-day return (market-relative)
  - mom_rel_spy_90d: same at 90-day horizon

Then walk-forward train ExtraTrees on subsets:
  baseline (8), base + each momentum alone, base + all five.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("price-feats")

RESULTS_CSV = ROOT / "upside_hunt_results.csv"
PRICES_DIR = ROOT / "data_cache" / "prices"
OUTPUT_JSON = Path(__file__).parent / "price_features_results_2026_04_17.json"
TOP_N = 20
THRESHOLD = 1.0

PRICE_FIELDS = [
    "hi_52w_dist", "mom_30d", "mom_60d", "mom_90d",
    "mom_rel_spy_30d", "mom_rel_spy_90d",
]

BASE_FEATURES = [
    "log_price", "sigma", "p90_ratio", "p10_ratio", "asymmetry",
    "vol_low", "vol_hi", "H7_ewma_p90",
]


def load_prices(sym: str) -> pd.DataFrame | None:
    p = PRICES_DIR / f"{sym}.csv"
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p, usecols=["Date", "Close"])
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.sort_values("Date").set_index("Date")
        return df
    except Exception:
        return None


def price_at(df: pd.DataFrame, target: pd.Timestamp, lookback_days: int = 5):
    """Close on or just before `target`. None if no data in window."""
    if df is None or df.empty:
        return None
    sliced = df[df.index <= target]
    if sliced.empty:
        return None
    last = sliced.iloc[-1]
    if (target - sliced.index[-1]).days > lookback_days:
        return None
    return float(last["Close"])


def compute_price_feats(df: pd.DataFrame, as_of: pd.Timestamp,
                         spy_df: pd.DataFrame) -> dict:
    """All 6 price features for one (symbol, as_of). Zeros on missing data."""
    out = {f: 0.0 for f in PRICE_FIELDS}
    if df is None:
        return out
    p_now = price_at(df, as_of)
    if p_now is None or p_now <= 0:
        return out

    # 52-week high distance — max Close over the prior 252 trading days
    window = df[(df.index <= as_of) & (df.index >= as_of - pd.Timedelta(days=365))]
    if len(window) >= 30:  # need meaningful lookback
        p_max = float(window["Close"].max())
        if p_max > 0:
            out["hi_52w_dist"] = (p_now - p_max) / p_max

    for h in (30, 60, 90):
        p_then = price_at(df, as_of - pd.Timedelta(days=h), lookback_days=10)
        if p_then and p_then > 0:
            out[f"mom_{h}d"] = (p_now - p_then) / p_then

    # Market-relative: subtract SPY's trailing return at same horizon
    if spy_df is not None:
        spy_now = price_at(spy_df, as_of)
        if spy_now and spy_now > 0:
            for h in (30, 90):
                spy_then = price_at(spy_df, as_of - pd.Timedelta(days=h), 10)
                if spy_then and spy_then > 0:
                    spy_mom = (spy_now - spy_then) / spy_then
                    out[f"mom_rel_spy_{h}d"] = out[f"mom_{h}d"] - spy_mom

    # Clip to reasonable range so wild data doesn't blow up the NN
    for k in out:
        v = out[k]
        if not np.isfinite(v):
            out[k] = 0.0
        else:
            out[k] = float(max(-2.0, min(5.0, v)))
    return out


def augment(df: pd.DataFrame) -> pd.DataFrame:
    spy = load_prices("SPY")
    if spy is None:
        log.warning("SPY prices not cached — mom_rel_spy_* will be zero")

    # Cache per-symbol price frames once (expensive CSV read).
    symbols = df["symbol"].astype(str).str.upper().unique()
    price_cache: dict[str, pd.DataFrame | None] = {}
    for s in symbols:
        price_cache[s] = load_prices(s)
    log.info("Loaded price frames for %d symbols (%d cache misses)",
             len(symbols), sum(1 for v in price_cache.values() if v is None))

    rows = df.to_dict("records")
    out_cols: dict[str, list] = {f: [] for f in PRICE_FIELDS}
    cache: dict[tuple[str, str], dict] = {}
    for r in rows:
        sym = str(r["symbol"]).upper()
        as_of = pd.to_datetime(str(r["as_of"]))
        key = (sym, as_of.strftime("%Y-%m-%d"))
        if key not in cache:
            cache[key] = compute_price_feats(price_cache[sym], as_of, spy)
        for f in PRICE_FIELDS:
            out_cols[f].append(cache[key][f])
    for f in PRICE_FIELDS:
        df[f] = out_cols[f]
    nonzero = (df["mom_90d"] != 0).mean() * 100
    log.info("Coverage: %.1f%% rows have non-zero mom_90d", nonzero)
    return df


def build_base(df: pd.DataFrame) -> pd.DataFrame:
    f = df.copy()
    f["log_price"] = np.log(f["current"].clip(lower=0.01))
    f["p90_ratio"] = f["p90"] / f["current"].clip(lower=0.01)
    f["p10_ratio"] = f["p10"] / f["current"].clip(lower=0.01)
    f["asymmetry"] = (f["p90_ratio"] - 1) - (1 - f["p10_ratio"])
    f["vol_low"] = (f["sigma"] < 0.30).astype(int)
    f["vol_hi"] = (f["sigma"] > 0.60).astype(int)
    return f


def walk_forward(feat: pd.DataFrame, use: list[str]) -> dict:
    from sklearn.ensemble import ExtraTreesRegressor
    from sklearn.preprocessing import StandardScaler

    windows = sorted(feat["as_of"].unique())
    preds = np.zeros(len(feat))
    for i, w in enumerate(windows):
        if i == 0:
            continue
        tr = feat["as_of"].isin(windows[:i])
        te = feat["as_of"] == w
        X_tr = feat.loc[tr, use].values
        y_tr = np.clip(feat.loc[tr, "realized_ret"].values, -0.95, 5.0)
        X_te = feat.loc[te, use].values
        if len(X_tr) < 100 or len(X_te) == 0:
            continue
        s = StandardScaler()
        X_tr_s = s.fit_transform(X_tr); X_te_s = s.transform(X_te)
        m = ExtraTreesRegressor(n_estimators=300, max_depth=14,
                                min_samples_leaf=20, n_jobs=-1, random_state=42)
        m.fit(X_tr_s, y_tr)
        preds[te.values] = m.predict(X_te_s)
    scored = feat.copy()
    scored["score"] = preds
    picks = (scored.sort_values(["as_of", "score"], ascending=[True, False])
                   .groupby("as_of").head(TOP_N))
    n = len(picks)
    if n == 0:
        return {"hit_100": 0.0, "mean_return": 0.0, "n": 0}
    return {
        "hit_100": round(int((picks["realized_ret"] >= THRESHOLD).sum()) / n, 4),
        "mean_return": round(picks["realized_ret"].mean(), 4),
        "n": n,
    }


def main() -> None:
    df = pd.read_csv(RESULTS_CSV)
    log.info("Loaded %d rows", len(df))
    df = build_base(df)
    t0 = time.time()
    df = augment(df)
    log.info("Augmented with %d price features in %.1fs", len(PRICE_FIELDS), time.time() - t0)

    subsets = {
        "base_8":               BASE_FEATURES,
        "base_plus_all_price":  BASE_FEATURES + PRICE_FIELDS,
        "base_plus_52w":        BASE_FEATURES + ["hi_52w_dist"],
        "base_plus_mom_30":     BASE_FEATURES + ["mom_30d"],
        "base_plus_mom_90":     BASE_FEATURES + ["mom_90d"],
        "base_plus_mom_rel30":  BASE_FEATURES + ["mom_rel_spy_30d"],
        "base_plus_mom_rel90":  BASE_FEATURES + ["mom_rel_spy_90d"],
        "base_plus_52w_rel90":  BASE_FEATURES + ["hi_52w_dist", "mom_rel_spy_90d"],
        "lean_2_plus_price":    ["log_price", "sigma"] + PRICE_FIELDS,
    }

    results = {"generated_at": int(time.time()), "rows": len(df),
               "TOP_N": TOP_N, "THRESHOLD": THRESHOLD, "runs": {}}
    baseline = None
    for name, feats in subsets.items():
        t0 = time.time()
        r = walk_forward(df, feats)
        r["features"] = feats
        r["elapsed_s"] = round(time.time() - t0, 1)
        results["runs"][name] = r
        if baseline is None:
            baseline = r["hit_100"]
            log.info("[BASE] %-22s hit=%.4f mean=%+.3f n=%d (%.1fs)",
                     name, r["hit_100"], r["mean_return"], r["n"], r["elapsed_s"])
        else:
            d = r["hit_100"] - baseline
            ar = "UP" if d > 0 else ("DN" if d < 0 else "==")
            log.info("%s %+.4f  %-22s hit=%.4f mean=%+.3f (%.1fs)",
                     ar, d, name, r["hit_100"], r["mean_return"], r["elapsed_s"])

    OUTPUT_JSON.write_text(json.dumps(results, indent=2))
    log.info("Wrote -> %s", OUTPUT_JSON)


if __name__ == "__main__":
    main()
