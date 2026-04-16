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
from signals_sec_edgar import get_fundamentals_signal

log = logging.getLogger("portfolio_scanner")

PICKS_PATH = Path(__file__).parent / "portfolio_picks.json"
HORIZON = 252  # standard 1-year horizon
TOP_N = 10     # picks per tier

# Common ETFs that end up in the price cache but don't belong in a stock-picks
# product. These are indices / sector baskets, not companies — surfacing them
# in "conservative picks" confuses the Strategist offering.
ETF_BLOCKLIST = {
    "SPY", "QQQ", "DIA", "IWM", "VOO", "VTI", "VXUS", "BND", "AGG",
    "GLD", "SLV", "USO", "UNG", "TLT", "IEF", "SHY", "LQD", "HYG",
    "XLF", "XLK", "XLE", "XLV", "XLY", "XLP", "XLB", "XLI", "XLU",
    "XLRE", "XLC", "EFA", "EEM", "VGK", "VWO", "ACWI", "QQQM",
    "SPLG", "RSP", "IVV", "VEA", "VNQ", "SCHX", "SCHB", "SCHF",
    "TQQQ", "SQQQ", "SOXL", "SOXS", "TMF", "TMV",
}


def _build_rationale(sec_sig: dict | None) -> str | None:
    """Turn an SEC fundamentals signal into a short human-readable
    rationale. Returns None if no notable signals stand out — better to
    show nothing than a bland line like "4% op margin".
    """
    if not sec_sig:
        return None
    parts: list[str] = []

    g = sec_sig.get("revenue_yoy_growth")
    if g is not None:
        if g >= 0.20:
            parts.append(f"Revenue +{g:.0%} YoY")
        elif g <= -0.08:
            parts.append(f"Revenue {g:+.0%} YoY")

    gm = sec_sig.get("gross_margin")
    if gm is not None and gm >= 0.55:
        parts.append(f"{gm:.0%} gross margin")

    opm = sec_sig.get("operating_margin")
    if opm is not None and opm >= 0.25:
        parts.append(f"{opm:.0%} op margin")

    fcf = sec_sig.get("fcf_to_revenue")
    if fcf is not None and fcf >= 0.15:
        parts.append(f"{fcf:.0%} FCF/sales")

    bb = sec_sig.get("buyback_intensity")
    if bb is not None and bb >= 0.05:
        parts.append(f"{bb:.0%} returned via buybacks")

    nd = sec_sig.get("net_debt_change_pct")
    if nd is not None:
        if nd <= -0.15:
            parts.append("deleveraging")
        elif nd >= 0.25:
            parts.append("leveraging up")

    if not parts:
        return None
    return " · ".join(parts[:3])


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
        if r["symbol"] in ETF_BLOCKLIST:
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

        # SEC EDGAR signal — best-effort; not all tickers will have one
        # (ETFs, foreign filers, very new listings).
        try:
            sec_sig = get_fundamentals_signal(conn, r["symbol"])
        except Exception as e:
            log.debug("sec signal failed for %s: %s", r["symbol"], e)
            sec_sig = None

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
            "sec_fundamentals": {
                "as_of": sec_sig.get("as_of") if sec_sig else None,
                "revenue_yoy_growth": sec_sig.get("revenue_yoy_growth") if sec_sig else None,
                "gross_margin": sec_sig.get("gross_margin") if sec_sig else None,
                "operating_margin": sec_sig.get("operating_margin") if sec_sig else None,
                "fcf_to_revenue": sec_sig.get("fcf_to_revenue") if sec_sig else None,
                "buyback_intensity": sec_sig.get("buyback_intensity") if sec_sig else None,
                "net_debt_change_pct": sec_sig.get("net_debt_change_pct") if sec_sig else None,
                "score": sec_sig.get("raw_score") if sec_sig else None,
            } if sec_sig else None,
            "rationale": _build_rationale(sec_sig),
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
