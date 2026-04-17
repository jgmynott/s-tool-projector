"""
S-Tool Projector API.

Thin FastAPI layer over the projection cache + on-demand computation.
Serves precomputed results from SQLite; falls back to live computation
for uncached symbols.

Run:  uvicorn api:app --reload --port 8000
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from db import init_db, get_projection, get_projection_age_hours, save_projection, list_cached_symbols, get_sentiment, get_picks_history
from projector_engine import run_projection
from hardening import health_checker
from portfolio_scanner import get_picks, get_scan_age_hours

import auth
import billing
import users_db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("api")

app = FastAPI(title="S-Tool Projector API", version="0.2.0")

# CORS: fail closed. No env var = dev localhost only. Never wildcard origins
# when Authorization bearer tokens are in play — a wildcard + credentials is a
# browser-side auth-bypass footgun even though we're not using cookies.
_cors_env = os.getenv("CORS_ORIGINS", "").strip()
_cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()] or [
    "http://localhost:8000",
    "http://localhost:5173",
    "http://127.0.0.1:8000",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Stripe-Signature"],
    allow_credentials=False,  # Bearer tokens, no cookies — keep this False.
)

# Per-IP rate limiting. Protects against abuse of /api/project (FMP quota +
# CPU) and /api/billing/checkout (spam Stripe customer creation). Webhook is
# exempt — Stripe retries hard and we MUST absorb those.
#
# Cloudflare strips upstream IP headers (x-forwarded-*) before proxying,
# so get_remote_address() sees Cloudflare's egress — one bucket for the
# whole internet. Cloudflare DOES pass cf-connecting-ip though, so we
# read that when present and fall back to the remote address otherwise.
def _client_ip(request: Request) -> str:
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=_client_ip, default_limits=["120/minute"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# ── Request counting & error rate tracking ──

_request_count = 0
_error_4xx = 0
_error_5xx = 0
_stats_lock = threading.Lock()


@app.middleware("http")
async def request_counter_middleware(request: Request, call_next):
    global _request_count, _error_4xx, _error_5xx
    response: Response = await call_next(request)

    with _stats_lock:
        _request_count += 1
        count = _request_count
        if 400 <= response.status_code < 500:
            _error_4xx += 1
        elif response.status_code >= 500:
            _error_5xx += 1

    if count % 100 == 0:
        log.info("Request count: %d (4xx=%d, 5xx=%d)", count, _error_4xx, _error_5xx)

    if response.status_code < 400:
        health_checker.record_success("api")
    else:
        health_checker.record_error("api")

    return response


# Separate connections: projector_cache.db (committed back via cron) vs
# users.db (private, never committed, under a Railway Volume in prod).
conn = init_db()
users_conn = users_db.get_users_db()
users_db.init_users_db(users_conn)

STALE_HOURS = 18  # recompute if older than this

# Paywall enforcement toggle. Off by default so scaffolding doesn't break the
# live site; flip to "true" in Railway env once the frontend can route users
# into sign-in / checkout on a 402.
PAYWALL_ENABLED = os.getenv("PAYWALL_ENABLED", "false").lower() == "true"

# Pre-launch safeguard: hard cap all non-owner users to 5 projections/hour.
# Prevents runaway costs from bots or abuse. Owner is identified by email.
# Remove this once we go live with proper paywall enforcement.
OWNER_EMAIL = os.getenv("OWNER_EMAIL", "jamesgmynott@gmail.com")
PRE_LAUNCH_HOURLY_CAP = 5


# ── Helpers ──

def _anon_key(request: Request) -> str:
    """Hashed client IP for anonymous rate limiting. Cheap, not PII-safe storage."""
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if not ip:
        ip = request.client.host if request.client else "unknown"
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


def _quota_headers(used: int, limit: Optional[int], tier: str) -> dict[str, str]:
    if limit is None:
        return {"X-Quota-Tier": tier, "X-Quota-Limit": "unlimited"}
    return {
        "X-Quota-Tier": tier,
        "X-Quota-Limit": str(limit),
        "X-Quota-Used": str(used),
        "X-Quota-Remaining": str(max(0, limit - used)),
    }


# ── Endpoints ──

@app.get("/api/project")
@limiter.limit("30/minute")
def project(
    request: Request,
    symbol: str = Query(..., min_length=1, max_length=10),
    horizon: int = Query(252, ge=5, le=756),
    force: bool = Query(False),
    user: Optional[dict] = Depends(auth.optional_user),
):
    """Return projection for a symbol. Uses cache if fresh, else computes live."""
    symbol = symbol.upper().strip()

    # ── Quota check ──
    clerk_user_id = user["user_id"] if user else None
    anon_key = None if clerk_user_id else _anon_key(request)
    user_row = None
    if clerk_user_id:
        user_row = users_db.upsert_user(users_conn, clerk_user_id, email=user.get("email"))

    quota = users_db.quota_for_user(user_row)
    used = users_db.projections_in_last_24h(
        users_conn, clerk_user_id=clerk_user_id, anon_key=anon_key
    )

    # Pre-launch safeguard: hard cap non-owner users to 5/hour.
    user_email = user.get("email") if user else None
    is_owner = user_email and user_email.lower() == OWNER_EMAIL.lower()
    if not is_owner:
        hourly = users_db.projections_in_last_hour(
            users_conn, clerk_user_id=clerk_user_id, anon_key=anon_key
        )
        if hourly >= PRE_LAUNCH_HOURLY_CAP:
            return JSONResponse(
                status_code=429,
                content={
                    "error": "pre_launch_cap",
                    "limit": PRE_LAUNCH_HOURLY_CAP,
                    "used": hourly,
                    "hint": "Pre-launch rate limit: 5 projections per hour. Check back soon.",
                },
            )

    if PAYWALL_ENABLED and quota["limit"] is not None and used >= quota["limit"]:
        return JSONResponse(
            status_code=402,
            content={
                "error": "quota_exceeded",
                "tier": quota["tier"],
                "limit": quota["limit"],
                "used": used,
                "hint": "Sign in for 3 free projections/day, or upgrade to Pro for unlimited.",
            },
            headers=_quota_headers(used, quota["limit"], quota["tier"]),
        )

    # ── Cache hit path ──
    if not force:
        age = get_projection_age_hours(conn, symbol, horizon)
        if age is not None and age < STALE_HOURS:
            cached = get_projection(conn, symbol, horizon)
            if cached:
                log.info(f"Cache hit: {symbol} h={horizon} age={age:.1f}h user={clerk_user_id or 'anon'}")
                users_db.record_usage(
                    users_conn, action="project", clerk_user_id=clerk_user_id,
                    anon_key=anon_key, symbol=symbol,
                )
                return JSONResponse(
                    content=cached,
                    headers=_quota_headers(used + 1, quota["limit"], quota["tier"]),
                )

    # ── Compute on demand ──
    log.info(f"Computing: {symbol} h={horizon} user={clerk_user_id or 'anon'}")
    try:
        result = run_projection(symbol, horizon_days=horizon)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        log.exception(f"Projection failed for {symbol}")
        raise HTTPException(status_code=500, detail=f"Projection failed: {e}")

    save_projection(conn, result)
    users_db.record_usage(
        users_conn,
        action="project_force" if force else "project",
        clerk_user_id=clerk_user_id,
        anon_key=anon_key,
        symbol=symbol,
    )
    log.info(f"Saved: {symbol} h={horizon} in {result['compute_secs']:.1f}s")

    return JSONResponse(
        content=get_projection(conn, symbol, horizon),
        headers=_quota_headers(used + 1, quota["limit"], quota["tier"]),
    )


@app.get("/api/cached")
def cached():
    return list_cached_symbols(conn)


@app.get("/api/sentiment")
def sentiment(
    ticker: str = Query(..., min_length=1, max_length=10),
    days: int = Query(10, ge=1, le=90),
):
    rows = get_sentiment(conn, ticker.upper().strip(), days)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No sentiment data for {ticker}")
    return rows


@app.get("/api/track-record")
@limiter.limit("30/minute")
def track_record(
    request: Request,
    tier: Optional[str] = Query(None, regex="^(conservative|moderate|aggressive)$"),
    lookback_days: int = Query(90, ge=7, le=365),
    user: Optional[dict] = Depends(auth.optional_user),
):
    """Historical picks ledger + realized performance per tier.

    Every pick ever shown on /picks is captured in picks_history by the
    nightly worker. This endpoint joins that ledger with each symbol's
    latest cached price to compute realized return, then summarises by
    tier (hit rate + median/mean return + n).

    Gated to Strategist tier — this is the track-record page that proves
    the picks are worth the $29/mo, so it belongs on the same side of the
    paywall as /picks.
    """
    is_strategist = False
    if user:
        user_row = users_db.upsert_user(users_conn, user["user_id"], email=user.get("email"))
        is_strategist = users_db.can_access_picks(user_row)

    from datetime import datetime as _dt, timedelta as _td
    since = (_dt.utcnow().date() - _td(days=lookback_days)).isoformat()

    rows = get_picks_history(conn, tier=tier, since_date=since, limit=2000)

    # Attach latest cached price + realized return
    price_cache: dict[str, float | None] = {}
    def _latest_price(sym: str):
        if sym in price_cache:
            return price_cache[sym]
        p = get_projection(conn, sym, 252)
        price = p.get("current_price") if p else None
        price_cache[sym] = price
        return price

    for r in rows:
        cur = _latest_price(r["symbol"])
        r["current_price"] = cur
        entry = r.get("entry_price")
        if cur and entry:
            r["realized_return"] = (cur - entry) / entry
            r["toward_target_pct"] = (
                (cur - entry) / (r["p50_target"] - entry)
                if r.get("p50_target") and r["p50_target"] != entry else None
            )
        else:
            r["realized_return"] = None
            r["toward_target_pct"] = None

    # Summary by tier — only picks that had at least 7 days to play out
    cutoff = (_dt.utcnow().date() - _td(days=7)).isoformat()
    matured = [r for r in rows if r["pick_date"] <= cutoff and r["realized_return"] is not None]
    summary: dict = {}
    for tname in ("conservative", "moderate", "aggressive"):
        bucket = [r for r in matured if r["tier"] == tname]
        if not bucket:
            summary[tname] = {"n": 0}
            continue
        rets = sorted(r["realized_return"] for r in bucket)
        n = len(rets)
        median = rets[n // 2] if n % 2 else (rets[n // 2 - 1] + rets[n // 2]) / 2
        mean = sum(rets) / n
        hits = sum(1 for r in bucket if r["realized_return"] > 0)
        summary[tname] = {
            "n": n,
            "median_return": round(median, 4),
            "mean_return": round(mean, 4),
            "hit_rate": round(hits / n, 3),
        }

    if not is_strategist:
        return JSONResponse(
            status_code=402,
            content={
                "error": "strategist_required",
                "tier_required": "strategist",
                "summary": summary,  # teaser: show the aggregate stats
                "hint": "Full pick-by-pick track record is part of Strategist ($29/mo).",
            },
        )

    return {
        "lookback_days": lookback_days,
        "tier_filter": tier,
        "summary": summary,
        "count": len(rows),
        "picks": rows,
    }


@app.get("/api/picks")
@limiter.limit("30/minute")
def picks(
    request: Request,
    tier: Optional[str] = Query(None, regex="^(conservative|moderate|aggressive)$"),
    user: Optional[dict] = Depends(auth.optional_user),
):
    """Risk-tiered stock picks from cached projection scan.

    Gated to the Strategist tier — this is the primary value exchange
    for that tier. Anonymous + free + pro users get a 402 with a teaser
    payload (top 3 tickers per bucket, no prices/scores) so the UI can
    render an "unlock" state. Strategist users get the full ranked list.
    """
    # Upgrade-aware preview for non-Strategists.
    is_strategist = False
    if user:
        user_row = users_db.upsert_user(users_conn, user["user_id"], email=user.get("email"))
        is_strategist = users_db.can_access_picks(user_row)

    results = get_picks(tier=tier)
    if not results:
        raise HTTPException(status_code=404, detail="No scan results available yet")
    scan_age = get_scan_age_hours()

    if not is_strategist:
        # Teaser: 3 per bucket, not results[:9] (which would be same-tier heavy).
        teaser: list[dict] = []
        for t in ("conservative", "moderate", "aggressive"):
            bucket = [p for p in results if p.get("tier") == t][:3]
            teaser.extend({"symbol": p["symbol"], "tier": t} for p in bucket)
        return JSONResponse(
            status_code=402,
            content={
                "error": "strategist_required",
                "tier_required": "strategist",
                "teaser": teaser,
                "scan_age_hours": round(scan_age, 1) if scan_age is not None else None,
                "hint": "Portfolio picks are part of the Strategist tier ($29/mo). Upgrade to unlock full ranked lists.",
            },
        )

    return {
        "scan_age_hours": round(scan_age, 1) if scan_age is not None else None,
        "tier_filter": tier,
        "count": len(results),
        "picks": results,
    }


# ── User + billing ──

@app.get("/api/me")
def api_me(user: Optional[dict] = Depends(auth.optional_user)):
    """Return current user context. Anonymous when unauthed."""
    if not user:
        return {"authenticated": False, "tier": "anonymous"}
    user_row = users_db.upsert_user(users_conn, user["user_id"], email=user.get("email"))
    quota = users_db.quota_for_user(user_row)
    used = users_db.projections_in_last_24h(users_conn, clerk_user_id=user["user_id"])
    return {
        "authenticated": True,
        "user_id": user["user_id"],
        "email": user.get("email"),
        "tier": users_db.effective_tier(user_row),
        "subscription_status": user_row.get("subscription_status"),
        "quota": {
            "limit": quota["limit"],
            "used": used,
            "remaining": (None if quota["limit"] is None
                          else max(0, quota["limit"] - used)),
        },
    }


@app.post("/api/billing/checkout")
@limiter.limit("10/minute")
def api_billing_checkout(
    request: Request,
    tier: str = Query("pro", regex="^(pro|strategist)$"),
    user: dict = Depends(auth.current_user),
):
    """Start a Stripe Checkout session for the requested tier."""
    origin = request.headers.get("origin") or "https://s-tool.io"
    url = billing.create_checkout_session(
        users_conn,
        clerk_user_id=user["user_id"],
        email=user.get("email"),
        success_url=f"{origin}/?billing=success&tier={tier}",
        cancel_url=f"{origin}/?billing=cancel",
        tier=tier,
    )
    return {"checkout_url": url, "tier": tier}


@app.post("/api/billing/portal")
@limiter.limit("10/minute")
def api_billing_portal(
    request: Request,
    user: dict = Depends(auth.current_user),
):
    """Return a Stripe Customer Portal URL for the current user."""
    origin = request.headers.get("origin") or "https://s-tool.io"
    url = billing.create_portal_session(
        users_conn,
        clerk_user_id=user["user_id"],
        return_url=f"{origin}/",
    )
    return {"portal_url": url}


@app.post("/api/billing/webhook")
async def api_billing_webhook(request: Request):
    """Stripe webhook endpoint. Signature verified via STRIPE_WEBHOOK_SECRET."""
    sig = request.headers.get("stripe-signature", "")
    payload = await request.body()
    return billing.handle_webhook(users_conn, payload=payload, signature=sig)


@app.get("/api/_admin/user/{clerk_user_id}")
@limiter.limit("10/minute")
def api_admin_user(clerk_user_id: str, request: Request):
    """Admin lookup: dump a user's row. Protected by ADMIN_TOKEN header."""
    expected = os.getenv("ADMIN_TOKEN", "")
    if not expected or request.headers.get("x-admin-token", "") != expected:
        raise HTTPException(status_code=403, detail="Forbidden")
    row = users_db.get_user(users_conn, clerk_user_id)
    return {"user": row}


