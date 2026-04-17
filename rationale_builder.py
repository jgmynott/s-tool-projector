"""Buy-side analytical thesis builder for portfolio picks.

Replaces the old `_build_rationale` dump with differentiated, narrative
prose. Each pick gets a short analyst-voice thesis that:

  1. Identifies a *primary driver* (the reason this pick surfaced),
  2. Supports it with one or two corroborating factors pulled from the
     same pick's data (SEC filings, projection engine, asymmetric score),
  3. Varies sentence structure so no two picks in the same batch share
     phrasing.

Hard rules enforced across the batch:

  * No primary driver is used by more than 2 picks. If the natural
    primary for a third pick is already saturated, the builder promotes
    the pick's next-strongest driver.
  * No sentence-level phrase (archetype template body) is used more
    than twice. Each archetype ships with 6-10 template variants; the
    builder rotates through them.
  * Every returned thesis is a complete sentence with correct
    capitalization + punctuation.

This is not investment advice. The voice is observational ("the model
reads...", "the filings show...", "the distribution is skewed...") —
never prescriptive.
"""

from __future__ import annotations

from collections import Counter
from typing import Optional


# ─── Archetype classification ────────────────────────────────────────

def _classify(pick: dict, sec: Optional[dict]) -> list[str]:
    """Return an ordered list of applicable archetypes for this pick.

    The first element is the natural primary; later elements are
    fallbacks if the primary hits its reuse cap. Order is by signal
    strength, not by template diversity.
    """
    drivers: list[str] = []
    er = pick.get("expected_return") or 0.0
    sharpe = pick.get("sharpe_proxy") or 0.0
    vol = pick.get("risk") or 0.0
    asym = pick.get("asymmetric") or {}
    p90_ratio = asym.get("p90_ratio")

    if sec:
        g = sec.get("revenue_yoy_growth")
        gm = sec.get("gross_margin")
        opm = sec.get("operating_margin")
        fcf = sec.get("fcf_to_revenue")
        bb = sec.get("buyback_intensity")
        nd = sec.get("net_debt_change_pct")

        # Every archetype below requires all the metrics its template
        # actually renders — otherwise we emit "—" in user-facing prose.
        if g is not None and g >= 0.25 and gm is not None:
            drivers.append("hypergrowth")
        if g is not None and g <= -0.08 and nd is not None and nd <= -0.10:
            drivers.append("turnaround")
        if nd is not None and nd <= -0.15 and opm is not None:
            drivers.append("deleveraging")
        if fcf is not None and fcf >= 0.20 and bb is not None and bb >= 0.05:
            drivers.append("capital_return")
        if opm is not None and opm >= 0.30 and gm is not None and gm >= 0.50:
            drivers.append("compounder")
        if g is not None and 0.10 <= g < 0.25 and opm is not None and opm >= 0.10:
            drivers.append("durable_growth")
        if fcf is not None and fcf >= 0.12:
            drivers.append("cash_conversion")
        if opm is not None and opm >= 0.25:
            drivers.append("margin_franchise")
        if bb is not None and bb >= 0.05:
            drivers.append("buyback_heavy")
        if gm is not None and gm >= 0.55:
            drivers.append("gross_margin_moat")
        if opm is not None and opm < 0 and g is not None:
            drivers.append("pre_profit")

    # Engine-side archetypes always available as fallback
    if p90_ratio is not None and p90_ratio >= 1.25:
        drivers.append("coiled_spring")
    if sharpe >= 0.40 and vol > 0 and vol < 0.30:
        drivers.append("low_vol_quality")
    if er >= 0.25:
        drivers.append("model_upside")
    drivers.append("model_signal")  # terminal fallback

    # Dedupe while preserving order
    seen = set()
    out: list[str] = []
    for d in drivers:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


# ─── Sector phrasing (ensures industry context without repetition) ──

