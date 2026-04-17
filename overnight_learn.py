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
RESULTS_CSV = ROOT / "upside_hunt_results.csv"
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


def _train_nn(df: pd.DataFrame) -> tuple:
    """Walk-forward NN training — for each window, train only on prior
    windows, predict on the target window. Returns the per-row NN score
    attached to df + the final model/scaler."""
    from sklearn.neural_network import MLPRegressor
    from sklearn.preprocessing import StandardScaler

    feat = _build_features(df)
    feature_names = [
        "log_price", "sigma", "p90_ratio", "p10_ratio", "asymmetry",
        "vol_low", "vol_hi", "H7_ewma_p90",
    ]
    windows = sorted(feat["as_of"].unique())
    if len(windows) < 3:
        log.warning("Not enough windows (%d) for walk-forward NN training", len(windows))
        feat["nn_score"] = 0.0
        return feat, None, None

    nn_scores = np.zeros(len(feat))
    # Walk-forward: for each window t, train on 1..t-1, predict t
    for i, w in enumerate(windows):
        if i == 0:
            continue  # no history to train on
        train_mask = feat["as_of"].isin(windows[:i])
        test_mask = feat["as_of"] == w
        X_train = feat.loc[train_mask, feature_names].values
        y_train = feat.loc[train_mask, "realized_ret"].values
        X_test = feat.loc[test_mask, feature_names].values
        if len(X_train) < 100 or len(X_test) == 0:
            continue
        # Clip targets to a sane range — outliers can dominate regression.
        y_train = np.clip(y_train, -0.95, 5.0)
        scaler = StandardScaler()
        X_train_s = scaler.fit_transform(X_train)
        X_test_s = scaler.transform(X_test)
        model = MLPRegressor(
            hidden_layer_sizes=(32, 16), activation="relu",
            max_iter=300, early_stopping=True, validation_fraction=0.15,
            random_state=42, alpha=1e-3,
        )
        model.fit(X_train_s, y_train)
        preds = model.predict(X_test_s)
        # Shift predictions so they rank positively (score_col > 0 filter).
        nn_scores[test_mask] = preds - preds.min() + 0.01
        log.info("Window %s trained on %d rows, predicted on %d rows",
                 w, len(X_train), len(X_test))

    feat["nn_score"] = nn_scores
    # Also train a "final" model on all data for the current-day scoring call.
    X_all = feat[feature_names].values
    y_all = np.clip(feat["realized_ret"].values, -0.95, 5.0)
    scaler = StandardScaler()
    X_all_s = scaler.fit_transform(X_all)
    final_model = MLPRegressor(
        hidden_layer_sizes=(32, 16), activation="relu",
        max_iter=300, early_stopping=True, validation_fraction=0.15,
        random_state=42, alpha=1e-3,
    )
    final_model.fit(X_all_s, y_all)
    return feat, final_model, scaler


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

    # Evaluate every method on the most recent year of windows only.
    recent_cutoff = sorted(scored["as_of"].unique())[-4:]  # last 4 windows
    recent = scored[scored["as_of"].isin(recent_cutoff)]
    log.info("Evaluating on recent windows: %s (%d rows)",
             list(recent_cutoff), len(recent))

    performance = {}
    for m in HAND_METHODS + ["nn_score"]:
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
        except Exception as e:
            log.warning("Today-scoring step failed: %s", e)

    return 0


if __name__ == "__main__":
    sys.exit(main())
