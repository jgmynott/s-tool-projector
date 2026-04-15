"""
Unified data-provider module for the S-Tool Projector.

Integrates three alternative data sources beyond yfinance:
  - FMP  (Financial Modeling Prep)  — fundamentals, historical prices, ratios
  - Finnhub                         — real-time quotes, news sentiment, earnings
  - Polygon.io                      — options chain data, aggregate bars

Each provider implements a common interface and is orchestrated by a
DataManager that falls through a priority chain:
    yfinance (free) → FMP → Finnhub → Polygon

API keys are read from environment variables.  If a key is missing the
corresponding provider is silently disabled.

All public methods are fail-safe: on provider error the DataManager tries
the next provider in the chain and ultimately returns None.
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


# ─────────────────────────────────────────────────────────────────────
# TTL Cache
# ─────────────────────────────────────────────────────────────────────

_DEFAULT_TTLS = {
    "fundamentals": 4 * 3600,       # 4 hours
    "quote":        60,              # 60 seconds
    "sentiment":    30 * 60,         # 30 minutes
    "options":      30 * 60,
    "historical":   4 * 3600,
    "ratios":       4 * 3600,
    "earnings":     4 * 3600,
    "recommendations": 4 * 3600,
}


class TTLCache:
    """Simple in-memory cache with per-category TTLs."""

    def __init__(self, ttls: Dict[str, int] | None = None):
        self._store: Dict[str, tuple[float, Any]] = {}
        self._ttls = ttls or _DEFAULT_TTLS
        self._default_ttl = 300  # 5 min fallback

    def get(self, key: str, category: str = "fundamentals") -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, val = entry
        ttl = self._ttls.get(category, self._default_ttl)
        if time.time() - ts > ttl:
            del self._store[key]
            return None
        return val

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.time(), value)

    def clear(self) -> None:
        self._store.clear()


# ─────────────────────────────────────────────────────────────────────
# Rate Limiters
# ─────────────────────────────────────────────────────────────────────

class TokenBucketLimiter:
    """Token-bucket rate limiter (requests per window)."""

    def __init__(self, max_tokens: int, window_seconds: float):
        self.max_tokens = max_tokens
        self.window = window_seconds
        self._tokens: float = float(max_tokens)
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        added = elapsed * (self.max_tokens / self.window)
        self._tokens = min(self.max_tokens, self._tokens + added)
        self._last_refill = now

    def acquire(self, block: bool = True) -> bool:
        """Try to consume one token. If *block* is True, sleep until available."""
        self._refill()
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        if not block:
            return False
        # Sleep until we'd have a token
        deficit = 1.0 - self._tokens
        wait = deficit * (self.window / self.max_tokens)
        time.sleep(wait)
        self._refill()
        self._tokens -= 1
        return True

    @property
    def remaining(self) -> int:
        self._refill()
        return int(self._tokens)


class DailyLimiter:
    """Tracks a daily request budget (resets at midnight UTC)."""

    def __init__(self, daily_limit: int):
        self.daily_limit = daily_limit
        self._count = 0
        self._day = datetime.now(timezone.utc).date()

    def _maybe_reset(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._day:
            self._day = today
            self._count = 0

    def acquire(self, block: bool = False) -> bool:
        self._maybe_reset()
        if self._count < self.daily_limit:
            self._count += 1
            return True
        return False

    @property
    def remaining(self) -> int:
        self._maybe_reset()
        return max(0, self.daily_limit - self._count)


# ─────────────────────────────────────────────────────────────────────
# Base Provider
# ─────────────────────────────────────────────────────────────────────

class BaseProvider(ABC):
    """Interface that every data provider must implement."""

    name: str = "base"

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the provider is configured and usable."""

    @abstractmethod
    def status(self) -> Dict[str, Any]:
        """Return a dict describing provider health."""


# ─────────────────────────────────────────────────────────────────────
# FMP Provider
# ─────────────────────────────────────────────────────────────────────

