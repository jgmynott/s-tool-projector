"""
FINRA consolidated short interest signal.

Pulls bi-monthly short interest snapshots per symbol from FINRA's public API
and builds two derivative signals:

  1. days_to_cover — a traditional short-squeeze proxy. High DTC means it would
     take many days of average volume for all shorts to cover, raising the
     probability of a squeeze-driven price move.

  2. short_interest_change_pct — the delta in short positions between the
     prior snapshot and the current one. Sharply rising short interest signals
     institutional bearishness (potential contrarian signal).

Data lives in real capital commitment (borrowing costs to maintain the short)
rather than text/sentiment — which is why it may survive post-2023 when every
crowd-sentiment signal failed on our backtests.

FINRA endpoint:
  https://api.finra.org/data/group/OTCMarket/name/consolidatedShortInterest

Returns CSV. 5,000 rows per page; paginate via `offset`. No auth.

Known API quirks (discovered empirically):
  * `issueName` is not quoted even when it contains commas → custom parser.
  * The `compareFilters=settlementDate=YYYY-MM-DD` param is silently ignored
    when used via GET query-string — the API returns the same (2020-era)
    dataset regardless of filter. Confirmed via curl with unencoded `=`.
    Workaround for real-time: fetch the whole dataset and filter client-side
    (200k rows is tractable), OR use FINRA's separate daily short-volume
    files endpoint for day-level data.
  * Sorting only works when a partition key (settlementDate) is specified
    via EQUAL filter first — which itself doesn't work. Chicken-and-egg.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

import requests

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

log = logging.getLogger("short_interest")

FINRA_BASE = "https://api.finra.org/data/group/OTCMarket/name/consolidatedShortInterest"
PAGE_SIZE = 5000
USER_AGENT = "s-tool-projector/1.0 (research)"

DB_PATH = Path(__file__).parent / "projector_cache.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS short_interest (
    symbol              TEXT    NOT NULL,
    settlement_date     TEXT    NOT NULL,
    current_short_qty   INTEGER,
    previous_short_qty  INTEGER,
    avg_daily_volume    INTEGER,
    days_to_cover       REAL,
    change_pct          REAL,
    exchange            TEXT,
    PRIMARY KEY (symbol, settlement_date)
);
CREATE INDEX IF NOT EXISTS idx_si_symbol ON short_interest(symbol);
CREATE INDEX IF NOT EXISTS idx_si_date   ON short_interest(settlement_date);
"""


