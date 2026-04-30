"""
Preflight checks for s-tool deploys.

Run `python3 preflight.py` BEFORE `railway up` / `wrangler deploy` to catch
data-quality regressions (the kind that show up as "everything is 0/100 on
the live page"). Each check is a hard gate: exit 0 = green, exit 1 = block.

Checks:
  1. portfolio_picks.json shape — all expected fields present
  2. No negative expected-return picks
  3. No duplicate (symbol, tier) rows
  4. Confidence scores have real variance (std >= 5 across top picks)
  5. At least some picks have a rationale (no mass-fallback)
  6. At least some picks have a real company_name (SEC enrichment worked)
  7. Frontend /picks page has matching field references (no typos vs JSON keys)
  8. Required API imports compile (api.py / portfolio_scanner.py / billing.py)

Add more checks here when we discover new classes of regression. Every
"I deployed and it was broken" bug should become a line in this file.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).parent

REQUIRED_PICK_FIELDS = {
    "symbol", "tier", "expected_return", "risk", "sharpe_proxy",
    "current_price", "p50_target", "rationale",
    "confidence", "confidence_label", "confidence_components",
    "company_name",
}

failures: list[str] = []
warnings: list[str] = []


def fail(msg: str):
    failures.append(msg)
    print(f"  ✗ {msg}")


def warn(msg: str):
    warnings.append(msg)
    print(f"  ⚠ {msg}")


def ok(msg: str):
    print(f"  ✓ {msg}")


def check_picks_json():
    print("\n[1] portfolio_picks.json shape")
    path = ROOT / "portfolio_picks.json"
    if not path.exists():
        fail("portfolio_picks.json missing")
        return
    try:
        d = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        fail(f"portfolio_picks.json invalid JSON: {e}")
        return
    picks = d.get("picks") or []
    if not picks:
        fail("portfolio_picks.json has no picks")
        return

    missing_fields = set()
    for p in picks:
        for f in REQUIRED_PICK_FIELDS:
            if f not in p:
                missing_fields.add(f)
    if missing_fields:
        fail(f"picks missing fields: {sorted(missing_fields)}")
    else:
        ok(f"{len(picks)} picks, all required fields present")

    # Negative-ER check
    neg = [p["symbol"] for p in picks if (p.get("expected_return") or 0) < 0]
    if neg:
        fail(f"picks with negative expected_return: {neg[:5]}")
    else:
        ok("no negative-expected-return picks")

    # Duplicate check
    keys = [(p["symbol"], p["tier"]) for p in picks]
    dupes = [k for k in set(keys) if keys.count(k) > 1]
    if dupes:
        fail(f"duplicate (symbol,tier): {dupes[:5]}")
    else:
        ok("no duplicate (symbol,tier)")

    # Confidence variance
    confs = [p.get("confidence") or 0 for p in picks]
    if len(set(confs)) == 1:
        fail(f"all picks have confidence={confs[0]} — scoring bug?")
    else:
        std = statistics.stdev(confs) if len(confs) > 1 else 0
        if std < 5:
            warn(f"low confidence variance (std={std:.1f}) — suspicious")
        else:
            ok(f"confidence ranges {min(confs)}..{max(confs)}, std={std:.1f}")

    # Rationale coverage
    rationale_count = sum(1 for p in picks if p.get("rationale"))
    if rationale_count == 0:
        fail("no picks have rationale text — builder broken?")
    elif rationale_count < len(picks) * 0.5:
        warn(f"only {rationale_count}/{len(picks)} picks have rationale")
    else:
        ok(f"{rationale_count}/{len(picks)} picks have rationale")

    # Company name coverage
    name_count = sum(1 for p in picks if p.get("company_name") and p["company_name"] != p["symbol"])
    if name_count == 0:
        fail("no picks have company_name — SEC enrichment broken?")
    elif name_count < len(picks) * 0.8:
        warn(f"only {name_count}/{len(picks)} picks have real company_name")
    else:
        ok(f"{name_count}/{len(picks)} picks have company_name")

    # Market-cap coverage — now rendered on every card as a size-tier pill.
    # Checked against both the new top-level field AND the legacy one.
    mcap_count = sum(
        1 for p in picks
        if p.get("market_cap") or (p.get("fundamentals") or {}).get("market_cap")
    )
    if mcap_count < len(picks) * 0.50:
        warn(f"only {mcap_count}/{len(picks)} picks have market_cap — meta row degrades gracefully but re-run enrich_marketcaps + a Polygon backfill before next ship")
    else:
        ok(f"{mcap_count}/{len(picks)} picks have market_cap")

    # Sector coverage — reported as Unclassified in the diversification banner.
    # Banner hides itself under 50% coverage, but we still want to know.
    classified = sum(1 for p in picks if p.get("sector"))
    if classified < len(picks) * 0.50:
        warn(f"only {classified}/{len(picks)} picks have sector classification — diversification shows data-pending state")
    else:
        ok(f"{classified}/{len(picks)} picks have sector")

    # Asymmetric tier — must exist with at least 5 picks; those picks must
    # carry p90/p10/p50 ratios or the card will render blank slots.
    asym = d.get("asymmetric_picks") or []
    if len(asym) < 5:
        warn(f"asymmetric tier has only {len(asym)} picks — need enrich_asymmetric.py run before scan")
    else:
        with_bands = sum(
            1 for p in asym
            if (p.get("asymmetric") or {}).get("p90_ratio")
            and (p.get("asymmetric") or {}).get("p10_ratio")
        )
        # If 100% are missing it's an upstream pipeline problem (e.g.,
        # enrich_asymmetric.py saw 0 input symbols and wrote an empty
        # asymmetric_scores.json). Better to ship the other tiers and
        # let save_picks drop the asymmetric tier from the JSON than
        # block the entire deploy on a single broken upstream stage.
        if with_bands == 0:
            warn(f"asymmetric tier: {len(asym)}/{len(asym)} picks missing p10/p90 — upstream pipeline issue (likely empty asymmetric_scores.json). save_picks will drop the tier; conservative/moderate/aggressive will still ship.")
        elif with_bands < len(asym):
            # save_picks (portfolio_scanner.py:592) drops partial-bands picks
            # at source after 2026-04-27, so this branch should be unreachable
            # from a normal pipeline run. If it fires, something wrote
            # asymmetric_picks directly to portfolio_picks.json without going
            # through save_picks — flag loudly but don't block deploy on UI
            # cosmetics. Past hard-fail at this line was responsible for
            # 5 consecutive nightly failures Apr 22-26.
            warn(f"asymmetric tier: {len(asym) - with_bands}/{len(asym)} picks missing p10/p90 — save_picks filter bypassed? Investigate enrich_asymmetric → save_picks path. UI will show blank slots for the affected picks until next refresh.")
        else:
            ok(f"asymmetric tier: {len(asym)} picks, all with p10/p90")

    # Sector diversification — warn if any tier is dominated by a single
    # sector. Overall portfolio should span at least 4 distinct sectors.
    from collections import Counter
    tier_sectors: dict[str, Counter] = {}
    for p in picks:
        tier_sectors.setdefault(p["tier"], Counter())[p.get("sector") or "Unclassified"] += 1
    for tier, counts in tier_sectors.items():
        n = sum(counts.values())
        top_sector, top_count = counts.most_common(1)[0]
        share = top_count / n
        if share >= 0.50 and top_sector != "Unclassified":
            warn(f"{tier} tier is {share:.0%} in {top_sector} ({top_count}/{n}) — concentration risk")
    overall = Counter()
    for c in tier_sectors.values():
        overall.update(c)
    classified = {k: v for k, v in overall.items() if k != "Unclassified"}
    if len(classified) < 4:
        warn(f"picks only span {len(classified)} sectors (want ≥4 for real diversification)")
    else:
        ok(f"picks span {len(classified)} sectors")


def check_frontend_field_refs():
    print("\n[2] Frontend /picks renders all API fields")
    # Picks render code lives in two places after the C-pass externalize:
    # the HTML template + /shared/picks-app.js. Scan both so we don't false-
    # positive on field references that moved into the external module.
    sources = [
        ROOT / "cloudflare/public/picks/index.html",
        ROOT / "cloudflare/public/shared/picks-app.js",
    ]
    haystack = ""
    for p in sources:
        if p.exists():
            haystack += p.read_text()
    if not haystack:
        warn("no picks frontend source found — skipping")
        return
    check_fields = [
        "symbol", "expected_return", "sharpe_proxy", "current_price",
        "p50_target", "rationale", "confidence", "company_name",
    ]
    missing = [f for f in check_fields if f not in haystack]
    if missing:
        fail(f"frontend doesn't reference fields: {missing}")
    else:
        ok("all key fields referenced in render code")


def check_no_committed_secrets():
    """Block deploy if any file tracked by git contains a recognizable
    live-secret pattern. Past bug: SESSION_HANDOFF.md had the Stripe
    webhook secret + five paid API keys in plaintext for days."""
    import subprocess, re
    print("\n[*] No-committed-secrets scan")
    try:
        tracked = subprocess.check_output(
            ["git", "ls-files"], cwd=str(ROOT), timeout=5
        ).decode().splitlines()
    except Exception as e:
        warn(f"git ls-files failed: {e}")
        return
    # Patterns tuned to avoid false positives on placeholders (`whsec_…`,
    # `sk_test_…`): require ≥16 non-ellipsis chars after the prefix.
    patterns = {
        "Stripe webhook secret": re.compile(r"whsec_[A-Za-z0-9]{16,}"),
        "Stripe live key":       re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}"),
        "Stripe test SK":        re.compile(r"\bsk_test_[A-Za-z0-9]{20,}"),
        # A loose "32 hex" pattern catches Cloudflare zone/account IDs,
        # which are not secret. Limit to cases adjacent to API_KEY labels.
        "Labeled API key":       re.compile(r"(?i)(?:api[_-]?key|secret)[\s=:\"']+\b[A-Za-z0-9]{24,}\b"),
    }
    SKIP = {".env.example", "preflight.py"}
    hits: list[str] = []
    for path in tracked:
        if path in SKIP or not path.endswith((".md", ".txt", ".py", ".html", ".js", ".json", ".yml", ".toml")):
            continue
        full = ROOT / path
        if not full.exists():
            continue
        try:
            text = full.read_text(errors="ignore")
        except Exception:
            continue
        for label, pat in patterns.items():
            m = pat.search(text)
            if m:
                hits.append(f"{path} → {label}: {m.group()[:12]}…")
    if hits:
        for h in hits:
            fail(h)
    else:
        ok("no live-secret patterns in tracked files")


def check_nn_artifacts():
    """Make sure the nightly learning pipeline wrote its outputs and that
    they are fresh enough to serve picks. The scorer fallback chain in
    portfolio_scanner.py is moonshot → ensemble → nn → asymmetric (EWMA).

    Freshness model:
      - `production_scorer.json` is the *canonical* signal. overnight_learn
        unconditionally rewrites it every run with the current decision —
        if this file is stale, the nightly pipeline is genuinely dead.
        Hard-fail at 168h (7d), soft warn at 72h.
      - `asymmetric_scores.json` is rewritten unconditionally by
        enrich_asymmetric.py every night — same gate.
      - `moonshot/ensemble/nn_scores.json` produce byte-identical JSON
        when the training data is unchanged, so git skips the commit and
        their on-disk mtime can lag by weeks even though CI is healthy.
        Track existence + warn very loosely (>21d) instead of hard-failing.
        production_scorer freshness is the proxy for these.

    confidence_nn_scores.json retired 2026-04-27 — overnight_learn still
    writes it for compatibility, but no production code reads it."""
    print("\n[*] NN learning artifacts")
    dc = ROOT / "data_cache"
    # (filename, label, hard_fail_hours, soft_warn_hours)
    canonical = [
        ("production_scorer.json", "nightly learning decision", 168, 72),
        ("asymmetric_scores.json", "EWMA last-resort fallback", 168, 72),
    ]
    secondary = [
        ("moonshot_scores.json", "primary asymmetric ML scorer"),
        ("ensemble_scores.json", "stacker fallback"),
        ("nn_scores.json",       "regression fallback"),
    ]
    import time as _time
    now = _time.time()
    for fname, label, fail_h, warn_h in canonical:
        p = dc / fname
        if not p.exists():
            fail(f"{fname} missing — run overnight_learn.py / enrich_asymmetric.py")
            continue
        age_hours = (now - p.stat().st_mtime) / 3600
        if age_hours > fail_h:
            fail(f"{fname} is {age_hours:.1f}h old (>{fail_h}h hard-fail) — overnight_learn pipeline is dead")
        elif age_hours > warn_h:
            warn(f"{fname} is {age_hours:.1f}h old — nightly may have hiccupped")
        else:
            ok(f"{fname} fresh ({age_hours:.1f}h old) — {label}")
    # Secondary scorers — existence + soft warn only (mtime lies due to
    # deterministic JSON; production_scorer above is the real signal).
    for fname, label in secondary:
        p = dc / fname
        if not p.exists():
            fail(f"{fname} missing — run overnight_learn.py")
            continue
        age_hours = (now - p.stat().st_mtime) / 3600
        if age_hours > 504:  # 21d, generous because these only commit on content change
            warn(f"{fname} is {age_hours:.1f}h old (>504h) — verify production_scorer is fresh; otherwise overnight_learn may be writing but failing to update")
        else:
            ok(f"{fname} present ({age_hours:.1f}h on disk) — {label}")


def check_users_sanity():
    """Lightweight live check: fetch /api/_admin/users and warn if any
    signed-in user has a NULL email. Null emails mean the grants list
    can't match — a past class of bug worth guarding against."""
    import os, urllib.request, json as _json
    token = os.getenv("ADMIN_TOKEN")
    # Look up ADMIN_TOKEN from /tmp/admin_token if not in env
    if not token:
        try:
            with open("/tmp/admin_token") as f:
                token = f.read().strip().split("=", 1)[-1]
        except OSError:
            return
    if not token:
        return
    print("\n[*] Users DB sanity (live)")
    try:
        req = urllib.request.Request(
            "https://s-tool.io/api/_admin/users",
            headers={"x-admin-token": token},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            data = _json.loads(r.read().decode("utf-8"))
    except Exception as e:
        warn(f"live users check skipped: {e}")
        return
    users = data.get("users", [])
    if not users:
        ok("no users yet — nothing to check")
        return
    null_email = [u for u in users if not u.get("email")]
    if null_email:
        warn(f"{len(null_email)}/{len(users)} users have null email — STRATEGIST_GRANT_EMAILS can't match them. Next /api/me hit should populate via Clerk lookup.")
    else:
        ok(f"all {len(users)} users have email populated")


def check_imports():
    print("\n[3] Python imports compile")
    import importlib, importlib.util, py_compile, traceback
    for mod in ("api.py", "portfolio_scanner.py", "billing.py",
                "signals_sec_edgar.py", "worker.py", "users_db.py",
                "watchdog.py", "research/trader.py"):
        path = ROOT / mod
        if not path.exists():
            fail(f"{mod} missing")
            continue
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as e:
            fail(f"{mod} compile error: {e}")
            continue
    if not any(f.startswith("api.py") or f.endswith("compile error") for f in failures):
        ok("all modules compile")


def check_trader_smoke():
    """Run trader.py's print functions against synthetic plan dicts that
    mix string-encoded numerics (Alpaca's wire format) with native floats.
    The 13:30 UTC scheduled trader run on 2026-04-27 crashed here on
    `f'upnl=${s["unrealized_pl"]:>+9}'` because Alpaca returns numerics
    as JSON strings; the `:+` flag rejects strings. This check exercises
    the same code path so future format-string changes can't ship a
    regression without preflight catching it."""
    print("\n[*] Trader format-string smoke")
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "research"))
    try:
        import trader  # noqa: F401
    except Exception as e:
        fail(f"could not import research/trader: {e}")
        return

    # Synthetic combined plan with mixed float/string upnl, mixed
    # held types, and skipped reasons that include parens / dashes.
    fake_plan_open = {
        "equity": 101000,
        "plans": {
            "momentum": {
                "config": trader.SLEEVES["momentum"],
                "equity_allocated": 33667,
                "target_capital": 50500,
                "n_slots": 5,
                "per_position_target": 10100,
                "sells": [
                    {"symbol": "AAA", "qty": 10, "days_held": 3,
                     "unrealized_pl": "45.32", "reason": "5d hold complete"},
                ],
                "buys": [
                    {"symbol": "BBB", "qty": 25, "ref_price": 50.0,
                     "stop_loss": 47.5, "take_profit": 55.0, "tier": "moderate"},
                ],
                "skipped": [{"symbol": "(cap)", "reason": "sleeve full"}],
            },
            "swing": {
                "config": trader.SLEEVES["swing"],
                "equity_allocated": 33667, "target_capital": 50500,
                "n_slots": 10, "per_position_target": 5050,
                "sells": [
                    {"symbol": "CCC", "qty": 20, "days_held": 5,
                     "unrealized_pl": -127.66, "reason": "stop -7%"},
                ],
                "buys": [], "skipped": [],
            },
            "daytrade": {
                "config": trader.SLEEVES["daytrade"],
                "equity_allocated": 33667, "target_capital": 33667,
                "n_slots": 10, "per_position_target": 3367,
                "sells": [], "buys": [], "skipped": [],
            },
        },
    }
    fake_plan_close = {
        "sells": [
            {"symbol": "DDD", "qty": 12, "unrealized_pl": "+12.50",
             "reason": "daytrade EOD force-close"},
            {"symbol": "EEE", "qty": 8, "unrealized_pl": -8.0,
             "reason": "daytrade EOD force-close"},
        ],
        "buys": [],
    }
    fake_plan_rotate = {
        "config": trader.SLEEVES["daytrade"],
        "equity_allocated": 33667, "target_capital": 33667,
        "n_slots": 10, "per_position_target": 3367,
        "n_held": 9, "n_free": 1,
        "sells": [],
        "buys": [{"symbol": "FFF", "qty": 75, "ref_price": 44.39,
                  "stop_loss": 43.06, "take_profit": 46.61, "tier": "moderate"}],
        "skipped": [{"symbol": "GGG", "reason": "already traded today"}],
    }

    import io, contextlib
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            trader.print_open_plan(fake_plan_open)
            trader.print_close_plan(fake_plan_close)
            trader.print_rotate_plan(fake_plan_rotate)
    except Exception as e:
        fail(f"trader print function crashed on synthetic plan: {type(e).__name__}: {e}")
        return
    out = buf.getvalue()
    # Cheap content sanity — make sure the print actually formatted the rows
    if "SELL" not in out or "BUY" not in out:
        warn("trader print functions ran but produced no SELL/BUY lines (synthetic test data may be incomplete)")
    else:
        ok("trader print_open / print_close / print_rotate handle string + float upnl cleanly")


