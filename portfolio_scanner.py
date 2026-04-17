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

from db import init_db, save_picks_history
from signals_sec_edgar import get_fundamentals_signal, load_company_info
import enrich_profiles
import enrich_marketcaps

log = logging.getLogger("portfolio_scanner")

PICKS_PATH = Path(__file__).parent / "portfolio_picks.json"
HORIZON = 252  # standard 1-year horizon
TOP_N = 10     # picks per tier

# Asymmetric tier: number of picks + size diversification.
# Research (v4 Phase K) showed unconstrained top-20 had 79/80 picks in
# the smallest log_price quintile — the headline 71% hit rate was driven
# more by the small-cap premium than by real stock-selection alpha.
# Within-quintile lift was still 3.49x, so we preserve model rankings
# but enforce diversification across price buckets.
ASYM_TOP_N = 20                    # total asymmetric picks surfaced
ASYM_SIZE_BUCKETS = 5              # split by current-price quintile
ASYM_PICKS_PER_BUCKET = 4          # 4 × 5 buckets = ASYM_TOP_N
SIZE_NEUTRAL_ASYMMETRIC = True     # flip to False for legacy unconstrained behavior

# Lazy-loaded SEC company-info map. Refreshed on every scan_universe call
# — cheap (disk cache) after the first hit.
_COMPANY_INFO: dict[str, dict] | None = None
_MCAP_CACHE: dict[str, dict] = {}
_ASYM_SCORES: dict[str, dict] = {}


def _ensure_company_info() -> dict[str, dict]:
    global _COMPANY_INFO, _MCAP_CACHE, _ASYM_SCORES
    if _COMPANY_INFO is None:
        try:
            _COMPANY_INFO = load_company_info()
        except Exception:
            _COMPANY_INFO = {}
    # Reload market-cap cache each scan so yfinance backfills get picked up.
    try:
        _MCAP_CACHE = enrich_marketcaps.load_cache()
    except Exception:
        _MCAP_CACHE = {}
    # Reload asymmetric-upside scores (EWMA-vol H7, backtested 2.02x lift).
    try:
        asym_path = Path(__file__).parent / "data_cache" / "asymmetric_scores.json"
        if asym_path.exists():
            _ASYM_SCORES = json.loads(asym_path.read_text())
        else:
            _ASYM_SCORES = {}
    except Exception:
        _ASYM_SCORES = {}
    return _COMPANY_INFO

# QC thresholds. Picks that fail these are dropped entirely — we never
# surface a "BUY" on a name with negative expected return or noise-level
# signal-to-vol. "Aggressive" ≠ "losers with high vol".
MIN_EXPECTED_RETURN = 0.01      # at least +1% expected — below this is noise
MIN_SHARPE_PROXY    = 0.05      # expected_return / projected_vol
MAX_EXPECTED_RETURN = 2.00      # reject absurd projections (>+200%); likely a data error
MAX_PROJ_VOL        = 1.50      # reject if projected annual sigma > 150%; blow-up risk

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

# Ubiquitous mega-caps. Every Strategist user already knows to consider
# these names — surfacing them as "picks" adds zero information. We block
# the most-held / most-covered handful so the picks surface shows tickers
# the user might NOT already be watching.
# TODO: once we expand the universe past the top-30 cache, replace this
# hard-coded set with a market-cap threshold (e.g. exclude > $500B unless
# rationale metric is genuinely differentiated).
MEGA_CAP_BLOCKLIST = {
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSLA",
    "JPM", "V", "MA", "UNH", "WMT", "XOM", "JNJ", "PG", "HD", "MRK",
    "BAC", "CVX", "ORCL", "KO", "PEP", "LLY", "ABBV", "COST",
    "BRK-A", "BRK-B", "BRK.A", "BRK.B",
}


