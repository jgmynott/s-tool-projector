"""
Overnight self-learning loop.

Each night this script:

  1. Pulls the historical (ticker × window × features × realized-return) table
     built by upside_hunt.py — that's our ground truth.
  2. Trains a small feed-forward neural network on that ground truth to
     predict realized 12-month returns, using walk-forward cross-validation
     so we're never cheating on out-of-sample data.
  3. Evaluates all candidate scoring methods (hand-crafted + neural)
     against the MOST RECENT windows. This keeps the production scorer
     adaptive — if market regime shifts, the winning method shifts too.
  4. Writes a `data_cache/production_scorer.json` describing the winner
     + a performance table. `portfolio_scanner.py` reads this on each
     scan and uses the winning method for the Asymmetric tier.

The NN is small on purpose — 2 hidden layers, regularized, early-stopped.
We're not chasing academic predictive accuracy; we're chasing a stable
edge over the H-methods in out-of-sample realized returns.

Run nightly after the GH Actions refresh:
    python3 upside_hunt.py        # rebuilds the ground truth
    python3 overnight_learn.py    # retrains + selects production scorer
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("overnight_learn")

ROOT = Path(__file__).parent
_RT_RESULTS = ROOT / "runtime_data" / "upside_hunt_results.csv"
RESULTS_CSV = _RT_RESULTS if _RT_RESULTS.exists() else ROOT / "upside_hunt_results.csv"
OUT_JSON = ROOT / "data_cache" / "production_scorer.json"

# Hand-crafted methods to compare the NN against. Columns in the CSV.
HAND_METHODS = [
    "H1_naive_p90", "H2_capped_p90", "H4_composite",
    "H5_sector_mom", "H6_small_cap", "H7_ewma_p90", "H9_full_stack",
]

# Features the NN gets to see. Everything derivable from the upside-hunt
# CSV plus simple engineered features. No forward-looking data.
FEATURE_COLS = [
    "current", "p10", "p90", "sigma",
    "H1_naive_p90", "H7_ewma_p90",  # engine scores count as features
]


def _build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Derive an engineered feature set from the raw results table."""
    f = df.copy()
    # Log-price (reduces skew)
    f["log_price"] = np.log(f["current"].clip(lower=0.01))
    # P90-to-current and P10-to-current ratios
    f["p90_ratio"] = f["p90"] / f["current"].clip(lower=0.01)
    f["p10_ratio"] = f["p10"] / f["current"].clip(lower=0.01)
    # Asymmetry score
    f["asymmetry"] = (f["p90_ratio"] - 1) - (1 - f["p10_ratio"])
    # Vol bucket
    f["vol_low"] = (f["sigma"] < 0.30).astype(int)
    f["vol_hi"] = (f["sigma"] > 0.60).astype(int)
    return f


def _hit_rate(scored: pd.DataFrame, score_col: str, top_n: int = 20,
              threshold: float = 1.0) -> dict:
    """For each window, take top-N by score_col, measure realized +threshold hits."""
    if score_col not in scored.columns:
        return {"n": 0, "hits": 0, "rate": 0, "median": 0, "mean": 0}
    df = scored.copy()
    df = df[df[score_col] > 0]  # methods that opt out with 0 score
    picks = df.sort_values(["as_of", score_col], ascending=[True, False])
    picks = picks.groupby("as_of").head(top_n)
    if len(picks) == 0:
        return {"n": 0, "hits": 0, "rate": 0, "median": 0, "mean": 0}
    hits = int((picks["realized_ret"] >= threshold).sum())
    return {
        "n": int(len(picks)),
        "hits": hits,
        "rate": hits / len(picks),
        "median": float(picks["realized_ret"].median()),
        "mean": float(picks["realized_ret"].mean()),
    }


