# s-tool deployment — automation handoff

The site is designed to run unattended. Every weeknight at **22:00 UTC**,
`.github/workflows/nightly-pipeline.yml` executes the full refresh:

1. Smoke-test external data providers
2. Refresh projections for the Russell 3000 (`worker.py --all`)
3. Market-cap + volume enrichment via yfinance
4. FMP profile enrichment (250/day rate limit, grows cache over time)
5. SEC EDGAR XBRL fundamentals refresh
6. Upside research sweep (`upside_hunt.py`)
7. Nightly NN training (`overnight_learn.py` — asymmetric + confidence)
8. Today's asymmetric scoring (`enrich_asymmetric.py`)
9. Final portfolio scan → `portfolio_picks.json`
10. **Preflight** — blocks deploy on data-quality failures
11. Commit cache + NN artifacts back to `main`
12. Deploy Cloudflare (frontend) + Railway (backend)

Total runtime: 30–60 min depending on how much is already fresh.

## Required GitHub secrets

Settings → Secrets and variables → Actions → New repository secret.

| Secret | Where to get it | Notes |
|---|---|---|
| `FMP_API_KEY` | financialmodelingprep.com | Paid tier — rotate if exposed |
| `FINNHUB_API_KEY` | finnhub.io | Free tier OK |
| `POLYGON_API_KEY` | polygon.io | Free tier OK |
| `FRED_API_KEY` | fredaccount.stlouisfed.org | Free |
| `ALPHA_VANTAGE_API_KEY` | alphavantage.co | Free, low rate limit |
| `RAILWAY_TOKEN` | railway.com/account/tokens → "Create Token" | **Account-level** (not project-scoped) |
| `CLOUDFLARE_API_TOKEN` | dash.cloudflare.com/profile/api-tokens → "Create Token" → use "Edit Cloudflare Workers" template | Minimum scope |

`GITHUB_TOKEN` is auto-provided; no action needed.

## Manual trigger

From GitHub UI: Actions → nightly-pipeline → "Run workflow" → main.

From CLI: `gh workflow run nightly-pipeline.yml --ref main`

## Health check after a run

```
curl -s https://s-tool.io/api/health | jq '.overall,.paywall_enabled'
curl -s https://s-tool.io/api/picks | jq '.teaser[0:3]'
```

If picks teaser doesn't refresh after 22:00 UTC, check the workflow
logs. Most common failure: FMP rate limit exceeded — non-fatal (the
pipeline continues with stale FMP data) but preflight may warn.

## What's NOT automated yet

- **User DB backup** to Railway Volume snapshot. The `/data/users.db` is
  currently only protected by Railway's own volume durability.
- **Key rotation** — secrets in GitHub have to be rotated manually when
  the provider keys change. Consider adding a reminder calendar event.
- **Anomaly alerting** — if the NN's winning scorer changes abruptly
  between nights, we don't currently get paged. Future work.

## Rolling back

If a nightly deploy ships something broken:

1. Revert the offending commit on `main` (GitHub UI or `git revert`).
2. Re-run `nightly-pipeline.yml` manually — it'll redeploy the reverted
   state.
3. For immediate rollback without waiting: `railway rollback --service api`
   from a machine with the `RAILWAY_TOKEN` env var.
