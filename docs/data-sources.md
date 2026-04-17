# Data sources — what's feeding the model

Last refreshed: **2026-04-17**

Every signal the nightly pipeline ingests, where it comes from, how often it refreshes, and the table it writes to.

## Live feeds (active in production)

### Prices & volumes
- **Source**: yfinance (Yahoo Finance proxy)
- **Coverage**: 2,272 training-set tickers, **2015-04 → present** daily OHLCV
- **Refresh**: incremental per-ticker during nightly pipeline; historical backfill via `backfill_prices_historical.py`
- **Storage**: `data_cache/prices/<SYM>.csv` per ticker
- **Used by**: projector engine, upside_hunt walk-forward ground truth, liquidity filter, 52-week-high derived features

### SEC EDGAR fundamentals
- **Source**: SEC XBRL company facts API
- **Coverage**: 1,947 companies, 98,071 quarterly rows
- **Refresh**: nightly (both paths call `signals_sec_edgar.refresh_universe`)
- **Storage**: `data_cache/sec_edgar/facts/CIK<id>.json` raw + `sec_fundamentals` SQLite table
- **Signals derived**: revenue YoY growth, gross margin, operating margin, FCF/revenue, buyback intensity, net-debt change
- **Used by**: `/picks` rationale strings, potential NN features

### FMP (Financial Modeling Prep, premium)
- **Source**: financialmodelingprep.com premium endpoints
- **Coverage**: 250 new profile fetches per day (rate-limited), grows cache over time
- **Refresh**: nightly
- **Storage**: `data_cache/profiles/<SYM>.json`, `data_cache/market_caps.json`
- **Signals**: analyst target mean, EPS growth estimates, sector/industry, market cap
- **Used by**: projection drift tilt, liquidity size labels

### FRED macro
- **Source**: Federal Reserve Economic Data
- **Coverage**: VIX, 2Y/5Y/10Y/30Y Treasury yields, inflation, commodities (gold/silver/oil/copper/platinum/palladium)
- **Refresh**: on-demand inside `/api/scan/latest`
- **Used by**: regime detection, sigma scaling (VIX)

### yfinance short interest (new 2026-04-17)
- **Source**: `Ticker.info` shortPctFloat, shortRatio, sharesShort, sharesShortPriorMonth
- **Coverage**: preferred universe (~494 of 537 return usable data)
- **Refresh**: nightly slow path
- **Storage**: `short_interest_yf` table
- **Features derived**: `si_pct_float`, `si_dtc` (days to cover), `si_chg_pct` (month-over-month delta)
- **Ablation**: `research/nightly_short_interest_ablation.py` runs after NN training, writes dated JSONs to `runtime_data/short_interest_ablation_<date>.json`

### yfinance fundamental ratios (new 2026-04-17)
- **Source**: `Ticker.info` — 38 valuation/profitability/balance-sheet/growth/ownership/technical/analyst fields
- **Coverage**: preferred universe
- **Refresh**: nightly slow path
- **Storage**: `ratios_yf` table
- **Signals**: P/E (trailing + forward), PEG, P/B, P/S, EV/EBITDA, EV/Revenue, ROE, ROA, all margins, debt/equity, current ratio, quick ratio, insider/institutional ownership %, beta, 50d/200d averages, 52w high/low, dist-from-52w-high, dividend yield, payout ratio, analyst target mean/high/low, recommendation mean, N analysts, market cap, enterprise value

### Finnhub earnings + analyst recommendations (new 2026-04-17)
- **Source**: finnhub.io `/calendar/earnings`, `/stock/earnings`, `/stock/recommendation`
- **Coverage**: preferred universe; ~10 min per nightly refresh at 1.1s pacing (60 req/min free tier)
- **Refresh**: nightly slow path
- **Storage**: `earnings_finnhub` table
- **Signals**: `days_to_next_earnings` (catalyst proximity), `last_surprise_pct` (beat magnitude), `rec_bullish_share`, `rec_bullish_delta_90d` (analyst sentiment trend)