def _compute_confidence(*, sharpe_proxy: float, p10: float | None, p50: float,
                        p90: float | None, current_price: float,
                        sec_sig: dict | None) -> dict:
    """Blend three independent signals into a 0-100 confidence score.

    Components (all clamped to their max, summed):
      1. Risk-adjusted return (0-40): Sharpe proxy scaled so 0.5 saturates.
      2. Fundamentals (0-35): rescaled from signals_sec_edgar raw_score
         ([-1, +1]); absent → 0.
      3. Distribution tightness (0-25): narrower P10-P90 band relative to
         P50 means the engine is more certain about the median; wider band
         means lower confidence even if the median looks attractive.

    Returns the score + a component breakdown so the UI can reveal it.
    """
    # ── 1. Sharpe component ──
    s = max(0.0, min(1.0, sharpe_proxy / 0.5))      # 0.5 → 1.0
    sharpe_points = round(40 * s, 1)

    # ── 2. Fundamentals component ──
    fund_points = 0.0
    if sec_sig is not None:
        raw = sec_sig.get("raw_score")
        if raw is not None:
            # raw_score roughly in [-0.5, +0.5] in practice; rescale so +0.3
            # saturates at full. Negative contributes 0 (no punishment —
            # punishment already lives in the rationale text).
            fund_points = round(35 * max(0.0, min(1.0, raw / 0.3)), 1)

    # ── 3. Distribution tightness ──
    tightness_points = 0.0
    if p10 is not None and p90 is not None and p50 > 0:
        # Half-width of the 80% band as a fraction of P50. Tight = small.
        half_width = (p90 - p10) / (2 * p50)
        # 0.20 half-width (i.e. +/-20%) = median uncertainty; 0.10 = tight;
        # 0.40+ = very wide. Map inversely: 0.10 → full points, 0.40 → 0.
        norm = max(0.0, min(1.0, (0.40 - half_width) / 0.30))
        tightness_points = round(25 * norm, 1)

    total = round(sharpe_points + fund_points + tightness_points)
    total = max(0, min(100, total))
    return {
        "score": total,
        "components": {
            "risk_adjusted":      sharpe_points,
            "fundamentals":       fund_points,
            "model_tightness":    tightness_points,
        },
    }


def _confidence_label(score: int) -> str:
    """Human-readable band for the confidence score. Used in card copy."""
    if score >= 80: return "High conviction"
    if score >= 60: return "Solid conviction"
    if score >= 40: return "Moderate conviction"
    return "Speculative"


