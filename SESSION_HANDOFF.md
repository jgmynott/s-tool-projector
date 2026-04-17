# Session Handoff — 2026-04-17 (evening, post honest-audit + docs sweep)

> **Resume with:** `Read SESSION_HANDOFF.md and MEMORY.md, then check the backfill + regen background jobs and tell me where we left off.`

---

## 🏃 Background jobs that should still be running

Two Python processes were left running when the previous session ended:

| Job | Purpose | Expected finish |
|---|---|---|
| `backfill_prices_historical.py --target-start 2015-01-01 --sleep 1.2` | Extending all 2,272 price CSVs back to 2015-01-02 via yfinance | ~90–120 min from 2026-04-17 17:33 local |
| `regenerate_training_set.py` (log: `/tmp/regen_main.log`) | Polls until ≥ 85% of tickers have history to 2015-01-15, then chains `upside_hunt.py → overnight_learn.py → overnight_backtest.py → research/wave1_honest_audit_2026_04_17.py` | ~3 h end-to-end once backfill finishes |

**First thing to do:** `ps aux | grep -E "(backfill_prices|regenerate_training)" | grep -v grep` and `tail -50 /tmp/regen_main.log`.

- If both dead + no regen logs in `/tmp/regen_*.log` → backfill failed; investigate and restart.
- If backfill done + regen running → let it run.
- If regen complete → four new artifacts to commit (below).

### Bug fixed mid-session (don't re-introduce)

`regenerate_training_set.py` originally had `TARGET_START = "2015-01-01"` — but yfinance's first trading day of 2015 is Jan 2, so `first <= target` was always False and the poller would hang forever. Changed to `"2015-01-15"`. If you see a similar check elsewhere, apply the same fix.

---

## ✅ What got shipped today

### Wave 1 honest audit (live on prod)
- `/api/honest-audit` serves `runtime_data/wave1_honest_audit.json`
- Landing page + `/track-record` show **37% hit / 8.7× lift / +145% mean** (overall, 2022-24) and **61.7% / 12.8× / +331%** (2024 OOS)
- Filters applied: $500k ADV liquidity floor + 1.5% round-trip tx cost + survivorship mid-window check
- Published with pre-filter delta (55% → 37%) documented openly in `docs/honest-metrics.md`

### Wave 2
- Top-N curve (top-5 46%, top-10 45%, top-20 37%, top-50 26%, top-100 20%)
- Wilson 95% CIs attached to every hit-rate variant
- 2024 OOS (n=60) spans roughly 48%–74% at 95%

### Four new data sources wired into nightly slow path
| Feed | Table | Script |
|---|---|---|
| yfinance short interest (shortPctFloat, shortRatio, MoM delta) | `short_interest_yf` | `signals_short_interest_yf.py` |
| yfinance 38 fundamental/technical ratios | `ratios_yf` | `signals_ratios_yf.py` |
| Finnhub earnings + analyst trend | `earnings_finnhub` | `signals_earnings_finnhub.py` |
| Finnhub `/stock/metric` (27 multi-year ratios) | `metrics_finnhub` | `signals_metrics_finnhub.py` |

Nightly ablation: `research/nightly_short_interest_ablation.py` writes dated JSONs to `runtime_data/short_interest_ablation_<date>.json`. Auto-promotion logic not yet built — ablations require manual review before features go into `FEATURE_COLS`.

### Pipeline split (stable for 2+ days)
- `daily-refresh-fast.yml` — 20:00 UTC, 45 min cap, `worker.py --preferred` (~537 tickers)
- `daily-refresh-slow.yml` — 23:00 UTC, 150 min cap, full research + NN retrain + backtest

### Docs refreshed
- `README.md` — full rewrite, honest numbers in hero table
- `docs/deployment.md` — Railway project-vs-account token gotcha, Volume setup, gitignore pattern
- `docs/data-sources.md` — NEW, every feed catalogued
- `docs/honest-metrics.md` — NEW, Wave 1+2 methodology

Committed in `d02e586`.

Linear S-88 updated with evening notes. Notion page `33cf521450a7812abfcdecf323b00da4` updated.

---

## 📋 When regen completes

```bash
# Verify artifacts look sane
jq '.scorers.nn_score.overall.E_all_three' runtime_data/wave1_honest_audit.json
jq '.honest_metrics.year_oos_hit_100' runtime_data/backtest_report.json
wc -l upside_hunt_results.csv  # should be 3-4× larger than the 2022-24-only version

# Commit + push
git add upside_hunt_results.csv upside_hunt_scored.csv \
        runtime_data/backtest_report.json \
        runtime_data/wave1_honest_audit.json \
        data_cache/*.json
git commit -m "data: regenerate training set 2016-2024 (35 windows, +2021 continuity)"
git push

# Kick a slow run to re-deploy artifacts + rebuild NN against the expanded set
gh workflow run daily-refresh-slow.yml --ref main
```

Numbers to watch after regen:
- **2018 Q4 / 2020 / 2022 regime coverage** now in the training set — expect the overall honest hit rate to shift (could go down if tech-bear windows hurt, up if 2021 continuity helps). Either direction is fine — ship the honest number.
- Re-run `research/wave1_honest_audit_2026_04_17.py` if the chained step failed but upside_hunt + backtest succeeded.

---

## 🎯 Active backlog

**Pending from previous sessions:**
- `#9` — verify live `/picks` renders asymmetric tier with 10 tickers (user verification still owed)
- `#14` — next-session new-data-source research per Linear S-88 consolidation

**Queued data sources (keys configured, not yet wired):**
- Polygon options flow — `/v3/snapshot/options/<symbol>`, 5 req/min free tier → top-100 tickers only
- SEC Form 4 insider transactions — bulk feed, free
- 13F institutional ownership — SEC quarterly, free

**Wave 3 skeletons (deferred, documented):**
- Universe-level survivorship (needs historical IWV constituents — paid data or iShares filings)
- Asymmetric tier standalone backtest + 95% CI publication
- Walk-forward window-overlap methodology note
- Auto-promotion of ablation-validated features into `FEATURE_COLS`

---

## 🚫 Do NOT

- Re-add Russell 3000 long-tail to slow path — two past runs timed out before deploy. If needed, weekly workflow, not nightly.
- Commit `data_cache/prices/` or `data_cache/sec_edgar/facts/` — heavy, already gitignored. Runtime JSONs belong in `runtime_data/`.
- Reference "Medallion" anywhere in copy (trademark).
- Quote pricing from memory — always WebFetch first.
- Publish methodology recipes (parameter values, method names, provider shopping lists) on the public site. Philosophy + stats only.
- Ship UI changes without the 7-page mobile eyeball sweep (320–480px) + hover/click/scroll/resize interaction QC.

---

## 📍 Current product state (2026-04-17 EOD)

- **Live:** https://s-tool.io
- **Stack:** Cloudflare Worker + Railway FastAPI + Clerk + Stripe + SQLite (Volume-persisted users.db)
- **Nightly:** Fast 20:00 UTC + Slow 23:00 UTC, both auto-deploy on green
- **Honest numbers:** 37% / 8.7× / +145% (pre-regen). Will refresh once `regen_main.log` shows `REGEN COMPLETE`.
- **Tiers comped manually:** `kevinrvandelden@gmail.com` (hard-coded in `users_db.py`)
- **Last commit:** `d02e586` (docs)
