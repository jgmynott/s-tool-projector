"""
Background precomputation worker.

Runs projections for a watchlist of symbols and caches results in SQLite.
Designed to run as a daily cron job after market close.

Usage:
    python worker.py                     # default watchlist, 252-day horizon
    python worker.py --symbols AAPL TSLA # specific symbols
    python worker.py --horizons 63 252   # multiple horizons
    python worker.py --all               # full 107-symbol universe
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone

from db import init_db, save_projection, get_projection_age_hours
from projector_engine import run_projection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("worker")

# Default watchlist — popular tickers that get precomputed daily
DEFAULT_WATCHLIST = [
    "SPY", "QQQ", "IWM", "DIA",          # index ETFs
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",  # mega-cap
    "AMD", "INTC", "MU", "AVGO",          # semis
    "JPM", "BAC", "GS",                   # financials
    "XOM", "CVX",                         # energy
    "NFLX", "DIS", "NKE",                 # consumer
    "COIN", "PLTR", "SOFI", "GME", "AMC", # WSB favorites
]

# WSB sentiment universe (107 symbols — used for sentiment backtest)
WSB_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "META", "TSLA", "BRK-B", "AVGO",
    "GME", "AMC", "BB", "PLTR", "SOFI", "CLOV", "SPCE", "RIVN",
    "LCID", "NIO", "COIN", "HOOD", "MSTR",
    "AMD", "INTC", "MU", "QCOM", "TSM", "AMAT", "LRCX", "KLAC", "MRVL", "ARM",
    "CRM", "SNOW", "NET", "DDOG", "CRWD", "ZS", "PANW", "SHOP", "XYZ", "ROKU",
    "U", "RBLX", "UBER", "ABNB", "DASH",
    "JPM", "BAC", "GS", "MS", "C", "WFC", "V", "MA", "PYPL", "AXP",
    "JNJ", "PFE", "MRNA", "BNTX", "LLY", "UNH", "ABBV",
    "XOM", "CVX", "OXY", "SLB", "COP",
    "DIS", "NFLX", "NKE", "SBUX", "MCD", "WMT", "COST", "TGT", "HD", "LOW",
    "BA", "CAT", "DE", "UPS", "FDX", "LMT", "RTX",
    "SPY", "QQQ", "IWM", "DIA", "ARKK", "XLF", "XLE", "XLK", "GLD", "SLV", "TLT",
]

# S&P 500 + Nasdaq 100 (516 unique tickers — full projection universe)
SP500_NDX100 = [
    "A", "AAPL", "ABBV", "ABNB", "ABT", "ACGL", "ACN", "ADBE", "ADI", "ADM",
    "ADP", "ADSK", "AEE", "AEP", "AES", "AFL", "AIG", "AIZ", "AJG", "AKAM",
    "ALB", "ALGN", "ALL", "ALLE", "ALNY", "AMAT", "AMCR", "AMD", "AME", "AMGN",
    "AMP", "AMT", "AMZN", "ANET", "AON", "AOS", "APA", "APD", "APH", "APO",
    "APP", "APTV", "ARE", "ARES", "ARM", "ASML", "ATO", "AVB", "AVGO", "AVY",
    "AWK", "AXON", "AXP", "AZO", "BA", "BAC", "BALL", "BAX", "BBY", "BDX",
    "BEN", "BG", "BIIB", "BK", "BKNG", "BKR", "BLDR", "BLK", "BMY",
    "BR", "BRK-B", "BRO", "BSX", "BX", "BXP", "C", "CAG", "CAH", "CARR",
    "CASY", "CAT", "CB", "CBRE", "CCEP", "CCI", "CCL", "CDNS", "CDW", "CEG",
    "CF", "CFG", "CHD", "CHRW", "CHTR", "CI", "CIEN", "CINF", "CL", "CLX",
    "CMCSA", "CME", "CMG", "CMI", "CMS", "CNC", "CNP", "COF", "COHR", "COIN",
    "COO", "COP", "COR", "COST", "CPAY", "CPB", "CPRT", "CPT", "CRH", "CRL",
    "CRM", "CRWD", "CSCO", "CSGP", "CSX", "CTAS", "CTRA", "CTSH", "CTVA", "CVNA",
    "CVS", "CVX", "D", "DAL", "DASH", "DD", "DDOG", "DE", "DECK", "DELL",
    "DG", "DGX", "DHI", "DHR", "DIS", "DLR", "DLTR", "DOC", "DOV", "DOW",
    "DPZ", "DRI", "DTE", "DUK", "DVA", "DVN", "DXCM", "EA", "EBAY", "ECL",
    "ED", "EFX", "EG", "EIX", "EL", "ELV", "EME", "EMR", "EOG", "EPAM",
    "EQIX", "EQR", "EQT", "ERIE", "ES", "ESS", "ETN", "ETR", "EVRG", "EW",
    "EXC", "EXE", "EXPD", "EXPE", "EXR", "F", "FANG", "FAST", "FCX", "FDS",
    "FDX", "FE", "FER", "FFIV", "FICO", "FIS", "FISV", "FITB", "FIX", "FOX",
    "FOXA", "FRT", "FSLR", "FTNT", "FTV", "GD", "GDDY", "GE", "GEHC", "GEN",
    "GEV", "GILD", "GIS", "GL", "GLW", "GM", "GNRC", "GOOG", "GOOGL", "GPC",
    "GPN", "GRMN", "GS", "GWW", "HAL", "HAS", "HBAN", "HCA", "HD", "HIG",
    "HII", "HLT", "HON", "HOOD", "HPE", "HPQ", "HRL", "HSIC", "HST", "HSY",
    "HUBB", "HUM", "HWM", "IBKR", "IBM", "ICE", "IDXX", "IEX", "IFF", "INCY",
    "INSM", "INTC", "INTU", "INVH", "IP", "IQV", "IR", "IRM", "ISRG", "IT",
    "ITW", "IVZ", "J", "JBHT", "JBL", "JCI", "JKHY", "JNJ", "JPM", "KDP",
    "KEY", "KEYS", "KHC", "KIM", "KKR", "KLAC", "KMB", "KMI", "KO", "KR",
    "KVUE", "L", "LDOS", "LEN", "LH", "LHX", "LII", "LIN", "LITE", "LLY",
    "LMT", "LNT", "LOW", "LRCX", "LULU", "LUV", "LVS", "LYB", "LYV", "MA",
    "MAA", "MAR", "MAS", "MCD", "MCHP", "MCK", "MCO", "MDLZ", "MDT", "MELI",
    "MET", "META", "MGM", "MKC", "MLM", "MMM", "MNST", "MO", "MOS", "MPC",
    "MPWR", "MRK", "MRNA", "MRVL", "MS", "MSCI", "MSFT", "MSI", "MSTR",
    "MTB", "MTD", "MU", "NCLH", "NDAQ", "NDSN", "NEE", "NEM", "NFLX", "NI",
    "NKE", "NOC", "NOW", "NRG", "NSC", "NTAP", "NTRS", "NUE", "NVDA", "NVR",
    "NWS", "NWSA", "NXPI", "O", "ODFL", "OKE", "OMC", "ON", "ORCL", "ORLY",
    "OTIS", "OXY", "PANW", "PAYX", "PCAR", "PCG", "PDD", "PEG", "PEP", "PFE",
    "PFG", "PG", "PGR", "PH", "PHM", "PKG", "PLD", "PLTR", "PM", "PNC",
    "PNR", "PNW", "PODD", "POOL", "PPG", "PPL", "PRU", "PSA", "PSX",
    "PTC", "PWR", "PYPL", "QCOM", "RCL", "REG", "REGN", "RF", "RJF",
    "RL", "RMD", "ROK", "ROL", "ROP", "ROST", "RSG", "RTX", "RVTY",
    "SBAC", "SBUX", "SCHW", "SHOP", "SHW", "SJM", "SLB", "SMCI", "SNA",
    "SNPS", "SO", "SOLV", "SPG", "SPGI", "SRE", "STE", "STLD", "STT", "STX",
    "STZ", "SW", "SWK", "SWKS", "SYF", "SYK", "SYY", "T", "TAP", "TDG",
    "TDY", "TEAM", "TECH", "TEL", "TER", "TFC", "TGT", "TJX", "TKO", "TMO",
    "TMUS", "TPL", "TPR", "TRGP", "TRI", "TRMB", "TROW", "TRV", "TSCO", "TSLA",
    "TSN", "TT", "TTD", "TTWO", "TXN", "TXT", "TYL", "UAL", "UBER", "UDR",
    "UHS", "ULTA", "UNH", "UNP", "UPS", "URI", "USB", "V", "VICI", "VLO",
    "VLTO", "VMC", "VRSK", "VRSN", "VRT", "VRTX", "VST", "VTR", "VTRS", "VZ",
    "WAB", "WAT", "WBD", "WDAY", "WDC", "WEC", "WELL", "WFC", "WM", "WMB",
    "WMT", "WRB", "WSM", "WST", "WTW", "WY", "WYNN", "XEL", "XOM", "XYL",
    "XYZ", "YUM", "ZBH", "ZBRA", "ZS", "ZTS",
]

# Combined: S&P 500 + Nasdaq 100 + WSB favorites (deduplicated)
FULL_UNIVERSE = sorted(set(SP500_NDX100 + WSB_UNIVERSE))

STALE_HOURS = 18  # skip symbols computed within this window
DEFAULT_HORIZONS = [252]


def run_worker(symbols: list[str], horizons: list[int], force: bool = False):
    conn = init_db()
    total = len(symbols) * len(horizons)
    done = 0
    skipped = 0
    failed = 0
    t0 = time.time()

    log.info(f"Worker starting: {len(symbols)} symbols × {len(horizons)} horizons = {total} projections")

    for sym in symbols:
        for h in horizons:
            done += 1

            # Check freshness
            if not force:
                age = get_projection_age_hours(conn, sym, h)
                if age is not None and age < STALE_HOURS:
                    log.info(f"[{done}/{total}] SKIP {sym} h={h} (age={age:.1f}h)")
                    skipped += 1
                    continue

            log.info(f"[{done}/{total}] RUN  {sym} h={h}")
            try:
                result = run_projection(sym, horizon_days=h)
                save_projection(conn, result)
                log.info(f"  → {sym} done in {result['compute_secs']:.1f}s  "
                         f"p50=${result['p50']}  upside={result['upside_prob']:.0%}")
            except Exception as e:
                log.warning(f"  → {sym} FAILED: {e}")
                failed += 1

    elapsed = time.time() - t0
    log.info(
        f"Worker done: {total} total, {total - skipped - failed} computed, "
        f"{skipped} skipped, {failed} failed, {elapsed:.0f}s elapsed"
    )
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Precompute stock projections")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--symbols", nargs="+", help="Specific symbols to compute")
    group.add_argument("--all", action="store_true", help="Full 107-symbol universe")
    parser.add_argument("--horizons", nargs="+", type=int, default=DEFAULT_HORIZONS,
                        help="Horizon days (default: 252)")
    parser.add_argument("--force", action="store_true", help="Recompute even if fresh")
    args = parser.parse_args()

    if args.all:
        symbols = FULL_UNIVERSE
    elif args.symbols:
        symbols = [s.upper() for s in args.symbols]
    else:
        symbols = DEFAULT_WATCHLIST

    run_worker(symbols, args.horizons, args.force)


if __name__ == "__main__":
    main()
