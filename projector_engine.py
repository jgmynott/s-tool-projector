"""
Projection engine — importable Python module.

Contains the same Monte Carlo + Mean Reversion models as the dashboard JS,
plus data fetching (yfinance), VIX lookup, and StockTwits sentiment.
Used by the background worker and the on-demand API endpoint.

All heavy computation happens here. The API and frontend are thin.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
import requests
import yfinance as yf


# ── Configuration ──

BLEND_MC = 0.30  # 30% MC / 70% MR (from sweep_report.md)
DEFAULT_PATHS = 2000
DEFAULT_HORIZON = 252  # 1 year

MILESTONES = [
    ("1 Week", 5),
    ("1 Month", 21),
    ("3 Months", 63),
    ("6 Months", 126),
    ("1 Year", 252),
    ("2 Years", 504),
]

VIX_REGIMES = [
    (40, "Crisis", 1.50),
    (25, "High", 1.20),
    (15, "Normal", 1.00),
    (0, "Low", 0.85),
]


# ── Data Fetching ──

def fetch_history(symbol: str, years: int = 3) -> dict | None:
    """Fetch OHLC history from yfinance. Returns dict with arrays or None."""
    end = pd.Timestamp.today()
    start = end - pd.DateOffset(years=years)
    df = yf.download(symbol, start=start, end=end, progress=False, auto_adjust=False)
    if df is None or len(df) < 30:
        return None
    # Flatten multi-level columns if present
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    dates = [d.to_pydatetime() for d in df.index]
    return {
        "dates": dates,
        "opens": df["Open"].values.astype(float),
        "highs": df["High"].values.astype(float),
        "lows": df["Low"].values.astype(float),
        "closes": df["Close"].values.astype(float),
        "symbol": symbol.upper(),
    }


def fetch_vix() -> dict | None:
    """Fetch current VIX level from yfinance."""
    try:
        df = yf.download("^VIX", period="5d", progress=False, auto_adjust=False)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        close = float(df["Close"].iloc[-1])
        as_of = df.index[-1].to_pydatetime()
        for threshold, regime, mult in VIX_REGIMES:
            if close >= threshold:
                return {"vix": close, "as_of": as_of, "regime": regime, "sigma_mult": mult}
    except Exception:
        pass
    return None


def fetch_stocktwits(symbol: str) -> dict | None:
    """Fetch live StockTwits sentiment. Server-side: no CORS issues."""
    url = f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
    try:
        r = requests.get(url, timeout=10)
        if not r.ok:
            return None
        j = r.json()
        messages = j.get("messages", [])
        if not messages:
            return None
        bull = bear = 0
        for m in messages:
            sent = (m.get("entities") or {}).get("sentiment", {}).get("basic")
            if sent == "Bullish":
                bull += 1
            elif sent == "Bearish":
                bear += 1
        tagged = bull + bear
        return {
            "bull": bull,
            "bear": bear,
            "total": len(messages),
            "tagged": tagged,
            "bull_pct": bull / tagged if tagged > 0 else None,
            "bear_pct": bear / tagged if tagged > 0 else None,
            "net": (bull - bear) / tagged if tagged > 0 else None,
        }
    except Exception:
        return None


def fetch_fundamentals(symbol: str) -> dict | None:
    """Fetch key fundamentals from yfinance Ticker.info."""
    try:
        info = yf.Ticker(symbol).info
        if not info or info.get("regularMarketPrice") is None:
            return None
        return {
            "pe_trailing": info.get("trailingPE"),
            "pe_forward": info.get("forwardPE"),
            "market_cap": info.get("marketCap"),
            "eps_trailing": info.get("trailingEps"),
            "eps_forward": info.get("forwardEps"),
            "dividend_yield": info.get("trailingAnnualDividendYield"),
            "beta": info.get("beta"),
            "week52_high": info.get("fiftyTwoWeekHigh"),
            "week52_low": info.get("fiftyTwoWeekLow"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "short_name": info.get("shortName"),
        }
    except Exception:
        return None


def compute_momentum(closes: np.ndarray) -> dict:
    """Compute price momentum indicators from historical closes."""
    S0 = float(closes[-1])
    n = len(closes)
    mom = {}

    # Price change over 30/60/90 trading days
    for label, days in [("30d", 21), ("60d", 42), ("90d", 63)]:
        if n > days:
            prev = float(closes[-1 - days])
            mom[f"chg_{label}"] = round((S0 - prev) / prev * 100, 2)
        else:
            mom[f"chg_{label}"] = None

    # Distance from 52-week high/low (using available history)
    lookback = min(252, n)
    high_252 = float(np.max(closes[-lookback:]))
    low_252 = float(np.min(closes[-lookback:]))
    mom["off_high_pct"] = round((S0 - high_252) / high_252 * 100, 2)
    mom["off_low_pct"] = round((S0 - low_252) / low_252 * 100, 2)

    # Simple RSI (14-day)
    if n > 15:
        deltas = np.diff(closes[-15:])
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = float(np.mean(gains))
        avg_loss = float(np.mean(losses))
        if avg_loss > 0:
            rs = avg_gain / avg_loss
            mom["rsi_14"] = round(100 - (100 / (1 + rs)), 1)
        else:
            mom["rsi_14"] = 100.0
    else:
        mom["rsi_14"] = None

    # Trend score: simple composite (-1 to +1)
    # Positive if above 50/200 SMA, price rising, RSI not extreme
    signals = []
    if n >= 50:
        sma50 = float(np.mean(closes[-50:]))
        signals.append(1 if S0 > sma50 else -1)
    if n >= 200:
        sma200 = float(np.mean(closes[-200:]))
        signals.append(1 if S0 > sma200 else -1)
    if mom.get("chg_30d") is not None:
        signals.append(1 if mom["chg_30d"] > 0 else -1)
    if mom.get("chg_90d") is not None:
        signals.append(1 if mom["chg_90d"] > 0 else -1)
    mom["trend_score"] = round(sum(signals) / max(len(signals), 1), 2) if signals else None

    return mom


# ── Simulation Engines ──

def run_monte_carlo(
    closes: np.ndarray,
    num_paths: int = DEFAULT_PATHS,
    horizon_days: int = DEFAULT_HORIZON,
    sigma_mult: float = 1.0,
    rng: np.random.Generator | None = None,
) -> dict:
    if rng is None:
        rng = np.random.default_rng()
    log_returns = np.diff(np.log(closes))
    mu = float(np.mean(log_returns) * 252)
    sigma_hist = float(np.std(log_returns, ddof=1) * np.sqrt(252))
    sigma = sigma_hist * sigma_mult
    dt = 1.0 / 252
    S0 = float(closes[-1])
    drift = (mu - 0.5 * sigma ** 2) * dt
    diff = sigma * math.sqrt(dt)
    Z = rng.standard_normal((num_paths, horizon_days))
    log_steps = drift + diff * Z
    log_paths = np.cumsum(log_steps, axis=1)
    paths = S0 * np.exp(log_paths)
    return {
        "paths": paths,
        "params": {
            "mu": mu,
            "sigma": sigma,
            "sigma_hist": sigma_hist,
            "sigma_mult": sigma_mult,
            "S0": S0,
        },
    }


def run_mean_reversion(
    closes: np.ndarray,
    num_paths: int = DEFAULT_PATHS,
    horizon_days: int = DEFAULT_HORIZON,
    sigma_mult: float = 1.0,
    rng: np.random.Generator | None = None,
) -> dict:
    if rng is None:
        rng = np.random.default_rng()
    S0 = float(closes[-1])
    n = len(closes)
    sma_period = 200 if n >= 200 else (50 if n >= 50 else max(10, n // 2))

    # Compute SMA
    ma = np.convolve(closes, np.ones(sma_period) / sma_period, mode="valid")
    # Pad front
    pad = np.full(sma_period - 1, ma[0])
    ma = np.concatenate([pad, ma])
    current_ma = float(ma[-1])

    # Trend slope from regression on recent SMA
    recent = ma[-sma_period:]
    x = np.arange(len(recent), dtype=float)
    slope_daily = float(np.polyfit(x, recent, 1)[0])
    trend_slope_ann = slope_daily * 252 / current_ma

    def eq_price(t):
        return current_ma + slope_daily * t

    # OU regression
    log_c = np.log(closes)
    log_ma = np.log(ma)
    devs = log_c - log_ma
    delta_devs = np.diff(devs)
    prev_devs = devs[:-1]
    if len(prev_devs) > 10:
        coeffs = np.polyfit(prev_devs, delta_devs, 1)
        kappa = float(-coeffs[0])
        half_life = math.log(2) / kappa if kappa > 0 else 252.0
        kappa = max(math.log(2) / 252, min(math.log(2) / 10, kappa))
        predicted = coeffs[1] + coeffs[0] * prev_devs
        residuals = delta_devs - predicted
        sigma_ou_hist = float(np.std(residuals, ddof=1) * np.sqrt(252))
    else:
        kappa = math.log(2) / 126
        half_life = 126.0
        sigma_ou_hist = float(np.std(np.diff(np.log(closes)), ddof=1) * np.sqrt(252))

    sigma_ou = sigma_ou_hist * sigma_mult

    # Momentum
    mom_period = min(20, n - 1)
    momentum = (S0 / float(closes[-1 - mom_period]) - 1) / mom_period

    # Simulate
    dt = 1.0 / 252
    paths = np.empty((num_paths, horizon_days))
    Z = rng.standard_normal((num_paths, horizon_days))
    for p in range(num_paths):
        x = math.log(S0)
        for t in range(horizon_days):
            x_eq = math.log(eq_price(t + 1))
            mom_decay = momentum * math.exp(-t / 20) if t < 60 else 0.0
            x = x + kappa * (x_eq - x) * dt + mom_decay * dt + sigma_ou * math.sqrt(dt) * Z[p, t]
            paths[p, t] = math.exp(x)

    return {
        "paths": paths,
        "params": {
            "kappa": kappa,
            "half_life": half_life,
            "sigma_ou": sigma_ou,
            "sigma_ou_hist": sigma_ou_hist,
            "sigma_mult": sigma_mult,
            "current_ma": current_ma,
            "trend_slope_ann": trend_slope_ann,
            "sma_period": sma_period,
            "momentum": momentum,
        },
    }


def blend_and_percentile(
    mc_paths: np.ndarray,
    mr_paths: np.ndarray,
    horizon_days: int,
    blend_mc: float = BLEND_MC,
) -> dict:
    total = mc_paths.shape[0] + mr_paths.shape[0]
    n_mc = min(mc_paths.shape[0], round(blend_mc * total))
    n_mr = min(mr_paths.shape[0], total - n_mc)
    combined = np.vstack([mc_paths[:n_mc], mr_paths[:n_mr]])
    pctiles = {}
    for label, q in [("p10", 0.10), ("p25", 0.25), ("p50", 0.50), ("p75", 0.75), ("p90", 0.90)]:
        pctiles[label] = np.percentile(combined, q * 100, axis=0).tolist()
    # Upside probability
    last = combined[:, -1]
    S0 = float(mc_paths[0, 0] / np.exp(np.log(mc_paths[0, 0]) - np.log(mc_paths[0, 0])))
    # Better: use the actual S0
    upside = float(np.mean(last > mc_paths[0, 0]))
    return {**pctiles, "upside_prob": upside, "n_mc": n_mc, "n_mr": n_mr}


def trading_dates(start: datetime, count: int) -> list[str]:
    """Generate ISO date strings for the next `count` trading days after `start`."""
    dates = []
    d = start
    while len(dates) < count:
        d += timedelta(days=1)
        if d.weekday() < 5:  # Mon-Fri
            dates.append(d.strftime("%Y-%m-%d"))
    return dates


def compute_milestones(
    proj_dates: list[str],
    pctiles: dict,
    current_price: float,
    horizon_days: int,
) -> list[dict]:
    out = []
    for label, days in MILESTONES:
        idx = days - 1
        if idx >= horizon_days:
            continue
        ret = (pctiles["p50"][idx] - current_price) / current_price * 100
        out.append({
            "label": label,
            "date": proj_dates[idx],
            "p10": round(pctiles["p10"][idx], 2),
            "p25": round(pctiles["p25"][idx], 2),
            "p50": round(pctiles["p50"][idx], 2),
            "p75": round(pctiles["p75"][idx], 2),
            "p90": round(pctiles["p90"][idx], 2),
            "ret_pct": round(ret, 1),
        })
    return out


# ── Full Projection Pipeline ──

def run_projection(
    symbol: str,
    horizon_days: int = DEFAULT_HORIZON,
    num_paths: int = DEFAULT_PATHS,
    blend_mc: float = BLEND_MC,
    seed: int | None = None,
) -> dict:
    """Run full projection for a symbol. Returns everything the frontend needs."""
    t0 = time.time()
    rng = np.random.default_rng(seed)

    # Fetch data
    hist = fetch_history(symbol, years=3)
    if hist is None:
        raise ValueError(f"Could not fetch history for {symbol}")

    vix_info = fetch_vix()
    sigma_mult = vix_info["sigma_mult"] if vix_info else 1.0

    sent_info = fetch_stocktwits(symbol)
    fundamentals = fetch_fundamentals(symbol)

    closes = hist["closes"]
    S0 = float(closes[-1])
    momentum = compute_momentum(closes)

    # Merge momentum into fundamentals
    if fundamentals:
        fundamentals.update(momentum)
    else:
        fundamentals = momentum

    # Run engines
    mc = run_monte_carlo(closes, num_paths, horizon_days, sigma_mult, rng)
    mr = run_mean_reversion(closes, num_paths, horizon_days, sigma_mult, rng)
    pctiles = blend_and_percentile(mc["paths"], mr["paths"], horizon_days, blend_mc)

    # Upside prob (recalculate properly)
    total = mc["paths"].shape[0] + mr["paths"].shape[0]
    n_mc = min(mc["paths"].shape[0], round(blend_mc * total))
    n_mr = min(mr["paths"].shape[0], total - n_mc)
    combined_last = np.concatenate([mc["paths"][:n_mc, -1], mr["paths"][:n_mr, -1]])
    upside = float(np.mean(combined_last > S0))

    # Dates
    last_date = hist["dates"][-1]
    proj_dates = trading_dates(last_date, horizon_days)
    run_date = last_date.strftime("%Y-%m-%d")

    # MR equilibrium curve (OU expected path)
    _ma = mr["params"]["current_ma"]
    _tsd = (mr["params"]["trend_slope_ann"] * _ma) / 252
    _k = mr["params"]["kappa"]
    mr_eq = [(_ma + _tsd * (i + 1)) + (S0 - _ma) * math.exp(-_k * (i + 1))
             for i in range(horizon_days)]

    # Milestones
    milestones = compute_milestones(proj_dates, pctiles, S0, horizon_days)

    # Trim historical data for chart (last 252 trading days)
    chart_len = min(252, len(hist["dates"]))
    hist_chart = {
        "dates": [d.strftime("%Y-%m-%d") for d in hist["dates"][-chart_len:]],
        "opens": hist["opens"][-chart_len:].tolist(),
        "highs": hist["highs"][-chart_len:].tolist(),
        "lows": hist["lows"][-chart_len:].tolist(),
        "closes": hist["closes"][-chart_len:].tolist(),
    }

    elapsed = time.time() - t0

    # Round curves for storage efficiency
    def _round_list(arr, decimals=2):
        return [round(v, decimals) for v in arr]

    return {
        "symbol": symbol.upper(),
        "run_date": run_date,
        "horizon_days": horizon_days,
        "current_price": round(S0, 2),
        # End-of-horizon percentiles
        "p10": round(pctiles["p10"][-1], 2),
        "p25": round(pctiles["p25"][-1], 2),
        "p50": round(pctiles["p50"][-1], 2),
        "p75": round(pctiles["p75"][-1], 2),
        "p90": round(pctiles["p90"][-1], 2),
        # Full curves
        "curves_json": json.dumps({
            "p10": _round_list(pctiles["p10"]),
            "p25": _round_list(pctiles["p25"]),
            "p50": _round_list(pctiles["p50"]),
            "p75": _round_list(pctiles["p75"]),
            "p90": _round_list(pctiles["p90"]),
        }),
        # Model params
        "mu": round(mc["params"]["mu"], 6),
        "sigma": round(mc["params"]["sigma"], 6),
        "sigma_hist": round(mc["params"]["sigma_hist"], 6),
        "sigma_mult": sigma_mult,
        "mr_target": round(mr["params"]["current_ma"], 2),
        "mr_kappa": round(mr["params"]["kappa"], 6),
        "upside_prob": round(upside, 3),
        # VIX
        "vix_value": round(vix_info["vix"], 1) if vix_info else None,
        "vix_regime": vix_info["regime"] if vix_info else None,
        # Sentiment
        "sent_bull": sent_info["bull"] if sent_info else None,
        "sent_bear": sent_info["bear"] if sent_info else None,
        "sent_tagged": sent_info["tagged"] if sent_info else None,
        "sent_net": round(sent_info["net"], 3) if sent_info and sent_info["net"] is not None else None,
        # Milestones + curves for chart
        "milestones_json": json.dumps(milestones),
        "mr_eq_json": json.dumps(_round_list(mr_eq)),
        "hist_json": json.dumps(hist_chart),
        "proj_dates_json": json.dumps(proj_dates),
        # Fundamentals (JSON blob — changes more often than projections)
        "fundamentals_json": json.dumps(fundamentals) if fundamentals else None,
        # Meta
        "num_paths": num_paths,
        "blend_mc": blend_mc,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "compute_secs": round(elapsed, 2),
    }
