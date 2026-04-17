"""
Deep research v4 — the honesty round.

v2 + v3 consistently showed one result: the NN's edge at picking
+100% winners is driven almost entirely by `log_price`. Permutation
importance in v3 was `log_price` +0.505 vs every other feature at
<0.06. That's suspicious — it suggests the model is just picking
small-cap stocks and riding the small-cap premium, rather than
finding actual alpha.

v4 asks the tough questions:

  I. Price-controlled study — within each log_price quintile, does
     the model still find picks that beat the within-bucket baseline?
     If yes → the model has real alpha beyond small-cap beta.
     If no → the edge IS just small-cap, and we should re-frame.

  J. Excess-return labeling — retrain with the realized return MINUS
     the universe-median return for that window (regime-adjusted
     alpha). Does the model still work when we strip out the "2022
     was a good year for small-caps" effect?

  K. Size-neutral ensemble — force equal representation across price
     quintiles when picking top-20. Does this hurt the hit rate?
     How much?

  L. Year-out-of-sample — train on 2022-2023 windows only, test on
     2024 windows in complete isolation. No hyperparameter tuning
     on 2024. This is a true OOS test the research so far has not
     delivered.

  M. Feature-interaction engineering — hand-add log_price × sigma,
     log_price × mom_180d, etc. Does giving the model explicit
     interaction terms improve it, or is the ET already capturing
     those?

  N. Consolidated honest-findings report with caveats. The goal here
     is NOT to "make the numbers look better" — it's to establish
     what the model can and can't do, before we trade real money.

Runtime: ~30-60 minutes. Not huge compute; the experiments are more
focused than v2/v3's grid searches.
"""
from __future__ import annotations

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
        logging.FileHandler("deep_research_v4.log", mode="a"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("deep_research_v4")

ROOT = Path(__file__).parent
EXT_CSV = ROOT / "upside_hunt_extended.csv"
RESULTS_CSV = ROOT / "upside_hunt_results.csv"
OUT_DIR = ROOT / "research"
OUT_DIR.mkdir(exist_ok=True)

DATE = datetime.now().strftime("%Y-%m-%d")
REPORT_JSON = OUT_DIR / f"deep_findings_v4_{DATE}.json"
REPORT_MD = OUT_DIR / f"deep_findings_v4_{DATE}.md"

ALL_FEATURES = [
    "log_price", "sigma", "p90_ratio", "p10_ratio", "asymmetry",
    "vol_low", "vol_hi", "H7_ewma_p90",
    "mom_20d", "mom_60d", "mom_180d",
    "high_52w_ratio", "low_52w_ratio",
    "realized_vol_60d", "volume_z60d", "beta_180d",
]
WINNING_CONFIG = {"n_estimators": 300, "max_depth": 14, "min_samples_leaf": 20}
TOP_N = 20
REPORT: dict = {"started_at": time.time(), "date": DATE,
                "preceded_by": ["deep_findings_v2", "deep_findings_v3"],
                "phases": {}}


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


def _load():
    if EXT_CSV.exists():
        df = pd.read_csv(EXT_CSV, parse_dates=["as_of"])
    else:
        df = pd.read_csv(RESULTS_CSV, parse_dates=["as_of"])
    return _build_base(df)


def _et_regressor(seed=42):
    from sklearn.ensemble import ExtraTreesRegressor
    return ExtraTreesRegressor(
        n_estimators=WINNING_CONFIG["n_estimators"],
        max_depth=WINNING_CONFIG["max_depth"],
        min_samples_leaf=WINNING_CONFIG["min_samples_leaf"],
        n_jobs=-1, random_state=seed,
    )


def _walk_forward(feat, features, target_col, clip=False, seed=42):
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
        y_train = feat.loc[train_mask, target_col].values
        X_test = feat.loc[test_mask, features].values
        if len(X_train) < 100 or len(X_test) == 0:
            continue
        if clip:
            y_train = np.clip(y_train, -0.95, 5.0)
        scaler = StandardScaler()
        X_tr = scaler.fit_transform(X_train)
        X_te = scaler.transform(X_test)
        m = _et_regressor(seed=seed)
        m.fit(X_tr, y_train)
        scores[test_mask] = m.predict(X_te)
    scored = feat.assign(_score=scores - scores.min() + 0.01)
    return scored, windows, time.time() - t0


def _pick_top_n(scored, windows, recent_k=4, top_n=TOP_N):
    recent = windows[-recent_k:]
    r = scored[scored["as_of"].isin(recent)]
    return (r.sort_values(["as_of", "_score"], ascending=[True, False])
             .groupby("as_of").head(top_n))


# ─────────────────────────────────────
# PHASE I — price-controlled study
# ─────────────────────────────────────

def phase_I_price_controlled(feat):
    t0 = time.time()
    log.info("=== PHASE I — price-controlled study ===")
    # Bucket each row into log_price quintiles (within its window so the
    # ranking is time-stable). Then train a model per quintile and measure
    # hit rate of top-N within each bucket vs within-bucket baseline.
    feat = feat.copy()
    feat["price_q"] = feat.groupby("as_of")["log_price"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False,
                          duplicates="drop"))

    results = {}
    baseline_rates = {}
    for q in sorted(feat["price_q"].dropna().unique()):
        sub = feat[feat["price_q"] == q].copy()
        log.info("  quintile %d: %d rows", int(q), len(sub))
        if len(sub) < 500:
            continue
        baseline_rates[int(q)] = float((sub["realized_ret"] >= 1.0).mean())
        scored, windows, el = _walk_forward(sub, ALL_FEATURES, "realized_ret")
        if scored is None:
            continue
        picked = _pick_top_n(scored, windows)
        if len(picked) == 0:
            continue
        hit_rate = float((picked["realized_ret"] >= 1.0).mean())
        mean_return = float(picked["realized_ret"].mean())
        results[f"quintile_{int(q)+1}"] = {
            "n_rows": int(len(sub)),
            "n_picks": int(len(picked)),
            "within_bucket_baseline": baseline_rates[int(q)],
            "model_hit_rate": hit_rate,
            "model_lift": (hit_rate / baseline_rates[int(q)]
                          if baseline_rates[int(q)] > 0 else 0),
            "mean_return": mean_return,
            "elapsed_s": round(el, 1),
        }
        log.info("    q%d  rate=%.3f lift=%.2fx mean=%+.1f%% (bucket baseline %.3f)",
                 int(q) + 1, hit_rate,
                 hit_rate / max(baseline_rates[int(q)], 1e-9),
                 mean_return * 100,
                 baseline_rates[int(q)])

    # Interpretation helper: if lift across ALL quintiles stays >1.5x, the
    # model finds alpha within price levels. If lift collapses to ~1.0x,
    # it's just picking small-cap.
    lifts = [r["model_lift"] for r in results.values() if r.get("model_lift")]
    median_lift = float(np.median(lifts)) if lifts else 0
    log.info("  median within-quintile lift: %.2fx", median_lift)

    REPORT["phases"]["I_price_controlled"] = {
        "status": "completed",
        "elapsed_s": round(time.time() - t0, 1),
        "quintiles": results,
        "median_within_quintile_lift": median_lift,
        "interpretation": ("Strong within-quintile lift → model has alpha "
                          "beyond small-cap beta. Lift near 1.0 → the edge "
                          "IS the small-cap premium."),
    }
    _save_partial()
    log.info("PHASE I done (%.1f min)", (time.time() - t0) / 60)