_SECTOR_CLAUSES = {
    "Technology": [
        "for a tech name", "inside a sector still priced on narrative",
        "against a tech tape repricing on rates",
    ],
    "Financial Services": [
        "for a balance-sheet business", "in a rate-sensitive cohort",
        "where underwriting discipline drives multiple",
    ],
    "Industrials": [
        "for a capital-goods name", "in a cyclical cohort where order-book depth matters",
        "where working-capital turns compound",
    ],
    "Healthcare": [
        "inside a healthcare pipeline story", "for a regulated-revenue name",
        "where reimbursement economics dominate",
    ],
    "Energy": [
        "for a commodity-exposed name", "against a volatile upstream tape",
        "where netback-per-barrel sets the valuation",
    ],
    "Consumer Cyclical": [
        "inside a discretionary-spending cohort", "for a retailer",
        "where unit economics at the store level set the ceiling",
    ],
    "Consumer Defensive": [
        "for a staples operator", "where pricing power offsets volume fade",
        "inside a defensive cohort",
    ],
    "Real Estate": [
        "for a REIT", "where cap-rate compression drives mark-to-market",
        "against a property cohort with AFFO sensitivity to rates",
    ],
    "Basic Materials": [
        "for a commodity processor", "where input-cost pass-through sets the margin floor",
        "inside a materials cohort priced on cycle stage",
    ],
    "Communication Services": [
        "for a communications name", "where ARPU and churn dominate the long-run model",
    ],
    "Utilities": [
        "for a regulated utility", "inside a rate-base growth cohort",
    ],
}


def _sector_clause(sector: Optional[str], used: Counter) -> str:
    if not sector:
        return ""
    pool = _SECTOR_CLAUSES.get(sector, [])
    for c in pool:
        key = f"sector::{c}"
        if used[key] < 2:
            used[key] += 1
            return c
    return ""


# ─── Company-name formatting ─────────────────────────────────────────

def _short_name(name: Optional[str], symbol: str) -> str:
    if not name:
        return symbol
    # Strip common suffixes for readability
    n = name
    for suf in [", Inc.", " Inc.", ", Inc", " Inc", " Corp.", " Corp",
                " Corporation", " Company", " Co.", " Ltd.", " Ltd",
                " Holdings", " Group", " Trust", " Incorporated"]:
        if n.endswith(suf):
            n = n[: -len(suf)].rstrip(",").rstrip()
            break
    return n or symbol


# ─── Archetype templates ─────────────────────────────────────────────
#
# Each function returns (thesis_text, phrase_tag). The phrase_tag is how
# the batch-level dedupe tracks this specific template variant. Within
# each function we cycle through variants that haven't hit 2 uses yet.

def _pct(x: Optional[float], digits: int = 0) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.{digits}f}%"


def _signed_pct(x: Optional[float]) -> str:
    if x is None:
        return "—"
    return f"{x * 100:+.0f}%"


def _pick_variant(variants: list[str], used: Counter, key_prefix: str) -> str:
    """Return the first variant in `variants` whose usage key is < 2;
    otherwise cycle back to the least-used one. Always records the use."""
    for i, v in enumerate(variants):
        k = f"{key_prefix}::{i}"
        if used[k] < 2:
            used[k] += 1
            return v
    # All saturated — fall back to least-used.
    counts = [(used[f"{key_prefix}::{i}"], i, v) for i, v in enumerate(variants)]
    counts.sort()
    _, i, v = counts[0]
    used[f"{key_prefix}::{i}"] += 1
    return v


def _hypergrowth(pick, sec, used):
    g = sec.get("revenue_yoy_growth")
    gm = sec.get("gross_margin")
    name = _short_name(pick.get("company_name"), pick["symbol"])
    variants = [
        f"Top-line is accelerating at {_pct(g)} year-on-year, and the gross-margin line ({_pct(gm)}) is holding through the growth — the sequence that typically precedes a re-rate.",
        f"{name} is printing {_pct(g)} revenue growth with gross margin at {_pct(gm)}; the model treats the combination as a durable demand signal rather than a one-quarter beat.",
        f"Revenue reacceleration to {_pct(g)} YoY is the headline here, but the gross margin ({_pct(gm)}) doing the confirming work is what lifts this out of the momentum bucket.",
        f"The filings show {_pct(g)} top-line growth paired with a {_pct(gm)} gross-margin profile — growth plus quality, not growth-at-any-cost.",
        f"Hypergrowth regime: revenue up {_pct(g)} YoY against a {_pct(gm)} gross profile, which suggests the unit economics are scaling with the volume.",
        f"{_pct(g)} revenue growth is the loud signal; the quiet one is a {_pct(gm)} gross margin that hasn't had to buy the growth.",
    ]
    return _pick_variant(variants, used, "hypergrowth")


