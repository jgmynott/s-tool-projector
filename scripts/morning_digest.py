#!/usr/bin/env python3
"""
Morning digest — pushed to ntfy/Discord at 08:00 ET on weekdays.

Replaces the laptop-tied `SessionStart` hook so you don't need Claude or
your laptop on to know what happened overnight + how the trader is doing.
Fires from .github/workflows/morning-digest.yml.

Reads from the live API (already cloud-hosted), so no local state.
Pushes a single short message to ntfy and/or Discord. Designed to fit on
a phone lock-screen notification — keep tight.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

API = os.environ.get("API_BASE", "https://s-tool.io")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
DISCORD = os.environ.get("DISCORD_WEBHOOK_URL", "")
GH_REPO = os.environ.get("GH_REPO", "jgmynott/s-tool-projector")


def fetch_json(url: str, timeout: int = 10):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "morning-digest"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
        print(f"fetch failed {url}: {e}", file=sys.stderr)
        return None


def fmt_pct(x: float | None) -> str:
    if x is None:
        return "—"
    sign = "+" if x >= 0 else "−"
    return f"{sign}{abs(x) * 100:.2f}%"


def fmt_money(x: float | None) -> str:
    if x is None:
        return "—"
    sign = "+" if x >= 0 else "−"
    return f"{sign}${abs(x):,.0f}"


def build_message() -> tuple[str, str, str]:
    """Returns (title, short_body, priority). Priority is ntfy-format."""
    pf = fetch_json(f"{API}/api/portfolio")
    tr = fetch_json(f"{API}/api/trade-journal?lookback_days=1")
    ds = fetch_json(f"{API}/api/data-status")
    issues = fetch_json(f"https://api.github.com/repos/{GH_REPO}/issues?labels=watchdog,trader-failure&state=open")

    lines: list[str] = []
    title = "S-Tool morning digest"
    priority = "default"

    if pf and pf.get("account"):
        a = pf["account"]
        eq = a.get("equity")
        dc = a.get("day_change")
        dcp = a.get("day_change_pct")
        n_pos = sum((s or {}).get("n", 0) for s in (pf.get("sleeves") or {}).values())
        lines.append(f"Equity ${eq:,.0f} · {fmt_money(dc)} ({fmt_pct(dcp)})")
        lines.append(f"Positions: {n_pos} ({(pf.get('sleeves') or {}).get('momentum', {}).get('n', 0)}m / {(pf.get('sleeves') or {}).get('swing', {}).get('n', 0)}s / {(pf.get('sleeves') or {}).get('daytrade', {}).get('n', 0)}d)")
        if dcp is not None and dcp <= -0.02:
            priority = "high"
            title = "S-Tool: bad morning"
        elif dcp is not None and dcp >= 0.01:
            title = "S-Tool: good morning"

    if tr and tr.get("stats"):
        s = tr["stats"]
        wins = s.get("wins", 0)
        losses = s.get("losses", 0)
        if wins + losses > 0:
            lines.append(f"24h trades: {wins}W/{losses}L · realized {fmt_money(s.get('realized_pnl_total'))}")

    if ds:
        # /api/data-status nests the picks freshness under feeds.picks_history
        # (matches post_deploy_verify.py's contract) — top-level latest_pick_date
        # was never returned and the previous code silently never alerted.
        ph = (ds.get("feeds") or {}).get("picks_history") or {}
        latest = ph.get("latest_pick_date")
        if latest:
            today = datetime.now(timezone.utc).date().isoformat()
            if latest != today:
                lines.append(f"⚠ Picks stale: latest={latest}, today={today}")
                priority = "high"

    if isinstance(issues, list) and issues:
        lines.append(f"⚠ {len(issues)} open watchdog issue(s)")
        priority = "high"

    if not lines:
        lines.append("API unreachable — check Railway")
        priority = "high"
        title = "S-Tool: API down"

    return title, "\n".join(lines), priority


def push_ntfy(topic: str, title: str, body: str, priority: str) -> None:
    if not topic:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            method="POST",
            headers={
                "Title": title,
                "Priority": priority,
                "Tags": "chart_with_upwards_trend",
                "Click": "https://s-tool.io/picks",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        print(f"ntfy push ok ({priority})")
    except Exception as e:
        print(f"ntfy push failed: {e}", file=sys.stderr)


def push_discord(webhook: str, title: str, body: str) -> None:
    if not webhook:
        return
    try:
        payload = json.dumps({
            "username": "S-Tool",
            "embeds": [{
                "title": title,
                "description": body,
                "color": 0x4F46E5,
                "url": "https://s-tool.io/picks",
            }],
        }).encode("utf-8")
        req = urllib.request.Request(
            webhook, data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        print("discord push ok")
    except Exception as e:
        print(f"discord push failed: {e}", file=sys.stderr)


def main() -> int:
    title, body, priority = build_message()
    print(f"=== {title} ===")
    print(body)
    push_ntfy(NTFY_TOPIC, title, body, priority)
    push_discord(DISCORD, title, body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
