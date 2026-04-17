"""ExtraTrees hyperparameter sweep — 2026-04-17.

Lean-features angle was killed (null on production's ExtraTrees).
Turning attention to whether production's hyperparameters are optimal.
Currently: n_estimators=300, max_depth=14, min_samples_leaf=20.

Sweep ~15 configurations around that point. All 8 features (production
feature set). Walk-forward setup unchanged.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hp-sweep")

RESULTS_CSV = ROOT / "upside_hunt_results.csv"
OUTPUT_JSON = Path(__file__).parent / "extratrees_hp_results_2026_04_17.json"
TOP_N = 20
THRESHOLD = 1.0

FEATURES = [
    "log_price", "sigma", "p90_ratio", "p10_ratio", "asymmetry",
    "vol_low", "vol_hi", "H7_ewma_p90",
]


def build(df: pd.DataFrame) -> pd.DataFrame:
    f = df.copy()
    f["log_price"] = np.log(f["current"].clip(lower=0.01))
    f["p90_ratio"] = f["p90"] / f["current"].clip(lower=0.01)
    f["p10_ratio"] = f["p10"] / f["current"].clip(lower=0.01)
    f["asymmetry"] = (f["p90_ratio"] - 1) - (1 - f["p10_ratio"])
    f["vol_low"] = (f["sigma"] < 0.30).astype(int)
    f["vol_hi"] = (f["sigma"] > 0.60).astype(int)
    return f


def evaluate(feat: pd.DataFrame, hp: dict) -> dict:
    from sklearn.ensemble import ExtraTreesRegressor
    from sklearn.preprocessing import StandardScaler

    windows = sorted(feat["as_of"].unique())
    preds = np.zeros(len(feat))
    for i, w in enumerate(windows):
        if i == 0:
            continue
        train = feat["as_of"].isin(windows[:i])
        test = feat["as_of"] == w
        X_tr = feat.loc[train, FEATURES].values
        y_tr = np.clip(feat.loc[train, "realized_ret"].values, -0.95, 5.0)
        X_te = feat.loc[test, FEATURES].values
        if len(X_tr) < 100 or len(X_te) == 0:
            continue
        s = StandardScaler()
        X_tr_s = s.fit_transform(X_tr); X_te_s = s.transform(X_te)
        m = ExtraTreesRegressor(**hp, n_jobs=-1, random_state=42)
        m.fit(X_tr_s, y_tr)
        preds[test.values] = m.predict(X_te_s)
    feat = feat.copy()
    feat["score"] = preds
    picks = (feat.sort_values(["as_of", "score"], ascending=[True, False])
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
    feat = build(df)

    # Production: n_estimators=300, max_depth=14, min_samples_leaf=20.
    # Sweep around it, plus a couple aggressive-regularisation tests.
    grid = {
        "n_estimators": [100, 300, 500, 1000],
        "max_depth":    [8, 14, 20, None],
        "min_samples_leaf": [5, 20, 50],
    }
    # Full grid = 4×4×3 = 48. Trim to interesting combinations to stay fast.
    candidates = [
        # production baseline first
        dict(n_estimators=300, max_depth=14, min_samples_leaf=20),
        # vary one axis at a time
        dict(n_estimators=100, max_depth=14, min_samples_leaf=20),
        dict(n_estimators=500, max_depth=14, min_samples_leaf=20),
        dict(n_estimators=1000, max_depth=14, min_samples_leaf=20),
        dict(n_estimators=300, max_depth=8,  min_samples_leaf=20),
        dict(n_estimators=300, max_depth=20, min_samples_leaf=20),
        dict(n_estimators=300, max_depth=None, min_samples_leaf=20),
        dict(n_estimators=300, max_depth=14, min_samples_leaf=5),
        dict(n_estimators=300, max_depth=14, min_samples_leaf=50),
        # a few combined variants
        dict(n_estimators=500, max_depth=20, min_samples_leaf=5),
        dict(n_estimators=1000, max_depth=8, min_samples_leaf=50),
        dict(n_estimators=500, max_depth=None, min_samples_leaf=10),
    ]

    results = {"generated_at": int(time.time()), "features": FEATURES,
               "TOP_N": TOP_N, "THRESHOLD": THRESHOLD, "runs": []}
    baseline = None
    for hp in candidates:
        t0 = time.time()
        r = evaluate(feat, hp)
        elapsed = round(time.time() - t0, 1)
        r["hp"] = hp
        r["elapsed_s"] = elapsed
        results["runs"].append(r)
        tag = "[BASE]" if baseline is None else ""
        if baseline is None:
            baseline = r["hit_100"]
            log.info("%s hp=%s hit=%.4f mean=%+.3f (%.1fs)",
                     tag, hp, r["hit_100"], r["mean_return"], elapsed)
        else:
            d = r["hit_100"] - baseline
            ar = "UP" if d > 0 else ("DN" if d < 0 else "==")
            log.info("%s %+.4f hp=%s hit=%.4f mean=%+.3f (%.1fs)",
                     ar, d, hp, r["hit_100"], r["mean_return"], elapsed)

    OUTPUT_JSON.write_text(json.dumps(results, indent=2))
    log.info("Wrote -> %s", OUTPUT_JSON)


if __name__ == "__main__":
    main()