def _train_confidence_nn(df: pd.DataFrame) -> tuple:
    """Train an NN that predicts the realized absolute % error of the
    engine's P50 call. Lower predicted error = higher confidence.

    This replaces the rule-based confidence blend (Sharpe + SEC + band
    tightness) with something that actually correlates with whether the
    engine's median projection holds up out-of-sample.

    Returns (feat_df_with_predicted_mape, final_model, scaler).
    """
    from sklearn.neural_network import MLPRegressor
    from sklearn.preprocessing import StandardScaler

    feat = _build_features(df)
    feature_names = [
        "log_price", "sigma", "p90_ratio", "p10_ratio", "asymmetry",
        "vol_low", "vol_hi", "H7_ewma_p90",
    ]
    # Target: abs(realized - projected_p50_return). Use p50-based return.
    feat["p50_return"] = (feat["p10"] + feat["p90"]) / (2 * feat["current"]) - 1
    feat["mape_p50"] = (feat["realized_ret"] - feat["p50_return"]).abs()

    windows = sorted(feat["as_of"].unique())
    if len(windows) < 3:
        feat["predicted_mape"] = 0.5
        return feat, None, None

    pred_mape = np.zeros(len(feat))
    for i, w in enumerate(windows):
        if i == 0:
            continue
        train_mask = feat["as_of"].isin(windows[:i])
        test_mask = feat["as_of"] == w
        X_train = feat.loc[train_mask, feature_names].values
        y_train = feat.loc[train_mask, "mape_p50"].values
        X_test = feat.loc[test_mask, feature_names].values
        if len(X_train) < 100 or len(X_test) == 0:
            continue
        y_train = np.clip(y_train, 0, 3.0)
        scaler = StandardScaler()
        X_train_s = scaler.fit_transform(X_train)
        X_test_s = scaler.transform(X_test)
        model = MLPRegressor(
            hidden_layer_sizes=(24, 12), activation="relu",
            max_iter=300, early_stopping=True, validation_fraction=0.15,
            random_state=42, alpha=1e-2,
        )
        model.fit(X_train_s, y_train)
        preds = model.predict(X_test_s)
        pred_mape[test_mask] = np.clip(preds, 0.01, 3.0)
    feat["predicted_mape"] = pred_mape

    # Train final model on all data for today-scoring
    X_all = feat[feature_names].values
    y_all = np.clip(feat["mape_p50"].values, 0, 3.0)
    scaler = StandardScaler()
    X_all_s = scaler.fit_transform(X_all)
    final_model = MLPRegressor(
        hidden_layer_sizes=(24, 12), activation="relu",
        max_iter=300, early_stopping=True, validation_fraction=0.15,
        random_state=42, alpha=1e-2,
    )
    final_model.fit(X_all_s, y_all)
    return feat, final_model, scaler


def _train_moonshot_nn(df: pd.DataFrame) -> tuple:
    """Binary classifier trained on the +100% label.

    Uses ExtraTreesClassifier with balanced class weights. v2 research
    showed ET dominated MLP for the regression task at 71.3% vs 62.5%;
    the same pattern holds for classification and ET supports class_weight
    natively (MLPClassifier doesn't). Config matches the regressor.
    """
    from sklearn.ensemble import ExtraTreesClassifier
    from sklearn.preprocessing import StandardScaler

    feat = _build_features(df)
    feat = _attach_sec_features(feat)
    feature_names = [
        "log_price", "sigma", "p90_ratio", "p10_ratio", "asymmetry",
        "vol_low", "vol_hi", "H7_ewma_p90",
    ] + SEC_FEATURE_NAMES
    feat["moonshot_label"] = (feat["realized_ret"] >= 1.0).astype(int)

    windows = sorted(feat["as_of"].unique())
    if len(windows) < 3:
        feat["moonshot_score"] = 0.0
        return feat, None, None

    def _balance(X, y, seed=42):
        pos_idx = np.where(y == 1)[0]
        neg_idx = np.where(y == 0)[0]
        if len(pos_idx) == 0 or len(neg_idx) <= len(pos_idx):
            return X, y
        factor = len(neg_idx) // max(len(pos_idx), 1)
        oversampled = np.tile(pos_idx, factor)
        idx = np.concatenate([neg_idx, oversampled])
        rng = np.random.default_rng(seed)
        rng.shuffle(idx)
        return X[idx], y[idx]

    scores = np.zeros(len(feat))
    for i, w in enumerate(windows):
        if i == 0:
            continue
        train_mask = feat["as_of"].isin(windows[:i])
        test_mask = feat["as_of"] == w
        X_train = feat.loc[train_mask, feature_names].values
        y_train = feat.loc[train_mask, "moonshot_label"].values
        X_test = feat.loc[test_mask, feature_names].values
        if len(X_train) < 200 or y_train.sum() < 10 or len(X_test) == 0:
            continue
        X_bal, y_bal = _balance(X_train, y_train)
        scaler = StandardScaler()
        X_bal_s = scaler.fit_transform(X_bal)
        X_test_s = scaler.transform(X_test)
        model = ExtraTreesClassifier(
            n_estimators=300, max_depth=14, min_samples_leaf=20,
            n_jobs=-1, random_state=42,
        )
        model.fit(X_bal_s, y_bal)
        probs = model.predict_proba(X_test_s)
        pos_col = int(np.where(model.classes_ == 1)[0][0]) if 1 in model.classes_ else None
        if pos_col is None:
            continue
        scores[test_mask] = probs[:, pos_col]
        log.info("Moonshot window %s: trained on %d rows (%d positives), predicted %d",
                 w, len(X_bal), int(y_bal.sum()), len(X_test))

    feat["moonshot_score"] = scores

    X_all = feat[feature_names].values
    y_all = feat["moonshot_label"].values
    X_bal, y_bal = _balance(X_all, y_all)
    scaler = StandardScaler()
    X_bal_s = scaler.fit_transform(X_bal)
    final_model = ExtraTreesClassifier(
        n_estimators=300, max_depth=14, min_samples_leaf=20,
        n_jobs=-1, random_state=42,
    )
    final_model.fit(X_bal_s, y_bal)
    return feat, final_model, scaler


