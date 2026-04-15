"""
User + billing + usage tables.

Extends the existing projector_cache.db with per-user state so the paywall
can enforce 3 projections/day for free users and track Pro subscriptions.

Schema:
  users: one row per Clerk user, with current tier and Stripe customer id.
  usage: append-only log of metered actions (currently: projections).
         Daily quota is computed from this table; no reset job needed.

Keeping everything in SQLite keeps the deploy story identical — D1 migration
is still a one-file swap when we outgrow it.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

from db import get_db

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    clerk_user_id       TEXT PRIMARY KEY,
    email               TEXT,
    tier                TEXT NOT NULL DEFAULT 'free',   -- 'free' | 'pro'
    stripe_customer_id  TEXT,
    stripe_subscription_id TEXT,
    subscription_status TEXT,                            -- Stripe's status string
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_stripe_customer
    ON users(stripe_customer_id);

CREATE TABLE IF NOT EXISTS usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    clerk_user_id   TEXT,       -- NULL for anonymous; IP tracked via anon_key
    anon_key        TEXT,       -- hashed IP for anonymous rate limiting
    action          TEXT NOT NULL,     -- 'project' | 'project_force' | ...
    symbol          TEXT,
    at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_user_at
    ON usage(clerk_user_id, at);

CREATE INDEX IF NOT EXISTS idx_usage_anon_at
    ON usage(anon_key, at);
"""


def init_users_db(conn: sqlite3.Connection) -> None:
    """Create user + usage tables if missing. Idempotent."""
    conn.executescript(SCHEMA)
    conn.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── User CRUD ──

def upsert_user(
    conn: sqlite3.Connection,
    clerk_user_id: str,
    email: Optional[str] = None,
) -> dict:
    """Ensure a user row exists; return the current row."""
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO users (clerk_user_id, email, tier, created_at, updated_at)
        VALUES (?, ?, 'free', ?, ?)
        ON CONFLICT(clerk_user_id) DO UPDATE SET
            email = COALESCE(excluded.email, users.email),
            updated_at = excluded.updated_at
        """,
        (clerk_user_id, email, now, now),
    )
    conn.commit()
    return get_user(conn, clerk_user_id)


def get_user(conn: sqlite3.Connection, clerk_user_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM users WHERE clerk_user_id = ?", (clerk_user_id,)
    ).fetchone()
    return dict(row) if row else None


def get_user_by_customer(conn: sqlite3.Connection, stripe_customer_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM users WHERE stripe_customer_id = ?", (stripe_customer_id,)
    ).fetchone()
    return dict(row) if row else None


def set_stripe_customer(
    conn: sqlite3.Connection,
    clerk_user_id: str,
    stripe_customer_id: str,
) -> None:
    conn.execute(
        "UPDATE users SET stripe_customer_id = ?, updated_at = ? WHERE clerk_user_id = ?",
        (stripe_customer_id, _now_iso(), clerk_user_id),
    )
    conn.commit()


def set_subscription(
    conn: sqlite3.Connection,
    clerk_user_id: str,
    *,
    subscription_id: Optional[str],
    status: Optional[str],
    tier: str,
) -> None:
    """Update subscription state. `tier` must be 'free' or 'pro'."""
    assert tier in ("free", "pro"), f"bad tier: {tier}"
    conn.execute(
        """
        UPDATE users SET
            stripe_subscription_id = ?,
            subscription_status = ?,
            tier = ?,
            updated_at = ?
        WHERE clerk_user_id = ?
        """,
        (subscription_id, status, tier, _now_iso(), clerk_user_id),
    )
    conn.commit()


# ── Usage + quotas ──

FREE_DAILY_PROJECTIONS = 3


def record_usage(
    conn: sqlite3.Connection,
    *,
    action: str,
    clerk_user_id: Optional[str] = None,
    anon_key: Optional[str] = None,
    symbol: Optional[str] = None,
) -> None:
    conn.execute(
        "INSERT INTO usage (clerk_user_id, anon_key, action, symbol, at) "
        "VALUES (?, ?, ?, ?, ?)",
        (clerk_user_id, anon_key, action, symbol, _now_iso()),
    )
    conn.commit()


def projections_in_last_24h(
    conn: sqlite3.Connection,
    *,
    clerk_user_id: Optional[str] = None,
    anon_key: Optional[str] = None,
) -> int:
    """Count 'project' actions by this actor in the past 24h."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    if clerk_user_id:
        row = conn.execute(
            "SELECT COUNT(*) FROM usage WHERE clerk_user_id = ? AND action = 'project' AND at > ?",
            (clerk_user_id, cutoff),
        ).fetchone()
    elif anon_key:
        row = conn.execute(
            "SELECT COUNT(*) FROM usage WHERE anon_key = ? AND action = 'project' AND at > ?",
            (anon_key, cutoff),
        ).fetchone()
    else:
        return 0
    return int(row[0]) if row else 0


def quota_for_user(user_row: Optional[dict]) -> dict:
    """Return {'limit': int|None, 'tier': str}. None limit = unlimited (Pro)."""
    if user_row and user_row.get("tier") == "pro":
        return {"limit": None, "tier": "pro"}
    return {"limit": FREE_DAILY_PROJECTIONS, "tier": "free"}
