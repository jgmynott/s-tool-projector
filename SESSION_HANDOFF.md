# Session Handoff — 2026-04-17 (second handoff, post-redesign)

> Resume with: "Read SESSION_HANDOFF.md and MEMORY.md then tell me where we left off."

Previous handoff (now superseded): the session before this one finished, documented its own mistakes, and exited. This session picked up the P0 item from that handoff — the `/picks` redesign — and shipped it.

---

## ✅ What got done this session

**Commit:** `f231cc2` — `redesign(picks): in-page tabs + numeric donut allocator, drop Wild/sleeve`

Single-file change: `cloudflare/public/picks/index.html` (+404 / -197 lines).

### UI changes on `/picks`

1. **In-page tab nav.** Four pills (Conservative / Moderate / Aggressive / Asymmetric) nested in the container below the allocator. Not sticky — never conflicts with top site nav. Each tab shows `[color dot] Name` on row one and `N picks · $X,XXX` on row two, so the nav carries its own numbers. Active-tab state persists to `localStorage.stool_picks_tab`.
2. **Numeric donut allocator.** Replaces the horizontal-bar allocation viz. SVG donut (4 segments, rotated so 12 o'clock is the start) with each arc labelled by its own percentage (slices ≥ 8% only, to avoid unreadable labels). Center shows `$total · PROFILE NAME`. Right side is a composition table where every row is `[dot] TierName | sub-description | N picks available | PCT% | $DOLLARS` — every element is numeric.
3. **Dropped the Wild risk profile.** User flagged it as "aligns to nothing." Legacy `localStorage.stool_alloc_profile === 'wild'` auto-migrates to `'aggressive'`.
4. **Stripped "sleeve" copy everywhere.** Asymmetric tab blurb now reads "High-variance — size each name small." Profile subs use "allocation" instead of "sleeve." `TIER_COPY.aggressive.blurb` says "Tactical set — size small per name."
5. **Top-5 per tab with Show-remaining toggle.** Each tab renders 5 cards then a pill button "Show remaining N picks →". Overflow cards are in the DOM with `data-overflow="1"`, hidden via CSS (`.tab-panel:not(.show-all) [data-overflow] { display: none; }`) — no JS re-render.
6. **Deleted dead functions.** Old `renderTierSection` + `renderAsymmetricTier` + `collapsible` / `toggleCollapsible` / `setAllCollapsed` are gone. Old CSS for `.collapsible`, `.collapse-btn`, `.collapse-master`, `.alloc-bar`, `.alloc-legend`, `div-bar` removed.

### What was verified locally

Used a preview harness: copied `index.html` to `/tmp/picks-preview/`, injected a `fetch` mock that reads `portfolio_picks.json` for `/api/picks`, mocked Clerk + STNav, served via `python3 -m http.server 8899`, screenshotted with headless Chrome.

- ✅ Desktop 1800×1600: donut renders, 4 segments in correct proportions, center `$10,000 BALANCED`, composition table shows Conservative 40% $4,000 / Moderate 35% $3,500 / Aggressive 20% $2,000 / Asymmetric 5% $500, tabs show correct counts + dollars.
- ✅ Mobile 390×3800: donut stacks above table, controls drop to 2-col row, tabs wrap to 2×2 grid, pick cards render single-column. No overlaps.
- ✅ Donut labels align with segments after I fixed the 3-o'clock-vs-12-o'clock mismatch (added `transform="rotate(-90 21 21)"` on segment circles).

### What is NOT yet verified

- **Live site behind Clerk auth.** I could only hit the gate path from the local preview. The full logged-in Strategist render has only been tested against local mock JSON, not the Railway API response. The JSON shapes match (preflight confirms all expected fields present) so this should work, but eyeball verification is still owed.
- **Deploy has not run.** Nightly pipeline (run `24563292177`) was on step 7/22 when I committed. It won't pick up `f231cc2` since it checked out an earlier SHA. For live changes you need either (a) manual `cd cloudflare && wrangler deploy`, or (b) the next nightly cycle at 22:00 UTC.
- **Race caveat with pipeline:** pipeline's own commit step (step 18) will land data-file updates only (`portfolio_picks.json`, `backtest_report.json`, CSVs); HTML files aren't in that commit, so there's no merge conflict with `f231cc2`. Safe to push anytime.

---

## 🎯 Current product state

### Live on `s-tool.io` as of 2026-04-17 ~08:30 ET

| Layer | Version | Notes |
|---|---|---|
| Cloudflare Worker `s-tool-site` | **b0e467c7** (prior session) | `/picks` still showing the pre-redesign collapsibles. Deploy `f231cc2` to see the new version. |
| Railway API | unchanged from prior session | Still serves `backtest_report.json` without `honest_metrics` until the running pipeline finishes. |
| `main` branch head | **f231cc2** (local, unpushed at handoff write time — check `git log origin/main..HEAD`) | `/picks` redesign |
| Pipeline run `24563292177` | in progress (step 7/22 as of 08:30) | Will commit fresh picks + run NN training + deploy to Railway + Cloudflare. Cloudflare deploy uses the pipeline's checked-out SHA, NOT `f231cc2`. |

### Data state
- `portfolio_picks.json` — 30 picks + 10 asymmetric, today's scan from 00:15 UTC. Pipeline will overwrite with fresh scan when it finishes.
- `backtest_report.json` — still pre-`honest_metrics` until pipeline completes.

---

## 🔨 What the next session should do

### Immediate (if the redesign isn't already live)
1. Check `git log origin/main..HEAD` — if `f231cc2` is unpushed, push it.
2. `cd cloudflare && wrangler deploy` to push the new `/picks` to prod.
3. Take mobile + desktop screenshots of the live page logged in as Strategist. Confirm:
   - Four tab pills render with correct counts and dollars for the selected risk profile
   - Donut segments are labelled with percentages, center shows `$10,000 BALANCED`
   - Switching tabs hides/shows the right panels
   - "Show remaining N picks" button reveals hidden picks when clicked
   - Changing risk profile updates the donut + table live
4. Report back to user with screenshots. Per user's standing guidance: do NOT claim done before the user has verified via screenshot.

### Follow-up polish candidates (user did not ask for these — confirm before doing)
- **Session-length tab persistence is localStorage only**, so across devices the tab resets. If user wants cross-device, push to user prefs API.
- **Donut slices < 8% have no label.** Only asymmetric at 5% in the Balanced profile hits this. An on-hover tooltip or a small outside-arc label could handle it, but the composition table already shows the number.
- **Tab overflow on narrow screens.** At 390px tabs wrap to 2×2; below 420px they remain 2×2 (grid-template-columns: 1fr 1fr). Test at 320px (iPhone SE) — might need a horizontal scroll treatment if a 2-col 4-row ends up ugly.
- **Portfolio signal block duplicates info.** "Top conviction", "biggest projected upside", "best expected return" each name a ticker. If you want, make each a tappable chip that jumps to the right tab + scrolls the card into view.

### Still pending from prior handoff (not touched this session)
- P1: Verify `/track-record` renders `honest_metrics` after pipeline completes
- P2: Alpaca integration Phase 0 (split-adjustment bug, survivorship, OOS holdout, tx-cost model)
- P3: Regime classifier NN, confidence NN rescale 0-48 → 0-100, downside NN, PDF export, weekly email, Polygon backfill, users DB backup, rotate exposed keys, CSP `unsafe-inline` removal, SQL f-string tighten in `signals_sec_edgar.py`, rate limits on `/api/sentiment` + `/api/cached`

---

## 📁 Files touched this session

**Committed (`f231cc2`):**
- `cloudflare/public/picks/index.html` — the redesign

**Untracked (ignore / clean up if desired):**
- `.wrangler/`, `cloudflare/.wrangler/` — local wrangler state
- `upside_hunt_extended.csv` — leftover from a prior run
- `/tmp/picks-preview/` — local preview harness (delete any time)
- `/tmp/picks-shots/` — screenshots (delete any time)

---

## 🚨 Standing rules (reinforced, did not break this session)

1. Never overwrite `portfolio_picks.json` from a local script without a `len(scored) > 100` guard — the prior session's burn is documented in memory.
2. Don't claim "mobile fix done" without user-verified screenshots. This session's screenshots are from a local fetch-mock; the live Strategist view is **not yet verified**.
3. No jargon — "sleeve", "tactical tilt", "WILD" are banned. This session removed all of those.
4. Every visual must carry numbers. The new donut + composition table is compliant; the tab pills are compliant; the portfolio signal text is compliant. If you add anything new, verify.
5. `python3 preflight.py` before every deploy. Preflight passed at commit time with 3 pre-existing warnings (market_cap coverage, sector coverage, users DB 403 — all inherited).

---

## Current git state

```
main @ f231cc2 — redesign(picks): in-page tabs + numeric donut allocator, drop Wild/sleeve
  (check push status with: git log origin/main..HEAD)
nn-research @ aa71e02 (pushed, unchanged this session)
```

Pipeline: `gh run view 24563292177 --repo jgmynott/s-tool-projector`