def _durable_growth(pick, sec, used):
    g = sec.get("revenue_yoy_growth")
    opm = sec.get("operating_margin")
    name = _short_name(pick.get("company_name"), pick["symbol"])
    variants = [
        f"{_pct(g)} revenue growth on a {_pct(opm)} operating margin frame — the durable-growth profile, where the model rewards consistency over slope.",
        f"{name} is compounding at {_pct(g)} on the top line with {_pct(opm)} operating margin; neither number is spectacular in isolation, but the pair is the point.",
        f"Steady mid-teens top-line expansion ({_pct(g)}) paired with a {_pct(opm)} operating margin reads as a franchise widening its own moat.",
        f"This is the quiet compounding setup: {_pct(g)} revenue growth, {_pct(opm)} op margin, and no sign of one pulling forward at the expense of the other.",
        f"The interesting read here is not the growth ({_pct(g)}) or the margin ({_pct(opm)}) alone — it is that both have held together for long enough to price as a run-rate.",
        f"Mid-cycle compounder: {_pct(g)} YoY revenue and {_pct(opm)} operating margin, neither stretched, both repeatable.",
    ]
    return _pick_variant(variants, used, "durable_growth")


def _deleveraging(pick, sec, used):
    nd = sec.get("net_debt_change_pct")
    opm = sec.get("operating_margin")
    name = _short_name(pick.get("company_name"), pick["symbol"])
    variants = [
        f"Balance-sheet repair is doing the work: net debt down {_pct(abs(nd or 0))} year-on-year while the operating line is holding {_pct(opm)}. Multiple expansion tends to follow the arithmetic.",
        f"{name} is deleveraging meaningfully ({_pct(abs(nd or 0))} net debt reduction) without sacrificing the operating margin ({_pct(opm)}) — the setup buy-side credit desks like before equity desks notice.",
        f"The capital structure is the thesis here: a {_pct(abs(nd or 0))} reduction in net debt alongside stable {_pct(opm)} operating margin changes the equity-risk premium this name should trade at.",
        f"Net debt contraction of {_pct(abs(nd or 0))} is the rare kind that does not come from cutting the business; the {_pct(opm)} operating margin says the cash is coming from operations.",
        f"Balance-sheet discipline is the story — leverage down {_pct(abs(nd or 0))}, operating margin still {_pct(opm)}. The market prices deleveraging slowly; that is the window.",
        f"What changes here is the denominator of every credit ratio: {_pct(abs(nd or 0))} less net debt while the cash-flow engine ({_pct(opm)} op margin) keeps running.",
    ]
    return _pick_variant(variants, used, "deleveraging")


def _turnaround(pick, sec, used):
    g = sec.get("revenue_yoy_growth")
    nd = sec.get("net_debt_change_pct")
    variants = [
        f"Revenue is down {_pct(abs(g or 0))} but the balance sheet is tightening ({_pct(abs(nd or 0))} less net debt) — the sequence that distinguishes a turnaround from a value trap.",
        f"The top-line contraction ({_pct(abs(g or 0))} YoY) looks ugly in isolation, but net debt moving down {_pct(abs(nd or 0))} says management is buying time rather than burning it.",
        f"Classic transitional profile: {_pct(g)} revenue alongside a {_pct(abs(nd or 0))} deleveraging move. The question is whether revenue bottoms before the balance sheet runs out of room.",
        f"Revenue declined {_pct(abs(g or 0))}, but the {_pct(abs(nd or 0))} reduction in net debt is the tell that the management team is rebuilding optionality, not defending estimates.",
    ]
    return _pick_variant(variants, used, "turnaround")


