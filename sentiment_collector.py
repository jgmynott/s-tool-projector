"""
Phase B — Historical retail sentiment collector for the S-Tool projector.

Pulls /r/wallstreetbets submissions + comments from the arctic-shift archive
(the public mirror of the old Pushshift dump), tags ticker mentions for our
backtest universe, and runs FinBERT (`ProsusAI/finbert`) over the matched
text. Writes a daily sentiment time series so the projector backtester can
later test whether retail tilt has any predictive value as a drift adjustment.

Outputs:
    sentiment_<symbol>_<start>_<end>.csv      one file per symbol, daily rows
    sentiment_combined_<start>_<end>.csv      stacked long-form for all symbols

Usage:
    python sentiment_collector.py --symbols TSLA NVDA --start 2024-01-01 --end 2024-02-01
    python sentiment_collector.py --all --start 2023-01-01 --end 2024-01-01

Notes:
- arctic-shift is rate-limited; we throttle to ~2 req/s and chunk by day so
  one bad day can't take out the run.
- FinBERT runs locally on CPU. ~30ms per sample on a typical Mac, so 10k texts
  is around 5 minutes. We batch (32) and short-circuit empty days.
- This script intentionally does NOT touch the projector itself. Its only job
  is to produce a clean CSV that the backtester can join on (date, ticker).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import requests

# Universe — match projector_backtest.DEFAULT_SYMBOLS so we can join later.
DEFAULT_SYMBOLS = [
    "AAPL", "MSFT", "GOOGL", "NVDA", "AMZN",
    "SPY", "QQQ", "IWM",
    "XLF", "XLE", "XLV",
    "JPM", "JNJ", "PG",
    "TSLA",
]

# arctic-shift mirror of the old Pushshift dump.
# Public, no key required, but rate-limited.
ARCTIC_BASE = "https://arctic-shift.photon-reddit.com/api"
SUB = "wallstreetbets"

# Words to never treat as a ticker even though uppercase pattern matches.
# Aggressive list — false positives in WSB chatter would dominate signal.
TICKER_BLOCKLIST = {
    "A", "I", "AM", "PM", "AT", "BE", "DO", "GO", "HE", "IF", "IN", "IS",
    "IT", "ME", "MY", "NO", "OF", "ON", "OR", "SO", "TO", "UP", "US", "WE",
    "YOLO", "WSB", "FD", "DD", "ATH", "ATM", "OTM", "ITM", "EOD", "EOY",
    "PR", "IPO", "CEO", "CFO", "ETF", "USD", "EU", "UK", "FOMO", "BTFD",
    "TLDR", "EPS", "PE", "PT", "GDP", "FED", "CPI", "QE", "QT", "TLT",
    "TBH", "IMO", "FYI", "AFAIK", "OP", "ELI", "LMAO", "LOL", "WTF", "OMG",
    "FAQ", "ASAP", "USA", "LLC",
}

# Throttle: arctic-shift is generous but not unlimited.
REQ_DELAY = 0.45     # seconds between API calls
MAX_RETRIES = 4
PAGE_LIMIT = 100     # max records per page on arctic-shift

# FinBERT lazy import — only when needed (so --dry-run works without torch).
_FINBERT = None

# ─────────────────────────────────────────────────────────────────────
# arctic-shift fetcher
# ─────────────────────────────────────────────────────────────────────

@dataclass
class FetchStats:
    posts: int = 0
    comments: int = 0
    api_calls: int = 0
    failures: int = 0
    retries: int = 0


def _http_get(url: str, params: dict, stats: FetchStats) -> dict | None:
    for attempt in range(MAX_RETRIES):
        try:
            stats.api_calls += 1
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 429:
                stats.retries += 1
                time.sleep(2 ** attempt)
                continue
            if not r.ok:
                stats.retries += 1
                time.sleep(0.5 + attempt)
                continue
            return r.json()
        except (requests.RequestException, json.JSONDecodeError):
            stats.retries += 1
            time.sleep(0.5 + attempt)
    stats.failures += 1
    return None


def _date_chunks(start: datetime, end: datetime) -> Iterable[tuple[datetime, datetime]]:
    """Yield (day_start, day_end) UTC chunks so a single failure caps to 1 day."""
    cur = start
    while cur < end:
        nxt = cur + timedelta(days=1)
        yield cur, min(nxt, end)
        cur = nxt


def fetch_arctic_page(kind: str, after: int, before: int, stats: FetchStats) -> list[dict]:
    """One page (up to PAGE_LIMIT records) for a date window."""
    assert kind in ("posts", "comments")
    fields = "id,title,selftext,created_utc" if kind == "posts" else "id,body,created_utc"
    params = {
        "subreddit": SUB,
        "after": after,
        "before": before,
        "limit": PAGE_LIMIT,
        "fields": fields,
        "sort": "asc",
    }
    j = _http_get(f"{ARCTIC_BASE}/{kind}/search", params, stats)
    if not j or "data" not in j:
        return []
    return j["data"]


def fetch_day(day_start: datetime, day_end: datetime, stats: FetchStats) -> tuple[list[dict], list[dict]]:
    """Fetch all posts + comments in [day_start, day_end). Returns ([posts], [comments])."""
    posts, comments = [], []
    for kind, bucket in (("posts", posts), ("comments", comments)):
        cursor = int(day_start.timestamp())
        end_ts = int(day_end.timestamp())
        last_seen = -1
        while cursor < end_ts:
            time.sleep(REQ_DELAY)
            page = fetch_arctic_page(kind, cursor, end_ts, stats)
            if not page:
                break
            bucket.extend(page)
            new_cursor = max(int(p["created_utc"]) for p in page) + 1
            if new_cursor <= last_seen:
                break  # protect against pagination loops
            last_seen = new_cursor
            cursor = new_cursor
            if len(page) < PAGE_LIMIT:
                break  # last page in window
    stats.posts += len(posts)
    stats.comments += len(comments)
    return posts, comments


# ─────────────────────────────────────────────────────────────────────
# Ticker tagging
# ─────────────────────────────────────────────────────────────────────

def build_ticker_pattern(symbols: list[str]) -> re.Pattern:
    # Match $TICKER or bare TICKER as a whole word. Symbols are upper-only.
    escaped = [re.escape(s) for s in symbols]
    return re.compile(r"(?<![A-Z0-9])\$?(" + "|".join(escaped) + r")(?![A-Z0-9])")


def tag_text(text: str, pattern: re.Pattern) -> set[str]:
    if not text:
        return set()
    matches = pattern.findall(text)
    return {m for m in matches if m not in TICKER_BLOCKLIST}


# ─────────────────────────────────────────────────────────────────────
# FinBERT scoring (lazy load)
# ─────────────────────────────────────────────────────────────────────

def _ensure_finbert():
    global _FINBERT
    if _FINBERT is not None:
        return _FINBERT
    print("  loading FinBERT (first run downloads ~440MB)...", file=sys.stderr)
    from transformers import AutoTokenizer, AutoModelForSequenceClassification
    import torch
    tok = AutoTokenizer.from_pretrained("ProsusAI/finbert")
    mdl = AutoModelForSequenceClassification.from_pretrained("ProsusAI/finbert")
    mdl.eval()
    # FinBERT label order is [positive, negative, neutral] per the model card.
    _FINBERT = ("torch", torch, tok, mdl, ["positive", "negative", "neutral"])
    return _FINBERT


def score_batch(texts: list[str]) -> list[dict]:
    """Returns [{positive, negative, neutral, label, score_signed}, ...]"""
    if not texts:
        return []
    _, torch, tok, mdl, labels = _ensure_finbert()
    out = []
    BATCH = 16
    for i in range(0, len(texts), BATCH):
        chunk = texts[i:i + BATCH]
        # FinBERT input cap is 512 tokens; we hard-trim to 320 chars to save time.
        chunk = [(t or "")[:1200] for t in chunk]
        with torch.no_grad():
            enc = tok(chunk, return_tensors="pt", truncation=True, max_length=256, padding=True)
            logits = mdl(**enc).logits
            probs = torch.softmax(logits, dim=-1).cpu().numpy()
        for p in probs:
            d = {labels[k]: float(p[k]) for k in range(len(labels))}
            d["label"] = labels[int(p.argmax())]
            # Signed score: +pos − neg, ignore neutral mass. Range −1..+1.
            d["score_signed"] = d["positive"] - d["negative"]
            out.append(d)
    return out


# ─────────────────────────────────────────────────────────────────────
# Pipeline
# ─────────────────────────────────────────────────────────────────────

@dataclass
class DayBucket:
    date: str
    by_ticker: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))


def collect(symbols: list[str], start: datetime, end: datetime, dry_run: bool = False) -> dict:
    pattern = build_ticker_pattern(symbols)
    stats = FetchStats()

    # day_iso -> ticker -> list[text]
    day_buckets: dict[str, DayBucket] = {}

    print(f"Collecting WSB {start.date()} → {end.date()} for {len(symbols)} symbols", file=sys.stderr)
    for ds, de in _date_chunks(start, end):
        day_iso = ds.date().isoformat()
        posts, comments = fetch_day(ds, de, stats)

        bucket = day_buckets.setdefault(day_iso, DayBucket(date=day_iso))
        # Submissions: title + selftext together
        for p in posts:
            text = f"{p.get('title','')}\n{p.get('selftext','') or ''}".strip()
            tags = tag_text(text, pattern)
            for tk in tags:
                bucket.by_ticker[tk].append(text[:1200])
        # Comments
        for c in comments:
            text = (c.get("body") or "").strip()
            tags = tag_text(text, pattern)
            for tk in tags:
                bucket.by_ticker[tk].append(text[:1200])

        n_tags = sum(len(v) for v in bucket.by_ticker.values())
        print(f"  {day_iso}: {len(posts)} posts, {len(comments)} comments → {n_tags} ticker tags",
              file=sys.stderr)

    # Score
    print(f"\nFetched: {stats.posts} posts, {stats.comments} comments, "
          f"{stats.api_calls} API calls, {stats.failures} failures", file=sys.stderr)

    if dry_run:
        return {"stats": stats, "day_buckets": day_buckets, "rows": []}

    rows = []
    total_to_score = sum(len(v) for b in day_buckets.values() for v in b.by_ticker.values())
    print(f"Scoring {total_to_score} tagged texts with FinBERT...", file=sys.stderr)
    scored = 0
    for day_iso, bucket in sorted(day_buckets.items()):
        for ticker, texts in bucket.by_ticker.items():
            if not texts:
                continue
            scores = score_batch(texts)
            scored += len(texts)
            pos = sum(1 for s in scores if s["label"] == "positive")
            neg = sum(1 for s in scores if s["label"] == "negative")
            neu = sum(1 for s in scores if s["label"] == "neutral")
            mean_signed = sum(s["score_signed"] for s in scores) / len(scores)
            rows.append({
                "date": day_iso,
                "ticker": ticker,
                "mentions": len(texts),
                "positive": pos,
                "negative": neg,
                "neutral": neu,
                "score_signed_mean": round(mean_signed, 4),
                "bullish_ratio": round(pos / (pos + neg), 4) if (pos + neg) > 0 else None,
            })
            if scored % 200 == 0:
                print(f"  scored {scored}/{total_to_score}", file=sys.stderr)
    return {"stats": stats, "day_buckets": day_buckets, "rows": rows}


def write_outputs(result: dict, out_dir: Path, start: datetime, end: datetime):
    rows = result["rows"]
    if not rows:
        print("No rows to write.", file=sys.stderr)
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{start.date()}_{end.date()}"
    combined = out_dir / f"sentiment_combined_{tag}.csv"
    with open(combined, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows → {combined}", file=sys.stderr)

    # Per-ticker pivots
    by_ticker = defaultdict(list)
    for r in rows:
        by_ticker[r["ticker"]].append(r)
    for ticker, tr in by_ticker.items():
        per = out_dir / f"sentiment_{ticker}_{tag}.csv"
        with open(per, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(tr[0].keys()))
            w.writeheader()
            w.writerows(sorted(tr, key=lambda r: r["date"]))


def parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="+", default=None,
                   help="Override symbol list. Default uses DEFAULT_SYMBOLS.")
    p.add_argument("--all", action="store_true", help="Use the full DEFAULT_SYMBOLS universe.")
    p.add_argument("--start", required=True, type=parse_date)
    p.add_argument("--end", required=True, type=parse_date)
    p.add_argument("--out", default="sentiment_data", help="Output directory.")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch + tag only, skip FinBERT (useful to size the workload).")
    args = p.parse_args()

    syms = DEFAULT_SYMBOLS if args.all or not args.symbols else [s.upper() for s in args.symbols]
    if args.end <= args.start:
        sys.exit("--end must be after --start")

    result = collect(syms, args.start, args.end, dry_run=args.dry_run)
    write_outputs(result, Path(args.out), args.start, args.end)


if __name__ == "__main__":
    main()
