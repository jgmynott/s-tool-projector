# Deep research v2 findings — 2026-04-16

Total runtime: **52.4 min**

## Phase 1 — extended feature engineering

- Status: completed
- Rows processed: 22218
- Missing-feature rows: 0
- Elapsed: 9.2s
- New features added: mom_20d, mom_60d, mom_180d, high_52w_ratio, low_52w_ratio, realized_vol_60d, volume_z60d, beta_180d

## Phase 2 — base vs extended features

| variant | hit rate | mean return |
|---|---|---|
| `et_extended_16` | 0.688 | +561.6% |
| `et_base_8` | 0.662 | +515.6% |
| `mlp_base_8` | 0.625 | +478.9% |
| `mlp_extended_16` | 0.562 | +425.1% |

## Phase 3 — deep hyperparameter search

Evaluated 105 configs. Top 10:

| rank | config | hit rate | mean return |
|---|---|---|---|
| 1 | `et_300_14_20` | 0.713 | +579.6% |
| 2 | `et_500_14_20` | 0.713 | +579.5% |
| 3 | `et_500_20_20` | 0.713 | +577.1% |
| 4 | `et_500_8_5` | 0.713 | +573.2% |
| 5 | `et_300_20_20` | 0.700 | +575.5% |
| 6 | `et_300_8_10` | 0.700 | +575.5% |
| 7 | `et_300_8_20` | 0.700 | +575.2% |
| 8 | `et_100_8_10` | 0.700 | +572.9% |
| 9 | `et_100_20_20` | 0.700 | +563.0% |
| 10 | `et_300_8_5` | 0.700 | +562.5% |

## Phase 4 — multi-seed ensemble stability

- Seeds: 30
- Hit rate: **0.698 ± 0.012** (min 0.662, max 0.713)
- Mean return: **+574.3% ± 3.6%**
- Jaccard (pick stability across seeds): **0.938 ± 0.045**

## Phase 5 — robustness under perturbation

- Reps: 20, dropped 10% of training rows per rep
- Hit rate: **0.696 ± 0.017**
- Mean return: **+564.8% ± 11.0%**
- Jaccard (pick stability under perturbation): **0.898**

## Phase 6 — bootstrap 95% confidence intervals

- Hit rate 95% CI: **[0.693, 0.702]**
- Mean return 95% CI: **[+572.8%, +575.5%]**

## Notes

- Walk-forward CV: for each window t, train on windows < t, score window t.
- Hit rate = % of top-20 picks that reach +100% return within 12 months.
- Recent-4 aggregation: windows with complete 12mo forward data.
- Jaccard measures pick-set overlap (1.0 = identical sets, 0 = disjoint).

Report generated: 2026-04-16T22:52:00.192133