### Finnhub /stock/metric — 27 additional ratios (new 2026-04-17)
- **Source**: finnhub.io `/stock/metric?metric=all`
- **Refresh**: nightly slow path
- **Storage**: `metrics_finnhub` table
- **Signals**: multi-year averaged margins, ROIC, revenue/EPS 3Y and 5Y CAGRs, price-return windows (4w/13w/26w/52w), asset turnover, inventory turnover, long-term debt/equity. Complements `ratios_yf` with time-averaged (less noisy) fundamentals.

### FinBERT historical sentiment (retired post-2023)
- **Source**: WSB via arctic-shift + StockTwits + FinBERT scoring
- **Coverage**: 107 symbols, 731 days (2022-2024 historical)
- **Refresh**: static (backtest artifact)
- **Status**: **RETIRED** — research confirmed all crowd-sentiment signals died around 2023 regime shift. Code preserved in `signals_sentiment*`, data in `sentiment_data/`. Not wired into current NN features.

## Queued feeds (API keys configured, not yet wired)

### Polygon options flow
- **Endpoint**: `/v3/snapshot/options/<symbol>`, `/v3/reference/options/contracts`
- **Rate limit**: 5 req/min free tier → ~100 min for preferred universe
- **Signals to build**: put/call ratio, IV percentile, 0-30d call volume (speculative activity), unusual options sweeps
- **Status**: scoped for next slow-path step; top-100 tickers by volume to fit rate limit

### SEC Form 4 insider transactions
- **Endpoint**: SEC bulk feed (free)
- **Signals to build**: insider buying/selling aggregate, cluster-buy flags, officer-level buys vs 10b5-1 automated sells
- **Research basis**: insider cluster-buys are a well-documented predictive signal, especially post-drawdown

### 13F institutional ownership
- **Endpoint**: SEC quarterly filings (free, bulk)
- **Signals to build**: concentration deltas, new-position flags, smart-money rotation (comparing top-quintile-performing funds' position changes)

## Historical backfill program

The original training set covered 2022-05 → 2024-08 because `data_cache/prices/` only ran back to 2021-04. On 2026-04-17 we kicked off `backfill_prices_historical.py` to merge yfinance OHLCV back to **2015-01-01** for every ticker in the training set. Once complete, `upside_hunt.py` regenerates with walk-forward windows 2016-Q1 through 2024-Q3 — **35 windows** covering:

| Year | Regime |
|---|---|
| 2016-2017 | Bull |
| 2018 Q4 | Drawdown |
| 2019 | Recovery |
| 2020 | COVID crash + V-rebound |
| 2021 | Full-year continuity |
| 2022 | Tech bear |
| 2023 | Chop |
| 2024 | Bull |

The chained `regenerate_training_set.py` waits for backfill coverage ≥ 85%, then runs `upside_hunt.py` → `overnight_learn.py` → `overnight_backtest.py` → `wave1_honest_audit.py` in sequence. Logs to `/tmp/regen_*.log`.

## What the NN trains on today

Feature set in `overnight_learn.py` (as of 2026-04-17):

```python
FEATURE_COLS = [
    "current", "p10", "p90", "sigma",
    "H1_naive_p90", "H7_ewma_p90",
]
# Plus derived in _build_features:
#   log_price, p90_ratio, p10_ratio, asymmetry, vol_low, vol_hi
```

Total: **8 features** for the ExtraTreesRegressor. Research completed 2026-04-17 showed lean subsets (2-4 features) did not transfer from MLP to ExtraTrees. The production model is saturated on this feature set at the +100% hit metric.

The path to improvement is NEW information (the feeds above), not new combinations of existing features. As each new feed accumulates 2-3 weeks of history, its ablation result is expected to move hit_100 by 1-3pp per genuine signal.

## Autonomous signal promotion (future)

Each night's ablation writes a dated JSON. After a signal accumulates ≥14 nights with a consistent positive delta (e.g. ≥+1pp over baseline in ≥10 of 14 nights), the workflow will promote it into `FEATURE_COLS` automatically. Not yet built — ablation JSONs currently require manual review.
