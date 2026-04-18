"""
SEC EDGAR XBRL fundamentals signal.

Pulls structured financial facts (Revenues, margins, cash flow, buybacks,
leverage) from the free SEC EDGAR XBRL Company Facts API and derives
point-in-time fundamental signals that are:

  * real capital commitments (filed under penalty of law, not sentiment)
  * orthogonal to price-momentum signals (which have all failed backtests)
  * available for every US public company back to ~2009

Why build this after S-64 was canceled (2026-04-08):
  S-64 framed EDGAR as a display-side fundamentals fetcher. FMP Premium
  covered that use case (S-79). This module is different — it wires
  EDGAR as an ALPHA SOURCE + /picks rationale layer:

    1. per-ticker quarterly rows for backtest-style tilt evaluation
    2. latest-period signal dict for portfolio_scanner rationale strings

Endpoints used:
  * https://data.sec.gov/api/xbrl/companyfacts/CIK{10-digit}.json
  * https://data.sec.gov/api/xbrl/frames/us-gaap/{concept}/USD/CY{Y}Q{Q}I.json
  * https://www.sec.gov/files/company_tickers.json  (CIK↔ticker map)

SEC requires a descriptive User-Agent with contact info; we read
OWNER_EMAIL from env and fall back to a generic research contact.
Rate limit is 10 req/sec — we sleep 0.12s between requests.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

import requests

log = logging.getLogger("sec_edgar")

BASE = "https://data.sec.gov"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

DB_PATH = Path(__file__).parent / "projector_cache.db"
CACHE_DIR = Path(__file__).parent / "data_cache" / "sec_edgar"
CIK_MAP_PATH = CACHE_DIR / "cik_map.json"
FACTS_DIR = CACHE_DIR / "facts"

_OWNER_EMAIL = os.getenv("OWNER_EMAIL", "research@s-tool.io")
USER_AGENT = f"s-tool-projector/1.0 ({_OWNER_EMAIL})"
RATE_SLEEP = 0.12  # 10 req/sec limit → 0.1s buffer a touch

# Facts cache TTL — fundamentals change quarterly; weekly refresh is plenty.
FACTS_TTL_SECS = 7 * 24 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS sec_fundamentals (
    symbol          TEXT    NOT NULL,
    period_end      TEXT    NOT NULL,   -- ISO date, fiscal period end
    period_type     TEXT    NOT NULL,   -- 'Q' (single quarter) or 'FY' (full year)
    form            TEXT,               -- '10-Q' / '10-K'
    revenues        REAL,
    gross_profit    REAL,
    operating_income REAL,
    net_income      REAL,
    cfo             REAL,               -- cash from operations
    capex           REAL,               -- positive by SEC convention (outflow)
    buybacks_value  REAL,
    long_term_debt  REAL,
    cash            REAL,
    stockholders_equity REAL,
    shares_out      REAL,
    filed_at        TEXT,               -- ISO date
    PRIMARY KEY (symbol, period_end, period_type)
);
CREATE INDEX IF NOT EXISTS idx_sec_symbol ON sec_fundamentals(symbol);
CREATE INDEX IF NOT EXISTS idx_sec_period ON sec_fundamentals(period_end);
"""