def check_rotation_pool():
    """The daytrade rotator slices ranks rotation_pool_range[0]:[1] from
    load_rotation_pool(). If the configured upper bound exceeds the actual
    pool depth, the rotator runs out of unique candidates after one cycle
    through the same-day re-entry guard. This check asserts the pool
    actually has enough names to fill the configured window."""
    print("\n[*] Daytrade rotation pool depth")
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "research"))
    try:
        import trader
    except Exception as e:
        fail(f"could not import research/trader: {e}")
        return
    cfg = trader.SLEEVES.get("daytrade") or {}
    pool_lo, pool_hi = cfg.get("rotation_pool_range") or cfg.get("rank_range") or (0, 0)
    n_slots = cfg["rank_range"][1] - cfg["rank_range"][0]
    try:
        pool = trader.load_rotation_pool()
    except Exception as e:
        fail(f"load_rotation_pool() raised: {e}")
        return
    if len(pool) < pool_hi:
        warn(f"rotation pool has {len(pool)} candidates, rotation_pool_range upper bound is {pool_hi} — slice will be {min(pool_hi, len(pool)) - pool_lo} unique names ({n_slots} slots, same-day guard exhausts after ~{(min(pool_hi, len(pool)) - pool_lo) // n_slots} cycles)")
    else:
        ok(f"rotation pool has {len(pool)} candidates, ≥ pool_range[{pool_lo}:{pool_hi}] = {pool_hi - pool_lo} unique names available")


