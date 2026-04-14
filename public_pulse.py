"""
public_pulse.py
───────────────
"Public Pulse" — US general-population sentiment signal for the S-Tool Projector.

Unlike our existing sentiment sources (StockTwits, WSB) which capture *finance-crowd*
sentiment, Public Pulse aims to capture regular-person sentiment: what ordinary
Americans are searching for, reading about, talking about in non-finance forums,
and feeling about the economy. These signals often LEAD price because they
front-run earnings surprises, consumer-spending shifts, and macro turns.

Sources (all free-tier):
  1. Google Trends        — search interest per ticker/brand (pytrends)
  2. Wikipedia             — pageview spikes on company pages (Wikimedia REST API)
  3. GDELT                 — mainstream news tone (GDELT 2.0 GKG CSV)
  4. Broad Reddit          — r/news, r/economy, r/personalfinance, r/investing
                             (arctic-shift + FinBERT — reuses existing pipeline)
  5. Michigan CSI          — consumer confidence (FRED UMCSENT, already live)

Composite score: weighted average of normalised per-source scores, each in [-1, +1].
Feeds into projector_engine.compute_fundamental_tilt via a new "public_pulse" weight.

Usage:
    # Live snapshot for a ticker today
    python3 public_pulse.py snapshot AAPL

    # Historical collection (backfill)
    python3 public_pulse.py historical AAPL --start 2023-01-01 --end 2026-04-01

    # Smoke-test each source
    python3 public_pulse.py test
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# Ticker → lookup terms for Google Trends / Wikipedia
# Where the ticker symbol alone is ambiguous, use company name.
TICKER_TERMS: Dict[str, Dict[str, str]] = {
    "AAPL":  {"trends": "Apple stock",          "wiki": "Apple_Inc."},
    "MSFT":  {"trends": "Microsoft stock",      "wiki": "Microsoft"},
    "GOOGL": {"trends": "Google stock",         "wiki": "Alphabet_Inc."},
    "AMZN":  {"trends": "Amazon stock",         "wiki": "Amazon_(company)"},
    "TSLA":  {"trends": "Tesla stock",          "wiki": "Tesla,_Inc."},
    "NVDA":  {"trends": "Nvidia stock",         "wiki": "Nvidia"},
    "META":  {"trends": "Meta stock",           "wiki": "Meta_Platforms"},
    "NFLX":  {"trends": "Netflix stock",        "wiki": "Netflix"},
    "JPM":   {"trends": "JPMorgan stock",       "wiki": "JPMorgan_Chase"},
    "BAC":   {"trends": "Bank of America stock","wiki": "Bank_of_America"},
    "V":     {"trends": "Visa stock",           "wiki": "Visa_Inc."},
    "WMT":   {"trends": "Walmart stock",        "wiki": "Walmart"},
    "DIS":   {"trends": "Disney stock",         "wiki": "The_Walt_Disney_Company"},
    "KO":    {"trends": "Coca-Cola stock",      "wiki": "The_Coca-Cola_Company"},
    "PEP":   {"trends": "Pepsi stock",          "wiki": "PepsiCo"},
    "NKE":   {"trends": "Nike stock",           "wiki": "Nike,_Inc."},
    "SBUX":  {"trends": "Starbucks stock",      "wiki": "Starbucks"},
    "MCD":   {"trends": "McDonald's stock",     "wiki": "McDonald%27s"},
    # Fallback: symbol itself
}

BROAD_REDDIT_SUBS = [
    "news",           # mainstream news discussion
    "economy",        # general economic chatter
    "personalfinance",  # retail money mood
    "investing",      # broader than wallstreetbets
    "stocks",         # less degen than WSB
]


def _term_for(symbol: str, kind: str) -> str:
    """Return the lookup term for a ticker. Falls back to the symbol itself."""
    entry = TICKER_TERMS.get(symbol.upper())
    if entry and kind in entry:
        return entry[kind]
    return symbol.upper()


# ─────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────

@dataclass
class PulseComponent:
    """A single source's contribution to the Public Pulse composite."""
    source: str
    raw_value: Optional[float]    # native value (e.g. pageviews, search index)
    normalised: Optional[float]   # z-score-ish in [-1, +1]
    weight: float                 # contribution weight
    sample_size: Optional[int]    # articles/posts/etc.
    fresh_as_of: Optional[str]    # ISO date string
    source_url: Optional[str] = None
    note: Optional[str] = None