# ─────────────────────────────────────
# PHASE J — excess-return labeling
# ─────────────────────────────────────

def phase_J_excess_return(feat):
    t0 = time.time()
    log.info("=== PHASE J — excess-return labeling ===")
    feat = feat.copy()
    # For each window, compute universe median return and subtract.
    feat["universe_median"] = feat.groupby("as_of")["realized_ret"].transform("median")
    feat["excess_ret"] = feat["realized_ret"] - feat["universe_median"]

    scored, windows, el = _walk_forward(feat, ALL_FEATURES, "excess_ret", clip=False)
    picked = _pick_top_n(scored, windows)
    hit_abs = float((picked["realized_ret"] >= 1.0).mean())
    hit_rel = float((picked["excess_ret"] >= 0.5).mean())  # beating market by +50%
    log.info("  excess-labeled model: abs_hit_100=%.3f excess_hit_50=%.3f mean_abs=%+.1f%%",
             hit_abs, hit_rel, picked["realized_ret"].mean() * 100)

    # Control: regular (absolute) labeling, same features and model config.
    scored_abs, _, _ = _walk_forward(feat, ALL_FEATURES, "realized_ret")
    picked_abs = _pick_top_n(scored_abs, windows)
    hit_control = float((picked_abs["realized_ret"] >= 1.0).mean())
    log.info("  absolute-labeled control: hit_100=%.3f mean=%+.1f%%",
             hit_control, picked_abs["realized_ret"].mean() * 100)

    REPORT["phases"]["J_excess_return"] = {
        "status": "completed",
        "elapsed_s": round(time.time() - t0, 1),
        "excess_labeled": {
            "hit_rate_at_100_abs": hit_abs,
            "hit_rate_at_50_excess": hit_rel,
            "mean_absolute_return": float(picked["realized_ret"].mean()),
        },
        "absolute_labeled_control": {
            "hit_rate_at_100": hit_control,
            "mean_return": float(picked_abs["realized_ret"].mean()),
        },
        "interpretation": ("If excess-labeled hit rate holds near the absolute "
                          "rate, the model captures regime-independent alpha. "
                          "If it collapses, the model is mostly picking up "
                          "high-beta names during bull windows."),
    }
    _save_partial()
    log.info("PHASE J done (%.1f min)", (time.time() - t0) / 60)


