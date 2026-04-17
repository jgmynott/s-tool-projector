# s-tool Projector

A nightly-retrained equity ranker that publishes ~40 US picks per day ranked by a machine-learning model trained on walk-forward realized returns. Every pick is logged to a public ledger, every backtest number is filtered for liquidity and transaction costs, and the whole thing ships from GitHub Actions → Cloudflare + Railway without manual intervention.

**Live:** https://s-tool.io  ·  **Picks:** https://s-tool.io/picks  ·  **Track record:** https://s-tool.io/track-record

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
│    Static HTML from cloudflare/public/, Clerk auth, Stripe CTAs     │
│                                                                     │
│  Railway FastAPI (api)                                              │
│    - /api/picks           ranked portfolio (Strategist-gated)       │
│    - /api/track-record    live scoreboard from picks_history        │
│    - /api/backtest-report honest_metrics from overnight_backtest    │
│    - /api/honest-audit    Wave 1 (liquidity + costs) + Wave 2       │
│    - /api/me              user tier resolution + self-heal          │
│                                                                     │
│  Users DB on Railway Volume (/data/users.db)                        │
│    Persists across redeploys. Self-heal re-links Stripe by email    │
│    on first /api/me after a deploy so paying users never lose tier. │
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

1. **Public ledger.** Every pick ever published lands in `picks_history`. The live scoreboard on `/track-record` joins that ledger against today's prices with no retroactive adjustment. You can verify every realized return yourself.
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

## Product surfaces

- **`/`** — landing page, leads with the honest 8.7× claim + comparison visual
- **`/picks`** — Strategist-gated full ranked list + allocation donut + per-tier mini pies
- **`/track-record`** — live scoreboard + backtest validation + honest-metrics block
- **`/app`** — free projector (3 runs/day, Pro = 10/day, Strategist = unlimited)
- **`/pricing`** — Free · Pro $8/mo · Strategist $29/mo
- **`/how`** — methodology + what signals have been retired

## Tiers

| Tier | Price | Projections/day | Ranked picks | Track record |
|---|---|---|---|---|
| Free | — | 3 | ❌ | Summary only |
| Pro | $8/mo | 10 | ❌ | Summary only |
| Strategist | $29/mo | unlimited | ✅ full list + asymmetric | ✅ pick-by-pick |

## Disclaimer

This is a research/educational tool. Backtest performance does not guarantee future returns. The model is systematic — it does not account for idiosyncratic news, litigation, or event risk. Size-appropriate positions only.
