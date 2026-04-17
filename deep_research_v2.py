"""
Deep research v2 — overnight 6-8 hour batch.

Six phases of compute-intensive NN research. Each phase saves partial
results to research/ so the whole run is restartable / introspectable.

Phases:
  1. Extended feature engineering from the price cache (20/60/180d
     momentum, 52-week-high proximity, realized vol, volume z-score,
     beta vs SPY). Extends upside_hunt_results.csv → upside_hunt_extended.csv.
  2. Base (7-feat) vs extended (~15-feat) comparison. One walk-forward
     run each; measures lift of the new features.
  3. Deep hyperparameter search — ~100 configs across 4 model families.
     Full walk-forward on whichever feature set won phase 2.
  4. Multi-seed ensemble — top config from phase 3 × 30 random seeds.
     Measures pick stability + how much the seed choice moves results.
  5. Robustness under perturbation — 20 reps of dropping 10% random
     training rows. Tests whether the signal is robust to training-set
     sampling noise.
  6. Bootstrap CIs (2000 iterations) + compile final markdown report.

Philosophy:
  - Never commit anything this script produces to main. It's research.
  - Every phase saves to disk the moment it finishes. A crash loses at
    most one phase of work.
  - Heavy logging — user can `tail -f deep_research_v2.log` to monitor.

Runtime estimate on a Mac laptop with 2,300 tickers and 22k rows:
  Phase 1: 30-45 min (disk-IO bound)
  Phase 2: 10 min
  Phase 3: 2-3 hrs (compute bound)
  Phase 4: 1-1.5 hrs
  Phase 5: 30-45 min
  Phase 6: 15 min
  Total:   5-7 hrs
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
        logging.FileHandler("deep_research_v2.log", mode="a"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("deep_research_v2")

ROOT = Path(__file__).parent
PRICES_DIR = ROOT / "data_cache" / "prices"
RESULTS_CSV = ROOT / "upside_hunt_results.csv"
EXT_CSV = ROOT / "upside_hunt_extended.csv"
OUT_DIR = ROOT / "research"
OUT_DIR.mkdir(exist_ok=True)

DATE = datetime.now().strftime("%Y-%m-%d")
REPORT_JSON = OUT_DIR / f"deep_findings_v2_{DATE}.json"
REPORT_MD = OUT_DIR / f"deep_findings_v2_{DATE}.md"

BASE_FEATURES = [
    "log_price", "sigma", "p90_ratio", "p10_ratio", "asymmetry",
    "vol_low", "vol_hi", "H7_ewma_p90",
]
EXTENDED_FEATURES_NEW = [
    "mom_20d", "mom_60d", "mom_180d",
    "high_52w_ratio", "low_52w_ratio",
    "realized_vol_60d", "volume_z60d",
    "beta_180d",
]
TOP_N = 20
THRESHOLD = 1.0

REPORT: dict = {
    "started_at": time.time(),
    "date": DATE,
    "phases": {},
}


def _save_partial():
    try:
        REPORT_JSON.write_text(json.dumps(REPORT, indent=2, default=str))
    except Exception as e:
        log.warning("Failed to save partial report: %s", e)


def _build_base(df):
    f = df.copy()
    f["log_price"] = np.log(f["current"].clip(lower=0.01))
    f["p90_ratio"] = f["p90"] / f["current"].clip(lower=0.01)
    f["p10_ratio"] = f["p10"] / f["current"].clip(lower=0.01)
    f["asymmetry"] = (f["p90_ratio"] - 1) - (1 - f["p10_ratio"])
    f["vol_low"] = (f["sigma"] < 0.30).astype(int)
    f["vol_hi"] = (f["sigma"] > 0.60).astype(int)
    return f


def _load_price(sym: str):
    path = PRICES_DIR / f"{sym}.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, parse_dates=["Date"])
        df = df.sort_values("Date").reset_index(drop=True)
        return df
    except Exception:
        return None


def _compute_features_at(prices, spy, as_of):
    cut = prices[prices["Date"] <= as_of]
    if len(cut) < 200:
        return None
    close = cut["Close"].values
    vol = cut["Volume"].values
    cur = close[-1]
    if cur <= 0:
        return None

    def _pct(h):
        if len(close) <= h:
            return 0.0
        return float((close[-1] - close[-h - 1]) / close[-h - 1])
    mom_20 = _pct(20)
    mom_60 = _pct(60)
    mom_180 = _pct(180)

    yr_close = close[-252:]
    high_52 = float(cur / yr_close.max())
    low_52 = float(cur / yr_close.min())

    rets_60 = np.diff(close[-61:]) / close[-61:-1]
    realized_vol = float(rets_60.std() * np.sqrt(252)) if len(rets_60) else 0.0

    vol_60 = vol[-60:]
    v_mean = vol_60.mean() if len(vol_60) else 1
    v_std = vol_60.std() if len(vol_60) else 1
    volume_z = float((vol[-1] - v_mean) / max(v_std, 1))

    beta = 0.0
    try:
        spy_cut = spy[spy["Date"] <= as_of]
        if len(spy_cut) >= 200:
            ticker_r = np.diff(close[-181:]) / close[-181:-1]
            spy_c = spy_cut["Close"].values[-181:]
            spy_r = np.diff(spy_c) / spy_c[:-1]
            n = min(len(ticker_r), len(spy_r))
            if n > 50:
                x = spy_r[-n:]
                y = ticker_r[-n:]
                var = x.var()
                if var > 0:
                    cov = np.mean((x - x.mean()) * (y - y.mean()))
                    beta = float(cov / var)
    except Exception:
        pass

    return {
        "mom_20d": mom_20,
        "mom_60d": mom_60,
        "mom_180d": mom_180,
        "high_52w_ratio": high_52,
        "low_52w_ratio": low_52,
        "realized_vol_60d": realized_vol,
        "volume_z60d": volume_z,
        "beta_180d": beta,
    }


def phase_1_feature_engineering():
    t0 = time.time()
    log.info("=== PHASE 1 — extended feature engineering ===")
    if EXT_CSV.exists():
        log.info("Extended CSV cached at %s; skipping", EXT_CSV)
        REPORT["phases"]["1_features"] = {
            "status": "cached", "path": str(EXT_CSV),
        }
        _save_partial()
        return pd.read_csv(EXT_CSV, parse_dates=["as_of"])

    df = pd.read_csv(RESULTS_CSV, parse_dates=["as_of"])
    log.info("Base CSV: %d rows, %d tickers, %d dates",
             len(df), df["symbol"].nunique(), df["as_of"].nunique())

    spy = _load_price("SPY")
    if spy is None:
        log.warning("SPY.csv missing — beta=0 for all")
        spy = pd.DataFrame({"Date": [], "Close": []})

    syms = df["symbol"].unique()
    log.info("Loading %d ticker price series …", len(syms))
    price_cache = {}
    for s in syms:
        p = _load_price(s)
        if p is not None:
            price_cache[s] = p
    log.info("Loaded %d / %d (%.0fs)", len(price_cache), len(syms), time.time() - t0)

    new_cols = {k: [] for k in EXTENDED_FEATURES_NEW}
    missing_count = 0
    for idx, row in df.iterrows():
        p = price_cache.get(row["symbol"])
        if p is None:
            missing_count += 1
            for k in EXTENDED_FEATURES_NEW:
                new_cols[k].append(np.nan)
            continue
        feats = _compute_features_at(p, spy, row["as_of"])
        if feats is None:
            missing_count += 1
            for k in EXTENDED_FEATURES_NEW:
                new_cols[k].append(np.nan)
            continue
        for k in EXTENDED_FEATURES_NEW:
            new_cols[k].append(feats[k])
        if idx > 0 and idx % 2000 == 0:
            log.info("  processed %d / %d (%.0fs)", idx, len(df), time.time() - t0)

    for k, v in new_cols.items():
        df[k] = v
    for k in EXTENDED_FEATURES_NEW:
        med = df[k].median()
        df[k] = df[k].fillna(med if pd.notna(med) else 0.0)

    df.to_csv(EXT_CSV, index=False)
    elapsed = time.time() - t0
    REPORT["phases"]["1_features"] = {
        "status": "completed",
        "elapsed_s": round(elapsed, 1),
        "rows": len(df),
        "missing_count": missing_count,
        "new_features": EXTENDED_FEATURES_NEW,
        "path": str(EXT_CSV),
    }
    _save_partial()
    log.info("PHASE 1 done: %d rows, %d missing, %.1f min",
             len(df), missing_count, elapsed / 60)
    return df


def _walk_forward(feat, features, make_model, clip_target=True):
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
        y_train = feat.loc[train_mask, "realized_ret"].values
        X_test = feat.loc[test_mask, features].values
        if len(X_train) < 100 or len(X_test) == 0:
            continue
        if clip_target:
            y_train = np.clip(y_train, -0.95, 5.0)
        scaler = StandardScaler()
        X_tr_s = scaler.fit_transform(X_train)
        X_te_s = scaler.transform(X_test)
        model = make_model()
        model.fit(X_tr_s, y_train)
        scores[test_mask] = model.predict(X_te_s)
    scored = feat.assign(_score=scores - scores.min() + 0.01)
    return scored, windows, time.time() - t0


def _evaluate_recent(scored, windows, threshold=THRESHOLD, recent_k=4):
    recent = windows[-recent_k:]
    recent_df = scored[scored["as_of"].isin(recent)]
    picked = (recent_df.sort_values(["as_of", "_score"], ascending=[True, False])
                        .groupby("as_of").head(TOP_N))
    if len(picked) == 0:
        return {"rate": 0, "mean": 0, "median": 0, "n": 0}
    return {
        "rate": float((picked["realized_ret"] >= threshold).mean()),
        "mean": float(picked["realized_ret"].mean()),
        "median": float(picked["realized_ret"].median()),
        "n": int(len(picked)),
    }


def phase_2_base_vs_extended(df):
    t0 = time.time()
    log.info("=== PHASE 2 — base vs extended features ===")
    from sklearn.neural_network import MLPRegressor
    from sklearn.ensemble import ExtraTreesRegressor

    feat_ext = _build_base(df)
    all_ext_features = BASE_FEATURES + EXTENDED_FEATURES_NEW

    def mlp():
        return MLPRegressor(hidden_layer_sizes=(32, 16), activation="relu",
                            max_iter=300, early_stopping=True,
                            validation_fraction=0.15, random_state=42,
                            alpha=1e-3)

    def et():
        return ExtraTreesRegressor(n_estimators=300, max_depth=14,
                                   min_samples_leaf=10, n_jobs=-1,
                                   random_state=42)

    variants = {}
    for model_name, factory in (("mlp", mlp), ("et", et)):
        for feat_name, features in (("base_8", BASE_FEATURES),
                                    ("extended_16", all_ext_features)):
            key = f"{model_name}_{feat_name}"
            log.info("  %s (%d feats) …", key, len(features))
            scored, windows, el = _walk_forward(
                feat_ext, features, factory,
                clip_target=(model_name == "mlp"))
            r = _evaluate_recent(scored, windows)
            r["features"] = features
            r["elapsed_s"] = round(el, 1)
            variants[key] = r
            log.info("    %s  rate=%.3f mean=%+.1f%% (%.0fs)",
                     key, r["rate"], r["mean"] * 100, el)

    REPORT["phases"]["2_base_vs_extended"] = {
        "status": "completed",
        "elapsed_s": round(time.time() - t0, 1),
        "variants": variants,
    }
    _save_partial()
    log.info("PHASE 2 done (%.1f min)", (time.time() - t0) / 60)

    best_key = max(variants.keys(), key=lambda k: (variants[k]["rate"],
                                                    variants[k]["mean"]))
    log.info("Phase 2 winner: %s → phase 3 uses these", best_key)
    return feat_ext, variants[best_key]["features"]


def _save_intermediate_hp(results, t0):
    REPORT["phases"]["3_hyperparameter_search"] = {
        "status": "in_progress",
        "elapsed_s": round(time.time() - t0, 1),
        "completed_configs": len(results),
        "partial_results": results,
    }
    _save_partial()


def phase_3_hyperparameter_search(feat, features):
    t0 = time.time()
    log.info("=== PHASE 3 — deep hyperparameter search ===")
    from sklearn.neural_network import MLPRegressor
    from sklearn.ensemble import (ExtraTreesRegressor, RandomForestRegressor,
                                  GradientBoostingRegressor)

    mlp_grid = list(itertools.product(
        [(16, 8), (32, 16), (64, 32), (32, 16, 8)],
        [1e-4, 1e-3, 1e-2],
        ["relu", "tanh"],
    ))
    et_grid = list(itertools.product(
        [100, 300, 500],
        [8, 14, 20],
        [5, 10, 20],
    ))
    rf_grid = list(itertools.product(
        [100, 300, 500],
        [8, 14, 20],
        [10, 20, 40],
    ))
    gb_grid = list(itertools.product(
        [100, 200, 400],
        [3, 4, 5],
        [0.02, 0.05, 0.1],
    ))
    total = len(mlp_grid) + len(et_grid) + len(rf_grid) + len(gb_grid)
    log.info("Total configs: %d (MLP=%d, ET=%d, RF=%d, GB=%d)",
             total, len(mlp_grid), len(et_grid), len(rf_grid), len(gb_grid))

    results = {}
    done = 0

    def _run_configs(grid, family, factory_for):
        nonlocal done
        for params in grid:
            key = f"{family}_{'_'.join(str(p) for p in params)}"
            factory = factory_for(*params)
            scored, windows, el = _walk_forward(
                feat, features, factory, clip_target=(family == "mlp"))
            r = _evaluate_recent(scored, windows)
            r["elapsed_s"] = round(el, 1)
            r["config"] = {"family": family, "params": list(params)}
            results[key] = r
            done += 1
            if done % 6 == 0:
                log.info("  [%d/%d] %s rate=%.3f mean=%+.1f%%",
                         done, total, key, r["rate"], r["mean"] * 100)
                _save_intermediate_hp(results, t0)

    _run_configs(mlp_grid, "mlp",
                 lambda sizes, alpha, act: (lambda: MLPRegressor(
                     hidden_layer_sizes=sizes, activation=act,
                     max_iter=300, early_stopping=True,
                     validation_fraction=0.15, random_state=42, alpha=alpha)))
    _run_configs(et_grid, "et",
                 lambda n, d, l: (lambda: ExtraTreesRegressor(
                     n_estimators=n, max_depth=d, min_samples_leaf=l,
                     n_jobs=-1, random_state=42)))
    _run_configs(rf_grid, "rf",
                 lambda n, d, l: (lambda: RandomForestRegressor(
                     n_estimators=n, max_depth=d, min_samples_leaf=l,
                     n_jobs=-1, random_state=42)))
    _run_configs(gb_grid, "gb",
                 lambda n, d, lr: (lambda: GradientBoostingRegressor(
                     n_estimators=n, max_depth=d, learning_rate=lr,
                     subsample=0.8, random_state=42)))

    ranked = sorted(results.items(),
                    key=lambda kv: (kv[1]["rate"], kv[1]["mean"]),
                    reverse=True)
    log.info("PHASE 3 complete in %.1f min. Top 5:", (time.time() - t0) / 60)
    for i, (k, r) in enumerate(ranked[:5]):
        log.info("  #%d  %s  rate=%.3f mean=%+.1f%%",
                 i + 1, k, r["rate"], r["mean"] * 100)

    REPORT["phases"]["3_hyperparameter_search"] = {
        "status": "completed",
        "elapsed_s": round(time.time() - t0, 1),
        "total_configs": total,
        "results": results,
        "top_5": [k for k, _ in ranked[:5]],
    }
    _save_partial()
    return results[ranked[0][0]]["config"]


def phase_4_multi_seed(feat, features, top_config, n_seeds=30):
    t0 = time.time()
    log.info("=== PHASE 4 — multi-seed ensemble (%d seeds) ===", n_seeds)
    from sklearn.neural_network import MLPRegressor
    from sklearn.ensemble import (ExtraTreesRegressor, RandomForestRegressor,
                                  GradientBoostingRegressor)

    family = top_config["family"]
    params = top_config["params"]

    def factory_for_seed(seed):
        if family == "mlp":
            sizes, alpha, act = params
            return lambda: MLPRegressor(
                hidden_layer_sizes=tuple(sizes) if isinstance(sizes, list) else sizes,
                activation=act, max_iter=300,
                early_stopping=True, validation_fraction=0.15,
                random_state=seed, alpha=alpha,
            )
        if family == "et":
            n, d, l = params
            return lambda: ExtraTreesRegressor(
                n_estimators=n, max_depth=d, min_samples_leaf=l,
                n_jobs=-1, random_state=seed,
            )
        if family == "rf":
            n, d, l = params
            return lambda: RandomForestRegressor(
                n_estimators=n, max_depth=d, min_samples_leaf=l,
                n_jobs=-1, random_state=seed,
            )
        n, d, lr = params
        return lambda: GradientBoostingRegressor(
            n_estimators=n, max_depth=d, learning_rate=lr,
            subsample=0.8, random_state=seed,
        )

    per_seed = []
    pick_sets = []
    for seed in range(n_seeds):
        factory = factory_for_seed(seed)
        scored, windows, el = _walk_forward(
            feat, features, factory, clip_target=(family == "mlp"))
        r = _evaluate_recent(scored, windows)
        r["seed"] = seed
        r["elapsed_s"] = round(el, 1)
        per_seed.append(r)
        recent = sorted(scored["as_of"].unique())[-1]
        top = scored[scored["as_of"] == recent].nlargest(TOP_N, "_score")
        pick_sets.append(set(top["symbol"].tolist()))
        if (seed + 1) % 5 == 0:
            log.info("  seed %d/%d  rate=%.3f mean=%+.1f%% (%.0fs)",
                     seed + 1, n_seeds, r["rate"], r["mean"] * 100, el)

    rates = [r["rate"] for r in per_seed]
    means = [r["mean"] for r in per_seed]
    jaccards = []
    for i in range(len(pick_sets)):
        for j in range(i + 1, len(pick_sets)):
            inter = len(pick_sets[i] & pick_sets[j])
            union = len(pick_sets[i] | pick_sets[j])
            if union > 0:
                jaccards.append(inter / union)

    REPORT["phases"]["4_multi_seed"] = {
        "status": "completed",
        "elapsed_s": round(time.time() - t0, 1),
        "n_seeds": n_seeds,
        "top_config": top_config,
        "rate_mean": float(np.mean(rates)),
        "rate_std": float(np.std(rates)),
        "rate_min": float(np.min(rates)),
        "rate_max": float(np.max(rates)),
        "return_mean": float(np.mean(means)),
        "return_std": float(np.std(means)),
        "jaccard_mean": float(np.mean(jaccards)) if jaccards else 0,
        "jaccard_std": float(np.std(jaccards)) if jaccards else 0,
        "per_seed": per_seed,
    }
    _save_partial()
    log.info("PHASE 4 done: rate %.3f ± %.3f, Jaccard %.3f (%.1f min)",
             np.mean(rates), np.std(rates),
             np.mean(jaccards) if jaccards else 0,
             (time.time() - t0) / 60)


def phase_5_perturbation(feat, features, top_config, n_reps=20, drop_frac=0.10):
    t0 = time.time()
    log.info("=== PHASE 5 — robustness perturbation (%d reps, drop %.0f%%) ===",
             n_reps, drop_frac * 100)
    from sklearn.neural_network import MLPRegressor
    from sklearn.ensemble import (ExtraTreesRegressor, RandomForestRegressor,
                                  GradientBoostingRegressor)
    from sklearn.preprocessing import StandardScaler

    family = top_config["family"]
    params = top_config["params"]

    def build_model():
        if family == "mlp":
            sizes, alpha, act = params
            return MLPRegressor(
                hidden_layer_sizes=tuple(sizes) if isinstance(sizes, list) else sizes,
                activation=act, max_iter=300, early_stopping=True,
                validation_fraction=0.15, random_state=42, alpha=alpha,
            )
        if family == "et":
            n, d, l = params
            return ExtraTreesRegressor(n_estimators=n, max_depth=d,
                                       min_samples_leaf=l, n_jobs=-1,
                                       random_state=42)
        if family == "rf":
            n, d, l = params
            return RandomForestRegressor(n_estimators=n, max_depth=d,
                                         min_samples_leaf=l, n_jobs=-1,
                                         random_state=42)
        n, d, lr = params
        return GradientBoostingRegressor(n_estimators=n, max_depth=d,
                                         learning_rate=lr, subsample=0.8,
                                         random_state=42)

    windows = sorted(feat["as_of"].unique())
    per_rep = []
    pick_sets = []
    for rep in range(n_reps):
        rng = np.random.default_rng(rep + 1000)
        scores = np.zeros(len(feat))
        for i, w in enumerate(windows):
            if i == 0:
                continue
            train_mask = feat["as_of"].isin(windows[:i]).values
            test_mask = (feat["as_of"] == w).values
            train_idx = np.where(train_mask)[0]
            keep = rng.choice(train_idx,
                              size=int(len(train_idx) * (1 - drop_frac)),
                              replace=False)
            X_train = feat.loc[keep, features].values
            y_train = feat.loc[keep, "realized_ret"].values
            X_test = feat.loc[test_mask, features].values
            if len(X_train) < 100 or len(X_test) == 0:
                continue
            if family == "mlp":
                y_train = np.clip(y_train, -0.95, 5.0)
            scaler = StandardScaler()
            X_tr_s = scaler.fit_transform(X_train)
            X_te_s = scaler.transform(X_test)
            model = build_model()
            model.fit(X_tr_s, y_train)
            scores[test_mask] = model.predict(X_te_s)

        scored = feat.assign(_score=scores - scores.min() + 0.01)
        r = _evaluate_recent(scored, windows)
        per_rep.append(r)
        recent = windows[-1]
        top = scored[scored["as_of"] == recent].nlargest(TOP_N, "_score")
        pick_sets.append(set(top["symbol"].tolist()))
        if (rep + 1) % 5 == 0:
            log.info("  rep %d/%d rate=%.3f mean=%+.1f%%",
                     rep + 1, n_reps, r["rate"], r["mean"] * 100)

    rates = [r["rate"] for r in per_rep]
    means = [r["mean"] for r in per_rep]
    jaccards = []
    for i in range(len(pick_sets)):
        for j in range(i + 1, len(pick_sets)):
            inter = len(pick_sets[i] & pick_sets[j])
            union = len(pick_sets[i] | pick_sets[j])
            if union > 0:
                jaccards.append(inter / union)

    REPORT["phases"]["5_perturbation"] = {
        "status": "completed",
        "elapsed_s": round(time.time() - t0, 1),
        "n_reps": n_reps,
        "drop_frac": drop_frac,
        "rate_mean": float(np.mean(rates)),
        "rate_std": float(np.std(rates)),
        "return_mean": float(np.mean(means)),
        "return_std": float(np.std(means)),
        "jaccard_mean": float(np.mean(jaccards)) if jaccards else 0,
        "jaccard_std": float(np.std(jaccards)) if jaccards else 0,
    }
    _save_partial()
    log.info("PHASE 5 done: rate %.3f ± %.3f, Jaccard %.3f (%.1f min)",
             np.mean(rates), np.std(rates),
             np.mean(jaccards) if jaccards else 0,
             (time.time() - t0) / 60)


def phase_6_bootstrap_and_report(n_iters=2000):
    t0 = time.time()
    log.info("=== PHASE 6 — bootstrap CIs (%d iters) + report ===", n_iters)

    cis = {}
    ms = REPORT["phases"].get("4_multi_seed", {})
    if ms.get("per_seed"):
        rates = np.array([s["rate"] for s in ms["per_seed"]])
        means = np.array([s["mean"] for s in ms["per_seed"]])
        rng = np.random.default_rng(42)

        def _boot(arr):
            boots = [rng.choice(arr, size=len(arr), replace=True).mean()
                     for _ in range(n_iters)]
            boots.sort()
            return (float(boots[int(0.025 * n_iters)]),
                    float(boots[int(0.975 * n_iters)]))

        cis["rate_95ci"] = _boot(rates)
        cis["mean_return_95ci"] = _boot(means)

    REPORT["phases"]["6_bootstrap"] = {
        "status": "completed",
        "elapsed_s": round(time.time() - t0, 1),
        "n_iters": n_iters,
        "confidence_intervals": cis,
    }

    md = [
        f"# Deep research v2 findings — {DATE}",
        "",
        f"Total runtime: **{(time.time() - REPORT['started_at']) / 60:.1f} min**",
        "",
    ]
    p1 = REPORT["phases"].get("1_features", {})
    md += ["## Phase 1 — extended feature engineering", "",
           f"- Status: {p1.get('status')}",
           f"- Rows processed: {p1.get('rows', 0)}",
           f"- Missing-feature rows: {p1.get('missing_count', 0)}",
           f"- Elapsed: {p1.get('elapsed_s', 0)}s",
           f"- New features added: {', '.join(p1.get('new_features', []))}",
           ""]

    p2 = REPORT["phases"].get("2_base_vs_extended", {})
    if p2.get("variants"):
        md += ["## Phase 2 — base vs extended features", "",
               "| variant | hit rate | mean return |",
               "|---|---|---|"]
        for k, v in sorted(p2["variants"].items(),
                           key=lambda kv: (kv[1]["rate"], kv[1]["mean"]),
                           reverse=True):
            md.append(f"| `{k}` | {v['rate']:.3f} | {v['mean']*100:+.1f}% |")
        md.append("")

    p3 = REPORT["phases"].get("3_hyperparameter_search", {})
    if p3.get("results"):
        md += ["## Phase 3 — deep hyperparameter search",
               "",
               f"Evaluated {p3.get('total_configs', 0)} configs. Top 10:",
               "",
               "| rank | config | hit rate | mean return |",
               "|---|---|---|---|"]
        ranked = sorted(p3["results"].items(),
                        key=lambda kv: (kv[1]["rate"], kv[1]["mean"]),
                        reverse=True)
        for i, (k, v) in enumerate(ranked[:10]):
            md.append(f"| {i+1} | `{k}` | {v['rate']:.3f} | {v['mean']*100:+.1f}% |")
        md.append("")

    if ms.get("n_seeds"):
        md += ["## Phase 4 — multi-seed ensemble stability", "",
               f"- Seeds: {ms['n_seeds']}",
               f"- Hit rate: **{ms['rate_mean']:.3f} ± {ms['rate_std']:.3f}** (min {ms['rate_min']:.3f}, max {ms['rate_max']:.3f})",
               f"- Mean return: **{ms['return_mean']*100:+.1f}% ± {ms['return_std']*100:.1f}%**",
               f"- Jaccard (pick stability across seeds): **{ms['jaccard_mean']:.3f} ± {ms['jaccard_std']:.3f}**",
               ""]

    p5 = REPORT["phases"].get("5_perturbation", {})
    if p5.get("n_reps"):
        md += ["## Phase 5 — robustness under perturbation",
               "",
               f"- Reps: {p5['n_reps']}, dropped {p5['drop_frac']*100:.0f}% of training rows per rep",
               f"- Hit rate: **{p5['rate_mean']:.3f} ± {p5['rate_std']:.3f}**",
               f"- Mean return: **{p5['return_mean']*100:+.1f}% ± {p5['return_std']*100:.1f}%**",
               f"- Jaccard (pick stability under perturbation): **{p5['jaccard_mean']:.3f}**",
               ""]

    if cis:
        md += ["## Phase 6 — bootstrap 95% confidence intervals", ""]
        if "rate_95ci" in cis:
            md.append(f"- Hit rate 95% CI: **[{cis['rate_95ci'][0]:.3f}, {cis['rate_95ci'][1]:.3f}]**")
        if "mean_return_95ci" in cis:
            md.append(f"- Mean return 95% CI: **[{cis['mean_return_95ci'][0]*100:+.1f}%, {cis['mean_return_95ci'][1]*100:+.1f}%]**")
        md.append("")

    md += ["## Notes", "",
           "- Walk-forward CV: for each window t, train on windows < t, score window t.",
           "- Hit rate = % of top-20 picks that reach +100% return within 12 months.",
           "- Recent-4 aggregation: windows with complete 12mo forward data.",
           "- Jaccard measures pick-set overlap (1.0 = identical sets, 0 = disjoint).",
           "",
           f"Report generated: {datetime.now().isoformat()}",
           ]
    REPORT_MD.write_text("\n".join(md))
    REPORT["completed_at"] = time.time()
    REPORT["total_runtime_min"] = round(
        (REPORT["completed_at"] - REPORT["started_at"]) / 60, 1)
    _save_partial()
    log.info("PHASE 6 done; report → %s", REPORT_MD)


def run():
    try:
        df = phase_1_feature_engineering()
        feat, best_features = phase_2_base_vs_extended(df)
        top_config = phase_3_hyperparameter_search(feat, best_features)
        phase_4_multi_seed(feat, best_features, top_config, n_seeds=30)
        phase_5_perturbation(feat, best_features, top_config, n_reps=20)
        phase_6_bootstrap_and_report(n_iters=2000)
        log.info("DEEP RESEARCH V2 COMPLETE — %s min",
                 REPORT.get("total_runtime_min"))
        return 0
    except Exception as e:
        log.exception("Deep research crashed: %s", e)
        REPORT["crashed_at"] = time.time()
        REPORT["crash_traceback"] = traceback.format_exc()
        _save_partial()
        return 1


if __name__ == "__main__":
    sys.exit(run())