@dataclass
class PulseSnapshot:
    """Composite Public Pulse result for a symbol at a point in time."""
    symbol: str
    computed_at: str
    composite_score: Optional[float]      # weighted avg of normalised components
    components: List[PulseComponent] = field(default_factory=list)
    active_sources: int = 0
    total_sources: int = 5

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "computed_at": self.computed_at,
            "composite_score": self.composite_score,
            "active_sources": self.active_sources,
            "total_sources": self.total_sources,
            "components": [asdict(c) for c in self.components],
        }


# ─────────────────────────────────────────────────────────────────────
# 1. Google Trends
# ─────────────────────────────────────────────────────────────────────

class GoogleTrendsCollector:
    """US search interest via pytrends.

    Google normalises interest to 0-100 within the requested window.
    We fetch the last 90 days and score:
      - Current (last 7d avg) vs 90d baseline → z-ish score
    """
    name = "google_trends"
    weight = 0.25

    def __init__(self):
        try:
            from pytrends.request import TrendReq
            # Use defaults — passing retries= triggers an urllib3 v2 kwarg error
            # on older pytrends. Our pinned version (≥4.9.3) handles this.
            self.trends = TrendReq(hl="en-US", tz=300)
            self.available = True
        except ImportError:
            self.available = False
            logger.warning("pytrends not installed — Google Trends disabled")
        except Exception as exc:
            self.available = False
            logger.warning("pytrends init failed: %s", exc)

    def snapshot(self, symbol: str) -> PulseComponent:
        if not self.available:
            return PulseComponent(self.name, None, None, self.weight, None, None, note="pytrends missing")

        term = _term_for(symbol, "trends")
        try:
            # 3-month daily series, US-only
            self.trends.build_payload([term], cat=0, timeframe="today 3-m", geo="US")
            df = self.trends.interest_over_time()
            if df is None or df.empty:
                return PulseComponent(self.name, None, None, self.weight, 0, None, note="no data")

            series = df[term].astype(float)
            # Baseline = mean of earliest 60 days; current = mean of last 7
            if len(series) < 30:
                return PulseComponent(self.name, None, None, self.weight, len(series), None, note="short series")

            baseline = series.iloc[:-7].mean() if len(series) > 14 else series.iloc[:-1].mean()
            current = series.iloc[-7:].mean()
            # Normalised: (current - baseline) / max(baseline, 1) clamped to [-1, +1]
            if baseline <= 0:
                norm = 0.0
            else:
                rel = (current - baseline) / max(baseline, 1.0)
                norm = max(-1.0, min(1.0, rel))

            return PulseComponent(
                source=self.name,
                raw_value=round(current, 1),
                normalised=round(norm, 4),
                weight=self.weight,
                sample_size=len(series),
                fresh_as_of=str(df.index[-1].date()),
                source_url=f"https://trends.google.com/trends/explore?geo=US&q={term.replace(' ', '%20')}",
                note=f"term='{term}' baseline={baseline:.1f} current={current:.1f}",
            )
        except Exception as exc:
            logger.warning("Google Trends failed for %s: %s", symbol, exc)
            return PulseComponent(self.name, None, None, self.weight, None, None, note=f"error: {exc}")

    def historical(self, symbol: str, start: str, end: str) -> List[Dict]:
        """Return daily series of [{date, value}] over [start, end]."""
        if not self.available:
            return []
        term = _term_for(symbol, "trends")
        try:
            self.trends.build_payload(
                [term], cat=0, timeframe=f"{start} {end}", geo="US"
            )
            df = self.trends.interest_over_time()
            if df is None or df.empty:
                return []
            return [
                {"date": str(idx.date()), "value": float(row[term])}
                for idx, row in df.iterrows()
            ]
        except Exception as exc:
            logger.warning("Google Trends historical failed for %s: %s", symbol, exc)
            return []


