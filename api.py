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

from db import (
    init_db, get_projection, get_projection_age_hours, save_projection,
    list_cached_symbols, get_sentiment, get_picks_history,
    save_pipeline_event, get_pipeline_events,
)
from projector_engine import run_projection
from hardening import health_checker
from portfolio_scanner import get_picks, get_scan_age_hours, load_cached_asymmetric_picks

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

# Seed the known nightly-pipeline outage so /track-record renders the
# 8-day gap as an explicit event rather than silent missing data. Insert
# is idempotent on (event_date, event_type), safe on every boot. We do
# NOT backfill picks_history with synthetic rows for these dates —
# computing what we "would have" picked using post-outage data would
# inject lookahead bias and corrupt the realized-return scoreboard.
for _outage_date in (
    "2026-04-19", "2026-04-20", "2026-04-21", "2026-04-22",
    "2026-04-23", "2026-04-24", "2026-04-25",
):
    save_pipeline_event(
        conn, _outage_date, "outage",
        summary="Nightly pipeline failed; no picks issued",
        detail="preflight.py crashed on a None asymmetric block; deploy gate aborted. Resolved 2026-04-26.",
    )

# Log the DB path on boot so Railway deploy logs make it obvious whether
# the USERS_DB_PATH env var is pointing at the mounted Volume or the
# ephemeral container fs. If you see a non-`/data/...` path on Railway,
# the Volume isn't wired up and every deploy will wipe paying users.
log.info("users_db path: %s", users_db.USERS_DB_PATH)

# Deploy-time diagnostic: what's actually under /app/data_cache/ at
# startup? Tells us whether the runtime JSONs the API endpoints rely on
# actually shipped via `railway up`, or were stripped by .gitignore.
try:
    import os as _os
    _dc = Path(__file__).parent / "data_cache"
    if _dc.exists():
        _top = sorted(_os.listdir(_dc))[:30]
        log.info("data_cache/ top entries (first 30): %s", _top)
        _btr = _dc / "backtest_report.json"
        log.info("backtest_report.json present: %s size: %s",
                 _btr.exists(),
                 _btr.stat().st_size if _btr.exists() else 0)
    else:
        log.warning("data_cache/ directory missing at %s", _dc)
except Exception as _e:
    log.warning("data_cache probe failed: %s", _e)

# On first boot after the Volume is attached, the file at /data/users.db
# is empty even though we shipped real users before. Log a warning so we
# notice and can run /api/_admin/backfill_emails + force_resync for
# anyone with a stripe_customer_id.
try:
    _user_count = users_conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    log.info("users_db row count on boot: %d", _user_count)
    if _user_count == 0 and users_db.USERS_DB_PATH.startswith("/data/"):
        log.warning(
            "users_db at /data/%s is empty on boot — this is either the "
            "first deploy with a Volume attached, or the Volume is fresh. "
            "Paid users will re-materialise on their next /api/me hit but "
            "will land as tier='free' until /api/billing/resync runs.",
            users_db.USERS_DB_PATH.split("/", 2)[-1],
        )
except Exception as e:
    log.warning("users_db row count probe failed: %s", e)

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


