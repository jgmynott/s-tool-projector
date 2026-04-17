"""
yfinance-backed market cap + avg volume enrichment.

FMP's fundamentals endpoint has a ~250 req/day quota which makes it unfit
for a 2,600-ticker universe. yfinance is rate-limited but not quota'd, and
`.fast_info` returns market_cap + last_volume in one call per ticker.

We cache a single JSON file: data_cache/market_caps.json
    { "AAPL": {"market_cap": 3.8e12, "avg_volume": 5e7, "fetched_at": ... }, ... }

portfolio_scanner reads this at scan time and attaches to each pick.
Run nightly before the scan; stale entries auto-refresh after TTL_DAYS.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("enrich_marketcaps")

CACHE_PATH = Path(__file__).parent / "data_cache" / "market_caps.json"
TTL_SECS = 2 * 24 * 3600  # 48h — market cap moves with price but slow enough


def load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, separators=(",", ":")))


def fetch_one(symbol: str) -> tuple[str, dict | None]:
    """Hit yfinance for market cap + avg volume. Returns (symbol, dict or None)."""
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        # .fast_info is lighter than .info — fewer network calls
        info = t.fast_info
        mc = info.get("market_cap") if hasattr(info, "get") else None
        if mc is None:
            mc = getattr(info, "market_cap", None)
        vol = info.get("ten_day_average_volume") if hasattr(info, "get") else None
        if vol is None:
            vol = getattr(info, "ten_day_average_volume", None)
        if not mc and not vol:
            return (symbol, None)
        return (symbol, {
            "market_cap": float(mc) if mc else None,
            "avg_volume": float(vol) if vol else None,
            "fetched_at": time.time(),
        })
    except Exception as e:
        log.debug("yfinance failed for %s: %s", symbol, e)
        return (symbol, None)


def enrich(symbols: list[str], workers: int = 4, force: bool = False) -> dict:
    cache = load_cache()
    now = time.time()
    to_fetch = []
    for s in symbols:
        s = s.upper()
        existing = cache.get(s)
        if not force and existing and (now - existing.get("fetched_at", 0) < TTL_SECS):
            continue
        to_fetch.append(s)
    if not to_fetch:
        log.info("All %d symbols already fresh in cache", len(symbols))
        return cache
    log.info("Enriching %d of %d (others fresh). workers=%d", len(to_fetch), len(symbols), workers)

    ok = fail = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch_one, s): s for s in to_fetch}
        done = 0
        for fut in as_completed(futures):
            sym, data = fut.result()
            done += 1
            if data:
                cache[sym] = data
                ok += 1
            else:
                fail += 1
            if done % 100 == 0 or done == len(to_fetch):
                log.info("[%d/%d] ok=%d fail=%d", done, len(to_fetch), ok, fail)
                # Persist periodically so a crash doesn't lose progress.
                save_cache(cache)
    save_cache(cache)
    log.info("Done: ok=%d fail=%d total_cache=%d", ok, fail, len(cache))
    return cache


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="+")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--force", action="store_true")
    args = p.parse_args()

    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
    else:
        import sqlite3
        conn = sqlite3.connect(str(Path(__file__).parent / "projector_cache.db"))
        symbols = [r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM projections").fetchall()]
        conn.close()
    if args.limit:
        symbols = symbols[: args.limit]
    enrich(symbols, workers=args.workers, force=args.force)


if __name__ == "__main__":
    sys.exit(main() or 0)