# ─────────────────────────────────────────────────────────────────────
# 2. Wikipedia pageviews
# ─────────────────────────────────────────────────────────────────────

class WikipediaCollector:
    """Wikimedia REST API: daily pageviews on the company's en.wiki page.

    Spikes correlate with attention — bearish or bullish depending on sign of news,
    but directionally informative when combined with other signals.
    """
    name = "wikipedia"
    weight = 0.20
    BASE = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
            "en.wikipedia.org/all-access/user")

    UA = "S-Tool-Projector/1.0 (https://s-tool.io; stool@s-tool.io)"

    def snapshot(self, symbol: str) -> PulseComponent:
        page = _term_for(symbol, "wiki")
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=90)
        url = (
            f"{self.BASE}/{page}/daily/"
            f"{start.strftime('%Y%m%d')}00/{end.strftime('%Y%m%d')}00"
        )
        try:
            r = requests.get(url, headers={"User-Agent": self.UA}, timeout=15)
            if r.status_code == 404:
                return PulseComponent(self.name, None, None, self.weight, None, None, note=f"page not found: {page}")
            r.raise_for_status()
            items = r.json().get("items", [])
            if not items:
                return PulseComponent(self.name, None, None, self.weight, 0, None, note="empty")

            views = [it["views"] for it in items]
            last7 = views[-7:] if len(views) >= 7 else views
            baseline = views[:-7] if len(views) > 14 else views
            current = sum(last7) / len(last7)
            base_mean = sum(baseline) / len(baseline) if baseline else current
            if base_mean <= 0:
                norm = 0.0
            else:
                rel = (current - base_mean) / max(base_mean, 1.0)
                norm = max(-1.0, min(1.0, rel))

            return PulseComponent(
                source=self.name,
                raw_value=round(current, 0),
                normalised=round(norm, 4),
                weight=self.weight,
                sample_size=len(items),
                fresh_as_of=items[-1]["timestamp"][:8],
                source_url=f"https://en.wikipedia.org/wiki/{page}",
                note=f"page={page} last7_avg={current:.0f} baseline={base_mean:.0f}",
            )
        except Exception as exc:
            logger.warning("Wikipedia failed for %s: %s", symbol, exc)
            return PulseComponent(self.name, None, None, self.weight, None, None, note=f"error: {exc}")

    def historical(self, symbol: str, start: str, end: str) -> List[Dict]:
        page = _term_for(symbol, "wiki")
        url = (
            f"{self.BASE}/{page}/daily/"
            f"{start.replace('-','')}00/{end.replace('-','')}00"
        )
        try:
            r = requests.get(url, headers={"User-Agent": self.UA}, timeout=30)
            r.raise_for_status()
            items = r.json().get("items", [])
            return [
                {"date": f"{it['timestamp'][:4]}-{it['timestamp'][4:6]}-{it['timestamp'][6:8]}",
                 "value": it["views"]}
                for it in items
            ]
        except Exception as exc:
            logger.warning("Wikipedia historical failed for %s: %s", symbol, exc)
            return []


# ─────────────────────────────────────────────────────────────────────
# 3. GDELT 2.0 news tone
# ─────────────────────────────────────────────────────────────────────

