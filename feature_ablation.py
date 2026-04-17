"""
Feature ablation study.

For the asymmetric NN regressor, train a fresh model with ONE feature
removed at a time and measure the drop in top-20 +100% hit rate on
recent windows. Features whose removal tanks the score are load-bearing;
features whose removal doesn't move the score are dead weight and
candidates for replacement with something more predictive.

Outputs data_cache/feature_importance.json — informs what features to
keep, what to drop, and where to invest in new signal sourcing (SEC
fundamentals, FINRA short interest, catalyst features).

Runtime: ~90 seconds. Runs as part of the nightly pipeline after
overnight_learn.py.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("feature_ablation")

ROOT = Path(__file__).parent
# Ablation trains fresh NNs with one feature dropped — it needs the raw
# engineered features (current, p10, p90, sigma). Always use the raw
# upside_hunt results CSV, which has every column. The scored CSV only
# carries the NN outputs and isn't self-contained.
RESULTS_CSV = ROOT / "upside_hunt_results.csv"
OUT_JSON = ROOT / "data_cache" / "feature_importance.json"

FEATURES = [
    "log_price", "sigma", "p90_ratio", "p10_ratio", "asymmetry",
    "vol_low", "vol_hi", "H7_ewma_p90",
]
TOP_N = 20


def _build(df: pd.DataFrame) -> pd.DataFrame:
    f = df.copy()
    f["log_price"] = np.log(f["current"].clip(lower=0.01))
    f["p90_ratio"] = f["p90"] / f["current"].clip(lower=0.01)
    f["p10_ratio"] = f["p10"] / f["current"].clip(lower=0.01)
    f["asymmetry"] = (f["p90_ratio"] - 1) - (1 - f["p10_ratio"])
    f["vol_low"] = (f["sigma"] < 0.30).astype(int)
    f["vol_hi"] = (f["sigma"] > 0.60).astype(int)
    return f


def _walk_forward_hit_rate(feat: pd.DataFrame, use_features: list[str]) -> dict:
    from sklearn.neural_network import MLPRegressor
    from sklearn.preprocessing import StandardScaler

    windows = sorted(feat["as_of"].unique())
    if len(windows) < 3:
        return {"rate_100": 0.0, "mean_return": 0.0, "n": 0}

    preds = np.zeros(len(feat))
    for i, w in enumerate(windows):
        if i == 0:
            continue
        train_mask = feat["as_of"].isin(windows[:i])
        test_mask = feat["as_of"] == w
        X_train = feat.loc[train_mask, use_features].values
        y_train = np.clip(feat.loc[train_mask, "realized_ret"].values, -0.95, 5.0)
        X_test = feat.loc[test_mask, use_features].values
        if len(X_train) < 100 or len(X_test) == 0:
            continue
        scaler = StandardScaler()
        X_tr_s = scaler.fit_transform(X_train)
        X_te_s = scaler.transform(X_test)
        model = MLPRegressor(
            hidden_layer_sizes=(32, 16), activation="relu",
            max_iter=300, early_stopping=True, validation_fraction=0.15,
            random_state=42, alpha=1e-3,
        )
        model.fit(X_tr_s, y_train)
        preds[test_mask] = model.predict(X_te_s)

    preds = preds - preds.min() + 0.01
    feat = feat.assign(_score=preds)
    # Evaluate on the most recent 4 windows.
    recent = windows[-4:]
    recent_df = feat[feat["as_of"].isin(recent)]
    if len(recent_df) == 0:
        return {"rate_100": 0.0, "mean_return": 0.0, "n": 0}
    picked = recent_df.sort_values(["as_of", "_score"], ascending=[True, False])
    picked = picked.groupby("as_of").head(TOP_N)
    if len(picked) == 0:
        return {"rate_100": 0.0, "mean_return": 0.0, "n": 0}
    return {
        "rate_100": float((picked["realized_ret"] >= 1.0).mean()),
        "mean_return": float(picked["realized_ret"].mean()),
        "n": int(len(picked)),
    }


def run():
    if not RESULTS_CSV.exists():
        log.error("No raw results CSV at %s — run upside_hunt.py first", RESULTS_CSV)
        return 1
    df = pd.read_csv(RESULTS_CSV)
    feat = _build(df)
    log.info("Loaded %d rows; running ablation across %d features",
             len(feat), len(FEATURES))

    # Baseline: all features.
    t0 = time.time()
    baseline = _walk_forward_hit_rate(feat, FEATURES)
    log.info("Baseline (all features): rate_100=%.3f mean=%+.1f%% n=%d (%.1fs)",
             baseline["rate_100"], baseline["mean_return"] * 100,
             baseline["n"], time.time() - t0)

    # Ablate: remove one feature at a time.
    ablation = {}
    for f in FEATURES:
        remaining = [x for x in FEATURES if x != f]
        t0 = time.time()
        result = _walk_forward_hit_rate(feat, remaining)
        delta_rate = result["rate_100"] - baseline["rate_100"]
        delta_mean = result["mean_return"] - baseline["mean_return"]
        ablation[f] = {
            "without_feature": result,
            "delta_rate_100": delta_rate,
            "delta_mean_return": delta_mean,
            # Positive importance = removal hurts performance = feature helps.
            "importance_rate": -delta_rate,
            "importance_mean": -delta_mean,
        }
        log.info("  -%-14s: rate_100=%.3f (Δ%+.3f) mean=%+.1f%% (Δ%+.1f%%) %.1fs",
                 f, result["rate_100"], delta_rate,
                 result["mean_return"] * 100, delta_mean * 100,
                 time.time() - t0)

    # Rank features by importance (higher = more critical).
    ranked = sorted(ablation.items(), key=lambda kv: kv[1]["importance_rate"], reverse=True)
    log.info("Feature importance ranking (by rate_100 drop when removed):")
    for f, a in ranked:
        log.info("  %-14s  importance=%+.3f", f, a["importance_rate"])

    report = {
        "generated_at": time.time(),
        "baseline": baseline,
        "features": FEATURES,
        "ablation": ablation,
        "ranking": [f for f, _ in ranked],
        "notes": [
            "Trains an NN with ONE feature removed at a time. Delta is the drop in top-20 +100% hit rate on the recent windows.",
            "Positive importance = removing the feature hurts performance (the feature is load-bearing).",
            "Near-zero or negative importance = the feature is redundant or adds noise. Candidates for replacement with better signals.",
            "Walk-forward discipline preserved — no test-set leakage.",
        ],
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2, default=str))
    log.info("Wrote feature importance → %s", OUT_JSON)
    return 0


if __name__ == "__main__":
    sys.exit(run())