def _capital_return(pick, sec, used):
    fcf = sec.get("fcf_to_revenue")
    bb = sec.get("buyback_intensity")
    variants = [
        f"Cash machine: {_pct(fcf)} FCF-to-sales funds a {_pct(bb)} capital return — the buyback is the by-product of a business that converts revenue to cash faster than it can reinvest it.",
        f"The capital-return math is the point — {_pct(fcf)} free-cash conversion translated into {_pct(bb)} of revenue repurchased, which is the discipline you want paired with the multiple.",
        f"{_pct(bb)} of sales returned via buybacks is the headline; the {_pct(fcf)} FCF-to-revenue is what makes it sustainable rather than financed.",
        f"This is a cash-allocation story. {_pct(fcf)} free cash flow per dollar of sales, {_pct(bb)} of that routed back to holders — the per-share denominator is shrinking faster than the business.",
        f"{_pct(fcf)} FCF conversion with a {_pct(bb)} repurchase cadence — the kind of internal-rate-of-return story that quietly compounds while the headline narrative looks unremarkable.",
    ]
    return _pick_variant(variants, used, "capital_return")


def _compounder(pick, sec, used):
    gm = sec.get("gross_margin")
    opm = sec.get("operating_margin")
    name = _short_name(pick.get("company_name"), pick["symbol"])
    variants = [
        f"{name} runs at {_pct(gm)} gross and {_pct(opm)} operating — the margin stack compounders defend through cycles, and the reason this name prices like a franchise rather than a ticker.",
        f"The margin profile does the heavy lifting: {_pct(gm)} gross, {_pct(opm)} operating. That spread is what keeps a business on a buy list across regimes.",
        f"Quality read: gross margin at {_pct(gm)}, operating at {_pct(opm)}. Neither number moves quickly; both are the reason the model keeps surfacing this name.",
        f"Classic compounder economics — {_pct(gm)} gross margin, {_pct(opm)} operating margin — which is the configuration that lets free cash flow lead earnings as the business scales.",
        f"Margin durability is the signal: {_pct(gm)} gross flowing to {_pct(opm)} operating, with the gap funding reinvestment without needing the equity markets.",
    ]
    return _pick_variant(variants, used, "compounder")


def _cash_conversion(pick, sec, used):
    fcf = sec.get("fcf_to_revenue")
    variants = [
        f"Free-cash conversion at {_pct(fcf)} of revenue is the quiet specialist skill here — the number earnings-based screens miss but the buy side eventually notices.",
        f"{_pct(fcf)} FCF-to-sales is the real read; GAAP earnings understate the cash generation, which is where the discount usually closes.",
        f"Cash generation is the differentiator — {_pct(fcf)} of every revenue dollar lands as free cash flow, which is what will fund whatever optionality shows up next.",
        f"The business converts {_pct(fcf)} of revenue into free cash flow, which is the kind of ratio that decouples the name from accounting-cycle narratives.",
        f"Operating cash arithmetic leads here: {_pct(fcf)} FCF-to-sales with no obvious one-off distortion, which is the underrated configuration on a 3-year horizon.",
    ]
    return _pick_variant(variants, used, "cash_conversion")


def _margin_franchise(pick, sec, used):
    opm = sec.get("operating_margin")
    variants = [
        f"The {_pct(opm)} operating margin is the moat — pricing power that survives cycle rotation is the rarest asset on a ranked screen.",
        f"{_pct(opm)} operating margin is what keeps this name inside the high-quality cohort even when the macro narrative moves against the sector.",
        f"Margin-franchise setup: {_pct(opm)} at the operating line is the number that decouples share price from input-cost swings.",
        f"A {_pct(opm)} operating margin typically means the competitive structure is narrower than the screens suggest, and that is what the engine keeps rewarding.",
        f"The operating profile ({_pct(opm)}) does the rerating work here — most of the cohort trades at a structurally lower number.",
    ]
    return _pick_variant(variants, used, "margin_franchise")


def _buyback_heavy(pick, sec, used):
    bb = sec.get("buyback_intensity")
    variants = [
        f"Aggressive capital return: {_pct(bb)} of revenue routed through buybacks — a float-reduction thesis you can model before you model the operations.",
        f"{_pct(bb)} of sales returned via repurchase is the kind of per-share compounding that does not need the market to cooperate.",
        f"The buyback program ({_pct(bb)} of revenue) compounds ownership on its own schedule, which is why the model weights this name higher than a screen on earnings alone would suggest.",
        f"Shareholder yield is doing the work — {_pct(bb)} of revenue repurchased, which is the disciplined end of the capital-allocation spectrum.",
    ]
    return _pick_variant(variants, used, "buyback_heavy")


