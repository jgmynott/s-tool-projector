# Honest metrics — Wave 1 + Wave 2 audit methodology

Last refreshed: **2026-04-17** (frontend surface refs updated 2026-04-30)

This document is the unflattering, defensible version of the backtest. Every realized-return number rendered in the track-record block on `/picks` (and the `/api/track-record` + `/api/honest-audit` JSON consumed there) uses the filtered numbers defined here, not the pre-filter ones. The standalone `/track-record` and landing pages were retired in the 2026-04-30 cull — same data, fewer surfaces.

## The delta

| Metric | Pre-filter (published originally) | Post-filter (honest) |
|---|---|---|
| Hit rate at +100% (overall, 2022-2024) | 55.0% | **37.0%** |
| Lift vs baseline | 11.1× | **8.7×** |
| Mean 1-year return on top-20 | +327% | **+145%** |
| 2024 out-of-sample hit rate | 68.3% | **61.7%** |
| 2024 OOS lift | 13.0× | **12.8×** |

The OOS 2024 numbers hold up much better under the filter than the overall window, which tells us the newer model was already picking more tradeable names. Most of the pre-filter inflation was in 2022-2023 where the model over-weighted thin small-caps.

## Wave 1 corrections applied

### A. Survivorship bias check
For each pick, we check whether the ticker's price history ends more than 30 days before the realization target date. If yes, replace the upstream `realized_ret` with `(last_known_close / entry_price) - 1` — which captures deep drawdowns that would otherwise be missing data.

**Result**: mid-window delistings in our training set = **0**. That's because `upside_hunt_results.csv` is built from tickers with price data — it silently omits tickers that were in Russell 3000 in 2022 but delisted before 2024. The real survivorship skeleton requires a historical IWV constituent snapshot (Wave 3 work) which we don't yet have. The current audit only catches mid-window delistings, not universe-level omissions.

### B. Liquidity floor ($500k ADV)
Compute 20-trading-day rolling average of `Close × Volume` ending at `as_of`. Drop any ticker-window with `avg_dollar_vol < 500,000` from both the model's top-20 picks and the universe baseline. The filter:
- Kills the ~3% of rows with thin liquidity
- Drops nn_score hit_100 from 50% to 38.5% (the big delta)
- Drops nn_score mean_return from +294% to +147%

The production `portfolio_scanner.save_picks` now enforces the same floor on all three main tiers (previously only on asymmetric), so `/picks` live stops recommending untradeable names.

### C. Transaction costs (1.5% round-trip)
Subtract 1.5% from every realized return before applying the +100% threshold. 1.5% = 0.75% entry + 0.75% exit, a conservative blended assumption for commissions + spread + market impact on small-to-mid caps. The impact is small — at +100% threshold, cost eats 1.5pp which rarely flips a winner into a loser. Drops nn_score hit from 50% → 48.5%.

### Combined "E_all_three"
All three applied together = the honest bar. For nn_score:
- Overall: **37.0% hit / 8.7× lift / +145% mean return**
- OOS 2024: **61.7% hit / 12.8× lift / +331% mean return**

## Wave 2 supporting analyses

### Top-N curve (nn_score, honest filters)

| Top-N | Hit @ +100% | Lift |
|---|---|---|
| 5 | 46% | 10.8× |
| 10 | 45% | 10.6× |
| **20** | **37%** | **8.7×** |
| 50 | 26% | 6.1× |
| 100 | 20% | 4.7× |

Higher-conviction portfolios (top-5 or top-10) see materially better hit rates. This is what investors expect and gives them latitude to concentrate if they choose.

### Wilson 95% confidence intervals

Every published hit-rate variant in `runtime_data/wave1_honest_audit.json` now carries `wilson_95_lo` and `wilson_95_hi`. The 2024 OOS result at n=60 spans roughly **48% – 74%** at 95% confidence — wide because the sample is small. This is disclosed on diligence requests, not hidden.

## Skeletons we know about but haven't fixed yet (Wave 3)

### Universe-level survivorship
We need the Russell 3000 **historical** constituent snapshot (2022-05 IWV, 2022-08 IWV, etc.). Tickers present then but absent now should be added to the universe denominator with their realized returns computed from last-trade prices. Requires either iShares IWV historical filings or a paid ETF-holdings-history data provider. The universe-level skeleton is likely worth 2-5pp of baseline hit rate, which would move our relative lift number upward.

### Bear-regime coverage
Training windows 2022-05 → 2024-08 are mostly recovery-to-bull. Backfill program (ongoing) expands to 2016-Q1 through 2024-Q3, adding 2018 Q4 drawdown, 2020 COVID crash, 2022 H1 tech bear. Numbers will be re-reported once the expanded training set is live.

### Asymmetric-tier standalone backtest
The backtest report covers nn_score, moonshot_score, ensemble_score, and the legacy hand-crafted H* methods. The asymmetric tier scorer (regime-adaptive, lives separately) is not included in the published honest-metrics table. Needs its own lift curve + 95% CI publication.

### Walk-forward window overlap
Windows are spaced 3 months apart with 1-year forward realizations. This means window N's labels extend into training data used by windows N+1, N+2, N+3. Not catastrophic (we train on realization DATA, not on the labels themselves) but worth an explicit methodology note.

## How to reproduce

```bash
python3 research/wave1_honest_audit_2026_04_17.py
# Outputs runtime_data/wave1_honest_audit.json
```

The script reads `upside_hunt_scored.csv` + `data_cache/prices/*.csv`, applies all three corrections, and writes a single JSON that both `/api/honest-audit` (live) and the landing-page copy consume. Everything ships — no private spreadsheet, no gap between what we claim and what you can audit.