@app.get("/api/data-status")
@limiter.limit("60/minute")
def data_status(request: Request):
    """Surface what's feeding the model — the answer to "what data are
    you running on today?". Covers:
      - short_interest_yf: yfinance short-interest snapshots
      - picks_history: the public ledger of what was recommended
      - most recent short-interest ablation run (from runtime_data/)
      - wave 1 honest-audit generation timestamp
    """
    from pathlib import Path as _Path
    import json as _json
    parent = _Path(__file__).parent

    out: dict = {"generated_at": int(__import__("time").time()), "feeds": {}}

    # Short interest (yfinance)
    try:
        r = conn.execute(
            """SELECT COUNT(*), COUNT(DISTINCT symbol), MAX(fetched_at),
                      MAX(short_snapshot_date)
               FROM short_interest_yf"""
        ).fetchone()
        out["feeds"]["short_interest_yf"] = {
            "rows": r[0], "symbols": r[1],
            "last_fetched_at": r[2],
            "last_snapshot_date": r[3],
            "source": "yfinance Ticker.info (shortPercentOfFloat, shortRatio)",
            "refresh_cadence": "nightly",
            "status": "live" if r[0] else "pending",
        }
    except Exception as e:
        out["feeds"]["short_interest_yf"] = {"status": "error", "error": str(e)}

    # Picks history ledger
    try:
        r = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT symbol), MAX(pick_date), MIN(pick_date) "
            "FROM picks_history"
        ).fetchone()
        out["feeds"]["picks_history"] = {
            "rows": r[0], "symbols": r[1],
            "earliest_pick_date": r[3], "latest_pick_date": r[2],
            "refresh_cadence": "every pipeline run (≥ daily)",
            "status": "live" if r[0] else "pending",
        }
    except Exception as e:
        out["feeds"]["picks_history"] = {"status": "error", "error": str(e)}

    # Latest short-interest ablation result
    si_abl_path = parent / "runtime_data" / "short_interest_ablation_latest.json"
    if si_abl_path.exists():
        try:
            d = _json.loads(si_abl_path.read_text())
            runs = d.get("runs", {})
            baseline_hit = runs.get("base_8", {}).get("hit_100")
            deltas = {}
            for name, r in runs.items():
                if name == "base_8":
                    continue
                if baseline_hit and r.get("hit_100") is not None:
                    deltas[name] = round(r["hit_100"] - baseline_hit, 4)
            out["feeds"]["short_interest_ablation"] = {
                "run_date": d.get("run_date"),
                "si_coverage_pct": d.get("si_coverage_pct"),
                "si_rows_loaded": d.get("si_rows_loaded"),
                "si_distinct_symbols": d.get("si_distinct_symbols"),
                "baseline_hit_100": baseline_hit,
                "deltas_vs_baseline": deltas,
                "status": "live",
            }
        except Exception as e:
            out["feeds"]["short_interest_ablation"] = {"status": "error", "error": str(e)}
    else:
        out["feeds"]["short_interest_ablation"] = {
            "status": "pending",
            "note": "first run lands after the next scheduled slow path (23:00 UTC)",
        }

    # Honest audit staleness
    audit = parent / "runtime_data" / "wave1_honest_audit.json"
    if audit.exists():
        try:
            d = _json.loads(audit.read_text())
            out["feeds"]["honest_audit"] = {
                "generated_at": d.get("generated_at"),
                "universe_rows": d.get("universe_rows"),
                "delisted_mid_window_pct": d.get("delisted_mid_window_pct"),
                "illiquid_pct": d.get("illiquid_pct"),
                "status": "live",
            }
        except Exception:
            pass

    # Roadmap teaser — data sources we haven't wired yet but have keys for
    out["pipeline_roadmap"] = [
        {"source": "Finnhub earnings calendar + recommendation trends",
         "status": "queued", "key_present": bool(os.getenv("FINNHUB_API_KEY"))},
        {"source": "Polygon options flow + put/call ratio",
         "status": "queued", "key_present": bool(os.getenv("POLYGON_API_KEY"))},
    ]

    return out


@app.get("/api/honest-audit")
@limiter.limit("60/minute")
def honest_audit(request: Request):
    """Wave 1 honest audit — survivorship + liquidity + transaction costs
    applied to our published rankings. Served as a second-layer overlay
    over /api/backtest-report so the site can quote tradeable numbers,
    not the pre-filter ones. File lives at runtime_data/ to guarantee
    it ships via railway up (see gitignore history)."""
    parent = Path(__file__).parent
    for candidate in (parent / "runtime_data" / "wave1_honest_audit.json",
                      parent / "data_cache"   / "wave1_honest_audit.json"):
        if candidate.exists():
            import json as _json
            try:
                return _json.loads(candidate.read_text())
            except Exception as e:
                raise HTTPException(status_code=500,
                                     detail=f"Honest-audit parse error: {e}")
    raise HTTPException(status_code=404, detail="Honest audit not yet generated")


