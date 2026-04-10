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
    for jf in ("curves_json", "milestones_json", "mr_eq_json", "hist_json", "proj_dates_json"):
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