def _build_rationale(sec_sig: dict | None, pick: dict | None = None) -> str:
    """Always return a substantive thesis sentence.

    Three-tier construction:
      (1) Standout SEC metrics — the "wow" case, same as before
          ("Revenue +56% YoY · 82% gross margin · 32% op margin").
      (2) Routine SEC metrics — if filings exist but nothing stood out,
          describe what IS there in prose, because concrete mediocrity is
          still signal ("Steady 11% op margin · 4% revenue growth · net
          cash positive"). Never a blank disclaimer.
      (3) Engine-only narrative — for tickers without EDGAR coverage,
          describe the forward-stats case directly from projection data
          ("Projected 14% upside · Sharpe 0.38 · bottom-tercile volatility
          — low-drawdown candidate").

    The /picks page now never shows a generic "no standout" line.
    """
    standout: list[str] = []
    routine: list[str] = []

    if sec_sig:
        g = sec_sig.get("revenue_yoy_growth")
        if g is not None:
            if g >= 0.20:
                standout.append(f"Revenue +{g:.0%} YoY")
            elif g <= -0.08:
                standout.append(f"Revenue {g:+.0%} YoY")
            elif -0.08 < g < 0.20:
                routine.append(f"{g:+.0%} revenue growth")

        gm = sec_sig.get("gross_margin")
        if gm is not None:
            if gm >= 0.55:
                standout.append(f"{gm:.0%} gross margin")
            elif gm >= 0.20:
                routine.append(f"{gm:.0%} gross margin")

        opm = sec_sig.get("operating_margin")
        if opm is not None:
            if opm >= 0.25:
                standout.append(f"{opm:.0%} op margin")
            elif opm >= 0.05:
                routine.append(f"{opm:.0%} op margin")
            elif opm < 0:
                routine.append("operating losses")

        fcf = sec_sig.get("fcf_to_revenue")
        if fcf is not None:
            if fcf >= 0.15:
                standout.append(f"{fcf:.0%} FCF/sales")
            elif fcf >= 0.05:
                routine.append(f"{fcf:.0%} FCF yield")
            elif fcf < 0:
                routine.append("cash-burn")

        bb = sec_sig.get("buyback_intensity")
        if bb is not None:
            if bb >= 0.05:
                standout.append(f"{bb:.0%} returned via buybacks")
            elif bb >= 0.01:
                routine.append(f"modest buybacks ({bb:.0%} of sales)")

        nd = sec_sig.get("net_debt_change_pct")
        if nd is not None:
            if nd <= -0.15:
                standout.append("deleveraging")
            elif nd >= 0.25:
                standout.append("leveraging up")
            elif -0.15 < nd < 0.25:
                routine.append("stable balance sheet")

    # Tier 1: standout — lead with those
    if standout:
        return " · ".join(standout[:3])

    # Tier 2: routine metrics — compose prose. If only one metric is
    # available, pad with the engine narrative so the thesis still feels
    # substantive; otherwise join the concrete filings-derived items.
    if routine:
        if len(routine) >= 2:
            body = ", ".join(routine[:3])
            return f"Latest filings: {body[0].upper() + body[1:]}"
        if pick:
            er = pick.get("expected_return") or 0
            body = f"{routine[0]} per latest filings · model projects {er*100:+.0f}% to P50 target"
            return body[0].upper() + body[1:]
        return f"Latest filings: {routine[0]}"

    # Tier 3: engine-only narrative — lead with the model's view, stated
    # confidently. We do NOT apologize for the absence of SEC commentary —
    # the engine signal is itself a valid reason to surface the pick.
    if pick:
        er = pick.get("expected_return") or 0
        sharpe = pick.get("sharpe_proxy") or 0
        vol = pick.get("risk") or 0
        er_desc = (
            f"Model projects +{er*100:.0f}% to 1-year P50 target" if er >= 0.10 else
            f"Model projects {er*100:+.0f}% to P50 target"
        )
        sharpe_desc = (
            f"Sharpe {sharpe:.2f}, top-quartile risk-adjusted"
                if sharpe >= 0.30 else
            f"Sharpe {sharpe:.2f}, positive but noise-adjacent"
                if sharpe >= 0.10 else
            f"Sharpe {sharpe:.2f} — position-size carefully"
        )
        vol_desc = (
            f"{vol*100:.0f}% projected vol (low-tercile)" if vol < 0.25 else
            f"{vol*100:.0f}% projected vol (mid-tercile)" if vol < 0.45 else
            f"{vol*100:.0f}% projected vol (high-tercile — tactical only)"
        )
        return f"{er_desc} · {sharpe_desc} · {vol_desc}"

    return "Ranked on projection-engine signal."


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
    _ensure_company_info()

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
        if r["symbol"] in MEGA_CAP_BLOCKLIST:
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

        # Profile enrichment — company name + sector + market cap + liquidity.
        # Sources (each may be partial):
        #   * FMP /profile → companyName, sector, industry (~30% coverage,
        #     rate-limited to 250/day so fills slowly)
        #   * SEC EDGAR title → company_name (100% coverage, free, local)
        #   * yfinance market_cap cache → market_cap + avg_volume
        #     (~95% coverage, our own daily cache)
        #   * FMP fundamentals on projection row → legacy market_cap
        profile = None
        try:
            profile = enrich_profiles.load_cached(r["symbol"])
        except Exception:
            pass
        sec_info = _COMPANY_INFO.get(r["symbol"], {}) if _COMPANY_INFO else {}
        mcap_info = _MCAP_CACHE.get(r["symbol"].upper(), {})
        company_name = (profile or {}).get("name") or sec_info.get("name") or r["symbol"]
        sector = (profile or {}).get("sector") or fundamentals.get("sector")
        industry = (profile or {}).get("industry")
        # Prefer yfinance cache (live) over stale projection-row fundamentals
        market_cap = mcap_info.get("market_cap") or fundamentals.get("market_cap")
        avg_volume = mcap_info.get("avg_volume")

        engine_stats = {
            "expected_return": expected_return,
            "sharpe_proxy": sharpe_proxy,
            "risk": sigma,
        }
        confidence = _compute_confidence(
            sharpe_proxy=sharpe_proxy,
            p10=r.get("p10"), p50=p50, p90=r.get("p90"),
            current_price=current_price,
            sec_sig=sec_sig,
        )
        scored.append({
            "symbol": r["symbol"],
            "company_name": company_name,
            "website": (profile or {}).get("website"),
            "sector": sector,
            "industry": industry,
            "expected_return": round(expected_return, 6),
            "risk": round(sigma, 6),
            "sharpe_proxy": round(sharpe_proxy, 6),
            "current_price": round(current_price, 2),
            "p50_target": round(p50, 2),
            "p10": round(r["p10"], 2) if r.get("p10") else None,
            "p90": round(r["p90"], 2) if r.get("p90") else None,
            "fundamentals": {
                "pe_trailing": fundamentals.get("pe_trailing"),
                "market_cap": fundamentals.get("market_cap"),
                "sector": fundamentals.get("sector"),
                "debt_equity": fundamentals.get("debt_equity"),
            },
            "market_cap": market_cap,
            "avg_volume": avg_volume,
            "asymmetric": _ASYM_SCORES.get(r["symbol"].upper()),
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
            "rationale": _build_rationale(sec_sig, engine_stats),
            "confidence": confidence["score"],
            "confidence_label": _confidence_label(confidence["score"]),
            "confidence_components": confidence["components"],
            # Carry sigma for tiering — will be dropped before output
            "_sigma": sigma,
        })

    if not scored:
        log.warning("No valid projections after filtering")
        return []

    # ── QC pass: drop anything that shouldn't be on a buy list ──
    #
    # Previously the tier-assignment loop had `else: entry["tier"] = "aggressive"`
    # which tagged every non-conservative, non-moderate name as aggressive —
    # including negative-expected-return names and noise-level signals. That
    # put GME −13% ER on the "aggressive picks" list, which is not a pick,
    # it's a short signal or a skip. Filter first, then tier.
    before = len(scored)
    scored = [
        e for e in scored
        if MIN_EXPECTED_RETURN <= e["expected_return"] <= MAX_EXPECTED_RETURN
        and e["sharpe_proxy"] >= MIN_SHARPE_PROXY
        and e["_sigma"] <= MAX_PROJ_VOL
    ]
    log.info("QC: %d/%d entries pass (ER ∈ [%.2f, %.2f], Sharpe ≥ %.2f, vol ≤ %.2f)",
             len(scored), before, MIN_EXPECTED_RETURN, MAX_EXPECTED_RETURN,
             MIN_SHARPE_PROXY, MAX_PROJ_VOL)
    if not scored:
        log.warning("QC filters dropped everything — no picks this run")
        return []

    # Sort by sigma to determine tercile boundaries among QC-passing names
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

    # Assign tiers. By construction every surviving entry has ER > 0, so
    # "aggressive" now genuinely means "high-vol positive-ER pick", not
    # "leftovers nobody else wanted".
    for entry in scored:
        sig = entry["_sigma"]
        if sig <= t1_cutoff and _passes_conservative_fundamentals(entry):
            entry["tier"] = "conservative"
        elif sig <= t2_cutoff:
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


