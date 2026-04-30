# Session Handoff — 2026-04-30 (post site cull)

> **Resume with:** `Read SESSION_HANDOFF.md and the MEMORY.md auto-memory, then check live deploy status with curl https://s-tool.io/app/ -o /dev/null -w "%{http_code}\n" before doing anything UI.`

---

## 📍 Current product state (2026-04-30)

- **Live:** https://s-tool.io/app + https://s-tool.io/picks. Every other route 302-redirects to `/app/`.
- **Stack:** Cloudflare Worker (`s-tool-site`) + Railway FastAPI (`api`) + SQLite (`projector_cache.db`). **No Clerk, no Stripe** on the frontend.
- **Mobile:** intentionally unsupported. Both pages ship `<meta name="viewport" content="width=1024">`. Don't propose breakpoints, don't QC at phone widths. See `feedback_mobile_qc.md`.
- **Last commit:** `bd02b17` — `cull(site): collapse to /app + /picks; remove Clerk, Stripe, mobile`. Worker version `f8ad5623-209e-4a87-b900-6d0d37e546b5`.

## 🔥 What just shipped (2026-04-30)

The marketing site got scrapped. User: "ditch all pages except for projector and picks page — barebones bc you cant QC anything of value" + "remove stripe and clerk — they are stupid pieces".

### Deleted from `cloudflare/public/`

- Pages: `index.html` (landing), `backtest/`, `faq/`, `how/`, `pricing/`, `studio/`, `track-record/`
- Shared: `mobile.css`, `nav.css`, `nav.js`, `clerk-lazy.js`, `track-record-app.js`, `tokens.css`

### Frontend strip-down

- `app/index.html`: dropped Clerk SDK script, sign-in pill, tier badge, "Get Pro" button, paywall handler, `/api/me` polling, post-checkout success flash, `authHeaders()` injection on `/api/project`. Header collapsed to logo + date/VIX. Nav reduced to Projector | Picks.
- `picks/index.html`: same — no Clerk script, no `/shared/nav.js`, no nav-end pill, footer trimmed.
- `shared/picks-app.js`: stripped `authHeaders()`, `startCheckout()`, `STNav` integration, the 402 strategist-required gate path. `load()` runs on DOMContentLoaded with no auth headers. Cache-bust to `?v=20260430a`.

### Worker + headers

- `cloudflare/src/worker.js`: CSP no longer allows `*.clerk.accounts.dev`, `*.stripe.com`, or `*.clerk.com`. `/studio` CSP block deleted. Added a `DROPPED` set that 302s every culled route to `/app/`. `/status` page links updated.
- `cloudflare/public/_headers`: Permissions-Policy stripped of Stripe payment allowance. CSP rule deleted (worker owns CSP for HTML now).
- `cloudflare/public/sw.js`: `VERSION` bumped to `v2` so the activate handler evicts the stale `v1` cache that prefetched `/track-record/`, `/pricing/`, etc. `SHELL_PATHS` reduced to `/app/`, `/picks/`, `/shared/skeleton.css`.

### Preflight

- `check_nav_consistency` rewritten — only audits the two surviving pages.
- `check_mobile_breakpoints` removed entirely.

### Backend untouched

`api.py` still defines `/api/me`, `/api/billing/checkout`, `/api/billing/portal`, `/api/billing/resync`, `/api/_admin/*`, the Clerk JWT verifier, the Stripe webhook handler, `users_db.py`, etc. Nothing on the live frontend calls them. They're orphans pending a backend cleanup pass.

## ✅ Live verification (2026-04-30 post-deploy)

- `/app` → 200, `viewport=1024`, zero "clerk|stripe|sign in" matches
- `/picks` → 200, same
- `/`, `/pricing`, `/track-record`, `/how`, `/faq` → 302 → `/app/`
- CSP header on HTML responses contains no Clerk/Stripe domains
- `/api/picks` → 200 (anonymous traffic works without an Authorization header)
- `/shared/picks-app.js?v=20260430a` → 200

## 🚫 Do NOT

- Re-add a marketing page (`/`, `/pricing`, `/how`, `/faq`, `/track-record`) without explicit confirmation. The cull was deliberate.
- Re-introduce Clerk SDK, sign-in pills, tier badges, paywall gates, or `/api/me` / `/api/billing/*` calls on the frontend.
- Add `@media (max-width:…)` rules or do mobile QC. Site is desktop-only.
- Reintroduce `check_mobile_breakpoints` or expand `check_nav_consistency` past the two surviving pages.
- Reference "Medallion" anywhere in copy (trademark — see `feedback_no_medallion_name.md`).
- Quote SaaS/API pricing from memory — always WebFetch the provider's pricing page first.
- Publish methodology recipes (parameter values, method names, provider shopping lists) on the public site. Philosophy + stats only.

## 🎯 Open questions / backlog

- **Backend cleanup.** Should `/api/me`, `/api/billing/*`, `/api/_admin/*`, `auth.py`, `billing.py`, `users_db.py`, the Clerk JWT verifier, and the Stripe webhook handler be deleted from `api.py`? They're orphan code now. Decision pending.
- **Comp list (`_COMPED_STRATEGIST_EMAILS` in `users_db.py`).** Irrelevant once auth is removed — but still in code.
- **Stripe-side cleanup.** Subscriptions on Stripe should be canceled or migrated. Out of scope for the frontend cull.
- **Backend `/api/portfolio`.** Picks page still calls it for the live Alpaca panel. Currently public — fine since equity figures are also visible on Alpaca's own status page. If the user wants this hidden again, gate it via something other than Clerk (IP allowlist, simple shared secret, etc.).

## 📚 Reference docs

- `README.md` — top-level project description, refreshed for the cull
- `docs/deployment.md` — Railway secrets + rollback + admin endpoints (auth bits stale, marked accordingly)
- `docs/data-sources.md` — every data feed in the nightly pipeline
- `docs/honest-metrics.md` — Wave 1 + 2 backtest audit methodology
- `docs/alpaca_integration_plan.md` — paper-trading roadmap
- Memory: `feedback_nav_consistency.md`, `feedback_mobile_qc.md`, `project_stool_live_stack.md`, `feedback_validate_then_ship.md`
