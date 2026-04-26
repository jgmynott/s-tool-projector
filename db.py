"""
Projection cache database.

SQLite locally, D1-compatible schema for eventual Cloudflare deploy.
All writes go through this module so we can swap the backend later
without touching the API or worker.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent / "projector_cache.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS projections (
    symbol        TEXT    NOT NULL,
    run_date      TEXT    NOT NULL,   -- ISO date of the run (market close date)
    horizon_days  INTEGER NOT NULL,
    current_price REAL    NOT NULL,
    -- Percentiles at horizon end
    p10           REAL,
    p25           REAL,
    p50           REAL,
    p75           REAL,
    p90           REAL,
    -- Full percentile curves (JSON arrays, one value per trading day)
    curves_json   TEXT,
    -- Model params
    mu            REAL,
    sigma         REAL,
    sigma_hist    REAL,
    sigma_mult    REAL    DEFAULT 1.0,
    mr_target     REAL,
    mr_kappa      REAL,
    upside_prob   REAL,
    -- VIX
    vix_value     REAL,
    vix_regime    TEXT,
    -- Sentiment (StockTwits snapshot)
    sent_bull     INTEGER,
    sent_bear     INTEGER,
    sent_tagged   INTEGER,
    sent_net      REAL,
    -- Milestones (JSON: [{label, date, p10, p25, p50, p75, p90, ret}])
    milestones_json TEXT,
    -- MR equilibrium curve (JSON array)
    mr_eq_json    TEXT,
    -- Historical OHLC for chart (JSON: {dates, opens, highs, lows, closes})
    hist_json     TEXT,
    -- Projection dates (JSON array of ISO dates)
    proj_dates_json TEXT,
    -- Fundamentals (JSON: {pe_trailing, market_cap, eps, sector, ...})
    fundamentals_json TEXT,
    -- Metadata
    num_paths     INTEGER,
    blend_mc      REAL    DEFAULT 0.30,
    computed_at   TEXT    NOT NULL,   -- ISO timestamp
    compute_secs  REAL,
    PRIMARY KEY (symbol, run_date, horizon_days)
);

CREATE TABLE IF NOT EXISTS sentiment_daily (
    date              TEXT NOT NULL,
    ticker            TEXT NOT NULL,
    mentions          INTEGER,
    positive          INTEGER,
    negative          INTEGER,
    neutral           INTEGER,
    score_signed_mean REAL,
    bullish_ratio     REAL,
    source            TEXT DEFAULT 'wsb',
    PRIMARY KEY (date, ticker, source)
);

CREATE INDEX IF NOT EXISTS idx_proj_symbol ON projections(symbol);
CREATE INDEX IF NOT EXISTS idx_proj_date ON projections(run_date);
CREATE INDEX IF NOT EXISTS idx_sent_ticker ON sentiment_daily(ticker);

-- Daily snapshot of the Strategist portfolio picks. One row per
-- (symbol, pick_date) — nightly worker inserts the top ranked picks so we
-- can retrospectively compute realized return, hit rate, and Sharpe by
-- tier. This is the ledger that gives /picks credibility.
CREATE TABLE IF NOT EXISTS picks_history (
    pick_date        TEXT    NOT NULL,   -- ISO date of the scan run
    symbol           TEXT    NOT NULL,
    tier             TEXT    NOT NULL,   -- conservative / moderate / aggressive
    entry_price      REAL    NOT NULL,   -- current_price at pick time
    p50_target       REAL    NOT NULL,   -- median 1-yr projection
    expected_return  REAL    NOT NULL,   -- (p50-current)/current
    risk             REAL,
    sharpe_proxy     REAL,
    horizon_days     INTEGER DEFAULT 252,
    rationale        TEXT,
    sec_fundamentals_json TEXT,
    created_at       TEXT    NOT NULL,
    PRIMARY KEY (pick_date, symbol)
);
CREATE INDEX IF NOT EXISTS idx_picks_hist_date ON picks_history(pick_date);
CREATE INDEX IF NOT EXISTS idx_picks_hist_symbol ON picks_history(symbol);
CREATE INDEX IF NOT EXISTS idx_picks_hist_tier ON picks_history(tier);

-- Pipeline-level events. Lets /track-record surface dates where no
-- picks were issued (outages, holidays, manual blackouts) instead of
-- presenting absence as silent missing data. Backfilling synthetic
-- picks would inject lookahead bias; an explicit outage marker keeps
-- the ledger honest.
CREATE TABLE IF NOT EXISTS pipeline_events (
    event_date  TEXT    NOT NULL,   -- ISO date the event applies to
    event_type  TEXT    NOT NULL,   -- 'outage' | 'maintenance' | 'holiday'
    summary     TEXT    NOT NULL,   -- human-readable one-liner for the UI
    detail      TEXT,                -- optional longer note
    created_at  TEXT    NOT NULL,
    PRIMARY KEY (event_date, event_type)
);
CREATE INDEX IF NOT EXISTS idx_pipeline_events_date ON pipeline_events(event_date);
"""