@app.get("/api/_admin/users")
@limiter.limit("10/minute")
def api_admin_list_users(request: Request):
    """Admin: list all users. Small table so no pagination yet."""
    expected = os.getenv("ADMIN_TOKEN", "")
    if not expected or request.headers.get("x-admin-token", "") != expected:
        raise HTTPException(status_code=403, detail="Forbidden")
    rows = users_conn.execute(
        "SELECT clerk_user_id, email, tier, subscription_status, created_at, updated_at "
        "FROM users ORDER BY created_at DESC"
    ).fetchall()
    return {"users": [dict(r) for r in rows], "count": len(rows)}


@app.post("/api/_admin/backfill_emails")
@limiter.limit("10/minute")
def api_admin_backfill_emails(request: Request):
    """Admin: walk every user row whose email is NULL and look it up via
    Clerk Backend API. Idempotent — safe to run repeatedly. Returns a
    summary of what was filled + which users still lack email (usually
    because the Clerk user has no primary email on file)."""
    expected = os.getenv("ADMIN_TOKEN", "")
    if not expected or request.headers.get("x-admin-token", "") != expected:
        raise HTTPException(status_code=403, detail="Forbidden")

    rows = users_conn.execute(
        "SELECT clerk_user_id FROM users WHERE email IS NULL OR email = ''"
    ).fetchall()
    to_fill = [r[0] for r in rows]
    filled: list[dict] = []
    skipped: list[str] = []
    for clerk_id in to_fill:
        email = auth.lookup_email_from_clerk(clerk_id)
        if email:
            users_conn.execute(
                "UPDATE users SET email = ?, updated_at = datetime('now') "
                "WHERE clerk_user_id = ?",
                (email, clerk_id),
            )
            filled.append({"clerk_user_id": clerk_id, "email": email})
        else:
            skipped.append(clerk_id)
    users_conn.commit()
    return {
        "candidates": len(to_fill),
        "filled": len(filled),
        "skipped": len(skipped),
        "filled_list": filled,
        "skipped_list": skipped,
    }


