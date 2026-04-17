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

import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# Users + usage live in a SEPARATE DB from projector_cache.db because the
# daily-refresh cron commits projector_cache.db back to a public repo. Keeping
# PII (email, Stripe customer ids) out of that DB is the whole point.
#
# Default path is repo-local for dev; in prod set USERS_DB_PATH to a Railway
# Volume mount (e.g. /data/users.db) so rows survive container restarts.
USERS_DB_PATH = os.getenv("USERS_DB_PATH", str(Path(__file__).parent / "users.db"))

# Strategist override — any authed user matching the owner env vars OR
# appearing in the comped-grants lists is treated as Strategist regardless
# of their actual `tier` column. Two uses:
#   1. Founder bypasses checkout (OWNER_EMAIL / OWNER_CLERK_USER_ID)
#   2. Friends, early testers, comped accounts (STRATEGIST_GRANT_EMAILS /
#      STRATEGIST_GRANT_CLERK_IDS — each comma-separated).
# Using env vars rather than a DB table keeps this auditable and easy to
# grant/revoke with a single Railway variable edit.
OWNER_CLERK_USER_ID = os.getenv("OWNER_CLERK_USER_ID", "").strip()
OWNER_EMAIL = os.getenv("OWNER_EMAIL", "").strip().lower()


def _split_csv_lower(v: str) -> set[str]:
    return {x.strip().lower() for x in (v or "").split(",") if x.strip()}


def _split_csv(v: str) -> set[str]:
    return {x.strip() for x in (v or "").split(",") if x.strip()}


# Hard-coded comped Strategist accounts. Audited via git history; add
# here for anyone who should always be on the top tier without going
# through Stripe and without depending on a Railway env var (which
# doesn't survive every operational reshuffle). Email must match what
# Clerk has on file.
_COMPED_STRATEGIST_EMAILS = {
    "kevinrvandelden@gmail.com",  # Comped 2026-04-17
}

STRATEGIST_GRANT_EMAILS = (
    _split_csv_lower(os.getenv("STRATEGIST_GRANT_EMAILS", ""))
    | _COMPED_STRATEGIST_EMAILS
)
STRATEGIST_GRANT_CLERK_IDS = _split_csv(os.getenv("STRATEGIST_GRANT_CLERK_IDS", ""))


def _has_strategist_override(user_row: Optional[dict]) -> bool:
    if not user_row:
        return False
    cid = user_row.get("clerk_user_id") or ""
    em = (user_row.get("email") or "").lower()
    if OWNER_CLERK_USER_ID and cid == OWNER_CLERK_USER_ID:
        return True
    if OWNER_EMAIL and em == OWNER_EMAIL:
        return True
    if cid and cid in STRATEGIST_GRANT_CLERK_IDS:
        return True
    if em and em in STRATEGIST_GRANT_EMAILS:
        return True
    return False


# Alias for callers that already import this name.
_is_owner = _has_strategist_override

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


def get_users_db(path: str | None = None) -> sqlite3.Connection:
    """Open (or create) the users DB, separate from projector_cache.db."""
    p = path or USERS_DB_PATH
    # Ensure parent dir exists (matters when mounted under /data on Railway).
    parent = Path(p).parent
    parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


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
    """Update subscription state. `tier` must be 'free' | 'pro' | 'strategist'."""
    assert tier in ("free", "pro", "strategist"), f"bad tier: {tier}"
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
PRO_DAILY_PROJECTIONS = 10
# Strategist tier ($29/mo) is the top tier: unlimited projections + access to
# portfolio picks (risk-tiered recommendations). `None` means no daily cap.
STRATEGIST_DAILY_PROJECTIONS = None


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


def projections_in_last_hour(
    conn: sqlite3.Connection,
    *,
    clerk_user_id: Optional[str] = None,
    anon_key: Optional[str] = None,
) -> int:
    """Count 'project' actions by this actor in the past 1 hour."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    if clerk_user_id:
        row = conn.execute(
            "SELECT COUNT(*) FROM usage WHERE clerk_user_id = ? AND action LIKE 'project%' AND at > ?",
            (clerk_user_id, cutoff),
        ).fetchone()
    elif anon_key:
        row = conn.execute(
            "SELECT COUNT(*) FROM usage WHERE anon_key = ? AND action LIKE 'project%' AND at > ?",
            (anon_key, cutoff),
        ).fetchone()
    else:
        return 0
    return int(row[0]) if row else 0


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


def effective_tier(user_row: Optional[dict]) -> str:
    """The tier we should treat this user as. Applies the owner override
    before falling back to the stored tier column. Everything else in the
    app should read through this helper (not user_row['tier']) so billing
    bugs can't lock the owner out of their own product."""
    if _is_owner(user_row):
        return "strategist"
    return (user_row.get("tier") if user_row else None) or "free"


def quota_for_user(user_row: Optional[dict]) -> dict:
    """Return {'limit': int|None, 'tier': str}.

    - strategist: unlimited (None)
    - pro: 10/day
    - free / anonymous: 3/day
    """
    tier = effective_tier(user_row)
    if tier == "strategist":
        return {"limit": STRATEGIST_DAILY_PROJECTIONS, "tier": "strategist"}
    if tier == "pro":
        return {"limit": PRO_DAILY_PROJECTIONS, "tier": "pro"}
    return {"limit": FREE_DAILY_PROJECTIONS, "tier": "free"}


def can_access_picks(user_row: Optional[dict]) -> bool:
    """Portfolio picks are gated to the Strategist tier (or the owner)."""
    return effective_tier(user_row) == "strategist"
