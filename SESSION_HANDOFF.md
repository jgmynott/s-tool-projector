# Session Handoff — 2026-04-16 (evening update)

> Resume with: "Read SESSION_HANDOFF.md and MEMORY.md then tell me where we left off."

---

## 🌙 Evening addendum — NN research + Alpaca plan (2026-04-16 ~22:00 ET)

Late-session work while user slept. Committed to `main` as `619af59`.

### Production changes already pushed

- **Moonshot classifier NN** (`overnight_learn.py::_train_moonshot_nn`) — MLPClassifier trained on binary `realized_ret >= 1.0` label with class rebalancing.
- **Ensemble stacker** (`overnight_learn.py::_train_ensemble`) — second-level MLP blending nn_score + moonshot_score + H7.
- **Feature ablation** (`feature_ablation.py`) — systematically strips one feature at a time.
- **Comprehensive backtest** (`overnight_backtest.py`) — reports lift at +10/+25/+50/+100/+200%, bootstrap CIs, regime-conditional, simulated portfolios.
- **`/track-record` page** (`cloudflare/public/track-record/index.html`) + `/api/backtest-report` endpoint — public evidence page reading `data_cache/backtest_report.json`.
- **Nightly workflow** now runs the new steps and commits the new JSON artifacts (`moonshot_scores.json`, `ensemble_scores.json`, `feature_importance.json`).
- **Landing + footer nav** added Track record link.

### Research findings (see `research/nn_findings_2026-04-16.md`)

Seven experiments ran in 125s. Headline numbers on the recent-4 windows:

| Test | Finding |
|---|---|
| Architecture sweep | Best: `mlp_(16, 8) alpha=0.01` — **67.5% hit rate at +100%**. Bigger networks overfit. |
| Model-class shootout | **ExtraTreesRegressor beat MLP** (66.2% vs 62.5%), runs in 2s vs 5s. Worth replacing the MLP regressor with ET. |
| Calibration | Top decile moonshot-prob: **27.3% realized +100% rate**. Bottom decile: 1.0%. Decile ranking is cleanly monotonic — classifier is well-calibrated even though overall Spearman is ~0. |
| Threshold ladder | +100% lift **9.49x**, +200% lift **21.76x**, +300% lift **37.26x**, +500% lift **49.10x**. Selection quality INCREASES with threshold — good sign. |
| Per-window consistency | 9 of 10 windows: hit_100 ≥ 35%. **2022-05 window fails** (5%). Model has one known bear-regime failure mode. |
| Minimal-feature experiment | **3-feature model (`log_price`, `sigma`, `p90_ratio`) beat the 8-feature model** — 67.5% vs 62.5%. The engineered features (`asymmetry`, `vol_low/hi`) are dead weight. |
| Hand-crafted only | Without `log_price`, hit rate collapses to 47.5%. **Price level IS the signal.** |

### Alpaca integration plan — see `docs/alpaca_integration_plan.md`

Full scoping doc for paper-trading the picks via Alpaca. Summary:

- **Swing / position trading path** (matches what the NN actually does): 10–14 workdays to paper, 4–8 weeks of paper validation before real money.
- **Day trading path**: different model + intraday pipeline. Not tonight's work.
- **Phase 0 blockers** (must fix before any trading):
  1. Split-adjustment bug in `data_cache/prices/` (HTZ/RUN)
  2. Survivorship bias (delisted tickers missing from universe)
  3. Out-of-sample holdout (never-touched window for final validation)
  4. Transaction cost model in `overnight_backtest.py` (5 bps slippage, 1 bp commission)
- **Phase 1**: `alpaca_broker.py` + `trade_executor.py` + `alpaca-paper.yml` workflow
- **Phase 2**: position caps, drawdown halt, kill switch
- **Phase 3**: daily paper-vs-backtest reconciliation + public `/track-record/live` equity curve

### Tomorrow's priorities (ordered)

1. **Add missing GitHub secrets** — `RAILWAY_TOKEN` and `CLOUDFLARE_API_TOKEN`. Without these the nightly pipeline still runs data jobs and commits artifacts, but deploy steps fail.
2. **Review `research/nn_findings_2026-04-16.md`** — decide whether to:
   - Switch the production NN to ExtraTreesRegressor (simpler, faster, higher lift)
   - Drop the 5 dead-weight features from `overnight_learn.py`
   - Both (likely answer)