TIER_MIN_DOLLAR_VOLUME = 500_000  # $500k avg daily turnover floor

def _liquid(pick: dict) -> bool:
    """$500k avg daily turnover floor. Applied to every tier so we don't
    recommend names a Strategist can't actually buy at their position
    size. Matches the filter already enforced on the asymmetric tier —
    investor-diligence Wave 1 alignment (2026-04-17)."""
    v = pick.get("avg_volume") or 0
    p = pick.get("current_price") or 0
    # Only filter when we have both; missing data (new ticker, no enrichment
    # yet) passes through rather than silently dropping.
    if not v or not p:
        return True
    return v * p >= TIER_MIN_DOLLAR_VOLUME


def get_picks(tier: str | None = None, results: list[dict] | None = None) -> list[dict]:
    """Return top N picks per tier from cached scan results.

    If *results* is None, loads from portfolio_picks.json.
    If *tier* is specified, returns only that tier's top picks.
    Picks below the liquidity floor are dropped; the tier ranker fills
    from deeper in the list.
    """
    if results is None:
        results = load_cached_picks()
        if results is None:
            # No cache — compute on the fly
            results = scan_universe()

    # Apply tradeable-liquidity floor across every tier (matches what
    # the asymmetric tier has always done). Honest-audit Wave 1 showed
    # unfiltered picks gave a hit-rate ~12pp lift from names users
    # couldn't actually buy at scale.
    results = [r for r in results if _liquid(r)]

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


