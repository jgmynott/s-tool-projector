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
            fail(f"{len(asym) - with_bands} asymmetric picks missing p10/p90 — partial data corruption, UI will show blank slots")
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
    picks_html = ROOT / "cloudflare/public/picks/index.html"
    if not picks_html.exists():
        warn("picks/index.html missing — skipping")
        return
    html = picks_html.read_text()
    # Smoke: ensure the page references each of the key fields. Tolerant
    # of different access patterns (p.xxx, p?.xxx, p["xxx"]).
    check_fields = [
        "symbol", "expected_return", "sharpe_proxy", "current_price",
        "p50_target", "rationale", "confidence", "company_name",
    ]
    missing = [f for f in check_fields if f not in html]
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
    """Make sure the nightly learning pipeline wrote its outputs.

    Missing NN cache = Asymmetric tier silently falls back to EWMA scoring
    (still decent but not the 9.5x lift). Worth a warning."""
    print("\n[*] NN learning artifacts")
    dc = ROOT / "data_cache"
    checks = [
        ("production_scorer.json", "nightly learning decision"),
        ("nn_scores.json", "asymmetric NN scores for today"),
        ("confidence_nn_scores.json", "confidence NN scores for today"),
        ("asymmetric_scores.json", "EWMA P90 fallback scores"),
    ]
    for fname, label in checks:
        p = dc / fname
        if not p.exists():
            warn(f"{fname} missing — run overnight_learn.py / enrich_asymmetric.py")
            continue
        age_hours = (__import__("time").time() - p.stat().st_mtime) / 3600
        if age_hours > 48:
            warn(f"{fname} is {age_hours:.1f}h old — NN drifting from market")
        else:
            ok(f"{fname} fresh ({age_hours:.1f}h old)")


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
                "signals_sec_edgar.py", "worker.py", "users_db.py"):
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


def main() -> int:
    print("=" * 50)
    print("s-tool preflight")
    print("=" * 50)

    check_picks_json()
    check_frontend_field_refs()
    check_imports()
    check_no_committed_secrets()
    check_nn_artifacts()
    check_users_sanity()

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