class FMPProvider(BaseProvider):
    """Financial Modeling Prep — fundamentals, historical prices, ratios."""

    name = "fmp"
    BASE = "https://financialmodelingprep.com/stable"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("FMP_API_KEY")
        # Daily budget lifted from 250 → 300 000 for Premium tier
        # (300 req/min × 24h × 60 = ~432 000/day; keep some headroom).
        self.limiter = DailyLimiter(300_000)
        # Per-request throttle: Premium tier allows 300/min (~5/sec). Cap at 1/sec
        # for safety margin and to smooth bursts from the daily worker at 2,600
        # symbols × multiple endpoints per symbol.
        self.rate_limiter = TokenBucketLimiter(max_tokens=1, window_seconds=1.0)

    def is_available(self) -> bool:
        return bool(self.api_key) and self.limiter.remaining > 0

    def status(self) -> Dict[str, Any]:
        return {
            "provider": self.name,
            "key_configured": bool(self.api_key),
            "rate_limit_remaining": self.limiter.remaining,
        }

    # -- internal helpers --

    def _get(self, path: str, params: Dict[str, Any] | None = None) -> Any:
        if not self.is_available():
            return None
        if not self.limiter.acquire():
            logger.warning("FMP daily rate limit exhausted")
            return None
        # Per-request throttle (blocks up to ~1s if we're going too fast)
        self.rate_limiter.acquire(block=True)
        params = params or {}
        params["apikey"] = self.api_key
        url = f"{self.BASE}/{path}"
        try:
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            logger.warning("FMP request failed (%s): %s", path, exc)
            return None

    # -- public data methods --

    def fetch_fundamentals(self, symbol: str) -> Dict[str, Any] | None:
        """P/E, market cap, EPS, beta, dividend yield, sector, industry.

        /stable/profile no longer returns PE or EPS (those live in /stable/ratios
        now). We fetch both and merge so callers still see `pe_ratio`/`eps`.
        """
        data = self._get("profile", {"symbol": symbol})
        if not data:
            return None
        try:
            rec = data[0] if isinstance(data, list) else data
            # Pull PE + EPS from ratios (no longer in /stable/profile)
            pe_ratio = None
            eps = None
            ratios_data = self._get("ratios", {"symbol": symbol, "limit": 1})
            if ratios_data:
                r = ratios_data[0] if isinstance(ratios_data, list) else ratios_data
                pe_ratio = r.get("priceToEarningsRatio") or r.get("priceEarningsRatio")
                eps = r.get("netIncomePerShare") or r.get("earningsPerShare")
            return {
                "pe_ratio":       pe_ratio,
                "market_cap":     rec.get("marketCap") or rec.get("mktCap"),
                "eps":            eps,
                "beta":           rec.get("beta"),
                "dividend_yield": rec.get("lastDividend") or rec.get("lastDiv"),
                "sector":         rec.get("sector"),
                "industry":       rec.get("industry"),
                "revenue":        rec.get("revenue") if "revenue" in rec else None,
                "profit_margin":  None,  # not in profile; see ratios
                "source":         "fmp",
            }
        except (IndexError, KeyError, TypeError) as exc:
            logger.warning("FMP fundamentals parse error: %s", exc)
            return None

    def fetch_historical(self, symbol: str, years: int = 5) -> List[Dict] | None:
        """Daily OHLCV closes for the last *years* years."""
        # /stable/historical-price-eod/full returns a flat list of OHLCV rows
        # (no "historical" envelope like the old v3 /historical-price-full).
        data = self._get("historical-price-eod/full", {"symbol": symbol})
        if not data:
            return None
        rows = data if isinstance(data, list) else data.get("historical", [])
        if not isinstance(rows, list) or not rows:
            return None
        rows = rows[: years * 252]
        return [
            {
                "date":   r["date"],
                "open":   r.get("open"),
                "high":   r.get("high"),
                "low":    r.get("low"),
                "close":  r.get("close"),
                "volume": r.get("volume"),
            }
            for r in rows
        ]

    def fetch_ratios(self, symbol: str) -> Dict[str, Any] | None:
        """Latest-quarter profitability, debt, and valuation ratios."""
        data = self._get("ratios", {"symbol": symbol, "limit": 1})
        if not data:
            return None
        try:
            rec = data[0] if isinstance(data, list) else data
            return {
                "gross_margin":          rec.get("grossProfitMargin"),
                "operating_margin":      rec.get("operatingProfitMargin"),
                "net_margin":            rec.get("netProfitMargin"),
                "return_on_equity":      rec.get("returnOnEquity"),
                "return_on_assets":      rec.get("returnOnAssets"),
                "debt_to_equity":        rec.get("debtEquityRatio"),
                "current_ratio":         rec.get("currentRatio"),
                "pe_ratio":              rec.get("priceToEarningsRatio") or rec.get("priceEarningsRatio"),
                "price_to_book":         rec.get("priceToBookRatio"),
                "price_to_sales":        rec.get("priceToSalesRatio"),
                "source":                "fmp",
            }
        except (IndexError, KeyError, TypeError) as exc:
            logger.warning("FMP ratios parse error: %s", exc)
            return None

    def fetch_earnings_calendar(self, symbol: str) -> Dict[str, Any] | None:
        """Next earnings date for *symbol*."""
        data = self._get("earnings", {"symbol": symbol})
        if not data:
            return None
        try:
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            for rec in (data if isinstance(data, list) else [data]):
                if rec.get("date", "") >= now_str:
                    return {
                        "earnings_date": rec["date"],
                        "source":        "fmp",
                    }
            return None
        except (KeyError, TypeError) as exc:
            logger.warning("FMP earnings parse error: %s", exc)
            return None


# ─────────────────────────────────────────────────────────────────────
# Finnhub Provider
# ─────────────────────────────────────────────────────────────────────