def save_picks(results: list[dict], conn=None) -> None:
    """Persist scan results to JSON cache file AND the picks_history ledger.

    JSON cache drives the live `/api/picks` endpoint; the sqlite ledger is
    the immutable record we use to compute realized returns, hit rate, and
    Sharpe per tier on `/api/track-record`.
    """
    top_picks = get_picks(results=results)

    # ── Asymmetric upside tier ──
    # Scoring is picked NIGHTLY by overnight_learn.py. When the neural
    # network beats the hand-crafted methods in recent backtests, its
    # scores sit at data_cache/nn_scores.json and we use those;
    # otherwise we fall back to H7 (EWMA P90 ratio).
    #
    # Liquidity floor: require avg_volume * current_price > $500k/day.
    # Penny stocks with $50k/day liquidity can't absorb Strategist-scale
    # positions even if the model likes them. Protects product credibility.
    nn_scores = {}
    scoring_regime = {}
    try:
        nn_path = Path(__file__).parent / "data_cache" / "nn_scores.json"
        if nn_path.exists():
            nn_scores = json.loads(nn_path.read_text())
        regime_path = Path(__file__).parent / "data_cache" / "production_scorer.json"
        if regime_path.exists():
            scoring_regime = json.loads(regime_path.read_text())
    except Exception:
        pass

    use_nn = (scoring_regime.get("winner") == "nn_score" and bool(nn_scores))

    def _asym_score(r: dict) -> float:
        if use_nn:
            return float(nn_scores.get(r["symbol"].upper(), 0.0))
        return float((r.get("asymmetric") or {}).get("score", 0.0))

    MIN_DOLLAR_VOLUME = 500_000  # $500k avg daily turnover
    asym_eligible = []
    for r in results:
        score = _asym_score(r)
        if use_nn:
            if score < 0.05:
                continue  # NN scores are small floats; keep anything above trivial
        else:
            if score < 1.3:
                continue
        # Liquidity filter — skip uninvestable names
        avg_v = r.get("avg_volume") or 0
        cur_p = r.get("current_price") or 0
        if avg_v and cur_p and avg_v * cur_p < MIN_DOLLAR_VOLUME:
            continue
        asym_eligible.append((score, r))
    asym_eligible.sort(key=lambda sr: -sr[0])

    # Size-neutral bucketing: group by log_price quintile, take top-K per
    # bucket. Prevents the model from concentrating all picks in the
    # smallest-cap names (which v4 research showed inflated headline
    # numbers 2×). Picks within each bucket are still sorted by model score.
    if SIZE_NEUTRAL_ASYMMETRIC and len(asym_eligible) >= ASYM_SIZE_BUCKETS * 2:
        import math
        prices = sorted(
            (math.log(max(r.get("current_price") or 1.0, 0.01)), score, r)
            for score, r in asym_eligible
        )
        n = len(prices)
        bucket_size = n // ASYM_SIZE_BUCKETS
        buckets: list[list[tuple]] = [[] for _ in range(ASYM_SIZE_BUCKETS)]
        for i, (_, score, r) in enumerate(prices):
            # Last bucket absorbs any remainder.
            b = min(i // bucket_size, ASYM_SIZE_BUCKETS - 1)
            buckets[b].append((score, r))
        # Within each bucket, sort by model score desc and take top K.
        asym_picks = []
        bucket_picks_count = []
        for b in buckets:
            b.sort(key=lambda sr: -sr[0])
            kept = b[:ASYM_PICKS_PER_BUCKET]
            asym_picks.extend(r for _, r in kept)
            bucket_picks_count.append(len(kept))
        log.info(
            "Size-neutral asymmetric picks: %d total across %d price buckets "
            "(%s per bucket)",
            len(asym_picks), ASYM_SIZE_BUCKETS, bucket_picks_count,
        )
    else:
        asym_picks = [r for _, r in asym_eligible[:ASYM_TOP_N]]
        log.info("Unconstrained asymmetric picks: %d", len(asym_picks))

    for p in asym_picks:
        # Mark these specifically as asymmetric tier so the UI can show a
        # separate section. We DON'T mutate their original tier — they
        # still belong to conservative/moderate/aggressive for the other
        # views.
        p["in_asymmetric"] = True

    payload = {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "total_tickers": len(results),
        "picks": top_picks,
        "asymmetric_picks": asym_picks,
        "full_results": results,
    }

    # Guard: refuse to overwrite a good cache with a degraded scan. A real
    # scan of the live universe returns 800-1000+ tickers. Anything below
    # MIN_UNIVERSE means the engine cache was empty, data providers failed,
    # or the universe loader short-circuited — in every one of those cases,
    # preserving yesterday's picks is better than shipping a crippled set.
    # Enforced after 2026-04-17 when a cold-cache slow run wrote a 223-
    # ticker / 0-asymmetric payload over a healthy 914-ticker / 10-asym
    # one, silently degrading the live site until manually reverted.
    MIN_UNIVERSE = 500
    if len(results) < MIN_UNIVERSE:
        log.error(
            "save_picks REFUSED: scan returned %d tickers, below MIN_UNIVERSE=%d. "
            "Preserving existing %s. Investigate: data providers, cache restore, "
            "or the universe loader probably short-circuited.",
            len(results), MIN_UNIVERSE, PICKS_PATH,
        )
        return

    PICKS_PATH.write_text(json.dumps(payload, indent=2))
    log.info("Saved %d picks + %d asymmetric to %s",
             len(top_picks), len(asym_picks), PICKS_PATH)

    # Also append to picks_history ledger. Only write the top-N picks per
    # tier (what /api/picks surfaces) — the ledger tracks what the user
    # actually saw, not the long tail.
    close_conn = False
    if conn is None:
        conn = init_db()
        close_conn = True
    try:
        written = save_picks_history(conn, top_picks)
        log.info("Appended %d rows to picks_history ledger", written)
    except Exception as e:
        log.warning("picks_history append failed: %s", e)
    finally:
        if close_conn:
            conn.close()


def load_cached_picks() -> list[dict] | None:
    """Load picks from JSON cache, or None if missing/corrupt."""
    if not PICKS_PATH.exists():
        return None
    try:
        data = json.loads(PICKS_PATH.read_text())
        return data.get("full_results") or data.get("picks")
    except (json.JSONDecodeError, KeyError):
        return None


def load_cached_asymmetric_picks() -> list[dict]:
    """Load asymmetric picks from JSON cache. Empty list if missing/corrupt."""
    if not PICKS_PATH.exists():
        return []
    try:
        data = json.loads(PICKS_PATH.read_text())
        return data.get("asymmetric_picks") or []
    except (json.JSONDecodeError, KeyError):
        return []


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