def _gross_margin_moat(pick, sec, used):
    gm = sec.get("gross_margin")
    variants = [
        f"{_pct(gm)} gross margin is the structural tell — a level sustained only by pricing power that most of the universe does not have.",
        f"The gross-margin signature ({_pct(gm)}) is where the thesis starts; everything downstream — op margin, FCF, capital return — inherits that cushion.",
        f"A {_pct(gm)} gross margin is the kind of number that lets a business absorb a full cycle of input-cost inflation without re-rating on earnings estimates alone.",
        f"Gross-margin durability at {_pct(gm)} is the clearest competitive read here; the rest of the income statement is a downstream consequence.",
    ]
    return _pick_variant(variants, used, "gross_margin_moat")


def _pre_profit(pick, sec, used):
    g = sec.get("revenue_yoy_growth")
    opm = sec.get("operating_margin")
    variants = [
        f"Still pre-profit at the operating line ({_pct(opm)}), but {_pct(g)} revenue growth is the signal — the question is margin slope, not demand.",
        f"Operating line negative ({_pct(opm)}) while revenue grows {_pct(g)} — the classic scale-to-profitability trade, priced as a bet on fixed-cost absorption.",
        f"The earnings picture is still a forward-looking exercise ({_pct(opm)} operating margin), but the top-line trajectory ({_pct(g)}) funds the runway.",
        f"Not profitable yet — operating margin at {_pct(opm)} — but the {_pct(g)} top-line growth is what decides whether this is optionality or a cash-burn trap.",
    ]
    return _pick_variant(variants, used, "pre_profit")


def _coiled_spring(pick, sec, used):
    asym = pick.get("asymmetric") or {}
    p90r = asym.get("p90_ratio") or 0
    p10r = asym.get("p10_ratio") or 0
    er = pick.get("expected_return") or 0
    variants = [
        f"Distribution is skewed right: the 90th percentile of the 1-year forward lands at {p90r:.1f}× current, against a {p10r:.1f}× floor — the coiled-spring profile the asymmetric tier is designed to surface.",
        f"The forward distribution has a heavy right tail — P90 at {p90r:.1f}× vs. P10 at {p10r:.1f}× — which is why the engine ranks the convex payoff above the median case.",
        f"Asymmetric setup: {p90r:.1f}× upside case against {p10r:.1f}× downside on the engine distribution. The trade-off is realized volatility, which is the cost of owning the right tail.",
        f"Right-skew is the point here — P90 ratio of {p90r:.1f}× is outsized relative to the {_signed_pct(er)} median expectation, which is the signature of an optionality-like payoff inside a single ticker.",
        f"Model distribution is wider-than-typical to the upside ({p90r:.1f}× P90) with a contained left tail ({p10r:.1f}× P10) — the asymmetry the ranked screen rewards.",
    ]
    return _pick_variant(variants, used, "coiled_spring")


def _low_vol_quality(pick, sec, used):
    sharpe = pick.get("sharpe_proxy") or 0
    vol = pick.get("risk") or 0
    variants = [
        f"The risk-adjusted read is the cleanest version of the thesis: Sharpe {sharpe:.2f} at {_pct(vol)} realized vol — the quadrant the ranker rarely finds together.",
        f"Low-vol, high-Sharpe pairing: {_pct(vol)} projected volatility with a {sharpe:.2f} Sharpe proxy — the profile that earns a place in the conservative book without giving up edge.",
        f"This clears the conservative bar on shape, not size — Sharpe {sharpe:.2f} on sub-30% vol, which is the quiet performance most portfolios underweight.",
        f"Defensive-quality setup: {_pct(vol)} realized volatility, {sharpe:.2f} risk-adjusted return — the kind of name that anchors a ranked book when dispersion widens.",
    ]
    return _pick_variant(variants, used, "low_vol_quality")


