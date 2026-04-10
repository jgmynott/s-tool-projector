# S-Tool Projector Backtest Report

**Total forecasts:** 5,040  
**Symbols:** 15  
**Date range:** 2018-04-10 → 2025-03-17


## Calibration & Accuracy by Horizon

| Horizon | N | 80% Coverage | 50% Coverage | Median Bias % | Median MAE % | Directional Accuracy |
|---|---|---|---|---|---|---|
| 1mo | 1260 | 83.7% (target 80%) | 53.6% (target 50%) | -0.38% | 6.12% | 53.1% |
| 3mo | 1260 | 82.0% (target 80%) | 51.9% (target 50%) | -1.29% | 11.15% | 51.4% |
| 6mo | 1260 | 81.4% (target 80%) | 52.9% (target 50%) | -2.58% | 15.76% | 55.3% |
| 1yr | 1260 | 78.0% (target 80%) | 50.2% (target 50%) | -5.30% | 23.78% | 57.7% |

## Skill Score vs Naive (price-stays-flat) Baseline

Skill = 1 − (model MAE / naive MAE).  Positive = model beats naive.

| Horizon | Model MAE % | Naive MAE % | Skill Score |
|---|---|---|---|
| 1mo | 6.12% | 6.10% | -0.003 |
| 3mo | 11.15% | 10.88% | -0.025 |
| 6mo | 15.76% | 15.12% | -0.042 |
| 1yr | 23.78% | 22.10% | -0.076 |

## Per-Symbol Summary (1-year horizon)

| Symbol | N | 80% Cov | Bias % | MAE % | Dir Acc |
|---|---|---|---|---|---|
| AAPL | 84 | 82.1% | -10.21% | 18.65% | 67.9% |
| AMZN | 84 | 70.2% | +0.34% | 30.29% | 57.1% |
| GOOGL | 84 | 69.0% | -7.27% | 29.70% | 50.0% |
| IWM | 84 | 82.1% | -1.54% | 19.59% | 42.9% |
| JNJ | 84 | 92.9% | -5.31% | 9.66% | 53.6% |
| JPM | 84 | 72.6% | -5.46% | 26.51% | 42.9% |
| MSFT | 84 | 82.1% | -8.04% | 21.57% | 72.6% |
| NVDA | 84 | 60.7% | -7.68% | 52.66% | 54.8% |
| PG | 84 | 88.1% | -4.78% | 11.99% | 61.9% |
| QQQ | 84 | 70.2% | -7.08% | 20.48% | 69.0% |
| SPY | 84 | 79.8% | -5.85% | 15.12% | 64.3% |
| TSLA | 84 | 70.2% | -9.83% | 45.47% | 58.3% |
| XLE | 84 | 73.8% | +0.06% | 24.77% | 54.8% |
| XLF | 84 | 79.8% | -3.50% | 19.95% | 44.0% |
| XLV | 84 | 96.4% | -3.38% | 10.33% | 71.4% |