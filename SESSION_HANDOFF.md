# Session Handoff — 2026-04-15

Write-up for the terminal Claude Code session picking up from the desktop app session.

## What's in flight vs done

### ✅ Shipped this week
- **6 tilt signals wired into production** engine (`projector_engine.py`):
  analyst_target (FMP), eps_growth (FMP), insider (FMP), macro (FRED), recommendation (Finnhub), put_call_contra (yfinance options), public_pulse (composite).
- **Earnings-proximity sigma boost** — widens bands as earnings approach, via Finnhub calendar.
- **Public Pulse module** (`public_pulse.py`) — Google Trends + Wikipedia + GDELT + broad Reddit + Michigan CSI, all collectors built.
- **Full PP backfill** — 107 symbols × 3 years × 3 sources in `public_pulse_data/`.
- **Frontend updated** — `frontend.html` renders the new Public Pulse panel and tilt breakdown (committed, not deployed).
- **Figma mockup** — hero dashboard at https://www.figma.com/design/eMBkOhNhxJMt9FbREpaLYa
- **GitHub repo** at `github.com/stool-cell/s-tool-projector` (will migrate to `jgmynott` — see below)

### 🔬 Research finding (this is the headline)
**Crowd-sentiment tilts worked 2018-2022 (−8.4pp MAPE at 1yr), DIED in 2023-2025 (+0.5pp worse).**
Every text-based signal we can backtest is noise on post-2023 data. See:
- Linear S-87 (the finding) and S-88 (new-signal research task)
- Notion subpage: `2026-04-15 — Crowd-sentiment signals died ~2023 (backtest forensic)`
- Memory file: `project_regime_change_finding.md` (auto-loads in future sessions)

### 🔬 Non-text signals tested today
**None of them move the needle either:**

| Signal | Verdict | File |
|---|---|---|
| Net Liquidity (FRED WALCL−WTREGEN−RRP) | dead, −0.04 to −0.31pp | `net_liquidity.py`, `netliq_backtest.csv` |
| Form 4 insider buying (openinsider) | faint +0.01-0.04pp (5% of obs have signal — data-sparse) | `form4_insider.py`, `form4_backtest.csv` |
| HY OAS enhanced (4 features) | **built but NOT backtested** | `macro_signals.py` |
| Margin debt (FINRA via FRED) | **built but NOT backtested** | `macro_signals.py` |

Live readings right now: HY OAS tilt **−0.48** (credit widening), Margin debt tilt **−1.00** (extreme peak → NYU Stern says expect SPX −7.8% over 12mo).

### 🔜 Pending when you pick up in terminal
1. **Form 4 cohort test** — rerun the 22 symbols that actually had signal (full universe diluted it)
2. **Backtest HY OAS + margin debt** — modules built, just run `comprehensive_backtest.py --mode` for each (need to add modes to the script — quick edit)
3. **GitHub migration stool-cell → jgmynott** — user exported (`~/Downloads/443ea640-*.tar.gz`), gh CLI auth on this machine is logged out. Terminal session: run `gh auth login -h github.com --web --scopes repo,workflow`, then I'll help retire stool-cell and push to jgmynott/s-tool-projector.
4. **Production decision** — dial sentiment + PP tilts to zero in `projector_engine.py`? Discussed but unshipped.
5. **Workflow file push** — `.github/workflows/daily-refresh.yml` is staged locally. Needs workflow OAuth scope + push.
6. **Cloudflare deploy** — final milestone. All scaffolding ready.

## Quick state check commands

```bash
cd ~/Documents/Claude/s2tool-projector
git log --oneline -10                      # commit history
git status                                  # should be clean
ls public_pulse_data/*.csv | wc -l          # backfill data count
python3 net_liquidity.py latest            # live net-liq reading
python3 macro_signals.py latest            # live HY OAS + margin debt
python3 form4_insider.py latest TSLA       # sample ticker form4 signal
```

## Key files (absolute paths)

**Signal modules (all have CLIs):**
- `data_providers.py` — live provider chain (FMP, Finnhub, Polygon, FRED, yfinance)
- `public_pulse.py` — 5-source composite for PP
- `public_pulse_backfill.py` — historical PP crawler
- `net_liquidity.py` — TGA/reserves/RRP signal
- `form4_insider.py` — openinsider scraper + signal
- `macro_signals.py` — HY OAS + margin debt signals

**Backtesters:**
- `comprehensive_backtest.py` — unified harness (sentiment, PP, net_liq, form4 modes)
- `sentiment_backtest.py` — original Phase C engine (kept for reference)
- `projector_backtest.py` — original simple backtester

**Engine + API + UI:**
- `projector_engine.py` — production MC+MR + tilts
- `api.py` — FastAPI backend (port 8000)
- `worker.py` — batch precompute for S&P 500 + Nasdaq 100
- `frontend.html` — dashboard (PP panel live)

**Data:**
- `sentiment_data/` — WSB FinBERT results
- `public_pulse_data/` — all PP backfill + form4 + net_liq + macro CSVs
- `sentiment_backtest_full_results.csv` — the original Phase C 2018-2025 data
- `comprehensive_backtest_full.csv` — 107 × 3yr × 17 config sweep (62,985 rows)
- `phase_c_reproduction.csv` — the definitive reproduction showing signal died 2023+
- `form4_backtest.csv` — today's Form 4 result
- `netliq_backtest.csv` — today's net-liq result

**Ops / infra (for migration):**
- `.env` — API keys (gitignored)
- `.github/workflows/daily-refresh.yml` — cron (STAGED, unpushed; needs workflow OAuth)
- `run_overnight.sh`, `overnight_v3.sh` — overnight pipelines (already executed)

## Auth state

- **gh CLI**: logged out (stool-cell signed out, jgmynott browser step never completed)
- **Local git remote**: still points to `https://github.com/stool-cell/s-tool-projector.git`
- Terminal should start with: `gh auth login -h github.com --web --scopes repo,workflow` — login as jgmynott.

## Backtest results reference (for "did we already test this?")

| Mode | Result | File |
|---|---|---|
| sent_only 107×2023-25 (phase_c_reproduction) | ΔMAPE −0.00pp | `phase_c_reproduction.csv` |
| sent_only 107×2018-25 (original Phase C) | ΔMAPE −8.38pp ✅ (signal's dead era) | `sentiment_backtest_full_results.csv` |
| sweep (17 configs PP+sent+combo) 107×2023-25 | all within ±0.07pp | `comprehensive_backtest_full.csv` |
| netliq_sweep 107×2020-25 | −0.04 to −0.31pp (worse) | `netliq_backtest.csv` |
| form4_sweep 107×2020-25 | +0.01 to +0.04pp (tiny, data-sparse) | `form4_backtest.csv` |

## When the terminal session starts

Tell it: **"Read SESSION_HANDOFF.md and MEMORY.md then tell me where we left off."** The memory system auto-loads from `~/.claude/projects/…/memory/MEMORY.md` in any Claude Code instance on the same machine — it's shared.
