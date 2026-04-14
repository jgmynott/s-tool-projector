# Phase C: Sentiment-Tilted Drift Backtest Report

**Total rows:** 27,800  
**Symbols:** 103 (AAPL, ABBV, AMC, AMD, AMZN, ARKK, AVGO, BA, BAC, BB, BNTX, C, CAT, CLNE, CLOV, COIN, COST, CRM, CRWD, CVX, DDOG, DIA, DIS, FSLR, GE, GME, GOOG, GOOGL, GS, HOOD, INTC, IWM, JNJ, JPM, KO, LCID, LI, LLY, LMT, LYFT, MA, MCD, META, MRNA, MRVL, MS, MSFT, MU, MVIS, NET, NFLX, NIO, NKE, NVDA, OXY, PANW, PFE, PG, PINS, PLTR, PYPL, QCOM, QQQ, RBLX, RIVN, ROKU, RTX, SBUX, SCHW, SHOP, SLB, SMCI, SNAP, SNOW, SOFI, SOXL, SOXS, SPCE, SPXS, SPY, SQQQ, TGT, TQQQ, TSLA, TSM, UBER, UNH, UVXY, V, VOO, VTI, WFC, WMT, XLE, XLF, XLI, XLK, XLP, XLRE, XLV, XOM, XPEV, ZS)  
**Date range:** 2018-04-13 to 2025-04-08  
**Models tested:** 10


## 1MO Horizon

| Model | Tilt | Lookback | N | MAE ($) | RMSE ($) | Hit Rate | IC |
|---|---|---|---|---|---|---|---|
| baseline | 0.000 | 0d | 7637 | 821.68 | 16532.74 | 52.0% | -0.007 |
| tilt_L10_S0.005 | 0.005 | 10d | 2237 | 12.54 (-98.5%) | 51.56 | 52.8% | 0.064 |
| tilt_L10_S0.01 | 0.010 | 10d | 2237 | 12.54 (-98.5%) | 51.56 | 52.8% | 0.064 |
| tilt_L10_S0.02 | 0.020 | 10d | 2237 | 12.54 (-98.5%) | 51.56 | 52.7% | 0.064 |
| tilt_L21_S0.005 | 0.005 | 21d | 2393 | 12.31 (-98.5%) | 50.09 | 52.8% | 0.057 |
| tilt_L21_S0.01 | 0.010 | 21d | 2393 | 12.32 (-98.5%) | 50.09 | 52.8% | 0.057 |
| tilt_L21_S0.02 | 0.020 | 21d | 2393 | 12.32 (-98.5%) | 50.09 | 52.7% | 0.057 |
| tilt_L5_S0.005 | 0.005 | 5d | 2091 | 12.86 (-98.4%) | 53.18 | 53.0% | 0.072 |
| tilt_L5_S0.01 | 0.010 | 5d | 2091 | 12.86 (-98.4%) | 53.17 | 53.0% | 0.072 |
| tilt_L5_S0.02 | 0.020 | 5d | 2091 | 12.86 (-98.4%) | 53.17 | 53.0% | 0.072 |

## 3MO Horizon

| Model | Tilt | Lookback | N | MAE ($) | RMSE ($) | Hit Rate | IC |
|---|---|---|---|---|---|---|---|
| baseline | 0.000 | 0d | 7637 | 1438.08 | 27759.00 | 51.5% | 0.022 |
| tilt_L10_S0.005 | 0.005 | 10d | 2237 | 22.01 (-98.5%) | 66.39 | 55.1% | 0.145 |
| tilt_L10_S0.01 | 0.010 | 10d | 2237 | 22.01 (-98.5%) | 66.38 | 55.0% | 0.145 |
| tilt_L10_S0.02 | 0.020 | 10d | 2237 | 22.01 (-98.5%) | 66.38 | 55.0% | 0.145 |
| tilt_L21_S0.005 | 0.005 | 21d | 2393 | 22.06 (-98.5%) | 65.04 | 54.7% | 0.127 |
| tilt_L21_S0.01 | 0.010 | 21d | 2393 | 22.06 (-98.5%) | 65.04 | 54.7% | 0.127 |
| tilt_L21_S0.02 | 0.020 | 21d | 2393 | 22.06 (-98.5%) | 65.03 | 54.7% | 0.127 |
| tilt_L5_S0.005 | 0.005 | 5d | 2091 | 22.37 (-98.4%) | 68.14 | 55.1% | 0.160 |
| tilt_L5_S0.01 | 0.010 | 5d | 2091 | 22.37 (-98.4%) | 68.14 | 55.1% | 0.160 |
| tilt_L5_S0.02 | 0.020 | 5d | 2091 | 22.37 (-98.4%) | 68.13 | 55.1% | 0.160 |

## 6MO Horizon