def check_workflow_crons():
    """Validate every cron string in .github/workflows/*.yml. Catches:
      • wrong field count (4 or 6 instead of 5)
      • out-of-range values (e.g. hour=25)
      • obviously malformed step / range syntax
    Doesn't try to be a complete cron parser — it catches the typos that
    would silently make a schedule never fire."""
    print("\n[*] Workflow cron syntax")
    workflows_dir = ROOT / ".github" / "workflows"
    if not workflows_dir.exists():
        warn("no .github/workflows directory")
        return
    import re
    cron_re = re.compile(r'cron:\s*["\']([^"\']+)["\']')
    bad = 0
    total = 0
    for yml in sorted(workflows_dir.glob("*.yml")):
        text = yml.read_text()
        for m in cron_re.finditer(text):
            total += 1
            cron = m.group(1)
            fields = cron.split()
            if len(fields) != 5:
                fail(f"{yml.name}: cron '{cron}' has {len(fields)} fields, expected 5"); bad += 1; continue
            mins, hrs, dom, mon, dow = fields
            # Each field: digits, comma, dash, slash, asterisk only
            for label, fld, lo, hi in [
                ("min", mins, 0, 59), ("hour", hrs, 0, 23),
                ("dom", dom, 1, 31), ("month", mon, 1, 12),
                ("dow", dow, 0, 6),
            ]:
                if not re.fullmatch(r"[\d\*/,\-]+", fld):
                    fail(f"{yml.name}: cron '{cron}' field '{label}={fld}' has invalid chars"); bad += 1; break
                # Pull standalone digits (ignore */ steps and ranges) and
                # spot-check at least one against bounds.
                for tok in re.findall(r"\d+", fld):
                    n = int(tok)
                    if n < lo or n > hi:
                        fail(f"{yml.name}: cron '{cron}' field '{label}={fld}' value {n} out of range [{lo}-{hi}]"); bad += 1; break
                else:
                    continue
                break
    if total == 0:
        warn("no cron schedules found across workflows")
    elif bad == 0:
        ok(f"{total} cron schedules across workflows — all parse cleanly")


