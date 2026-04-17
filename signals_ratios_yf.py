"""Fundamental ratios from yfinance — the 20+ fields we're not using.

yfinance Ticker.info carries a huge dict of fundamental ratios
(valuation, profitability, balance sheet, growth, ownership,
technicals, analyst aggregates). We've been leaving it on the floor.
This module extracts the fields most likely to help a 1-year return
predictor and writes them to a dated table. Each night's snapshot
upserts; accumulation gives us a time-series per ticker over weeks.

Free. No API key. ~0.3s per ticker.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# dotenv for local dev
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(Path(__file__).parent / ".env")
except Exception:
    pass

log = logging.getLogger("ratios-yf")
DB_PATH = Path(__file__).parent / "projector_cache.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ratios_yf (
    symbol                   TEXT NOT NULL,
    fetched_at               TEXT NOT NULL,
    fetched_date             TEXT NOT NULL,

    -- Valuation
    trailing_pe              REAL,
    forward_pe               REAL,
    peg_ratio                REAL,
    price_to_book            REAL,
    price_to_sales_ttm       REAL,
    ev_to_ebitda             REAL,
    ev_to_revenue            REAL,

    -- Profitability
    return_on_equity         REAL,
    return_on_assets         REAL,
    gross_margin             REAL,
    operating_margin         REAL,
    profit_margin            REAL,
    ebitda_margin            REAL,

    -- Balance sheet
    debt_to_equity           REAL,
    current_ratio            REAL,
    quick_ratio              REAL,
    total_cash_per_share     REAL,
    book_value               REAL,

    -- Growth (YoY)
    earnings_growth          REAL,
    revenue_growth           REAL,

    -- Ownership
    held_pct_insiders        REAL,
    held_pct_institutions    REAL,

    -- Risk / technicals
    beta                     REAL,
    fifty_day_average        REAL,
    two_hundred_day_average  REAL,
    fifty_two_week_high      REAL,
    fifty_two_week_low       REAL,
    dist_from_52w_high_pct   REAL,    -- (current / 52w_high) - 1

    -- Dividend
    dividend_yield           REAL,
    payout_ratio             REAL,

    -- Analyst
    target_mean_price        REAL,
    target_high_price        REAL,
    target_low_price         REAL,
    recommendation_mean      REAL,
    number_of_analyst_opinions INTEGER,

    -- Scale
    market_cap               INTEGER,
    enterprise_value         INTEGER,
    shares_outstanding       INTEGER,

    PRIMARY KEY (symbol, fetched_date)
);
CREATE INDEX IF NOT EXISTS idx_ratios_yf_symbol ON ratios_yf(symbol);
CREATE INDEX IF NOT EXISTS idx_ratios_yf_date   ON ratios_yf(fetched_date);
"""

FIELD_MAP = {
    "trailing_pe":             "trailingPE",
    "forward_pe":              "forwardPE",
    "peg_ratio":               "pegRatio",
    "price_to_book":           "priceToBook",
    "price_to_sales_ttm":      "priceToSalesTrailing12Months",
    "ev_to_ebitda":            "enterpriseToEbitda",
    "ev_to_revenue":           "enterpriseToRevenue",
    "return_on_equity":        "returnOnEquity",
    "return_on_assets":        "returnOnAssets",
    "gross_margin":            "grossMargins",
    "operating_margin":        "operatingMargins",
    "profit_margin":           "profitMargins",
    "ebitda_margin":           "ebitdaMargins",
    "debt_to_equity":          "debtToEquity",
    "current_ratio":           "currentRatio",
    "quick_ratio":             "quickRatio",
    "total_cash_per_share":    "totalCashPerShare",
    "book_value":              "bookValue",
    "earnings_growth":         "earningsGrowth",
    "revenue_growth":          "revenueGrowth",
    "held_pct_insiders":       "heldPercentInsiders",
    "held_pct_institutions":   "heldPercentInstitutions",
    "beta":                    "beta",
    "fifty_day_average":       "fiftyDayAverage",
    "two_hundred_day_average": "twoHundredDayAverage",
    "fifty_two_week_high":     "fiftyTwoWeekHigh",
    "fifty_two_week_low":      "fiftyTwoWeekLow",
    "dividend_yield":          "dividendYield",
    "payout_ratio":            "payoutRatio",
    "target_mean_price":       "targetMeanPrice",
    "target_high_price":       "targetHighPrice",
    "target_low_price":        "targetLowPrice",
    "recommendation_mean":     "recommendationMean",
    "number_of_analyst_opinions": "numberOfAnalystOpinions",
    "market_cap":              "marketCap",
    "enterprise_value":        "enterpriseValue",
    "shares_outstanding":      "sharesOutstanding",
}


def init_table(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def fetch_symbol(symbol: str) -> dict | None:
    import yfinance as yf
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception as e:
        log.debug("%s: yfinance error: %s", symbol, e)
        return None

    # Skip if we got almost nothing back
    if not info or info.get("marketCap") is None:
        return None

    row = {
        "symbol": symbol.upper(),
        "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
        "fetched_date": datetime.now(tz=timezone.utc).date().isoformat(),
    }
    for col, src in FIELD_MAP.items():
        v = info.get(src)
        if v is None:
            row[col] = None
            continue
        try:
            if col in ("market_cap", "enterprise_value", "shares_outstanding",
                       "number_of_analyst_opinions"):
                row[col] = int(v)
            else:
                row[col] = float(v)
        except (TypeError, ValueError):
            row[col] = None

    # Derived: distance from 52-week high (momentum / drawdown proxy)
    cur = info.get("currentPrice") or info.get("regularMarketPrice")
    hi = row.get("fifty_two_week_high")
    if cur and hi and hi > 0:
        row["dist_from_52w_high_pct"] = round(cur / hi - 1.0, 4)
    else:
        row["dist_from_52w_high_pct"] = None

    return row


COLS = (["symbol", "fetched_at", "fetched_date"]
        + list(FIELD_MAP.keys())
        + ["dist_from_52w_high_pct"])
# dist_from_52w_high_pct sits between the ownership cols and dividend
# in the schema, so we resort COLS to the schema order for the INSERT:
INSERT_COLS = [
    "symbol", "fetched_at", "fetched_date",
    "trailing_pe", "forward_pe", "peg_ratio", "price_to_book",
    "price_to_sales_ttm", "ev_to_ebitda", "ev_to_revenue",
    "return_on_equity", "return_on_assets", "gross_margin",
    "operating_margin", "profit_margin", "ebitda_margin",
    "debt_to_equity", "current_ratio", "quick_ratio",
    "total_cash_per_share", "book_value",
    "earnings_growth", "revenue_growth",
    "held_pct_insiders", "held_pct_institutions",
    "beta", "fifty_day_average", "two_hundred_day_average",
    "fifty_two_week_high", "fifty_two_week_low", "dist_from_52w_high_pct",
    "dividend_yield", "payout_ratio",
    "target_mean_price", "target_high_price", "target_low_price",
    "recommendation_mean", "number_of_analyst_opinions",
    "market_cap", "enterprise_value", "shares_outstanding",
]


def save_row(conn: sqlite3.Connection, row: dict) -> None:
    ph = ",".join(["?"] * len(INSERT_COLS))
    conn.execute(
        f"INSERT OR REPLACE INTO ratios_yf ({','.join(INSERT_COLS)}) VALUES ({ph})",
        tuple(row.get(c) for c in INSERT_COLS),
    )


def refresh_universe(symbols: Iterable[str], sleep_s: float = 0.3) -> dict:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
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
    parser.add_argument("--sleep", type=float, default=0.3)
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