SEC_FEATURE_NAMES = [
    "revenue_yoy_growth", "gross_margin", "operating_margin",
    "fcf_to_revenue", "buyback_intensity", "net_debt_change_pct",
]


def _attach_sec_features(feat: pd.DataFrame) -> pd.DataFrame:
    """Best-effort SEC feature augmentation. If the projector cache + SEC
    fundamentals table aren't available, returns feat unchanged with the
    SEC columns set to 0 — so the same `feature_names` list still works
    and the model just sees a constant-zero contribution.

    Research 2026-04-17: adding the 6 SEC features alongside the 8 base
    features lifted hit-rate +1.72pp on the expanded 2016-2024 set.
    Single-feature additions were noise; the combined block was the win.
    """
    for f in SEC_FEATURE_NAMES:
        if f not in feat.columns:
            feat[f] = 0.0
    try:
        from db import init_db
        from signals_sec_edgar import (augment_dataframe_with_sec,
                                        populate_from_cache)
        conn = init_db()
        try:
            # First-run safety: build the sec_fundamentals table from
            # cached SEC EDGAR facts JSONs if it's missing or empty.
            try:
                row_count = conn.execute(
                    "SELECT COUNT(*) FROM sec_fundamentals"
                ).fetchone()[0]
            except Exception:
                row_count = 0
            if row_count == 0:
                log.info("sec_fundamentals empty — populating from cache")
                populate_from_cache(conn)
            tmp = feat[["symbol", "as_of"]].copy()
            tmp = augment_dataframe_with_sec(tmp, conn)
            for f in SEC_FEATURE_NAMES:
                feat[f] = tmp[f].values
            log.info("SEC features attached (coverage: %.1f%% non-zero rev_yoy)",
                     100.0 * (feat["revenue_yoy_growth"] != 0).mean())
        finally:
            conn.close()
    except Exception as e:
        log.warning("SEC feature augmentation failed (%s) — features stay 0", e)
    return feat