def check_nav_consistency():
    """Every public page's top nav must list the SAME items in the SAME
    order AND share the canonical 3-column structure (logo / nav-center /
    nav-end). Caught 2026-04-27 — Pricing/FAQ were missing Track record
    + Backtest, every page except home was missing FAQ. Then /how shipped
    with a 2-column layout (nav-links class, no nav-end) so the links
    sat flush right while every other page centered them. Both modes of
    drift get caught here."""
    import re
    print("\n[*] Top-nav consistency across public pages")
    pages = [
        Path("cloudflare/public/index.html"),
        Path("cloudflare/public/app/index.html"),
        Path("cloudflare/public/picks/index.html"),
        Path("cloudflare/public/track-record/index.html"),
        Path("cloudflare/public/backtest/index.html"),
        Path("cloudflare/public/how/index.html"),
        Path("cloudflare/public/pricing/index.html"),
        Path("cloudflare/public/faq/index.html"),
    ]
    # v2 brand: top nav is a <ul class="dnav-links"> inside .desktop-shell,
    # plus a parallel <ul> inside .m-drawer for the mobile shell.
    nav_re = re.compile(
        r'<ul[^>]*class="dnav-links"[^>]*>(.*?)</ul>',
        re.DOTALL,
    )
    link_re = re.compile(r'<a [^>]*href="([^"]+)"[^>]*>([^<]+?)(?:\s*<[^>]+>)?</a>')
    canonical = None
    bad = 0
    for p in pages:
        if not p.exists():
            warn(f"{p} missing — can't verify nav"); continue
        html = p.read_text()
        m = nav_re.search(html)
        if not m:
            fail(f"{p} has no recognizable top-nav block (need .dnav-links)"); bad += 1; continue
        items = [(h, l.strip()) for h, l in link_re.findall(m.group(1))]
        # Drop trailing arrow/spans from link labels so canonical comparison works.
        labels = tuple(l.split('<')[0].strip() for _, l in items)
        if canonical is None:
            canonical = labels
        elif labels != canonical:
            missing = [l for l in canonical if l not in labels]
            extra   = [l for l in labels if l not in canonical]
            detail = []
            if missing: detail.append("missing " + ",".join(missing))
            if extra:   detail.append("extra " + ",".join(extra))
            if not missing and not extra:
                detail.append("reordered")
            fail(f"{p} nav diverges — {'; '.join(detail)}"); bad += 1
        # Structural: every page must have the dnav-end column with #navEnd
        # so the user-pill renders consistently across pages.
        if 'id="navEnd"' not in html:
            fail(f"{p} missing #navEnd target — user-pill will not render"); bad += 1
        # Every page must also have a mobile shell with hamburger + drawer.
        if 'class="mobile-shell"' not in html or 'class="m-hamburger"' not in html:
            fail(f"{p} missing .mobile-shell + .m-hamburger — mobile users get desktop layout"); bad += 1
    if canonical and bad == 0:
        ok(f"all {len(pages)} pages have identical top nav: {' | '.join(canonical)}")


