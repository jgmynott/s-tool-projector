"""Finnhub earnings & recommendation signal — pulled nightly.

Three things per ticker per night:

  1. Days to next earnings event  (approx catalyst proximity)
  2. Last earnings-surprise %     (analyst-beat magnitude, lagged)
  3. Analyst recommendation trend (sum of buy/strongBuy delta in last 3 mo)

Finnhub free tier is 60 req/min, so ~10 min for a 500-ticker sweep.
Keyed by FINNHUB_API_KEY env var.

Schema keyed by (symbol, fetched_date) so repeated fetches upsert. The
nightly ablation script joins the most recent snapshot before as_of to
the upside_hunt ground-truth rows (no look-ahead).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone, date, timedelta
from pathlib import Path
from typing import Iterable

import requests

# In local dev, credentials live in .env; CI injects them directly.
# load_dotenv is a no-op when env already carries the value.
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(Path(__file__).parent / ".env")
except Exception:
    pass

log = logging.getLogger("earnings-finnhub")

DB_PATH = Path(__file__).parent / "projector_cache.db"
API_KEY = os.getenv("FINNHUB_API_KEY", "")
BASE = "https://finnhub.io/api/v1"
TIMEOUT = 15
USER_AGENT = "s-tool/1.0 (james@s-tool.io)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS earnings_finnhub (
    symbol                  TEXT NOT NULL,
    fetched_at              TEXT NOT NULL,
    fetched_date            TEXT NOT NULL,
    next_earnings_date      TEXT,
    days_to_next_earnings   INTEGER,
    last_earnings_date      TEXT,
    last_eps_actual         REAL,
    last_eps_estimate       REAL,
    last_surprise_pct       REAL,
    rec_strong_buy          INTEGER,
    rec_buy                 INTEGER,
    rec_hold                INTEGER,
    rec_sell                INTEGER,
    rec_strong_sell         INTEGER,
    rec_bullish_share       REAL,   -- (strongBuy+buy)/total, most recent month
    rec_bullish_share_90d   REAL,   -- same, average of last 3 months
    rec_bullish_delta_90d   REAL,   -- most recent - 3-month avg (trend)
    PRIMARY KEY (symbol, fetched_date)
);

CREATE INDEX IF NOT EXISTS idx_earn_fh_symbol ON earnings_finnhub(symbol);
CREATE INDEX IF NOT EXISTS idx_earn_fh_date   ON earnings_finnhub(fetched_date);
"""


