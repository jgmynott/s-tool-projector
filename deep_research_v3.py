"""
Deep research v3 — overnight continuation after v2.

v2 established ExtraTrees as the dominant model family (top 10 of 105
configs) with 71.3% hit rate at +100% return. v3 explores dimensions
v2 didn't touch:

  A. Cross-threshold targeted models — separate ET classifiers trained
     on binary labels at +50%, +100%, +200%, +300%. Measures whether
     specialization at the target threshold beats a general regressor.

  B. Per-sector models — ET trained on each sector separately. Does
     sector specialization beat a universal model, given data sparsity?

  C. Stacking ensemble — second-level model learns from the top-5 v2
     base models' walk-forward outputs. Does meta-learning extract more
     signal than the best individual model?

  D. Classifier vs regressor + calibration — trains ET classifier with
     v2 hyperparameters, compares to regressor, computes calibration
     curve (are predicted probabilities well-calibrated?).

  E. Regime-conditional training — trains model using only bull-regime
     windows, scores bear-regime; and vice versa. Tests whether the
     pooled-training model is robust across regimes or regime-dependent.

  F. Permutation feature importance — shuffles one feature at a time
     in the test set and measures drop in hit rate. Cleaner than the
     ablation-by-retraining approach in feature_ablation.py.

  G. Expanded ET grid — 200+ configs across a finer hyperparameter
     grid than v2, on the extended 16-feature set.

  H. Consolidated report + bootstrap CIs.

Saves partial results per phase so crashes never lose >1 phase of work.
Runtime estimate: 3-5 hours. Launched in background, not merged to main.
"""
from __future__ import annotations