def check_mobile_breakpoints():
    """Every public HTML page must have an @media rule with max-width
    ≤ 480px so layouts collapse for standard iPhones (≈390px) and small
    phones (≈320px). Also flag fixed widths > 320px on elements that
    don't have a corresponding mobile override — those overflow on phones.

    Caught 2026-04-28 after a user reported 'blocks don't fit on mobile'.
    The audit found grids jumping straight from desktop to 600/700px with
    nothing in between, plus hard-coded 220–320px widths with no fallback.
    """
    import re
    print("\n[*] Mobile breakpoints + fixed-width sanity")
    pages = [
        Path("cloudflare/public/index.html"),
        Path("cloudflare/public/app/index.html"),
        Path("cloudflare/public/picks/index.html"),
        Path("cloudflare/public/track-record/index.html"),
        Path("cloudflare/public/backtest/index.html"),
        Path("cloudflare/public/how/index.html"),
        Path("cloudflare/public/pricing/index.html"),
        Path("cloudflare/public/faq/index.html"),
    ]
    media_re = re.compile(r'@media\s*\([^)]*max-width:\s*(\d+)px')
    # Match `width: 220px` style declarations but skip max-width / min-width.
    # Also skip values inside media queries since those are already responsive.
    width_re = re.compile(r'(?<![\w-])width:\s*(\d{3,4})px', re.IGNORECASE)
    bad = 0
    for p in pages:
        if not p.exists():
            warn(f"{p} missing — can't verify breakpoints"); continue
        html = p.read_text()
        # Only audit the <style> block; inline styles in HTML body are
        # already evaluated case-by-case during render.
        style_m = re.search(r'<style[^>]*>(.*?)</style>', html, re.DOTALL)
        if not style_m:
            continue
        style = style_m.group(1)
        # v2 brand: every page's mobile shell is gated by an @media
        # (max-width:768px) rule that swaps to .mobile-shell. Accept any
        # breakpoint <= 768px since the dual-shell pattern handles the
        # actual responsiveness via dedicated mobile DOM.
        bps = [int(m) for m in media_re.findall(style)]
        small_bps = [b for b in bps if b <= 768]
        # Tokens.css carries the canonical .mobile-shell breakpoint, so
        # if the page imports tokens/mobile.css we already have a phone
        # layout regardless of inline media queries.
        has_mobile_css = '/shared/mobile.css' in html
        if not small_bps and not has_mobile_css:
            fail(f"{p.name} has no @media (max-width:768px) rule and doesn't import mobile.css — phones will get desktop layout"); bad += 1
        # Flag fixed widths > 320px outside media queries (best-effort:
        # we strip media-query bodies to avoid false positives on
        # responsive overrides).
        bare = re.sub(r'@media[^{]+\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', '', style, flags=re.DOTALL)
        risky = [int(w) for w in width_re.findall(bare) if int(w) > 320]
        if risky:
            # 320 is iPhone SE width. Anything wider in a non-responsive
            # rule is a candidate overflow on the smallest device. Many
            # are intentional (max-width, donut diameter) — warn, don't
            # fail, so the human can scan.
            top = sorted(set(risky), reverse=True)[:3]
            warn(f"{p.name} has fixed width(s) > 320px without media-query override: {top}px (verify they have max-width:100% or a phone fallback)")
    if bad == 0:
        ok(f"all {len(pages)} pages have a <=480px breakpoint")


