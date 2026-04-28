# Form 4 cluster signal — findings (2026-04-27)

## Headline

**Inconclusive — data window mismatch, not a signal failure.**

openinsider scrapes return the *latest 500 filings per symbol*, which for the cached universe (large-cap survivors) means 2024-2026 only. But the walk-forward windows in `upside_hunt_scored.csv` span 2016-02 → 2024-08. Almost no overlap exists, so even the loosest gate (`≥1 insider buying ≥$100k in 30d`) fires only 3 times across 2,851 symbol-windows. We cannot answer the ship/kill question with this data.

**Next step before another run**: backfill historical Form 4 filings from SEC EDGAR (free, complete back to 2003). The EDGAR Form 4 archive is per-CIK XML; we already have the CIK mapping in `signals_sec_edgar.py` so the lift is fetcher + parser + DB ingestion, not cold-start.

Once EDGAR-backed data covers ≥1,500 symbols × 2018-2024, re-run this script unchanged — feature builder + walk-forward harness are already wired and the gate variants will give a clean directional answer.

---

**Coverage**: 2,851 / 67,599 scored rows (4.2%) — Form 4 cache covers 107 symbols vs 2272 in full universe.

**Gates tested** (windows = 30 days prior to each as_of):

- `gate_strict`: 0 symbol-windows fired (0.0% of joined sample)
- `gate_moderate`: 0 symbol-windows fired (0.0% of joined sample)
- `gate_loose`: 3 symbol-windows fired (0.1% of joined sample)
- `gate_z_only`: 0 symbol-windows fired (0.0% of joined sample)
- `any_buy`: 3 symbol-windows fired (0.1% of joined sample)

## Cohort hit rates

| Cohort | n | mean_ret | median | hit +10% | +25% | +50% | +100% |
|---|---|---|---|---|---|---|---|
| A. Form 4 universe baseline (all symbol-windows) | 2,851 | +36.9% | +11.0% | 51.4% | 34.7% | 18.3% | 7.8% |
| B. Top-50/win by ensemble_score (current production) | 1,750 | +50.4% | +12.3% | 52.5% | 37.5% | 21.1% | 10.2% |
| C[gate_strict]. Top-50 AND ≥3 insiders AND z>1.0 | 0 | — | — | — | — | — | — |
| D[gate_strict]. Top-50 AND NOT ≥3 insiders AND z>1.0 | 1,750 | +50.4% | +12.3% | 52.5% | 37.5% | 21.1% | 10.2% |
| E[gate_strict]. ≥3 insiders AND z>1.0 (any score) | 0 | — | — | — | — | — | — |
| C[gate_moderate]. Top-50 AND ≥2 insiders AND z>0.5 | 0 | — | — | — | — | — | — |
| D[gate_moderate]. Top-50 AND NOT ≥2 insiders AND z>0.5 | 1,750 | +50.4% | +12.3% | 52.5% | 37.5% | 21.1% | 10.2% |
| E[gate_moderate]. ≥2 insiders AND z>0.5 (any score) | 0 | — | — | — | — | — | — |
| C[gate_loose]. Top-50 AND ≥1 insider AND ≥$100k | 1 | +1.8% | +1.8% | 0.0% | 0.0% | 0.0% | 0.0% |
| D[gate_loose]. Top-50 AND NOT ≥1 insider AND ≥$100k | 1,749 | +50.4% | +12.4% | 52.5% | 37.6% | 21.1% | 10.2% |
| E[gate_loose]. ≥1 insider AND ≥$100k (any score) | 3 | -3.1% | +1.8% | 33.3% | 0.0% | 0.0% | 0.0% |
| C[gate_z_only]. Top-50 AND z>1.0 (any insider count) | 0 | — | — | — | — | — | — |
| D[gate_z_only]. Top-50 AND NOT z>1.0 (any insider count) | 1,750 | +50.4% | +12.3% | 52.5% | 37.5% | 21.1% | 10.2% |
| E[gate_z_only]. z>1.0 (any insider count) (any score) | 0 | — | — | — | — | — | — |
| C[any_buy]. Top-50 AND any insider buy in 30d | 1 | +1.8% | +1.8% | 0.0% | 0.0% | 0.0% | 0.0% |
| D[any_buy]. Top-50 AND NOT any insider buy in 30d | 1,749 | +50.4% | +12.4% | 52.5% | 37.6% | 21.1% | 10.2% |
| E[any_buy]. any insider buy in 30d (any score) | 3 | -3.1% | +1.8% | 33.3% | 0.0% | 0.0% | 0.0% |

## Per-gate lift vs production baseline B

| Gate | n | hit_100 | Δ vs B | mean_return | Δ vs B mean | call |
|---|---|---|---|---|---|---|
| C[gate_loose]. Top-50 AND ≥1 insider AND ≥$100k | 1 | 0.0% | -10.2pp | +1.8% | -48.6pp | kill |
| C[any_buy]. Top-50 AND any insider buy in 30d | 1 | 0.0% | -10.2pp | +1.8% | -48.6pp | kill |

## Limitations

- Form 4 cache covers 107 symbols, all large-cap survivors. Full universe = 2272 symbols. Sample is biased toward names that have continued to exist and attract investor attention.
- openinsider scrapes have variable coverage by year; older filings may be missing for some symbols.
- Bar from memory: ≥1pp 1yr MAPE lift on 2,000+ symbols × 2018-2025 walk-forward. This experiment hits ≪2,000 symbols, so lift here is directional only, not ship-grade.