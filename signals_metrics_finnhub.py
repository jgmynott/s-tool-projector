"""Finnhub /stock/metric — 60+ fundamental metrics per ticker.

We already hit Finnhub nightly for earnings + rec-trends; this adds
a second endpoint to the same API budget. /stock/metric?metric=all
returns dozens of derived ratios spanning valuation, profitability,
growth, risk, liquidity, and per-share scale.

Fields worth testing as NN features (subset that's distinct from
yfinance ratios_yf and specifically constructed for equity research):

  - Growth (3-yr, 5-yr, TTM revenue and EPS CAGRs)
  - Capital efficiency (ROIC, ROCE)
  - Valuation multiples averaged over recent quarters (less noisy
    than point-in-time spot values)
  - Operating leverage / net profit margin 5-year averages
  - Cash conversion cycle, days-sales-outstanding
  - Altman Z-score (bankruptcy risk proxy)
  - Piotroski F-score (quality composite, if available)

Free tier: 60 req/min. Shares budget with the earnings fetcher which
also runs at 1.1s sleeps.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(Path(__file__).parent / ".env")
except Exception:
    pass

log = logging.getLogger("metrics-finnhub")
DB_PATH = Path(__file__).parent / "projector_cache.db"
API_KEY = os.getenv("FINNHUB_API_KEY", "")
BASE = "https://finnhub.io/api/v1"
TIMEOUT = 15
USER_AGENT = "s-tool/1.0 (james@s-tool.io)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics_finnhub (
    symbol                   TEXT NOT NULL,
    fetched_at               TEXT NOT NULL,
    fetched_date             TEXT NOT NULL,

    -- Valuation (trailing, averaged)
    pe_ttm                   REAL,
    pe_annual                REAL,
    pb_quarterly             REAL,
    ps_ttm                   REAL,
    ev_to_ebitda_ttm         REAL,
    price_to_fcf_ttm         REAL,

    -- Growth (compounded)
    rev_growth_3y            REAL,
    rev_growth_5y            REAL,
    eps_growth_3y            REAL,
    eps_growth_5y            REAL,
    eps_growth_ttm_yoy       REAL,

    -- Profitability (multi-year averages dampen outlier quarters)
    roic_ttm                 REAL,
    roe_ttm                  REAL,
    roa_ttm                  REAL,
    gross_margin_5y_avg      REAL,
    operating_margin_5y_avg  REAL,
    net_margin_5y_avg        REAL,
    fcf_margin_ttm           REAL,

    -- Quality / risk
    current_ratio_quarterly  REAL,
    quick_ratio_quarterly    REAL,
    long_term_debt_to_equity REAL,
    total_debt_to_equity     REAL,

    -- Price performance (context for 1y prediction)
    price_52w_return         REAL,
    price_26w_return         REAL,
    price_13w_return         REAL,
    price_4w_return          REAL,
    beta                     REAL,

    -- Operating intensity
    asset_turnover_ttm       REAL,
    inventory_turnover_ttm   REAL,

    PRIMARY KEY (symbol, fetched_date)
);
CREATE INDEX IF NOT EXISTS idx_metrics_fh_symbol ON metrics_finnhub(symbol);
CREATE INDEX IF NOT EXISTS idx_metrics_fh_date   ON metrics_finnhub(fetched_date);
"""

