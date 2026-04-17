# NN research findings — 2026-04-16

Total runtime: **125s**

## 1. Architecture sweep (MLP hidden-layer sizes × alpha)

Walk-forward hit rate at +100% return, top-20 picks, recent-4 windows.

| rank | config | hit rate | mean return | elapsed |
|---|---|---|---|---|
| 1 | `mlp_(16, 8)_a0.01` | 0.675 | +506.5% | 5.1s |
| 2 | `mlp_(32, 16, 8)_a0.0001` | 0.662 | +529.1% | 3.8s |
| 3 | `mlp_(32, 16, 8)_a0.001` | 0.662 | +526.3% | 3.5s |
| 4 | `mlp_(64, 32)_a0.0001` | 0.662 | +521.7% | 2.9s |
| 5 | `mlp_(16, 8)_a0.0001` | 0.662 | +508.7% | 5.4s |
| 6 | `mlp_(16, 8)_a0.001` | 0.650 | +501.1% | 5.1s |
| 7 | `mlp_(64, 32)_a0.001` | 0.650 | +496.1% | 2.5s |
| 8 | `mlp_(32, 16, 8)_a0.01` | 0.650 | +491.8% | 3.6s |
| 9 | `mlp_(32, 16)_a0.0001` | 0.637 | +498.6% | 4.5s |
| 10 | `mlp_(64, 32)_a0.01` | 0.637 | +489.6% | 2.4s |
| 11 | `mlp_(32, 16)_a0.001` | 0.625 | +478.9% | 4.7s |
| 12 | `mlp_(32, 16)_a0.01` | 0.625 | +478.3% | 5.2s |

## 2. Model-class shootout

Same features + walk-forward for each model family.

| rank | model | hit rate | mean return | elapsed |
|---|---|---|---|---|
| 1 | `et` | 0.662 | +515.6% | 1.9s |
| 2 | `rf` | 0.625 | +497.1% | 7.7s |
| 3 | `mlp` | 0.625 | +478.9% | 4.6s |
| 4 | `gbr` | 0.600 | +416.5% | 27.2s |

## 3. Moonshot-classifier calibration

Bucket all recent-window picks into deciles by predicted +100% probability. A well-calibrated classifier has top decile >> bottom decile on realized hit rate.

Spearman(prob, realized_ret) = **-0.000**  ·  n = 9035

| decile | mean prob | realized +100% rate | realized +200% rate | mean return |
|---|---|---|---|---|
| decile_10 | 0.823 | 0.273 | 0.125 | +99.4% |
| decile_9 | 0.678 | 0.122 | 0.041 | +27.1% |
| decile_8 | 0.574 | 0.083 | 0.018 | +18.5% |
| decile_7 | 0.473 | 0.061 | 0.016 | +13.3% |
| decile_6 | 0.363 | 0.043 | 0.006 | +13.2% |
| decile_5 | 0.241 | 0.038 | 0.006 | +13.4% |
| decile_4 | 0.140 | 0.019 | 0.001 | +13.2% |
| decile_3 | 0.083 | 0.004 | 0.000 | +14.9% |
| decile_2 | 0.043 | 0.006 | 0.000 | +12.9% |
| decile_1 | 0.016 | 0.010 | 0.001 | +13.9% |

## 4. Threshold ladder

| threshold | NN hit rate | baseline | lift |
|---|---|---|---|
| +100% | 0.625 | 0.066 | 9.49x |
| +200% | 0.463 | 0.021 | 21.76x |
| +300% | 0.400 | 0.011 | 37.26x |
| +500% | 0.250 | 0.005 | 49.10x |

## 5. Per-window consistency

| window | hit_100 | hit_200 | mean | median | max | min |
|---|---|---|---|---|---|---|
| 2022-05-01 | 0.050 | 0.000 | -4.0% | -5.2% | +102.4% | -48.5% |
| 2022-08-01 | 0.600 | 0.350 | +149.8% | +112.8% | +476.6% | -91.1% |
| 2022-11-01 | 0.450 | 0.250 | +283.5% | +89.3% | +2325.3% | -60.6% |
| 2023-02-01 | 0.350 | 0.100 | +69.4% | +57.5% | +307.9% | -56.2% |
| 2023-05-01 | 0.450 | 0.350 | +107.3% | +59.4% | +291.3% | -58.7% |
| 2023-08-01 | 0.350 | 0.050 | +59.5% | +53.0% | +226.0% | -98.4% |
| 2023-11-01 | 0.650 | 0.400 | +533.7% | +150.2% | +4245.2% | +24.2% |
| 2024-02-01 | 0.700 | 0.600 | +415.0% | +321.4% | +1481.4% | -67.2% |
| 2024-05-01 | 0.550 | 0.450 | +426.6% | +181.8% | +2844.2% | -22.9% |
| 2024-08-01 | 0.600 | 0.400 | +540.3% | +121.5% | +2824.3% | -44.3% |

## 6. Minimal-feature experiment

Does stripping features to the absolute minimum hurt performance?

| feature set | n features | hit rate | mean return |
|---|---|---|---|
| full_8 | 8 | 0.625 | +478.9% |
| minimal_2_logprice_sigma | 2 | 0.650 | +438.0% |
| minimal_3_add_p90_ratio | 3 | 0.675 | +498.5% |
| hand_crafted_only_5 | 5 | 0.475 | +342.9% |

## Notes

- Hit rate = % of top-20 picks that reached the return threshold within 12 months of pick date.
- All walk-forward: for each window t, train only on windows < t, score window t.
- Recent-4 aggregation = windows with the most complete 12-month forward data + newest regime.
- Baseline = universe-wide rate at same threshold (no selection).
