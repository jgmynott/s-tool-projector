"""
Nightly EWMA-vol asymmetric-upside scoring.

For every ticker in the cached price universe, runs a 1-year Monte Carlo
projection using an EWMA(λ=0.94) volatility estimator (RiskMetrics
standard) instead of simple stdev. The EWMA weights recent returns more
heavily, which widens the forecast tail when vol is actually rising —
i.e. exactly the regime in which 2x moves are possible.

The backtest sweep (upside_hunt.py) showed this scoring method
produces a 2.02× lift on +100% 12-month returns vs. the universe
baseline, with +19.5% mean return across top-20 picks.

Output: data_cache/asymmetric_scores.json
    { "AAPL": {"p10_ratio": 0.8, "p50_ratio": 1.05, "p90_ratio": 1.28,
               "score": 1.28, "computed_at": ts}, ... }

portfolio_scanner reads this file and emits a new "asymmetric" tier on
/picks containing the top-10 by score.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("enrich_asymmetric")

ROOT = Path(__file__).parent
PRICES_DIR = ROOT / "data_cache" / "prices"
CACHE_PATH = ROOT / "data_cache" / "asymmetric_scores.json"
HORIZON_DAYS = 252
PATHS = 2000


def _load_prices(sym: str) -> pd.Series | None:
    path = PRICES_DIR / f"{sym}.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
        s = df["Close"].dropna().sort_index()
        return s if len(s) >= 504 else None
    except Exception:
        return None


def _score_one(sym: str) -> tuple[str, dict | None]:
    prices = _load_prices(sym)
    if prices is None:
        return (sym, None)
    lookback = prices.tail(504)
    log_rets = np.log(lookback / lookback.shift(1)).dropna().values
    mu = float(np.mean(log_rets) * 252)
    # EWMA volatility — λ=0.94 RiskMetrics
    lam = 0.94
    weights = np.array([lam ** i for i in range(len(log_rets))][::-1])
    weights /= weights.sum()
    var = float(np.sum(weights * log_rets * log_rets))
    sigma = float(np.sqrt(var * 252))
    if sigma < 0.05:
        return (sym, None)
    current = float(lookback.iloc[-1])
    dt = 1.0 / 252
    # Fresh seed per symbol so picks aren't correlated by RNG state.
    rng = np.random.default_rng(hash(sym) & 0xFFFFFFFF)
    Z = rng.standard_normal((PATHS, HORIZON_DAYS))
    inc = (mu - 0.5 * sigma * sigma) * dt + sigma * np.sqrt(dt) * Z
    terminal = current * np.exp(np.cumsum(inc, axis=1)[:, -1])
    p10 = float(np.percentile(terminal, 10))
    p50 = float(np.percentile(terminal, 50))
    p90 = float(np.percentile(terminal, 90))
    return (sym, {
        "p10_ratio": p10 / current if current else 0,
        "p50_ratio": p50 / current if current else 0,
        "p90_ratio": p90 / current if current else 0,
        "score": p90 / current if current else 0,    # raw H7 score
        "sigma_ewma": sigma,
        "computed_at": time.time(),
    })


def enrich(symbols: list[str], workers: int = 6) -> dict:
    log.info("Scoring %d symbols (workers=%d)", len(symbols), workers)
    out: dict[str, dict] = {}
    ok = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_score_one, s): s for s in symbols}
        done = 0
        for fut in as_completed(futures):
            sym, data = fut.result()
            done += 1
            if data:
                out[sym] = data
                ok += 1
            else:
                fail += 1
            if done % 200 == 0 or done == len(symbols):
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                eta = (len(symbols) - done) / rate if rate > 0 else 0
                log.info("[%d/%d] ok=%d fail=%d · %.1f/s · eta %.1fmin",
                         done, len(symbols), ok, fail, rate, eta / 60)
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(out, separators=(",", ":")))
    log.info("Wrote %d scores to %s", len(out), CACHE_PATH)
    return out


def _fetch_one_to_csv(sym: str) -> bool:
    """Pull 2y daily OHLCV via DataManager and persist as
    data_cache/prices/<SYM>.csv. Returns True iff a CSV with >=504 rows
    is on disk after this call (either pre-existing or freshly written).

    Schema matches backfill_prices_historical.py: Date,Open,High,Low,Close,Volume.
    """
    out = PRICES_DIR / f"{sym}.csv"
    if out.exists():
        try:
            existing = pd.read_csv(out)
            if len(existing) >= 504:
                return True
        except Exception:
            pass  # fall through and refetch

    try:
        # Lazy import — DataManager pulls in numpy/yfinance and shouldn't
        # be loaded just to read existing CSVs.
        from data_providers import DataManager
        # 3 years (~756 rows) gives comfortable headroom over the 504-row
        # scoring threshold; 2 years lands at ~501 rows once weekends and
        # holidays are taken out, which is just under the cutoff.
        rows = DataManager().get_historical(sym, years=3)
    except Exception as exc:
        log.warning("self-heal: %s DataManager.get_historical raised: %s", sym, exc)
        return False
    if not rows or len(rows) < 504:
        return False
    rows_sorted = sorted(rows, key=lambda r: r["date"])
    df = pd.DataFrame([
        {"Date": r["date"], "Open": r.get("open"), "High": r.get("high"),
         "Low": r.get("low"), "Close": r.get("close"), "Volume": r.get("volume")}
        for r in rows_sorted
    ])
    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    return True


def _self_heal_prices(workers: int = 6) -> list[str]:
    """When PRICES_DIR is missing or empty (CI cache eviction), fetch the
    preferred universe (SP500 ∪ NDX100 ∪ WSB) via the data-providers layer
    and write CSVs so subsequent runs can read them from cache. Returns
    the list of symbols with usable CSVs after the heal.

    First-run cost on a cold cache: ~3–5 min for ~600 symbols at workers=6
    (yfinance is unrate-limited; FMP fallback is 250 req/min). Subsequent
    runs hit the existing CSVs and skip this entirely.
    """
    # The preferred universe is what worker.py --preferred scans, so
    # everything enrich_asymmetric needs to score is in here.
    from worker import SP500_NDX100, WSB_UNIVERSE
    universe = sorted(set(SP500_NDX100 + WSB_UNIVERSE))
    log.warning("self-heal: PRICES_DIR empty — fetching %d symbols from data providers (~3-5 min)", len(universe))

    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    healed: list[str] = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_fetch_one_to_csv, s): s for s in universe}
        done = 0
        for fut in as_completed(futures):
            sym = futures[fut]
            done += 1
            try:
                if fut.result():
                    healed.append(sym)
            except Exception as exc:
                log.warning("self-heal: %s failed: %s", sym, exc)
            if done % 50 == 0 or done == len(universe):
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                log.info("self-heal [%d/%d] healed=%d · %.1f/s",
                         done, len(universe), len(healed), rate)
    log.warning("self-heal: wrote %d/%d price CSVs in %.1fs", len(healed), len(universe), time.time() - t0)
    return sorted(healed)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--no-self-heal", action="store_true",
                    help="Disable the data-providers fallback when PRICES_DIR is empty.")
    args = ap.parse_args()
    if args.symbols:
        syms = [s.upper() for s in args.symbols]
    else:
        syms = sorted(p.stem for p in PRICES_DIR.glob("*.csv"))

    # Self-heal: when CI's cache eviction has wiped data_cache/prices/ the
    # nightly slow-path no longer repopulates it (the Russell 3000 backfill
    # was removed for cost reasons). Rebuild from data-providers so the
    # asymmetric tier doesn't silently ship empty.
    if not syms and not args.no_self_heal:
        log.error(
            "no symbols to score. PRICES_DIR=%s exists=%s is_dir=%s",
            PRICES_DIR, PRICES_DIR.exists(), PRICES_DIR.is_dir() if PRICES_DIR.exists() else None,
        )
        if PRICES_DIR.exists():
            entries = list(PRICES_DIR.iterdir())
            log.error("PRICES_DIR has %d entries; first 10: %s",
                      len(entries), [e.name for e in entries[:10]])
        else:
            parent = PRICES_DIR.parent
            log.error("PRICES_DIR parent %s exists=%s; siblings: %s",
                      parent, parent.exists(),
                      [p.name for p in parent.iterdir()][:10] if parent.exists() else 'n/a')
        syms = _self_heal_prices(workers=args.workers)

    if args.limit:
        syms = syms[: args.limit]
    if not syms:
        log.error("self-heal also produced 0 symbols — asymmetric tier will be empty this run")
    enrich(syms, workers=args.workers)


if __name__ == "__main__":
    sys.exit(main() or 0)
