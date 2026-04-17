"""Lean-NN experiment — 2026-04-17.

Follow-up to feature_ablation.py. Ablation showed 6 of 8 features in
the production NN have zero or negative importance. This sweep trains
a fresh MLP (same config as feature_ablation for comparability) on
various subsets and reports top-20 +100% hit rate.

Hypothesis: the "drop_bottom_4" subset (log_price, sigma, p10_ratio,
vol_low) outperforms the 8-feature baseline on hit_100.

Runtime: ~5 minutes for 9 subsets. Writes research/lean_nn_results_*.json.
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
log = logging.getLogger("lean-nn")

# Same data source and feature derivation as feature_ablation.py for
# apples-to-apples comparison with that study's 0.625 baseline.
RESULTS_CSV = ROOT / "upside_hunt_results.csv"
OUTPUT_JSON = Path(__file__).parent / "lean_nn_results_2026_04_17.json"
TOP_N = 20
THRESHOLD = 1.0


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    f = df.copy()
    f["log_price"] = np.log(f["current"].clip(lower=0.01))
    f["p90_ratio"] = f["p90"] / f["current"].clip(lower=0.01)
    f["p10_ratio"] = f["p10"] / f["current"].clip(lower=0.01)
    f["asymmetry"] = (f["p90_ratio"] - 1) - (1 - f["p10_ratio"])
    f["vol_low"] = (f["sigma"] < 0.30).astype(int)
    f["vol_hi"] = (f["sigma"] > 0.60).astype(int)
    return f


def walk_forward(feat: pd.DataFrame, use_features: list[str]) -> dict:
    from sklearn.neural_network import MLPRegressor
    from sklearn.preprocessing import StandardScaler

    windows = sorted(feat["as_of"].unique())
    if len(windows) < 3:
        return {"hit_100": 0.0, "mean_return": 0.0, "n": 0}

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
            hidden_layer_sizes=(32, 16),
            max_iter=400,
            early_stopping=True,
            random_state=42,
        )
        model.fit(X_tr_s, y_train)
        preds[test_mask.values] = model.predict(X_te_s)

    feat = feat.copy()
    feat["score"] = preds
    picks = (feat.sort_values(["as_of", "score"], ascending=[True, False])
                 .groupby("as_of").head(TOP_N))
    if len(picks) == 0:
        return {"hit_100": 0.0, "mean_return": 0.0, "n": 0}
    n = len(picks)
    hits = int((picks["realized_ret"] >= THRESHOLD).sum())
    return {
        "hit_100": round(hits / n, 4),
        "mean_return": round(picks["realized_ret"].mean(), 4),
        "n": n,
    }


def main() -> None:
    if not RESULTS_CSV.exists():
        log.error("upside_hunt_results.csv not found at %s", RESULTS_CSV)
        return
    df = pd.read_csv(RESULTS_CSV)
    log.info("Loaded %d rows from %s", len(df), RESULTS_CSV.name)
    feat = build_features(df)

    ALL_FEATURES = [
        "log_price", "sigma", "p90_ratio", "p10_ratio", "asymmetry",
        "vol_low", "vol_hi", "H7_ewma_p90",
    ]

    subsets = {
        "8_baseline":          ALL_FEATURES,
        "lean_2":              ["log_price", "sigma"],
        "lean_3_top":          ["log_price", "sigma", "p10_ratio"],
        "lean_4_top":          ["log_price", "sigma", "p10_ratio", "vol_low"],
        "log_price_only":      ["log_price"],
        "sigma_only":          ["sigma"],
        "lean_plus_H7":        ["log_price", "sigma", "H7_ewma_p90"],
        "drop_p90_ratio":      [f for f in ALL_FEATURES if f != "p90_ratio"],
        "drop_bottom_2":       [f for f in ALL_FEATURES if f not in ("p90_ratio", "asymmetry")],
    }

    results = {"generated_at": int(time.time()), "rows": len(df),
               "TOP_N": TOP_N, "THRESHOLD": THRESHOLD, "runs": {}}
    baseline_hit = None
    for name, feats in subsets.items():
        t0 = time.time()
        r = walk_forward(feat, feats)
        r["features"] = feats
        r["elapsed_s"] = round(time.time() - t0, 1)
        results["runs"][name] = r
        if name == "8_baseline":
            baseline_hit = r["hit_100"]
            log.info("[BASELINE] %-22s hit=%.4f mean=%+.3f n=%d (%.1fs)",
                     name, r["hit_100"], r["mean_return"], r["n"], r["elapsed_s"])
        else:
            delta = r["hit_100"] - baseline_hit if baseline_hit is not None else 0
            arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "=")
            log.info("%s %+.4f  %-22s hit=%.4f mean=%+.3f (%.1fs)",
                     arrow, delta, name, r["hit_100"], r["mean_return"], r["elapsed_s"])

    OUTPUT_JSON.write_text(json.dumps(results, indent=2))
    log.info("Wrote → %s", OUTPUT_JSON)


if __name__ == "__main__":
    main()