def init_sec_fundamentals_table(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def populate_from_cache(conn: sqlite3.Connection) -> int:
    """Build the production `sec_fundamentals` table from the cached
    `data_cache/sec_edgar/facts/` JSONs that the nightly pipeline
    downloads. No network calls. Returns the row count written.

    Idempotent — INSERT OR REPLACE on (symbol, period_end, period_type).
    Safe to call on every overnight_learn run; takes ~30s for the full
    1,947-company cache.
    """
    cache_dir = Path(__file__).parent / "data_cache" / "sec_edgar" / "facts"
    cik_map_path = Path(__file__).parent / "data_cache" / "sec_edgar" / "cik_map.json"
    if not cache_dir.exists():
        log.warning("populate_from_cache: %s missing", cache_dir)
        return 0
    if not cik_map_path.exists():
        log.warning("populate_from_cache: %s missing", cik_map_path)
        return 0

    init_sec_fundamentals_table(conn)
    cik_map = json.loads(cik_map_path.read_text())
    cik_to_syms: dict[str, list[str]] = {}
    for sym, cik in cik_map.items():
        cik_to_syms.setdefault(cik, []).append(sym)

    facts_files = sorted(cache_dir.glob("CIK*.json"))
    total_rows = 0
    for p in facts_files:
        cik = p.stem[3:]
        syms = cik_to_syms.get(cik) or cik_to_syms.get(cik.lstrip("0"))
        if not syms:
            continue
        primary = syms[0]
        try:
            facts = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        rows = parse_facts_to_rows(facts, primary)
        if rows:
            total_rows += save_rows(conn, rows)
    conn.commit()
    log.info("populate_from_cache: %d rows from %d cached fact files",
             total_rows, len(facts_files))
    return total_rows


# ── HTTP helpers ────────────────────────────────────────────────────────

_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT
_session.headers["Accept-Encoding"] = "gzip, deflate"
_last_call = 0.0


def _get_json(url: str, timeout: int = 30) -> dict | None:
    """GET + JSON parse with SEC rate limiting. Returns None on 404."""
    global _last_call
    elapsed = time.time() - _last_call
    if elapsed < RATE_SLEEP:
        time.sleep(RATE_SLEEP - elapsed)
    _last_call = time.time()
    r = _session.get(url, timeout=timeout)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


# ── CIK map ─────────────────────────────────────────────────────────────

def load_cik_map(refresh: bool = False) -> dict[str, str]:
    """Return {ticker: 10-digit-zero-padded CIK}. Cached to disk."""
    info = load_company_info(refresh=refresh)
    return {t: v["cik"] for t, v in info.items()}


COMPANY_INFO_PATH = CACHE_DIR / "company_info.json"


def load_company_info(refresh: bool = False) -> dict[str, dict]:
    """Return {ticker: {cik, name}} from SEC's company_tickers.json.

    SEC's registrant titles are SCREAMING CASE ("APPLE INC") so we also
    store a display-friendly title-cased version. This is the fastest +
    freest source of company names — no API key, cached locally after
    first fetch, covers every US registrant.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if COMPANY_INFO_PATH.exists() and not refresh:
        try:
            return json.loads(COMPANY_INFO_PATH.read_text())
        except json.JSONDecodeError:
            pass

    log.info("Fetching SEC CIK↔ticker↔name map")
    data = _get_json(TICKERS_URL)
    if not data:
        raise RuntimeError("SEC company_tickers.json unreachable")

    out: dict[str, dict] = {}
    for entry in data.values():
        ticker = str(entry.get("ticker", "")).strip().upper()
        cik_int = entry.get("cik_str")
        title = str(entry.get("title", "")).strip()
        if ticker and cik_int is not None:
            out[ticker] = {
                "cik": f"{int(cik_int):010d}",
                "name": _titlecase_company(title),
            }
    COMPANY_INFO_PATH.write_text(json.dumps(out, separators=(",", ":")))
    # Also refresh cik-only file for anything still reading it.
    CIK_MAP_PATH.write_text(json.dumps({t: v["cik"] for t, v in out.items()},
                                        separators=(",", ":")))
    log.info("Company info map: %d tickers", len(out))
    return out


# Words that should stay uppercase or have non-standard casing when we
# title-case SEC's all-caps registrant names.
_KEEP_UPPER = {"ABM", "ADT", "AES", "AMD", "AMN", "AMP", "AOL", "AON", "AT&T",
               "ATT", "BJ", "BP", "CBRE", "CDW", "CF", "CGI", "CMS", "CNA",
               "CVS", "DBD", "DDOG", "DTE", "EA", "EMC", "EPR", "ETF", "EV",
               "F5", "FMC", "GE", "GLW", "GP", "GPI", "GS", "HCA", "HCP", "HP",
               "HPE", "HPQ", "IBM", "IDCC", "IFF", "IRM", "ITT", "JPM", "KKR",
               "KLA", "LLY", "LQ", "LUV", "MBT", "MGM", "MMM", "MOS", "MSI",
               "NASDAQ", "NCR", "NET", "NYSE", "PCG", "PPG", "PPL", "PRU",
               "REIT", "RV", "SEC", "SL", "SLM", "TD", "TFC", "TJX", "TPG",
               "TV", "UK", "US", "USA", "UTX", "VF", "VMC", "VRT", "WRK",
               "XTO", "YUM"}
_SMALL_WORDS = {"and", "or", "of", "the", "for", "to", "at", "in", "on",
                "a", "an", "de", "la"}


def _titlecase_company(s: str) -> str:
    """Convert SEC all-caps titles to presentation-ready mixed case.

    Rules:
      * Preserve known acronyms ("IBM", "JPM") — full list above.
      * Lowercase small filler words except as first word ("Bank of America").
      * Title-case anything else, including compound names like "MCDONALD'S".
    """
    if not s:
        return s
    parts = s.split()
    out = []
    for i, w in enumerate(parts):
        stripped = w.strip(",./()&")
        if stripped.upper() in _KEEP_UPPER:
            out.append(w.upper())
        elif stripped.lower() in _SMALL_WORDS and i > 0:
            out.append(stripped.lower())
        else:
            # Keep dots in corp suffixes: INC. → Inc.
            out.append(w.capitalize())
    joined = " ".join(out)
    # Common suffix fixes
    joined = joined.replace(" Corp.", " Corp").replace(" Inc.", " Inc") \
                    .replace(" Co.", " Co").replace(" Ltd.", " Ltd")
    return joined


def cik_for(symbol: str, mapping: dict[str, str] | None = None) -> str | None:
    if mapping is None:
        mapping = load_cik_map()
    return mapping.get(symbol.upper())


# ── Company Facts ───────────────────────────────────────────────────────

def fetch_company_facts(cik: str, refresh: bool = False) -> dict | None:
    """Fetch raw SEC companyfacts JSON, on-disk cached. None if 404."""
    FACTS_DIR.mkdir(parents=True, exist_ok=True)
    path = FACTS_DIR / f"CIK{cik}.json"
    if path.exists() and not refresh:
        age = time.time() - path.stat().st_mtime
        if age < FACTS_TTL_SECS:
            try:
                return json.loads(path.read_text())
            except json.JSONDecodeError:
                pass

    url = f"{BASE}/api/xbrl/companyfacts/CIK{cik}.json"
    data = _get_json(url)
    if data is None:
        return None
    path.write_text(json.dumps(data, separators=(",", ":")))
    return data


# Tag priority lists — SEC filers use different tags for the same concept,
# especially post-ASC 606. Earlier tag wins.
TAG_PRIORITY = {
    "revenues": [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ],
    "buybacks_value": [
        "StockRepurchasedAndRetiredDuringPeriodValue",
        "PaymentsForRepurchaseOfCommonStock",
    ],
    "long_term_debt": ["LongTermDebt", "LongTermDebtNoncurrent"],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
    "stockholders_equity": ["StockholdersEquity"],
    "shares_out": [
        "CommonStockSharesOutstanding",
        "EntityCommonStockSharesOutstanding",
    ],
}


def _iter_tag_entries(facts: dict, tag_list: list[str]) -> Iterable[tuple[str, dict]]:
    """Yield (tag, entry) across ALL matching tags, in priority order.

    A single company can switch tags over time (e.g. pre/post ASC 606
    revenue reporting) — we need to combine entries from every tag in the
    priority list, not pick just one.
    """
    usgaap = facts.get("facts", {}).get("us-gaap", {})
    for tag in tag_list:
        entry = usgaap.get(tag)
        if not entry:
            continue
        units = entry.get("units", {})
        # Prefer USD; fall back to shares (for shares-outstanding tags).
        unit_list = None
        for key in ("USD", "shares", "USD/shares"):
            if key in units:
                unit_list = units[key]
                break
        if unit_list is None and units:
            unit_list = next(iter(units.values()))
        if not unit_list:
            continue
        for e in unit_list:
            yield tag, e


def _classify_period(start: str | None, end: str | None) -> str | None:
    """Return 'Q' (single quarter), 'FY' (full year), or None (skip).

    EDGAR's `fy` metadata is the FILING year, not the period year, and
    comparison-period data gets re-tagged with each new filing. So we
    ignore fy/fp entirely and derive period type from the actual duration
    (end - start). YTD cumulatives (6mo, 9mo) are skipped — we can
    reconstruct single-quarter values later by differencing if needed.
    """
    if not (start and end):
        return None
    try:
        s = date.fromisoformat(start[:10])
        e = date.fromisoformat(end[:10])
    except ValueError:
        return None
    dur = (e - s).days
    if 80 <= dur <= 100:
        return "Q"
    if 350 <= dur <= 380:
        return "FY"
    return None


def _apply_flow_entry(bucket: dict, end: str, period_type: str, field: str,
                      e: dict, anchor_dates: bool) -> None:
    """Write e[val] into bucket[(end, period_type)][field], keeping the
    latest filing per period (handles restatements). Anchor fields (from
    the revenue pass) also populate period_end / form / filed_at."""
    key = (end, period_type)
    row = bucket[key]
    prev_filed = row.get(f"_{field}_filed", "")
    this_filed = str(e.get("filed", ""))
    if prev_filed and this_filed <= prev_filed:
        return
    try:
        row[field] = float(e.get("val", 0.0))
    except (TypeError, ValueError):
        return
    row[f"_{field}_filed"] = this_filed
    if anchor_dates:
        row["form"] = e.get("form")
        row["filed_at"] = e.get("filed")


def parse_facts_to_rows(facts: dict, symbol: str) -> list[dict]:
    """Turn raw EDGAR companyfacts JSON into per-period fundamental rows.

    Key design choice: we key rows on (period_end, period_type), NOT on
    (fy, fp). EDGAR's fy/fp metadata is the filing year — comparison-period
    data shows up tagged with the current filing's fy, so (fy, fp) does
    not uniquely identify a period. The actual period_end date does.

    Three passes:
      1. Revenue flow: establish the skeleton of real periods + period_end.
      2. Other flow fields (income, cash flow): fill only known periods.
      3. Instant (balance-sheet) fields: match to the nearest known
         period_end by filing.
    """
    bucket: dict[tuple[str, str], dict] = {}

    # ── Pass 1: revenue anchor ────────────────────────────────────────
    for tag, e in _iter_tag_entries(facts, TAG_PRIORITY["revenues"]):
        ptype = _classify_period(e.get("start"), e.get("end"))
        if ptype is None:
            continue
        end = e.get("end")
        key = (end, ptype)
        if key not in bucket:
            bucket[key] = {
                "symbol": symbol.upper(),
                "period_end": end,
                "period_type": ptype,
                "form": None,
                "filed_at": None,
            }
        _apply_flow_entry(bucket, end, ptype, "revenues", e, anchor_dates=True)

    # ── Pass 2: other flow fields ─────────────────────────────────────
    FLOW_FIELDS = ("gross_profit", "operating_income", "net_income",
                   "cfo", "capex", "buybacks_value")
    for field in FLOW_FIELDS:
        for tag, e in _iter_tag_entries(facts, TAG_PRIORITY[field]):
            ptype = _classify_period(e.get("start"), e.get("end"))
            if ptype is None:
                continue
            end = e.get("end")
            if (end, ptype) not in bucket:
                continue
            _apply_flow_entry(bucket, end, ptype, field, e, anchor_dates=False)

    # ── Pass 3: instant (balance-sheet) fields ────────────────────────
    # These have no `start`; their `end` is an as-of date. Match each to
    # the nearest revenue period_end within ±10 days, same period_type
    # inferred from the filing form (10-K → FY, 10-Q → Q).
    known_ends_by_type: dict[str, list[date]] = {"Q": [], "FY": []}
    for (end, ptype) in bucket.keys():
        try:
            known_ends_by_type[ptype].append(date.fromisoformat(end[:10]))
        except ValueError:
            pass
    INSTANT_FIELDS = ("long_term_debt", "cash", "stockholders_equity", "shares_out")
    for field in INSTANT_FIELDS:
        for tag, e in _iter_tag_entries(facts, TAG_PRIORITY[field]):
            end = e.get("end")
            form = e.get("form") or ""
            if not end:
                continue
            if e.get("start"):  # defensive — instant tags shouldn't have start
                continue
            ptype = "FY" if form.startswith("10-K") else "Q" if form.startswith("10-Q") else None
            if ptype is None:
                continue
            try:
                e_date = date.fromisoformat(end[:10])
            except ValueError:
                continue
            # Nearest known end within ±10 days
            candidates = known_ends_by_type.get(ptype, [])
            if not candidates:
                continue
            nearest = min(candidates, key=lambda d: abs((d - e_date).days))
            if abs((nearest - e_date).days) > 10:
                continue
            matched_end = nearest.isoformat()
            if (matched_end, ptype) not in bucket:
                continue
            _apply_flow_entry(bucket, matched_end, ptype, field, e, anchor_dates=False)

    # Strip internal _*_filed bookkeeping and drop any period missing revenue
    out = []
    for row in bucket.values():
        if row.get("revenues") is None:
            continue
        clean = {k: v for k, v in row.items() if not k.startswith("_")}
        out.append(clean)
    out.sort(key=lambda r: (r["period_end"], r["period_type"]))
    return out


def save_rows(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    cols = [
        "symbol", "period_end", "period_type", "form",
        "revenues", "gross_profit", "operating_income", "net_income",
        "cfo", "capex", "buybacks_value", "long_term_debt", "cash",
        "stockholders_equity", "shares_out", "filed_at",
    ]
    placeholders = ", ".join("?" for _ in cols)
    sql = f"INSERT OR REPLACE INTO sec_fundamentals ({', '.join(cols)}) VALUES ({placeholders})"
    n = 0
    for r in rows:
        vals = [r.get(c) for c in cols]
        conn.execute(sql, vals)
        n += 1
    conn.commit()
    return n


# ── Derived signals ─────────────────────────────────────────────────────

def _ttm_from_period(conn: sqlite3.Connection, symbol: str, field: str,
                      anchor_end: str) -> float | None:
    """Sum `field` over the trailing ~1y ending on-or-before anchor_end.

    Three strategies (first one that works wins):
      A. FY row at or within 30 days before the anchor → return directly.
      B. Four single-Q rows spanning ~365 days → sum them. Works for
         income-statement tags filers report per-quarter.
      C. FY_prior + YTD_current − YTD_prior_year. The standard way to
         derive TTM cash-flow numbers, since most filers report CF as
         year-to-date in their 10-Q rather than single-quarter.
    """
    # ── A. FY row match ──
    fy_row = conn.execute(
        f"""SELECT period_end, {field} FROM sec_fundamentals
             WHERE symbol = ? AND period_type = 'FY' AND {field} IS NOT NULL
               AND period_end <= ? AND period_end >= date(?, '-30 days')
             ORDER BY period_end DESC LIMIT 1""",
        (symbol.upper(), anchor_end, anchor_end),
    ).fetchone()
    if fy_row and fy_row[1] is not None:
        return float(fy_row[1])

    # ── B. Four single-Q sum ──
    q_rows = conn.execute(
        f"""SELECT period_end, {field} FROM sec_fundamentals
             WHERE symbol = ? AND period_type = 'Q' AND {field} IS NOT NULL
               AND period_end <= ?
             ORDER BY period_end DESC LIMIT 6""",
        (symbol.upper(), anchor_end),
    ).fetchall()
    if len(q_rows) >= 4:
        picked = q_rows[:4]
        span = (date.fromisoformat(picked[0][0][:10])
                - date.fromisoformat(picked[3][0][:10])).days
        if 270 <= span <= 380:
            return float(sum(r[1] for r in picked))

    # ── C. FY_prior + YTD_current − YTD_prior_year ──
    ytd_cur = conn.execute(
        f"""SELECT {field} FROM sec_fundamentals
             WHERE symbol = ? AND period_end = ? AND period_type = 'Q'
               AND {field} IS NOT NULL""",
        (symbol.upper(), anchor_end),
    ).fetchone()
    if not ytd_cur:
        return None
    prior_anchor = (date.fromisoformat(anchor_end[:10])
                    - timedelta(days=365)).isoformat()
    ytd_prior = conn.execute(
        f"""SELECT {field} FROM sec_fundamentals
             WHERE symbol = ? AND period_type = 'Q' AND {field} IS NOT NULL
               AND period_end BETWEEN date(?, '-14 days') AND date(?, '+14 days')
             ORDER BY period_end DESC LIMIT 1""",
        (symbol.upper(), prior_anchor, prior_anchor),
    ).fetchone()
    if not ytd_prior:
        return None
    fy_prior_row = conn.execute(
        f"""SELECT {field} FROM sec_fundamentals
             WHERE symbol = ? AND period_type = 'FY' AND {field} IS NOT NULL
               AND period_end <= date(?, '-60 days')
               AND period_end >= date(?, '-400 days')
             ORDER BY period_end DESC LIMIT 1""",
        (symbol.upper(), anchor_end, anchor_end),
    ).fetchone()
    if not fy_prior_row:
        return None
    return float(fy_prior_row[0] + ytd_cur[0] - ytd_prior[0])


def get_fundamentals_signal(conn: sqlite3.Connection, symbol: str) -> dict | None:
    """Point-in-time fundamentals signal for the latest available period.

    Output:
      {
        symbol, as_of, period_type, form,
        revenue_ttm, revenue_yoy_growth,
        gross_margin, operating_margin, net_margin,
        fcf_to_revenue, buyback_intensity, net_debt_change_pct,
        raw_score
      }
    """
    latest = conn.execute(
        """SELECT period_end, period_type, form FROM sec_fundamentals
             WHERE symbol = ? AND revenues IS NOT NULL
             ORDER BY period_end DESC LIMIT 1""",
        (symbol.upper(),),
    ).fetchone()
    if not latest:
        return None
    pend, ptype, form = latest
    prior_end = (date.fromisoformat(pend[:10]) - timedelta(days=365)).isoformat()

    rev_ttm = _ttm_from_period(conn, symbol, "revenues", pend)
    rev_ttm_yoy = _ttm_from_period(conn, symbol, "revenues", prior_end)
    gp_ttm = _ttm_from_period(conn, symbol, "gross_profit", pend)
    opi_ttm = _ttm_from_period(conn, symbol, "operating_income", pend)
    ni_ttm = _ttm_from_period(conn, symbol, "net_income", pend)
    cfo_ttm = _ttm_from_period(conn, symbol, "cfo", pend)
    capex_ttm = _ttm_from_period(conn, symbol, "capex", pend)
    bb_ttm = _ttm_from_period(conn, symbol, "buybacks_value", pend)

    def _safe_div(a, b):
        if a is None or not b:
            return None
        return a / b

    revenue_yoy_growth = None
    if rev_ttm is not None and rev_ttm_yoy:
        revenue_yoy_growth = (rev_ttm - rev_ttm_yoy) / abs(rev_ttm_yoy)

    fcf_ttm = None
    if cfo_ttm is not None and capex_ttm is not None:
        # SEC tags capex as positive (cash outflow); subtract to get FCF
        fcf_ttm = cfo_ttm - capex_ttm

    # Net debt change: current vs ~4 quarters ago (balance sheet, not TTM)
    bs_rows = conn.execute(
        """SELECT period_end, long_term_debt, cash FROM sec_fundamentals
             WHERE symbol = ? AND long_term_debt IS NOT NULL
             ORDER BY period_end DESC LIMIT 5""",
        (symbol.upper(),),
    ).fetchall()
    net_debt_change_pct = None
    if len(bs_rows) >= 5:
        now_nd = (bs_rows[0][1] or 0) - (bs_rows[0][2] or 0)
        then_nd = (bs_rows[4][1] or 0) - (bs_rows[4][2] or 0)
        if then_nd:
            net_debt_change_pct = (now_nd - then_nd) / abs(then_nd)

    gross_margin = _safe_div(gp_ttm, rev_ttm)
    operating_margin = _safe_div(opi_ttm, rev_ttm)
    net_margin = _safe_div(ni_ttm, rev_ttm)
    fcf_to_revenue = _safe_div(fcf_ttm, rev_ttm)
    buyback_intensity = _safe_div(bb_ttm, rev_ttm)

    def _clip(x, lo=-1.0, hi=1.0):
        if x is None:
            return 0.0
        return max(lo, min(hi, x))

    raw_score = (
        _clip(revenue_yoy_growth)
        + _clip(operating_margin)
        + _clip(fcf_to_revenue)
        + _clip(buyback_intensity)
        - _clip(net_debt_change_pct)
    ) / 5.0

    return {
        "symbol": symbol.upper(),
        "as_of": pend,
        "period_type": ptype,
        "form": form,
        "revenue_ttm": rev_ttm,
        "revenue_yoy_growth": revenue_yoy_growth,
        "gross_margin": gross_margin,
        "operating_margin": operating_margin,
        "net_margin": net_margin,
        "fcf_to_revenue": fcf_to_revenue,
        "buyback_intensity": buyback_intensity,
        "net_debt_change_pct": net_debt_change_pct,
        "raw_score": round(raw_score, 4),
    }


def get_fundamentals_at(conn: sqlite3.Connection, symbol: str,
                         cutoff: str) -> dict:
    """Point-in-time fundamentals lookup — returns the SEC signal as it
    would have been reported on or before `cutoff` (YYYY-MM-DD).

    Distinct from `get_fundamentals_signal`, which always returns the
    latest period. This variant is what walk-forward training/evaluation
    must use to avoid lookahead bias.

    Returns the 6 NN-ready features as a dict (zeros when unknown):
      revenue_yoy_growth, gross_margin, operating_margin,
      fcf_to_revenue, buyback_intensity, net_debt_change_pct
    """
    NN_FIELDS = [
        "revenue_yoy_growth", "gross_margin", "operating_margin",
        "fcf_to_revenue", "buyback_intensity", "net_debt_change_pct",
    ]
    zero = {f: 0.0 for f in NN_FIELDS}

    row = conn.execute(
        """SELECT period_end FROM sec_fundamentals
             WHERE symbol = ? AND revenues IS NOT NULL
               AND period_end <= ?
             ORDER BY period_end DESC LIMIT 1""",
        (symbol.upper(), cutoff),
    ).fetchone()
    if not row:
        return zero
    pend = row[0]
    prior_end = (date.fromisoformat(pend[:10]) - timedelta(days=365)).isoformat()

    rev = _ttm_from_period(conn, symbol, "revenues", pend)
    rev_yoy = _ttm_from_period(conn, symbol, "revenues", prior_end)
    gp = _ttm_from_period(conn, symbol, "gross_profit", pend)
    opi = _ttm_from_period(conn, symbol, "operating_income", pend)
    cfo = _ttm_from_period(conn, symbol, "cfo", pend)
    capex = _ttm_from_period(conn, symbol, "capex", pend)
    bb = _ttm_from_period(conn, symbol, "buybacks_value", pend)

    def _div(a, b):
        if a is None or not b:
            return 0.0
        return a / b

    growth = 0.0
    if rev is not None and rev_yoy:
        growth = (rev - rev_yoy) / abs(rev_yoy)
    fcf = (cfo - capex) if (cfo is not None and capex is not None) else None

    bs = conn.execute(
        """SELECT long_term_debt, cash FROM sec_fundamentals
             WHERE symbol = ? AND period_end <= ?
               AND long_term_debt IS NOT NULL
             ORDER BY period_end DESC LIMIT 5""",
        (symbol.upper(), pend),
    ).fetchall()
    ndc = 0.0
    if len(bs) >= 5:
        now = (bs[0][0] or 0) - (bs[0][1] or 0)
        then = (bs[4][0] or 0) - (bs[4][1] or 0)
        if then:
            ndc = (now - then) / abs(then)

    def clip(x):
        if x is None or not (isinstance(x, (int, float))) or x != x or x in (float("inf"), -float("inf")):
            return 0.0
        return max(-3.0, min(3.0, float(x)))

    return {
        "revenue_yoy_growth":  clip(growth),
        "gross_margin":        clip(_div(gp, rev)),
        "operating_margin":    clip(_div(opi, rev)),
        "fcf_to_revenue":      clip(_div(fcf, rev)),
        "buyback_intensity":   clip(_div(bb, rev)),
        "net_debt_change_pct": clip(ndc),
    }


def augment_dataframe_with_sec(df, conn: sqlite3.Connection):
    """Attach point-in-time SEC features to a DataFrame with `symbol`
    and `as_of` columns. Modifies in place; returns df. Caches lookups
    per (symbol, cutoff) to avoid redundant DB hits across rows."""
    NN_FIELDS = [
        "revenue_yoy_growth", "gross_margin", "operating_margin",
        "fcf_to_revenue", "buyback_intensity", "net_debt_change_pct",
    ]
    cache: dict[tuple[str, str], dict] = {}
    cols = {f: [] for f in NN_FIELDS}
    for sym, as_of in zip(df["symbol"].astype(str), df["as_of"].astype(str)):
        cutoff = as_of[:10]
        key = (sym.upper(), cutoff)
        if key not in cache:
            cache[key] = get_fundamentals_at(conn, sym.upper(), cutoff)
        for f in NN_FIELDS:
            cols[f].append(cache[key][f])
    for f in NN_FIELDS:
        df[f] = cols[f]
    return df


# ── Cross-sectional Frames API (cheap full-universe snapshot) ───────────

def fetch_frame(concept: str, year: int, quarter: int | None = None) -> dict | None:
    """Fetch one concept across all reporting companies for a period.

    If quarter is None, returns the full-year (CY{Y}) frame; otherwise the
    instant-point quarterly frame CY{Y}Q{Q}I.
    Returns the raw JSON with a `data` list of {cik, entityName, val, ...}.
    """
    period = f"CY{year}" if quarter is None else f"CY{year}Q{quarter}I"
    url = f"{BASE}/api/xbrl/frames/us-gaap/{concept}/USD/{period}.json"
    return _get_json(url)


# ── Batch refresh ───────────────────────────────────────────────────────

def refresh_universe(symbols: list[str], limit: int | None = None) -> dict:
    """Fetch + persist fundamentals for each symbol. Returns summary counts."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    conn = sqlite3.connect(str(DB_PATH))
    init_sec_fundamentals_table(conn)

    cik_map = load_cik_map()
    ok = missing_cik = fetch_fail = 0
    total_rows = 0
    to_process = symbols[:limit] if limit else symbols
    for i, sym in enumerate(to_process, 1):
        cik = cik_map.get(sym.upper())
        if not cik:
            missing_cik += 1
            continue
        try:
            facts = fetch_company_facts(cik)
        except requests.HTTPError as e:
            log.warning("fetch failed %s (CIK %s): %s", sym, cik, e)
            fetch_fail += 1
            continue
        if not facts:
            fetch_fail += 1
            continue
        rows = parse_facts_to_rows(facts, sym)
        written = save_rows(conn, rows)
        total_rows += written
        ok += 1
        if i % 50 == 0:
            log.info("[%d/%d] %s → %d rows (cum %d)", i, len(to_process), sym, written, total_rows)

    log.info("refresh_universe done: ok=%d missing_cik=%d fetch_fail=%d rows=%d",
             ok, missing_cik, fetch_fail, total_rows)
    conn.close()
    return {
        "ok": ok, "missing_cik": missing_cik,
        "fetch_fail": fetch_fail, "rows": total_rows,
    }


# ── CLI ─────────────────────────────────────────────────────────────────

def _load_universe_from_cache() -> list[str]:
    """Pull the ticker list out of projector_cache.db's projections table."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM projections ORDER BY symbol"
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="SEC EDGAR fundamentals refresh")
    p.add_argument("--symbols", nargs="+", help="Override ticker list")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--test", action="store_true",
                   help="Sanity-check one ticker (AAPL) and print the signal")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.test:
        conn = sqlite3.connect(str(DB_PATH))
        init_sec_fundamentals_table(conn)
        cik = cik_for("AAPL")
        log.info("AAPL CIK=%s", cik)
        facts = fetch_company_facts(cik)
        rows = parse_facts_to_rows(facts, "AAPL")
        save_rows(conn, rows)
        log.info("Saved %d rows for AAPL", len(rows))
        sig = get_fundamentals_signal(conn, "AAPL")
        log.info("Signal: %s", json.dumps(sig, indent=2, default=str))
        conn.close()
    else:
        symbols = args.symbols or _load_universe_from_cache()
        log.info("Refreshing fundamentals for %d symbols", len(symbols))
        summary = refresh_universe(symbols, limit=args.limit)
        log.info("Summary: %s", summary)
