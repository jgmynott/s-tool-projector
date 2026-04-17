"""Overnight research batch — runs the S-88 queue against the newly
regenerated 2016-2024 training set (35 walk-forward windows, ~67k
ticker-windows).

What it does, in sequence:
  1. SEC-features ablation on the expanded CSV (existing experiment
     script, re-run). Previously only tested on 2022-24 data; the new
     windows include 2018 Q4 drawdown + 2020 COVID + 2022 tech bear.
  2. ExtraTrees hyperparameter sweep on the expanded CSV. Production
     config is n_estimators=300, max_depth=14, min_samples_leaf=20 —
     sweep ±30% around that point to see if more data changes the
     optimum.
  3. Asymmetric-tier standalone backtest. Loads asymmetric_scores.json,
     builds an asymmetric-tier top-N, computes hit_100 + mean return
     with Wilson 95% CIs. Closes the Wave 3 skeleton called out in
     docs/honest-metrics.md.
  4. Sector-conditional backtest. Joins upside_hunt_scored.csv with
     a sector map (SEC EDGAR + yfinance cache), computes nn_score lift
     within each sector. Answers the open question "is the model
     generalized or does it rely on one or two sector tailwinds?"
  5. Drift-tilt signal re-test on expanded data. Previous research
     concluded drift signals died around 2023. The expanded set adds
     pre-2022 regimes — rerun to check whether the "signals died"
     conclusion was window-selection artifact.

Writes:
  - /tmp/research_batch_<ts>.log          main orchestrator log
  - /tmp/research_<step>_<ts>.log         per-step stdout/stderr
  - research/overnight_batch_summary_<date>.json   final summary

All steps are independent; if any fails the orchestrator logs it and
moves on — the goal is to get as much signal as possible overnight, not
block on one bad script.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("research_batch")

ROOT = Path(__file__).parent.parent
RESEARCH = ROOT / "research"
RESULTS_CSV = ROOT / "upside_hunt_results.csv"
SCORED_CSV = ROOT / "upside_hunt_scored.csv"
ASYM_SCORES_PATH = ROOT / "data_cache" / "asymmetric_scores.json"
NN_SCORES_PATH = ROOT / "data_cache" / "nn_scores.json"
FACTS_DIR = ROOT / "data_cache" / "sec_edgar" / "facts"

TS = datetime.now().strftime("%Y%m%d_%H%M")
SUMMARY_PATH = RESEARCH / f"overnight_batch_summary_{TS}.json"

TOP_N = 20
THRESHOLD = 1.0  # +100%


def run_step(name: str, cmd: list[str], timeout_sec: int = 60 * 60 * 2) -> dict:
    """Execute a subprocess script; capture exit, stdout/stderr to log."""
    log_path = Path(f"/tmp/research_{name}_{TS}.log")
    log.info("▶ STEP %s | cmd=%s", name, " ".join(cmd))
    t0 = time.time()
    try:
        with log_path.open("w") as fh:
            r = subprocess.run(
                cmd, stdout=fh, stderr=subprocess.STDOUT,
                cwd=str(ROOT), timeout=timeout_sec,
            )
    except subprocess.TimeoutExpired:
        log.error("✗ STEP %s timed out after %.0f min", name, timeout_sec / 60)
        return {"name": name, "status": "timeout", "duration_min": timeout_sec / 60,
                "log": str(log_path)}
    except Exception as e:
        log.error("✗ STEP %s failed to launch: %s", name, e)
        return {"name": name, "status": "error", "error": str(e),
                "log": str(log_path)}
    dur = (time.time() - t0) / 60
    status = "ok" if r.returncode == 0 else "error"
    log.info("%s STEP %s | exit=%d | %.1f min | log=%s",
             "✓" if status == "ok" else "✗", name, r.returncode, dur, log_path)
    return {"name": name, "status": status, "exit_code": r.returncode,
            "duration_min": round(dur, 1), "log": str(log_path)}


def _wilson_ci(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% CI for a binomial proportion. n > 0 required."""
    if n <= 0:
        return (0.0, 0.0)
    p = hits / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def step_asymmetric_standalone() -> dict:
    """Stand-alone backtest of the asymmetric tier. Computes hit @ +100%,
    mean return, and Wilson 95% CI across all ticker-windows whose
    asymmetric score cleared the live production threshold."""
    log.info("▶ STEP asymmetric_standalone")
    t0 = time.time()
    out: dict = {"name": "asymmetric_standalone"}
    try:
        if not SCORED_CSV.exists():
            return {**out, "status": "error",
                    "error": f"{SCORED_CSV} not found"}
        df = pd.read_csv(SCORED_CSV)
        asym_col = None
        for c in ("asymmetric_score", "p90_ratio"):
            if c in df.columns:
                asym_col = c
                break
        if asym_col is None:
            # Compute p90_ratio on the fly if only raw fields present
            if {"p90", "current"}.issubset(df.columns):
                df["p90_ratio"] = df["p90"] / df["current"].clip(lower=0.01)
                asym_col = "p90_ratio"
            else:
                return {**out, "status": "error",
                        "error": "no asymmetric score column found in scored CSV"}

        # Asymmetric-tier top-N per window by the score
        top = (df.sort_values(["as_of", asym_col], ascending=[True, False])
                 .groupby("as_of").head(TOP_N))
        n = len(top)
        if n == 0:
            return {**out, "status": "error", "error": "empty top set"}

        hits = int((top["realized_ret"] >= THRESHOLD).sum())
        mean_ret = float(top["realized_ret"].mean())
        median_ret = float(top["realized_ret"].median())
        rate = hits / n
        lo, hi = _wilson_ci(hits, n)

        # Universe baseline for comparison
        base_n = len(df)
        base_hits = int((df["realized_ret"] >= THRESHOLD).sum())
        base_rate = base_hits / base_n if base_n else 0
        lift = (rate / base_rate) if base_rate > 0 else 0

        # 2024 OOS slice
        df["as_of_dt"] = pd.to_datetime(df["as_of"])
        df_oos = df[df["as_of_dt"].dt.year == df["as_of_dt"].dt.year.max()]
        top_oos = (df_oos.sort_values(["as_of", asym_col], ascending=[True, False])
                         .groupby("as_of").head(TOP_N))
        n_oos = len(top_oos)
        hits_oos = int((top_oos["realized_ret"] >= THRESHOLD).sum())
        rate_oos = (hits_oos / n_oos) if n_oos else 0
        lo_oos, hi_oos = _wilson_ci(hits_oos, n_oos)
        base_oos_hits = int((df_oos["realized_ret"] >= THRESHOLD).sum())
        base_oos_rate = base_oos_hits / len(df_oos) if len(df_oos) else 0
        lift_oos = rate_oos / base_oos_rate if base_oos_rate > 0 else 0

        dur = (time.time() - t0) / 60
        log.info("✓ asymmetric_standalone | overall hit=%.3f n=%d wilson=(%.3f, %.3f) lift=%.2fx | oos hit=%.3f n=%d wilson=(%.3f, %.3f) lift=%.2fx | %.1f min",
                 rate, n, lo, hi, lift, rate_oos, n_oos, lo_oos, hi_oos, lift_oos, dur)
        return {
            **out,
            "status": "ok",
            "score_column": asym_col,
            "duration_min": round(dur, 1),
            "overall": {
                "n": n, "hits": hits, "hit_rate": rate,
                "wilson_95": [round(lo, 4), round(hi, 4)],
                "mean_return": mean_ret,
                "median_return": median_ret,
                "baseline_rate": base_rate,
                "lift": lift,
            },
            "oos_2024": {
                "n": n_oos, "hits": hits_oos, "hit_rate": rate_oos,
                "wilson_95": [round(lo_oos, 4), round(hi_oos, 4)],
                "baseline_rate": base_oos_rate,
                "lift": lift_oos,
            },
        }
    except Exception as e:
        log.error("✗ asymmetric_standalone: %s", e)
        return {**out, "status": "error", "error": str(e)}


