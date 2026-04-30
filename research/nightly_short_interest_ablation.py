"""Nightly short-interest ablation.

Runs each night inside the slow workflow. Joins FINRA short-interest
snapshots to upside_hunt_results.csv and reports whether
days_to_cover, short_pct_change, and short_qty_per_volume improve
hit_100 vs the 8-feature baseline.

Writes results to runtime_data/short_interest_ablation_YYYYMMDD.json
so seven overnight runs produce a time-series of deltas we can read
at the end of the week without remote-SSH-ing into the pipeline.

Runtime budget: ~90 seconds on a warm cache. Safe to gate with
`|| true` in the workflow so a bad run doesn't break the deploy.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import signals_short_interest_yf as si  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("si-ablation")

# Canonical 67k-row, 35-window CSV lives in runtime_data (committed to repo
# so cron has access). Falls back to root for local dev workflows that
# regenerate via upside_hunt.py without copying to runtime_data.
RUNTIME_CSV = ROOT / "runtime_data" / "upside_hunt_results.csv"
ROOT_CSV    = ROOT / "upside_hunt_results.csv"
RESULTS_CSV = RUNTIME_CSV if RUNTIME_CSV.exists() else ROOT_CSV
OUT_DIR     = ROOT / "runtime_data"
TOP_N = 20
THRESHOLD = 1.0

BASE_FEATURES = [
    "log_price", "sigma", "p90_ratio", "p10_ratio", "asymmetry",
    "vol_low", "vol_hi", "H7_ewma_p90",
]

SI_FIELDS = ["si_dtc", "si_chg_pct", "si_pct_float"]


def load_short_interest() -> pd.DataFrame:
    """Load short-interest history from the yfinance-sourced table.
    Derives three features per (symbol, snapshot_date):
      si_dtc           = shortRatio (days to cover)
      si_chg_pct       = (shares_short - prior_month) / prior_month
      si_pct_float     = shortPercentOfFloat (0..1)
    """
    conn = sqlite3.connect(str(si.DB_PATH))
    try:
        df = pd.read_sql(
            """SELECT symbol, short_snapshot_date, short_ratio,
                      shares_short, shares_short_prior_month,
                      short_pct_of_float
               FROM short_interest_yf
               ORDER BY symbol, short_snapshot_date""",
            conn,
        )
    except Exception as e:
        log.warning("short_interest_yf table unavailable: %s", e)
        return pd.DataFrame()
    conn.close()
    if df.empty:
        return df
    df["settlement_date"] = pd.to_datetime(df["short_snapshot_date"])
    df["si_dtc"] = df["short_ratio"]
    df["si_chg_pct"] = df.apply(
        lambda r: ((r["shares_short"] - r["shares_short_prior_month"])
                    / r["shares_short_prior_month"])
                   if (r.get("shares_short_prior_month") and
                       r.get("shares_short_prior_month") != 0)
                   else None,
        axis=1,
    )
    df["si_pct_float"] = df["short_pct_of_float"]
    return df[["symbol", "settlement_date"] + SI_FIELDS]


def attach_si(df: pd.DataFrame, si_df: pd.DataFrame) -> pd.DataFrame:
    """For each (symbol, as_of), find the short-interest snapshot most
    recently published BEFORE as_of. as-of join, no lookahead."""
    if si_df.empty:
        for c in SI_FIELDS:
            df[c] = 0.0
        return df
    df = df.copy()
    df["as_of_ts"] = pd.to_datetime(df["as_of"])
    df = df.sort_values("as_of_ts").reset_index(drop=True)
    si_df = si_df.sort_values("settlement_date").reset_index(drop=True)

    joined_parts = []
    for sym, group in df.groupby("symbol"):
        sub_si = si_df[si_df["symbol"] == sym]
        if sub_si.empty:
            for c in SI_FIELDS:
                group[c] = 0.0
            joined_parts.append(group)
            continue
        merged = pd.merge_asof(
            group.sort_values("as_of_ts"),
            sub_si.rename(columns={"settlement_date": "si_date"}),
            left_on="as_of_ts",
            right_on="si_date",
            direction="backward",
            tolerance=pd.Timedelta(days=45),
            suffixes=("", "_dup"),
        )
        joined_parts.append(merged)
    out = pd.concat(joined_parts, ignore_index=True)
    for c in SI_FIELDS:
        if c not in out.columns:
            out[c] = 0.0
        out[c] = out[c].fillna(0.0).astype(float)
    return out


def build_base_feats(df: pd.DataFrame) -> pd.DataFrame:
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
    if len(windows) < 3:
        return {"hit_100": 0.0, "mean_return": 0.0, "n": 0}

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
        X_tr_s = s.fit_transform(X_tr)
        X_te_s = s.transform(X_te)
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
    if not RESULTS_CSV.exists():
        log.warning("upside_hunt_results.csv missing — skipping")
        return
    df = pd.read_csv(RESULTS_CSV)
    log.info("loaded %d upside_hunt rows", len(df))

    si_df = load_short_interest()
    log.info("loaded %d short-interest rows across %d symbols",
             len(si_df), si_df["symbol"].nunique() if not si_df.empty else 0)

    feat = build_base_feats(df)
    feat = attach_si(feat, si_df)
    coverage = (feat["si_dtc"] != 0).mean() * 100
    log.info("SI coverage: %.1f%% of rows have non-zero days_to_cover", coverage)

    subsets = {
        "base_8":              BASE_FEATURES,
        "base_plus_all_si":    BASE_FEATURES + SI_FIELDS,
        "base_plus_dtc":       BASE_FEATURES + ["si_dtc"],
        "base_plus_chg":       BASE_FEATURES + ["si_chg_pct"],
        "base_plus_pct_float": BASE_FEATURES + ["si_pct_float"],
    }

    results = {
        "generated_at": int(time.time()),
        "run_date": date.today().isoformat(),
        "rows": len(feat),
        "si_coverage_pct": round(coverage, 1),
        "si_rows_loaded": len(si_df),
        "si_distinct_symbols": int(si_df["symbol"].nunique()) if not si_df.empty else 0,
        "runs": {},
    }
    baseline = None
    for name, feats in subsets.items():
        t0 = time.time()
        r = walk_forward(feat, feats)
        r["features"] = feats
        r["elapsed_s"] = round(time.time() - t0, 1)
        results["runs"][name] = r
        if baseline is None:
            baseline = r["hit_100"]
            log.info("[BASE] %-18s hit=%.4f mean=%+.3f n=%d (%.1fs)",
                     name, r["hit_100"], r["mean_return"], r["n"], r["elapsed_s"])
        else:
            d = r["hit_100"] - baseline
            arrow = "UP" if d > 0 else ("DN" if d < 0 else "==")
            log.info("%s %+.4f  %-18s hit=%.4f mean=%+.3f (%.1fs)",
                     arrow, d, name, r["hit_100"], r["mean_return"], r["elapsed_s"])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"short_interest_ablation_{date.today().isoformat()}.json"
    out_path.write_text(json.dumps(results, indent=2))
    log.info("wrote %s", out_path)
    # Also update a "latest" pointer so UI/devs can grab it without
    # knowing the date.
    (OUT_DIR / "short_interest_ablation_latest.json").write_text(
        json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