def init_short_interest_table(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# FINRA's CSV has 14 header columns (below). issueName may contain commas
# without being quoted, so we can't use csv.DictReader — instead we split on
# comma and anchor on the known-clean positions at the start (2 fields) and
# end (11 fields), treating whatever's in the middle as the issueName.
HEADER = [
    "accountingYearMonthNumber", "symbolCode", "issueName",
    "issuerServicesGroupExchangeCode", "marketClassCode",
    "currentShortPositionQuantity", "previousShortPositionQuantity",
    "stockSplitFlag", "averageDailyVolumeQuantity", "daysToCoverQuantity",
    "revisionFlag", "changePercent", "changePreviousNumber", "settlementDate",
]
N_COLS = len(HEADER)


def _parse_line(line: str) -> dict | None:
    """Parse one FINRA CSV data row, tolerating unquoted commas in issueName."""
    tokens = line.split(",")
    if len(tokens) < N_COLS:
        return None
    if len(tokens) > N_COLS:
        # Extra tokens belong to issueName. Fold them back.
        extras = len(tokens) - N_COLS
        name = ",".join(tokens[2 : 3 + extras])
        tokens = tokens[:2] + [name] + tokens[3 + extras:]
    if len(tokens) != N_COLS:
        return None
    row = dict(zip(HEADER, tokens))
    # Sanity: settlement_date must be YYYY-MM-DD, accounting must be 8 digits.
    if not _DATE_RE.match(row["settlementDate"]):
        return None
    if not re.match(r"^\d{6,8}$", row["accountingYearMonthNumber"]):
        return None
    return row


def _fetch_page(offset: int = 0, limit: int = PAGE_SIZE, settlement_date: str | None = None) -> list[dict]:
    """Fetch one page of FINRA short interest data as list of dicts."""
    params = {"limit": limit, "offset": offset}
    if settlement_date:
        params["compareFilters"] = f"settlementDate={settlement_date}"
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(FINRA_BASE, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    rows = []
    for line in r.text.splitlines()[1:]:  # skip header
        row = _parse_line(line)
        if row:
            rows.append(row)
    return rows


def fetch_all_for_date(settlement_date: str, max_pages: int = 40) -> list[dict]:
    """Fetch all short interest rows for a single settlement date (~8k symbols)."""
    rows = []
    for page in range(max_pages):
        batch = _fetch_page(offset=page * PAGE_SIZE, settlement_date=settlement_date)
        if not batch:
            break
        rows.extend(batch)
        log.info(f"  fetched page {page+1}: {len(batch)} rows (total {len(rows)})")
        if len(batch) < PAGE_SIZE:
            break
        time.sleep(1.0)  # be polite
    return rows


def save_rows(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    """Write FINRA rows into the short_interest table. Returns count written.

    Drops rows with malformed dates — FINRA's CSV doesn't quote `issueName`
    even when it contains commas (e.g. "AMC ENTERTAINMENT HOLDINGS, IN"),
    which shifts every subsequent field by one position. Detect and skip.
    """
    count = skipped = 0
    for r in rows:
        settlement = (r.get("settlementDate") or "").strip()
        if not _DATE_RE.match(settlement):
            skipped += 1
            continue
        try:
            conn.execute(
                """INSERT OR REPLACE INTO short_interest
                   (symbol, settlement_date, current_short_qty, previous_short_qty,
                    avg_daily_volume, days_to_cover, change_pct, exchange)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    r.get("symbolCode", "").strip().upper(),
                    settlement,
                    _i(r.get("currentShortPositionQuantity")),
                    _i(r.get("previousShortPositionQuantity")),
                    _i(r.get("averageDailyVolumeQuantity")),
                    _f(r.get("daysToCoverQuantity")),
                    _f(r.get("changePercent")),
                    r.get("issuerServicesGroupExchangeCode", "").strip() or None,
                ),
            )
            count += 1
        except Exception as e:
            log.warning(f"skip row {r.get('symbolCode', '?')}: {e}")
            skipped += 1
    conn.commit()
    if skipped:
        log.info(f"skipped {skipped} malformed rows (unquoted-comma CSV corruption)")
    return count


def _i(v) -> int | None:
    if v in (None, "", " "):
        return None
    try: return int(float(v))
    except (ValueError, TypeError): return None


def _f(v) -> float | None:
    if v in (None, "", " "):
        return None
    try: return float(v)
    except (ValueError, TypeError): return None


# ── Signal computation ──

def get_short_interest_signal(conn: sqlite3.Connection, symbol: str) -> dict | None:
    """Return latest short-interest metrics for a symbol, or None if not available.

    Output:
      {
        "symbol":            upper-cased ticker,
        "settlement_date":   ISO date of the latest snapshot,
        "days_to_cover":     float (squeeze-risk metric; 5+ is elevated),
        "short_pct_change":  change in short positions (%) vs previous snapshot,
        "current_short_qty": raw shares short,
        "raw_score":         normalized composite for use as an engine tilt
                             (positive = bearish / potential squeeze),
      }
    """
    row = conn.execute(
        """SELECT settlement_date, days_to_cover, change_pct,
                  current_short_qty, avg_daily_volume
           FROM short_interest WHERE symbol = ?
           ORDER BY settlement_date DESC LIMIT 1""",
        (symbol.upper(),),
    ).fetchone()
    if not row:
        return None
    dtc, chg, cur, adv = row[1], row[2], row[3], row[4]

    # Composite: weight DTC (standardized ~0-10 scale) and 1-month change %
    raw_score = 0.0
    if dtc is not None:
        raw_score += min(dtc, 20) / 20  # 0..1
    if chg is not None:
        raw_score += max(-1.0, min(1.0, chg / 100))  # -1..1

    return {
        "symbol": symbol.upper(),
        "settlement_date": row[0],
        "days_to_cover": dtc,
        "short_pct_change": chg,
        "current_short_qty": cur,
        "avg_daily_volume": adv,
        "raw_score": round(raw_score, 3),
    }


# ── CLI ──

def discover_latest_date() -> str | None:
    """Find the most recent FINRA short-interest settlement date with data.

    FINRA publishes mid-month and end-of-month. Probe backward from today's
    date at those cadences until a query returns rows.
    """
    from datetime import date, timedelta
    today = date.today()
    # Candidate dates: mid-15 and end-of-month for last 3 months
    candidates = []
    for months_back in range(4):
        y, m = today.year, today.month - months_back
        while m <= 0:
            m += 12
            y -= 1
        # end of month
        if m == 12:
            eom = date(y + 1, 1, 1) - timedelta(days=1)
        else:
            eom = date(y, m + 1, 1) - timedelta(days=1)
        candidates.append(eom.isoformat())
        candidates.append(date(y, m, 15).isoformat())
    candidates = sorted(set(candidates), reverse=True)

    for d in candidates:
        if d > today.isoformat():
            continue
        try:
            rows = _fetch_page(offset=0, limit=5, settlement_date=d)
            if rows:
                log.info(f"Latest settlement date with data: {d}")
                return d
        except Exception as e:
            log.warning(f"probe {d} failed: {e}")
    return None


def refresh_latest() -> None:
    """Fetch the most recent FINRA short-interest snapshot. For cron/manual use."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    conn = sqlite3.connect(str(DB_PATH))
    init_short_interest_table(conn)

    latest_date = discover_latest_date()
    if not latest_date:
        log.error("Could not find a recent FINRA settlement date with data")
        return

    rows = fetch_all_for_date(latest_date)
    log.info(f"Fetched {len(rows)} rows for {latest_date}")
    written = save_rows(conn, rows)
    log.info(f"Saved {written} rows to short_interest table")


if __name__ == "__main__":
    refresh_latest()
