#!/usr/bin/env python3
"""S-Tool watchdog — health monitor for the live trading + picks stack.

Runs every 15 min during US market hours (14:00-19:30 UTC weekdays) and hourly
otherwise. Each tick checks a fixed set of invariants against the live system,
prints a structured summary, and on critical failures opens a GitHub issue
(rate-limited 1/hour) and posts to Discord.

Two invariants self-correct when they fail:
  - railway_sha: if Railway is N commits behind origin/main, dispatch the fast
    pipeline (which deploys both Railway + Cloudflare). Cooldown derived from
    GH run history — won't re-dispatch if a manual fast run was kicked off in
    the last 60 min.
  - rotation_recent: during market hours, if no trader run has succeeded in
    the last 60 min, dispatch a live rotate. Same-window cooldown.

Cooldowns use GH Actions run history rather than a state file; no commit per
tick. Issues are rate-limited by querying open `watchdog` label.

Exit codes:
  0 = all green
  1 = warnings only
  2 = critical failures (issue opened, may have self-corrected)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
SITE_URL = "https://s-tool.io"
GH_REPO = "jgmynott/s-tool-projector"
NOW = datetime.now(timezone.utc)

SELFHEAL_COOLDOWN_MIN = 60
ISSUE_RATE_LIMIT_MIN = 60


@dataclass
class CheckResult:
    name: str
    ok: bool
    severity: str  # "critical" | "warning" | "info"
    detail: str
    selfheal_action: Optional[str] = None
    selfheal_result: Optional[str] = None


# ── helpers ──

def http_json(url: str, timeout: int = 15):
    """Fetch URL → (status, parsed_json_or_None, raw_body_first_400)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "watchdog/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(body), body[:400]
            except json.JSONDecodeError:
                return r.status, None, body[:400]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if getattr(e, "fp", None) else ""
        # 4xx responses (e.g., 402 strategist_required) often carry a JSON
        # payload — try to parse so callers can inspect the body, not just
        # the status code.
        try:
            return e.code, json.loads(body), body[:400]
        except json.JSONDecodeError:
            return e.code, None, body[:400]
    except Exception as e:
        return 0, None, repr(e)[:400]


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def gh_run_list(workflow: str, limit: int = 10) -> list[dict]:
    cmd = ["gh", "run", "list", f"--workflow={workflow}", f"--limit={limit}",
           "--json", "status,conclusion,createdAt,event,databaseId,headSha"]
    try:
        return json.loads(subprocess.check_output(cmd, timeout=20).decode())
    except Exception:
        return []


def gh_dispatch(workflow: str, fields: dict | None = None) -> bool:
    cmd = ["gh", "workflow", "run", workflow]
    for k, v in (fields or {}).items():
        cmd.extend(["-f", f"{k}={v}"])
    try:
        subprocess.check_output(cmd, timeout=20, stderr=subprocess.STDOUT)
        return True
    except Exception:
        return False


def gh_open_issues(label: str) -> list[dict]:
    cmd = ["gh", "issue", "list", "--state=open", f"--label={label}",
           "--limit=20", "--json", "number,title,createdAt"]
    try:
        return json.loads(subprocess.check_output(cmd, timeout=20).decode())
    except Exception:
        return []


def gh_issue_create(title: str, body: str, label: str = "watchdog") -> bool:
    body_path = Path("/tmp/watchdog_issue_body.md")
    body_path.write_text(body)
    cmd = ["gh", "issue", "create", "--title", title,
           "--body-file", str(body_path), "--label", label]
    try:
        subprocess.check_output(cmd, timeout=20, stderr=subprocess.STDOUT)
        return True
    except Exception:
        return False


def discord_post(content: str) -> None:
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    try:
        body = json.dumps({"content": content[:1900]}).encode()
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


def is_market_hours() -> bool:
    """US equity hours: weekday + 13:30-20:00 UTC (covers DST + standard)."""
    if NOW.weekday() >= 5:
        return False
    hf = NOW.hour + NOW.minute / 60.0
    return 13.5 <= hf <= 20.0


def selfheal_eligible(workflow: str, max_age_min: int = SELFHEAL_COOLDOWN_MIN) -> bool:
    """Eligible if no workflow_dispatch of `workflow` in last `max_age_min`."""
    runs = gh_run_list(workflow, limit=15)
    for r in runs:
        if r.get("event") != "workflow_dispatch":
            continue
        age = (NOW - parse_iso(r["createdAt"])).total_seconds() / 60
        if age < max_age_min:
            return False
    return True


# ── invariants ──

