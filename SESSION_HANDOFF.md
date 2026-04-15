# Session Handoff — 2026-04-15 (end of day)

> Replace "Read SESSION_HANDOFF.md and MEMORY.md then tell me where we left off" to resume.

## TL;DR of today's session

Shipped a **drift-free, honest projection engine** to production at **https://s-tool.io** running a **Russell 3000 universe (2,609 tickers)** on a FastAPI backend at Railway, fronted by a Cloudflare Worker that serves the static UI and reverse-proxies `/api/*`. All drift-tilt signals (sentiment, Public Pulse, HY OAS, margin debt) were backtested and zeroed — they're noise on post-2023 data. The frontend got a landing hero, watchlist, info tooltips on every important field, a glossary card, and a "how it works" explainer. A SKEW + breadth strength sweep is still running in the background — if SKEW with a flipped sign (momentum interpretation) shows meaningful MAPE lift, it's the first signal we've found that actually works post-2023.

## Live production stack

| Layer | What | Where |
|---|---|---|
| Domain | s-tool.io | Cloudflare zone `c0abd1c036057a443c0a40aa25d1da14` |
| Edge worker | `s-tool-site` (serves UI + proxies /api/*) | wrangler deploys from `cloudflare/` |
| API | FastAPI (`api.py`) | Railway service `c2ee9f5b-22e3-41db-b9f4-77bb53843c87` at `api-production-9fce.up.railway.app` |
| Daily cron | `worker.py --all` at 10:00 UTC (6am ET DST) | Railway service `a4a9e05b-83fb-4e7e-b1bc-2a4e6c83fdde` |
| Price cache | `data_cache/prices/<SYM>.csv` (2,597/2,609 = 99.5%) | local + shipped with deploy |
| R2 bucket | `s-tool` | created, not wired to code yet |
| GitHub | `jgmynott/s-tool-projector` | auth via `gh` CLI as `jgmynott` |

**Railway manual deploy** (no auto-deploy wired): pass `latestCommit: true` to `serviceInstanceDeploy` mutation. Example in recent calls.

**Cloudflare MCP** works for account `18479801bf03932e04409c95b49e358a` (Stool@s-tool.io's Account). Worker **deploy** requires wrangler (authed via browser earlier).

## What's shipped this session

- **Tilts zeroed** in `projector_engine.py` — SENTIMENT_TILT_STRENGTH=0, all TILT_WEIGHTS=0, MAX_FUNDAMENTAL_TILT=0
- **Universe expanded** 537 → **2,609** via new `universe.py` module (iShares IWV holdings, auto-refresh every 24h)
- **`price_backfill.py`** — yfinance-batched 5yr-then-2yr-fallback history cache for the whole universe
- **FMP migration** `/v3/` → `/stable/`: profile, ratios (+ key-metrics merge for ROE/ROA), historical-price-eod/full, earnings, insider-trading/latest, price-target-consensus. Field renames fixed (`pe` → `priceToEarningsRatio`, `debtEquityRatio` → `debtToEquityRatio`).
- **1 req/sec FMP throttle** (`TokenBucketLimiter`) + daily limit raised from 250 → 300,000 for Premium tier.
- **Railway deploy scaffolding**: `Procfile`, `railway.json`, `runtime.txt`, slimmed `requirements.txt` (moved torch/transformers/pytrends to `requirements-research.txt`).
- **`cloudflare/` directory**: `wrangler.toml` + `src/worker.js` + `public/index.html`. Worker routes `s-tool.io/*` → ASSETS for UI, `/api/*` → Railway.
- **Frontend cleanup**: retired Public Pulse panel, hid tilt breakdown + sentiment intel + put/call + news-sentiment rows since they're all dead or paid-tier gated.
- **Landing hero + "How it works" explainer + glossary + 27 info tooltips + watchlist (localStorage).**
- **Two new signals built and wired into `comprehensive_backtest.py`**: `signals_skew.py` (CBOE SKEW z-score) and `signals_breadth.py` (% of our cached universe above 200-day SMA).

## Backtest findings (hardened)

| Signal | Test | Result |
|---|---|---|
| Sentiment / Public Pulse / HY OAS / margin debt | Various full-universe 2018–2025 sweeps | All within ±0.17 to ±0.43pp MAPE; hit rate drops with tilts. **DEAD.** |
| SKEW (contrarian sign, S0.06) | 2,478 symbols × 466 windows × 2018–2025 | 1yr MAPE **–0.73pp** (worse). Implies flipped sign (high SKEW → bearish, momentum interp) = **+0.73pp improvement**. |
| Breadth (contrarian sign, S0.06) | same | 1yr MAPE **+0.43pp** (small positive, hit rate slightly worse). Marginal. |

**The SKEW sweep with both signs and 5 strengths is running in background as of handoff.** Output lands in `skew_sweep_backtest.csv` / `skew_sweep_report.md`. If positive sign flip confirms, this is the first signal we've found that works post-2023 — wire into `projector_engine.py` with the winning strength, add a `SKEW` line to the glossary, redeploy.

## Known costs + tokens rotated

- **FMP Premium** — user subscribed, price not verified by me (user said $29/mo for Starter; I should check site.financialmodelingprep.com/pricing before quoting any number)
- **Railway** — on free/hobby tier, no charges seen yet
- **Cloudflare R2 + Workers + Pages** — free tiers, no charges seen yet; **docs say no minimum monthly charge** for R2 above free tier
- **Domain** — ~$60/yr per user
- **Claude Code** — $200/mo (big rock)
- **Tokens**: Railway `1ababd88-…` rotated to `130f3dca-19a6-458a-a527-d22c26f5e283` via API. Old Cloudflare `cfat_5C9v…` never worked (likely a CF Access token, not API) — user to manually delete from dashboard if wanted.

## What's NOT done and why

| Item | Why not | How to finish |
|---|---|---|
| SKEW strength sweep results analysis | Still running when handoff written | Read `skew_sweep_report.md` once done; pick the winning (sign × strength) pair; wire into engine |
| Auto-deploy Railway on `git push` | Railway GraphQL `deploymentTriggerCreate` blocked: "no one in the project has access" — needs GitHub OAuth linked at account level | User visits https://railway.com/account → Integrations → Connect GitHub. Then retry trigger mutation. |
| PDF export feature | Asked at end of session. Recommended Option A (magic-URL unlock + client-side PDF via `html2pdf.js`) | Wait for user to confirm option, then build. ~15 min implementation. |
| CF `cfat_…` token manual delete | Requires user dashboard action; it's invalid so low urgency | Delete from https://dash.cloudflare.com/profile/api-tokens or Zero Trust → Access |
| Paid Finnhub/Polygon data | Backtest said same-family signals are dead | Don't pay until a free-data signal is proven to work |
| Proper user auth + watchlists server-side | Deferred for beta; localStorage watchlist is good enough | When MAU justifies it, add Clerk or Cloudflare Access |

## Key files to know about

**Research / backtest:**
- `comprehensive_backtest.py` — unified harness, modes include `skew_sweep`, `breadth_sweep`, `skew_breadth_combo` now
- `signals_skew.py`, `signals_breadth.py` — today's new signals
- `skew_breadth_backtest.csv`, `skew_breadth_report.md` — first pass results
- `skew_sweep_backtest.csv`, `skew_sweep_report.md` — full strength sweep (in flight at handoff)

**Engine + API + UI:**
- `projector_engine.py` — engine, tilts zeroed with comment explaining why
- `data_providers.py` — FMP migrated to /stable/, throttled
- `api.py` — FastAPI, `/api/health`, `/api/project`, 18h projection cache
- `worker.py` — daily cron target; FULL_UNIVERSE imported from `universe.py`
- `universe.py` — IWV holdings → Russell 3000 (2,581) + SP500 ∪ WSB → 2,609 combined
- `frontend.html` — hero, watchlist, tooltips, glossary, how-it-works, same render paths for projection

**Infra:**
- `cloudflare/wrangler.toml`, `cloudflare/src/worker.js`, `cloudflare/public/index.html` (copy of frontend.html)
- `Procfile`, `railway.json`, `runtime.txt`
- `.gitignore` excludes `data_cache/`, `*_backtest*.csv`, overnight logs, Claude internals

## Resume recipe

1. Read this file + `MEMORY.md` (auto-loads)
2. Check if SKEW sweep finished: `cat skew_sweep_report.md 2>/dev/null | tail -30`
3. If SKEW works → wire into `projector_engine.py`, redeploy Railway (`serviceInstanceDeploy latestCommit: true`)
4. Confirm PDF export approach with user and build if asked
5. Otherwise, pick from the pending list above

## Snapshot commands

```bash
cd ~/Documents/Claude/s2tool-projector
git log --oneline -10
/usr/bin/curl -s https://s-tool.io/api/health | python3 -m json.tool
ls data_cache/prices/*.csv | wc -l                 # 2,597 or 2,598 expected
python3 universe.py list                            # Russell 3000 universe sizes
tail -30 skew_sweep_report.md                       # if running/done
```