class GDELTCollector:
    """GDELT 2.0 DOC API — mainstream-news sentiment via TimelineTone mode.

    The DOC API has a **5-second** rate limit; we throttle accordingly.
    Returns aggregated tone value across the last 7 days.
    """
    name = "gdelt"
    weight = 0.22
    DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
    _last_request_ts = 0.0  # class-level throttle clock

    @classmethod
    def _throttle(cls):
        delta = time.time() - cls._last_request_ts
        if delta < 5.1:
            time.sleep(5.1 - delta)
        cls._last_request_ts = time.time()

    def snapshot(self, symbol: str) -> PulseComponent:
        term = _term_for(symbol, "trends").replace("+", " ")
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=7)
        params = {
            "query":          f'"{term}" sourcecountry:US',
            "mode":           "TimelineTone",
            "format":         "json",
            "startdatetime":  start.strftime("%Y%m%d%H%M%S"),
            "enddatetime":    end.strftime("%Y%m%d%H%M%S"),
        }
        try:
            self._throttle()
            r = requests.get(self.DOC_API, params=params, timeout=20)
            if r.status_code == 429:
                return PulseComponent(self.name, None, None, self.weight, None, None, note="rate limited")
            r.raise_for_status()
            data = r.json()
            timeline = data.get("timeline", [])
            if not timeline:
                return PulseComponent(self.name, None, None, self.weight, 0, None, note="empty timeline")

            rows = timeline[0].get("data", [])
            vals = [float(x["value"]) for x in rows if "value" in x]
            if not vals:
                return PulseComponent(self.name, None, None, self.weight, 0, None, note="no tone values")

            avg = sum(vals) / len(vals)
            # Empirical: GDELT daily tone typically lives in [-5, +5]; /5 + clamp.
            norm = max(-1.0, min(1.0, avg / 5.0))

            return PulseComponent(
                source=self.name,
                raw_value=round(avg, 3),
                normalised=round(norm, 4),
                weight=self.weight,
                sample_size=len(vals),
                fresh_as_of=end.strftime("%Y-%m-%d"),
                source_url=f"https://api.gdeltproject.org/api/v2/doc/doc?query=%22{term.replace(' ', '%20')}%22&mode=TimelineTone",
                note=f"{len(vals)} 15-min tone samples (7d), mean {avg:+.2f}",
            )
        except Exception as exc:
            logger.warning("GDELT failed for %s: %s", symbol, exc)
            return PulseComponent(self.name, None, None, self.weight, None, None, note=f"error: {exc}")

    def historical(self, symbol: str, start: str, end: str) -> List[Dict]:
        """GDELT TimelineTone returns daily tone — perfect for historical backfill."""
        term = _term_for(symbol, "trends").replace(" ", "+")
        params = {
            "query":    f'"{term.replace("+", " ")}" sourcecountry:US',
            "mode":     "TimelineTone",
            "format":   "json",
            "startdatetime": start.replace("-", "") + "000000",
            "enddatetime":   end.replace("-", "") + "235959",
        }
        try:
            self._throttle()   # respect 5s rate limit
            r = requests.get(self.DOC_API, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            timeline = data.get("timeline", [])
            if not timeline:
                return []
            rows = timeline[0].get("data", [])
            out = []
            for rec in rows:
                dt = rec.get("date", "")
                if len(dt) >= 8:
                    out.append({
                        "date": f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}",
                        "value": float(rec.get("value", 0)),
                    })
            return out
        except Exception as exc:
            logger.warning("GDELT historical failed for %s: %s", symbol, exc)
            return []


# ─────────────────────────────────────────────────────────────────────
# 4. Broad Reddit (arctic-shift, reuses existing pipeline conceptually)
# ─────────────────────────────────────────────────────────────────────