# ─────────────────────────────────────
# PHASE K — size-neutral ensemble
# ─────────────────────────────────────

def phase_K_size_neutral(feat):
    t0 = time.time()
    log.info("=== PHASE K — size-neutral ensemble (equal picks per quintile) ===")
    feat = feat.copy()
    feat["price_q"] = feat.groupby("as_of")["log_price"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False,
                          duplicates="drop"))
    scored, windows, el = _walk_forward(feat, ALL_FEATURES, "realized_ret")
    scored["price_q"] = feat["price_q"].values

    recent = windows[-4:]
    rdf = scored[scored["as_of"].isin(recent)]
    # Size-neutral: pick top-4 per quintile per window → 20 picks total
    per_bucket = 4
    picks_neutral = []
    for _, g in rdf.groupby("as_of"):
        for q in sorted(g["price_q"].dropna().unique()):
            bucket = g[g["price_q"] == q]
            picks_neutral.append(bucket.nlargest(per_bucket, "_score"))
    if picks_neutral:
        picks_neutral = pd.concat(picks_neutral)
    else:
        picks_neutral = pd.DataFrame()

    # Compare to unconstrained top-20.
    picks_uncon = (rdf.sort_values(["as_of", "_score"], ascending=[True, False])
                        .groupby("as_of").head(TOP_N))

    def _stats(picks):
        if len(picks) == 0:
            return {"n": 0}
        return {
            "n": int(len(picks)),
            "hit_rate_100": float((picks["realized_ret"] >= 1.0).mean()),
            "hit_rate_200": float((picks["realized_ret"] >= 2.0).mean()),
            "mean_return": float(picks["realized_ret"].mean()),
            "median_return": float(picks["realized_ret"].median()),
        }

    neutral = _stats(picks_neutral)
    uncon = _stats(picks_uncon)
    log.info("  unconstrained top-20:  rate=%.3f mean=%+.1f%%",
             uncon.get("hit_rate_100", 0), uncon.get("mean_return", 0) * 100)
    log.info("  size-neutral (4/q):   rate=%.3f mean=%+.1f%%",
             neutral.get("hit_rate_100", 0), neutral.get("mean_return", 0) * 100)

    # Also report per-quintile composition of unconstrained picks.
    composition = {}
    if len(picks_uncon) > 0:
        for q in range(5):
            composition[f"quintile_{q+1}"] = int((picks_uncon["price_q"] == q).sum())

    REPORT["phases"]["K_size_neutral"] = {
        "status": "completed",
        "elapsed_s": round(time.time() - t0, 1),
        "unconstrained": uncon,
        "size_neutral": neutral,
        "unconstrained_quintile_composition": composition,
        "interpretation": ("If size-neutral rate is close to unconstrained, "
                          "the model has edge across all sizes. If it "
                          "drops sharply, most of the edge concentrates "
                          "in one (small-cap) quintile."),
    }
    _save_partial()
    log.info("PHASE K done (%.1f min)", (time.time() - t0) / 60)


