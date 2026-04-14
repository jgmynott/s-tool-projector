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

from data_providers import DataManager

# Module-level DataManager — singleton, lazy-inits providers
_dm = DataManager()

# Lazy-loaded sentiment DataFrame (loaded once, cached)
_sentiment_df: pd.DataFrame | None = None


def _load_sentiment() -> pd.DataFrame | None:
    """Load the sentiment CSV into a DataFrame (cached after first call)."""
    global _sentiment_df
    if _sentiment_df is not None:
        return _sentiment_df
    from pathlib import Path
    csv_path = Path(__file__).parent / SENTIMENT_CSV
    if not csv_path.exists():
        return None
    try:
        df = pd.read_csv(csv_path, parse_dates=["date"])
        df["ticker"] = df["ticker"].str.upper()
        df = df.sort_values(["ticker", "date"]).reset_index(drop=True)
        _sentiment_df = df
        return df
    except Exception:
        return None


def get_sentiment_tilt(symbol: str, as_of_date: datetime | None = None) -> dict:
    """Compute the sentiment-based drift tilt for a symbol.

    Returns dict with:
        mu_tilt:    float — annual drift adjustment (add to MC mu)
        avg_score:  float | None — trailing mean sentiment score
        lookback:   int — days of sentiment used
        n_days:     int — number of days with data in the window
        active:     bool — whether tilt is being applied
    """
    sent_df = _load_sentiment()
    if sent_df is None:
        return {"mu_tilt": 0.0, "avg_score": None, "lookback": SENTIMENT_LOOKBACK_DAYS,
                "n_days": 0, "active": False}

    if as_of_date is None:
        as_of_date = datetime.now(timezone.utc)
    # Strip timezone — sentiment CSV dates are tz-naive
    as_of_ts = pd.Timestamp(as_of_date).tz_localize(None) if pd.Timestamp(as_of_date).tzinfo else pd.Timestamp(as_of_date)

    start = as_of_ts - pd.Timedelta(days=SENTIMENT_LOOKBACK_DAYS)
    mask = (
        (sent_df["ticker"] == symbol.upper())
        & (sent_df["date"] >= start)
        & (sent_df["date"] <= as_of_ts)
    )
    sub = sent_df.loc[mask, "score_signed_mean"]

    if sub.empty or len(sub) < 3:  # require at least 3 days of data
        return {"mu_tilt": 0.0, "avg_score": None, "lookback": SENTIMENT_LOOKBACK_DAYS,
                "n_days": len(sub), "active": False}

    avg_score = float(sub.mean())
    mu_tilt = SENTIMENT_TILT_STRENGTH * avg_score

    return {
        "mu_tilt": round(mu_tilt, 6),
        "avg_score": round(avg_score, 4),
        "lookback": SENTIMENT_LOOKBACK_DAYS,
        "n_days": len(sub),
        "active": True,
    }


# ── Configuration ──

BLEND_MC = 0.30  # 30% MC / 70% MR (from sweep_report.md)
DEFAULT_PATHS = 2000
DEFAULT_HORIZON = 252  # 1 year

# Sentiment tilt — from Phase C full backtest (107 symbols, 27,800 forecasts)
# Best config: L21_S0.005 → -12.6% MAPE improvement at 1yr, +5.2% hit rate
SENTIMENT_LOOKBACK_DAYS = 21
SENTIMENT_TILT_STRENGTH = 0.005
SENTIMENT_CSV = "sentiment_data/sentiment_combined_2023-01-01_2026-04-15.csv"

# Fundamental tilt weights — how much each signal adjusts annualized drift
# Weights do not need to sum to 1.0; MAX_FUNDAMENTAL_TILT caps the combined tilt.
TILT_WEIGHTS = {
    "analyst_target":    0.30,  # FMP analyst consensus price target vs current
    "eps_growth":        0.18,  # FMP EPS growth estimate
    "insider":           0.10,  # FMP insider buy/sell net ratio
    "macro":             0.15,  # FRED macro regime (yield curve, rates, unemployment)
    "recommendation":    0.07,  # Finnhub buy/hold/sell consensus (fresher than FMP target)
    "put_call_contra":   0.05,  # yfinance put/call — CONTRARIAN (high P/C → bullish tilt)
    "public_pulse":      0.15,  # US general-public sentiment (Google Trends, Wiki, GDELT, Reddit, CSI)
}
MAX_FUNDAMENTAL_TILT = 0.06  # ±6% max annual drift from all fundamentals combined

