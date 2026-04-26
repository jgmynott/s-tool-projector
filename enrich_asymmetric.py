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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="+")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--workers", type=int, default=6)
    args = p.parse_args()
    if args.symbols:
        syms = [s.upper() for s in args.symbols]
    else:
        syms = sorted(p.stem for p in PRICES_DIR.glob("*.csv"))
    if args.limit:
        syms = syms[: args.limit]
    if not syms:
        # Diagnostic for the 2026-04-26 mystery: in CI we kept getting
        # "Scoring 0 symbols" despite cache restore reporting success.
        # Print enough to figure out the mismatch on the next run.
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
    enrich(syms, workers=args.workers)


if __name__ == "__main__":
    sys.exit(main() or 0)