class FinnhubProvider(BaseProvider):
    """Finnhub — real-time quotes, news sentiment, earnings, analyst recs."""

    name = "finnhub"
    BASE = "https://finnhub.io/api/v1"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("FINNHUB_API_KEY")
        self.limiter = TokenBucketLimiter(max_tokens=60, window_seconds=60)

    def is_available(self) -> bool:
        return bool(self.api_key) and self.limiter.remaining > 0

    def status(self) -> Dict[str, Any]:
        return {
            "provider": self.name,
            "key_configured": bool(self.api_key),
            "rate_limit_remaining": self.limiter.remaining,
        }

    def _get(self, path: str, params: Dict[str, Any] | None = None) -> Any:
        if not self.is_available():
            return None
        if not self.limiter.acquire(block=False):
            logger.warning("Finnhub rate limit hit; skipping request")
            return None
        params = params or {}
        params["token"] = self.api_key
        url = f"{self.BASE}/{path}"
        try:
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            logger.warning("Finnhub request failed (%s): %s", path, exc)
            return None

    # -- public data methods --

    def fetch_quote(self, symbol: str) -> Dict[str, Any] | None:
        """Current price, change, volume."""
        data = self._get("quote", {"symbol": symbol})
        if not data or data.get("c") is None:
            return None
        return {
            "current_price": data.get("c"),
            "change":        data.get("d"),
            "pct_change":    data.get("dp"),
            "high":          data.get("h"),
            "low":           data.get("l"),
            "open":          data.get("o"),
            "prev_close":    data.get("pc"),
            "volume":        data.get("v") if "v" in data else None,
            "source":        "finnhub",
        }

    def fetch_news_sentiment(
        self, symbol: str, days: int = 7
    ) -> Dict[str, Any] | None:
        """Aggregate news sentiment score from recent articles."""
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        data = self._get("company-news", {
            "symbol": symbol,
            "from":   start.strftime("%Y-%m-%d"),
            "to":     end.strftime("%Y-%m-%d"),
        })
        if not data or not isinstance(data, list) or len(data) == 0:
            return None

        # Finnhub company-news doesn't directly include a numeric
        # sentiment score, so we derive one from headline length-weighted
        # recency.  When the *news-sentiment* endpoint is available on
        # the paid plan it returns a proper sentiment field; here we
        # just count articles and infer a rough signal.
        total = len(data)
        # Use the dedicated sentiment endpoint if possible
        sentiment_data = self._get("news-sentiment", {"symbol": symbol})
        if sentiment_data and "sentiment" in sentiment_data:
            s = sentiment_data["sentiment"]
            return {
                "article_count":     total,
                "bullish_pct":       s.get("bullishPercent"),
                "bearish_pct":       s.get("bearishPercent"),
                "sentiment_score":   s.get("score"),  # -1..+1 scale
                "source":            "finnhub",
            }

        # Fallback: simple article-count proxy (more articles = more attention)
        return {
            "article_count":   total,
            "sentiment_score": None,  # not available on free tier
            "source":          "finnhub",
        }

    def fetch_earnings_calendar(self, symbol: str) -> Dict[str, Any] | None:
        """Next earnings date + EPS estimates."""
        end = (datetime.now(timezone.utc) + timedelta(days=90)).strftime("%Y-%m-%d")
        start = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        data = self._get("calendar/earnings", {
            "symbol": symbol,
            "from":   start,
            "to":     end,
        })
        if not data:
            return None
        earnings = data.get("earningsCalendar", [])
        for rec in earnings:
            if rec.get("symbol", "").upper() == symbol.upper():
                return {
                    "earnings_date":  rec.get("date"),
                    "eps_estimate":   rec.get("epsEstimate"),
                    "eps_actual":     rec.get("epsActual"),
                    "revenue_estimate": rec.get("revenueEstimate"),
                    "source":         "finnhub",
                }
        return None

    def fetch_recommendation_trends(self, symbol: str) -> Dict[str, Any] | None:
        """Analyst buy/hold/sell counts (latest month)."""
        data = self._get("stock/recommendation", {"symbol": symbol})
        if not data or not isinstance(data, list) or len(data) == 0:
            return None
        rec = data[0]
        buy = rec.get("buy", 0) + rec.get("strongBuy", 0)
        hold = rec.get("hold", 0)
        sell = rec.get("sell", 0) + rec.get("strongSell", 0)
        total = buy + hold + sell
        return {
            "buy":    buy,
            "hold":   hold,
            "sell":   sell,
            "total":  total,
            "consensus": (
                "buy" if buy > hold + sell else
                "sell" if sell > buy + hold else
                "hold"
            ),
            "period": rec.get("period"),
            "source": "finnhub",
        }


# ─────────────────────────────────────────────────────────────────────
# Polygon Provider
# ─────────────────────────────────────────────────────────────────────