# ─────────────────────────────────────
# PHASE L — year-out-of-sample
# ─────────────────────────────────────

def phase_L_year_oos(feat):
    t0 = time.time()
    log.info("=== PHASE L — year-out-of-sample (train 2022-2023, test 2024) ===")
    feat = feat.copy()
    # Get year from as_of.
    years = feat["as_of"].dt.year if hasattr(feat["as_of"], "dt") else pd.to_datetime(feat["as_of"]).dt.year
    feat["_year"] = years
    train = feat[feat["_year"].isin([2022, 2023])].copy()
    test = feat[feat["_year"] == 2024].copy()
    log.info("  train windows: %d rows; test windows: %d rows",
             len(train), len(test))
    if len(train) < 500 or len(test) < 500:
        log.warning("Insufficient data for year-OOS split")
        REPORT["phases"]["L_year_oos"] = {"status": "skipped"}
        _save_partial()
        return

    from sklearn.preprocessing import StandardScaler
    scaler = StandardScaler()
    X_train = scaler.fit_transform(train[ALL_FEATURES].values)
    y_train = train["realized_ret"].values
    X_test = scaler.transform(test[ALL_FEATURES].values)

    m = _et_regressor()
    m.fit(X_train, y_train)
    preds = m.predict(X_test)
    test["_score"] = preds - preds.min() + 0.01

    picked = (test.sort_values(["as_of", "_score"], ascending=[True, False])
                    .groupby("as_of").head(TOP_N))
    hit = float((picked["realized_ret"] >= 1.0).mean())
    mean = float(picked["realized_ret"].mean())
    baseline = float((test["realized_ret"] >= 1.0).mean())
    log.info("  2024 OOS: hit=%.3f lift=%.2fx mean=%+.1f%% (baseline %.3f)",
             hit, hit / max(baseline, 1e-9), mean * 100, baseline)

    REPORT["phases"]["L_year_oos"] = {
        "status": "completed",
        "elapsed_s": round(time.time() - t0, 1),
        "train_rows": int(len(train)),
        "test_rows": int(len(test)),
        "test_windows": int(test["as_of"].nunique()),
        "hit_rate_100": hit,
        "mean_return": mean,
        "baseline_rate": baseline,
        "lift": hit / baseline if baseline > 0 else 0,
        "interpretation": ("This is the cleanest OOS test possible: no 2024 "
                          "data ever seen during training/tuning. If lift "
                          "holds here near the recent-4-window numbers, the "
                          "model generalizes. If it collapses, prior reports "
                          "were at least partially overfit."),
    }
    _save_partial()
    log.info("PHASE L done (%.1f min)", (time.time() - t0) / 60)


# ─────────────────────────────────────
# PHASE M — feature interactions
# ─────────────────────────────────────

