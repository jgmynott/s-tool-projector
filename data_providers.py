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
    BASE = "https://financialmodelingprep.com/api/v3"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("FMP_API_KEY")
        self.limiter = DailyLimiter(250)

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
        """P/E, market cap, EPS, beta, dividend yield, sector, industry,
        revenue, profit margin."""
        data = self._get(f"profile/{symbol}")
        if not data:
            return None
        try:
            rec = data[0] if isinstance(data, list) else data
            return {
                "pe_ratio":       rec.get("pe"),
                "market_cap":     rec.get("mktCap"),
                "eps":            rec.get("eps") if rec.get("eps") else None,
                "beta":           rec.get("beta"),
                "dividend_yield": rec.get("lastDiv"),
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
        data = self._get(f"historical-price-full/{symbol}", {
            "timeseries": years * 252,
        })
        if not data or "historical" not in data:
            return None
        rows = data["historical"]
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
        data = self._get(f"ratios/{symbol}", {"limit": 1})
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
                "pe_ratio":              rec.get("priceEarningsRatio"),
                "price_to_book":         rec.get("priceToBookRatio"),
                "price_to_sales":        rec.get("priceToSalesRatio"),
                "source":                "fmp",
            }
        except (IndexError, KeyError, TypeError) as exc:
            logger.warning("FMP ratios parse error: %s", exc)
            return None

    def fetch_earnings_calendar(self, symbol: str) -> Dict[str, Any] | None:
        """Next earnings date for *symbol*."""
        data = self._get(f"earning_calendar", {"symbol": symbol})
        if not data:
            return None
        try:
            # FMP returns a list sorted by date; pick first future date
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            for rec in data:
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

    def provider_status(self) -> Dict[str, Any]:
        """Which providers are active, keys configured, rate limits."""
        status: Dict[str, Any] = {
            "yfinance": {"available": self.yf is not None},
            "fmp":      self.fmp.status(),
            "finnhub":  self.finnhub.status(),
            "polygon":  self.polygon.status(),
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
