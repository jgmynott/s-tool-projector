"""
S-Tool Projector API.

Thin FastAPI layer over the projection cache + on-demand computation.
Serves precomputed results from SQLite; falls back to live computation
for uncached symbols.

Run:  uvicorn api:app --reload --port 8000
"""

from __future__ import annotations

import logging
import time
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from db import init_db, get_projection, get_projection_age_hours, save_projection, list_cached_symbols, get_sentiment
from projector_engine import run_projection
from hardening import health_checker, HealthChecker

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("api")

app = FastAPI(title="S-Tool Projector API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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

    # Track API component health
    if response.status_code < 400:
        health_checker.record_success("api")
    else:
        health_checker.record_error("api")

    return response

# DB connection (module-level, WAL mode is fine for concurrent reads)
conn = init_db()

STALE_HOURS = 18  # recompute if older than this


# ── Endpoints ──

@app.get("/api/project")
def project(
    symbol: str = Query(..., min_length=1, max_length=10),
    horizon: int = Query(252, ge=5, le=756),
    force: bool = Query(False),
):
    """Return projection for a symbol. Uses cache if fresh, else computes live."""
    symbol = symbol.upper().strip()

    if not force:
        age = get_projection_age_hours(conn, symbol, horizon)
        if age is not None and age < STALE_HOURS:
            cached = get_projection(conn, symbol, horizon)
            if cached:
                log.info(f"Cache hit: {symbol} h={horizon} age={age:.1f}h")
                return cached

    # Compute on demand
    log.info(f"Computing: {symbol} h={horizon}")
    try:
        result = run_projection(symbol, horizon_days=horizon)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        log.exception(f"Projection failed for {symbol}")
        raise HTTPException(status_code=500, detail=f"Projection failed: {e}")

    save_projection(conn, result)
    log.info(f"Saved: {symbol} h={horizon} in {result['compute_secs']:.1f}s")

    # Return the saved version (JSON fields parsed)
    return get_projection(conn, symbol, horizon)


@app.get("/api/cached")
def cached():
    """List all cached symbols with last run date."""
    return list_cached_symbols(conn)


@app.get("/api/sentiment")
def sentiment(
    ticker: str = Query(..., min_length=1, max_length=10),
    days: int = Query(10, ge=1, le=90),
):
    """Return recent daily sentiment for a ticker."""
    rows = get_sentiment(conn, ticker.upper().strip(), days)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No sentiment data for {ticker}")
    return rows


# ── Health & Provider Status ──

@app.get("/api/health")
def api_health():
    """Return health status of all system components."""
    health = health_checker.check_health()
    with _stats_lock:
        health["api_stats"] = {
            "total_requests": _request_count,
            "errors_4xx": _error_4xx,
            "errors_5xx": _error_5xx,
        }
    health["overall"] = "healthy" if health_checker.is_healthy() else "degraded"
    return health


@app.get("/api/providers")
def api_providers():
    """Show data provider availability status."""
    health = health_checker.check_health()
    providers = health.get("providers", {})
    # Include defaults if not yet tracked
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
    # Fall back to the original dashboard
    dash = Path(__file__).parent / "projection_dashboard.html"
    if dash.exists():
        return FileResponse(dash, media_type="text/html")
    return {"message": "S-Tool Projector API", "docs": "/docs"}