# Earnings proximity — sigma boost when earnings are near (known vol event)
EARNINGS_SIGMA_MAX = 1.25   # max sigma multiplier when earnings = 0 days away
EARNINGS_SIGMA_WINDOW = 30  # days before earnings to start boosting

def compute_fundamental_tilt(
    symbol: str,
    current_price: float,
    horizon_days: int = DEFAULT_HORIZON,
) -> dict:
    """Compute drift adjustment from fundamental signals.

    Combines:
      1. Analyst price target gap (target vs current price)
      2. EPS growth estimates
      3. Insider trading net buy/sell
      4. Macro regime (yield curve, Fed funds, unemployment)

    Returns dict with:
        mu_tilt:     float — annual drift adjustment (add to MC mu)
        components:  dict  — breakdown of each signal's contribution
        active:      bool  — whether any signal contributed
        data:        dict  — raw data from providers
    """
    components = {}
    raw_data = {}
    total_tilt = 0.0

    # ── 1. Analyst Price Target ──
    target_data = _dm.get_price_target(symbol)
    if target_data and target_data.get("target_mean"):
        target = target_data["target_mean"]
        raw_data["price_target"] = target_data
        # Compute implied return from current price to target
        # Annualize: targets are typically 12-month
        implied_return = (target - current_price) / current_price
        # Cap at ±30% to avoid outlier targets dominating
        implied_return = max(-0.30, min(0.30, implied_return))
        tilt = implied_return * TILT_WEIGHTS["analyst_target"]
        components["analyst_target"] = {
            "target_mean": round(target, 2),
            "implied_return_pct": round(implied_return * 100, 1),
            "tilt": round(tilt, 5),
        }
        total_tilt += tilt

    # ── 2. EPS Growth ──
    eps_data = _dm.get_analyst_estimates(symbol)
    if eps_data and eps_data.get("eps_growth_pct") is not None:
        raw_data["eps_estimates"] = eps_data
        growth = eps_data["eps_growth_pct"] / 100  # convert to decimal
        # EPS growth maps roughly to price appreciation
        # Cap at ±50% growth
        growth = max(-0.50, min(0.50, growth))
        tilt = growth * TILT_WEIGHTS["eps_growth"]
        components["eps_growth"] = {
            "growth_pct": round(growth * 100, 1),
            "tilt": round(tilt, 5),
        }
        total_tilt += tilt

    # ── 3. Insider Trading ──
    insider_data = _dm.get_insider_signal(symbol)
    if insider_data and insider_data.get("net_ratio") is not None:
        raw_data["insider"] = insider_data
        # net_ratio: -1 (all sells) to +1 (all buys)
        net = insider_data["net_ratio"]
        # Scale: strong insider buying (+1) → +3% drift; strong selling (-1) → -3%
        tilt = net * 0.03 * TILT_WEIGHTS["insider"]
        components["insider"] = {
            "net_ratio": net,
            "signal": insider_data["signal"],
            "tilt": round(tilt, 5),
        }
        total_tilt += tilt

    # ── 4. Macro Regime ──
    macro = _dm.get_macro_regime()
    if macro and macro.get("drift_adj") is not None:
        raw_data["macro"] = macro
        tilt = macro["drift_adj"] * TILT_WEIGHTS["macro"]
        components["macro"] = {
            "regime": macro["regime"],
            "score": macro.get("score", 0),
            "tilt": round(tilt, 5),
        }
        total_tilt += tilt

    # ── 5. Finnhub Recommendation Trends (fresher than FMP target) ──
    recs = _dm.get_recommendation_trends(symbol)
    if recs and recs.get("total", 0) >= 5:  # need a real consensus
        raw_data["recommendations"] = recs
        buy, hold, sell, total = recs["buy"], recs["hold"], recs["sell"], recs["total"]
        # Signed score in [-1, +1]: (buy - sell) / total
        # Pure buys → +1 → maps to +6% implied annual return (strong bullish call)
        # Pure sells → -1 → -6%
        rec_score = (buy - sell) / total
        implied = rec_score * 0.06
        tilt = implied * TILT_WEIGHTS["recommendation"]
        components["recommendation"] = {
            "buy": buy, "hold": hold, "sell": sell, "total": total,
            "consensus": recs.get("consensus"),
            "score": round(rec_score, 3),
            "tilt": round(tilt, 5),
        }
        total_tilt += tilt

    # ── 7. Public Pulse (US general-public sentiment composite) ──
    pp = _dm.get_public_pulse(symbol, fast=True)
    if pp and pp.get("composite_score") is not None and pp.get("active_sources", 0) >= 2:
        raw_data["public_pulse"] = pp
        composite = pp["composite_score"]  # already in [-1, +1]
        # Map composite to implied annual return: +1 → +6%, -1 → -6%
        implied = composite * 0.06
        tilt = implied * TILT_WEIGHTS["public_pulse"]
        components["public_pulse"] = {
            "composite_score": round(composite, 3),
            "active_sources": pp["active_sources"],
            "total_sources": pp["total_sources"],
            "tilt": round(tilt, 5),
        }
        total_tilt += tilt

    # ── 6. Put/Call Ratio (CONTRARIAN) ──
    # Academic lit: high retail put/call = excessive hedging/fear → contrarian bullish.
    # Neutral ~0.85; >1.0 = fearful (bullish tilt); <0.7 = complacent (bearish tilt).
    pc = _dm.get_put_call_ratio(symbol)
    if pc and pc.get("put_call_ratio") is not None:
        raw_data["put_call"] = pc
        ratio = pc["put_call_ratio"]
        # Signed z-ish score around 0.85, clamped to [-1, +1]
        # 1.35 → +1 (very bearish crowd → bullish contrarian)
        # 0.35 → -1 (very bullish crowd → bearish contrarian)
        deviation = (ratio - 0.85) / 0.50
        deviation = max(-1.0, min(1.0, deviation))
        implied = deviation * 0.05  # ±5% implied annual return from sentiment extremes
        tilt = implied * TILT_WEIGHTS["put_call_contra"]
        components["put_call_contra"] = {
            "ratio": round(ratio, 3),
            "deviation_from_neutral": round(deviation, 3),
            "tilt": round(tilt, 5),
        }
        total_tilt += tilt

    # Clamp total
    total_tilt = max(-MAX_FUNDAMENTAL_TILT, min(MAX_FUNDAMENTAL_TILT, total_tilt))

    active = len(components) > 0 and abs(total_tilt) > 0.0001

    return {
        "mu_tilt": round(total_tilt, 6),
        "components": components,
        "active": active,
        "n_signals": len(components),
        "data": raw_data,
    }