@app.post("/api/_admin/force_resync/{clerk_user_id}")
@limiter.limit("10/minute")
def api_admin_force_resync(clerk_user_id: str, request: Request):
    """Admin: force a tier resync for any user by clerk_user_id."""
    expected = os.getenv("ADMIN_TOKEN", "")
    if not expected or request.headers.get("x-admin-token", "") != expected:
        raise HTTPException(status_code=403, detail="Forbidden")
    return billing.resync_by_customer(users_conn, clerk_user_id)


@app.post("/api/billing/resync")
@limiter.limit("5/minute")
def api_billing_resync(request: Request, user: dict = Depends(auth.current_user)):
    """Re-fetch the authed user's subscription from Stripe and refresh tier.

    Self-service fix for tier-drift (e.g. when a webhook handler bug wrote
    an incorrect tier). Safe to call repeatedly — the derivation is
    idempotent and always reflects the current Stripe state.
    """
    # Always route through resync_by_customer — it handles the case where
    # stripe_subscription_id was never recorded locally (e.g. when the Stripe
    # webhook isn't configured for checkout.session.completed events) by
    # looking the subscription up via the customer id.
    return billing.resync_by_customer(users_conn, user["user_id"])


# ── Health & Provider Status ──

@app.get("/api/health")
def api_health():
    health = health_checker.check_health()
    with _stats_lock:
        health["api_stats"] = {
            "total_requests": _request_count,
            "errors_4xx": _error_4xx,
            "errors_5xx": _error_5xx,
        }
    health["paywall_enabled"] = PAYWALL_ENABLED
    health["overall"] = "healthy" if health_checker.is_healthy() else "degraded"
    return health


@app.get("/api/providers")
def api_providers():
    health = health_checker.check_health()
    providers = health.get("providers", {})
    for name in ("yfinance", "stocktwits", "arctic_shift"):
        if name not in providers:
            providers[name] = {"available": True, "status": "not_tracked"}
    return {"providers": providers}


# ── Serve frontend ──

FRONTEND = Path(__file__).parent / "frontend.html"


@app.get("/")
def index():
    if FRONTEND.exists():
        return FileResponse(FRONTEND, media_type="text/html")
    dash = Path(__file__).parent / "projection_dashboard.html"
    if dash.exists():
        return FileResponse(dash, media_type="text/html")
    return {"message": "S-Tool Projector API", "docs": "/docs"}