def _train_nn(df: pd.DataFrame) -> tuple:
    """Walk-forward training — for each window, train only on prior windows,
    predict on the target window. Uses ExtraTreesRegressor (v2 research
    winner at 71.3% +100% hit rate; MLP peaked at 62.5%). Faster training
    and more stable across seeds/perturbations.

    Config `n_estimators=300, max_depth=14, min_samples_leaf=20` is the
    v2 hyperparameter search winner. See research/deep_findings_v2_*.md.

    Feature set extended on 2026-04-17 to include 6 point-in-time SEC
    fundamentals (revenue_yoy_growth, gross_margin, operating_margin,
    fcf_to_revenue, buyback_intensity, net_debt_change_pct). Ablation
    on the expanded 2016-2024 training set: +1.72pp hit-rate."""
    from sklearn.ensemble import ExtraTreesRegressor
    from sklearn.preprocessing import StandardScaler

    feat = _build_features(df)
    feat = _attach_sec_features(feat)
    feature_names = [
        "log_price", "sigma", "p90_ratio", "p10_ratio", "asymmetry",
        "vol_low", "vol_hi", "H7_ewma_p90",
    ] + SEC_FEATURE_NAMES
    windows = sorted(feat["as_of"].unique())
    if len(windows) < 3:
        log.warning("Not enough windows (%d) for walk-forward training", len(windows))
        feat["nn_score"] = 0.0
        return feat, None, None

    nn_scores = np.zeros(len(feat))
    for i, w in enumerate(windows):
        if i == 0:
            continue
        train_mask = feat["as_of"].isin(windows[:i])
        test_mask = feat["as_of"] == w
        X_train = feat.loc[train_mask, feature_names].values
        y_train = feat.loc[train_mask, "realized_ret"].values
        X_test = feat.loc[test_mask, feature_names].values
        if len(X_train) < 100 or len(X_test) == 0:
            continue
        scaler = StandardScaler()
        X_train_s = scaler.fit_transform(X_train)
        X_test_s = scaler.transform(X_test)
        model = ExtraTreesRegressor(
            n_estimators=300, max_depth=14, min_samples_leaf=20,
            n_jobs=-1, random_state=42,
        )
        model.fit(X_train_s, y_train)
        preds = model.predict(X_test_s)
        nn_scores[test_mask] = preds - preds.min() + 0.01
        log.info("Window %s trained on %d rows, predicted on %d rows",
                 w, len(X_train), len(X_test))

    feat["nn_score"] = nn_scores
    X_all = feat[feature_names].values
    y_all = feat["realized_ret"].values
    scaler = StandardScaler()
    X_all_s = scaler.fit_transform(X_all)
    final_model = ExtraTreesRegressor(
        n_estimators=300, max_depth=14, min_samples_leaf=20,
        n_jobs=-1, random_state=42,
    )
    final_model.fit(X_all_s, y_all)
    return feat, final_model, scaler


def _train_ensemble(scored: pd.DataFrame) -> tuple:
    """Second-level stacker: learns the best blend of walk-forward
    base-model scores. Inputs are the OOS predictions produced by
    _train_nn / _train_moonshot_nn / the hand-crafted H7 EWMA. Target
    is realized_ret. Walk-forward on the same window axis.

    Returns (scored_with_ensemble_score, final_stacker_model, scaler).
    """
    from sklearn.neural_network import MLPRegressor
    from sklearn.preprocessing import StandardScaler

    feat_cols = [c for c in ("nn_score", "moonshot_score", "H7_ewma_p90",
                             "H9_full_stack") if c in scored.columns]
    if len(feat_cols) < 2 or "realized_ret" not in scored.columns:
        scored["ensemble_score"] = 0.0
        return scored, None, None

    work = scored[feat_cols + ["as_of", "realized_ret"]].copy()
    work = work.fillna(0.0)

    windows = sorted(work["as_of"].unique())
    if len(windows) < 3:
        scored["ensemble_score"] = 0.0
        return scored, None, None

    preds_out = np.zeros(len(work))
    for i, w in enumerate(windows):
        if i == 0:
            continue
        train_mask = work["as_of"].isin(windows[:i])
        test_mask = work["as_of"] == w
        X_train = work.loc[train_mask, feat_cols].values
        y_train = np.clip(work.loc[train_mask, "realized_ret"].values, -0.95, 5.0)
        X_test = work.loc[test_mask, feat_cols].values
        if len(X_train) < 100 or len(X_test) == 0:
            continue
        scaler = StandardScaler()
        X_tr_s = scaler.fit_transform(X_train)
        X_te_s = scaler.transform(X_test)
        model = MLPRegressor(
            hidden_layer_sizes=(16, 8), activation="relu",
            max_iter=300, early_stopping=True, validation_fraction=0.15,
            random_state=42, alpha=1e-2,
        )
        model.fit(X_tr_s, y_train)
        preds_out[test_mask] = model.predict(X_te_s)

    # Shift so scores are positive for ranking (score_col > 0 filter).
    preds_out = preds_out - preds_out.min() + 0.01
    scored.loc[work.index, "ensemble_score"] = preds_out

    # Final stacker on all data for today-scoring.
    X_all = work[feat_cols].values
    y_all = np.clip(work["realized_ret"].values, -0.95, 5.0)
    scaler = StandardScaler()
    X_all_s = scaler.fit_transform(X_all)
    final_model = MLPRegressor(
        hidden_layer_sizes=(16, 8), activation="relu",
        max_iter=300, early_stopping=True, validation_fraction=0.15,
        random_state=42, alpha=1e-2,
    )
    final_model.fit(X_all_s, y_all)
    return scored, final_model, scaler