def get_db(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path else DB_PATH
    conn = sqlite3.connect(str(p), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: Path | str | None = None) -> sqlite3.Connection:
    conn = get_db(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ── Projection CRUD ──

def save_projection(conn: sqlite3.Connection, data: dict):
    cols = [
        "symbol", "run_date", "horizon_days", "current_price",
        "p10", "p25", "p50", "p75", "p90",
        "curves_json", "mu", "sigma", "sigma_hist", "sigma_mult",
        "mr_target", "mr_kappa", "upside_prob",
        "vix_value", "vix_regime",
        "sent_bull", "sent_bear", "sent_tagged", "sent_net",
        "milestones_json", "mr_eq_json", "hist_json", "proj_dates_json",
        "fundamentals_json",
        "num_paths", "blend_mc", "computed_at", "compute_secs",
    ]
    placeholders = ", ".join("?" for _ in cols)
    col_str = ", ".join(cols)
    vals = [data.get(c) for c in cols]
    conn.execute(
        f"INSERT OR REPLACE INTO projections ({col_str}) VALUES ({placeholders})",
        vals,
    )
    conn.commit()


def get_projection(
    conn: sqlite3.Connection,
    symbol: str,
    horizon_days: int,
    run_date: str | None = None,
) -> dict | None:
    if run_date is None:
        row = conn.execute(
            "SELECT * FROM projections WHERE symbol = ? AND horizon_days = ? "
            "ORDER BY run_date DESC LIMIT 1",
            (symbol.upper(), horizon_days),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM projections WHERE symbol = ? AND horizon_days = ? AND run_date = ?",
            (symbol.upper(), horizon_days, run_date),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    # Parse JSON fields
    for jf in ("curves_json", "milestones_json", "mr_eq_json", "hist_json", "proj_dates_json", "fundamentals_json"):
        if d.get(jf):
            d[jf] = json.loads(d[jf])
    return d


def list_cached_symbols(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT symbol, MAX(run_date) as last_run, "
        "GROUP_CONCAT(DISTINCT horizon_days) as horizons "
        "FROM projections GROUP BY symbol ORDER BY symbol"
    ).fetchall()
    return [dict(r) for r in rows]


def get_projection_age_hours(conn: sqlite3.Connection, symbol: str, horizon_days: int) -> float | None:
    row = conn.execute(
        "SELECT computed_at FROM projections WHERE symbol = ? AND horizon_days = ? "
        "ORDER BY run_date DESC LIMIT 1",
        (symbol.upper(), horizon_days),
    ).fetchone()
    if not row:
        return None
    computed = datetime.fromisoformat(row["computed_at"])
    if computed.tzinfo is None:
        computed = computed.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return (now - computed).total_seconds() / 3600


# ── Sentiment CRUD ──

def save_sentiment_rows(conn: sqlite3.Connection, rows: list[dict]):
    for r in rows:
        conn.execute(
            "INSERT OR REPLACE INTO sentiment_daily "
            "(date, ticker, mentions, positive, negative, neutral, "
            "score_signed_mean, bullish_ratio, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (r["date"], r["ticker"], r.get("mentions"), r.get("positive"),
             r.get("negative"), r.get("neutral"), r.get("score_signed_mean"),
             r.get("bullish_ratio"), r.get("source", "wsb")),
        )
    conn.commit()


def get_sentiment(
    conn: sqlite3.Connection,
    ticker: str,
    lookback_days: int = 10,
) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM sentiment_daily WHERE ticker = ? "
        "ORDER BY date DESC LIMIT ?",
        (ticker.upper(), lookback_days),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Picks history ──

def save_picks_history(conn: sqlite3.Connection, picks: list[dict],
                        pick_date: str | None = None) -> int:
    """Insert one row per pick into picks_history for the given date.

    Idempotent on (pick_date, symbol): re-running the same day's scan
    overwrites the row with the latest values rather than duplicating.
    Returns the number of rows written.
    """
    if pick_date is None:
        pick_date = datetime.now(timezone.utc).date().isoformat()
    created_at = datetime.now(timezone.utc).isoformat()
    n = 0
    for p in picks:
        sec_json = None
        if p.get("sec_fundamentals"):
            try:
                sec_json = json.dumps(p["sec_fundamentals"])
            except (TypeError, ValueError):
                sec_json = None
        conn.execute(
            """INSERT OR REPLACE INTO picks_history
               (pick_date, symbol, tier, entry_price, p50_target,
                expected_return, risk, sharpe_proxy, horizon_days,
                rationale, sec_fundamentals_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pick_date,
                p["symbol"].upper(),
                p.get("tier"),
                p.get("current_price"),
                p.get("p50_target"),
                p.get("expected_return"),
                p.get("risk"),
                p.get("sharpe_proxy"),
                p.get("horizon_days", 252),
                p.get("rationale"),
                sec_json,
                created_at,
            ),
        )
        n += 1
    conn.commit()
    return n


def get_picks_history(
    conn: sqlite3.Connection,
    tier: str | None = None,
    symbol: str | None = None,
    since_date: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """Fetch historical picks, newest first. Caller supplies current prices
    separately if realized returns are needed — we don't price here to keep
    this hot-path fast."""
    sql = "SELECT * FROM picks_history"
    conds, args = [], []
    if tier:
        conds.append("tier = ?"); args.append(tier)
    if symbol:
        conds.append("symbol = ?"); args.append(symbol.upper())
    if since_date:
        conds.append("pick_date >= ?"); args.append(since_date)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY pick_date DESC, symbol ASC LIMIT ?"
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("sec_fundamentals_json"):
            try: d["sec_fundamentals"] = json.loads(d["sec_fundamentals_json"])
            except (json.JSONDecodeError, TypeError): d["sec_fundamentals"] = None
        d.pop("sec_fundamentals_json", None)
        out.append(d)
    return out


# ── Pipeline events ──

def save_pipeline_event(
    conn: sqlite3.Connection,
    event_date: str,
    event_type: str,
    summary: str,
    detail: str | None = None,
) -> None:
    """Idempotent on (event_date, event_type) — re-running overwrites the row."""
    conn.execute(
        """INSERT OR REPLACE INTO pipeline_events
           (event_date, event_type, summary, detail, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (event_date, event_type, summary, detail,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def get_pipeline_events(
    conn: sqlite3.Connection,
    since_date: str | None = None,
    limit: int = 500,
) -> list[dict]:
    sql = "SELECT event_date, event_type, summary, detail FROM pipeline_events"
    args: list = []
    if since_date:
        sql += " WHERE event_date >= ?"
        args.append(since_date)
    sql += " ORDER BY event_date DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args).fetchall()]