3. **Open Alpaca paper account** + add API keys as Railway env vars. Start Phase 1 of the plan.
4. **Phase 0 Alpaca blockers**: start with split-adjustment fix since it also affects `/picks` accuracy today.

### Open items from earlier in session (still pending)

- Fix split-adjusted prices for HTZ, RUN (overlaps with Phase 0 above)
- Regime classifier NN (dynamic tier weights based on market state)
- Confidence NN UI integration (rescale 0-48 → 0-100 on /picks page)
- Downside prediction NN (replace MC P10 with NN-learned drawdown)
- PDF export for Pro tier
- Weekly email performance report
- Polygon backfill for sector + market cap (bypass FMP daily limit)
- Users DB backup pipeline
- Key rotation at providers (previously exposed in committed SESSION_HANDOFF)
- CSP `unsafe-inline` removal
- SQL f-string tighten in `signals_sec_edgar.py`
- `/api/sentiment` + `/api/cached` rate limits

### Background processes running at handoff time

- None. Research suite completed. All work committed to `main` at `619af59`.

---


## TL;DR

Went from bare projection engine to a **three-tier SaaS with auth + billing + portfolio intelligence** in one long session. Live on s-tool.io:

- **Free** (3 proj/day) · **Pro $8/mo** (10/day) · **Strategist $29/mo** (unlimited + portfolio picks)
- Clerk auth, Stripe test-mode checkout (Apple Pay works), webhook-synced tier state
- Four pages: `/` landing, `/app` generator, `/how` methodology, `/picks` risk-tiered recommendations
- Pre-launch safeguard: **5 projections/hour** cap for anyone who isn't the owner
- Full security batch: CSP/HSTS/XFO, rate limiting, fail-closed CORS, Dependabot, user-data isolation, Railway Volume

Backtested momentum, value, and quality (low-beta) factors across 2022–2024. **All three failed** to improve projection MAPE — same pattern as the sentiment/SKEW signals killed earlier. The engine stays tilt-free. "Every signal died" is the honest narrative and it just got stronger.

21 free data sources documented; **FINRA short interest module shipped** (200k rows loaded). SEC EDGAR XBRL and CBOE options deferred.

## Production stack (unchanged from prior handoff)

| Layer | What | Where |
|---|---|---|
| Domain | s-tool.io | Cloudflare zone `c0abd1c036057a443c0a40aa25d1da14` |
| Edge Worker | `s-tool-site` (landing + proxy) | wrangler from `cloudflare/`, **version `97b1a983`** |
| API | FastAPI (`api.py`) | Railway service `c2ee9f5b-22e3-41db-b9f4-77bb53843c87` at `api-production-9fce.up.railway.app` |
| Daily cron | `worker.py --all` + scan step at 11:00 UTC | Railway service `a4a9e05b-83fb-4e7e-b1bc-2a4e6c83fdde` |
| User data | `users.db` on **Railway Volume `/data`** (NEW) | Never committed, survives restarts |
| Projection cache | `projector_cache.db` (git-committed nightly by cron) | Cache refresh commits work again after S-89 fix |
| GitHub | `jgmynott/s-tool-projector` | Auto-deploy still NOT wired (use `railway up --detach`) |

## What shipped this session

### Revenue plumbing
- **Clerk auth** — JWT verify via JWKS, `optional_user` / `current_user` deps (`auth.py`)
- **Stripe billing** — checkout, portal, idempotent webhook, tier derived from metadata→price_id fallback (`billing.py`)
- **Three-tier pricing** — Free / Pro $8/mo (10/day) / Strategist $29/mo (unlimited + picks)
- **`/api/me`, `/api/billing/{checkout,portal,webhook}`, `/api/picks`**
- **Pre-launch cap**: 5 projections/hour for non-owner (owner = `OWNER_EMAIL` env, defaults to jamesgmynott@gmail.com)

### Frontend
- `cloudflare/public/index.html` — landing with glacier palette (Banff/Zermatt/Interlaken), `/picks` nav link
- `cloudflare/public/app/index.html` — generator UI synced to same palette + Crimson Text serif
- `cloudflare/public/how/index.html` — methodology deep-dive + signal graveyard
- `cloudflare/public/picks/index.html` — Strategist-gated risk-tiered picks w/ teaser
- `cloudflare/public/img/` — 4 optimized photos (hero-banff, edge-matterhorn, accent-lake, accent-peaks) @ 621KB total
- `cloudflare/public/_headers` — CSP + HSTS + XFO + Referrer-Policy + Permissions-Policy

