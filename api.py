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


# ── Trade-journal fetch with 60s cache ────────────────────────────────────
# The journal lives at runtime_data/trade_journal.json on disk inside the
# Docker image. The trader workflow commits updates to main but Railway
# only redeploys via the fast/slow pipelines — so without this hop the API
# serves the journal as it was at last `railway up`, sometimes days stale.
# Resolution: fetch the file from GitHub raw on every request, cached 60s
# in-process. Falls back to the baked-in local file when GitHub is down.
_TRADE_JOURNAL_RAW = (
    "https://raw.githubusercontent.com/jgmynott/s-tool-projector/main/"
    "runtime_data/trade_journal.json"
)
_TRADE_JOURNAL_CACHE_TTL_S = 60
_trade_journal_cache: dict = {"ts": 0.0, "rows": None}


def _fetch_trade_journal_rows():
    """Return list of journal rows, or [] if no journal exists, or None for a
    hard parse failure. Cached 60s in-process."""
    import time as _tjt, urllib.request as _tju, urllib.error as _tje
    import json as _tjj
    from pathlib import Path as _TJPath

    if _tjt.time() - _trade_journal_cache["ts"] < _TRADE_JOURNAL_CACHE_TTL_S \
            and _trade_journal_cache["rows"] is not None:
        return _trade_journal_cache["rows"]

    rows = None
    try:
        req = _tju.Request(_TRADE_JOURNAL_RAW, headers={"Cache-Control": "no-cache"})
        with _tju.urlopen(req, timeout=4) as r:
            data = _tjj.loads(r.read().decode("utf-8", "replace"))
        rows = data.get("rows") if isinstance(data, dict) else (data if isinstance(data, list) else [])
    except (_tje.URLError, _tje.HTTPError, _tjj.JSONDecodeError, TimeoutError, OSError):
        rows = None

    if rows is None:
        # GitHub unreachable — fall back to whatever shipped in the container.
        path = _TJPath(__file__).parent / "runtime_data" / "trade_journal.json"
        if not path.exists():
            return []
        try:
            data = _tjj.loads(path.read_text())
            rows = data.get("rows") if isinstance(data, dict) else (data if isinstance(data, list) else [])
        except Exception:
            return []

    _trade_journal_cache["ts"] = _tjt.time()
    _trade_journal_cache["rows"] = rows
    return rows
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

    # Fallback: production Railway DB starts empty on every redeploy
    # (projector_cache.db lives in GH Actions cache, not in the railway
    # up tarball). The nightly fast/slow workflows write a snapshot of
    # picks_history to runtime_data/picks_history.json that ships with
    # the deploy. Use it when the DB has nothing to say.
    if not rows:
        ph_path = _Path(__file__).parent / "runtime_data" / "picks_history.json"
        if ph_path.exists():
            try:
                import json as _json3
                snap = _json3.loads(ph_path.read_text())
                snap_rows = snap.get("rows") or []
                # Filter by tier + since_date the same way get_picks_history does.
                rows = [
                    r for r in snap_rows
                    if r.get("pick_date") and r["pick_date"] >= since
                       and (tier is None or r.get("tier") == tier)
                ]
            except Exception:
                rows = []

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
        # proof-of-edge, not personalized recommendations. We also surface
        # a *teaser* row list with limited fields (symbol, tier, entry,
        # realized) so the public track-record page can render a
        # last-30-days picks table — that's the credibility moment that
        # converts free → paid. Rationale, sec_fundamentals, and
        # expected_return stay behind the paywall.
        public_fields = (
            "pick_date", "symbol", "tier", "entry_price", "p50_target",
            "current_price", "realized_return", "toward_target_pct", "days_held",
        )
        teaser_rows = [
            {k: r.get(k) for k in public_fields}
            for r in rows
            if (r.get("days_held") or 0) >= 1 and r.get("realized_return") is not None
        ][:60]
        return JSONResponse(
            status_code=402,
            content={
                "error": "strategist_required",
                "tier_required": "strategist",
                "summary": summary,
                "aggregate": aggregate,
                "rows": teaser_rows,
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
        # Mirror under "rows" so the public + paid frontends share a key
        # path; renderRecentPicksTable on /track-record reads live.rows.
        "rows": rows,
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

_PORTFOLIO_CACHE: dict = {}  # { is_strategist: (epoch_seconds, payload_dict) }
_PORTFOLIO_CACHE_TTL = 5.0   # seconds — short enough to feel live, long
                             # enough to absorb a burst of /picks refreshes


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

    Response is micro-cached for 5s per (is_strategist) bucket — the
    fast path now serves cached bytes during refresh bursts instead
    of fanning out 7 Alpaca calls + 2 SPY fetches on every poll.
    """
    is_strategist = False
    if user:
        user_row = users_db.upsert_user(users_conn, user["user_id"], email=user.get("email"))
        is_strategist = users_db.can_access_picks(user_row)

    # Cache check — if a recent payload exists for this tier, serve it.
    # The response is a fully-rendered dict; we only need to wrap it in
    # the same JSONResponse with no-store so the BROWSER doesn't cache
    # (we want the cache hit to be the only one, not a stale tab).
    import time as _t_pf
    _now_pf = _t_pf.time()
    _hit = _PORTFOLIO_CACHE.get(is_strategist)
    if _hit and (_now_pf - _hit[0]) < _PORTFOLIO_CACHE_TTL:
        return JSONResponse(headers={"Cache-Control": "no-store", "X-Cache": "HIT"}, content=_hit[1])

    api_key = os.getenv("ALPACA_API_KEY")
    api_secret = os.getenv("ALPACA_API_SECRET")
    base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2").rstrip("/")
    if not api_key or not api_secret:
        return JSONResponse(status_code=503, content={
            "error": "alpaca_not_configured",
            "hint": "Set ALPACA_API_KEY/ALPACA_API_SECRET on the Railway service.",
        })

    import urllib.request as _ur, urllib.error as _ue
    import json as _json
    from concurrent.futures import ThreadPoolExecutor
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret}
    def _alpaca(path: str):
        url = f"{base_url}/{path.lstrip('/')}"
        req = _ur.Request(url, headers=headers)
        with _ur.urlopen(req, timeout=10) as r:
            return _json.loads(r.read())
    def _alpaca_data(path: str):
        """Hits the market-data API (data.alpaca.markets) — same creds, same
        headers. Used for the SPY benchmarking and any other quote/bars
        lookup we want server-side."""
        url = f"https://data.alpaca.markets/v2/{path.lstrip('/')}"
        req = _ur.Request(url, headers=headers)
        with _ur.urlopen(req, timeout=10) as r:
            return _json.loads(r.read())
    def _alpaca_safe(path: str, default):
        """Best-effort fetch; returns default on any error so one slow or
        unavailable Alpaca endpoint doesn't crater the whole panel."""
        try:
            return _alpaca(path)
        except (_ue.HTTPError, _ue.URLError, ValueError):
            return default

    from datetime import datetime as _dt_local
    today_iso = _dt_local.utcnow().date().isoformat()

    # Fan out the 7 Alpaca reads in parallel — sequential they cost ~2.5s
    # which is brutal on a 30-second auto-refresh. account + positions are
    # mandatory; everything else degrades to defaults if it errors.
    fan_paths = {
        "acct":        ("account", None),
        "positions":   ("positions", None),
        "hist":        ("account/portfolio/history?period=1M&timeframe=1D", {}),
        "intraday":    ("account/portfolio/history?period=1D&timeframe=15Min&extended_hours=true", {}),
        # 500 covers a heavy daytrade + scalper day with multiple entries
        # per symbol. At limit=200 we were rolling off attribution for any
        # symbol that fully cycled (bought + sold) today when concurrent
        # fills pushed the parent buy out of the window — every closed-
        # today row then read "unattributed" even though we generated the
        # CID. Bumping the cap is cheap (one Alpaca call, parallel-fetched).
        "orders":      ("orders?status=closed&direction=desc&limit=500&side=buy", []),
        "open_orders": ("orders?status=open&direction=desc&limit=100", []),
        "activities":  (f"account/activities/FILL?date={today_iso}", []),
        # Redundant SELL source for closed_today. The activities/FILL feed
        # has historically returned empty for some accounts mid-session
        # (Alpaca lag — activities pipeline batches behind orders).
        # Caught 2026-04-29 when Alpaca order panel showed 4+ filled
        # bracket SELLs but /api/portfolio.closed_today stayed at 0.
        # Pulling closed SELL orders directly gives us a same-second
        # source that's never empty when sells have actually filled.
        "sell_orders": ("orders?status=closed&direction=desc&limit=200&side=sell", []),
    }
    # SPY daily bars over the last ~30 trading days. Used to compute the
    # "live alpha" stat — paper account return vs SPY over the window
    # the trader has been live. Pulled from data.alpaca.markets so the
    # same creds work; degrades to {} if the data API is down.
    def _spy_safe():
        from datetime import timedelta as _td_local
        start = (_dt_local.utcnow().date() - _td_local(days=45)).isoformat()
        try:
            return _alpaca_data(f"stocks/SPY/bars?timeframe=1Day&start={start}&limit=60")
        except (_ue.HTTPError, _ue.URLError, ValueError):
            return {}

    # SPY intraday 15Min bars for today only — feeds the 1D view of the
    # equity chart so users see SPY alongside the trader on the same time
    # axis. RTH only (Alpaca defaults to non-extended for stocks/bars,
    # which is what we want here — pre-market noise would dwarf the trader
    # signal). Bars run 13:30-20:00 UTC = 9:30-16:00 ET on a normal day.
    def _spy_intraday_safe():
        try:
            start = today_iso  # current UTC date; bars before 13:30 don't exist on RTH-only
            return _alpaca_data(f"stocks/SPY/bars?timeframe=15Min&start={start}&limit=200")
        except (_ue.HTTPError, _ue.URLError, ValueError):
            return {}

    with ThreadPoolExecutor(max_workers=len(fan_paths) + 2) as ex:
        futs = {k: ex.submit(_alpaca_safe if default is not None else _alpaca, path,
                             *( (default,) if default is not None else () ))
                for k, (path, default) in fan_paths.items()}
        futs["spy"] = ex.submit(_spy_safe)
        futs["spy_intra"] = ex.submit(_spy_intraday_safe)
        try:
            acct = futs["acct"].result()
            positions = futs["positions"].result()
        except (_ue.HTTPError, _ue.URLError, ValueError) as e:
            return JSONResponse(status_code=502, content={
                "error": "alpaca_upstream_error", "detail": str(e)[:200],
            })
        hist = futs["hist"].result()
        intraday = futs["intraday"].result()
        orders = futs["orders"].result()
        open_orders = futs["open_orders"].result()
        activities = futs["activities"].result()
        sell_orders = futs["sell_orders"].result()
        spy_bars = futs["spy"].result()
        spy_intra_bars = futs["spy_intra"].result()

    # Synthesize FILL-shaped rows from closed SELL + BUY orders that filled
    # today. The activities/FILL endpoint paginates (~100 most recent entries)
    # so on a heavy trading day the morning BUYs roll off and only afternoon
    # SELLs remain — leaves the FIFO with sells but no lots to pair, so every
    # cross-day-bought-and-sold-today symbol falls through with buy_price=null.
    # Caught 2026-04-29: 34 merged_fills, but closed_today rows had pnl=None
    # because today's morning buys were out of the activities window. Fix:
    # also synthesize buys from the closed-orders feed (limit=500, today only).
    def _synth_from_order(o, side):
        if (o.get("status") or "").lower() != "filled":
            return None
        filled_at = o.get("filled_at") or ""
        if not filled_at.startswith(today_iso):
            return None
        try:
            qty = float(o.get("filled_qty") or o.get("qty") or 0)
            price = float(o.get("filled_avg_price") or 0)
        except (TypeError, ValueError):
            return None
        if qty <= 0 or price <= 0:
            return None
        return {
            "type": "FILL",
            "side": side,
            "symbol": o.get("symbol"),
            "qty": qty,
            "price": price,
            "transaction_time": filled_at,
            "order_id": o.get("id"),
        }
    today_synth_sells: list[dict] = []
    for o in (sell_orders or []):
        row = _synth_from_order(o, "sell")
        if row: today_synth_sells.append(row)
    today_synth_buys: list[dict] = []
    for o in (orders or []):
        row = _synth_from_order(o, "buy")
        if row: today_synth_buys.append(row)

    # Build sym → {sleeve, opened_at} from the most recent FILLED buy per
    # symbol. We walk newest-first so the first hit wins — that's the
    # current open lot. Bracket parents have order_class="bracket"; the
    # auto-generated stop/target legs are children with parent_order_id
    # set, so we filter on the parent.
    #
    # Also build cid_by_order_id so the closed-today FILL loop can resolve
    # sleeve via the activity's exact order_id rather than relying on
    # symbol-level "most-recent buy wins" — a symbol that cycled twice
    # today (e.g. daytrade rotator) would otherwise misattribute the
    # earlier close to the later buy's sleeve.
    sleeve_by_sym: dict = {}
    cid_by_order_id: dict = {}
    SLEEVE_PREFIXES = {"momentum", "swing", "daytrade", "scalper"}
    def _parse_sleeve_from_cid(cid: str) -> str | None:
        if not cid:
            return None
        parts = cid.split("-")
        if len(parts) >= 2 and parts[0] in SLEEVE_PREFIXES:
            return parts[0]
        return None
    for o in (orders or []):
        sym = o.get("symbol")
        cid = o.get("client_order_id") or ""
        oid = o.get("id")
        if oid and cid:
            cid_by_order_id[oid] = cid
        if not sym or not cid or sym in sleeve_by_sym:
            continue
        if o.get("status") != "filled":
            continue
        if o.get("parent_order_id"):
            continue  # skip bracket legs
        sleeve = _parse_sleeve_from_cid(cid)
        if sleeve:
            sleeve_by_sym[sym] = {
                "sleeve": sleeve,
                "opened_at": o.get("filled_at") or o.get("submitted_at"),
                "ref_price": float(o.get("filled_avg_price") or 0) or None,
            }

    # Fallback: legacy trader_state.json deploy path. Only used for
    # positions that don't have a parsable client_order_id (e.g.
    # pre-Apr-26 orders submitted before the client_order_id encoding).
    state_path = Path(__file__).parent / "research" / "trader_state.json"
    legacy_entries: dict = {}
    if state_path.exists():
        try:
            legacy_entries = (_json.loads(state_path.read_text()) or {}).get("entries", {})
        except Exception:
            legacy_entries = {}

    # Build sym → source_tier map from the daily picks scan. Asymmetric
    # picks are checked first so a name appearing in both lists is tagged
    # "asymmetric" (the more specific signal). Lets the live position card
    # show users which section of /picks the position originated from —
    # ties the live execution back to the published thesis.
    tier_by_sym: dict = {}
    picks_path = Path(__file__).parent / "portfolio_picks.json"
    if picks_path.exists():
        try:
            picks_data = _json.loads(picks_path.read_text()) or {}
            for ap in (picks_data.get("asymmetric_picks") or []):
                s = ap.get("symbol")
                if s:
                    tier_by_sym[s] = "asymmetric"
            for pk in (picks_data.get("picks") or []):
                s = pk.get("symbol")
                t = pk.get("tier")
                if s and t and s not in tier_by_sym:
                    tier_by_sym[s] = t
        except Exception:
            tier_by_sym = {}

    from datetime import datetime as _dt2
    now = _dt2.utcnow()

    # Bracket levels per sleeve. Mirror SLEEVES config from research/trader.py
    # — keep these in sync with that file or the panel display will diverge
    # from the actual orders sitting at Alpaca.
    SLEEVE_BRACKETS = {
        "momentum": {"stop_pct": -0.05, "target_pct": +0.10},
        "swing":    {"stop_pct": -0.07, "target_pct": +0.15},
        "daytrade": {"stop_pct": -0.03, "target_pct": +0.05},
    }

    pos_out = []
    for p in positions:
        sym = p["symbol"]
        meta = sleeve_by_sym.get(sym) or legacy_entries.get(sym) or {}
        opened_at = meta.get("opened_at")
        sleeve = meta.get("sleeve") or "unattributed"
        days_held = None
        if opened_at:
            try:
                d0 = _dt2.fromisoformat(opened_at.replace("Z", "+00:00")).replace(tzinfo=None)
                days_held = max(0, (now.date() - d0.date()).days)
            except Exception:
                days_held = None
        avg_entry = float(p["avg_entry_price"])
        bracket_cfg = SLEEVE_BRACKETS.get(sleeve)
        stop_price = round(avg_entry * (1 + bracket_cfg["stop_pct"]), 2) if bracket_cfg else None
        target_price = round(avg_entry * (1 + bracket_cfg["target_pct"]), 2) if bracket_cfg else None
        pos_out.append({
            "symbol": sym,
            "qty": p["qty"], "side": p["side"],
            "avg_entry_price": avg_entry,
            "current_price": float(p["current_price"]),
            "market_value": float(p["market_value"]),
            "cost_basis": float(p["cost_basis"]),
            "unrealized_pl": float(p["unrealized_pl"]),
            "unrealized_plpc": float(p["unrealized_plpc"]),
            "change_today_pct": float(p.get("change_today", 0)) if p.get("change_today") else None,
            "sleeve": sleeve,
            "source_tier": tier_by_sym.get(sym),
            "stop_price": stop_price,
            "target_price": target_price,
            "opened_at": opened_at,
            "days_held": days_held,
        })
    pos_out.sort(key=lambda x: x["unrealized_pl"], reverse=True)

    # ── Realized P&L for today, FIFO-paired from FILL activities.
    # Each activity row has side (buy/sell), symbol, qty, price. Walk
    # in chronological order so daytrade entries pair with their EOD
    # exits cleanly. Per-symbol FIFO queue of buy lots; each sell pops
    # from the queue and accumulates realized P&L.
    closed_today: list[dict] = []
    realized_today_total = 0.0
    realized_by_sleeve: dict = {"momentum": 0.0, "swing": 0.0, "daytrade": 0.0, "scalper": 0.0, "unattributed": 0.0}
    # Build the unified fill stream: real Alpaca FILL activities + synthesized
    # FILLs from closed BUY/SELL orders. Dedupe by (symbol, side, qty, time-trunc)
    # so a fill that exists in both feeds doesn't double-book.
    real_fills = [a for a in (activities or []) if a.get("type") in ("FILL", "PARTIAL_FILL")]
    seen_fill_keys = set()
    for a in real_fills:
        sym = a.get("symbol")
        try:
            q = float(a.get("qty") or 0)
        except (TypeError, ValueError):
            q = 0
        side_a = (a.get("side") or "").lower()
        ts_a = (a.get("transaction_time") or "")[:19]   # second-precision
        if sym and side_a in ("buy", "sell") and q > 0 and ts_a:
            seen_fill_keys.add((sym, side_a, round(q, 2), ts_a))
    merged_fills = list(real_fills)
    for synth_list in (today_synth_buys, today_synth_sells):
        for s in synth_list:
            key = (s.get("symbol"), (s.get("side") or "").lower(),
                   round(float(s.get("qty") or 0), 2),
                   (s.get("transaction_time") or "")[:19])
            if key in seen_fill_keys:
                continue
            seen_fill_keys.add(key)
            merged_fills.append(s)
    if merged_fills:
        # Activities come back newest-first; oldest-first is the natural
        # FIFO direction.
        chrono = sorted(
            merged_fills,
            key=lambda a: a.get("transaction_time") or a.get("submitted_at") or "",
        )
        buy_lots: dict = {}  # sym → list[(qty, price, sleeve)]
        for a in chrono:
            sym = a.get("symbol")
            if not sym:
                continue
            try:
                qty = float(a.get("qty") or 0)
                price = float(a.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if qty <= 0 or price <= 0:
                continue
            side = (a.get("side") or "").lower()
            if side == "buy":
                # Resolution cascade — most precise first:
                #   1. Activity's own order_id → that order's CID prefix.
                #      Guarantees the right sleeve for THIS specific buy
                #      even when a symbol cycled multiple times today.
                #   2. sleeve_by_sym (most-recent-buy-wins) — works when
                #      the order_id lookup misses (e.g. older parent order
                #      rolled out of the orders window).
                #   3. legacy_entries from trader_state.json — covers
                #      pre-CID-encoding fills the trader is still tracking.
                #   4. "unattributed" — true legacy / manual. The UI labels
                #      these "Manual / legacy" with a hover explanation
                #      rather than the opaque "UNATTRIBUTED" badge.
                sleeve = (
                    _parse_sleeve_from_cid(cid_by_order_id.get(a.get("order_id"), ""))
                    or (sleeve_by_sym.get(sym) or {}).get("sleeve")
                    or (legacy_entries.get(sym) or {}).get("sleeve")
                    or "unattributed"
                )
                buy_lots.setdefault(sym, []).append([qty, price, sleeve])
            elif side == "sell":
                remaining = qty
                lots = buy_lots.get(sym, [])
                while remaining > 0 and lots:
                    lot_qty, lot_price, lot_sleeve = lots[0]
                    take = min(lot_qty, remaining)
                    pnl = (price - lot_price) * take
                    realized_today_total += pnl
                    realized_by_sleeve[lot_sleeve if lot_sleeve in realized_by_sleeve else "unattributed"] += pnl
                    closed_today.append({
                        "symbol": sym, "qty": take,
                        "buy_price": lot_price, "sell_price": price,
                        "pnl": pnl, "sleeve": lot_sleeve,
                        "source_tier": tier_by_sym.get(sym),
                        "closed_at": a.get("transaction_time"),
                    })
                    lot_qty -= take
                    remaining -= take
                    if lot_qty <= 0:
                        lots.pop(0)
                    else:
                        lots[0][0] = lot_qty
                # Cross-day fallback: SELL had no same-day buy lot left
                # (entry was a prior day — typical for swing exits or any
                # bracket that fires before today's entry has filled into
                # activities). Reach into trader_state.entries for the
                # original buy_price so the close still surfaces in the
                # live ticker. Without this, intraday bracket exits stay
                # invisible until trader.py's EOD journal_alpaca_fills()
                # at 19:25 UTC, which left a 6+ hour blind spot.
                if remaining > 0:
                    legacy = legacy_entries.get(sym) or {}
                    legacy_buy = legacy.get("buy_price")
                    legacy_sleeve = (
                        legacy.get("sleeve")
                        or (sleeve_by_sym.get(sym) or {}).get("sleeve")
                        or "unattributed"
                    )
                    sleeve_key = legacy_sleeve if legacy_sleeve in realized_by_sleeve else "unattributed"
                    pnl_val = None
                    if legacy_buy:
                        try:
                            pnl_val = (price - float(legacy_buy)) * remaining
                            realized_today_total += pnl_val
                            realized_by_sleeve[sleeve_key] += pnl_val
                        except (TypeError, ValueError):
                            pnl_val = None
                    closed_today.append({
                        "symbol": sym, "qty": remaining,
                        "buy_price": float(legacy_buy) if legacy_buy else None,
                        "sell_price": price,
                        "pnl": pnl_val,
                        "sleeve": legacy_sleeve,
                        "source_tier": tier_by_sym.get(sym),
                        "closed_at": a.get("transaction_time"),
                    })
    # Backfill closes from the trade journal for any sell that today's
    # FIFO-from-today-buys logic couldn't pair (because the buy happened
    # on a prior day — typical for circuit-breaker liquidations or swing
    # exits). The journal already has pnl computed via cross-day FIFO,
    # so we just lift those rows whose ts.date == today and aren't
    # already represented in closed_today by symbol.
    try:
        from datetime import datetime as _dt_close, timezone as _tz_close
        journal_rows = _fetch_trade_journal_rows() or []
        today_iso = _dt_close.now(_tz_close.utc).date().isoformat()
        seen_symbols = {(r["symbol"], r["qty"]) for r in closed_today}
        for jr in journal_rows:
            if jr.get("event") != "sell":
                continue
            ts = jr.get("ts") or ""
            if not ts.startswith(today_iso):
                continue
            pnl = jr.get("pnl")
            if pnl is None:
                continue
            sym = jr.get("symbol")
            qty = float(jr.get("qty") or 0)
            if (sym, qty) in seen_symbols:
                continue
            # Same cascade as the live activity loop. The journal already
            # carries sleeve for any trade the engine wrote, but a SELL
            # whose entry pre-dated the sleeve-encoding rollout (early
            # Apr-26) lands here with sleeve == "unattributed" literal —
            # try sleeve_by_sym and legacy_entries before accepting it.
            jr_sleeve = jr.get("sleeve")
            if jr_sleeve and jr_sleeve != "unattributed":
                sleeve = jr_sleeve
            else:
                sleeve = (
                    (sleeve_by_sym.get(sym) or {}).get("sleeve")
                    or (legacy_entries.get(sym) or {}).get("sleeve")
                    or "unattributed"
                )
            sleeve_key = sleeve if sleeve in realized_by_sleeve else "unattributed"
            realized_today_total += float(pnl)
            realized_by_sleeve[sleeve_key] += float(pnl)
            closed_today.append({
                "symbol": sym, "qty": qty,
                "buy_price": jr.get("buy_price"),
                "sell_price": jr.get("sell_price"),
                "pnl": float(pnl),
                "sleeve": sleeve,
                "source_tier": jr.get("tier") or tier_by_sym.get(sym),
                "closed_at": ts,
            })
            seen_symbols.add((sym, qty))
    except Exception as e:
        print(f"closed_today journal backfill failed: {e}")
    # Most recent close first for display.
    closed_today.sort(key=lambda r: r.get("closed_at") or "", reverse=True)

    eq = float(acct["equity"])
    last_eq = float(acct.get("last_equity") or 0) or eq
    day_change = eq - last_eq
    day_change_pct = (day_change / last_eq) if last_eq else 0.0

    # Make today's last daily point land on today's calendar date in the
    # viewer's local timezone. Alpaca's portfolio/history?timeframe=1D returns
    # a bar for today at UTC midnight — but UTC midnight 2026-04-28 is
    # 2026-04-27 20:00 EDT, so the frontend's toLocaleDateString renders the
    # last tick as "Apr 27" (yesterday) for any viewer west of UTC. The 1D
    # intraday chart doesn't have this issue (its timestamps are mid-day).
    # Fix: stamp today's bar at "now" instead of UTC midnight; equity/PL are
    # already current. If Alpaca hasn't included today yet, append.
    #
    # Use timezone-aware datetimes throughout — naive datetime.timestamp()
    # silently uses LOCAL time, which on a non-UTC Railway container produces
    # the wrong unix seconds and the date comparison below would never match.
    import time as _time_chart
    from datetime import datetime as _dt_chart, timezone as _tz_chart
    _now_aware = _dt_chart.now(_tz_chart.utc)
    today_utc_date = _now_aware.date()
    now_ts = int(_time_chart.time())
    hist_ts = list(hist.get("timestamp") or [])
    hist_eq = list(hist.get("equity") or [])
    hist_pl = list(hist.get("profit_loss") or [])
    if hist_ts:
        last_utc_date = _dt_chart.fromtimestamp(hist_ts[-1], tz=_tz_chart.utc).date()
        if last_utc_date == today_utc_date:
            hist_ts[-1] = now_ts
            hist_eq[-1] = eq
            hist_pl[-1] = float(day_change)
        else:
            hist_ts.append(now_ts)
            hist_eq.append(eq)
            hist_pl.append(float(day_change))
    else:
        hist_ts.append(now_ts)
        hist_eq.append(eq)
        hist_pl.append(float(day_change))
    hist = {"timestamp": hist_ts, "equity": hist_eq, "profit_loss": hist_pl}

    # Live alpha vs SPY: trader return − SPY return *over the same
    # window*. Naive "first non-zero equity" anchoring breaks when the
    # account sat idle at $100k for weeks before the trader fired —
    # eq_baseline ≈ eq_now → trader_return ≈ 0, but SPY's 30-day window
    # is 30 days long, so alpha looks like −SPY_return. Wrong window
    # alignment.
    #
    # Fix: anchor on the first day where equity *actually moved* (first
    # day the trader was live, not the first day the account had funds).
    # Then truncate SPY bars to that same date range.
    live_alpha = None
    spy_return = None
    trader_return = None
    benchmark_first_day = None
    try:
        eq_series = hist.get("equity") or []
        ts_series = hist.get("timestamp") or []
        # Find first day where equity diverged > $5 from the day-before
        # baseline. $5 (5 bps on $100k) is below normal jitter so we
        # don't lose precision but well above any rounding noise.
        first_active_idx = None
        if len(eq_series) >= 2:
            baseline_seed = eq_series[0]
            for i in range(1, len(eq_series)):
                v = eq_series[i]
                if v and abs(v - baseline_seed) > 5.0:
                    first_active_idx = i - 1  # use the day BEFORE first move as the entry point
                    break
        if first_active_idx is not None:
            eq_anchor = eq_series[first_active_idx] or eq_series[-1]
            eq_now = eq_series[-1]
            if eq_anchor and eq_now:
                trader_return = (eq_now - eq_anchor) / eq_anchor
            # Pin SPY to the same anchor date
            try:
                from datetime import datetime as _dt_a
                anchor_ts = ts_series[first_active_idx]
                benchmark_first_day = _dt_a.utcfromtimestamp(anchor_ts).date().isoformat()
            except Exception:
                benchmark_first_day = None
        else:
            # Daily history is flat — trader started fresh today, so
            # daily snapshots haven't captured movement yet. Fall back
            # to intraday equity to surface a meaningful day-1 number;
            # SPY uses today's session-open as the matching anchor.
            intra_eq = intraday.get("equity") or []
            intra_ts = intraday.get("timestamp") or []
            if len(intra_eq) >= 2:
                baseline_seed = intra_eq[0]
                eq_now = intra_eq[-1]
                if baseline_seed and eq_now and abs(eq_now - baseline_seed) > 5.0:
                    trader_return = (eq_now - baseline_seed) / baseline_seed
                    if intra_ts:
                        try:
                            from datetime import datetime as _dt_a
                            anchor_ts = intra_ts[0]
                            benchmark_first_day = _dt_a.utcfromtimestamp(anchor_ts).date().isoformat()
                        except Exception:
                            pass

        bars = (spy_bars or {}).get("bars") or []
        if bars and benchmark_first_day:
            # Filter SPY bars to those at-or-after the trader's anchor date
            aligned = [b for b in bars if (b.get("t") or "")[:10] >= benchmark_first_day]
            if len(aligned) >= 2:
                spy_first = float(aligned[0].get("c") or 0)
                spy_last = float(aligned[-1].get("c") or 0)
                if spy_first and spy_last:
                    spy_return = (spy_last - spy_first) / spy_first

        if trader_return is not None and spy_return is not None:
            live_alpha = trader_return - spy_return
    except Exception:
        pass

    # Sleeve-level attribution: total P&L grouped by sleeve.
    sleeve_summary: dict = {"momentum": {"n": 0, "mv": 0.0, "upnl": 0.0, "realized_today": 0.0},
                            "swing": {"n": 0, "mv": 0.0, "upnl": 0.0, "realized_today": 0.0},
                            "daytrade": {"n": 0, "mv": 0.0, "upnl": 0.0, "realized_today": 0.0},
                            "scalper": {"n": 0, "mv": 0.0, "upnl": 0.0, "realized_today": 0.0},
                            "unattributed": {"n": 0, "mv": 0.0, "upnl": 0.0, "realized_today": 0.0}}
    for p in pos_out:
        s = p["sleeve"] if p["sleeve"] in sleeve_summary else "unattributed"
        sleeve_summary[s]["n"] += 1
        sleeve_summary[s]["mv"] += p["market_value"]
        sleeve_summary[s]["upnl"] += p["unrealized_pl"]
    for s, pnl in realized_by_sleeve.items():
        if s in sleeve_summary:
            sleeve_summary[s]["realized_today"] = pnl

    # Build a SPY close-series aligned to equity_history.timestamps so the
    # frontend chart can overlay SPY as a comparison line without a second
    # network round-trip. spy_bars is daily; equity_history is also daily —
    # match by ISO date and forward-fill weekends/holidays where Alpaca's
    # SPY history skips a day. Returns parallel arrays so chart code can
    # zip them by index.
    spy_history_aligned: list = []
    try:
        bars_all = (spy_bars or {}).get("bars") or []
        spy_by_date: dict = {}
        for b in bars_all:
            t = b.get("t") or ""
            c = b.get("c")
            if t and c is not None:
                spy_by_date[t[:10]] = float(c)
        if spy_by_date and hist.get("timestamp"):
            from datetime import datetime as _dt_spy
            last_close = None
            for ts in (hist.get("timestamp") or []):
                try:
                    iso = _dt_spy.utcfromtimestamp(ts).date().isoformat()
                except Exception:
                    spy_history_aligned.append(None)
                    continue
                v = spy_by_date.get(iso)
                if v is None:
                    # forward-fill weekends/holidays from the prior known close
                    spy_history_aligned.append(last_close)
                else:
                    last_close = v
                    spy_history_aligned.append(v)
    except Exception:
        spy_history_aligned = []

    # SPY intraday alignment — same idea but over today's 15-min bars so
    # the 1D chart can render SPY too. equity_intraday timestamps are unix
    # seconds at 15-min boundaries; SPY bars come in as ISO8601. Bucket
    # SPY by minute-of-day, then for each equity ts pull the matching SPY
    # close (forward-fill across the rare gap where Alpaca skips a bar).
    spy_intraday_aligned: list = []
    try:
        ibars = (spy_intra_bars or {}).get("bars") or []
        if ibars and intraday.get("timestamp"):
            from datetime import datetime as _dt_si
            spy_by_minute: dict = {}
            for b in ibars:
                t = b.get("t") or ""
                c = b.get("c")
                if not t or c is None:
                    continue
                try:
                    dt_b = _dt_si.fromisoformat(t.replace("Z", "+00:00"))
                    key = dt_b.strftime("%Y-%m-%dT%H:%M")
                    spy_by_minute[key] = float(c)
                except Exception:
                    continue
            last = None
            for ts in (intraday.get("timestamp") or []):
                try:
                    key = _dt_si.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M")
                except Exception:
                    spy_intraday_aligned.append(None)
                    continue
                v = spy_by_minute.get(key)
                if v is None:
                    spy_intraday_aligned.append(last)
                else:
                    last = v
                    spy_intraday_aligned.append(v)
    except Exception:
        spy_intraday_aligned = []

    # Temporary diagnostic counters for the closed_today empty-feed
    # incident 2026-04-29. Safe to expose: just fan-out counts, no PII.
    # Remove once we've confirmed the feeds are populating.
    _diag = {
        "activities_n": len(activities or []),
        "sell_orders_n": len(sell_orders or []),
        "today_synth_sells_n": len(today_synth_sells),
        "today_synth_buys_n": len(today_synth_buys),
        "merged_fills_n": len(merged_fills),
        "today_iso": today_iso,
    }
    payload = {
        "is_strategist": is_strategist,
        "as_of": now.isoformat() + "Z",
        "_diag_closed_today": _diag,
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
            "spy_close": spy_history_aligned,
        },
        "equity_intraday": {
            "timestamps": intraday.get("timestamp", []),
            "equity": intraday.get("equity", []),
            "profit_loss": intraday.get("profit_loss", []),
            "spy_close": spy_intraday_aligned,
        },
        "positions": pos_out if is_strategist else pos_out[:3],  # teaser when not paid
        "sleeves": sleeve_summary,
        "realized_today": realized_today_total,
        "closed_today": closed_today if is_strategist else closed_today[:3],
        "benchmark": {
            "trader_return": trader_return,
            "spy_return": spy_return,
            "alpha": live_alpha,
            "since_date": benchmark_first_day,
            "as_of": now.isoformat() + "Z",
        },
        "open_orders": [
            {
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "qty": o.get("qty"),
                "status": o.get("status"),
                "order_class": o.get("order_class"),
                "client_order_id": o.get("client_order_id"),
                "submitted_at": o.get("submitted_at"),
            }
            for o in (open_orders or [])
            # Only surface our trader-submitted entry parents. Bracket
            # child legs (auto-generated stop/target sells) come back
            # with parent_order_id null in /v2/orders listings once the
            # parent has filled, so we can't filter on that field. Match
            # on side=buy + our "<sleeve>-SYM-YYYYMMDD" client_order_id
            # prefix instead — that captures live entry orders that
            # haven't filled yet without picking up their child legs.
            if o.get("side") == "buy" and (o.get("client_order_id") or "").split("-")[0] in {"momentum", "swing", "daytrade", "scalper"}
        ],
        "strategy_doc": "momentum=top 5, 3-day hold, 1.5× lev, -5%/+10% brackets. swing=ranks 6-15, 5-day hold, 1.5× lev, -7%/+15%. daytrade=ranks 16-25, intraday, 1× lev, -3%/+5%.",
    }
    if not is_strategist:
        payload["teaser"] = True
        payload["hint"] = "Position-level detail unlocks at Strategist tier."
    _PORTFOLIO_CACHE[is_strategist] = (_now_pf, payload)
    return JSONResponse(headers={"Cache-Control": "no-store", "X-Cache": "MISS"}, content=payload)


@app.get("/api/trade-journal")
@limiter.limit("60/minute")
def trade_journal(
    request: Request,
    lookback_days: int = Query(90, ge=1, le=365),
    user: Optional[dict] = Depends(auth.optional_user),
):
    """Per-trade ledger — every buy/sell the live paper trader has made,
    with sleeve attribution, bracket math, and realized P&L on closes.

    The trader writes runtime_data/trade_journal.json after each open
    and close window (committed via .github/workflows/trader.yml).
    Strategist-gated like /api/portfolio because it surfaces the same
    position-level data; the aggregate stats (trade count, win rate)
    flow through anonymously to support the public track-record page.
    """
    is_strategist = False
    if user:
        user_row = users_db.upsert_user(users_conn, user["user_id"], email=user.get("email"))
        is_strategist = users_db.can_access_picks(user_row)

    from datetime import datetime as _dtj, timedelta as _tdj
    from pathlib import Path as _Pathj
    import json as _jsonj
    # Fetch the journal from GitHub raw with a 60s in-process cache so each
    # trader.yml push is visible within ~1 min without redeploying Railway.
    # Falls back to the local file (baked into the container at build time)
    # if GitHub is unreachable. Without this hop the API is forever stale by
    # however long since the last `railway up` — which means breaker
    # liquidations and end-of-day sells never reach the dashboard until the
    # next nightly deploy.
    rows = _fetch_trade_journal_rows()
    if rows is None:
        return JSONResponse(content={
            "rows": [], "n_rows": 0, "stats": {"n_buys": 0, "n_sells": 0, "win_rate": None},
            "hint": "No trades journaled yet. Live trader writes here after open/close.",
        })

    cutoff = (_dtj.utcnow() - _tdj(days=lookback_days)).isoformat()
    recent = [r for r in rows if (r.get("ts") or "") >= cutoff]

    # Aggregate stats — total buys, sells, wins/losses, win-rate. Public
    # (no gating) since these are headline numbers for the track-record
    # page that prove the strategy works without exposing per-trade detail.
    n_buys = sum(1 for r in recent if r.get("event") == "buy")
    sells = [r for r in recent if r.get("event") == "sell"]
    closed = [r for r in sells if r.get("pnl") is not None]
    wins = sum(1 for r in closed if r["pnl"] > 0)
    losses = sum(1 for r in closed if r["pnl"] < 0)
    win_rate = (wins / len(closed)) if closed else None
    realized_total = sum(float(r.get("pnl") or 0) for r in closed)

    payload = {
        "as_of": _dtj.utcnow().isoformat() + "Z",
        "lookback_days": lookback_days,
        "n_rows": len(recent),
        "stats": {
            "n_buys": n_buys,
            "n_sells": len(sells),
            "n_closed_pairs": len(closed),
            "wins": wins, "losses": losses,
            "win_rate": round(win_rate, 3) if win_rate is not None else None,
            "realized_pnl_total": round(realized_total, 2),
            "best_trade": max(closed, key=lambda r: r["pnl"]) if closed else None,
            "worst_trade": min(closed, key=lambda r: r["pnl"]) if closed else None,
        },
    }
    if is_strategist:
        # Full ledger for paid tier; truncate to keep payload sane.
        payload["rows"] = recent[-500:]
    else:
        payload["teaser"] = True
        payload["hint"] = "Per-trade detail unlocks at Strategist tier."
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