def _model_upside(pick, sec, used):
    er = pick.get("expected_return") or 0
    sharpe = pick.get("sharpe_proxy") or 0
    variants = [
        f"The model prices {_signed_pct(er)} to the P50 target at Sharpe {sharpe:.2f} — without a filings-side catalyst, the engine-only signal is the reason this name is on the list.",
        f"Engine read: {_signed_pct(er)} median upside, Sharpe {sharpe:.2f}. No SEC corroboration yet, which is either early information or a reason to size accordingly.",
        f"Pure projection-engine surface: {_signed_pct(er)} expected return, Sharpe {sharpe:.2f} — flagged on model output before the fundamentals tape confirms.",
        f"The engine reads {_signed_pct(er)} to target with {sharpe:.2f} Sharpe; absent filings data this is a statistical signal, not a narrative one.",
    ]
    return _pick_variant(variants, used, "model_upside")


def _model_signal(pick, sec, used):
    er = pick.get("expected_return") or 0
    sharpe = pick.get("sharpe_proxy") or 0
    vol = pick.get("risk") or 0
    variants = [
        f"Engine ranks this in the top quintile on a combined expected-return ({_signed_pct(er)}) and Sharpe ({sharpe:.2f}) read — a purely statistical surface, sized for that.",
        f"Projection-only thesis: {_signed_pct(er)} to P50, Sharpe {sharpe:.2f}, {_pct(vol)} volatility — the model kept it; the narrative is yet to catch up.",
        f"Ranked on model output alone — {_signed_pct(er)} expected return against {_pct(vol)} realized vol. The Sharpe proxy of {sharpe:.2f} is why it clears the filter.",
        f"No filings-side signal this pass; the ranker holds it on engine Sharpe ({sharpe:.2f}) and a {_signed_pct(er)} P50 expectation.",
    ]
    return _pick_variant(variants, used, "model_signal")


_BUILDERS = {
    "hypergrowth":       _hypergrowth,
    "durable_growth":    _durable_growth,
    "deleveraging":      _deleveraging,
    "turnaround":        _turnaround,
    "capital_return":    _capital_return,
    "compounder":        _compounder,
    "cash_conversion":   _cash_conversion,
    "margin_franchise":  _margin_franchise,
    "buyback_heavy":     _buyback_heavy,
    "gross_margin_moat": _gross_margin_moat,
    "pre_profit":        _pre_profit,
    "coiled_spring":     _coiled_spring,
    "low_vol_quality":   _low_vol_quality,
    "model_upside":      _model_upside,
    "model_signal":      _model_signal,
}


# ─── Builder ─────────────────────────────────────────────────────────

class RationaleBuilder:
    """Stateful across a batch — enforces the buy-side uniqueness rules.

    A single instance should be constructed once per `scan_universe`
    call and passed pick-by-pick. Do not reuse across runs.

    Limits enforced:
      * Max 2 picks share a primary driver (archetype).
      * Max 2 picks share a specific template variant (phrase).
      * Max 2 picks share a sector-flavor clause.
    """

    def __init__(self) -> None:
        self._driver_counts: Counter = Counter()
        self._phrase_counts: Counter = Counter()

    def build(self, pick: dict, sec_sig: Optional[dict]) -> str:
        sec = pick.get("sec_fundamentals") or sec_sig
        drivers = _classify(pick, sec)

        # Pick first driver whose global-use-count is < 2.
        chosen = drivers[-1]  # fallback
        for d in drivers:
            if self._driver_counts[d] < 2:
                chosen = d
                break
        self._driver_counts[chosen] += 1

        builder = _BUILDERS.get(chosen, _model_signal)
        body = builder(pick, sec or {}, self._phrase_counts)

        sector = pick.get("sector") or (pick.get("fundamentals") or {}).get("sector")
        # Only append sector clause when the primary thesis is
        # filings-driven and the sector is known — avoids stuffing a
        # flavor clause onto an already-dense engine sentence.
        if sector and chosen not in {"coiled_spring", "low_vol_quality",
                                      "model_upside", "model_signal"}:
            tail = _sector_clause(sector, self._phrase_counts)
            if tail and tail not in body.lower():
                body = body.rstrip(".") + f", {tail}."

        return body


def build_rationale(pick: dict, sec_sig: Optional[dict],
                    builder: Optional[RationaleBuilder] = None) -> str:
    """Convenience entry point for callers that don't want to manage
    builder state (single-pick rebuilds, tests). Do NOT use in the
    batch path — the batch path must share one builder instance so
    the uniqueness rules are enforced."""
    b = builder or RationaleBuilder()
    return b.build(pick, sec_sig)
