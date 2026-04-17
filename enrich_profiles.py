"""
FMP-backed company-profile enrichment for /picks.

One JSON file per ticker under data_cache/profiles/{TICKER}.json. Each file
holds {name, website, sector, industry, ipo_date, country, image} — whatever
FMP returns, normalized to these fields.

SEC's company_tickers.json covers name-only. For websites + proper sector
labels we need FMP's /profile. Caching aggressively avoids burning the
250-req/day FMP limit on re-runs.

Usage:
    python3 enrich_profiles.py                      # full universe
    python3 enrich_profiles.py --symbols AAPL MSFT  # specific
    python3 enrich_profiles.py --limit 100          # cap
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("enrich_profiles")

FMP_API_KEY = os.getenv("FMP_API_KEY", "")
BASE = "https://financialmodelingprep.com/stable"

CACHE_DIR = Path(__file__).parent / "data_cache" / "profiles"
CACHE_TTL_SECS = 30 * 24 * 3600  # 30 days — profiles rarely change


def _profile_path(sym: str) -> Path:
    return CACHE_DIR / f"{sym.upper()}.json"


def load_cached(sym: str) -> dict | None:
    """Return cached profile or None if missing / expired."""
    path = _profile_path(sym)
    if not path.exists():
        return None
    try:
        age = time.time() - path.stat().st_mtime
        if age > CACHE_TTL_SECS:
            return None
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _normalize(rec: dict, sym: str) -> dict:
    return {
        "symbol":   sym.upper(),
        "name":     rec.get("companyName") or rec.get("name"),
        "website":  rec.get("website"),
        "sector":   rec.get("sector"),
        "industry": rec.get("industry"),
        "country":  rec.get("country"),
        "ipo_date": rec.get("ipoDate"),
        "image":    rec.get("image"),
        "source":   "fmp",
        "fetched_at": time.time(),
    }


def fetch_one(sym: str, session: requests.Session) -> dict | None:
    """Fetch + cache a single ticker's profile. Returns normalized dict."""
    cached = load_cached(sym)
    if cached:
        return cached
    if not FMP_API_KEY:
        return None
    url = f"{BASE}/profile"
    try:
        r = session.get(url, params={"symbol": sym, "apikey": FMP_API_KEY},
                        timeout=12)
        if r.status_code != 200:
            return None
        data = r.json()
        if not data:
            return None
        rec = data[0] if isinstance(data, list) else data
        profile = _normalize(rec, sym)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _profile_path(sym).write_text(json.dumps(profile, separators=(",", ":")))
        return profile
    except (requests.RequestException, json.JSONDecodeError, KeyError) as e:
        log.debug("profile fetch %s: %s", sym, e)
        return None


def enrich_many(symbols: list[str], workers: int = 4) -> dict[str, dict]:
    """Fetch profiles for many tickers. Returns {symbol: profile}."""
    out: dict[str, dict] = {}
    session = requests.Session()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch_one, s, session): s for s in symbols}
        done = 0
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                profile = fut.result()
            except Exception as e:
                log.debug("%s fetch failed: %s", sym, e)
                profile = None
            if profile:
                out[sym] = profile
            done += 1
            if done % 50 == 0 or done == len(symbols):
                log.info("[%d/%d] enriched=%d", done, len(symbols), len(out))
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="+")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args()

    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
    else:
        # Pick up whatever the scanner is going to surface
        import sqlite3
        conn = sqlite3.connect(str(Path(__file__).parent / "projector_cache.db"))
        symbols = [r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM projections").fetchall()]
        conn.close()
    if args.limit:
        symbols = symbols[: args.limit]

    log.info("Enriching %d symbols (workers=%d)", len(symbols), args.workers)
    result = enrich_many(symbols, workers=args.workers)
    log.info("Done: %d profiles cached", len(result))


if __name__ == "__main__":
    sys.exit(main() or 0)