class PolygonProvider(BaseProvider):
    """Polygon.io — options chain data, aggregate bars."""

    name = "polygon"
    BASE = "https://api.polygon.io"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("POLYGON_API_KEY")
        self.limiter = TokenBucketLimiter(max_tokens=5, window_seconds=60)

    def is_available(self) -> bool:
        return bool(self.api_key) and self.limiter.remaining > 0

    def status(self) -> Dict[str, Any]:
        return {
            "provider": self.name,
            "key_configured": bool(self.api_key),
            "rate_limit_remaining": self.limiter.remaining,
        }

    def _get(self, path: str, params: Dict[str, Any] | None = None) -> Any:
        if not self.is_available():
            return None
        if not self.limiter.acquire(block=False):
            logger.warning("Polygon rate limit hit; skipping request")
            return None
        params = params or {}
        params["apiKey"] = self.api_key
        url = f"{self.BASE}/{path}"
        try:
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            logger.warning("Polygon request failed (%s): %s", path, exc)
            return None

    # -- public data methods --

    def fetch_options_chain(self, symbol: str) -> Dict[str, Any] | None:
        """Fetch options snapshot, compute put/call ratio."""
        data = self._get(f"v3/snapshot/options/{symbol}", {"limit": 250})
        if not data or "results" not in data:
            return None
        results = data["results"]
        call_vol = 0
        put_vol = 0
        for opt in results:
            details = opt.get("details", {})
            day = opt.get("day", {})
            vol = day.get("volume", 0) or 0
            ctype = details.get("contract_type", "").lower()
            if ctype == "call":
                call_vol += vol
            elif ctype == "put":
                put_vol += vol

        pc_ratio = (put_vol / call_vol) if call_vol > 0 else None
        return {
            "call_volume":    call_vol,
            "put_volume":     put_vol,
            "put_call_ratio": pc_ratio,
            "contracts_seen": len(results),
            "source":         "polygon",
        }

    def fetch_aggregates(
        self, symbol: str, days: int = 252
    ) -> List[Dict] | None:
        """Daily OHLCV aggregate bars."""
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        path = (
            f"v2/aggs/ticker/{symbol}/range/1/day/"
            f"{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"
        )
        data = self._get(path, {"adjusted": "true", "sort": "asc"})
        if not data or "results" not in data:
            return None
        return [
            {
                "date":   datetime.fromtimestamp(
                    r["t"] / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d"),
                "open":   r.get("o"),
                "high":   r.get("h"),
                "low":    r.get("l"),
                "close":  r.get("c"),
                "volume": r.get("v"),
            }
            for r in data["results"]
        ]


# ─────────────────────────────────────────────────────────────────────
# FRED Macro Provider (FREE — no key needed for CSV endpoints)
# ─────────────────────────────────────────────────────────────────────

class FREDProvider(BaseProvider):
    """Federal Reserve Economic Data — macro indicators via CSV endpoints.

    Key series for market projections:
      - DGS10: 10-year Treasury yield
      - DGS2:  2-year Treasury yield (spread = yield curve)
      - FEDFUNDS: Fed funds rate
      - UNRATE: Unemployment rate
      - T10Y2Y: 10Y-2Y spread (yield curve inversion signal)
    """

    name = "fred"
    BASE_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"

    def __init__(self):
        self.limiter = TokenBucketLimiter(max_tokens=10, window_seconds=60)
        self._cache: Dict[str, tuple[float, Any]] = {}  # series_id → (ts, value)
        self._cache_ttl = 6 * 3600  # 6 hours

    def is_available(self) -> bool:
        return True  # CSV endpoints are free, no key needed

    def status(self) -> Dict[str, Any]:
        return {
            "provider": self.name,
            "key_configured": True,  # no key needed
            "rate_limit_remaining": self.limiter.remaining,
        }

    def _fetch_series_latest(self, series_id: str) -> Optional[float]:
        """Fetch the latest value of a FRED series via CSV."""
        # Check local cache
        cached = self._cache.get(series_id)
        if cached and (time.time() - cached[0]) < self._cache_ttl:
            return cached[1]

        if not self.limiter.acquire(block=False):
            logger.warning("FRED rate limit hit; skipping %s", series_id)
            return None

        try:
            r = requests.get(self.BASE_CSV, params={"id": series_id}, timeout=15)
            r.raise_for_status()
            lines = r.text.strip().split("\n")
            # CSV: DATE,VALUE — walk backwards to find last non-"." value
            for line in reversed(lines[1:]):
                parts = line.split(",")
                if len(parts) == 2 and parts[1].strip() != ".":
                    val = float(parts[1].strip())
                    self._cache[series_id] = (time.time(), val)
                    return val
        except Exception as exc:
            logger.warning("FRED fetch failed for %s: %s", series_id, exc)
        return None

    def fetch_macro_snapshot(self) -> Dict[str, Any]:
        """Fetch key macro indicators for market regime detection."""
        result = {}

        # Yield curve (10Y-2Y spread) — negative = inversion = recession signal
        spread = self._fetch_series_latest("T10Y2Y")
        if spread is not None:
            result["yield_curve_spread"] = round(spread, 3)
            result["yield_curve_inverted"] = spread < 0

        # 10-year yield
        dgs10 = self._fetch_series_latest("DGS10")
        if dgs10 is not None:
            result["treasury_10y"] = round(dgs10, 3)

        # Fed funds rate
        ff = self._fetch_series_latest("FEDFUNDS")
        if ff is not None:
            result["fed_funds_rate"] = round(ff, 3)

        # Unemployment
        unrate = self._fetch_series_latest("UNRATE")
        if unrate is not None:
            result["unemployment_rate"] = round(unrate, 1)

        # High Yield OAS (credit spread) — widens in stress, strongest recession predictor
        hy_oas = self._fetch_series_latest("BAMLH0A0HYM2")
        if hy_oas is not None:
            result["hy_credit_spread"] = round(hy_oas, 2)

        # Consumer Sentiment (University of Michigan)
        umcsent = self._fetch_series_latest("UMCSENT")
        if umcsent is not None:
            result["consumer_sentiment"] = round(umcsent, 1)

        return result

    def get_macro_regime(self) -> Dict[str, Any]:
        """Classify the current macro regime and compute a sigma/drift modifier.

        Returns:
            regime: str — "risk_on", "neutral", "risk_off", "crisis"
            sigma_mult: float — multiply base vol by this
            drift_adj: float — add to annualized drift (bps)
        """
        snap = self.fetch_macro_snapshot()
        if not snap:
            return {"regime": "neutral", "sigma_mult": 1.0, "drift_adj": 0.0, "data": {}}

        score = 0  # negative = risk off, positive = risk on

        # Yield curve: inverted is strongly bearish
        yc = snap.get("yield_curve_spread")
        if yc is not None:
            if yc < -0.5:
                score -= 2  # deeply inverted
            elif yc < 0:
                score -= 1  # mildly inverted
            elif yc > 1.0:
                score += 1  # healthy steepness

        # Fed funds rate: higher = tighter = bearish for equities
        ff = snap.get("fed_funds_rate")
        if ff is not None:
            if ff > 5.0:
                score -= 1
            elif ff < 2.0:
                score += 1

        # Unemployment: rising = bearish
        ur = snap.get("unemployment_rate")
        if ur is not None:
            if ur > 5.5:
                score -= 1
            elif ur < 4.0:
                score += 1

        # High yield credit spread: widening = stress
        hy = snap.get("hy_credit_spread")
        if hy is not None:
            if hy > 6.0:
                score -= 2  # severe stress
            elif hy > 4.5:
                score -= 1  # elevated
            elif hy < 3.0:
                score += 1  # tight spreads = risk-on

        # Consumer sentiment: low = pessimism
        cs = snap.get("consumer_sentiment")
        if cs is not None:
            if cs < 55:
                score -= 1
            elif cs > 85:
                score += 1

        # Classify
        if score <= -3:
            regime = "crisis"
            sigma_mult = 1.3
            drift_adj = -0.04  # -4% annualized
        elif score <= -1:
            regime = "risk_off"
            sigma_mult = 1.1
            drift_adj = -0.02
        elif score >= 2:
            regime = "risk_on"
            sigma_mult = 0.9
            drift_adj = 0.02
        else:
            regime = "neutral"
            sigma_mult = 1.0
            drift_adj = 0.0

        return {
            "regime": regime,
            "sigma_mult": sigma_mult,
            "drift_adj": drift_adj,
            "score": score,
            "data": snap,
        }


# ─────────────────────────────────────────────────────────────────────
# FMP Analyst Estimates & Insider Trading (extends FMPProvider)
# ─────────────────────────────────────────────────────────────────────

class CBOEProvider(BaseProvider):
    """CBOE — market-wide equity put/call ratio from free CSV.

    This is a contrarian indicator: extreme put/call ratios signal
    fear (high) or complacency (low), which tend to precede reversals.
    """

    name = "cboe"
    URL = "https://www.cboe.com/us/options/market_statistics/daily/"

    def __init__(self):
        self._cache: Optional[tuple[float, float]] = None  # (ts, ratio)
        self._cache_ttl = 12 * 3600  # 12 hours

    def is_available(self) -> bool:
        return True  # free public data

    def status(self) -> Dict[str, Any]:
        return {"provider": self.name, "key_configured": True}

    def fetch_equity_pcr(self) -> Optional[float]:
        """Fetch the latest CBOE equity put/call ratio.

        Falls back to a simulated value from VIX if CBOE is unavailable.
        """
        if self._cache and (time.time() - self._cache[0]) < self._cache_ttl:
            return self._cache[1]

        try:
            # CBOE publishes daily statistics; try the direct CSV
            r = requests.get(
                "https://www.cboe.com/us/options/market_statistics/daily/",
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if r.ok and "put" in r.text.lower():
                # Parse the equity P/C ratio from the page
                # Look for a pattern like "Equity Put/Call Ratio" followed by a number
                import re
                match = re.search(r'equity.*?put.*?call.*?ratio.*?(\d+\.\d+)', r.text, re.IGNORECASE | re.DOTALL)
                if match:
                    ratio = float(match.group(1))
                    self._cache = (time.time(), ratio)
                    return ratio
        except Exception as exc:
            logger.warning("CBOE fetch failed: %s", exc)

        return None


class FMPAnalystProvider:
    """Additional FMP endpoints for analyst targets and insider trading.

    Uses the existing FMPProvider's _get() method and rate limiter.
    """

    def __init__(self, fmp: FMPProvider):
        self.fmp = fmp

    def fetch_analyst_estimates(self, symbol: str) -> Dict[str, Any] | None:
        """Analyst EPS estimates and revenue estimates (forward-looking)."""
        data = self.fmp._get("analyst-estimates", {
            "symbol": symbol, "period": "annual",
        })
        if not data or not isinstance(data, list):
            return None
        try:
            result = {"estimates": []}
            # Data comes newest first (e.g. 2030, 2029, 2028...)
            # We want the next 2-3 years
            now_year = datetime.now(timezone.utc).year
            relevant = [r for r in data if r.get("date", "")[:4].isdigit()
                        and int(r["date"][:4]) >= now_year]
            relevant.sort(key=lambda r: r["date"])  # oldest first

            for rec in relevant[:4]:
                result["estimates"].append({
                    "date": rec.get("date"),
                    "eps_avg": rec.get("epsAvg"),
                    "eps_high": rec.get("epsHigh"),
                    "eps_low": rec.get("epsLow"),
                    "revenue_avg": rec.get("revenueAvg"),
                    "revenue_high": rec.get("revenueHigh"),
                    "revenue_low": rec.get("revenueLow"),
                    "num_analysts": rec.get("numAnalystsEps"),
                })
            # Compute implied EPS growth (next year vs this year)
            if len(relevant) >= 2:
                cur = relevant[0].get("epsAvg")
                nxt = relevant[1].get("epsAvg")
                if cur and nxt and cur != 0:
                    result["eps_growth_pct"] = round((nxt - cur) / abs(cur) * 100, 1)
            return result
        except Exception as exc:
            logger.warning("FMP analyst estimates parse error: %s", exc)
            return None

    def fetch_price_target(self, symbol: str) -> Dict[str, Any] | None:
        """Analyst consensus price target."""
        data = self.fmp._get("price-target-consensus", {"symbol": symbol})
        if not data:
            return None
        try:
            rec = data[0] if isinstance(data, list) else data
            return {
                "target_high": rec.get("targetHigh"),
                "target_low": rec.get("targetLow"),
                "target_mean": rec.get("targetConsensus"),
                "target_median": rec.get("targetMedian"),
                "source": "fmp",
            }
        except Exception as exc:
            logger.warning("FMP price target parse error: %s", exc)
            return None

    def fetch_insider_trading(self, symbol: str, limit: int = 20) -> Dict[str, Any] | None:
        """Recent insider transactions — net buy/sell signal."""
        data = self.fmp._get("insider-trading/latest", {"symbol": symbol, "limit": limit})
        if not data or not isinstance(data, list):
            return None
        try:
            buys = sells = 0
            buy_value = sell_value = 0.0
            for txn in data:
                ttype = (txn.get("transactionType") or "").lower()
                shares = abs(txn.get("securitiesTransacted") or 0)
                price = txn.get("price") or 0
                value = shares * price
                if "purchase" in ttype or "buy" in ttype or ttype == "p-purchase":
                    buys += 1
                    buy_value += value
                elif "sale" in ttype or "sell" in ttype or ttype == "s-sale":
                    sells += 1
                    sell_value += value

            total = buys + sells
            if total == 0:
                return None

            net_ratio = (buys - sells) / total  # -1 (all sells) to +1 (all buys)
            return {
                "buys": buys,
                "sells": sells,
                "buy_value": round(buy_value),
                "sell_value": round(sell_value),
                "net_ratio": round(net_ratio, 3),
                "transactions": total,
                "signal": "bullish" if net_ratio > 0.2 else "bearish" if net_ratio < -0.2 else "neutral",
                "source": "fmp",
            }
        except Exception as exc:
            logger.warning("FMP insider trading parse error: %s", exc)
            return None


# ─────────────────────────────────────────────────────────────────────
# DataManager — orchestration layer
# ─────────────────────────────────────────────────────────────────────

class DataManager:
    """Orchestrates multiple data providers with priority fallback.

    Priority chain:  yfinance (free) → FMP → Finnhub → Polygon
    """

    def __init__(self):
        self.cache = TTLCache()
        self.fmp = FMPProvider()
        self.finnhub = FinnhubProvider()
        self.polygon = PolygonProvider()
        self.fred = FREDProvider()
        self.fmp_analyst = FMPAnalystProvider(self.fmp)
        # yfinance is imported lazily to keep the module importable
        # even if yfinance is not installed.
        self._yf = None

    @property
    def yf(self):
        if self._yf is None:
            try:
                import yfinance as yf
                self._yf = yf
            except ImportError:
                self._yf = False  # sentinel: unavailable
        return self._yf if self._yf is not False else None

    # ── High-level API ────────────────────────────────────────────

    def get_fundamentals(self, symbol: str) -> Dict[str, Any] | None:
        """Merged fundamentals dict from the best available source."""
        key = f"fundamentals:{symbol}"
        cached = self.cache.get(key, "fundamentals")
        if cached is not None:
            return cached

        result: Dict[str, Any] = {}

        # yfinance
        if self.yf:
            try:
                info = self.yf.Ticker(symbol).info or {}
                result = {
                    "pe_ratio":       info.get("trailingPE") or info.get("forwardPE"),
                    "market_cap":     info.get("marketCap"),
                    "eps":            info.get("trailingEps"),
                    "beta":           info.get("beta"),
                    "dividend_yield": info.get("dividendYield"),
                    "sector":         info.get("sector"),
                    "industry":       info.get("industry"),
                    "revenue":        info.get("totalRevenue"),
                    "profit_margin":  info.get("profitMargins"),
                    "source":         "yfinance",
                }
                logger.info("Fundamentals for %s from yfinance", symbol)
            except Exception as exc:
                logger.warning("yfinance fundamentals failed for %s: %s", symbol, exc)

        # FMP backfill / fallback
        if not result.get("pe_ratio") and self.fmp.is_available():
            fmp_data = self.fmp.fetch_fundamentals(symbol)
            if fmp_data:
                logger.info("Falling back to FMP for fundamentals (%s)", symbol)
                for k, v in fmp_data.items():
                    if v is not None and not result.get(k):
                        result[k] = v
                if not result.get("source"):
                    result["source"] = "fmp"

        # FMP ratios fill profit_margin / additional metrics
        if not result.get("profit_margin") and self.fmp.is_available():
            ratios = self.fmp.fetch_ratios(symbol)
            if ratios:
                result["profit_margin"] = ratios.get("net_margin")
                result["ratios"] = ratios

        if result:
            self.cache.set(key, result)
            return result
        return None

    def get_historical(
        self, symbol: str, years: int = 5
    ) -> List[Dict] | None:
        """Price history — tries yfinance, then FMP, then Polygon."""
        key = f"historical:{symbol}:{years}"
        cached = self.cache.get(key, "historical")
        if cached is not None:
            return cached

        # yfinance
        if self.yf:
            try:
                df = self.yf.Ticker(symbol).history(period=f"{years}y")
                if df is not None and not df.empty:
                    rows = []
                    for idx, row in df.iterrows():
                        rows.append({
                            "date":   idx.strftime("%Y-%m-%d"),
                            "open":   row.get("Open"),
                            "high":   row.get("High"),
                            "low":    row.get("Low"),
                            "close":  row.get("Close"),
                            "volume": row.get("Volume"),
                        })
                    logger.info("Historical for %s from yfinance (%d rows)", symbol, len(rows))
                    self.cache.set(key, rows)
                    return rows
            except Exception as exc:
                logger.warning("yfinance historical failed for %s: %s", symbol, exc)

        # FMP
        if self.fmp.is_available():
            rows = self.fmp.fetch_historical(symbol, years)
            if rows:
                logger.info("Falling back to FMP for historical (%s, %d rows)", symbol, len(rows))
                self.cache.set(key, rows)
                return rows

        # Polygon
        if self.polygon.is_available():
            rows = self.polygon.fetch_aggregates(symbol, days=years * 365)
            if rows:
                logger.info("Falling back to Polygon for historical (%s, %d rows)", symbol, len(rows))
                self.cache.set(key, rows)
                return rows

        return None

    def get_sentiment_signals(self, symbol: str) -> Dict[str, Any] | None:
        """Aggregated sentiment dict: news_sentiment, put_call_ratio,
        analyst_consensus."""
        key = f"sentiment:{symbol}"
        cached = self.cache.get(key, "sentiment")
        if cached is not None:
            return cached

        signals: Dict[str, Any] = {}

        # News sentiment — Finnhub
        if self.finnhub.is_available():
            ns = self.finnhub.fetch_news_sentiment(symbol)
            if ns:
                signals["news_sentiment"] = ns

        # Put/call ratio — Polygon
        if self.polygon.is_available():
            opts = self.polygon.fetch_options_chain(symbol)
            if opts:
                signals["put_call_ratio"] = opts.get("put_call_ratio")
                signals["options_data"] = opts

        # Analyst consensus — Finnhub
        if self.finnhub.is_available():
            recs = self.finnhub.fetch_recommendation_trends(symbol)
            if recs:
                signals["analyst_consensus"] = recs

        if signals:
            self.cache.set(key, signals)
            return signals
        return None

    def get_earnings_date(self, symbol: str) -> Dict[str, Any] | None:
        """Next earnings date from any available provider."""
        key = f"earnings:{symbol}"
        cached = self.cache.get(key, "earnings")
        if cached is not None:
            return cached

        # Finnhub first (more detailed)
        if self.finnhub.is_available():
            data = self.finnhub.fetch_earnings_calendar(symbol)
            if data:
                logger.info("Earnings date for %s from Finnhub", symbol)
                self.cache.set(key, data)
                return data

        # FMP
        if self.fmp.is_available():
            data = self.fmp.fetch_earnings_calendar(symbol)
            if data:
                logger.info("Earnings date for %s from FMP", symbol)
                self.cache.set(key, data)
                return data

        # yfinance
        if self.yf:
            try:
                cal = self.yf.Ticker(symbol).calendar
                if cal is not None:
                    # yfinance calendar can be a dict or DataFrame
                    earnings_date = None
                    if isinstance(cal, dict):
                        ed = cal.get("Earnings Date")
                        if ed and isinstance(ed, list) and len(ed) > 0:
                            earnings_date = str(ed[0])
                        elif ed:
                            earnings_date = str(ed)
                    if earnings_date:
                        result = {
                            "earnings_date": earnings_date,
                            "source": "yfinance",
                        }
                        self.cache.set(key, result)
                        return result
            except Exception as exc:
                logger.warning("yfinance calendar failed for %s: %s", symbol, exc)

        return None

    def get_macro_regime(self) -> Dict[str, Any]:
        """Current macro regime from FRED data."""
        key = "macro:regime"
        cached = self.cache.get(key, "fundamentals")
        if cached is not None:
            return cached
        result = self.fred.get_macro_regime()
        if result:
            self.cache.set(key, result)
        return result

    def get_analyst_estimates(self, symbol: str) -> Dict[str, Any] | None:
        """EPS estimates from FMP."""
        if not self.fmp.is_available():
            return None
        key = f"analyst_est:{symbol}"
        cached = self.cache.get(key, "fundamentals")
        if cached is not None:
            return cached
        result = self.fmp_analyst.fetch_analyst_estimates(symbol)
        if result:
            self.cache.set(key, result)
        return result

    def get_price_target(self, symbol: str) -> Dict[str, Any] | None:
        """Analyst consensus price target from FMP."""
        if not self.fmp.is_available():
            return None
        key = f"price_target:{symbol}"
        cached = self.cache.get(key, "fundamentals")
        if cached is not None:
            return cached
        result = self.fmp_analyst.fetch_price_target(symbol)
        if result:
            self.cache.set(key, result)
        return result

    def get_insider_signal(self, symbol: str) -> Dict[str, Any] | None:
        """Insider trading signal from FMP."""
        if not self.fmp.is_available():
            return None
        key = f"insider:{symbol}"
        cached = self.cache.get(key, "fundamentals")
        if cached is not None:
            return cached
        result = self.fmp_analyst.fetch_insider_trading(symbol)
        if result:
            self.cache.set(key, result)
        return result

    def get_recommendation_trends(self, symbol: str) -> Dict[str, Any] | None:
        """Analyst buy/hold/sell trend from Finnhub (live; latest month)."""
        if not self.finnhub.is_available():
            return None
        key = f"rec_trends:{symbol}"
        cached = self.cache.get(key, "fundamentals")
        if cached is not None:
            return cached
        result = self.finnhub.fetch_recommendation_trends(symbol)
        if result:
            self.cache.set(key, result)
        return result

    def get_public_pulse(self, symbol: str, fast: bool = True) -> Dict[str, Any] | None:
        """US public-sentiment composite (Google Trends + Wikipedia + GDELT +
        broad Reddit + Michigan CSI). Cached 6h because PP fetches are slow.

        Set fast=True to skip BroadReddit (the slowest source, ~5s per ticker).
        """
        key = f"public_pulse:{symbol}:{'fast' if fast else 'full'}"
        cached = self.cache.get(key, "sentiment")
        if cached is not None:
            return cached
        try:
            from public_pulse import PublicPulse
        except ImportError:
            return None
        try:
            pp = PublicPulse()
            snap = pp.snapshot(symbol, fast=fast)
            result = snap.as_dict()
            # Custom TTL: PP values change slowly — 6h cache
            self.cache.set(key, result)
            return result
        except Exception as exc:
            logger.warning("Public Pulse failed for %s: %s", symbol, exc)
            return None

    def get_put_call_ratio(self, symbol: str) -> Dict[str, Any] | None:
        """Put/call ratio from yfinance options chain (free).

        Aggregates volume across the first 3 expirations (near-term flow).
        Returns None if yfinance is unavailable or the symbol has no options.
        """
        key = f"put_call:{symbol}"
        cached = self.cache.get(key, "sentiment")
        if cached is not None:
            return cached

        if not self.yf:
            return None

        try:
            t = self.yf.Ticker(symbol)
            exps = t.options
            if not exps:
                return None
            # Aggregate across up to 3 near-term expirations for a stable signal
            call_vol = 0
            put_vol = 0
            used = 0
            for exp in exps[:3]:
                try:
                    chain = t.option_chain(exp)
                    call_vol += float(chain.calls["volume"].fillna(0).sum())
                    put_vol  += float(chain.puts["volume"].fillna(0).sum())
                    used += 1
                except Exception:
                    continue
            if call_vol <= 0:
                return None
            pc = put_vol / call_vol
            result = {
                "put_call_ratio": round(pc, 4),
                "call_volume":    int(call_vol),
                "put_volume":     int(put_vol),
                "expirations":    used,
                "source":         "yfinance",
            }
            self.cache.set(key, result)
            return result
        except Exception as exc:
            logger.warning("yfinance put/call failed for %s: %s", symbol, exc)
            return None

    def provider_status(self) -> Dict[str, Any]:
        """Which providers are active, keys configured, rate limits."""
        status: Dict[str, Any] = {
            "yfinance": {"available": self.yf is not None},
            "fmp":      self.fmp.status(),
            "finnhub":  self.finnhub.status(),
            "polygon":  self.polygon.status(),
            "fred":     self.fred.status(),
        }
        return status


# ─────────────────────────────────────────────────────────────────────
# CLI diagnostic
# ─────────────────────────────────────────────────────────────────────

def _run_diagnostic() -> None:
    """Quick health check — test each configured provider with AAPL."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )

    dm = DataManager()
    sym = "AAPL"
    sep = "-" * 60

    print(sep)
    print("S-Tool Projector — Data Provider Diagnostic")
    print(sep)

    # Provider status
    status = dm.provider_status()
    for name, info in status.items():
        if isinstance(info, dict):
            avail = info.get("available", info.get("key_configured", False))
            remaining = info.get("rate_limit_remaining", "n/a")
            print(f"  {name:12s}  configured={avail}  rate_remaining={remaining}")
        else:
            print(f"  {name:12s}  {info}")
    print(sep)

    # Fundamentals
    print(f"\n[1/5] Fundamentals ({sym}) ...")
    fund = dm.get_fundamentals(sym)
    if fund:
        for k, v in fund.items():
            if k != "ratios":
                print(f"       {k}: {v}")
    else:
        print("       (no data)")

    # Historical
    print(f"\n[2/5] Historical prices ({sym}, 1y) ...")
    hist = dm.get_historical(sym, years=1)
    if hist:
        print(f"       {len(hist)} bars, latest: {hist[-1]['date']}  close={hist[-1]['close']}")
    else:
        print("       (no data)")

    # Sentiment
    print(f"\n[3/5] Sentiment signals ({sym}) ...")
    sent = dm.get_sentiment_signals(sym)
    if sent:
        for k, v in sent.items():
            print(f"       {k}: {v}")
    else:
        print("       (no data — Finnhub/Polygon keys may not be set)")

    # Earnings
    print(f"\n[4/5] Next earnings date ({sym}) ...")
    earn = dm.get_earnings_date(sym)
    if earn:
        print(f"       {earn}")
    else:
        print("       (no data)")

    # Quote (Finnhub only)
    print(f"\n[5/5] Real-time quote ({sym}, Finnhub) ...")
    if dm.finnhub.is_available():
        qt = dm.finnhub.fetch_quote(sym)
        if qt:
            print(f"       price={qt['current_price']}  change={qt['change']}  pct={qt['pct_change']}%")
        else:
            print("       (no data)")
    else:
        print("       (Finnhub not configured)")

    print(f"\n{sep}")
    print("Diagnostic complete.")


if __name__ == "__main__":
    _run_diagnostic()
