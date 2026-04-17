"""Short-interest signal fetched from yfinance — pragmatic alternative
to FINRA whose public API is stuck on 2020 data.

yfinance's `Ticker.info` carries four short-interest fields per symbol,
sourced from the most recent FINRA reporting cycle (bi-monthly):
    sharesShort             raw shares shorted (most recent snapshot)
    sharesShortPriorMonth   raw shares shorted prior reporting cycle
    shortRatio              days-to-cover (float / avg daily volume)
    shortPercentOfFloat     normalised (0..1)
    dateShortInterest       UTC epoch of the snapshot
    floatShares             denominator

This gives us, from one call per ticker:
  - absolute short intensity         (shortPercentOfFloat)
  - direction                          (sharesShort vs sharesShortPriorMonth)
  - squeeze risk                       (shortRatio / days-to-cover)

Enough to test as NN features without waiting for 30 days of polling.
The values refresh every 2-4 weeks as FINRA publishes, but the first
fetch already carries the prior-month comparison.

Polite to Yahoo: we sleep between calls, surface-level pull only.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

log = logging.getLogger("si-yf")

DB_PATH = Path(__file__).parent / "projector_cache.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS short_interest_yf (
    symbol                    TEXT NOT NULL,
    fetched_at                TEXT NOT NULL,
    short_snapshot_date       TEXT,
    shares_short              INTEGER,
    shares_short_prior_month  INTEGER,
    short_ratio               REAL,
    short_pct_of_float        REAL,
    float_shares              INTEGER,
    PRIMARY KEY (symbol, short_snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_si_yf_symbol ON short_interest_yf(symbol);
CREATE INDEX IF NOT EXISTS idx_si_yf_snap   ON short_interest_yf(short_snapshot_date);
"""


def init_table(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _epoch_to_iso(ts) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
    except Exception:
        return None


def fetch_symbol(symbol: str) -> dict | None:
    """Pull short-interest fields from yfinance for one symbol.
    Returns None if yfinance has nothing useful."""
    import yfinance as yf
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception as e:
        log.warning("%s: yfinance error: %s", symbol, e)
        return None
    shares = info.get("sharesShort")
    pct = info.get("shortPercentOfFloat")
    if shares is None and pct is None:
        return None
    return {
        "symbol": symbol.upper(),
        "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
        "short_snapshot_date": _epoch_to_iso(info.get("dateShortInterest")),
        "shares_short": int(shares) if shares is not None else None,
        "shares_short_prior_month": (
            int(info["sharesShortPriorMonth"])
            if info.get("sharesShortPriorMonth") is not None else None
        ),
        "short_ratio": float(info["shortRatio"]) if info.get("shortRatio") is not None else None,
        "short_pct_of_float": float(info["shortPercentOfFloat"])
            if info.get("shortPercentOfFloat") is not None else None,
        "float_shares": int(info["floatShares"]) if info.get("floatShares") is not None else None,
    }


def save_row(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO short_interest_yf
           (symbol, fetched_at, short_snapshot_date, shares_short,
            shares_short_prior_month, short_ratio, short_pct_of_float,
            float_shares)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            row["symbol"], row["fetched_at"], row["short_snapshot_date"],
            row["shares_short"], row["shares_short_prior_month"],
            row["short_ratio"], row["short_pct_of_float"],
            row["float_shares"],
        ),
    )


def refresh_universe(symbols: Iterable[str], sleep_s: float = 0.4) -> dict:
    """Fetch short interest for every symbol. Returns summary dict."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    conn = sqlite3.connect(str(DB_PATH))
    init_table(conn)
    got = missing = errors = 0
    syms = list(symbols)
    for i, sym in enumerate(syms):
        try:
            row = fetch_symbol(sym)
            if row is None:
                missing += 1
            else:
                save_row(conn, row)
                got += 1
        except Exception as e:
            log.warning("%s: %s", sym, e)
            errors += 1
        if (i + 1) % 50 == 0:
            conn.commit()
            log.info("progress: %d/%d  got=%d miss=%d err=%d",
                     i + 1, len(syms), got, missing, errors)
        time.sleep(sleep_s)
    conn.commit()
    summary = {"total": len(syms), "got": got, "missing": missing, "errors": errors}
    log.info("refresh done: %s", summary)
    return summary


def _main_cli() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Refresh yfinance short interest")
    parser.add_argument("--universe", choices=["preferred", "full", "custom"],
                        default="preferred",
                        help="preferred = SP500+NDX100+WSB; full = Russell 3000; custom needs --symbols")
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument("--sleep", type=float, default=0.4,
                        help="seconds between yfinance calls (politeness)")
    args = parser.parse_args()

    if args.symbols:
        syms = [s.upper() for s in args.symbols]
    else:
        import worker
        if args.universe == "preferred":
            syms = sorted(set(worker.SP500_NDX100 + worker.WSB_UNIVERSE))
        elif args.universe == "full":
            syms = worker.FULL_UNIVERSE
        else:
            raise SystemExit("--symbols required with --universe custom")

    refresh_universe(syms, sleep_s=args.sleep)


if __name__ == "__main__":
    _main_cli()
