# Deployment & operations — s-tool

Last refreshed: **2026-04-17**

The site runs unattended. GitHub Actions fires two scheduled workflows on weekdays:

| Workflow | Schedule | Budget | What it does |
|---|---|---|---|
| `daily-refresh-fast.yml` | `0 20 * * 1-5` UTC | 45 min | Preferred-universe scan (~537 tickers), enrichment, picks, deploy |
| `daily-refresh-slow.yml` | `0 23 * * 1-5` UTC | 150 min | Short interest + Finnhub feeds, upside_hunt, NN training, backtest, re-deploy |

Both paths deploy Cloudflare (frontend) and Railway (backend) on completion.

## Required GitHub secrets

Settings → Secrets and variables → Actions → New repository secret.

| Secret | Where to get it | Notes |
|---|---|---|
| `FMP_API_KEY` | financialmodelingprep.com | Paid tier |
| `FINNHUB_API_KEY` | finnhub.io | Free tier — 60 req/min |
| `POLYGON_API_KEY` | polygon.io | Free tier — 5 req/min |
| `FRED_API_KEY` | fredaccount.stlouisfed.org | Free |
| `ALPHA_VANTAGE_API_KEY` | alphavantage.co | Free, tight rate limit |
| `RAILWAY_TOKEN` | **project** settings → Tokens (not the account token page) | see below |
| `CLOUDFLARE_API_TOKEN` | dash.cloudflare.com/profile/api-tokens → "Edit Cloudflare Workers" template | Minimum scope |

### Railway token: project vs account

There are two token types with different env-var names. The workflow reads `RAILWAY_TOKEN`, which is the **project-token** shape.

| Type | Env var | Create at |
|---|---|---|
| **Project token** | `RAILWAY_TOKEN` | `railway.com/project/<id>/settings/tokens` ← use this |
| Account / workspace token | `RAILWAY_API_TOKEN` | `railway.com/account/tokens` |

An account token placed in `RAILWAY_TOKEN` fails with "Invalid RAILWAY_TOKEN" — looks like token expiry but is wrong-shape rejection. Always create tokens from the project's Tokens page.

### Railway CLI installation in CI

The workflow uses `npm install -g @railway/cli` instead of the official `install.sh` because the install script hits GitHub's unauthenticated releases API which rate-limits on shared CI runners. npm has no such limit.

## Railway Volume (users DB persistence)

The API service mounts a Volume at `/data` so `users.db` survives every redeploy. Without this, every CI deploy wipes paying users' tier to `'free'`.

Dashboard setup (one-time):

1. Railway project → `api` service → **Settings** → **Volumes** → Create Volume, mount path `/data`, 1 GB
2. Same service → **Variables** → add `USERS_DB_PATH=/data/users.db`
3. Redeploy

Verify from logs on next deploy:

```
INFO:api:users_db path: /data/users.db
INFO:api:users_db row count on boot: N
```

## Data-file shipping (gitignore gotcha)

`data_cache/` has large subdirectories (prices/, sec_edgar/facts/) that must never ship to Railway. Runtime JSONs (backtest report, honest audit, feature importance) also live under `data_cache/` and DO need to ship.

The `.gitignore` excludes only the heavy subdirs explicitly:

```
data_cache/prices/
data_cache/sec_edgar/facts/
data_cache/public_pulse/
```

Everything else under `data_cache/` — the small JSON artifacts + profiles — uploads with `railway up`. Secondary runtime artifacts that need to ship reliably use `runtime_data/` (has never been gitignored, guaranteed to upload regardless of what `data_cache/` rules do).

Rule of thumb: if the API reads it on every request, put it in `runtime_data/`.

## Manual triggers

```bash
# Fire a fast run immediately (20:00 UTC scheduled equivalent)
gh workflow run daily-refresh-fast.yml --ref main

# Fire a slow run immediately
gh workflow run daily-refresh-slow.yml --ref main

# Watch a specific run
gh run view <run-id> --repo jgmynott/s-tool-projector --log
```

## Health checks after a deploy

```bash
curl -s https://s-tool.io/api/health | jq '.overall'
curl -s https://s-tool.io/api/backtest-report | jq '.honest_metrics.year_oos_hit_100'
curl -s https://s-tool.io/api/honest-audit | jq '.scorers.nn_score.overall.E_all_three'
curl -s https://s-tool.io/api/me -H "Authorization: Bearer <token>" | jq '.tier'
```

## Admin endpoints (internal)

Gated behind `ADMIN_TOKEN` env var on the Railway `api` service:

| Endpoint | What |
|---|---|
| `GET /api/_admin/users` | list every user (no pagination — table is small) |
| `GET /api/_admin/user/{clerk_user_id}` | dump a specific user's row |
| `POST /api/_admin/force_resync/{clerk_user_id}` | re-pull subscription from Stripe |
| `POST /api/_admin/backfill_emails` | fill `email IS NULL` rows from Clerk Backend API |

All require header `x-admin-token: <value matching ADMIN_TOKEN>`.

## Comping a user (no Stripe subscription)

Hard-coded comped-Strategist list lives in `users_db.py`:

```python
_COMPED_STRATEGIST_EMAILS = {
    "kevinrvandelden@gmail.com",
}
```

Adding an email here → `_is_owner()` returns True → `effective_tier()` returns `"strategist"`, regardless of what Stripe says. Edit, commit, push. Ships via nightly pipeline.

## What's NOT automated

- **Users DB backup** — rely on Railway Volume durability for now
- **Key rotation** — GitHub secret rotation is manual when provider keys change
- **Anomaly alerting** — no pager for abrupt NN scorer changes night-over-night

## Rolling back

```bash
# Revert the commit
git revert <sha> && git push origin main

# Force an immediate deploy of the reverted state
gh workflow run daily-refresh-slow.yml --ref main

# Or roll back Railway to the previous image
RAILWAY_TOKEN=<project-token> railway redeploy --service api
```

## Pipeline history & known gotchas

- **FINRA short interest** — their public consolidatedShortInterest API silently ignores `compareFilters=settlementDate` and returns 2020 data regardless. We switched to yfinance's shortPercentOfFloat.
- **Fast-path timeout** — `worker.py --all` on the full Russell 3000 routinely blew 90-min budgets. Fast path now uses `--preferred` (SP500 ∪ NDX100 ∪ WSB, ~537 tickers). Slow path handles the long tail when there's time.
- **Long-tail refresh in slow path** — briefly added then removed after two slow runs timed out before reaching the deploy steps. If we ever need the Russell 3000 long tail back, it belongs in a weekly workflow, not nightly.
