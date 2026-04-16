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

from db import init_db, get_projection, get_projection_age_hours, save_projection, list_cached_symbols, get_sentiment
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
limiter = Limiter(key_func=get_remote_address, default_limits=["120/minute"])
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


@app.get("/api/picks")
@limiter.limit("30/minute")
def picks(
    request: Request,
    tier: Optional[str] = Query(None, regex="^(conservative|moderate|aggressive)$"),
):
    """Risk-tiered stock picks from cached projection scan.

    Returns top 10 per tier (conservative / moderate / aggressive),
    optionally filtered to a single tier.  Reads from a pre-scanned
    JSON cache refreshed daily by the worker cron.
    """
    results = get_picks(tier=tier)
    if not results:
        raise HTTPException(status_code=404, detail="No scan results available yet")
    scan_age = get_scan_age_hours()
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
        "tier": user_row.get("tier", "free"),
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
    user: dict = Depends(auth.current_user),
):
    """Start a Stripe Checkout session for Pro. Returns the redirect URL."""
    origin = request.headers.get("origin") or "https://s-tool.io"
    url = billing.create_checkout_session(
        users_conn,
        clerk_user_id=user["user_id"],
        email=user.get("email"),
        success_url=f"{origin}/?billing=success",
        cancel_url=f"{origin}/?billing=cancel",
    )
    return {"checkout_url": url}


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