def check_pipeline_freshness(workflow: str, max_age_h: int,
                              weekday_only: bool, label: str) -> CheckResult:
    runs = gh_run_list(workflow, limit=20)
    if not runs:
        return CheckResult(label, False, "warning",
                           f"could not query gh run list for {workflow}")
    successes = [r for r in runs if r.get("conclusion") == "success"]
    if not successes:
        return CheckResult(label, False, "critical",
                           f"no successful run of {workflow} in last 20 attempts")
    last = max(successes, key=lambda r: r["createdAt"])
    age_h = (NOW - parse_iso(last["createdAt"])).total_seconds() / 3600
    if age_h > max_age_h:
        if weekday_only and NOW.weekday() in (5, 6):
            return CheckResult(label, True, "info",
                               f"last success {age_h:.1f}h ago — weekend tolerance")
        return CheckResult(label, False, "critical",
                           f"last success {age_h:.1f}h ago > {max_age_h}h limit")
    return CheckResult(label, True, "info", f"last success {age_h:.1f}h ago")


def check_api_portfolio() -> CheckResult:
    code, data, body = http_json(f"{SITE_URL}/api/portfolio?_v={int(time.time())}")
    if code != 200 or not isinstance(data, dict):
        return CheckResult("api_portfolio", False, "critical",
                           f"HTTP {code}: {body[:160]}")
    for k in ("account", "positions", "sleeves", "benchmark"):
        if k not in data:
            return CheckResult("api_portfolio", False, "critical",
                               f"missing key {k}; got {sorted(data.keys())}")
    eq = float((data.get("account") or {}).get("equity") or 0)
    if eq <= 0:
        return CheckResult("api_portfolio", False, "critical",
                           f"account.equity={eq}")
    return CheckResult("api_portfolio", True, "info", f"equity=${eq:,.0f}")


def check_api_picks() -> CheckResult:
    """Watchdog is unauthenticated, so /api/picks returns HTTP 402 with a
    teaser payload (correct gate behavior). 200 (full response) and 402
    (gated teaser) both count as healthy as long as the JSON is parseable
    and contains picks. Anything else (5xx, 4xx without teaser, malformed
    JSON) is a real outage."""
    code, data, body = http_json(f"{SITE_URL}/api/picks?_v={int(time.time())}")
    if code not in (200, 402) or not isinstance(data, dict):
        return CheckResult("api_picks", False, "critical",
                           f"HTTP {code}: {body[:160]}")
    teaser = data.get("teaser") or data.get("picks") or []
    if not teaser:
        return CheckResult("api_picks", False, "critical",
                           f"no picks visible; keys={sorted(data.keys())}")
    return CheckResult("api_picks", True, "info",
                       f"{len(teaser)} picks visible (HTTP {code})")


def check_picks_freshness() -> CheckResult:
    """Picks freshness via /api/picks. The gated response (HTTP 402 for
    non-strategist visitors) carries `scan_age_hours` directly; the
    strategist response uses `scanned_at` ISO timestamp. Handle both."""
    code, data, _ = http_json(f"{SITE_URL}/api/picks?_v={int(time.time())}")
    if code not in (200, 402) or not isinstance(data, dict):
        return CheckResult("picks_freshness", False, "warning",
                           f"could not fetch /api/picks (HTTP {code})")
    age_h: float | None = None
    if "scan_age_hours" in data:
        try:
            age_h = float(data["scan_age_hours"])
        except (TypeError, ValueError):
            age_h = None
    if age_h is None and data.get("scanned_at"):
        try:
            age_h = (NOW - parse_iso(data["scanned_at"])).total_seconds() / 3600
        except Exception:
            age_h = None
    if age_h is None:
        return CheckResult("picks_freshness", False, "warning",
                           f"no age field in /api/picks (keys={sorted(data.keys())})")
    if age_h > 28:
        return CheckResult("picks_freshness", False, "critical",
                           f"picks {age_h:.1f}h old > 28h")
    return CheckResult("picks_freshness", True, "info", f"picks {age_h:.1f}h old")


def check_railway_sha() -> CheckResult:
    runs = gh_run_list("daily-refresh-fast.yml", limit=1)
    if not runs:
        return CheckResult("railway_sha", False, "warning",
                           "could not query latest fast run")
    deployed = runs[0].get("headSha") or ""
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "origin/main"], cwd=str(ROOT), timeout=10
        ).decode().strip()
    except Exception:
        return CheckResult("railway_sha", False, "warning",
                           "could not git rev-parse origin/main")
    if deployed == head:
        return CheckResult("railway_sha", True, "info", f"in sync ({head[:7]})")
    try:
        lag = int(subprocess.check_output(
            ["git", "rev-list", "--count", f"{deployed}..{head}"],
            cwd=str(ROOT), timeout=10).decode().strip())
    except Exception:
        lag = -1
    severity = "critical" if (lag < 0 or lag > 5) else "warning"
    detail = f"deployed={deployed[:7]} head={head[:7]} lag={lag}"
    sh_action = sh_result = None
    if selfheal_eligible("daily-refresh-fast.yml"):
        if gh_dispatch("daily-refresh-fast.yml"):
            sh_action, sh_result = "fast_pipeline_dispatch", "ok"
        else:
            sh_action, sh_result = "fast_pipeline_dispatch", "failed"
    else:
        sh_action, sh_result = "fast_pipeline_dispatch", "skipped (cooldown)"
    return CheckResult("railway_sha", False, severity, detail, sh_action, sh_result)