def phase_M_interactions(feat):
    t0 = time.time()
    log.info("=== PHASE M — feature interactions ===")
    feat = feat.copy()
    # Explicit pairwise interactions of log_price with everything.
    for other in ["sigma", "mom_20d", "mom_60d", "mom_180d",
                  "realized_vol_60d", "beta_180d", "high_52w_ratio"]:
        if other in feat.columns:
            feat[f"lp_x_{other}"] = feat["log_price"] * feat[other]
    interaction_cols = [c for c in feat.columns if c.startswith("lp_x_")]
    features_ext = ALL_FEATURES + interaction_cols
    log.info("  feature count: %d base + %d interactions = %d total",
             len(ALL_FEATURES), len(interaction_cols), len(features_ext))

    # Baseline: ALL_FEATURES only
    scored_b, windows, el_b = _walk_forward(feat, ALL_FEATURES, "realized_ret")
    picks_b = _pick_top_n(scored_b, windows)
    hit_b = float((picks_b["realized_ret"] >= 1.0).mean())
    mean_b = float(picks_b["realized_ret"].mean())

    # With interactions
    scored_e, windows, el_e = _walk_forward(feat, features_ext, "realized_ret")
    picks_e = _pick_top_n(scored_e, windows)
    hit_e = float((picks_e["realized_ret"] >= 1.0).mean())
    mean_e = float(picks_e["realized_ret"].mean())

    log.info("  base (%d feats):          rate=%.3f mean=%+.1f%%",
             len(ALL_FEATURES), hit_b, mean_b * 100)
    log.info("  with interactions (%d):   rate=%.3f mean=%+.1f%%",
             len(features_ext), hit_e, mean_e * 100)

    REPORT["phases"]["M_interactions"] = {
        "status": "completed",
        "elapsed_s": round(time.time() - t0, 1),
        "base": {"hit_rate": hit_b, "mean_return": mean_b,
                 "n_features": len(ALL_FEATURES)},
        "with_interactions": {"hit_rate": hit_e, "mean_return": mean_e,
                              "n_features": len(features_ext),
                              "interaction_features": interaction_cols},
        "interpretation": ("Tree ensembles already capture nonlinear "
                          "interactions internally. If explicit interaction "
                          "features don't help, it's because the ET is "
                          "already doing this work. If they DO help, the "
                          "ET wasn't finding them — informs future feature "
                          "engineering priorities."),
    }
    _save_partial()
    log.info("PHASE M done (%.1f min)", (time.time() - t0) / 60)


# ─────────────────────────────────────
# PHASE N — consolidated report
# ─────────────────────────────────────

