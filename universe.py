"""
universe.py
───────────
Resolves the tradeable universe of US equities. Combines:

  • iShares IWV (Russell 3000) — ~2,580 tickers, refreshed daily by BlackRock
  • SP500_NDX100 + WSB_UNIVERSE from worker.py (for overlap/continuity)

Caches the iShares CSV to `data_cache/iwv_holdings.csv`. Re-fetches if older
than 24h. Normalizes class-share tickers (BRKB → BRK-B) to match yfinance/FMP.

Usage:
    from universe import RUSSELL_3000, FULL_UNIVERSE
    python3 universe.py refresh   # force re-fetch
    python3 universe.py list      # print counts + sample
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
from pathlib import Path
from typing import List, Set

import requests

logger = logging.getLogger("universe")

CACHE_DIR = Path("data_cache")
CACHE_DIR.mkdir(exist_ok=True)
IWV_CSV = CACHE_DIR / "iwv_holdings.csv"
STALE_HOURS = 24

IWV_URL = (
    "https://www.ishares.com/us/products/239714/ishares-russell-3000-etf/"
    "1467271812596.ajax?fileType=csv&fileName=IWV_holdings&dataType=fund"
)

# iShares uses concatenated class-share tickers; most data APIs use dashes.
CLASS_SHARE_NORMALIZE = {
    "BRKB": "BRK-B",
    "BFB": "BF-B",
    "BFA": "BF-A",
    "HEIA": "HEI-A",
    "LGFA": "LGF-A",
    "LGFB": "LGF-B",
    "MOGA": "MOG-A",
    "LENB": "LEN-B",
    "GEFB": "GEF-B",
    "CWENA": "CWEN-A",
    "RUSHA": "RUSHA",  # leave as-is; actually uses no dash
    "RUSHB": "RUSHB",
}

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9\-]*$")


def _fetch_iwv() -> None:
    logger.info("Fetching IWV holdings from iShares...")
    r = requests.get(IWV_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    r.raise_for_status()
    IWV_CSV.write_bytes(r.content)
    logger.info("Saved → %s (%d bytes)", IWV_CSV, len(r.content))


def _is_stale() -> bool:
    if not IWV_CSV.exists():
        return True
    import time
    age_hr = (time.time() - IWV_CSV.stat().st_mtime) / 3600
    return age_hr > STALE_HOURS


def _parse_iwv() -> List[str]:
    """Parse cached IWV CSV → list of normalized tickers."""
    tickers: Set[str] = set()
    with IWV_CSV.open("r") as f:
        reader = csv.reader(f)
        header_found = False
        for row in reader:
            if not row:
                continue
            if not header_found:
                if row and row[0].strip() == "Ticker":
                    header_found = True
                continue
            if len(row) < 4:
                continue
            raw_ticker = row[0].strip()
            asset_class = row[3].strip() if len(row) > 3 else ""
            if asset_class != "Equity":
                continue
            if not raw_ticker or raw_ticker == "-":
                continue
            norm = CLASS_SHARE_NORMALIZE.get(raw_ticker, raw_ticker)
            if TICKER_RE.match(norm):
                tickers.add(norm)
    return sorted(tickers)


def load_russell_3000(force_refresh: bool = False) -> List[str]:
    if force_refresh or _is_stale():
        _fetch_iwv()
    return _parse_iwv()


def load_full_universe(force_refresh: bool = False) -> List[str]:
    """Russell 3000 ∪ SP500_NDX100 ∪ WSB_UNIVERSE, deduped & sorted."""
    from worker import SP500_NDX100, WSB_UNIVERSE
    r3k = load_russell_3000(force_refresh=force_refresh)
    combined = sorted(set(r3k) | set(SP500_NDX100) | set(WSB_UNIVERSE))
    return combined


# Lazily-populated module-level constants (callers import these)
RUSSELL_3000: List[str] = []
FULL_UNIVERSE: List[str] = []


def _lazy_init() -> None:
    global RUSSELL_3000, FULL_UNIVERSE
    if not RUSSELL_3000:
        RUSSELL_3000 = load_russell_3000()
    if not FULL_UNIVERSE:
        FULL_UNIVERSE = load_full_universe()


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("refresh", help="Force re-fetch of IWV holdings")
    sub.add_parser("list", help="Print universe sizes and samples")
    args = p.parse_args()

    if args.cmd == "refresh":
        r3k = load_russell_3000(force_refresh=True)
        full = load_full_universe()
        print(f"Russell 3000: {len(r3k)} tickers")
        print(f"Full universe (R3K ∪ SP500+NDX100 ∪ WSB): {len(full)}")

    elif args.cmd == "list":
        r3k = load_russell_3000()
        full = load_full_universe()
        print(f"Russell 3000 (cached): {len(r3k)} tickers")
        print(f"  first 10: {r3k[:10]}")
        print(f"  last 10 : {r3k[-10:]}")
        print(f"Full universe: {len(full)} tickers")
        # Diff against existing
        from worker import FULL_UNIVERSE as OLD
        new_only = sorted(set(full) - set(OLD))
        removed = sorted(set(OLD) - set(full))
        print(f"  added vs old universe: {len(new_only)}")
        print(f"  dropped vs old universe: {len(removed)} {removed[:10]}")


if __name__ == "__main__":
    main()