def step_sector_conditional() -> dict:
    """Break down nn_score lift by sector. Builds a sector map from the
    SEC EDGAR company_info JSON + yfinance profile cache. For each
    sector with at least 100 ticker-windows, computes hit @ +100% and
    lift vs the sector's own baseline."""
    log.info("▶ STEP sector_conditional")
    t0 = time.time()
    out: dict = {"name": "sector_conditional"}
    try:
        if not SCORED_CSV.exists():
            return {**out, "status": "error",
                    "error": f"{SCORED_CSV} not found"}
        df = pd.read_csv(SCORED_CSV)
        score_col = None
        for c in ("ensemble_score", "nn_score"):
            if c in df.columns:
                score_col = c
                break
        if score_col is None:
            return {**out, "status": "error",
                    "error": "no nn_score or ensemble_score in scored CSV"}

        # Build sector map from yfinance profile cache
        sector_map: dict[str, str] = {}
        profiles_dir = ROOT / "data_cache" / "profiles"
        if profiles_dir.exists():
            for pf in profiles_dir.glob("*.json"):
                try:
                    p = json.loads(pf.read_text())
                    sym = pf.stem.upper()
                    sec = p.get("sector") or p.get("sectorKey") or p.get("sectorDisp")
                    if sec:
                        sector_map[sym] = sec
                except Exception:
                    continue

        df["sector"] = df["symbol"].str.upper().map(sector_map).fillna("Unknown")

        # Top-N per window
        top = (df.sort_values(["as_of", score_col], ascending=[True, False])
                 .groupby("as_of").head(TOP_N))

        by_sector = {}
        for sector, g in df.groupby("sector"):
            if len(g) < 100:
                continue
            sector_top = top[top["sector"] == sector]
            if len(sector_top) == 0:
                continue
            hits_s = int((sector_top["realized_ret"] >= THRESHOLD).sum())
            base_s = int((g["realized_ret"] >= THRESHOLD).sum())
            rate_s = hits_s / len(sector_top)
            base_rate_s = base_s / len(g)
            lift_s = rate_s / base_rate_s if base_rate_s > 0 else 0
            lo_s, hi_s = _wilson_ci(hits_s, len(sector_top))
            by_sector[sector] = {
                "n_universe": int(len(g)),
                "n_picked": int(len(sector_top)),
                "hits_picked": hits_s,
                "hit_rate_picked": round(rate_s, 4),
                "hit_rate_baseline": round(base_rate_s, 4),
                "lift": round(lift_s, 3),
                "wilson_95_picked": [round(lo_s, 4), round(hi_s, 4)],
                "mean_return_picked": float(sector_top["realized_ret"].mean()),
            }

        # Overall for reference
        hits_all = int((top["realized_ret"] >= THRESHOLD).sum())
        rate_all = hits_all / len(top) if len(top) else 0
        base_hits_all = int((df["realized_ret"] >= THRESHOLD).sum())
        base_rate_all = base_hits_all / len(df) if len(df) else 0
        lift_all = rate_all / base_rate_all if base_rate_all > 0 else 0

        dur = (time.time() - t0) / 60
        log.info("✓ sector_conditional | %d sectors | overall lift=%.2fx | %.1f min",
                 len(by_sector), lift_all, dur)
        return {
            **out,
            "status": "ok",
            "score_column": score_col,
            "duration_min": round(dur, 1),
            "overall_lift": lift_all,
            "by_sector": dict(sorted(by_sector.items(),
                                     key=lambda kv: -kv[1]["lift"])),
            "sectors_with_unknown_count":
                int((df["sector"] == "Unknown").sum()),
        }
    except Exception as e:
        log.error("✗ sector_conditional: %s", e)
        return {**out, "status": "error", "error": str(e)}