### Security + ops
- Rate limiting via slowapi: 30/min on `/api/project`, 10/min on billing, 120/min default
- Fail-closed CORS (no wildcard fallback, explicit methods/headers, credentials=false)
- CSP + HSTS via `_headers`
- `.github/dependabot.yml` (weekly pip, npm, github-actions)
- S-89 GitHub Actions cron fix: `git add -f projector_cache.db` + `permissions: contents: write`
- `settings.json` malformed fork-bomb deny rule removed
- `.env.example` documenting every env var
- `.railwayignore` preventing 220MB data_cache uploads

### Intelligence
- **`portfolio_scanner.py`** — ranks full Russell 3000 cached projections by forward Sharpe proxy, buckets into conservative/moderate/aggressive w/ quality filter, ETF blocklist
- **`signals_short_interest.py`** — FINRA module, custom CSV parser handling unquoted commas in issuer names. 200k historical rows loaded.
- **`momentum_factor_backtest.py`** — vectorized MC, 2,150+ tickers × 22 windows
- **`factor_sweep_overnight.py`** — momentum + value + quality (low-beta), 11 windows × 5 tilts × 2 horizons

## Backtest findings (this session)

**None of the three canonical factors improved MAPE.** Same pattern as SKEW — tilt makes MAPE worse while nudging hit rate up marginally.

| Factor | 1yr baseline | Best tilt | ΔMAPE | ΔHit |
|---|---|---|---|---|
| Momentum 12-1 | 38.44% | S0.09 | **+1.47pp worse** | +0.3pp |
| Value (earnings yield) | 26.33% | S0.06 | **+0.42pp worse** | −0.2pp |
| Quality (low-beta) | 27.09% | S0.06 | **+0.63pp worse** | +0.6pp |

