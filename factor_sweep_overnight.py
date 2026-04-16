"""
Overnight factor sweep: momentum + value + quality.

Tests three classic Fama-French-style factors as projection tilts:
  1. Momentum (12-1): already tested in momentum_factor_backtest.py, included
     here for a combined report.
  2. Value: earnings yield (1/PE) from cached fundamentals. High earnings yield
     = cheap = positive tilt.
  3. Quality: ROE from cached fundamentals. High ROE = quality = positive tilt.

Each factor is tested independently (not combined) at 5 tilt strengths on
the full cached universe across 2022-2024 quarterly windows.

Runs ~2-4 hours depending on cache coverage. Output:
  factor_sweep_results.csv
  factor_sweep_report.md
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s")
log = logging.getLogger("factors")

CACHE_DIR = Path(__file__).parent / "data_cache" / "prices"
DB_PATH = Path(__file__).parent / "projector_cache.db"
OUT_CSV = Path(__file__).parent / "factor_sweep_results.csv"
OUT_REPORT = Path(__file__).parent / "factor_sweep_report.md"

TILT_STRENGTHS = [0.0, 0.03, 0.06, 0.09, 0.12]
HORIZONS = {"6mo": 126, "1yr": 252}


def load_prices(symbol: str) -> pd.Series | None:
    path = CACHE_DIR / f"{symbol}.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
        if "Close" not in df.columns:
            return None
        s = df["Close"].dropna().sort_index()
        return s if len(s) >= 504 else None
    except Exception:
        return None


def load_fundamentals(conn: sqlite3.Connection, symbol: str) -> dict | None:
    """Load latest fundamentals_json from projection cache."""
    row = conn.execute(
        "SELECT fundamentals_json FROM projections WHERE symbol = ? "
        "ORDER BY run_date DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0]) if isinstance(row[0], str) else row[0]
    except (json.JSONDecodeError, TypeError):
        return None


def compute_momentum(prices: pd.Series, as_of: pd.Timestamp) -> float | None:
    end = as_of - timedelta(days=30)
    start = as_of - timedelta(days=365)
    mask = (prices.index >= start) & (prices.index <= end)
    w = prices[mask]
    if len(w) < 120:
        return None
    return (w.iloc[-1] / w.iloc[0]) - 1.0


def compute_value(fundamentals: dict) -> float | None:
    """Earnings yield = 1 / PE. Higher = cheaper = better value."""
    pe = (
        fundamentals.get("pe_trailing")
        or fundamentals.get("priceToEarningsRatio")
        or fundamentals.get("pe_forward")
    )
    if pe is None:
        return None
    if isinstance(pe, str):
        try: pe = float(pe)
        except ValueError: return None
    if pe <= 0 or pe > 500:
        return None
    return 1.0 / pe


def compute_quality(fundamentals: dict) -> float | None:
    """Low-beta quality proxy (Frazzini-Pedersen 'Betting Against Beta').

    Original plan was ROE, but the cached fundamentals pipeline stopped
    populating ROE/margins after the FMP v3→stable migration (0% coverage).
    Beta has 98% coverage and low-beta is an independent academic anomaly,
    so we score: quality = 1 / (1 + beta). Lower beta = higher quality score.
    """
    beta = fundamentals.get("beta")
    if beta is None:
        return None
    if isinstance(beta, str):
        try: beta = float(beta)
        except ValueError: return None
    if beta < -5 or beta > 10:  # strip outliers
        return None
    return 1.0 / (1.0 + float(beta))


def project_tilted(
    prices: pd.Series,
    as_of: pd.Timestamp,
    horizon_days: int,
    tilt_strength: float,
    quintile: int,
) -> float | None:
    lookback = prices[prices.index <= as_of].tail(504)
    if len(lookback) < 252:
        return None
    log_rets = np.log(lookback / lookback.shift(1)).dropna().values
    mu = np.mean(log_rets) * 252
    sigma = np.std(log_rets) * np.sqrt(252)
    if sigma < 0.01:
        return None
    current_price = float(lookback.iloc[-1])
    if tilt_strength > 0:
        if quintile == 5:
            mu += tilt_strength
        elif quintile == 1:
            mu -= tilt_strength
    dt = 1.0 / 252
    rng = np.random.default_rng(42)
    Z = rng.standard_normal((5000, horizon_days))
    increments = (mu - 0.5 * sigma**2) * dt + sigma * np.sqrt(dt) * Z
    terminal = current_price * np.exp(np.cumsum(increments, axis=1)[:, -1])
    return float(np.median(terminal))


def run_sweep():
    log.info("Loading prices...")
    symbols = sorted(p.stem for p in CACHE_DIR.glob("*.csv"))
    prices_map = {}
    for sym in symbols:
        s = load_prices(sym)
        if s is not None:
            prices_map[sym] = s
    log.info(f"Loaded {len(prices_map)} tickers with sufficient history")

    log.info("Loading fundamentals from projection cache...")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    fund_map = {}
    for sym in prices_map:
        f = load_fundamentals(conn, sym)
        if f:
            fund_map[sym] = f
    log.info(f"Loaded fundamentals for {len(fund_map)} tickers")

    windows = pd.date_range("2022-01-15", "2024-09-15", freq="3MS")
    log.info(f"Windows: {len(windows)} ({windows[0].date()} to {windows[-1].date()})")

    factors = {
        "momentum": lambda sym, as_of: compute_momentum(prices_map[sym], as_of),
        "value": lambda sym, _: compute_value(fund_map[sym]) if sym in fund_map else None,
        "quality": lambda sym, _: compute_quality(fund_map[sym]) if sym in fund_map else None,
    }

    results = []
    total_windows = len(windows)

    for w_idx, as_of in enumerate(windows):
        log.info(f"Window {w_idx+1}/{total_windows}: {as_of.date()}")

        eligible = [
            sym for sym in prices_map
            if prices_map[sym].index.max() >= as_of + timedelta(days=365)
        ]
        log.info(f"  {len(eligible)} tickers with forward data")

        for factor_name, factor_fn in factors.items():
            scores = {}
            for sym in eligible:
                sc = factor_fn(sym, as_of)
                if sc is not None:
                    scores[sym] = sc

            if len(scores) < 50:
                log.info(f"  {factor_name}: only {len(scores)} scores, skipping")
                continue

            sorted_syms = sorted(scores.keys(), key=lambda s: scores[s])
            n = len(sorted_syms)
            quintile_map = {}
            for i, sym in enumerate(sorted_syms):
                quintile_map[sym] = min(5, 1 + int(i / n * 5))

            log.info(f"  {factor_name}: {n} tickers ranked")

            for hz_name, hz_days in HORIZONS.items():
                for tilt in TILT_STRENGTHS:
                    mape_sum = 0.0
                    hit_sum = 0
                    count = 0

                    for sym in quintile_map:
                        prices = prices_map[sym]
                        q = quintile_map[sym]
                        p50 = project_tilted(prices, as_of, hz_days, tilt, q)
                        if p50 is None:
                            continue

                        future_date = as_of + timedelta(days=int(hz_days * 365 / 252))
                        future = prices[prices.index >= future_date]
                        if len(future) == 0:
                            continue
                        actual = float(future.iloc[0])
                        current = float(prices[prices.index <= as_of].iloc[-1])

                        ape = abs(p50 - actual) / actual
                        hit = (p50 > current) == (actual > current)
                        mape_sum += ape
                        hit_sum += int(hit)
                        count += 1

                    if count > 0:
                        results.append({
                            "window": as_of.date().isoformat(),
                            "factor": factor_name,
                            "horizon": hz_name,
                            "tilt": tilt,
                            "n": count,
                            "mape": round(mape_sum / count * 100, 3),
                            "hit_pct": round(hit_sum / count * 100, 2),
                        })

            log.info(f"  {factor_name}: done for this window")

    if not results:
        log.error("No results produced")
        return

    with open(OUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=results[0].keys())
        writer.writeheader()
        writer.writerows(results)
    log.info(f"Wrote {len(results)} rows to {OUT_CSV}")

    # Report
    df = pd.DataFrame(results)
    lines = [
        "# Factor Sweep Report — Momentum + Value + Quality",
        f"\n**Run:** {datetime.utcnow().isoformat()[:19]}Z",
        f"**Windows:** {total_windows} (2022-01 to 2024-09, quarterly)",
        f"**Tickers per window:** ~{len(prices_map)} (cached universe)",
        f"**Factors:** momentum (12-1), value (earnings yield), quality (ROE)",
        f"**Tilt strengths:** {TILT_STRENGTHS}",
        "",
    ]

    for factor in ["momentum", "value", "quality"]:
        for hz in HORIZONS:
            lines.append(f"\n## {factor} — {hz} horizon\n")
            lines.append("| Tilt | Avg MAPE | ΔMAPE vs base | Avg Hit % | N |")
            lines.append("|---|---|---|---|---|")
            subset = df[(df["factor"] == factor) & (df["horizon"] == hz)]
            base_mape = subset[subset["tilt"] == 0.0]["mape"].mean() if len(subset[subset["tilt"] == 0.0]) > 0 else 0
            for tilt in TILT_STRENGTHS:
                t_rows = subset[subset["tilt"] == tilt]
                if len(t_rows) == 0:
                    continue
                avg_mape = t_rows["mape"].mean()
                avg_hit = t_rows["hit_pct"].mean()
                total_n = t_rows["n"].sum()
                delta = avg_mape - base_mape
                label = f"S{tilt}" if tilt > 0 else "baseline"
                lines.append(f"| {label} | {avg_mape:.2f}% | {delta:+.2f}pp | {avg_hit:.1f}% | {total_n} |")

    lines.append(f"\n\n*Generated {datetime.utcnow().date()} by factor_sweep_overnight.py*")

    with open(OUT_REPORT, "w") as f:
        f.write("\n".join(lines))
    log.info(f"Report written to {OUT_REPORT}")


if __name__ == "__main__":
    run_sweep()
