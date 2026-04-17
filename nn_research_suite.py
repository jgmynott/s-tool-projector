"""
NN research suite — overnight investigation.

Goal: push beyond the 10.55x lift the basic MLP regressor achieved, by
exploring architecture, regularization, model class, calibration, and
threshold sensitivity. Produces research/nn_findings_<date>.md with
actionable findings for the next iteration.

Not intended to be part of the nightly production pipeline. Run
manually overnight, review outputs, then promote the winning config
into overnight_learn.py if it holds up.

Sections:

  1. Architecture sweep — MLP hidden layer sizes × alpha grid.
  2. Model-class comparison — MLP vs GradientBoostingRegressor vs
     RandomForestRegressor vs ExtraTreesRegressor.
  3. Calibration study — does the top-decile moonshot probability
     actually return more than the bottom-decile?
  4. Threshold ladder — hit rate at +100%, +200%, +300%, +500%.
  5. Per-window consistency — hit rate per window, not just pooled.
  6. Minimal-feature experiment — NN on {log_price, sigma} only, to
     quantify how much the hand-crafted features actually contribute.

All evaluations use walk-forward CV: predict window t using only
data from windows 1..t-1. Recent-4-window aggregation for headline
numbers so results reflect the current regime.
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
log = logging.getLogger("nn_research")

ROOT = Path(__file__).parent
RESULTS_CSV = ROOT / "upside_hunt_results.csv"
OUT_DIR = ROOT / "research"
OUT_DIR.mkdir(exist_ok=True)

FEATURES = [
    "log_price", "sigma", "p90_ratio", "p10_ratio", "asymmetry",
    "vol_low", "vol_hi", "H7_ewma_p90",
]
TOP_N = 20
THRESHOLDS = [1.0, 2.0, 3.0, 5.0]


def _build(df):
    f = df.copy()
    f["log_price"] = np.log(f["current"].clip(lower=0.01))
    f["p90_ratio"] = f["p90"] / f["current"].clip(lower=0.01)
    f["p10_ratio"] = f["p10"] / f["current"].clip(lower=0.01)
    f["asymmetry"] = (f["p90_ratio"] - 1) - (1 - f["p10_ratio"])
    f["vol_low"] = (f["sigma"] < 0.30).astype(int)
    f["vol_hi"] = (f["sigma"] > 0.60).astype(int)
    return f


def _walk_forward(feat, features, make_model, clip_target=True, seed=42):
    """Run walk-forward CV with any sklearn regressor factory. Returns
    (scored_df_with_score, per_window_hit_rates_dict)."""
    from sklearn.preprocessing import StandardScaler
    windows = sorted(feat["as_of"].unique())
    if len(windows) < 3:
        return None, None
    scores = np.zeros(len(feat))
    for i, w in enumerate(windows):
        if i == 0:
            continue
        train_mask = feat["as_of"].isin(windows[:i])
        test_mask = feat["as_of"] == w
        X_train = feat.loc[train_mask, features].values
        y_train = feat.loc[train_mask, "realized_ret"].values
        X_test = feat.loc[test_mask, features].values
        if len(X_train) < 100 or len(X_test) == 0:
            continue
        if clip_target:
            y_train = np.clip(y_train, -0.95, 5.0)
        scaler = StandardScaler()
        X_tr_s = scaler.fit_transform(X_train)
        X_te_s = scaler.transform(X_test)
        model = make_model(seed)
        model.fit(X_tr_s, y_train)
        scores[test_mask] = model.predict(X_te_s)
    scored = feat.assign(_score=scores - scores.min() + 0.01)
    return scored, windows


def _evaluate(scored, windows, threshold=1.0, recent_k=4):
    """Compute aggregate + per-window hit rates for the top-N by score."""
    recent = windows[-recent_k:]
    recent_df = scored[scored["as_of"].isin(recent)]
    picked = recent_df.sort_values(["as_of", "_score"], ascending=[True, False])
    picked = picked.groupby("as_of").head(TOP_N)
    if len(picked) == 0:
        return {"rate": 0, "mean": 0, "n": 0, "per_window": {}}
    agg = {
        "rate": float((picked["realized_ret"] >= threshold).mean()),
        "mean": float(picked["realized_ret"].mean()),
        "median": float(picked["realized_ret"].median()),
        "n": int(len(picked)),
        "per_window": {},
    }
    for w, g in picked.groupby("as_of"):
        agg["per_window"][str(w)] = {
            "rate": float((g["realized_ret"] >= threshold).mean()),
            "mean": float(g["realized_ret"].mean()),
            "n": int(len(g)),
        }
    return agg


def section_1_architecture_sweep(feat, windows):
    """MLP hidden-layer sizes × alpha grid."""
    from sklearn.neural_network import MLPRegressor
    configs = []
    for sizes in [(16, 8), (32, 16), (64, 32), (32, 16, 8)]:
        for alpha in [1e-4, 1e-3, 1e-2]:
            configs.append((sizes, alpha))
    results = {}
    for sizes, alpha in configs:
        key = f"mlp_{sizes}_a{alpha}"
        log.info("[arch] %s …", key)
        t0 = time.time()
        scored, _ = _walk_forward(
            feat, FEATURES,
            make_model=lambda seed, s=sizes, a=alpha: MLPRegressor(
                hidden_layer_sizes=s, activation="relu",
                max_iter=300, early_stopping=True, validation_fraction=0.15,
                random_state=seed, alpha=a,
            ),
        )
        r = _evaluate(scored, windows, threshold=1.0)
        r["elapsed_s"] = round(time.time() - t0, 1)
        r["config"] = {"sizes": list(sizes), "alpha": alpha}
        results[key] = r
        log.info("[arch] %s  rate=%.3f mean=%+.1f%% (%ss)",
                 key, r["rate"], r["mean"] * 100, r["elapsed_s"])
    # Rank by hit rate × mean.
    ranked = sorted(results.items(),
                    key=lambda kv: (kv[1]["rate"], kv[1]["mean"]),
                    reverse=True)
    return {"results": results, "ranking": [k for k, _ in ranked]}


def section_2_model_class_shootout(feat, windows):
    """MLP vs GradientBoosting vs RandomForest vs ExtraTrees."""
    from sklearn.neural_network import MLPRegressor
    from sklearn.ensemble import (GradientBoostingRegressor,
                                  RandomForestRegressor,
                                  ExtraTreesRegressor)

    def mlp_factory(seed):
        return MLPRegressor(hidden_layer_sizes=(32, 16), activation="relu",
                            max_iter=300, early_stopping=True,
                            validation_fraction=0.15,
                            random_state=seed, alpha=1e-3)

    def gb_factory(seed):
        return GradientBoostingRegressor(
            n_estimators=200, max_depth=4, learning_rate=0.05,
            subsample=0.8, random_state=seed,
        )

    def rf_factory(seed):
        return RandomForestRegressor(
            n_estimators=200, max_depth=10, min_samples_leaf=20,
            n_jobs=-1, random_state=seed,
        )

    def et_factory(seed):
        return ExtraTreesRegressor(
            n_estimators=300, max_depth=14, min_samples_leaf=10,
            n_jobs=-1, random_state=seed,
        )

    candidates = {"mlp": mlp_factory, "gbr": gb_factory,
                  "rf": rf_factory, "et": et_factory}
    results = {}
    for name, factory in candidates.items():
        log.info("[model] %s …", name)
        t0 = time.time()
        scored, _ = _walk_forward(feat, FEATURES, make_model=factory,
                                  clip_target=(name == "mlp"))
        r = _evaluate(scored, windows, threshold=1.0)
        r["elapsed_s"] = round(time.time() - t0, 1)
        results[name] = r
        log.info("[model] %s  rate=%.3f mean=%+.1f%% (%ss)",
                 name, r["rate"], r["mean"] * 100, r["elapsed_s"])
    ranked = sorted(results.items(),
                    key=lambda kv: (kv[1]["rate"], kv[1]["mean"]),
                    reverse=True)
    return {"results": results, "ranking": [k for k, _ in ranked]}


def section_3_calibration(feat, windows):
    """Train a moonshot classifier. Rank all picks by predicted moonshot
    probability. Bucket into deciles. Check realized hit rate per decile.
    If the model is well-calibrated, top decile should have far higher
    realized hit rate than bottom decile."""
    from sklearn.neural_network import MLPClassifier
    from sklearn.preprocessing import StandardScaler

    feat2 = feat.copy()
    feat2["moonshot_label"] = (feat2["realized_ret"] >= 1.0).astype(int)

    probs = np.zeros(len(feat2))
    for i, w in enumerate(windows):
        if i == 0:
            continue
        train_mask = feat2["as_of"].isin(windows[:i])
        test_mask = feat2["as_of"] == w
        X_train = feat2.loc[train_mask, FEATURES].values
        y_train = feat2.loc[train_mask, "moonshot_label"].values
        X_test = feat2.loc[test_mask, FEATURES].values
        if len(X_train) < 200 or y_train.sum() < 10 or len(X_test) == 0:
            continue
        pos_idx = np.where(y_train == 1)[0]
        neg_idx = np.where(y_train == 0)[0]
        if len(pos_idx) and len(neg_idx) > len(pos_idx):
            factor = len(neg_idx) // max(len(pos_idx), 1)
            idx = np.concatenate([neg_idx, np.tile(pos_idx, factor)])
            rng = np.random.default_rng(42)
            rng.shuffle(idx)
            X_train, y_train = X_train[idx], y_train[idx]
        scaler = StandardScaler()
        X_tr_s = scaler.fit_transform(X_train)
        X_te_s = scaler.transform(X_test)
        model = MLPClassifier(
            hidden_layer_sizes=(32, 16), activation="relu",
            max_iter=400, early_stopping=True, validation_fraction=0.15,
            random_state=42, alpha=1e-3,
        )
        model.fit(X_tr_s, y_train)
        p = model.predict_proba(X_te_s)
        pos_col = int(np.where(model.classes_ == 1)[0][0]) if 1 in model.classes_ else None
        if pos_col is not None:
            probs[test_mask] = p[:, pos_col]

    feat2["_prob"] = probs
    # Restrict to recent 4 windows for calibration study.
    recent = windows[-4:]
    recent_df = feat2[feat2["as_of"].isin(recent)].copy()
    if len(recent_df) == 0:
        return {}

    # Decile buckets by probability.
    recent_df["_decile"] = pd.qcut(recent_df["_prob"].rank(method="first"),
                                   10, labels=False, duplicates="drop")
    calib = {}
    for d in sorted(recent_df["_decile"].dropna().unique()):
        bucket = recent_df[recent_df["_decile"] == d]
        calib[f"decile_{int(d)+1}"] = {
            "n": int(len(bucket)),
            "mean_prob": float(bucket["_prob"].mean()),
            "realized_hit_100": float((bucket["realized_ret"] >= 1.0).mean()),
            "realized_hit_200": float((bucket["realized_ret"] >= 2.0).mean()),
            "realized_mean": float(bucket["realized_ret"].mean()),
        }
    # Spearman correlation between predicted and realized
    spearman = float(recent_df["_prob"].corr(
        recent_df["realized_ret"], method="spearman"))
    return {"deciles": calib, "spearman": spearman,
            "n_rows": int(len(recent_df))}


def section_4_threshold_ladder(feat, windows):
    """For each threshold in +100%, +200%, +300%, +500%, compute
    NN hit rate + baseline hit rate + lift."""
    from sklearn.neural_network import MLPRegressor

    def mlp(seed):
        return MLPRegressor(hidden_layer_sizes=(32, 16), activation="relu",
                            max_iter=300, early_stopping=True,
                            validation_fraction=0.15,
                            random_state=seed, alpha=1e-3)

    scored, _ = _walk_forward(feat, FEATURES, make_model=mlp)
    recent = windows[-4:]
    recent_df = scored[scored["as_of"].isin(recent)]
    picked = (recent_df.sort_values(["as_of", "_score"], ascending=[True, False])
                        .groupby("as_of").head(TOP_N))
    out = {}
    for t in THRESHOLDS:
        picked_rate = float((picked["realized_ret"] >= t).mean())
        base_rate = float((recent_df["realized_ret"] >= t).mean())
        out[f"+{int(t*100)}%"] = {
            "nn_rate": picked_rate,
            "baseline_rate": base_rate,
            "lift": picked_rate / base_rate if base_rate > 0 else 0,
            "n_picks": int(len(picked)),
        }
    return out


def section_5_per_window_consistency(feat, windows):
    """Does the NN win in every window, or is performance driven by
    one or two lucky windows?"""
    from sklearn.neural_network import MLPRegressor

    def mlp(seed):
        return MLPRegressor(hidden_layer_sizes=(32, 16), activation="relu",
                            max_iter=300, early_stopping=True,
                            validation_fraction=0.15,
                            random_state=seed, alpha=1e-3)

    scored, _ = _walk_forward(feat, FEATURES, make_model=mlp)
    out = {}
    for w in sorted(scored["as_of"].unique()):
        wdf = scored[scored["as_of"] == w]
        picked = wdf.nlargest(TOP_N, "_score")
        if len(picked) == 0:
            continue
        out[str(w)] = {
            "n_picks": int(len(picked)),
            "hit_100": float((picked["realized_ret"] >= 1.0).mean()),
            "hit_200": float((picked["realized_ret"] >= 2.0).mean()),
            "mean": float(picked["realized_ret"].mean()),
            "median": float(picked["realized_ret"].median()),
            "max": float(picked["realized_ret"].max()),
            "min": float(picked["realized_ret"].min()),
        }
    return out


def section_6_minimal_features(feat, windows):
    """How much of the NN's edge comes from just {log_price, sigma}?
    If a 2-feature model matches an 8-feature model, the other 6 are dead
    weight and we should replace them with new signal."""
    from sklearn.neural_network import MLPRegressor

    def mlp(seed):
        return MLPRegressor(hidden_layer_sizes=(32, 16), activation="relu",
                            max_iter=300, early_stopping=True,
                            validation_fraction=0.15,
                            random_state=seed, alpha=1e-3)

    tests = {
        "full_8": FEATURES,
        "minimal_2_logprice_sigma": ["log_price", "sigma"],
        "minimal_3_add_p90_ratio": ["log_price", "sigma", "p90_ratio"],
        "hand_crafted_only_5": ["p90_ratio", "p10_ratio", "asymmetry",
                                "vol_low", "vol_hi"],
    }
    out = {}
    for name, fs in tests.items():
        scored, _ = _walk_forward(feat, fs, make_model=mlp)
        r = _evaluate(scored, windows, threshold=1.0)
        r["features"] = fs
        out[name] = r
        log.info("[minimal] %s (%d feats)  rate=%.3f mean=%+.1f%%",
                 name, len(fs), r["rate"], r["mean"] * 100)
    return out


def compile_report(all_results, runtime_s):
    """Human-readable markdown summary."""
    from datetime import datetime
    date = datetime.now().strftime("%Y-%m-%d")
    md = [
        f"# NN research findings — {date}",
        "",
        f"Total runtime: **{runtime_s:.0f}s**",
        "",
        "## 1. Architecture sweep (MLP hidden-layer sizes × alpha)",
        "",
        "Walk-forward hit rate at +100% return, top-20 picks, recent-4 windows.",
        "",
        "| rank | config | hit rate | mean return | elapsed |",
        "|---|---|---|---|---|",
    ]
    arch = all_results.get("arch", {})
    for rank, k in enumerate(arch.get("ranking", []), 1):
        r = arch["results"][k]
        md.append(f"| {rank} | `{k}` | {r['rate']:.3f} | {r['mean']*100:+.1f}% | {r['elapsed_s']}s |")

    md += ["", "## 2. Model-class shootout",
           "",
           "Same features + walk-forward for each model family.",
           "",
           "| rank | model | hit rate | mean return | elapsed |",
           "|---|---|---|---|---|"]
    ms = all_results.get("models", {})
    for rank, k in enumerate(ms.get("ranking", []), 1):
        r = ms["results"][k]
        md.append(f"| {rank} | `{k}` | {r['rate']:.3f} | {r['mean']*100:+.1f}% | {r['elapsed_s']}s |")

    md += ["", "## 3. Moonshot-classifier calibration",
           "",
           "Bucket all recent-window picks into deciles by predicted +100% probability. A well-calibrated classifier has top decile >> bottom decile on realized hit rate.",
           ""]
    cal = all_results.get("calibration", {})
    if cal:
        md.append(f"Spearman(prob, realized_ret) = **{cal.get('spearman', 0):.3f}**  ·  n = {cal.get('n_rows', 0)}")
        md.append("")
        md.append("| decile | mean prob | realized +100% rate | realized +200% rate | mean return |")
        md.append("|---|---|---|---|---|")
        for k in sorted(cal["deciles"].keys(),
                        key=lambda s: int(s.split("_")[1]), reverse=True):
            d = cal["deciles"][k]
            md.append(f"| {k} | {d['mean_prob']:.3f} | {d['realized_hit_100']:.3f} | {d['realized_hit_200']:.3f} | {d['realized_mean']*100:+.1f}% |")

    md += ["", "## 4. Threshold ladder",
           "",
           "| threshold | NN hit rate | baseline | lift |",
           "|---|---|---|---|"]
    th = all_results.get("thresholds", {})
    for k, v in th.items():
        md.append(f"| {k} | {v['nn_rate']:.3f} | {v['baseline_rate']:.3f} | {v['lift']:.2f}x |")

    md += ["", "## 5. Per-window consistency",
           "",
           "| window | hit_100 | hit_200 | mean | median | max | min |",
           "|---|---|---|---|---|---|---|"]
    per_w = all_results.get("consistency", {})
    for w in sorted(per_w.keys()):
        d = per_w[w]
        md.append(f"| {w} | {d['hit_100']:.3f} | {d['hit_200']:.3f} | "
                  f"{d['mean']*100:+.1f}% | {d['median']*100:+.1f}% | "
                  f"{d['max']*100:+.1f}% | {d['min']*100:+.1f}% |")

    md += ["", "## 6. Minimal-feature experiment",
           "",
           "Does stripping features to the absolute minimum hurt performance?",
           "",
           "| feature set | n features | hit rate | mean return |",
           "|---|---|---|---|"]
    mf = all_results.get("minimal", {})
    for k, r in mf.items():
        md.append(f"| {k} | {len(r['features'])} | {r['rate']:.3f} | {r['mean']*100:+.1f}% |")

    md += ["",
           "## Notes",
           "",
           "- Hit rate = % of top-20 picks that reached the return threshold within 12 months of pick date.",
           "- All walk-forward: for each window t, train only on windows < t, score window t.",
           "- Recent-4 aggregation = windows with the most complete 12-month forward data + newest regime.",
           "- Baseline = universe-wide rate at same threshold (no selection).",
           "",
           ]
    return "\n".join(md)


def run():
    t_start = time.time()
    if not RESULTS_CSV.exists():
        log.error("%s missing", RESULTS_CSV); return 1
    df = pd.read_csv(RESULTS_CSV)
    feat = _build(df)
    windows = sorted(feat["as_of"].unique())
    log.info("Loaded %d rows across %d windows", len(feat), len(windows))

    results = {}
    log.info("=== Section 1: architecture sweep ===")
    results["arch"] = section_1_architecture_sweep(feat, windows)
    log.info("=== Section 2: model-class shootout ===")
    results["models"] = section_2_model_class_shootout(feat, windows)
    log.info("=== Section 3: calibration ===")
    results["calibration"] = section_3_calibration(feat, windows)
    log.info("=== Section 4: threshold ladder ===")
    results["thresholds"] = section_4_threshold_ladder(feat, windows)
    log.info("=== Section 5: per-window consistency ===")
    results["consistency"] = section_5_per_window_consistency(feat, windows)
    log.info("=== Section 6: minimal-feature experiment ===")
    results["minimal"] = section_6_minimal_features(feat, windows)

    runtime = time.time() - t_start
    from datetime import datetime
    date = datetime.now().strftime("%Y-%m-%d")
    (OUT_DIR / f"nn_findings_{date}.json").write_text(
        json.dumps(results, indent=2, default=str)
    )
    report = compile_report(results, runtime)
    (OUT_DIR / f"nn_findings_{date}.md").write_text(report)
    log.info("Wrote research report → research/nn_findings_%s.md (%.0fs)",
             date, runtime)
    return 0


if __name__ == "__main__":
    sys.exit(run())