# Map our column name → the Finnhub response key (metric.metric.<key>)
FIELD_MAP = {
    "pe_ttm":                   "peTTM",
    "pe_annual":                "peAnnual",
    "pb_quarterly":             "pbQuarterly",
    "ps_ttm":                   "psTTM",
    "ev_to_ebitda_ttm":         "currentEv/freeCashFlowTTM",  # closest common proxy
    "price_to_fcf_ttm":         "pfcfShareTTM",
    "rev_growth_3y":            "revenueGrowth3Y",
    "rev_growth_5y":            "revenueGrowth5Y",
    "eps_growth_3y":            "epsGrowth3Y",
    "eps_growth_5y":            "epsGrowth5Y",
    "eps_growth_ttm_yoy":       "epsGrowthTTMYoy",
    "roic_ttm":                 "roicTTM",
    "roe_ttm":                  "roeTTM",
    "roa_ttm":                  "roaTTM",
    "gross_margin_5y_avg":      "grossMargin5Y",
    "operating_margin_5y_avg":  "operatingMargin5Y",
    "net_margin_5y_avg":        "netProfitMargin5Y",
    "fcf_margin_ttm":           "fcfMargin1Y",
    "current_ratio_quarterly":  "currentRatioQuarterly",
    "quick_ratio_quarterly":    "quickRatioQuarterly",
    "long_term_debt_to_equity": "longTermDebt/equityQuarterly",
    "total_debt_to_equity":     "totalDebt/totalEquityQuarterly",
    "price_52w_return":         "52WeekPriceReturnDaily",
    "price_26w_return":         "26WeekPriceReturnDaily",
    "price_13w_return":         "13WeekPriceReturnDaily",
    "price_4w_return":          "monthToDatePriceReturnDaily",
    "beta":                     "beta",
    "asset_turnover_ttm":       "assetTurnoverTTM",
    "inventory_turnover_ttm":   "inventoryTurnoverTTM",
}


def init_table(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def fetch_symbol(symbol: str) -> dict | None:
    if not API_KEY:
        return None
    try:
        r = requests.get(f"{BASE}/stock/metric",
                         params={"symbol": symbol.upper(),
                                 "metric": "all",
                                 "token": API_KEY},
                         headers={"User-Agent": USER_AGENT},
                         timeout=TIMEOUT)
        if r.status_code == 429:
            time.sleep(2.0)
            return None
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        log.debug("%s: finnhub metric error: %s", symbol, e)
        return None

    metric = (body or {}).get("metric") or {}
    if not metric:
        return None

    row = {
        "symbol": symbol.upper(),
        "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
        "fetched_date": datetime.now(tz=timezone.utc).date().isoformat(),
    }
    # Fields may arrive as floats, ints, or missing. Coerce defensively.
    for col, src in FIELD_MAP.items():
        v = metric.get(src)
        if v is None:
            row[col] = None
            continue
        try:
            row[col] = float(v)
        except (TypeError, ValueError):
            row[col] = None

    return row


INSERT_COLS = [
    "symbol", "fetched_at", "fetched_date",
    *FIELD_MAP.keys(),
]


def save_row(conn: sqlite3.Connection, row: dict) -> None:
    ph = ",".join(["?"] * len(INSERT_COLS))
    conn.execute(
        f"INSERT OR REPLACE INTO metrics_finnhub ({','.join(INSERT_COLS)}) VALUES ({ph})",
        tuple(row.get(c) for c in INSERT_COLS),
    )


def refresh_universe(symbols: Iterable[str], sleep_s: float = 1.1) -> dict:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not API_KEY:
        log.warning("FINNHUB_API_KEY not set — skipping")
        return {"status": "no_key"}
    conn = sqlite3.connect(str(DB_PATH))
    init_table(conn)
    got = missing = errors = 0
    syms = list(symbols)
    for i, sym in enumerate(syms):
        try:
            row = fetch_symbol(sym)
            if row is None:
                missing += 1
            else:
                save_row(conn, row)
                got += 1
        except Exception as e:
            log.debug("%s: %s", sym, e)
            errors += 1
        if (i + 1) % 50 == 0:
            conn.commit()
            log.info("progress: %d/%d  got=%d miss=%d err=%d",
                     i + 1, len(syms), got, missing, errors)
        time.sleep(sleep_s)
    conn.commit()
    summary = {"total": len(syms), "got": got, "missing": missing, "errors": errors}
    log.info("refresh done: %s", summary)
    return summary


def _main_cli() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", choices=["preferred", "full", "custom"],
                        default="preferred")
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument("--sleep", type=float, default=1.1)
    args = parser.parse_args()

    if args.symbols:
        syms = [s.upper() for s in args.symbols]
    else:
        import worker
        if args.universe == "preferred":
            syms = sorted(set(worker.SP500_NDX100 + worker.WSB_UNIVERSE))
        elif args.universe == "full":
            syms = worker.FULL_UNIVERSE
        else:
            raise SystemExit("--symbols required with --universe custom")

    refresh_universe(syms, sleep_s=args.sleep)


if __name__ == "__main__":
    _main_cli()
