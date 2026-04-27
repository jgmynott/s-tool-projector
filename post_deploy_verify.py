#!/usr/bin/env python3
"""Post-deploy verification — runs after `railway up` in the fast/slow
pipelines. Hits the live API, asserts the deploy actually serves valid
responses (not 502, not stale-shape, not the prior container).

This catches the failure mode where Railway reports `success` from the
CLI but the new container fails to boot, so the public endpoint either
times out or keeps serving the previous version. Watchdog (L3) eventually
catches that, but L2 fires within ~60s of deploy completion.

Exits non-zero on failure so the workflow_run fails and the issue tripwire
opens. Polls with backoff because Railway typically takes 30-90s for the
container swap; we can't synchronously wait on the swap from the CLI.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

SITE_URL = "https://s-tool.io"
MAX_WAIT_SEC = 180
POLL_INTERVAL = 10


def http_json(url: str, timeout: int = 15):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "post-deploy-verify/1.0",
            "Cache-Control": "no-cache",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(body), body[:400]
            except json.JSONDecodeError:
                return r.status, None, body[:400]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if getattr(e, "fp", None) else ""
        try:
            return e.code, json.loads(body), body[:400]
        except json.JSONDecodeError:
            return e.code, None, body[:400]
    except Exception as e:
        return 0, None, repr(e)[:400]


def check_portfolio() -> tuple[bool, str]:
    code, data, body = http_json(f"{SITE_URL}/api/portfolio?_v={int(time.time())}")
    if code != 200:
        return False, f"HTTP {code}: {body[:160]}"
    if not isinstance(data, dict):
        return False, "non-dict body"
    for k in ("account", "positions", "sleeves", "benchmark"):
        if k not in data:
            return False, f"missing key {k}; got {sorted(data.keys())}"
    eq = float((data.get("account") or {}).get("equity") or 0)
    if eq <= 0:
        return False, f"account.equity={eq} (expected > 0)"
    return True, f"equity=${eq:,.0f}"


def check_picks() -> tuple[bool, str]:
    code, data, body = http_json(f"{SITE_URL}/api/picks?_v={int(time.time())}")
    if code not in (200, 402):
        return False, f"HTTP {code}: {body[:160]}"
    if not isinstance(data, dict):
        return False, "non-dict body"
    teaser = data.get("teaser") or data.get("picks") or []
    if not teaser:
        return False, f"no picks; keys={sorted(data.keys())}"
    return True, f"{len(teaser)} picks visible (HTTP {code})"


def check_track_record() -> tuple[bool, str]:
    code, data, body = http_json(f"{SITE_URL}/api/track-record?_v={int(time.time())}")
    if code not in (200, 402):
        return False, f"HTTP {code}: {body[:160]}"
    if not isinstance(data, dict):
        return False, "non-dict body"
    return True, f"HTTP {code}, keys={sorted(data.keys())[:5]}"


CHECKS = [
    ("portfolio", check_portfolio),
    ("picks", check_picks),
    ("track_record", check_track_record),
]


def run_all() -> tuple[bool, list[tuple[str, bool, str]]]:
    results = []
    all_ok = True
    for name, fn in CHECKS:
        ok, detail = fn()
        results.append((name, ok, detail))
        if not ok:
            all_ok = False
    return all_ok, results


def main() -> int:
    print(f"post-deploy verify @ {SITE_URL}")
    deadline = time.monotonic() + MAX_WAIT_SEC
    last_results: list = []
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        all_ok, results = run_all()
        last_results = results
        print(f"\n[attempt {attempt}]")
        for name, ok, detail in results:
            sym = "✓" if ok else "✗"
            print(f"  {sym} {name}: {detail}")
        if all_ok:
            print(f"\n✓ all checks green after attempt {attempt}")
            return 0
        remaining = deadline - time.monotonic()
        if remaining < POLL_INTERVAL:
            break
        print(f"  retry in {POLL_INTERVAL}s ({remaining:.0f}s remaining of {MAX_WAIT_SEC}s budget)")
        time.sleep(POLL_INTERVAL)
    print(f"\n✗ post-deploy verify failed after {attempt} attempt(s) over {MAX_WAIT_SEC}s")
    for name, ok, detail in last_results:
        if not ok:
            print(f"  - {name}: {detail}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