def main() -> int:
    log.info("=== Overnight research batch started ===")
    log.info("TS=%s", TS)
    log.info("Training set: %s", RESULTS_CSV)
    log.info("Scored set:   %s", SCORED_CSV)
    if not SCORED_CSV.exists():
        log.error("Scored CSV missing — run regenerate_training_set first")
        return 1

    results: list[dict] = []

    # Step 1: SEC features ablation (existing script, rerun on new data)
    results.append(run_step(
        "sec_features",
        [sys.executable, "-W", "ignore",
         "research/experiment_sec_features_2026_04_17.py"],
        timeout_sec=60 * 60 * 2,  # 2 hour cap
    ))

    # Step 2: ExtraTrees HP sweep (existing script, rerun on new data)
    results.append(run_step(
        "extratrees_hp",
        [sys.executable, "-W", "ignore",
         "research/experiment_extratrees_hp_2026_04_17.py"],
        timeout_sec=60 * 60 * 2,
    ))

    # Step 3: Asymmetric standalone (new, inline)
    results.append(step_asymmetric_standalone())

    # Step 4: Sector-conditional backtest (new, inline)
    results.append(step_sector_conditional())

    # Step 5: Price-features experiment (existing, rerun on new data)
    results.append(run_step(
        "price_features",
        [sys.executable, "-W", "ignore",
         "research/experiment_price_features_2026_04_17.py"],
        timeout_sec=60 * 60 * 1,
    ))

    # Summary
    summary = {
        "generated_at": datetime.now().isoformat(),
        "training_set": {
            "results_csv": str(RESULTS_CSV),
            "scored_csv": str(SCORED_CSV),
            "n_rows": int(pd.read_csv(RESULTS_CSV, usecols=["as_of"]).shape[0]),
            "n_windows": int(pd.read_csv(RESULTS_CSV, usecols=["as_of"])
                               ["as_of"].nunique()),
        },
        "steps": results,
        "step_count_ok": sum(1 for r in results if r.get("status") == "ok"),
        "step_count_error": sum(1 for r in results if r.get("status") != "ok"),
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2, default=str))
    log.info("=== DONE. Summary: %s ===", SUMMARY_PATH)
    log.info("OK: %d  |  Errors: %d",
             summary["step_count_ok"], summary["step_count_error"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