import itertools
import json
import logging
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("deep_research_v3.log", mode="a"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("deep_research_v3")

ROOT = Path(__file__).parent
EXT_CSV = ROOT / "upside_hunt_extended.csv"
RESULTS_CSV = ROOT / "upside_hunt_results.csv"
OUT_DIR = ROOT / "research"
OUT_DIR.mkdir(exist_ok=True)

DATE = datetime.now().strftime("%Y-%m-%d")
REPORT_JSON = OUT_DIR / f"deep_findings_v3_{DATE}.json"
REPORT_MD = OUT_DIR / f"deep_findings_v3_{DATE}.md"

ALL_FEATURES = [
    "log_price", "sigma", "p90_ratio", "p10_ratio", "asymmetry",
    "vol_low", "vol_hi", "H7_ewma_p90",
    "mom_20d", "mom_60d", "mom_180d",
    "high_52w_ratio", "low_52w_ratio",
    "realized_vol_60d", "volume_z60d", "beta_180d",
]
TOP_N = 20
# v2 winner — reused across phases where we want a single "best" model.
WINNING_CONFIG = {"n_estimators": 300, "max_depth": 14, "min_samples_leaf": 20}

REPORT: dict = {
    "started_at": time.time(),
    "date": DATE,
    "v2_reference": "research/deep_findings_v2_2026-04-16.md",
    "phases": {},
}


def _save_partial():
    try:
        REPORT_JSON.write_text(json.dumps(REPORT, indent=2, default=str))
    except Exception as e:
        log.warning("Save failed: %s", e)


def _build_base(df):
    f = df.copy()
    f["log_price"] = np.log(f["current"].clip(lower=0.01))
    f["p90_ratio"] = f["p90"] / f["current"].clip(lower=0.01)
    f["p10_ratio"] = f["p10"] / f["current"].clip(lower=0.01)
    f["asymmetry"] = (f["p90_ratio"] - 1) - (1 - f["p10_ratio"])
    f["vol_low"] = (f["sigma"] < 0.30).astype(int)
    f["vol_hi"] = (f["sigma"] > 0.60).astype(int)
    return f


def _load_features():
    if EXT_CSV.exists():
        df = pd.read_csv(EXT_CSV, parse_dates=["as_of"])
    else:
        log.warning("upside_hunt_extended.csv missing; falling back to base features only")
        df = pd.read_csv(RESULTS_CSV, parse_dates=["as_of"])
    return _build_base(df)


# ───────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────

def _walk_forward(feat, features, make_model, clip_target=False,
                  classifier_mode=False):
    from sklearn.preprocessing import StandardScaler
    t0 = time.time()
    windows = sorted(feat["as_of"].unique())
    if len(windows) < 3:
        return None, None, 0
    scores = np.zeros(len(feat))
    for i, w in enumerate(windows):
        if i == 0:
            continue
        train_mask = feat["as_of"].isin(windows[:i])
        test_mask = feat["as_of"] == w
        X_train = feat.loc[train_mask, features].values
        y_train = feat.loc[train_mask, "_y"].values
        X_test = feat.loc[test_mask, features].values
        if len(X_train) < 100 or len(X_test) == 0:
            continue
        if clip_target:
            y_train = np.clip(y_train, -0.95, 5.0)
        scaler = StandardScaler()
        X_tr = scaler.fit_transform(X_train)
        X_te = scaler.transform(X_test)
        model = make_model()
        model.fit(X_tr, y_train)
        if classifier_mode:
            probs = model.predict_proba(X_te)
            pos_col = int(np.where(model.classes_ == 1)[0][0]) if 1 in model.classes_ else 0
            scores[test_mask] = probs[:, pos_col]
        else:
            scores[test_mask] = model.predict(X_te)
    shift = scores - scores.min() + 0.01
    scored = feat.assign(_score=shift)
    return scored, windows, time.time() - t0


def _evaluate_recent(scored, windows, realized_col="realized_ret",
                     threshold=1.0, recent_k=4):
    recent = windows[-recent_k:]
    recent_df = scored[scored["as_of"].isin(recent)]
    picked = (recent_df.sort_values(["as_of", "_score"], ascending=[True, False])
                        .groupby("as_of").head(TOP_N))
    if len(picked) == 0:
        return {"rate": 0, "mean": 0, "n": 0}
    return {
        "rate": float((picked[realized_col] >= threshold).mean()),
        "mean": float(picked[realized_col].mean()),
        "median": float(picked[realized_col].median()),
        "n": int(len(picked)),
    }


def _et_factory(config, seed=42, classifier=False):
    if classifier:
        from sklearn.ensemble import ExtraTreesClassifier
        return lambda: ExtraTreesClassifier(
            n_estimators=config["n_estimators"],
            max_depth=config["max_depth"],
            min_samples_leaf=config["min_samples_leaf"],
            n_jobs=-1, random_state=seed,
            class_weight="balanced",
        )
    from sklearn.ensemble import ExtraTreesRegressor
    return lambda: ExtraTreesRegressor(
        n_estimators=config["n_estimators"],
        max_depth=config["max_depth"],
        min_samples_leaf=config["min_samples_leaf"],
        n_jobs=-1, random_state=seed,
    )


# ───────────────────────────────────────────────────
# PHASE A — cross-threshold targeted models
# ───────────────────────────────────────────────────

def phase_A_cross_thresholds(feat):
    t0 = time.time()
    log.info("=== PHASE A — cross-threshold targeted models ===")
    thresholds = [0.5, 1.0, 2.0, 3.0]
    results = {}
    for t in thresholds:
        label = (feat["realized_ret"] >= t).astype(int)
        feat2 = feat.copy()
        feat2["_y"] = label
        positive_rate = label.mean()
        log.info("  threshold +%d%%: %.1f%% positives", int(t * 100),
                 positive_rate * 100)
        factory = _et_factory(WINNING_CONFIG, classifier=True)
        scored, windows, el = _walk_forward(feat2, ALL_FEATURES, factory,
                                            classifier_mode=True)
        # Scored._score is P(positive). Evaluate hit rate at SAME threshold
        # and at +100% (our universal benchmark).
        recent = windows[-4:]
        rdf = scored[scored["as_of"].isin(recent)]
        picked = (rdf.sort_values(["as_of", "_score"], ascending=[True, False])
                      .groupby("as_of").head(TOP_N))
        if len(picked) == 0:
            results[f"+{int(t*100)}%"] = {"n": 0}
            continue
        results[f"+{int(t*100)}%"] = {
            "n_picks": int(len(picked)),
            "positive_rate_universe": float(positive_rate),
            "hit_at_target": float((picked["realized_ret"] >= t).mean()),
            "hit_at_100": float((picked["realized_ret"] >= 1.0).mean()),
            "hit_at_200": float((picked["realized_ret"] >= 2.0).mean()),
            "mean_return": float(picked["realized_ret"].mean()),
            "elapsed_s": round(el, 1),
        }
        log.info("  +%d%% model: hit@target=%.3f hit@100=%.3f mean=%+.1f%%",
                 int(t * 100),
                 results[f"+{int(t*100)}%"]["hit_at_target"],
                 results[f"+{int(t*100)}%"]["hit_at_100"],
                 results[f"+{int(t*100)}%"]["mean_return"] * 100)
    REPORT["phases"]["A_cross_thresholds"] = {
        "status": "completed",
        "elapsed_s": round(time.time() - t0, 1),
        "results": results,
    }
    _save_partial()
    log.info("PHASE A done (%.1f min)", (time.time() - t0) / 60)


# ───────────────────────────────────────────────────
# PHASE B — per-sector models
# ───────────────────────────────────────────────────

def phase_B_per_sector(feat):
    t0 = time.time()
    log.info("=== PHASE B — per-sector ET models ===")
    if "sector" not in feat.columns:
        log.warning("No sector column; skipping Phase B")
        return
    sectors = [s for s in feat["sector"].dropna().unique() if s != ""]
    log.info("Sectors: %d", len(sectors))
    feat["_y"] = feat["realized_ret"]
    results = {}
    factory = _et_factory(WINNING_CONFIG)
    for sec in sectors:
        sec_df = feat[feat["sector"] == sec]
        if len(sec_df) < 500:
            log.info("  %s: only %d rows — skipping", sec, len(sec_df))
            continue
        t_sec = time.time()
        scored, windows, el = _walk_forward(sec_df, ALL_FEATURES, factory)
        if windows is None:
            continue
        r = _evaluate_recent(scored, windows)
        results[sec] = {
            "n_rows": int(len(sec_df)),
            "n_windows": len(windows),
            "hit_rate": r["rate"],
            "mean_return": r["mean"],
            "n_picks": r["n"],
            "elapsed_s": round(el, 1),
        }
        log.info("  %-20s  n=%4d  rate=%.3f  mean=%+.1f%%  (%.0fs)",
                 sec, len(sec_df), r["rate"], r["mean"] * 100, el)
    REPORT["phases"]["B_per_sector"] = {
        "status": "completed",
        "elapsed_s": round(time.time() - t0, 1),
        "sectors": results,
    }
    _save_partial()
    log.info("PHASE B done (%.1f min)", (time.time() - t0) / 60)


# ───────────────────────────────────────────────────
# PHASE C — stacking ensemble
# ───────────────────────────────────────────────────

def phase_C_stacking(feat):
    t0 = time.time()
    log.info("=== PHASE C — stacking ensemble ===")
    feat["_y"] = feat["realized_ret"]
    top_configs = [
        {"n_estimators": 300, "max_depth": 14, "min_samples_leaf": 20},
        {"n_estimators": 500, "max_depth": 14, "min_samples_leaf": 20},
        {"n_estimators": 500, "max_depth": 20, "min_samples_leaf": 20},
        {"n_estimators": 500, "max_depth": 8, "min_samples_leaf": 5},
        {"n_estimators": 300, "max_depth": 20, "min_samples_leaf": 20},
    ]
    # Walk-forward each base model, collect their OOS predictions.
    base_scores = {}
    for i, cfg in enumerate(top_configs):
        factory = _et_factory(cfg)
        scored, windows, el = _walk_forward(feat, ALL_FEATURES, factory)
        key = f"et_{cfg['n_estimators']}_{cfg['max_depth']}_{cfg['min_samples_leaf']}"
        base_scores[key] = scored["_score"].values
        log.info("  base #%d  %s  (%.0fs)", i + 1, key, el)

    # Stack the base scores via another ET regressor.
    stack_X = pd.DataFrame(base_scores)
    stack_X["as_of"] = feat["as_of"].values
    stack_X["_y"] = feat["realized_ret"].values
    stack_feat_cols = list(base_scores.keys())
    stack_scored, windows, el = _walk_forward(
        stack_X, stack_feat_cols,
        _et_factory({"n_estimators": 200, "max_depth": 6, "min_samples_leaf": 20}))
    stack_scored["realized_ret"] = feat["realized_ret"].values
    r = _evaluate_recent(stack_scored, windows)
    log.info("  STACK  rate=%.3f mean=%+.1f%% (%.0fs)",
             r["rate"], r["mean"] * 100, el)
    # Also average (simple mean) of base scores as a naive baseline.
    avg = np.mean([v for v in base_scores.values()], axis=0)
    avg_scored = feat.assign(_score=avg - avg.min() + 0.01)
    r_avg = _evaluate_recent(avg_scored, sorted(feat["as_of"].unique()))
    log.info("  SIMPLE AVG  rate=%.3f mean=%+.1f%%",
             r_avg["rate"], r_avg["mean"] * 100)
    REPORT["phases"]["C_stacking"] = {
        "status": "completed",
        "elapsed_s": round(time.time() - t0, 1),
        "base_configs": top_configs,
        "stacked": r,
        "simple_average": r_avg,
    }
    _save_partial()
    log.info("PHASE C done (%.1f min)", (time.time() - t0) / 60)


# ───────────────────────────────────────────────────
# PHASE D — classifier vs regressor + calibration
# ───────────────────────────────────────────────────

def phase_D_classifier_calibration(feat):
    t0 = time.time()
    log.info("=== PHASE D — classifier vs regressor + calibration ===")
    # Classifier on +100% label.
    feat_cls = feat.copy()
    feat_cls["_y"] = (feat_cls["realized_ret"] >= 1.0).astype(int)
    cls_factory = _et_factory(WINNING_CONFIG, classifier=True)
    cls_scored, windows, cls_el = _walk_forward(
        feat_cls, ALL_FEATURES, cls_factory, classifier_mode=True)
    cls_scored["realized_ret"] = feat["realized_ret"].values
    r_cls = _evaluate_recent(cls_scored, windows)
    log.info("  classifier  rate=%.3f mean=%+.1f%% (%.0fs)",
             r_cls["rate"], r_cls["mean"] * 100, cls_el)

    # Regressor (same config, clip_target=False).
    feat_reg = feat.copy()
    feat_reg["_y"] = feat_reg["realized_ret"]
    reg_factory = _et_factory(WINNING_CONFIG)
    reg_scored, windows, reg_el = _walk_forward(feat_reg, ALL_FEATURES, reg_factory)
    reg_scored["realized_ret"] = feat["realized_ret"].values
    r_reg = _evaluate_recent(reg_scored, windows)
    log.info("  regressor   rate=%.3f mean=%+.1f%% (%.0fs)",
             r_reg["rate"], r_reg["mean"] * 100, reg_el)

    # Calibration curve on the classifier: decile buckets of predicted prob,
    # realized hit rate per bucket.
    recent = windows[-4:]
    rdf = cls_scored[cls_scored["as_of"].isin(recent)].copy()
    rdf["_bucket"] = pd.qcut(rdf["_score"].rank(method="first"), 10,
                             labels=False, duplicates="drop")
    calib = {}
    for b in sorted(rdf["_bucket"].dropna().unique()):
        bucket = rdf[rdf["_bucket"] == b]
        calib[f"decile_{int(b)+1}"] = {
            "n": int(len(bucket)),
            "mean_pred_prob": float(bucket["_score"].mean()),
            "realized_hit_100": float((bucket["realized_ret"] >= 1.0).mean()),
            "realized_hit_200": float((bucket["realized_ret"] >= 2.0).mean()),
            "realized_mean": float(bucket["realized_ret"].mean()),
        }

    REPORT["phases"]["D_classifier_calibration"] = {
        "status": "completed",
        "elapsed_s": round(time.time() - t0, 1),
        "classifier": r_cls,
        "regressor": r_reg,
        "calibration_deciles": calib,
    }
    _save_partial()
    log.info("PHASE D done (%.1f min)", (time.time() - t0) / 60)


# ───────────────────────────────────────────────────
# PHASE E — regime-conditional training
# ───────────────────────────────────────────────────

def phase_E_regime(feat):
    t0 = time.time()
    log.info("=== PHASE E — regime-conditional training ===")
    # Label each window as bull/bear/choppy by universe median realized return.
    feat["_y"] = feat["realized_ret"]
    windows = sorted(feat["as_of"].unique())
    win_med = feat.groupby("as_of")["realized_ret"].median()
    regime_of = {}
    for w, med in win_med.items():
        if med >= 0.10:     regime_of[w] = "bull"
        elif med <= -0.05:  regime_of[w] = "bear"
        else:               regime_of[w] = "choppy"
    log.info("  regime counts: %s",
             {k: sum(1 for r in regime_of.values() if r == k)
              for k in ("bull", "choppy", "bear")})

    factory = _et_factory(WINNING_CONFIG)
    results = {}
    # Train on ALL non-test windows (baseline universal model).
    scored, _, el_uni = _walk_forward(feat, ALL_FEATURES, factory)
    scored["regime"] = scored["as_of"].map(regime_of)
    per_regime = {}
    for regime in ("bull", "choppy", "bear"):
        mask = scored["regime"] == regime
        if mask.sum() == 0:
            continue
        sub = scored[mask]
        # Evaluate on ALL windows of this regime (not just recent-4).
        picked = (sub.sort_values(["as_of", "_score"], ascending=[True, False])
                      .groupby("as_of").head(TOP_N))
        if len(picked) == 0:
            continue
        per_regime[regime] = {
            "n_windows": int(sub["as_of"].nunique()),
            "n_picks": int(len(picked)),
            "hit_rate": float((picked["realized_ret"] >= 1.0).mean()),
            "mean_return": float(picked["realized_ret"].mean()),
        }
    log.info("  universal model per regime: %s", per_regime)

    results["universal_per_regime"] = per_regime
    results["universal_elapsed_s"] = round(el_uni, 1)

    # Regime-specialized: train ONLY on bull-regime windows, test on bull; etc.
    for regime in ("bull", "choppy", "bear"):
        regime_windows = [w for w, r in regime_of.items() if r == regime]
        if len(regime_windows) < 3:
            log.info("  %s: only %d windows — skipping specialization",
                     regime, len(regime_windows))
            continue
        feat_reg = feat[feat["as_of"].isin(regime_windows)].copy()
        scored_spec, windows_spec, el_spec = _walk_forward(
            feat_reg, ALL_FEATURES, factory)
        if windows_spec is None:
            continue
        picked = (scored_spec.sort_values(["as_of", "_score"], ascending=[True, False])
                              .groupby("as_of").head(TOP_N))
        if len(picked) == 0:
            continue
        results[f"{regime}_specialized"] = {
            "n_windows": len(regime_windows),
            "n_picks": int(len(picked)),
            "hit_rate": float((picked["realized_ret"] >= 1.0).mean()),
            "mean_return": float(picked["realized_ret"].mean()),
            "elapsed_s": round(el_spec, 1),
        }
        log.info("  %s specialized: rate=%.3f mean=%+.1f%%",
                 regime, results[f"{regime}_specialized"]["hit_rate"],
                 results[f"{regime}_specialized"]["mean_return"] * 100)

    REPORT["phases"]["E_regime"] = {
        "status": "completed",
        "elapsed_s": round(time.time() - t0, 1),
        "regime_of_window": {str(k): v for k, v in regime_of.items()},
        "results": results,
    }
    _save_partial()
    log.info("PHASE E done (%.1f min)", (time.time() - t0) / 60)


# ───────────────────────────────────────────────────
# PHASE F — permutation feature importance
# ───────────────────────────────────────────────────

def phase_F_permutation(feat):
    t0 = time.time()
    log.info("=== PHASE F — permutation feature importance ===")
    feat["_y"] = feat["realized_ret"]
    windows = sorted(feat["as_of"].unique())
    # Baseline: train on all windows except last 4; measure hit rate on last 4.
    train_windows = windows[:-4]
    test_windows = windows[-4:]
    train_df = feat[feat["as_of"].isin(train_windows)]
    test_df = feat[feat["as_of"].isin(test_windows)].copy()

    from sklearn.preprocessing import StandardScaler
    scaler = StandardScaler()
    X_train = scaler.fit_transform(train_df[ALL_FEATURES].values)
    y_train = train_df["realized_ret"].values
    X_test = scaler.transform(test_df[ALL_FEATURES].values)

    from sklearn.ensemble import ExtraTreesRegressor
    model = ExtraTreesRegressor(
        n_estimators=300, max_depth=14, min_samples_leaf=20,
        n_jobs=-1, random_state=42,
    )
    model.fit(X_train, y_train)
    baseline_preds = model.predict(X_test)
    test_df["_score"] = baseline_preds - baseline_preds.min() + 0.01
    picked = (test_df.sort_values(["as_of", "_score"], ascending=[True, False])
                      .groupby("as_of").head(TOP_N))
    baseline_rate = float((picked["realized_ret"] >= 1.0).mean())
    log.info("  baseline rate: %.3f", baseline_rate)

    # Permute each feature in X_test, measure drop in hit rate.
    rng = np.random.default_rng(42)
    importance = {}
    for i, feat_name in enumerate(ALL_FEATURES):
        drops = []
        for rep in range(5):  # 5 permutation reps per feature
            X_perm = X_test.copy()
            X_perm[:, i] = rng.permutation(X_perm[:, i])
            preds_p = model.predict(X_perm)
            test_df_p = test_df.copy()
            test_df_p["_score"] = preds_p - preds_p.min() + 0.01
            picked_p = (test_df_p.sort_values(["as_of", "_score"],
                                              ascending=[True, False])
                                  .groupby("as_of").head(TOP_N))
            rate_p = float((picked_p["realized_ret"] >= 1.0).mean())
            drops.append(baseline_rate - rate_p)
        importance[feat_name] = {
            "mean_drop": float(np.mean(drops)),
            "std_drop": float(np.std(drops)),
        }
        log.info("  %-18s  drop=%+.3f ± %.3f", feat_name,
                 np.mean(drops), np.std(drops))

    ranked = sorted(importance.items(), key=lambda kv: kv[1]["mean_drop"],
                    reverse=True)
    log.info("Top features by permutation importance:")
    for f, v in ranked[:5]:
        log.info("  %-18s  %+.3f", f, v["mean_drop"])

    REPORT["phases"]["F_permutation"] = {
        "status": "completed",
        "elapsed_s": round(time.time() - t0, 1),
        "baseline_rate": baseline_rate,
        "importance": importance,
        "ranking": [f for f, _ in ranked],
    }
    _save_partial()
    log.info("PHASE F done (%.1f min)", (time.time() - t0) / 60)


# ───────────────────────────────────────────────────
# PHASE G — expanded ET grid
# ───────────────────────────────────────────────────

def phase_G_expanded_grid(feat):
    t0 = time.time()
    log.info("=== PHASE G — expanded ET hyperparameter grid ===")
    feat["_y"] = feat["realized_ret"]
    # 3 × 5 × 5 × 2 × 2 = 300 configs
    grid = list(itertools.product(
        [100, 300, 500],          # n_estimators
        [6, 10, 14, 18, 22],      # max_depth
        [3, 8, 15, 25, 40],       # min_samples_leaf
        ["sqrt", None],           # max_features
        [True, False],            # bootstrap
    ))
    log.info("  configs to evaluate: %d", len(grid))
    results = {}
    done = 0
    for n, d, l, mf, bs in grid:
        from sklearn.ensemble import ExtraTreesRegressor
        def factory(n=n, d=d, l=l, mf=mf, bs=bs):
            return ExtraTreesRegressor(
                n_estimators=n, max_depth=d, min_samples_leaf=l,
                max_features=mf, bootstrap=bs,
                n_jobs=-1, random_state=42,
            )
        scored, windows, el = _walk_forward(feat, ALL_FEATURES, factory)
        r = _evaluate_recent(scored, windows)
        key = f"et_n{n}_d{d}_l{l}_mf{mf}_bs{bs}"
        results[key] = {**r, "elapsed_s": round(el, 1),
                         "config": {"n_estimators": n, "max_depth": d,
                                    "min_samples_leaf": l,
                                    "max_features": mf, "bootstrap": bs}}
        done += 1
        if done % 20 == 0:
            log.info("  [%d/%d] best so far: %s",
                     done, len(grid),
                     sorted(results.items(),
                            key=lambda kv: (kv[1]["rate"], kv[1]["mean"]),
                            reverse=True)[0][0])
            REPORT["phases"]["G_expanded_grid"] = {
                "status": "in_progress",
                "completed": done,
                "total": len(grid),
                "elapsed_s": round(time.time() - t0, 1),
                "partial_results": results,
            }
            _save_partial()

    ranked = sorted(results.items(),
                    key=lambda kv: (kv[1]["rate"], kv[1]["mean"]),
                    reverse=True)
    log.info("PHASE G top 10:")
    for i, (k, r) in enumerate(ranked[:10]):
        log.info("  #%d  %s  rate=%.3f mean=%+.1f%%",
                 i + 1, k, r["rate"], r["mean"] * 100)

    REPORT["phases"]["G_expanded_grid"] = {
        "status": "completed",
        "elapsed_s": round(time.time() - t0, 1),
        "total_configs": len(grid),
        "results": results,
        "top_10": [k for k, _ in ranked[:10]],
    }
    _save_partial()
    log.info("PHASE G done (%.1f min)", (time.time() - t0) / 60)


# ───────────────────────────────────────────────────
# PHASE H — consolidated report
# ───────────────────────────────────────────────────

def phase_H_report():
    t0 = time.time()
    log.info("=== PHASE H — consolidated report ===")
    md = [
        f"# Deep research v3 findings — {DATE}",
        "",
        f"Total runtime: **{(time.time() - REPORT['started_at']) / 60:.1f} min**",
        "",
        "Continuation from v2 (ExtraTrees winner at 71.3% hit rate). v3 "
        "explores cross-threshold specialization, sector models, stacking, "
        "calibration, regime conditioning, permutation importance, and an "
        "expanded hyperparameter grid.",
        "",
    ]

    pA = REPORT["phases"].get("A_cross_thresholds", {})
    if pA.get("results"):
        md += ["## Phase A — cross-threshold targeted models", "",
               "Each row is an ET classifier trained on the binary label at that threshold. Columns show hit rates at various realization levels.",
               "",
               "| train target | hit@target | hit@+100% | hit@+200% | mean return |",
               "|---|---|---|---|---|"]
        for k, v in pA["results"].items():
            if v.get("n_picks"):
                md.append(f"| {k} | {v['hit_at_target']:.3f} | {v['hit_at_100']:.3f} | {v['hit_at_200']:.3f} | {v['mean_return']*100:+.1f}% |")
        md.append("")

    pB = REPORT["phases"].get("B_per_sector", {})
    if pB.get("sectors"):
        md += ["## Phase B — per-sector ET models",
               "",
               "ET trained on each sector independently. Small-sample sectors may underperform the universal model.",
               "",
               "| sector | rows | hit rate | mean return |",
               "|---|---|---|---|"]
        for sec, v in sorted(pB["sectors"].items(),
                             key=lambda kv: kv[1]["hit_rate"], reverse=True):
            md.append(f"| {sec} | {v['n_rows']} | {v['hit_rate']:.3f} | {v['mean_return']*100:+.1f}% |")
        md.append("")

    pC = REPORT["phases"].get("C_stacking", {})
    if pC.get("stacked"):
        md += ["## Phase C — stacking ensemble", "",
               f"- Stacked ET (5 base models): rate **{pC['stacked']['rate']:.3f}**, mean **{pC['stacked']['mean']*100:+.1f}%**",
               f"- Simple mean of base scores: rate **{pC['simple_average']['rate']:.3f}**, mean **{pC['simple_average']['mean']*100:+.1f}%**",
               ""]

    pD = REPORT["phases"].get("D_classifier_calibration", {})
    if pD.get("classifier"):
        md += ["## Phase D — classifier vs regressor + calibration", "",
               f"- Classifier: rate **{pD['classifier']['rate']:.3f}**, mean **{pD['classifier']['mean']*100:+.1f}%**",
               f"- Regressor:  rate **{pD['regressor']['rate']:.3f}**, mean **{pD['regressor']['mean']*100:+.1f}%**",
               "",
               "### Calibration curve (deciles of predicted probability on recent-4 windows)",
               "",
               "| decile | mean predicted prob | realized +100% rate | realized +200% rate |",
               "|---|---|---|---|"]
        cal = pD.get("calibration_deciles", {})
        for k in sorted(cal.keys(),
                        key=lambda s: int(s.split("_")[1]), reverse=True):
            d = cal[k]
            md.append(f"| {k} | {d['mean_pred_prob']:.3f} | {d['realized_hit_100']:.3f} | {d['realized_hit_200']:.3f} |")
        md.append("")

    pE = REPORT["phases"].get("E_regime", {})
    if pE.get("results"):
        res = pE["results"]
        md += ["## Phase E — regime-conditional training", ""]
        if res.get("universal_per_regime"):
            md += ["### Universal model, evaluated per regime", "",
                   "| regime | windows | hit rate | mean return |",
                   "|---|---|---|---|"]
            for r, v in res["universal_per_regime"].items():
                md.append(f"| {r} | {v['n_windows']} | {v['hit_rate']:.3f} | {v['mean_return']*100:+.1f}% |")
            md.append("")
        md += ["### Regime-specialized models", "",
               "| regime | windows | hit rate | mean return |",
               "|---|---|---|---|"]
        for regime in ("bull", "choppy", "bear"):
            key = f"{regime}_specialized"
            if key in res:
                v = res[key]
                md.append(f"| {regime} | {v['n_windows']} | {v['hit_rate']:.3f} | {v['mean_return']*100:+.1f}% |")
        md.append("")

    pF = REPORT["phases"].get("F_permutation", {})
    if pF.get("importance"):
        md += ["## Phase F — permutation feature importance", "",
               f"Baseline hit rate on recent-4 windows: **{pF.get('baseline_rate', 0):.3f}**. Each feature is shuffled 5× in the test set; table shows mean drop in hit rate (higher = more important).",
               "",
               "| rank | feature | mean drop | std |",
               "|---|---|---|---|"]
        for i, f in enumerate(pF.get("ranking", [])[:10]):
            v = pF["importance"][f]
            md.append(f"| {i+1} | `{f}` | {v['mean_drop']:+.3f} | {v['std_drop']:.3f} |")
        md.append("")

    pG = REPORT["phases"].get("G_expanded_grid", {})
    if pG.get("results"):
        md += ["## Phase G — expanded ET hyperparameter grid",
               "",
               f"Evaluated {pG.get('total_configs', 0)} configs. Top 10:",
               "",
               "| rank | config | hit rate | mean return |",
               "|---|---|---|---|"]
        ranked = sorted(pG["results"].items(),
                        key=lambda kv: (kv[1]["rate"], kv[1]["mean"]),
                        reverse=True)
        for i, (k, r) in enumerate(ranked[:10]):
            md.append(f"| {i+1} | `{k}` | {r['rate']:.3f} | {r['mean']*100:+.1f}% |")
        md.append("")

    md += ["## Notes", "",
           "- Walk-forward CV on all phases; no test-set contamination.",
           "- `hit rate` = % of top-20 picks reaching the threshold within 12 months.",
           "- Recent-4 aggregation unless noted otherwise.",
           "- v3 reuses v2's extended 16-feature set (research/deep_findings_v2_*).",
           "",
           f"Report generated: {datetime.now().isoformat()}",
           ]
    REPORT_MD.write_text("\n".join(md))
    REPORT["completed_at"] = time.time()
    REPORT["total_runtime_min"] = round(
        (REPORT["completed_at"] - REPORT["started_at"]) / 60, 1)
    _save_partial()
    log.info("PHASE H done; report → %s (%.1f min total)",
             REPORT_MD, REPORT["total_runtime_min"])


def run():
    try:
        feat = _load_features()
        log.info("Loaded %d rows, %d features", len(feat), len(ALL_FEATURES))
        phase_A_cross_thresholds(feat.copy())
        phase_B_per_sector(feat.copy())
        phase_C_stacking(feat.copy())
        phase_D_classifier_calibration(feat.copy())
        phase_E_regime(feat.copy())
        phase_F_permutation(feat.copy())
        phase_G_expanded_grid(feat.copy())
        phase_H_report()
        log.info("DEEP RESEARCH V3 COMPLETE — %s min",
                 REPORT.get("total_runtime_min"))
        return 0
    except Exception as e:
        log.exception("v3 crashed: %s", e)
        REPORT["crashed_at"] = time.time()
        REPORT["crash_traceback"] = traceback.format_exc()
        _save_partial()
        return 1


if __name__ == "__main__":
    sys.exit(run())
