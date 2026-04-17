# Deep research v4 — the honesty round (2026-04-17)

Total runtime: **0.4 min**

v2 + v3 established ExtraTrees as the winning model with a 71.3% hit rate at +100% returns. But v3's permutation importance revealed that the model's edge is driven almost entirely by `log_price` (importance +0.505, all others <0.06). This raised a critical question: *is this real alpha, or just the small-cap premium?* v4 investigates.

## Phase I — price-controlled study

Train a model per log_price quintile. Lift should stay >1.5x if the model adds value beyond small-cap.

**Median within-quintile lift: 3.49x**

| quintile | rows | baseline | model hit rate | lift | mean return |
|---|---|---|---|---|---|
| quintile_1 | 4448 | 0.136 | 0.650 | 4.78x | +557.7% |
| quintile_2 | 4442 | 0.047 | 0.163 | 3.49x | +38.3% |
| quintile_3 | 4441 | 0.029 | 0.100 | 3.39x | +39.8% |
| quintile_4 | 4442 | 0.025 | 0.062 | 2.52x | +31.9% |
| quintile_5 | 4445 | 0.011 | 0.037 | 3.55x | +20.2% |

_Strong within-quintile lift → model has alpha beyond small-cap beta. Lift near 1.0 → the edge IS the small-cap premium._

## Phase J — excess-return labeling

- Excess-labeled model (strips universe median per window): absolute hit@+100% = **0.700**
- Absolute-labeled control: hit@+100% = **0.713**

_If excess-labeled hit rate holds near the absolute rate, the model captures regime-independent alpha. If it collapses, the model is mostly picking up high-beta names during bull windows._

## Phase K — size-neutral ensemble

| method | hit@+100% | mean return |
|---|---|---|
| unconstrained top-20 | 0.713 | +579.6% |
| size-neutral (4/quintile) | 0.325 | +249.6% |

Unconstrained pick composition across price quintiles:

| quintile | picks |
|---|---|
| quintile_1 | 79 |
| quintile_2 | 1 |
| quintile_3 | 0 |
| quintile_4 | 0 |
| quintile_5 | 0 |

_If size-neutral rate is close to unconstrained, the model has edge across all sizes. If it drops sharply, most of the edge concentrates in one (small-cap) quintile._

## Phase L — year-out-of-sample (train ≤2023, test 2024)

- Train rows: 15431, test rows: 6787 across 3 windows
- 2024 hit rate: **0.600**
- Baseline (no selection): **0.052**
- Lift: **11.44x**
- Mean return: **+479.6%**

_This is the cleanest OOS test possible: no 2024 data ever seen during training/tuning. If lift holds here near the recent-4-window numbers, the model generalizes. If it collapses, prior reports were at least partially overfit._

## Phase M — explicit feature interactions

- Base (16 features): rate 0.713, mean +579.6%
- With interactions (23 features): rate 0.675, mean +574.7%

_Tree ensembles already capture nonlinear interactions internally. If explicit interaction features don't help, it's because the ET is already doing this work. If they DO help, the ET wasn't finding them — informs future feature engineering priorities._

## Honest bottom line

The model's headline 71% hit rate at +100% is impressive, but:

1. `log_price` dominates feature importance by a factor of 10×. The model is largely picking small-cap stocks.
2. Phase I will tell us whether the model adds value *within* price quintiles — that's the test of real alpha.
3. Phase L is the cleanest OOS test — 2024 never seen during model choice. If it holds up there, the claim is honest.
4. Before live trading: fix split-adjustment bugs (Phase 0 of Alpaca plan), survivorship bias, and add transaction cost drag.

Report generated: 2026-04-17T06:57:57.164201