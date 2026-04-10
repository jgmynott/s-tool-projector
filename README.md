# S-Tool Projector

Interactive stock price projection dashboard using Monte Carlo simulation (GBM) and Mean Reversion (Ornstein-Uhlenbeck). Single HTML file — no build step, no server required for the dashboard.

## Quick Start

### Dashboard (no install needed)

Open `projection_dashboard.html` in any modern browser. Enter a ticker (e.g. QQQ, TSLA, AAPL), set your time horizon and bet size, hit **Run Projection**.

Features:
- Candlestick OHLC chart (past year) + projection fan chart (10th–90th percentile bands)
- Live VIX regime scaling (fetched from Yahoo ^VIX)
- Bet sizing conviction panel — dollar outcomes at each percentile
- Retail sentiment panel (live from StockTwits)
- Inline price labels at 1M / 3M / 6M milestones
- Model diagnostics with plain-English interpretation

Data is fetched client-side from Yahoo Finance (via CORS proxy) and StockTwits. No API keys required.

### Python Tools (backtest + sentiment)

```bash
pip install -r requirements.txt
```

**Run the backtester** (walk-forward, 15 symbols, 10 years):
```bash
python projector_backtest.py
```
Outputs `backtest_results.csv` and `backtest_report.md`.

**Run the hyperparameter sweep** (8 configs, VIX on/off):
```bash
python projector_sweep.py
```
Outputs `sweep_results.csv` and `sweep_report.md`.

**Collect WSB sentiment** (requires `transformers` + `torch`):
```bash
# Dry run (fetch + tag only, skip FinBERT):
python sentiment_collector.py --symbols TSLA NVDA --start 2024-01-01 --end 2024-02-01 --dry-run

# Full run with FinBERT scoring:
python sentiment_collector.py --symbols TSLA NVDA --start 2024-01-01 --end 2024-02-01

# All 15 backtest symbols:
python sentiment_collector.py --all --start 2023-01-01 --end 2024-01-01
```
Outputs daily sentiment CSVs to `sentiment_data/`.

## How It Works

### Models

| Model | Method | What it captures |
|---|---|---|
| Monte Carlo | Geometric Brownian Motion | Drift + random walks from historical returns |
| Mean Reversion | Ornstein-Uhlenbeck + Trend | Pull toward trending moving average |

The dashboard generates N paths from each model, concatenates them into 2N total paths (default 30% MC / 70% MR blend), and extracts percentiles from the combined pool.

### VIX Regime Scaling

| VIX Level | Regime | Sigma Multiplier |
|---|---|---|
| >= 40 | Crisis | x1.50 |
| >= 25 | High | x1.20 |
| >= 15 | Normal | x1.00 |
| < 15 | Low | x0.85 |

### Backtest Results (5,040 forecasts, 15 symbols, 7 years)

| Horizon | 80% Coverage | Median MAE | Skill vs Naive |
|---|---|---|---|
| 1 month | 83.7% | 6.12% | -0.003 |
| 3 months | 82.0% | 11.15% | -0.025 |
| 6 months | 81.4% | 15.76% | -0.042 |
| 1 year | 78.0% | 23.78% | -0.076 |

The 80% bands are well-calibrated. The median forecast has no edge over "price stays flat" — this is expected for single-stock returns and is why we're adding sentiment data to move the drift.

## File Structure

```
projection_dashboard.html   Main dashboard — open in browser
projector_backtest.py        Walk-forward backtester
projector_sweep.py           Hyperparameter sweep
sentiment_collector.py       WSB/arctic-shift + FinBERT pipeline
sentiment_data/              FinBERT output CSVs
serve.py                     Optional local dev server
backtest_report.md           Latest backtest results
sweep_report.md              Latest sweep results
requirements.txt             Python dependencies
```

## Requirements

- **Dashboard**: Any modern browser (Chrome, Firefox, Safari, Edge)
- **Python tools**: Python 3.9+, packages in `requirements.txt`
- **Sentiment collector**: Additionally needs `transformers` and `torch` (~440MB download on first run for FinBERT model weights)
- **No API keys needed** — all data sources are free and keyless

## Disclaimer

This is a research/educational tool. The projection bands show the range of plausible outcomes from historical price dynamics — they are not investment advice. The median forecast is statistically indistinguishable from "price stays flat" at all horizons tested. Use the bands to understand uncertainty, not to set price targets.