def phase_N_report():
    t0 = time.time()
    log.info("=== PHASE N — consolidated honest-findings report ===")
    md = [
        f"# Deep research v4 — the honesty round ({DATE})",
        "",
        f"Total runtime: **{(time.time() - REPORT['started_at']) / 60:.1f} min**",
        "",
        "v2 + v3 established ExtraTrees as the winning model with a 71.3% "
        "hit rate at +100% returns. But v3's permutation importance revealed "
        "that the model's edge is driven almost entirely by `log_price` "
        "(importance +0.505, all others <0.06). This raised a critical "
        "question: *is this real alpha, or just the small-cap premium?* v4 "
        "investigates.",
        "",
    ]

    pI = REPORT["phases"].get("I_price_controlled", {})
    if pI.get("quintiles"):
        md += ["## Phase I — price-controlled study", "",
               "Train a model per log_price quintile. Lift should stay >1.5x if the model adds value beyond small-cap.",
               "",
               f"**Median within-quintile lift: {pI.get('median_within_quintile_lift', 0):.2f}x**",
               "",
               "| quintile | rows | baseline | model hit rate | lift | mean return |",
               "|---|---|---|---|---|---|"]
        for k, v in sorted(pI["quintiles"].items()):
            md.append(f"| {k} | {v['n_rows']} | {v['within_bucket_baseline']:.3f} | {v['model_hit_rate']:.3f} | {v['model_lift']:.2f}x | {v['mean_return']*100:+.1f}% |")
        md.append("")
        md.append(f"_{pI.get('interpretation')}_")
        md.append("")

    pJ = REPORT["phases"].get("J_excess_return", {})
    if pJ.get("excess_labeled"):
        md += ["## Phase J — excess-return labeling", "",
               f"- Excess-labeled model (strips universe median per window): absolute hit@+100% = **{pJ['excess_labeled']['hit_rate_at_100_abs']:.3f}**",
               f"- Absolute-labeled control: hit@+100% = **{pJ['absolute_labeled_control']['hit_rate_at_100']:.3f}**",
               "",
               f"_{pJ.get('interpretation')}_",
               ""]

    pK = REPORT["phases"].get("K_size_neutral", {})
    if pK.get("size_neutral"):
        md += ["## Phase K — size-neutral ensemble", "",
               "| method | hit@+100% | mean return |",
               "|---|---|---|"]
        if pK.get("unconstrained", {}).get("n"):
            md.append(f"| unconstrained top-20 | {pK['unconstrained']['hit_rate_100']:.3f} | {pK['unconstrained']['mean_return']*100:+.1f}% |")
        if pK.get("size_neutral", {}).get("n"):
            md.append(f"| size-neutral (4/quintile) | {pK['size_neutral']['hit_rate_100']:.3f} | {pK['size_neutral']['mean_return']*100:+.1f}% |")
        md.append("")
        comp = pK.get("unconstrained_quintile_composition", {})
        if comp:
            md.append("Unconstrained pick composition across price quintiles:")
            md.append("")
            md.append("| quintile | picks |")
            md.append("|---|---|")
            for q, n in sorted(comp.items()):
                md.append(f"| {q} | {n} |")
            md.append("")
        md.append(f"_{pK.get('interpretation')}_")
        md.append("")

    pL = REPORT["phases"].get("L_year_oos", {})
    if pL.get("status") == "completed":
        md += ["## Phase L — year-out-of-sample (train ≤2023, test 2024)", "",
               f"- Train rows: {pL['train_rows']}, test rows: {pL['test_rows']} across {pL['test_windows']} windows",
               f"- 2024 hit rate: **{pL['hit_rate_100']:.3f}**",
               f"- Baseline (no selection): **{pL['baseline_rate']:.3f}**",
               f"- Lift: **{pL['lift']:.2f}x**",
               f"- Mean return: **{pL['mean_return']*100:+.1f}%**",
               "",
               f"_{pL.get('interpretation')}_",
               ""]

    pM = REPORT["phases"].get("M_interactions", {})
    if pM.get("base"):
        md += ["## Phase M — explicit feature interactions", "",
               f"- Base ({pM['base']['n_features']} features): rate {pM['base']['hit_rate']:.3f}, mean {pM['base']['mean_return']*100:+.1f}%",
               f"- With interactions ({pM['with_interactions']['n_features']} features): rate {pM['with_interactions']['hit_rate']:.3f}, mean {pM['with_interactions']['mean_return']*100:+.1f}%",
               "",
               f"_{pM.get('interpretation')}_",
               ""]

    md += ["## Honest bottom line",
           "",
           "The model's headline 71% hit rate at +100% is impressive, but:",
           "",
           "1. `log_price` dominates feature importance by a factor of 10×. The model is largely picking small-cap stocks.",
           "2. Phase I will tell us whether the model adds value *within* price quintiles — that's the test of real alpha.",
           "3. Phase L is the cleanest OOS test — 2024 never seen during model choice. If it holds up there, the claim is honest.",
           "4. Before live trading: fix split-adjustment bugs (Phase 0 of Alpaca plan), survivorship bias, and add transaction cost drag.",
           "",
           f"Report generated: {datetime.now().isoformat()}",
           ]
    REPORT_MD.write_text("\n".join(md))
    REPORT["completed_at"] = time.time()
    REPORT["total_runtime_min"] = round(
        (REPORT["completed_at"] - REPORT["started_at"]) / 60, 1)
    _save_partial()
    log.info("PHASE N done; report → %s (%.1f min total)",
             REPORT_MD, REPORT["total_runtime_min"])


def run():
    try:
        feat = _load()
        log.info("Loaded %d rows, %d unique windows",
                 len(feat), feat["as_of"].nunique())
        phase_I_price_controlled(feat.copy())
        phase_J_excess_return(feat.copy())
        phase_K_size_neutral(feat.copy())
        phase_L_year_oos(feat.copy())
        phase_M_interactions(feat.copy())
        phase_N_report()
        log.info("DEEP RESEARCH V4 COMPLETE — %s min",
                 REPORT.get("total_runtime_min"))
        return 0
    except Exception as e:
        log.exception("v4 crashed: %s", e)
        REPORT["crashed_at"] = time.time()
        REPORT["crash_traceback"] = traceback.format_exc()
        _save_partial()
        return 1


if __name__ == "__main__":
    sys.exit(run())