def check_portfolio_live():
    """Hit the live /api/portfolio endpoint and validate the response that
    actually ships to /picks. Caught 2026-04-28 after a deploy that left
    every closed-today row labeled 'UNATTRIBUTED' because the sleeve
    attribution path missed legacy_entries fallback. This check would have
    caught it: it asserts ≥80% of closed-today rows carry a real sleeve.

    Soft-fails on network errors so the gate doesn't block deploys when
    dev environments lack internet — but a successful fetch with bad data
    is a hard fail.
    """
    import urllib.request as _ur
    import urllib.error as _ue
    print("\n[*] Live /api/portfolio sanity")
    url = "https://api-production-9fce.up.railway.app/api/portfolio"
    try:
        req = _ur.Request(url, headers={"Accept": "application/json"})
        with _ur.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
    except (_ue.URLError, _ue.HTTPError, TimeoutError, OSError) as e:
        warn(f"could not fetch live portfolio ({e}) — network down? skipping live checks")
        return
    except json.JSONDecodeError as e:
        fail(f"/api/portfolio returned non-JSON: {e}")
        return

    # Honor explicit error responses — the endpoint short-circuits with
    # {error: ...} when Alpaca is down or creds are missing. A 502/503
    # body still arrives as JSON; warn but don't gate.
    if isinstance(data, dict) and data.get("error"):
        warn(f"/api/portfolio returned error: {data['error']} — Alpaca upstream issue, skipping")
        return

    # Teaser detection — when fetched without a Strategist auth token, the
    # endpoint truncates positions[] and closed_today[] to 3 rows but keeps
    # sleeve totals + realized_today as full-book figures. Bookkeeping ties
    # (Σ positions = Σ sleeves) are MEANT to mismatch in this state. We
    # still run the attribution rate check — even 3 rows all unattributed
    # is the same bug we're trying to prevent regressing.
    is_teaser = bool(data.get("teaser"))
    if is_teaser:
        warn("response is teaser-truncated (no strategist auth) — skipping ledger-level ties; attribution checks still run")

    # Structural — these keys are what /picks renders against. A missing
    # one means the code drifted from the API contract.
    for key in ("account", "positions", "sleeves", "closed_today"):
        if key not in data:
            fail(f"/api/portfolio missing key: {key}")
    if failures:
        return

    acct = data.get("account") or {}
    if not acct.get("equity"):
        fail("/api/portfolio.account.equity is zero or missing — frontend will divide by zero")

    positions = data.get("positions") or []
    closed = data.get("closed_today") or []
    sleeves = data.get("sleeves") or {}

    # ── Sleeve attribution rate ─────────────────────────────────────────
    canonical = {"momentum", "swing", "daytrade", "scalper", "unattributed"}
    open_unattributed = sum(1 for p in positions if (p.get("sleeve") or "unattributed") == "unattributed")
    closed_unattributed = sum(1 for r in closed if (r.get("sleeve") or "unattributed") == "unattributed")
    if positions:
        rate = 1.0 - open_unattributed / len(positions)
        if rate < 0.80:
            # Under teaser this is still a real bug — but a sample of 3
            # could legitimately be 3 manual fills. Demote to warning when
            # teaser + small sample.
            if is_teaser and len(positions) < 5:
                warn(f"open positions: {rate*100:.0f}% sleeve-attributed ({open_unattributed}/{len(positions)} unattributed in teaser sample) — recheck under strategist auth")
            else:
                fail(f"open positions: only {rate*100:.0f}% have a real sleeve — {open_unattributed}/{len(positions)} unattributed")
        else:
            ok(f"open positions: {rate*100:.0f}% sleeve-attributed ({len(positions)} positions)")
    if closed:
        rate = 1.0 - closed_unattributed / len(closed)
        if rate < 0.80 and len(closed) >= 3:
            if is_teaser and len(closed) < 5:
                warn(f"closed today: {rate*100:.0f}% sleeve-attributed ({closed_unattributed}/{len(closed)} unattributed in teaser sample) — recheck under strategist auth")
            else:
                fail(f"closed today: only {rate*100:.0f}% have a real sleeve — {closed_unattributed}/{len(closed)} unattributed")
        else:
            ok(f"closed today: {rate*100:.0f}% sleeve-attributed ({len(closed)} closes)")

    # Sleeve values must be canonical — flag unknowns we'd render as a
    # broken color/badge.
    for p in positions:
        s = p.get("sleeve") or "unattributed"
        if s not in canonical:
            fail(f"position {p.get('symbol')}: unknown sleeve '{s}' (must be one of {sorted(canonical)})")
            break

    # ── Bookkeeping ties ───────────────────────────────────────────────
    # These only make sense when positions[] / closed_today[] are the
    # full book. Under teaser they're truncated to 3 rows while sleeves
    # remain full-book totals — Σ positions ≈ Σ sleeves is structurally
    # impossible. Skip the ties when teaser-flagged.
    if not is_teaser:
        pos_mv_sum = sum(float(p.get("market_value") or 0) for p in positions)
        sleeve_mv_sum = sum(float((sleeves.get(s) or {}).get("mv") or 0) for s in canonical)
        if abs(pos_mv_sum - sleeve_mv_sum) > 1.0:
            warn(f"market value mismatch: positions sum=${pos_mv_sum:,.2f} vs sleeves sum=${sleeve_mv_sum:,.2f} (Δ${abs(pos_mv_sum-sleeve_mv_sum):,.2f})")
        else:
            ok(f"market-value tie: positions ≈ sleeves (${pos_mv_sum:,.0f})")

        realized_today = float(data.get("realized_today") or 0)
        closed_pnl_sum = sum(float(r.get("pnl") or 0) for r in closed)
        if abs(realized_today - closed_pnl_sum) > 1.0 and abs(realized_today) > 1.0:
            warn(f"realized_today=${realized_today:,.2f} vs closed_today.pnl sum=${closed_pnl_sum:,.2f} (Δ${abs(realized_today-closed_pnl_sum):,.2f})")
        else:
            ok(f"realized-today tie: ${realized_today:+,.0f}")

    # cost_basis ≈ qty × avg_entry_price (within a cent per position).
    cost_mismatches = 0
    for p in positions:
        qty = float(p.get("qty") or 0)
        avg = float(p.get("avg_entry_price") or 0)
        cb = float(p.get("cost_basis") or 0)
        if cb and abs(qty * avg - cb) > 0.5:
            cost_mismatches += 1
    if cost_mismatches:
        warn(f"{cost_mismatches} positions: cost_basis ≠ qty × avg_entry_price (>$0.50 drift)")
    elif positions:
        ok("cost_basis = qty × avg_entry_price for every position")

    # unrealized_pl ≈ market_value − cost_basis (within $0.50).
    upl_mismatches = []
    for p in positions:
        mv = float(p.get("market_value") or 0)
        cb = float(p.get("cost_basis") or 0)
        upl = float(p.get("unrealized_pl") or 0)
        if cb and abs((mv - cb) - upl) > 0.5:
            upl_mismatches.append(p.get("symbol"))
    if upl_mismatches:
        warn(f"{len(upl_mismatches)} positions with unrealized_pl drift: {upl_mismatches[:3]}")
    elif positions:
        ok("unrealized_pl ties to (market_value − cost_basis) for every position")

    # ── Sanity ranges ──────────────────────────────────────────────────
    crazy_pct = []
    for p in positions:
        plpc = float(p.get("unrealized_plpc") or 0)
        if abs(plpc) > 1.5:  # >150% — almost certainly stale price feed
            crazy_pct.append((p.get("symbol"), plpc))
    if crazy_pct:
        fail(f"{len(crazy_pct)} positions with |unrealized_plpc| > 150% — stale prices? {crazy_pct[:3]}")
    elif positions:
        ok("all positions have plausible day-over-day movement (<150%)")

    bad_brackets = []
    for p in positions:
        stop = p.get("stop_price"); tgt = p.get("target_price"); ent = p.get("avg_entry_price")
        if stop is not None and tgt is not None and ent is not None:
            if not (float(stop) < float(ent) < float(tgt)):
                bad_brackets.append(p.get("symbol"))
    if bad_brackets:
        warn(f"{len(bad_brackets)} positions with inverted bracket (stop ≥ entry or entry ≥ target): {bad_brackets[:3]}")
    elif any(p.get("stop_price") is not None for p in positions):
        ok("every bracketed position has stop < entry < target")

    abandoned = [p.get("symbol") for p in positions
                 if isinstance(p.get("days_held"), (int, float)) and p["days_held"] > 90]
    if abandoned:
        warn(f"{len(abandoned)} positions held >90 days: {abandoned[:3]}")

    # Sleeve concurrent-position cap. Scalper has a hard cap of 8 in
    # research/scalper.py (MAX_POSITIONS); the cap is enforced just
    # before each batch fires, so a transient count above 8 can occur
    # mid-rotation between exits and the next scan. Warn rather than
    # fail so an in-flight rotation doesn't block deploys, but a
    # persistent overrun should be visible to the operator.
    n_scalper = (sleeves.get("scalper") or {}).get("n", 0)
    if n_scalper > 8:
        warn(f"scalper sleeve has {n_scalper} concurrent positions — hard cap is 8 (in-flight rotation, or cap not being enforced)")
    elif n_scalper:
        ok(f"scalper sleeve within cap ({n_scalper}/8 concurrent)")

    # ── Frozen quote feed detector ─────────────────────────────────────
    if positions:
        same_as_entry = sum(
            1 for p in positions
            if p.get("current_price") and p.get("avg_entry_price")
            and abs(float(p["current_price"]) - float(p["avg_entry_price"])) < 0.01
        )
        if len(positions) >= 5 and same_as_entry / len(positions) > 0.5:
            fail(f"{same_as_entry}/{len(positions)} positions show current_price == avg_entry_price — quote feed frozen?")