def init_table(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _get(path: str, params: dict | None = None) -> dict | list | None:
    if not API_KEY:
        return None
    p = dict(params or {})
    p["token"] = API_KEY
    try:
        r = requests.get(f"{BASE}{path}", params=p,
                         headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        if r.status_code == 429:
            # rate-limited — step back
            time.sleep(2.0)
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.debug("finnhub %s failed: %s", path, e)
        return None


def _nearest_future_date(iso_list: list[str]) -> str | None:
    today = date.today()
    future = sorted(d for d in iso_list
                    if d and d >= today.isoformat())
    return future[0] if future else None


def fetch_symbol(symbol: str) -> dict | None:
    """Pull earnings calendar + recent surprise + recommendation trend.
    Returns None if Finnhub has nothing usable for this ticker."""
    sym = symbol.upper()

    # Next earnings — calendar/earnings with a 60-day window
    today = date.today()
    horizon = today + timedelta(days=60)
    cal = _get("/calendar/earnings", {"symbol": sym,
                                       "from": today.isoformat(),
                                       "to": horizon.isoformat()})
    cal_rows = (cal or {}).get("earningsCalendar") if isinstance(cal, dict) else None
    next_date = None
    if cal_rows:
        candidates = [r.get("date") for r in cal_rows if r.get("date")]
        next_date = _nearest_future_date(candidates)
    days_to_next = (date.fromisoformat(next_date) - today).days if next_date else None

    # Past earnings — /stock/earnings returns up to 4 quarters
    past = _get("/stock/earnings", {"symbol": sym})
    last_date = last_actual = last_est = last_surprise = None
    if isinstance(past, list) and past:
        by_date = sorted(past, key=lambda r: r.get("period", ""), reverse=True)
        for r in by_date:
            if r.get("period"):
                last_date = r["period"]
                last_actual = r.get("actual")
                last_est = r.get("estimate")
                if (last_actual is not None and last_est is not None
                        and last_est != 0):
                    last_surprise = (last_actual - last_est) / abs(last_est)
                break

    # Recommendation trends — monthly, returns list (most recent first)
    rec = _get("/stock/recommendation", {"symbol": sym})
    rec_latest = None
    rec_90d_share = None
    rec_bullish = None
    rec_delta = None
    if isinstance(rec, list) and rec:
        def bullish_share(r):
            tot = (r.get("strongBuy", 0) + r.get("buy", 0)
                   + r.get("hold", 0) + r.get("sell", 0)
                   + r.get("strongSell", 0))
            if tot == 0:
                return None
            return (r.get("strongBuy", 0) + r.get("buy", 0)) / tot
        rec_latest = rec[0]
        rec_bullish = bullish_share(rec_latest)
        last3 = rec[:3]
        shares = [bullish_share(r) for r in last3 if bullish_share(r) is not None]
        if shares:
            rec_90d_share = sum(shares) / len(shares)
        if rec_bullish is not None and rec_90d_share is not None:
            rec_delta = rec_bullish - rec_90d_share

    # If everything's None, skip.
    if (next_date is None and last_date is None and rec_latest is None):
        return None

    return {
        "symbol": sym,
        "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
        "fetched_date": today.isoformat(),
        "next_earnings_date": next_date,
        "days_to_next_earnings": days_to_next,
        "last_earnings_date": last_date,
        "last_eps_actual": last_actual,
        "last_eps_estimate": last_est,
        "last_surprise_pct": (round(last_surprise, 4) if last_surprise is not None
                              else None),
        "rec_strong_buy": rec_latest.get("strongBuy") if rec_latest else None,
        "rec_buy":         rec_latest.get("buy") if rec_latest else None,
        "rec_hold":        rec_latest.get("hold") if rec_latest else None,
        "rec_sell":        rec_latest.get("sell") if rec_latest else None,
        "rec_strong_sell": rec_latest.get("strongSell") if rec_latest else None,
        "rec_bullish_share": round(rec_bullish, 4) if rec_bullish is not None else None,
        "rec_bullish_share_90d": round(rec_90d_share, 4) if rec_90d_share is not None else None,
        "rec_bullish_delta_90d": round(rec_delta, 4) if rec_delta is not None else None,
    }


def save_row(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO earnings_finnhub
           (symbol, fetched_at, fetched_date,
            next_earnings_date, days_to_next_earnings,
            last_earnings_date, last_eps_actual, last_eps_estimate, last_surprise_pct,
            rec_strong_buy, rec_buy, rec_hold, rec_sell, rec_strong_sell,
            rec_bullish_share, rec_bullish_share_90d, rec_bullish_delta_90d)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            row["symbol"], row["fetched_at"], row["fetched_date"],
            row["next_earnings_date"], row["days_to_next_earnings"],
            row["last_earnings_date"], row["last_eps_actual"],
            row["last_eps_estimate"], row["last_surprise_pct"],
            row["rec_strong_buy"], row["rec_buy"], row["rec_hold"],
            row["rec_sell"], row["rec_strong_sell"],
            row["rec_bullish_share"], row["rec_bullish_share_90d"],
            row["rec_bullish_delta_90d"],
        ),
    )


def refresh_universe(symbols: Iterable[str], sleep_s: float = 1.1) -> dict:
    """Fetch Finnhub earnings + recs for every symbol. Polite 1s sleep
    between calls to stay under the 60 req/min tier."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not API_KEY:
        log.warning("FINNHUB_API_KEY not set — skipping")
        return {"status": "no_key"}
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
            log.debug("%s: %s", sym, e)
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", choices=["preferred", "full", "custom"],
                        default="preferred")
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument("--sleep", type=float, default=1.1)
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
