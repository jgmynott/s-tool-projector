# Deep research v3 findings — 2026-04-16

Total runtime: **12.2 min**

Continuation from v2 (ExtraTrees winner at 71.3% hit rate). v3 explores cross-threshold specialization, sector models, stacking, calibration, regime conditioning, permutation importance, and an expanded hyperparameter grid.

## Phase A — cross-threshold targeted models

Each row is an ET classifier trained on the binary label at that threshold. Columns show hit rates at various realization levels.

| train target | hit@target | hit@+100% | hit@+200% | mean return |
|---|---|---|---|---|
| +50% | 0.662 | 0.575 | 0.362 | +416.9% |
| +100% | 0.588 | 0.588 | 0.375 | +438.8% |
| +200% | 0.388 | 0.575 | 0.388 | +420.3% |
| +300% | 0.312 | 0.537 | 0.350 | +404.7% |

## Phase B — per-sector ET models

ET trained on each sector independently. Small-sample sectors may underperform the universal model.

| sector | rows | hit rate | mean return |
|---|---|---|---|
| Healthcare | 909 | 0.300 | +90.3% |
| Technology | 892 | 0.263 | +96.6% |
| Industrials | 675 | 0.250 | +99.6% |
| Financial Services | 768 | 0.013 | +20.4% |

## Phase C — stacking ensemble

- Stacked ET (5 base models): rate **0.713**, mean **+575.9%**
- Simple mean of base scores: rate **0.713**, mean **+577.1%**

## Phase D — classifier vs regressor + calibration

- Classifier: rate **0.588**, mean **+438.8%**
- Regressor:  rate **0.713**, mean **+579.6%**

### Calibration curve (deciles of predicted probability on recent-4 windows)

| decile | mean predicted prob | realized +100% rate | realized +200% rate |
|---|---|---|---|
| decile_10 | 0.741 | 0.296 | 0.134 |
| decile_9 | 0.641 | 0.140 | 0.044 |
| decile_8 | 0.526 | 0.070 | 0.013 |
| decile_7 | 0.451 | 0.054 | 0.011 |
| decile_6 | 0.392 | 0.028 | 0.004 |
| decile_5 | 0.334 | 0.025 | 0.002 |
| decile_4 | 0.282 | 0.022 | 0.002 |
| decile_3 | 0.206 | 0.015 | 0.001 |
| decile_2 | 0.100 | 0.002 | 0.000 |
| decile_1 | 0.074 | 0.006 | 0.000 |

## Phase E — regime-conditional training

### Universal model, evaluated per regime

| regime | windows | hit rate | mean return |
|---|---|---|---|
| bull | 4 | 0.600 | +342.4% |
| choppy | 5 | 0.460 | +279.8% |
| bear | 1 | 0.450 | +278.7% |

### Regime-specialized models

| regime | windows | hit rate | mean return |
|---|---|---|---|
| bull | 4 | 0.388 | +272.8% |
| choppy | 5 | 0.450 | +240.5% |

## Phase F — permutation feature importance

Baseline hit rate on recent-4 windows: **0.650**. Each feature is shuffled 5× in the test set; table shows mean drop in hit rate (higher = more important).

| rank | feature | mean drop | std |
|---|---|---|---|
| 1 | `log_price` | +0.505 | 0.017 |
| 2 | `beta_180d` | +0.057 | 0.026 |
| 3 | `vol_low` | +0.045 | 0.029 |
| 4 | `mom_180d` | +0.000 | 0.014 |
| 5 | `high_52w_ratio` | +0.000 | 0.018 |
| 6 | `volume_z60d` | +0.000 | 0.008 |
| 7 | `realized_vol_60d` | -0.005 | 0.006 |
| 8 | `vol_hi` | -0.012 | 0.008 |
| 9 | `low_52w_ratio` | -0.013 | 0.011 |
| 10 | `p10_ratio` | -0.022 | 0.009 |

## Phase G — expanded ET hyperparameter grid

Evaluated 300 configs. Top 10:

| rank | config | hit rate | mean return |
|---|---|---|---|
| 1 | `et_n100_d22_l8_mfNone_bsTrue` | 0.725 | +586.4% |
| 2 | `et_n300_d10_l25_mfNone_bsFalse` | 0.725 | +582.2% |
| 3 | `et_n300_d14_l40_mfNone_bsFalse` | 0.725 | +563.2% |
| 4 | `et_n100_d6_l40_mfNone_bsFalse` | 0.725 | +552.2% |
| 5 | `et_n500_d18_l25_mfNone_bsFalse` | 0.713 | +581.0% |
| 6 | `et_n300_d18_l25_mfNone_bsFalse` | 0.713 | +581.0% |
| 7 | `et_n300_d22_l15_mfNone_bsTrue` | 0.713 | +580.3% |
| 8 | `et_n500_d14_l25_mfNone_bsFalse` | 0.713 | +579.2% |
| 9 | `et_n500_d22_l25_mfNone_bsFalse` | 0.713 | +578.0% |
| 10 | `et_n300_d14_l8_mfNone_bsTrue` | 0.713 | +578.0% |

## Notes

- Walk-forward CV on all phases; no test-set contamination.
- `hit rate` = % of top-20 picks reaching the threshold within 12 months.
- Recent-4 aggregation unless noted otherwise.
- v3 reuses v2's extended 16-feature set (research/deep_findings_v2_*).

Report generated: 2026-04-16T23:17:11.943490