def compute_earnings_proximity_sigma(symbol: str) -> dict:
    """Return a sigma multiplier that widens bands as earnings approach.

    Earnings are known vol events. When earnings are within EARNINGS_SIGMA_WINDOW
    days, boost sigma linearly up to EARNINGS_SIGMA_MAX at 0 days.

    Returns dict with:
        sigma_mult:      float — multiplier (1.0 = no boost)
        days_to_earnings: int | None
        earnings_date:    str | None
        active:           bool
    """
    try:
        earn = _dm.get_earnings_date(symbol)
        if not earn or not earn.get("earnings_date"):
            return {"sigma_mult": 1.0, "active": False}
        # Parse date — Finnhub gives "2026-04-30"; yfinance may give a timestamp
        date_str = str(earn["earnings_date"]).split(" ")[0][:10]
        edate = datetime.strptime(date_str, "%Y-%m-%d")
        days = (edate - datetime.now()).days
        # Only boost for future earnings within window
        if days < 0 or days > EARNINGS_SIGMA_WINDOW:
            return {
                "sigma_mult": 1.0,
                "active": False,
                "days_to_earnings": days,
                "earnings_date": date_str,
            }
        # Linear boost: 30 days out → 1.00, 0 days → EARNINGS_SIGMA_MAX
        frac = (EARNINGS_SIGMA_WINDOW - days) / EARNINGS_SIGMA_WINDOW
        mult = 1.0 + frac * (EARNINGS_SIGMA_MAX - 1.0)
        return {
            "sigma_mult":       round(mult, 4),
            "days_to_earnings": days,
            "earnings_date":    date_str,
            "active":           True,
        }
    except Exception:
        return {"sigma_mult": 1.0, "active": False}


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
    """Fetch OHLC history with fallback chain: yfinance → FMP → Polygon.

    Returns dict with numpy arrays {dates, opens, highs, lows, closes} or None.
    """
    # ── Primary: yfinance ──
    try:
        end = pd.Timestamp.today()
        start = end - pd.DateOffset(years=years)
        df = yf.download(symbol, start=start, end=end, progress=False, auto_adjust=False)
        if df is not None and len(df) >= 30:
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
                "source": "yfinance",
            }
    except Exception:
        pass

    # ── Fallback: FMP / Polygon via DataManager ──
    rows = _dm.get_historical(symbol, years=years)
    if rows and len(rows) >= 30:
        # DataManager returns list of dicts; convert to numpy arrays
        # Rows may be newest-first (FMP) or oldest-first (Polygon)
        rows_sorted = sorted(rows, key=lambda r: r["date"])
        dates = [datetime.strptime(r["date"], "%Y-%m-%d") for r in rows_sorted]
        return {
            "dates": dates,
            "opens": np.array([r.get("open") or 0 for r in rows_sorted], dtype=float),
            "highs": np.array([r.get("high") or 0 for r in rows_sorted], dtype=float),
            "lows": np.array([r.get("low") or 0 for r in rows_sorted], dtype=float),
            "closes": np.array([r.get("close") or 0 for r in rows_sorted], dtype=float),
            "symbol": symbol.upper(),
            "source": "fmp_or_polygon",
        }

    return None


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
    """Fetch key fundamentals via DataManager fallback chain.

    Priority: yfinance → FMP → Finnhub.
    Enriches with FMP ratios and Finnhub analyst recs when available.
    """
    # Primary: yfinance (free, no key)
    result = {}
    try:
        info = yf.Ticker(symbol).info
        if info and info.get("regularMarketPrice") is not None:
            result = {
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
                "source": "yfinance",
            }
    except Exception:
        pass

    # Fallback / enrichment: FMP fundamentals + ratios
    if _dm.fmp.is_available():
        if not result.get("pe_trailing"):
            fmp_fund = _dm.fmp.fetch_fundamentals(symbol)
            if fmp_fund:
                for k, v in [
                    ("pe_trailing", fmp_fund.get("pe_ratio")),
                    ("market_cap", fmp_fund.get("market_cap")),
                    ("eps_trailing", fmp_fund.get("eps")),
                    ("beta", fmp_fund.get("beta")),
                    ("dividend_yield", fmp_fund.get("dividend_yield")),
                    ("sector", fmp_fund.get("sector")),
                    ("industry", fmp_fund.get("industry")),
                ]:
                    if v is not None and not result.get(k):
                        result[k] = v
                if not result.get("source"):
                    result["source"] = "fmp"

        # FMP ratios — always try to enrich
        fmp_ratios = _dm.fmp.fetch_ratios(symbol)
        if fmp_ratios:
            result["gross_margin"] = fmp_ratios.get("gross_margin")
            result["operating_margin"] = fmp_ratios.get("operating_margin")
            result["net_margin"] = fmp_ratios.get("net_margin")
            result["return_on_equity"] = fmp_ratios.get("return_on_equity")
            result["debt_to_equity"] = fmp_ratios.get("debt_to_equity")
            result["price_to_book"] = fmp_ratios.get("price_to_book")

    # Enrichment: Finnhub analyst consensus
    if _dm.finnhub.is_available():
        recs = _dm.finnhub.fetch_recommendation_trends(symbol)
        if recs:
            result["analyst_buy"] = recs.get("buy")
            result["analyst_hold"] = recs.get("hold")
            result["analyst_sell"] = recs.get("sell")
            result["analyst_consensus"] = recs.get("consensus")

    # Enrichment: Finnhub news sentiment
    if _dm.finnhub.is_available():
        news = _dm.finnhub.fetch_news_sentiment(symbol)
        if news:
            result["news_sentiment_score"] = news.get("sentiment_score")
            result["news_article_count"] = news.get("article_count")

    # Enrichment: Polygon put/call ratio
    if _dm.polygon.is_available():
        opts = _dm.polygon.fetch_options_chain(symbol)
        if opts:
            result["put_call_ratio"] = opts.get("put_call_ratio")

    # Enrichment: next earnings date
    earnings = _dm.get_earnings_date(symbol)
    if earnings:
        result["next_earnings_date"] = earnings.get("earnings_date")

    return result if result else None


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
    mu_tilt: float = 0.0,
) -> dict:
    if rng is None:
        rng = np.random.default_rng()
    log_returns = np.diff(np.log(closes))
    mu_base = float(np.mean(log_returns) * 252)
    mu = mu_base + mu_tilt  # Apply sentiment tilt to annualised drift
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
            "mu_base": mu_base,
            "mu_tilt": mu_tilt,
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

    # Enrich with alternative provider sentiment signals
    alt_sentiment = _dm.get_sentiment_signals(symbol)

    closes = hist["closes"]
    S0 = float(closes[-1])
    momentum = compute_momentum(closes)

    # Merge momentum + alt sentiment into fundamentals
    if fundamentals:
        fundamentals.update(momentum)
    else:
        fundamentals = momentum

    if alt_sentiment:
        if alt_sentiment.get("put_call_ratio") is not None:
            fundamentals["put_call_ratio"] = round(alt_sentiment["put_call_ratio"], 3)
        news = alt_sentiment.get("news_sentiment")
        if news:
            fundamentals["news_sentiment_score"] = news.get("sentiment_score")
            fundamentals["news_article_count"] = news.get("article_count")
        analyst = alt_sentiment.get("analyst_consensus")
        if analyst:
            fundamentals["analyst_consensus"] = analyst.get("consensus")
            fundamentals["analyst_buy"] = analyst.get("buy")
            fundamentals["analyst_hold"] = analyst.get("hold")
            fundamentals["analyst_sell"] = analyst.get("sell")

    # Sentiment tilt — from Phase C backtest (L21, 0.5% strength)
    sentiment_info = get_sentiment_tilt(symbol)
    sentiment_tilt = sentiment_info["mu_tilt"]
    fundamentals["sentiment_tilt"] = sentiment_info

    # Fundamental tilt — analyst targets, EPS growth, insider trading, macro regime
    fundamental_info = compute_fundamental_tilt(symbol, S0, horizon_days)
    fundamental_tilt = fundamental_info["mu_tilt"]
    fundamentals["fundamental_tilt"] = fundamental_info

    # Combined tilt = sentiment + fundamentals (capped at ±10% annual drift)
    mu_tilt = max(-0.10, min(0.10, sentiment_tilt + fundamental_tilt))

    # Macro regime sigma overlay — multiplies on top of VIX sigma_mult
    macro = _dm.get_macro_regime()
    macro_sigma = macro.get("sigma_mult", 1.0) if macro else 1.0

    # Earnings proximity sigma overlay — widens bands near known earnings events
    earnings_sigma_info = compute_earnings_proximity_sigma(symbol)
    earnings_sigma = earnings_sigma_info.get("sigma_mult", 1.0)
    fundamentals["earnings_proximity"] = earnings_sigma_info

    combined_sigma_mult = sigma_mult * macro_sigma * earnings_sigma

    # Track data sources used
    fundamentals["data_sources"] = {
        "price": hist.get("source", "yfinance"),
        "fundamentals": fundamentals.get("source", "yfinance"),
        "sentiment_tilt_active": sentiment_info["active"],
        "fundamental_tilt_active": fundamental_info["active"],
        "total_mu_tilt": round(mu_tilt, 6),
        "total_mu_tilt_bps": round(mu_tilt * 10000, 1),
        "macro_regime": macro.get("regime") if macro else None,
        "macro_sigma_mult": round(macro_sigma, 3),
        "earnings_sigma_mult": round(earnings_sigma, 3),
        "earnings_proximity_active": earnings_sigma_info.get("active", False),
        "providers_active": {
            k: v.get("key_configured", v.get("available", False))
            for k, v in _dm.provider_status().items()
        },
    }

    # Run engines (MC gets combined tilt; MR uses combined sigma; MR is untilted — it has its own equilibrium)
    mc = run_monte_carlo(closes, num_paths, horizon_days, combined_sigma_mult, rng, mu_tilt=mu_tilt)
    mr = run_mean_reversion(closes, num_paths, horizon_days, combined_sigma_mult, rng)
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
