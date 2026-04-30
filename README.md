# s-tool Projector

A nightly-retrained equity ranker that publishes ~40 US picks per day ranked by a machine-learning model trained on walk-forward realized returns. Every pick is logged to a public ledger, every backtest number is filtered for liquidity and transaction costs, and the whole thing ships from GitHub Actions → Cloudflare + Railway without manual intervention.

**Live:** https://s-tool.io/app  ·  **Picks:** https://s-tool.io/picks

> **Site cull (2026-04-30):** the public site collapsed to two desktop-only pages — `/app` (projector) and `/picks` (ranked portfolio). Clerk auth, Stripe billing, every marketing page, and all mobile responsive work were removed. Every dropped route 302-redirects to `/app/`. Rationale lives in the `cull(site): collapse to /app + /picks` commit message and `feedback_nav_consistency.md`. Backend `api.py` still defines the auth + billing endpoints — they're orphans until the next cleanup pass.

---

## The numbers (honest — after liquidity + 1.5% round-trip costs)

| | Overall (22k obs, 2022-2024) | 2024 out-of-sample |
|---|---|---|
| Hit rate at +100% in 1 year | **37%** | **62%** |
| Lift vs random-picking baseline | **8.7×** | **12.8×** |
| Mean 1-year return on top-20 | **+145%** | **+331%** |

Baseline = any tradeable Russell 3000 ticker (1 in 20 doubles per year). Top-20 = our model's highest-ranked names. Filters enforced: $500k average daily dollar-volume minimum, 1.5% round-trip transaction cost haircut, survivorship mid-window check. See [docs/honest-metrics.md](docs/honest-metrics.md) for the full Wave 1 audit methodology.

## Architecture at a glance

```
┌─────────────────────────────────────────────────────────────────────┐
│  Nightly pipeline (GitHub Actions, weekdays)                        │
│                                                                     │
│  20:00 UTC   nightly-pipeline-fast    ┐                             │
│    - Smoke test providers             │                             │
│    - Refresh projections (preferred)  │  45 min cap                 │
│    - Enrichment (marketcap, FMP, SEC) │                             │
│    - Portfolio scan + picks.json      │                             │
│    - Deploy Cloudflare + Railway      ┘                             │
│                                                                     │
│  23:00 UTC   nightly-pipeline-slow    ┐                             │
│    - Short interest refresh (yf)      │                             │
│    - Finnhub earnings + metrics       │                             │
│    - Upside_hunt research sweep       │  150 min cap                │
│    - NN training (ExtraTrees + MLP)   │                             │
│    - Feature ablation + backtest      │                             │
│    - Fresh NN re-score → re-deploy    ┘                             │
└─────────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Production                                                         │
│                                                                     │
│  Cloudflare Worker (s-tool-site)                                    │
│    Serves /app and /picks from cloudflare/public/.                  │
│    302-redirects every dropped marketing route → /app/.             │
│    Reverse-proxies /api/* to Railway. No auth, no billing.          │
│                                                                     │
│  Railway FastAPI (api)                                              │
│    - /api/picks           ranked portfolio                          │
│    - /api/track-record    realized-return scoreboard                │
│    - /api/portfolio       live Alpaca account snapshot              │
│    - /api/project         single-ticker projection                  │
│    - /api/health          liveness                                  │
│    Auth/billing routes (/api/me, /api/billing/*) still exist as     │
│    orphans — no current frontend calls them.                        │
│                                                                     │
│  Projector cache (projector_cache.db)                               │
│    SQLite — projections + fundamentals + picks_history ledger       │
└─────────────────────────────────────────────────────────────────────┘
```

See [docs/deployment.md](docs/deployment.md) for secrets + rollback and [docs/data-sources.md](docs/data-sources.md) for every data feed wired into the model.

## Data sources feeding the model

| Source | What | Cadence | Status |
|---|---|---|---|
| yfinance (OHLCV) | ~2,272 tickers, **2015-04 → present** | nightly | ✅ live |
| SEC EDGAR XBRL | 1,947 companies, 98k quarterly rows | nightly | ✅ live |
| FMP (premium) | Analyst consensus, EPS estimates | nightly | ✅ live |
| FRED macro | VIX, yields, inflation, commodities | nightly | ✅ live |
| FinBERT sentiment | Historical WSB + StockTwits | static | ✅ live (retired post-2023) |
| **yfinance short interest** | shortPctFloat, shortRatio, short delta | nightly | ✅ live |
| **yfinance ratios** | 38 fundamental/technical ratios | nightly | ✅ live |
| **Finnhub earnings** | Next earnings date, surprise, analyst trend | nightly | ✅ live |
| **Finnhub metrics** | 27 multi-year averages (ROIC, 5y margins) | nightly | ✅ live |
| Polygon options flow | Put/call, IV percentile, call volume | — | queued |
| SEC Form 4 | Insider transactions (buying vs selling) | — | queued |
| 13F institutional | Position deltas (smart-money signal) | — | queued |

All live sources are free or already-paid. The queued ones have keys configured and are scoped for future slow-path steps.

## Why this is different from every other "stock picker"

1. **Public ledger.** Every pick ever published lands in `picks_history`. The track-record API joins that ledger against today's prices with no retroactive adjustment. You can verify every realized return yourself via `/api/track-record`.
2. **Honest backtest.** After publishing a pre-liquidity hit rate of 55% / 11× lift, we applied a diligence-grade review (survivorship, $500k ADV floor, 1.5% round-trip costs) and ship the 37% / 8.7× number even though it's less flattering. The pre-filter numbers inflated returns by recommending untradeable small-caps.
3. **Nightly retraining.** The model rebuilds itself every night on newly realized returns. Not a static rulebook decaying in a drawer. Signal degradation (e.g. crowd-sentiment dying around 2023) gets detected and retired.
4. **Size-neutral + regime-checked.** Within-quintile lift holds across every market-cap bucket (8.45× median). 2024 out-of-sample performance strengthens on unseen data. Training set now spans 2016-2024, covering the 2018 Q4 drawdown, 2020 COVID crash, 2022 tech bear, 2023 chop, and 2024 bull.

## Developer quick start

```bash
# Install (Python 3.11)
pip install -r requirements.txt

# Run the API locally (reads local projector_cache.db)
uvicorn api:app --reload --port 8000

# Refresh the preferred-universe projections
python3 worker.py --preferred

# Pull new short-interest snapshots
python3 -m signals_short_interest_yf --universe preferred

# Run the honest-metrics audit on the latest upside_hunt output
python3 research/wave1_honest_audit_2026_04_17.py
```

The dashboard at `cloudflare/public/app/index.html` runs standalone in a browser against the local API — no build step.

## Public surfaces

- **`/app`** — single-ticker projector. Open access, no quota. `?ticker=XYZ` deep-link supported (used by `/picks` to launch a projection on click).
- **`/picks`** — full ranked portfolio with allocation donut, per-tier mini pies, and live Alpaca portfolio panel.

That's it. Mobile is intentionally not supported — both pages set `viewport=width=1024` and skip every responsive rule.

## Disclaimer

This is a research/educational tool. Backtest performance does not guarantee future returns. The model is systematic — it does not account for idiosyncratic news, litigation, or event risk. Size-appropriate positions only.