class BroadRedditCollector:
    """Reddit posts mentioning ticker across broader subs than WSB.

    Uses Reddit's JSON search endpoint directly (no auth needed). Compares
    recent attention (past week) to the monthly baseline — spikes signal
    increased retail attention, regardless of finance-crowd bias.
    """
    name = "broad_reddit"
    weight = 0.18
    UA = "S-Tool-Projector/1.0 (by u/stool)"

    def _search_count(self, sub: str, symbol: str, t: str) -> int:
        """Count results from /r/{sub}/search.json?q={symbol}&t={t}.

        `t`: hour/day/week/month/year/all — Reddit's time filter.
        """
        url = f"https://www.reddit.com/r/{sub}/search.json"
        params = {"q": symbol, "restrict_sr": "on", "sort": "new",
                  "limit": 100, "t": t}
        try:
            r = requests.get(url, params=params, headers={"User-Agent": self.UA}, timeout=15)
            if r.status_code != 200:
                return 0
            data = r.json()
            return len(data.get("data", {}).get("children", []))
        except Exception:
            return 0

    def snapshot(self, symbol: str) -> PulseComponent:
        last7 = 0
        month = 0
        subs_used = 0
        try:
            for sub in BROAD_REDDIT_SUBS:
                wk = self._search_count(sub, symbol, "week")
                mo = self._search_count(sub, symbol, "month")
                last7 += wk
                month += mo
                if wk or mo:
                    subs_used += 1
                time.sleep(0.4)  # politeness

            # per-day averages: last-7 is over ~7d, last-month ~30d
            last7_pd = last7 / 7
            # baseline = avg daily over the non-last-7 portion of the month (~23 days)
            older = max(month - last7, 0)
            baseline_pd = older / 23 if older > 0 else 0

            if baseline_pd <= 0:
                norm = 0.5 if last7_pd > 0 else 0.0
            else:
                rel = (last7_pd - baseline_pd) / max(baseline_pd, 1.0)
                norm = max(-1.0, min(1.0, rel))

            return PulseComponent(
                source=self.name,
                raw_value=round(last7_pd, 2),
                normalised=round(norm, 4),
                weight=self.weight,
                sample_size=last7 + month,
                fresh_as_of=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                source_url=f"https://www.reddit.com/search?q={symbol}",
                note=(f"last7_pd={last7_pd:.2f} baseline_pd={baseline_pd:.2f} "
                      f"across {subs_used}/{len(BROAD_REDDIT_SUBS)} subs"),
            )
        except Exception as exc:
            logger.warning("Broad Reddit failed for %s: %s", symbol, exc)
            return PulseComponent(self.name, None, None, self.weight, None, None, note=f"error: {exc}")


# ─────────────────────────────────────────────────────────────────────
# 5. Michigan CSI (wraps FRED)
# ─────────────────────────────────────────────────────────────────────

class MichiganCSICollector:
    """University of Michigan Consumer Sentiment Index via FRED.

    This is a MACRO signal — it's the same value for all symbols at a given time,
    but it's a classic lead indicator for discretionary consumer spending.
    Normalised against the post-1980 series mean (≈ 85) and sd (≈ 15).
    """
    name = "michigan_csi"
    weight = 0.15
    SERIES_ID = "UMCSENT"
    # Long-run stats of UMCSENT (1978-2024, monthly)
    MEAN = 85.0
    SD = 15.0

    def __init__(self):
        # Lazy import of FRED provider
        try:
            from data_providers import FREDProvider
            self.fred = FREDProvider()
            self.available = True
        except ImportError:
            self.available = False
            self.fred = None

    def snapshot(self, symbol: str) -> PulseComponent:
        if not self.available:
            return PulseComponent(self.name, None, None, self.weight, None, None, note="FRED unavailable")
        try:
            val = self.fred._fetch_series_latest(self.SERIES_ID)
            if val is None:
                return PulseComponent(self.name, None, None, self.weight, None, None, note="no data")
            # Z-score, clamped to [-1, +1]
            z = (val - self.MEAN) / self.SD
            norm = max(-1.0, min(1.0, z / 2.0))  # z=2 → norm=1

            return PulseComponent(
                source=self.name,
                raw_value=round(val, 1),
                normalised=round(norm, 4),
                weight=self.weight,
                sample_size=1,
                fresh_as_of=datetime.now().strftime("%Y-%m-%d"),
                source_url="https://fred.stlouisfed.org/series/UMCSENT",
                note=f"UMCSENT={val:.1f} (mean={self.MEAN}, sd={self.SD}), z={z:+.2f}",
            )
        except Exception as exc:
            logger.warning("Michigan CSI failed: %s", exc)
            return PulseComponent(self.name, None, None, self.weight, None, None, note=f"error: {exc}")


# ─────────────────────────────────────────────────────────────────────
# Composite
# ─────────────────────────────────────────────────────────────────────

