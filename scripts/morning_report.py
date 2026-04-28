#!/usr/bin/env python3
"""
S-Tool morning report — distills overnight + production state into one digest.

Invoked by:
  - The SessionStart hook (~/.claude/settings.json) so each new Claude
    session starts with up-to-date context on what ran overnight.
  - Manually:  python3 scripts/morning_report.py

Reads (no writes, no network beyond two cheap GETs):
  - research/overnight_status.json    (local launchd job)
  - research/overnight_<latest>.log    (last 30 lines)
  - research/overnight.pid             (is overnight job still alive?)
  - data_cache/production_scorer.json  (canonical NN-pipeline freshness)
  - data_cache/asymmetric_scores.json  (mtime only)
  - https://api-production-9fce.up.railway.app/api/data-status  (live data)
  - https://api.github.com/repos/jgmynott/s-tool-projector/actions/runs (last 5)

Output: short plain text on stdout. Errors don't break the hook (always exit 0).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/Users/jamesmynott_macbook/Documents/Claude/s2tool-projector")
STATUS = REPO / "research" / "overnight_status.json"
PIDFILE = REPO / "research" / "overnight.pid"
QUEUE = REPO / "research" / "overnight_queue.txt"
FINDINGS = REPO / "research" / "findings.jsonl"
PROD_SCORER = REPO / "data_cache" / "production_scorer.json"
ASYM_SCORES = REPO / "data_cache" / "asymmetric_scores.json"
API_BASE = "https://api-production-9fce.up.railway.app"
GH_API = "https://api.github.com/repos/jgmynott/s-tool-projector"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def age_str(ts: datetime) -> str:
    delta = now_utc() - ts
    h = delta.total_seconds() / 3600
    if h < 1:
        return f"{int(delta.total_seconds() / 60)}m ago"
    if h < 48:
        return f"{h:.1f}h ago"
    return f"{h / 24:.1f}d ago"


def mtime(p: Path):
    if not p.exists():
        return None
    return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)


def fetch_json(url: str, timeout: int = 6):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "morning-report/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as e:
        return 0, {"_error": repr(e)[:200]}


def queue_runnable_count() -> int:
    if not QUEUE.exists():
        return 0
    return sum(
        1 for ln in QUEUE.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    )


def read_findings(n: int = 3) -> list[dict]:
    if not FINDINGS.exists():
        return []
    out: list[dict] = []
    try:
        for line in FINDINGS.read_text().splitlines()[-n:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return out


def consecutive_fallback_runs() -> int:
    """Count trailing entries in findings.jsonl whose source == 'fallback'."""
    if not FINDINGS.exists():
        return 0
    count = 0
    try:
        for line in reversed(FINDINGS.read_text().splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("source") == "fallback":
                count += 1
            else:
                break
    except OSError:
        pass
    return count


def section_overnight() -> list[str]:
    out = ["── overnight job ──"]
    if not STATUS.exists():
        out.append("  no overnight_status.json yet (launchd hasn't fired or runner failed)")
        return out
    try:
        st = json.loads(STATUS.read_text())
    except (json.JSONDecodeError, OSError) as e:
        out.append(f"  status unreadable: {e}")
        return out
    out.append(f"  started:        {st.get('started_at', '?')}")
    out.append(f"  script:         {st.get('script', '?')}  (source={st.get('source', '?')})")
    out.append(f"  queue_remaining: {st.get('queue_remaining', '?')}")

    pid = st.get("pid")
    if pid and PIDFILE.exists():
        alive = is_pid_alive(int(pid))
        out.append(f"  pid {pid}:        {'STILL RUNNING' if alive else 'finished'}")

    log = Path(st.get("log", ""))
    if log.exists():
        try:
            tail = log.read_text(errors="replace").splitlines()[-12:]
            out.append(f"  log tail ({log.name}):")
            for line in tail:
                out.append(f"    {line[:160]}")
        except OSError as e:
            out.append(f"  log unreadable: {e}")
    else:
        out.append(f"  log path missing: {log}")
    return out


def section_findings() -> list[str]:
    out = ["── recent findings (last 3) ──"]
    runnable = queue_runnable_count()
    fallback_streak = consecutive_fallback_runs()
    out.append(f"  queue runnable lines: {runnable}")
    if runnable == 0:
        out.append("  (queue is empty — fallback will run tonight; populate research/overnight_queue.txt)")
    if fallback_streak >= 7:
        out.append(f"  WARN: {fallback_streak} consecutive fallback runs — same script every night, populate the queue")
    findings = read_findings(3)
    if not findings:
        out.append("  no findings yet (findings.jsonl empty or missing)")
        return out
    for f in findings:
        ts = f.get("ts", "?")
        script = f.get("script", "?")
        exit_code = f.get("exit", "?")
        dur = f.get("duration_s", "?")
        src = f.get("source", "?")
        flag = "OK" if exit_code == 0 else f"FAIL exit={exit_code}"
        out.append(f"  {ts}  [{src}]  {flag}  ({dur}s)  {script[:90]}")
    return out


def section_pipeline_freshness() -> list[str]:
    out = ["── pipeline freshness ──"]
    p_mt = mtime(PROD_SCORER)
    a_mt = mtime(ASYM_SCORES)
    if p_mt:
        out.append(f"  production_scorer.json:   {age_str(p_mt)}  ({p_mt.isoformat()})")
    else:
        out.append("  production_scorer.json:   MISSING")
    if a_mt:
        out.append(f"  asymmetric_scores.json:   {age_str(a_mt)}  ({a_mt.isoformat()})")
    else:
        out.append("  asymmetric_scores.json:   MISSING")
    if p_mt and (now_utc() - p_mt).total_seconds() > 7 * 86400:
        out.append("  WARN: production_scorer is older than 7 days (slow pipeline likely failing)")
    return out


def section_live_api() -> list[str]:
    out = ["── live API state ──"]
    status, data = fetch_json(f"{API_BASE}/api/data-status")
    if status == 200 and isinstance(data, dict):
        feeds = data.get("feeds", {})
        ph = feeds.get("picks_history", {})
        si = feeds.get("short_interest_yf", {})
        ab = feeds.get("short_interest_ablation", {})
        out.append(f"  picks latest:      {ph.get('latest_pick_date', '?')}")
        out.append(f"  short_interest:    snapshot {si.get('last_snapshot_date', '?')}, fetched {si.get('last_fetched_at', '?')}")
        out.append(f"  ablation run_date: {ab.get('run_date', '?')}")
    else:
        out.append(f"  /api/data-status failed: status={status} err={data.get('_error', '?')}")
    h_status, h = fetch_json(f"{API_BASE}/api/health")
    if h_status == 200 and isinstance(h, dict):
        out.append(f"  /api/health:       {h.get('overall', '?')}  api_errors_4xx={h.get('api_stats', {}).get('errors_4xx')}  5xx={h.get('api_stats', {}).get('errors_5xx')}")
    else:
        out.append(f"  /api/health failed: status={h_status}")
    return out


def section_gh_actions() -> list[str]:
    out = ["── GH Actions (last 24h) ──"]
    cutoff = now_utc().timestamp() - 86400
    status, data = fetch_json(f"{GH_API}/actions/runs?per_page=20", timeout=8)
    if status != 200 or not isinstance(data, dict):
        out.append(f"  GH API failed: status={status}")
        return out
    seen_workflows = {}
    for run in data.get("workflow_runs", []):
        try:
            t = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if t.timestamp() < cutoff:
            continue
        wf = run.get("name", "?")
        seen_workflows.setdefault(wf, []).append(run)
    for wf in ("nightly-pipeline-slow", "nightly-pipeline-fast", "watchdog", "trader"):
        runs = seen_workflows.get(wf, [])
        if not runs:
            out.append(f"  {wf}: no runs in last 24h")
            continue
        latest = runs[0]
        concl = latest.get("conclusion") or latest.get("status") or "?"
        n_total = len(runs)
        n_failed = sum(1 for r in runs if r.get("conclusion") not in ("success", None))
        flag = "OK" if concl == "success" and n_failed == 0 else f"WARN ({n_failed}/{n_total} failed)"
        out.append(f"  {wf}: {n_total} runs, latest={concl}  {flag}")
    return out


def section_watchdog_issues() -> list[str]:
    out = ["── watchdog open issues ──"]
    status, data = fetch_json(f"{GH_API}/issues?labels=watchdog&state=open&per_page=10", timeout=8)
    if status != 200 or not isinstance(data, list):
        out.append(f"  GH API failed: status={status}")
        return out
    if not data:
        out.append("  none open")
        return out
    for iss in data[:5]:
        out.append(f"  #{iss.get('number')}  {iss.get('title','')[:90]}")
    return out


def main() -> int:
    print("=== S-Tool morning report ===")
    print(f"  generated:      {now_utc().isoformat()}")
    print(f"  repo:           {REPO}")
    print()
    for fn in (
        section_overnight,
        section_findings,
        section_pipeline_freshness,
        section_live_api,
        section_gh_actions,
        section_watchdog_issues,
    ):
        try:
            for line in fn():
                print(line)
        except Exception as e:  # never break the SessionStart hook
            print(f"  [section error: {fn.__name__}: {e!r}]")
        print()
    print("=== end S-Tool morning report ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