| Model | Tilt | Lookback | N | MAE ($) | RMSE ($) | Hit Rate | IC |
|---|---|---|---|---|---|---|---|
| baseline | 0.000 | 0d | 7637 | 2064.19 | 37394.52 | 53.4% | 0.027 |
| tilt_L10_S0.005 | 0.005 | 10d | 2237 | 30.26 (-98.5%) | 86.08 | 56.8% | 0.186 |
| tilt_L10_S0.01 | 0.010 | 10d | 2237 | 30.26 (-98.5%) | 86.08 | 56.8% | 0.186 |
| tilt_L10_S0.02 | 0.020 | 10d | 2237 | 30.26 (-98.5%) | 86.06 | 56.9% | 0.186 |
| tilt_L21_S0.005 | 0.005 | 21d | 2393 | 29.68 (-98.6%) | 83.82 | 57.0% | 0.186 |
| tilt_L21_S0.01 | 0.010 | 21d | 2393 | 29.68 (-98.6%) | 83.82 | 57.0% | 0.186 |
| tilt_L21_S0.02 | 0.020 | 21d | 2393 | 29.68 (-98.6%) | 83.81 | 57.0% | 0.186 |
| tilt_L5_S0.005 | 0.005 | 5d | 2091 | 30.80 (-98.5%) | 88.26 | 57.2% | 0.196 |
| tilt_L5_S0.01 | 0.010 | 5d | 2091 | 30.80 (-98.5%) | 88.26 | 57.1% | 0.196 |
| tilt_L5_S0.02 | 0.020 | 5d | 2091 | 30.80 (-98.5%) | 88.25 | 57.1% | 0.196 |

## 1YR Horizon

| Model | Tilt | Lookback | N | MAE ($) | RMSE ($) | Hit Rate | IC |
|---|---|---|---|---|---|---|---|
| baseline | 0.000 | 0d | 7637 | 2312.52 | 43818.47 | 56.0% | -0.025 |
| tilt_L10_S0.005 | 0.005 | 10d | 2237 | 45.97 (-98.0%) | 107.23 | 60.7% | 0.248 |
| tilt_L10_S0.01 | 0.010 | 10d | 2237 | 45.98 (-98.0%) | 107.23 | 60.8% | 0.248 |
| tilt_L10_S0.02 | 0.020 | 10d | 2237 | 45.98 (-98.0%) | 107.22 | 60.7% | 0.248 |
| tilt_L21_S0.005 | 0.005 | 21d | 2393 | 45.03 (-98.1%) | 104.57 | 61.2% | 0.248 |
| tilt_L21_S0.01 | 0.010 | 21d | 2393 | 45.04 (-98.1%) | 104.57 | 61.2% | 0.248 |
| tilt_L21_S0.02 | 0.020 | 21d | 2393 | 45.04 (-98.1%) | 104.56 | 61.2% | 0.248 |
| tilt_L5_S0.005 | 0.005 | 5d | 2091 | 46.86 (-98.0%) | 109.60 | 61.1% | 0.257 |
| tilt_L5_S0.01 | 0.010 | 5d | 2091 | 46.86 (-98.0%) | 109.60 | 61.1% | 0.257 |
| tilt_L5_S0.02 | 0.020 | 5d | 2091 | 46.87 (-98.0%) | 109.59 | 61.0% | 0.257 |

## Best Sentiment Configuration

**1MO:**
- Lowest MAE: tilt_L21_S0.005 -- MAE $12.31 (-98.5% vs baseline)
- Best Hit Rate: tilt_L5_S0.005 -- 53.0% (+1.0% vs baseline 52.0%)
- Best IC: tilt_L5_S0.01 -- 0.072 (+0.079 vs baseline -0.007)

**3MO:**
- Lowest MAE: tilt_L10_S0.005 -- MAE $22.01 (-98.5% vs baseline)
- Best Hit Rate: tilt_L5_S0.005 -- 55.1% (+3.6% vs baseline 51.5%)
- Best IC: tilt_L5_S0.02 -- 0.160 (+0.138 vs baseline 0.022)

**6MO:**
- Lowest MAE: tilt_L21_S0.005 -- MAE $29.68 (-98.6% vs baseline)
- Best Hit Rate: tilt_L5_S0.005 -- 57.2% (+3.8% vs baseline 53.4%)
- Best IC: tilt_L5_S0.01 -- 0.196 (+0.169 vs baseline 0.027)

**1YR:**
- Lowest MAE: tilt_L21_S0.005 -- MAE $45.03 (-98.1% vs baseline)
- Best Hit Rate: tilt_L21_S0.005 -- 61.2% (+5.1% vs baseline 56.0%)
- Best IC: tilt_L5_S0.02 -- 0.257 (+0.282 vs baseline -0.025)


## Verdict

Sentiment tilt **improves** 1-year MAE by 98.1% (best config: tilt_L21_S0.005).