class PublicPulse:
    """Orchestrates all 5 sources into a single composite score."""

    def __init__(self):
        self.collectors = [
            GoogleTrendsCollector(),
            WikipediaCollector(),
            GDELTCollector(),
            BroadRedditCollector(),
            MichiganCSICollector(),
        ]

    def snapshot(self, symbol: str, fast: bool = False) -> PulseSnapshot:
        """Fetch and combine all sources for `symbol`.

        If `fast` is True, skip slow sources (BroadReddit is the slowest).
        """
        components: List[PulseComponent] = []
        for c in self.collectors:
            if fast and c.name == "broad_reddit":
                continue
            components.append(c.snapshot(symbol))

        # Weighted composite of normalised values (ignoring None)
        num = 0.0
        denom = 0.0
        active = 0
        for c in components:
            if c.normalised is None:
                continue
            num += c.normalised * c.weight
            denom += c.weight
            active += 1
        composite = round(num / denom, 4) if denom > 0 else None

        return PulseSnapshot(
            symbol=symbol.upper(),
            computed_at=datetime.now(timezone.utc).isoformat(),
            composite_score=composite,
            components=components,
            active_sources=active,
            total_sources=len(self.collectors),
        )


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def _fmt_snapshot(s: PulseSnapshot) -> str:
    lines = [
        "─" * 68,
        f"Public Pulse — {s.symbol}   @ {s.computed_at}",
        "─" * 68,
    ]
    composite = s.composite_score
    if composite is not None:
        mood = "BULLISH" if composite > 0.1 else "BEARISH" if composite < -0.1 else "NEUTRAL"
        lines.append(f"Composite score: {composite:+.3f}  ({mood})")
    else:
        lines.append("Composite score: (no data)")
    lines.append(f"Active sources:  {s.active_sources} / {s.total_sources}")
    lines.append("─" * 68)
    for c in s.components:
        mark = "✓" if c.normalised is not None else "✗"
        norm = f"{c.normalised:+.3f}" if c.normalised is not None else "  —  "
        raw = f"{c.raw_value:>10}" if c.raw_value is not None else "     —    "
        lines.append(f"  {mark} {c.source:14s}  norm={norm}  raw={raw}  w={c.weight:.2f}")
        if c.note:
            lines.append(f"      └ {c.note}")
    lines.append("─" * 68)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Public Pulse collector CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    snap = sub.add_parser("snapshot", help="Live snapshot for a symbol")
    snap.add_argument("symbol")
    snap.add_argument("--fast", action="store_true", help="Skip slow sources (BroadReddit)")

    test = sub.add_parser("test", help="Smoke-test all sources on AAPL")

    hist = sub.add_parser("historical", help="Backfill one source across a date range")
    hist.add_argument("symbol")
    hist.add_argument("--source", required=True, choices=["google_trends", "wikipedia", "gdelt"])
    hist.add_argument("--start", required=True, help="YYYY-MM-DD")
    hist.add_argument("--end", required=True, help="YYYY-MM-DD")
    hist.add_argument("--out", help="CSV output path (default stdout)")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    # Load .env if present for FRED key
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    if args.cmd == "snapshot":
        pp = PublicPulse()
        snap = pp.snapshot(args.symbol, fast=args.fast)
        print(_fmt_snapshot(snap))

    elif args.cmd == "test":
        pp = PublicPulse()
        snap = pp.snapshot("AAPL", fast=False)
        print(_fmt_snapshot(snap))

    elif args.cmd == "historical":
        collector_map = {
            "google_trends": GoogleTrendsCollector(),
            "wikipedia":     WikipediaCollector(),
            "gdelt":         GDELTCollector(),
        }
        c = collector_map[args.source]
        if not hasattr(c, "historical"):
            print(f"No historical support for {args.source}")
            sys.exit(1)
        rows = c.historical(args.symbol, args.start, args.end)
        print(f"Rows: {len(rows)}")
        if args.out:
            import csv
            with open(args.out, "w", newline="") as f:
                if rows:
                    w = csv.DictWriter(f, fieldnames=rows[0].keys())
                    w.writeheader()
                    w.writerows(rows)
            print(f"Wrote → {args.out}")
        else:
            for r in rows[:10]:
                print(r)
            if len(rows) > 10:
                print(f"... ({len(rows)-10} more)")


if __name__ == "__main__":
    main()
