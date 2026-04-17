"""
Parallel Russell 3000 backfill — fast-path projector refresh for /picks.

Why this exists: worker.py runs projections sequentially with optional
public-pulse snapshots bolted on. For a cold-cache full-universe refresh,
that's ~8+ hours per run. This script drops sentiment fetches and runs
8 workers in parallel — typically 10-20 min end-to-end for the full
Russell 3000 on a warm machine.

Not a replacement for worker.py (which also does short-interest, EDGAR,
and scan steps). Use this when you need a fast cache warm-up and plan to
run worker.py afterwards for the full signal refresh.

Usage:
    python backfill_parallel.py                     # full universe
    python backfill_parallel.py --workers 16        # more concurrency
    python backfill_parallel.py --limit 200         # first N symbols only
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from db import init_db, get_projection_age_hours, save_projection
from projector_engine import run_projection

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("backfill")

HORIZON = 252
STALE_HOURS = 18


def _compute_one(symbol: str, force: bool) -> tuple[str, str, float | None]:
    """Run a single projection. Returns (symbol, status, elapsed)."""
    t0 = time.time()
    try:
        conn = init_db()
        if not force:
            age = get_projection_age_hours(conn, symbol, HORIZON)
            if age is not None and age < STALE_HOURS:
                return (symbol, "skip", time.time() - t0)
        result = run_projection(symbol, horizon_days=HORIZON)
        save_projection(conn, result)
        conn.close()
        return (symbol, "ok", time.time() - t0)
    except Exception as e:
        return (symbol, f"fail:{type(e).__name__}", time.time() - t0)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--force", action="store_true", help="ignore STALE_HOURS cache")
    p.add_argument("--symbols", nargs="+")
    args = p.parse_args()

    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
    else:
        from universe import load_full_universe
        symbols = load_full_universe()
    if args.limit:
        symbols = symbols[: args.limit]

    log.info("Backfilling %d symbols with %d workers", len(symbols), args.workers)

    ok = skip = fail = 0
    t_start = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_compute_one, s, args.force): s for s in symbols}
        done = 0
        for fut in as_completed(futures):
            sym, status, elapsed = fut.result()
            done += 1
            if status == "ok":
                ok += 1
            elif status == "skip":
                skip += 1
            else:
                fail += 1
            if done % 50 == 0 or done == len(symbols):
                total_elapsed = time.time() - t_start
                rate = done / total_elapsed if total_elapsed > 0 else 0
                eta = (len(symbols) - done) / rate if rate > 0 else 0
                log.info("[%d/%d] ok=%d skip=%d fail=%d · %.1f/s · eta %.1fmin",
                         done, len(symbols), ok, skip, fail, rate, eta / 60)

    log.info("Done: ok=%d skip=%d fail=%d in %.1fmin",
             ok, skip, fail, (time.time() - t_start) / 60)


if __name__ == "__main__":
    sys.exit(main() or 0)