def check_rotation_recent() -> CheckResult:
    """During market hours, expect a successful trader run within last 60 min."""
    if not is_market_hours():
        return CheckResult("rotation_recent", True, "info", "outside market hours")
    runs = gh_run_list("trader.yml", limit=15)
    if not runs:
        return CheckResult("rotation_recent", False, "warning",
                           "no trader runs visible")
    successes = [r for r in runs if r.get("conclusion") == "success"]
    if not successes:
        return CheckResult("rotation_recent", False, "critical",
                           "no recent successful trader runs in last 15 attempts")
    newest = max(successes, key=lambda r: r["createdAt"])
    age_min = (NOW - parse_iso(newest["createdAt"])).total_seconds() / 60
    if age_min <= 60:
        return CheckResult("rotation_recent", True, "info",
                           f"last trader success {age_min:.0f} min ago")
    sh_action = sh_result = None
    if selfheal_eligible("trader.yml", max_age_min=30):
        if gh_dispatch("trader.yml", {"mode": "live", "window": "rotate"}):
            sh_action, sh_result = "rotate_dispatch", "ok"
        else:
            sh_action, sh_result = "rotate_dispatch", "failed"
    else:
        sh_action, sh_result = "rotate_dispatch", "skipped (cooldown)"
    return CheckResult("rotation_recent", False, "warning",
                       f"last trader success {age_min:.0f} min ago > 60 min during market",
                       sh_action, sh_result)


# ── main ──

def main() -> int:
    checks = [
        check_pipeline_freshness("daily-refresh-fast.yml", 28, False, "fast_pipeline"),
        check_pipeline_freshness("daily-refresh-slow.yml", 28, True, "slow_pipeline"),
        check_api_portfolio(),
        check_api_picks(),
        check_picks_freshness(),
        check_railway_sha(),
        check_rotation_recent(),
    ]

    print(f"watchdog tick @ {NOW.isoformat()}")
    print("=" * 60)
    n_critical = n_warn = n_ok = 0
    for c in checks:
        sym = "✓" if c.ok else ("⚠" if c.severity == "warning" else "✗")
        line = f"  {sym} [{c.severity:>8}] {c.name}: {c.detail}"
        if c.selfheal_action:
            line += f"  [selfheal: {c.selfheal_action} → {c.selfheal_result}]"
        print(line)
        if c.ok: n_ok += 1
        elif c.severity == "warning": n_warn += 1
        else: n_critical += 1
    print("=" * 60)
    print(f"  ok={n_ok}  warn={n_warn}  critical={n_critical}")

    if n_critical > 0:
        # Rate-limit: skip if any open watchdog issue created in last 60 min.
        recent = [
            i for i in gh_open_issues("watchdog")
            if (NOW - parse_iso(i["createdAt"])).total_seconds() / 60
            < ISSUE_RATE_LIMIT_MIN
        ]
        if not recent:
            body_lines = [f"Watchdog tick: {NOW.isoformat()}", "", "**Critical failures:**"]
            for c in checks:
                if not c.ok and c.severity == "critical":
                    body_lines.append(f"- **{c.name}**: {c.detail}")
                    if c.selfheal_action:
                        body_lines.append(f"  - selfheal: {c.selfheal_action} → {c.selfheal_result}")
            body_lines += ["", "**Warnings:**"]
            for c in checks:
                if not c.ok and c.severity == "warning":
                    body_lines.append(f"- {c.name}: {c.detail}")
                    if c.selfheal_action:
                        body_lines.append(f"  - selfheal: {c.selfheal_action} → {c.selfheal_result}")
            body_lines += ["", f"Run: https://github.com/{GH_REPO}/actions"]
            gh_issue_create(
                f"⚠️ Watchdog: {n_critical} critical fail(s) — "
                f"{NOW.strftime('%Y-%m-%d %H:%M UTC')}",
                "\n".join(body_lines),
            )
            discord_post(f"🚨 Watchdog: {n_critical} critical — check GH issues")
        return 2
    if n_warn > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
