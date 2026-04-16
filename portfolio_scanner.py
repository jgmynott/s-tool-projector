"""
Portfolio scanner — risk-tiered stock recommendations.

Reads cached projections from SQLite, ranks by a forward Sharpe proxy
(expected_return / volatility), and buckets tickers into conservative /
moderate / aggressive tiers based on volatility tercile + fundamentals
quality.  Designed to complete in seconds (no network calls, no
recomputation).

Usage:
    from portfolio_scanner import scan_universe, get_picks
    results = scan_universe()          # full ranked list
    picks   = get_picks("conservative") # top 10 conservative
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from db import init_db

log = logging.getLogger("portfolio_scanner")

PICKS_PATH = Path(__file__).parent / "portfolio_picks.json"
HORIZON = 252  # standard 1-year horizon
TOP_N = 10     # picks per tier


def scan_universe(conn=None) -> list[dict]:
    """Scan all cached projections and return ranked, tiered results.

    Reads the latest 252-day projection for every symbol in the DB,
    computes a forward Sharpe proxy, assigns risk tiers, and returns
    the full list sorted by sharpe_proxy descending within each tier.
    """
    close_conn = False
    if conn is None:
        conn = init_db()
        close_conn = True

    t0 = time.time()

    # Fetch the most recent projection per symbol for the standard horizon.
    # Using a window function to grab the latest run_date per symbol.
    rows = conn.execute(
        """
        SELECT p.*
        FROM projections p
        INNER JOIN (
            SELECT symbol, MAX(run_date) AS max_date
            FROM projections
            WHERE horizon_days = ?
            GROUP BY symbol
        ) latest
        ON p.symbol = latest.symbol
           AND p.run_date = latest.max_date
           AND p.horizon_days = ?
        """,
        (HORIZON, HORIZON),
    ).fetchall()

    if not rows:
        log.warning("No cached projections found for horizon=%d", HORIZON)
        return []

    # Build scored list — skip tickers missing key fields.
    scored = []
    for row in rows:
        r = dict(row)
        current_price = r.get("current_price")
        p50 = r.get("p50")
        sigma = r.get("sigma")

        if not current_price or not p50 or not sigma or sigma <= 0 or current_price <= 0:
            continue

        expected_return = (p50 - current_price) / current_price
        sharpe_proxy = expected_return / sigma

        # Parse fundamentals if available
        fundamentals = {}
        fj = r.get("fundamentals_json")
        if fj:
            if isinstance(fj, str):
                try:
                    fundamentals = json.loads(fj)
                except (json.JSONDecodeError, TypeError):
                    pass
            elif isinstance(fj, dict):
                fundamentals = fj

        scored.append({
            "symbol": r["symbol"],
            "expected_return": round(expected_return, 6),
            "risk": round(sigma, 6),
            "sharpe_proxy": round(sharpe_proxy, 6),
            "current_price": round(current_price, 2),
            "p50_target": round(p50, 2),
            "fundamentals": {
                "pe_trailing": fundamentals.get("pe_trailing"),
                "market_cap": fundamentals.get("market_cap"),
                "sector": fundamentals.get("sector"),
                "debt_equity": fundamentals.get("debt_equity"),
            },
            # Carry sigma for tiering — will be dropped before output
            "_sigma": sigma,
        })

    if not scored:
        log.warning("No valid projections after filtering")
        return []

    # Sort by sigma to determine tercile boundaries
    sigmas = sorted(s["_sigma"] for s in scored)
    n = len(sigmas)
    t1_cutoff = sigmas[n // 3]       # bottom tercile upper bound
    t2_cutoff = sigmas[2 * n // 3]   # middle tercile upper bound

    def _passes_conservative_fundamentals(entry: dict) -> bool:
        """Conservative tier prefers quality: positive PE, moderate leverage."""
        f = entry["fundamentals"]
        pe = f.get("pe_trailing")
        de = f.get("debt_equity")
        # If PE available, must be positive (profitable)
        if pe is not None and pe <= 0:
            return False
        # If debt/equity available, must be < 1.5
        if de is not None and de > 1.5:
            return False
        return True

    # Assign tiers
    for entry in scored:
        sig = entry["_sigma"]
        er = entry["expected_return"]

        if sig <= t1_cutoff and er > 0 and _passes_conservative_fundamentals(entry):
            entry["tier"] = "conservative"
        elif sig <= t2_cutoff and er > 0:
            entry["tier"] = "moderate"
        else:
            entry["tier"] = "aggressive"

    # Remove internal field
    for entry in scored:
        del entry["_sigma"]

    # Sort each tier by sharpe_proxy descending
    scored.sort(key=lambda x: (-_tier_rank(x["tier"]), -x["sharpe_proxy"]))

    elapsed = time.time() - t0
    log.info(
        "Scanned %d tickers in %.1fs — conservative=%d, moderate=%d, aggressive=%d",
        len(scored), elapsed,
        sum(1 for s in scored if s["tier"] == "conservative"),
        sum(1 for s in scored if s["tier"] == "moderate"),
        sum(1 for s in scored if s["tier"] == "aggressive"),
    )

    if close_conn:
        conn.close()

    return scored


def _tier_rank(tier: str) -> int:
    """Numeric rank for sorting tiers: conservative first."""
    return {"conservative": 0, "moderate": 1, "aggressive": 2}.get(tier, 3)


def get_picks(tier: str | None = None, results: list[dict] | None = None) -> list[dict]:
    """Return top N picks per tier from cached scan results.

    If *results* is None, loads from portfolio_picks.json.
    If *tier* is specified, returns only that tier's top picks.
    """
    if results is None:
        results = load_cached_picks()
        if results is None:
            # No cache — compute on the fly
            results = scan_universe()

    if tier:
        tier = tier.lower()
        filtered = [r for r in results if r["tier"] == tier]
        return filtered[:TOP_N]

    # All tiers, top N each
    out = []
    for t in ("conservative", "moderate", "aggressive"):
        bucket = [r for r in results if r["tier"] == t]
        out.extend(bucket[:TOP_N])
    return out


def save_picks(results: list[dict]) -> None:
    """Persist scan results to JSON cache file."""
    payload = {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "total_tickers": len(results),
        "picks": get_picks(results=results),
        "full_results": results,
    }
    PICKS_PATH.write_text(json.dumps(payload, indent=2))
    log.info("Saved %d picks to %s", len(payload["picks"]), PICKS_PATH)


def load_cached_picks() -> list[dict] | None:
    """Load picks from JSON cache, or None if missing/corrupt."""
    if not PICKS_PATH.exists():
        return None
    try:
        data = json.loads(PICKS_PATH.read_text())
        return data.get("full_results") or data.get("picks")
    except (json.JSONDecodeError, KeyError):
        return None


def get_scan_age_hours() -> float | None:
    """How old is the cached scan, in hours? None if no cache."""
    if not PICKS_PATH.exists():
        return None
    try:
        data = json.loads(PICKS_PATH.read_text())
        scanned_at = datetime.fromisoformat(data["scanned_at"])
        if scanned_at.tzinfo is None:
            scanned_at = scanned_at.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - scanned_at).total_seconds() / 3600
    except (json.JSONDecodeError, KeyError, ValueError):
        return None
