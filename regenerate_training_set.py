"""Regenerate the training set end-to-end after a price backfill.

Chained overnight sequence (runs in the background after
backfill_prices_historical.py completes):

  1. upside_hunt.py         → upside_hunt_results.csv with 35 quarterly
                               windows covering 2016-Q1 → 2024-Q3
  2. overnight_learn.py     → retrains NN (nn_score, ensemble_score,
                               moonshot) on the expanded dataset
  3. overnight_backtest.py  → backtest_report.json with new honest_metrics
  4. wave1_honest_audit.py  → wave1_honest_audit.json (liquidity + tx-cost
                               adjusted) against the expanded dataset

Each step logs to /tmp/regen_<step>.log so failures are debuggable
without losing context.

Pre-flight: checks that the price backfill has actually reached the
target date before launching, so we don't regenerate upside_hunt with
partial history.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("regen")

ROOT = Path(__file__).parent
PRICES_DIR = ROOT / "data_cache" / "prices"
TARGET_START = "2015-01-01"
# Acceptance threshold: at least this % of our tickers should have
# prices back to TARGET_START before we regenerate. Stragglers are
# individual tickers missing data from yfinance — waiting forever for
# 100% isn't realistic.
COVERAGE_THRESHOLD = 0.85
# How long to keep polling before giving up.
MAX_WAIT_MIN = 240


def coverage_pct(target: str) -> float:
    """Fraction of ticker CSVs whose earliest date is ≤ target."""
    csvs = list(PRICES_DIR.glob("*.csv"))
    if not csvs:
        return 0.0
    good = total = 0
    target_d = date.fromisoformat(target)
    for p in csvs:
        if p.name.startswith("_"):
            continue
        try:
            df = pd.read_csv(p, usecols=["Date"], nrows=2)
            if df.empty:
                continue
            first = pd.to_datetime(df["Date"].iloc[0]).date()
            total += 1
            if first <= target_d:
                good += 1
        except Exception:
            continue
    return good / max(total, 1)


def wait_for_backfill() -> bool:
    """Poll price cache until COVERAGE_THRESHOLD is met or we time out."""
    start = time.time()
    while (time.time() - start) / 60 < MAX_WAIT_MIN:
        cov = coverage_pct(TARGET_START)
        log.info("backfill coverage: %.1f%% of tickers have history back to %s",
                 cov * 100, TARGET_START)
        if cov >= COVERAGE_THRESHOLD:
            return True
        time.sleep(60)
    log.warning("backfill did not reach %.0f%% within %d min — aborting",
                COVERAGE_THRESHOLD * 100, MAX_WAIT_MIN)
    return False


def run_step(name: str, cmd: list[str], log_path: Path) -> bool:
    log.info("=== STEP: %s ===", name)
    log.info("cmd: %s", " ".join(cmd))
    t0 = time.time()
    with log_path.open("w") as fh:
        try:
            r = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT,
                               cwd=str(ROOT), timeout=60 * 60 * 4)
        except subprocess.TimeoutExpired:
            log.error("step %s timed out after 4 hours", name)
            return False
    elapsed = (time.time() - t0) / 60
    log.info("step %s finished in %.1f min (exit=%d, log=%s)",
             name, elapsed, r.returncode, log_path)
    return r.returncode == 0


def main() -> None:
    # 1. Wait for backfill
    if not wait_for_backfill():
        log.error("aborting — backfill incomplete")
        sys.exit(1)

    # 2. Regenerate upside_hunt with new window range
    if not run_step(
        "upside_hunt",
        [sys.executable, "upside_hunt.py"],
        Path("/tmp/regen_upside_hunt.log"),
    ):
        log.error("upside_hunt failed — check /tmp/regen_upside_hunt.log")
        sys.exit(2)

    # 3. Retrain NN against the expanded ground truth
    if not run_step(
        "overnight_learn",
        [sys.executable, "-W", "ignore", "overnight_learn.py"],
        Path("/tmp/regen_overnight_learn.log"),
    ):
        log.error("overnight_learn failed — check log")
        sys.exit(3)

    # 4. Rebuild backtest_report.json with new NN + more windows
    if not run_step(
        "overnight_backtest",
        [sys.executable, "overnight_backtest.py"],
        Path("/tmp/regen_overnight_backtest.log"),
    ):
        log.error("overnight_backtest failed — check log")
        sys.exit(4)

    # 5. Honest audit: apply survivorship + liquidity + tx-cost to the
    # new data, write runtime_data/wave1_honest_audit.json
    if not run_step(
        "honest_audit",
        [sys.executable, "research/wave1_honest_audit_2026_04_17.py"],
        Path("/tmp/regen_honest_audit.log"),
    ):
        log.error("honest_audit failed — check log")
        sys.exit(5)

    log.info("REGEN COMPLETE. Commit the changes with:")
    log.info("  git add upside_hunt_results.csv upside_hunt_scored.csv "
             "runtime_data/backtest_report.json "
             "runtime_data/wave1_honest_audit.json "
             "data_cache/*.json")
    log.info("  git commit -m 'data: regenerate training set 2016-2024'")


if __name__ == "__main__":
    main()
