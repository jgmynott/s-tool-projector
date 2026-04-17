"""SEC features at +25% threshold — 2026-04-17.

At +100% hit rate, SEC features were null. Hypothesis: fundamentals
signal long-term value, not explosive upside. The +100% metric only
catches speculative/catalyst-driven moves where fundamentals don't
apply.

Testing the same experiment at +25% threshold — a "fair-value
convergence" metric where a margin-expanding / deleveraging name
reasonably rises ~25% as the market catches up. If SEC features
matter anywhere, they matter here.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Re-use the 2026-04-17 SEC experiment's helpers.
sys.path.insert(0, str(Path(__file__).parent))
from experiment_sec_features_2026_04_17 import (  # noqa: E402
    ensure_local_fundamentals_db,
    augment_with_sec,
    build_base_features,
    BASE_FEATURES,
    SEC_FIELDS,
    RESULTS_CSV,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sec-25pct")

OUTPUT_JSON = Path(__file__).parent / "sec_features_25pct_results_2026_04_17.json"
TOP_N = 20
THRESHOLD = 0.25   # +25% instead of +100%


def walk_forward(feat: pd.DataFrame, use_features: list[str]) -> dict:
    from sklearn.ensemble import ExtraTreesRegressor
    from sklearn.preprocessing import StandardScaler

    windows = sorted(feat["as_of"].unique())
    preds = np.zeros(len(feat))
    for i, w in enumerate(windows):
        if i == 0:
            continue
        tr = feat["as_of"].isin(windows[:i])
        te = feat["as_of"] == w
        X_tr = feat.loc[tr, use_features].values
        y_tr = np.clip(feat.loc[tr, "realized_ret"].values, -0.95, 5.0)
        X_te = feat.loc[te, use_features].values
        if len(X_tr) < 100 or len(X_te) == 0:
            continue
        s = StandardScaler()
        X_tr_s = s.fit_transform(X_tr)
        X_te_s = s.transform(X_te)
        m = ExtraTreesRegressor(n_estimators=300, max_depth=14, min_samples_leaf=20,
                                n_jobs=-1, random_state=42)
        m.fit(X_tr_s, y_tr)
        preds[te.values] = m.predict(X_te_s)

    scored = feat.copy()
    scored["score"] = preds
    picks = (scored.sort_values(["as_of", "score"], ascending=[True, False])
                   .groupby("as_of").head(TOP_N))
    n = len(picks)
    if n == 0:
        return {"hit_25": 0.0, "mean_return": 0.0, "n": 0}
    return {
        "hit_25": round(int((picks["realized_ret"] >= THRESHOLD).sum()) / n, 4),
        "mean_return": round(picks["realized_ret"].mean(), 4),
        "n": n,
    }


def main() -> None:
    conn = ensure_local_fundamentals_db()
    df = pd.read_csv(RESULTS_CSV)
    log.info("Loaded %d rows", len(df))
    df = build_base_features(df)
    df = augment_with_sec(df, conn)

    subsets = {
        "base_8":             BASE_FEATURES,
        "base_plus_all_sec":  BASE_FEATURES + SEC_FIELDS,
        "base_plus_growth":   BASE_FEATURES + ["revenue_yoy_growth"],
        "base_plus_margins":  BASE_FEATURES + ["gross_margin", "operating_margin"],
        "base_plus_fcf":      BASE_FEATURES + ["fcf_to_revenue"],
        "base_plus_debt":     BASE_FEATURES + ["net_debt_change_pct"],
        "base_plus_quality":  BASE_FEATURES + ["operating_margin", "fcf_to_revenue", "net_debt_change_pct"],
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
            baseline = r["hit_25"]
            log.info("[BASE] %-22s hit_25=%.4f mean=%+.3f n=%d (%.1fs)",
                     name, r["hit_25"], r["mean_return"], r["n"], r["elapsed_s"])
        else:
            d = r["hit_25"] - baseline
            ar = "UP" if d > 0 else ("DN" if d < 0 else "==")
            log.info("%s %+.4f  %-22s hit_25=%.4f mean=%+.3f (%.1fs)",
                     ar, d, name, r["hit_25"], r["mean_return"], r["elapsed_s"])

    OUTPUT_JSON.write_text(json.dumps(results, indent=2))
    log.info("Wrote -> %s", OUTPUT_JSON)


if __name__ == "__main__":
    main()