@app.get("/api/backtest-report")
@limiter.limit("60/minute")
def backtest_report(request: Request):
    """Serve the nightly backtest report as JSON.

    The file is written by overnight_backtest.py after each refresh and
    contains the provable-performance evidence the /track-record page
    renders: hit-rate distributions, lift vs baseline, regime-conditional
    performance, simulated-portfolio stats, bootstrap confidence
    intervals. Cached, cheap to serve.
    """
    # Dual-path lookup: runtime_data/ is the Railway-shipped copy (ships
    # reliably because the directory has no gitignore entry), data_cache/
    # is the local dev path. Fall through either order.
    parent = Path(__file__).parent
    for candidate in (parent / "runtime_data" / "backtest_report.json",
                      parent / "data_cache" / "backtest_report.json"):
        if candidate.exists():
            path = candidate
            break
    else:
        raise HTTPException(status_code=404, detail="Backtest report not yet generated")
    try:
        import json as _json
        return _json.loads(path.read_text())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Report parse error: {e}")


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
    from pathlib import Path as _Path
    import csv as _csv
    since = (_dt.utcnow().date() - _td(days=lookback_days)).isoformat()

    rows = get_picks_history(conn, tier=tier, since_date=since, limit=2000)

    # Price source hierarchy:
    #   1. Most-recent Close from data_cache/prices/<SYM>.csv  (updated by the
    #      nightly yfinance refresh, freshest available)
    #   2. projection cache current_price                       (lagging, but
    #      always present for any symbol the engine has scanned)
    # Falling back on (2) alone caused realized-return values to stop updating
    # for tickers that weren't re-scanned in the current window.
    _prices_dir = _Path(__file__).parent / "data_cache" / "prices"
    price_cache: dict[str, float | None] = {}
    def _latest_price(sym: str):
        if sym in price_cache:
            return price_cache[sym]
        csv_path = _prices_dir / f"{sym}.csv"
        if csv_path.exists():
            try:
                with csv_path.open() as fh:
                    last = None
                    for row in _csv.DictReader(fh):
                        last = row
                    if last and last.get("Close"):
                        price_cache[sym] = float(last["Close"])
                        return price_cache[sym]
            except Exception:
                pass
        p = get_projection(conn, sym, 252)
        price = p.get("current_price") if p else None
        price_cache[sym] = price
        return price

    today = _dt.utcnow().date()
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
        # Days a pick has been held — lets the UI bucket by maturity.
        try:
            pd = _dt.fromisoformat(r["pick_date"][:10]).date()
            r["days_held"] = (today - pd).days
        except Exception:
            r["days_held"] = None

    # Summary by tier — only picks that had at least 7 days to play out.
    # "asymmetric" included for the live scoreboard; the backtest-level
    # teasers historically only used three tiers because the asymmetric
    # list is scored separately, but those picks go into picks_history
    # just the same and their live results belong on the scoreboard.
    cutoff = (_dt.utcnow().date() - _td(days=7)).isoformat()
    matured = [r for r in rows if r["pick_date"] <= cutoff and r["realized_return"] is not None]
    summary: dict = {}
    for tname in ("conservative", "moderate", "aggressive", "asymmetric"):
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

    # Aggregate scoreboard across all matured picks — the one-number
    # trust headline on /track-record.
    aggregate: dict = {"n": 0}
    if matured:
        rets = sorted(r["realized_return"] for r in matured)
        n = len(rets)
        median = rets[n // 2] if n % 2 else (rets[n // 2 - 1] + rets[n // 2]) / 2
        mean = sum(rets) / n
        hits = sum(1 for r in matured if r["realized_return"] > 0)
        earliest = min(r["pick_date"] for r in rows) if rows else None
        aggregate = {
            "n": n,
            "hit_rate": round(hits / n, 3),
            "mean_return": round(mean, 4),
            "median_return": round(median, 4),
            "total_picks_logged": len(rows),
            "earliest_pick_date": earliest,
            "as_of": today.isoformat(),
        }
    scoreboard = {"summary": summary, "aggregate": aggregate}

    # Pipeline events (outages, maintenance) over the same lookback window so
    # the UI can render gap markers instead of silently missing dates. Always
    # included — these aren't pick-level data and tell the same story to free
    # and paid viewers.
    events = get_pipeline_events(conn, since_date=since)

    if not is_strategist:
        # Aggregate + per-tier stats are OK to expose publicly — they are
        # proof-of-edge, not personalized recommendations. Pick-level detail
        # stays behind the paywall.
        return JSONResponse(
            status_code=402,
            content={
                "error": "strategist_required",
                "tier_required": "strategist",
                "summary": summary,
                "aggregate": aggregate,
                "events": events,
                "hint": "Full pick-by-pick track record is part of Strategist ($29/mo).",
            },
        )

    return {
        "lookback_days": lookback_days,
        "tier_filter": tier,
        "summary": summary,
        "aggregate": aggregate,
        "events": events,
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

    # Prevent intermediate caches (browser, Cloudflare, Fastly) from
    # returning a stale pick list after a refresh. Ran into this
    # 2026-04-18 when a user saw yesterday's rationales even though
    # Railway had already been redeployed with new data.
    no_cache = {
        "Cache-Control": "no-store, must-revalidate",
        "Pragma": "no-cache",
    }

    if not is_strategist:
        # Teaser: 3 per bucket, not results[:9] (which would be same-tier heavy).
        teaser: list[dict] = []
        for t in ("conservative", "moderate", "aggressive"):
            bucket = [p for p in results if p.get("tier") == t][:3]
            teaser.extend({"symbol": p["symbol"], "tier": t} for p in bucket)
        return JSONResponse(
            status_code=402,
            headers=no_cache,
            content={
                "error": "strategist_required",
                "tier_required": "strategist",
                "teaser": teaser,
                "scan_age_hours": round(scan_age, 1) if scan_age is not None else None,
                "hint": "Portfolio picks are part of the Strategist tier ($29/mo). Upgrade to unlock full ranked lists.",
            },
        )

    # Asymmetric picks live in the same JSON cache but are a separate 10-name
    # list with its own scoring (NN when winning, EWMA otherwise). The UI
    # renders them in a distinct tab, so we ship them in a distinct field.
    # Only included when no tier filter is applied — per-tier queries stay
    # focused on that tier's bucket.
    asym_picks = [] if tier else load_cached_asymmetric_picks()

    return JSONResponse(
        headers=no_cache,
        content={
            "scan_age_hours": round(scan_age, 1) if scan_age is not None else None,
            "tier_filter": tier,
            "count": len(results),
            "picks": results,
            "asymmetric_picks": asym_picks,
        },
    )


# ── Live paper-trading portfolio (Alpaca) ──

@app.get("/api/portfolio")
@limiter.limit("60/minute")
def portfolio(request: Request, user: Optional[dict] = Depends(auth.optional_user)):
    """Live actively-managed paper portfolio on Alpaca.

    Returns account equity, today's P&L, recent equity curve, and
    per-position state with sleeve attribution (swing vs daytrade)
    pulled from research/trader_state.json. Strategist-gated because
    it surfaces position-level data for paying users.

    Set ALPACA_API_KEY / ALPACA_API_SECRET / ALPACA_BASE_URL on the
    Railway service. Returns 503 if creds aren't configured (the
    /picks UI degrades to "portfolio not yet wired").
    """
    is_strategist = False
    if user:
        user_row = users_db.upsert_user(users_conn, user["user_id"], email=user.get("email"))
        is_strategist = users_db.can_access_picks(user_row)

    api_key = os.getenv("ALPACA_API_KEY")
    api_secret = os.getenv("ALPACA_API_SECRET")
    base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2").rstrip("/")
    if not api_key or not api_secret:
        return JSONResponse(status_code=503, content={
            "error": "alpaca_not_configured",
            "hint": "Set ALPACA_API_KEY/ALPACA_API_SECRET on the Railway service.",
        })

    import urllib.request as _ur, urllib.error as _ue
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret}
    def _alpaca(path: str):
        url = f"{base_url}/{path.lstrip('/')}"
        req = _ur.Request(url, headers=headers)
        with _ur.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    try:
        acct = _alpaca("account")
        positions = _alpaca("positions")
        # 1-week daily equity curve for the chart at the top of /picks
        hist = _alpaca("account/portfolio/history?period=1W&timeframe=1D")
    except (_ue.HTTPError, _ue.URLError, ValueError) as e:
        return JSONResponse(status_code=502, content={
            "error": "alpaca_upstream_error", "detail": str(e)[:200],
        })

    # Sleeve attribution from the local state file written by trader.py.
    # If the file isn't present (first deploy, no orders yet), positions
    # render under "unattributed" and the UI shows a friendly hint.
    state_path = Path(__file__).parent / "research" / "trader_state.json"
    entries: dict = {}
    if state_path.exists():
        try:
            entries = (json.loads(state_path.read_text()) or {}).get("entries", {})
        except Exception:
            entries = {}

    from datetime import datetime as _dt2
    now = _dt2.utcnow()

    pos_out = []
    for p in positions:
        sym = p["symbol"]
        ent = entries.get(sym) or {}
        opened_at = ent.get("opened_at")
        days_held = None
        if opened_at:
            try:
                d0 = _dt2.fromisoformat(opened_at.replace("Z", "+00:00")).replace(tzinfo=None)
                days = (now.date() - d0.date()).days
                wd = sum(1 for i in range(max(0, days))
                         if (d0.date() + (now.date() - d0.date())).weekday() < 5)
                days_held = max(0, days)  # calendar days; UI can label "days"
            except Exception:
                days_held = None
        pos_out.append({
            "symbol": sym,
            "qty": p["qty"], "side": p["side"],
            "avg_entry_price": float(p["avg_entry_price"]),
            "current_price": float(p["current_price"]),
            "market_value": float(p["market_value"]),
            "cost_basis": float(p["cost_basis"]),
            "unrealized_pl": float(p["unrealized_pl"]),
            "unrealized_plpc": float(p["unrealized_plpc"]),
            "change_today_pct": float(p.get("change_today", 0)) if p.get("change_today") else None,
            "sleeve": ent.get("sleeve") or "unattributed",
            "opened_at": opened_at,
            "days_held": days_held,
        })
    pos_out.sort(key=lambda x: x["unrealized_pl"], reverse=True)

    eq = float(acct["equity"])
    last_eq = float(acct.get("last_equity") or 0) or eq
    day_change = eq - last_eq
    day_change_pct = (day_change / last_eq) if last_eq else 0.0

    # Sleeve-level attribution: total P&L grouped by sleeve.
    sleeve_summary: dict = {"swing": {"n": 0, "mv": 0.0, "upnl": 0.0},
                            "daytrade": {"n": 0, "mv": 0.0, "upnl": 0.0},
                            "unattributed": {"n": 0, "mv": 0.0, "upnl": 0.0}}
    for p in pos_out:
        s = p["sleeve"] if p["sleeve"] in sleeve_summary else "unattributed"
        sleeve_summary[s]["n"] += 1
        sleeve_summary[s]["mv"] += p["market_value"]
        sleeve_summary[s]["upnl"] += p["unrealized_pl"]

    payload = {
        "is_strategist": is_strategist,
        "as_of": now.isoformat() + "Z",
        "account": {
            "equity": eq, "cash": float(acct["cash"]),
            "buying_power": float(acct["buying_power"]),
            "last_equity": last_eq,
            "day_change": day_change, "day_change_pct": day_change_pct,
            "multiplier": acct.get("multiplier"),
            "currency": acct.get("currency", "USD"),
            "is_paper": "paper" in base_url,
        },
        "equity_history": {
            "timestamps": hist.get("timestamp", []),
            "equity": hist.get("equity", []),
            "profit_loss": hist.get("profit_loss", []),
        },
        "positions": pos_out if is_strategist else pos_out[:3],  # teaser when not paid
        "sleeves": sleeve_summary,
        "strategy_doc": "swing=ranks 1-10, 5-day hold, 1.5× lev. daytrade=ranks 11-20, intraday only, 1× lev.",
    }
    if not is_strategist:
        payload["teaser"] = True
        payload["hint"] = "Position-level detail unlocks at Strategist tier."
    return JSONResponse(headers={"Cache-Control": "no-store"}, content=payload)


# ── User + billing ──

@app.get("/api/me")
def api_me(user: Optional[dict] = Depends(auth.optional_user)):
    """Return current user context. Anonymous when unauthed."""
    if not user:
        return {"authenticated": False, "tier": "anonymous"}
    user_row = users_db.upsert_user(users_conn, user["user_id"], email=user.get("email"))

    # Self-heal after a DB wipe. If this user has an email but no Stripe
    # link yet AND no record of a prior check, look them up in Stripe
    # once. If they were a paying customer before the wipe, their tier
    # gets restored automatically. If they never paid, we record a
    # sentinel so we don't re-query on every subsequent /api/me.
    needs_resync = (
        user.get("email")
        and not user_row.get("stripe_customer_id")
        and not user_row.get("subscription_status")
        and users_db.effective_tier(user_row) == "free"
    )
    if needs_resync:
        try:
            billing.resync_by_email(users_conn, user["user_id"], user["email"])
            user_row = users_db.get_user(users_conn, user["user_id"])
        except Exception as e:
            log.warning("resync_by_email failed for %s: %s", user["user_id"], e)

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