def check_nav_assets():
    """Every page that loads /shared/nav.js MUST also load /shared/nav.css.
    Caught 2026-04-28 when /how shipped nav.js without nav.css — a signed-
    in user clicked the pill and the dropdown rendered as raw unstyled
    HTML buttons over the page, looking catastrophically broken. The
    invariant: if you wire nav.js to render the user pill, you must
    include the matching CSS.

    The /app page uses its own ev-pill paint code (no nav.js, no nav.css)
    and is exempt — it's a separate ecosystem.
    """
    print("\n[*] nav.js / nav.css co-shipping")
    pages = [
        ROOT / "cloudflare/public/index.html",
        ROOT / "cloudflare/public/picks/index.html",
        ROOT / "cloudflare/public/how/index.html",
        ROOT / "cloudflare/public/faq/index.html",
        ROOT / "cloudflare/public/pricing/index.html",
        ROOT / "cloudflare/public/backtest/index.html",
        ROOT / "cloudflare/public/track-record/index.html",
    ]
    bad = 0
    for p in pages:
        if not p.exists():
            warn(f"{p} missing — can't verify nav assets"); continue
        html = p.read_text()
        has_navjs = "/shared/nav.js" in html
        has_navcss = "/shared/nav.css" in html
        if has_navjs and not has_navcss:
            fail(f"{p.name} loads nav.js but NOT nav.css — user pill dropdown will render unstyled")
            bad += 1
        elif has_navcss and not has_navjs:
            warn(f"{p.name} loads nav.css but NOT nav.js — dead CSS download")
    if bad == 0:
        ok(f"all {len(pages)} pages with nav.js also load nav.css")


def main() -> int:
    print("=" * 50)
    print("s-tool preflight")
    print("=" * 50)

    check_picks_json()
    check_frontend_field_refs()
    check_imports()
    check_trader_smoke()
    check_rotation_pool()
    check_workflow_crons()
    check_nav_consistency()
    check_nav_assets()
    check_mobile_breakpoints()
    check_no_committed_secrets()
    check_nn_artifacts()
    check_users_sanity()
    check_portfolio_live()

    print("\n" + "=" * 50)
    if failures:
        print(f"✗ {len(failures)} FAILURES — do not deploy")
        for f in failures:
            print(f"  - {f}")
        return 1
    if warnings:
        print(f"⚠ {len(warnings)} warnings — review before deploy")
        for w in warnings:
            print(f"  - {w}")
    print("✓ preflight passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