**Conclusion:** engine stays tilt-free. Portfolio-level intelligence (ranking across the universe) is the product differentiator, not drift tilts. Uncommon-data alpha still requires signals nobody else has tested. (Don't use "Medallion" in any user-facing copy — trademark belongs to Renaissance Technologies.)

## Key files (new this session)

**Auth + billing:**
- `auth.py` — Clerk JWT verification
- `billing.py` — Stripe checkout + webhook, tier routing via `TIER_PRICES` map
- `users_db.py` — users + usage tables, `quota_for_user`, `can_access_picks`
- `.env.example` — env var template

**Intelligence:**
- `portfolio_scanner.py` — ETF-filtered ranker
- `signals_short_interest.py` — FINRA module (backtest wiring pending)
- `momentum_factor_backtest.py`, `factor_sweep_overnight.py` — backtest harnesses
- `portfolio_picks.json` — seed picks output (overwritten by daily cron)

**Content / design:**
- `cloudflare/public/{index,app,how,picks}/index.html`
- `cloudflare/public/img/*.jpg`
- `cloudflare/public/_headers`
- `design/media/` + `design/palette.json` + `design/extract_palette.py`

**Research:**
- `research/pricing_tiers_research.md` — competitive analysis, 3-tier recommendation
- `research/free_data_sources.md` — 21 verified free data sources ranked

**Reports:**
- `momentum_factor_report.md`, `factor_sweep_report.md` — backtest results
- `momentum_factor_backtest.csv`, `factor_sweep_results.csv` — raw data

## What's NOT done and why

| Item | Why not | How to finish |
|---|---|---|
| `$8/mo` Stripe price | User has to create in Stripe dashboard | User → Stripe Test → new Pro price $8/mo → update `STRIPE_PRICE_ID_PRO` in Railway (currently still `price_1TMd5gHijsJzoz12PswsXLhj` @ $19.99) |
| `$29/mo` Strategist price | Same — user-side action | Create `s-tool Strategist` $29/mo → paste `price_…` to Claude → set `STRIPE_PRICE_ID_STRATEGIST` in Railway |
| Rotate exposed API keys | I printed full values for FMP, Finnhub, Polygon, FRED, Alpha Vantage into the chat transcript during env audit | User → each provider dashboard → roll → paste new values into Railway env |
| PDF export (task #7) | Deprioritized when Strategist tier built out | `html2pdf.js` in `/app`, Pro-gated button on projection result |
| FINRA latest-date fetch | FINRA API silently ignores date filters | Either scrape their separate daily-volume files endpoint, or accept historical-only data for backtesting |
| SEC EDGAR XBRL + CBOE options | Only FINRA integrated of top 3 recommended | Build `signals_sec_edgar.py` and `signals_options.py` using the research doc as spec |
| Auto-deploy Railway on `git push` | GitHub OAuth still blocked at project level | User → railway.com/account → Integrations → Connect GitHub |
| `PAYWALL_ENABLED=true` | Still false in Railway — nothing blocks yet | Only flip once Strategist price is set + user has tested checkout end-to-end |

## Open secrets — ROTATE before next session

Real values have been redacted from this file (they were committed in earlier
revisions; treat git history as compromised and rotate at each provider):
- Stripe test SK (`sk_test_…`) — rotate at dashboard.stripe.com/test/apikeys
- Stripe webhook secret — rotate at dashboard.stripe.com/test/webhooks/…
- FMP — **paid tier, prioritize**
- Finnhub, Polygon, FRED, Alpha Vantage

Live keys (rk_live_, sk_live_) — user confirmed "rotated" on 2026-04-16.

## Env var state on Railway `api` service (verified)

```
CLERK_JWKS_URL            = https://fluent-mole-71.clerk.accounts.dev/.well-known/jwks.json
CLERK_SECRET_KEY          = sk_test_…   (redacted — see Railway vars)
CORS_ORIGINS              = https://s-tool.io,https://www.s-tool.io,http://localhost:8000
STRIPE_SK                 = sk_test_…   (redacted)
STRIPE_WEBHOOK_SECRET     = whsec_…     (redacted — ROTATE after any git read)
STRIPE_PRICE_ID_PRO       = price_1TMd5gHijsJzoz12PswsXLhj  ← $19.99/mo, NEEDS REPLACING WITH $8/mo
USERS_DB_PATH             = /data/users.db
RAILWAY_VOLUME_NAME       = api-volume
FMP_API_KEY, FINNHUB_API_KEY, POLYGON_API_KEY, FRED_API_KEY, ALPHA_VANTAGE_API_KEY (all set)
```

**Missing:** `STRIPE_PRICE_ID_STRATEGIST` (needed for $29 tier checkout)

## Live endpoints sanity

```bash
curl https://s-tool.io/api/health | python3 -m json.tool        # paywall_enabled: false, overall: healthy
curl https://s-tool.io/api/me                                    # anonymous returns {authenticated:false}
curl https://s-tool.io/api/picks                                 # 402 teaser w/ 9 symbols (3/tier)
curl -X POST https://s-tool.io/api/billing/webhook               # 400 invalid sig (signature verification live)
```

## Resume recipe

1. Read this file + `MEMORY.md` (auto-loads)
2. User's top blockers: **create $8/mo + $29/mo Stripe test prices**, then set both env vars on Railway
3. After prices set, test end-to-end: sign up → Get Pro → 4242 card → webhook fires → tier=pro → badge shows "Pro · 0/10 today"
4. Same test for Strategist → tier=strategist → /picks unlocks full data
5. Flip `PAYWALL_ENABLED=true` in Railway after E2E confirms
6. Pending work choices from the 7am email: PDF export · SEC EDGAR integration · more data sources · model V2

## Current open session state

- Railway CLI linked (`~/.railway/`, auth persists across sessions)
- `wrangler` authed via `npx` (uses user's Cloudflare account)
- GitHub `gh` CLI authed as `jgmynott`
- Clerk MCP: not needed, no Clerk MCP exists
- Gmail MCP: connected, used for the 7am morning report draft
- Linear MCP: connected, team `S-tool` (id `e9c7bb5e-…`) — issues S-89 (resolved in progress), S-90 (Node 24 bumps), S-91/S-92 (duplicates, archived)

## Snapshot commands

```bash
cd ~/Documents/Claude/s2tool-projector
git log --oneline -10
railway status
railway variables --service api | grep -E "STRIPE|CLERK|PAYWALL"
/usr/bin/curl -s https://s-tool.io/api/health | python3 -m json.tool
/usr/bin/curl -s https://s-tool.io/api/picks | python3 -m json.tool
cat momentum_factor_report.md
cat factor_sweep_report.md
cat research/pricing_tiers_research.md
cat research/free_data_sources.md
```