def main():
    if not RESULTS_CSV.exists():
        log.error("%s missing — run upside_hunt.py first", RESULTS_CSV)
        return 1
    df = pd.read_csv(RESULTS_CSV)
    log.info("Loaded %d historical ticker-window rows", len(df))

    # Train the NN and score rows via walk-forward CV.
    try:
        scored, final_model, scaler = _train_nn(df)
    except Exception as e:
        log.exception("NN training failed: %s", e)
        scored = df.copy()
        scored["nn_score"] = 0.0
        final_model = None

    # Moonshot classifier — binary NN trained specifically on the +100% label.
    moon_model = moon_scaler = None
    try:
        moon_feat, moon_model, moon_scaler = _train_moonshot_nn(df)
        # Both dfs derive from the same source with preserved row order.
        if len(moon_feat) == len(scored) and "moonshot_score" in moon_feat.columns:
            scored["moonshot_score"] = moon_feat["moonshot_score"].values
        else:
            scored["moonshot_score"] = 0.0
    except Exception as e:
        log.exception("Moonshot NN training failed: %s", e)
        scored["moonshot_score"] = 0.0

    # Ensemble stacker — blends walk-forward base scores into one.
    ens_model = ens_scaler = None
    try:
        scored, ens_model, ens_scaler = _train_ensemble(scored)
    except Exception as e:
        log.exception("Ensemble stacker training failed: %s", e)
        scored["ensemble_score"] = 0.0

    # Evaluate every method on the most recent year of windows only.
    recent_cutoff = sorted(scored["as_of"].unique())[-4:]  # last 4 windows
    recent = scored[scored["as_of"].isin(recent_cutoff)]
    log.info("Evaluating on recent windows: %s (%d rows)",
             list(recent_cutoff), len(recent))

    performance = {}
    for m in HAND_METHODS + ["nn_score", "moonshot_score", "ensemble_score"]:
        performance[m] = _hit_rate(recent, m, top_n=20, threshold=1.0)
    # Also compute a baseline: hit rate across the entire recent universe
    uni = recent["realized_ret"] >= 1.0
    baseline_rate = float(uni.mean()) if len(uni) else 0
    for m, r in performance.items():
        r["lift"] = (r["rate"] / baseline_rate) if baseline_rate else 0

    # Pick the winner: highest hit rate (primary) with mean return as tiebreaker.
    scored_methods = [(m, r) for m, r in performance.items() if r["n"] >= 20]
    if not scored_methods:
        log.warning("No method had ≥20 picks in recent windows — retaining H7_ewma_p90 as default")
        winner = "H7_ewma_p90"
    else:
        scored_methods.sort(
            key=lambda kv: (kv[1]["rate"], kv[1]["mean"]),
            reverse=True,
        )
        winner = scored_methods[0][0]
    w = performance[winner]
    log.info("Winner: %s  rate=%.1f%% lift=%.2fx mean=%+.1f%%",
             winner, w["rate"] * 100, w["lift"], w["mean"] * 100)

    # Persist
    out = {
        "promoted_at": time.time(),
        "winner": winner,
        "baseline_rate": baseline_rate,
        "recent_windows": list(recent_cutoff),
        "performance": performance,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, indent=2, default=str))
    log.info("Wrote scoring decision to %s", OUT_JSON)

    # Persist the scored df for downstream overnight_backtest.py. Contains
    # the walk-forward nn_score / moonshot_score / ensemble_score columns
    # the backtest needs to report NN-family lift alongside hand-crafted.
    # Write scored CSV to runtime_data when that's where the input came from
    # so cron pipelines downstream can find it. Otherwise root for local dev.
    scored_csv = (ROOT / "runtime_data" / "upside_hunt_scored.csv"
                  if RESULTS_CSV.parent.name == "runtime_data"
                  else ROOT / "upside_hunt_scored.csv")
    try:
        # Keep raw feature columns too so downstream (overnight_backtest)
        # can compute size-neutral and year-OOS metrics which need the
        # current-price and projection columns.
        keep_cols = [c for c in scored.columns
                     if c in ("as_of", "symbol", "realized_ret",
                              "current", "p10", "p90", "sigma", "sector",
                              "nn_score", "moonshot_score", "ensemble_score")
                     or c in HAND_METHODS]
        scored[keep_cols].to_csv(scored_csv, index=False)
        log.info("Wrote scored CSV → %s (%d rows, %d cols)",
                 scored_csv, len(scored), len(keep_cols))
    except Exception as e:
        log.warning("Failed to write scored CSV: %s", e)

    # ── Confidence NN ── separate model, same feature set.
    # Predicts realized P50 error (MAPE). Confidence = 100 * (1 - clipped_mape).
    try:
        conf_feat, conf_model, conf_scaler = _train_confidence_nn(df)
        # Evaluate: correlation between predicted MAPE and realized MAPE on
        # the recent windows (out-of-sample via walk-forward).
        recent_conf = conf_feat[conf_feat["as_of"].isin(recent_cutoff)]
        if len(recent_conf) > 50:
            import scipy.stats as stats  # might not be available — fall through
            try:
                corr = float(recent_conf["predicted_mape"].corr(recent_conf["mape_p50"]))
            except Exception:
                corr = None
            log.info("Confidence NN: predicted-vs-realized MAPE correlation = %s",
                     f"{corr:.3f}" if corr is not None else "n/a")
    except Exception as e:
        log.exception("Confidence NN training failed: %s", e)
        conf_model = conf_scaler = None

    # ── Today's scores ──
    # The walk-forward scores above are for HISTORICAL windows. For
    # production we need scores computed from TODAY's feature set per
    # ticker. Load today's EWMA scores (already computed by
    # enrich_asymmetric.py) and pass them through the final NN.
    if final_model is not None and scaler is not None:
        try:
            asym_path = ROOT / "data_cache" / "asymmetric_scores.json"
            if asym_path.exists():
                asym = json.loads(asym_path.read_text())
                # Feature vector must match training: log_price, sigma,
                # p90_ratio, p10_ratio, asymmetry, vol_low, vol_hi, H7.
                syms, rows = [], []
                for sym, a in asym.items():
                    # Skip rows missing any feature
                    sigma = a.get("sigma_ewma")
                    p90r = a.get("p90_ratio")
                    p10r = a.get("p10_ratio")
                    if sigma is None or p90r is None or p10r is None:
                        continue
                    # We don't have today's price separately — log_price
                    # is approximated via the sigma/vol profile. Fallback
                    # to 0 if unknown; the NN was trained with log_price
                    # as a feature so this is an approximation.
                    log_p = 0.0
                    asymmetry = (p90r - 1) - (1 - p10r)
                    vol_low = 1 if sigma < 0.30 else 0
                    vol_hi = 1 if sigma > 0.60 else 0
                    h7 = p90r  # H7 is itself p90_ratio from EWMA engine
                    syms.append(sym)
                    rows.append([log_p, sigma, p90r, p10r, asymmetry,
                                 vol_low, vol_hi, h7])
                if rows:
                    X_today = scaler.transform(np.array(rows))
                    preds = final_model.predict(X_today)
                    # Rescale to positive domain for easy ranking.
                    preds_pos = preds - preds.min() + 0.01
                    nn_today = dict(zip(syms, preds_pos.tolist()))
                    (ROOT / "data_cache" / "nn_scores.json").write_text(
                        json.dumps(nn_today, separators=(",", ":"))
                    )
                    log.info("Wrote %d NN scores for today using final model",
                             len(nn_today))

                    # Moonshot probability scores (binary classifier).
                    if moon_model is not None and moon_scaler is not None:
                        try:
                            X_today_m = moon_scaler.transform(np.array(rows))
                            m_probs = moon_model.predict_proba(X_today_m)
                            pos_col = int(np.where(moon_model.classes_ == 1)[0][0]) if 1 in moon_model.classes_ else None
                            if pos_col is not None:
                                moon_by_sym = {
                                    s: round(float(p), 4)
                                    for s, p in zip(syms, m_probs[:, pos_col].tolist())
                                }
                                (ROOT / "data_cache" / "moonshot_scores.json").write_text(
                                    json.dumps(moon_by_sym, separators=(",", ":"))
                                )
                                log.info("Wrote %d moonshot probabilities (range %.3f..%.3f)",
                                         len(moon_by_sym),
                                         min(moon_by_sym.values()), max(moon_by_sym.values()))
                        except Exception as e:
                            log.warning("Moonshot today-scoring failed: %s", e)

                    # Confidence scores from the same feature set.
                    if conf_model is not None and conf_scaler is not None:
                        X_today_c = conf_scaler.transform(np.array(rows))
                        predicted_mape = conf_model.predict(X_today_c)
                        predicted_mape = np.clip(predicted_mape, 0.03, 1.0)
                        # Convert predicted-mape to a 0–100 confidence score.
                        # 3% error = 97 confidence; 50% error = 50; ≥100% = 0.
                        conf_scores = (1.0 - np.clip(predicted_mape, 0, 1.0)) * 100
                        conf_by_sym = {s: round(float(c), 1) for s, c in zip(syms, conf_scores)}
                        (ROOT / "data_cache" / "confidence_nn_scores.json").write_text(
                            json.dumps(conf_by_sym, separators=(",", ":"))
                        )
                        log.info("Wrote %d confidence-NN scores (range %.0f..%.0f)",
                                 len(conf_by_sym),
                                 min(conf_by_sym.values()), max(conf_by_sym.values()))

                    # Ensemble stacker — feeds each ticker's base-model
                    # today-scores through the stacker NN. Feature order
                    # must match _train_ensemble feat_cols:
                    # [nn_score, moonshot_score, H7_ewma_p90, H9_full_stack].
                    if ens_model is not None and ens_scaler is not None:
                        try:
                            nn_today_path = ROOT / "data_cache" / "nn_scores.json"
                            moon_today_path = ROOT / "data_cache" / "moonshot_scores.json"
                            nn_today_d = json.loads(nn_today_path.read_text()) if nn_today_path.exists() else {}
                            moon_today_d = json.loads(moon_today_path.read_text()) if moon_today_path.exists() else {}
                            ens_rows, ens_syms = [], []
                            for sym, a in asym.items():
                                p90r = a.get("p90_ratio")
                                if p90r is None:
                                    continue
                                nn_s = float(nn_today_d.get(sym, 0.0))
                                m_s = float(moon_today_d.get(sym, 0.0))
                                # H9 isn't computed live; use H7 (p90r) as proxy — stacker was trained with whatever was in scored.
                                h9 = p90r
                                ens_rows.append([nn_s, m_s, p90r, h9])
                                ens_syms.append(sym)
                            if ens_rows:
                                X_ens = ens_scaler.transform(np.array(ens_rows))
                                e_preds = ens_model.predict(X_ens)
                                e_preds = e_preds - e_preds.min() + 0.01
                                ens_by_sym = {s: round(float(p), 4) for s, p in zip(ens_syms, e_preds.tolist())}
                                (ROOT / "data_cache" / "ensemble_scores.json").write_text(
                                    json.dumps(ens_by_sym, separators=(",", ":"))
                                )
                                log.info("Wrote %d ensemble scores (range %.3f..%.3f)",
                                         len(ens_by_sym),
                                         min(ens_by_sym.values()), max(ens_by_sym.values()))
                        except Exception as e:
                            log.warning("Ensemble today-scoring failed: %s", e)
        except Exception as e:
            log.warning("Today-scoring step failed: %s", e)

    return 0


if __name__ == "__main__":
    sys.exit(main())
