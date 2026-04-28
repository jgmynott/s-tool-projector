#!/usr/bin/env python3
"""
One-shot Alpaca activity backfill into runtime_data/trade_journal.json.

Why this exists: trader.py only journals fills it sees pass through its own
submit_buy/submit_sell paths plus a same-day FILL pull at the close window.
Anything that closes outside that path — bracket child fills, circuit-breaker
liquidations on prior code, runs that refused due to market-closed clock —
silently never reaches the journal. The track-record page therefore shows
the wrong number of trades.

This script pulls FILL activities for the past N days from Alpaca's
/v2/account/activities/FILL endpoint, derives journal rows, and merges them
into the existing journal (deduped by order_id when present, else by
ts+symbol+side+qty). Buys carry sleeve="unattributed" because we don't have
the historic state.entries; the website tolerates that.

Run via the backfill-journal.yml workflow — local creds are typically
expired/different from the GH secret.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parent.parent
JOURNAL = REPO / "runtime_data" / "trade_journal.json"
DAYS_BACK = int(os.environ.get("BACKFILL_DAYS", "14"))


def alpaca_get(path: str) -> list:
    base = os.environ.get("ALPACA_BASE_URL", "").rstrip("/")
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_API_SECRET", "")
    if not (base and key and secret):
        print("missing ALPACA_* env vars", file=sys.stderr)
        sys.exit(2)
    url = f"{base}{path}"
    req = Request(url, headers={
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
    }, method="GET")
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        print(f"HTTP {e.code} from {url}: {body}", file=sys.stderr)
        sys.exit(3)


def fetch_fills(days_back: int) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    out: list[dict] = []
    for i in range(days_back + 1):
        d = (today - timedelta(days=i)).isoformat()
        rows = alpaca_get(f"/account/activities/FILL?date={d}")
        if not isinstance(rows, list):
            continue
        out.extend(rows)
    return out


def load_journal() -> list[dict]:
    if not JOURNAL.exists():
        return []
    raw = json.loads(JOURNAL.read_text())
    return raw if isinstance(raw, list) else (raw.get("rows") or [])


def existing_keys(rows: list[dict]) -> set:
    keys: set = set()
    for r in rows:
        oid = r.get("order_id")
        if oid:
            keys.add(("oid", oid))
        keys.add((
            "tup",
            (r.get("ts") or "")[:19],
            r.get("symbol"),
            r.get("event"),
            float(r.get("qty") or 0),
        ))
    return keys


def fill_to_row(a: dict, side: str) -> dict | None:
    sym = a.get("symbol")
    try:
        qty = float(a.get("qty") or 0)
        price = float(a.get("price") or 0)
    except (TypeError, ValueError):
        return None
    if not sym or qty <= 0 or price <= 0:
        return None
    ts = a.get("transaction_time") or a.get("submitted_at") or ""
    if side == "buy":
        return {
            "ts": ts,
            "event": "buy",
            "sleeve": "unattributed",
            "symbol": sym,
            "qty": qty,
            "ref_price": price,
            "live_price": price,
            "stop": None,
            "target": None,
            "tier": None,
            "rationale": "",
            "result": "backfilled",
            "order_id": a.get("order_id"),
        }
    return {
        "ts": ts,
        "event": "sell",
        "sleeve": "unattributed",
        "symbol": sym,
        "qty": qty,
        "buy_price": None,
        "sell_price": price,
        "pnl": None,
        "exit_reason": a.get("order_type") or "backfilled",
        "order_id": a.get("order_id"),
    }


def fifo_pair_pnl(rows: list[dict]) -> list[dict]:
    """Walk chronologically and FIFO-pair sells against open buys per symbol
    so backfilled sells get a buy_price + pnl. Without this every backfilled
    sell shows pnl=None and the track-record realized totals stay wrong."""
    rows = sorted(rows, key=lambda r: (r.get("ts") or ""))
    open_lots: dict[str, list[list]] = {}
    for r in rows:
        sym = r.get("symbol")
        if not sym:
            continue
        if r.get("event") == "buy":
            qty = float(r.get("qty") or 0)
            px = r.get("ref_price") or r.get("live_price")
            if qty > 0 and px:
                open_lots.setdefault(sym, []).append([qty, float(px)])
        elif r.get("event") == "sell":
            need = float(r.get("qty") or 0)
            sell_px = r.get("sell_price")
            if need <= 0 or not sell_px:
                continue
            cost_basis = 0.0
            matched = 0.0
            lots = open_lots.get(sym, [])
            while need > 0 and lots:
                lot_qty, lot_px = lots[0]
                take = min(lot_qty, need)
                cost_basis += take * lot_px
                matched += take
                need -= take
                lot_qty -= take
                if lot_qty <= 1e-9:
                    lots.pop(0)
                else:
                    lots[0][0] = lot_qty
            if matched > 0:
                avg_buy = cost_basis / matched
                r["buy_price"] = avg_buy
                r["pnl"] = (float(sell_px) - avg_buy) * matched
    return rows


def main() -> int:
    print(f"backfill: pulling {DAYS_BACK} days of FILL activities")
    activities = fetch_fills(DAYS_BACK)
    print(f"backfill: got {len(activities)} raw activity rows")

    existing = load_journal()
    print(f"backfill: existing journal has {len(existing)} rows")
    keys = existing_keys(existing)

    if activities:
        print(f"backfill: sample activity keys={list(activities[0].keys())}")
        print(f"backfill: sample activity={json.dumps(activities[0])[:500]}")
        from collections import Counter as _C
        types = _C(a.get("activity_type") or a.get("type") for a in activities)
        sides = _C((a.get("side") or "") for a in activities)
        print(f"backfill: type counts={dict(types)}")
        print(f"backfill: side counts={dict(sides)}")

    new_rows: list[dict] = []
    for a in activities:
        atype = a.get("activity_type") or a.get("type")
        if atype not in ("FILL", "PARTIAL_FILL"):
            continue
        side = (a.get("side") or "").lower()
        if side not in ("buy", "sell"):
            continue
        row = fill_to_row(a, side)
        if not row:
            continue
        oid = row.get("order_id")
        sig_oid = ("oid", oid) if oid else None
        sig_tup = (
            "tup",
            (row.get("ts") or "")[:19],
            row.get("symbol"),
            row.get("event"),
            float(row.get("qty") or 0),
        )
        if sig_oid and sig_oid in keys:
            continue
        if sig_tup in keys:
            continue
        new_rows.append(row)
        if sig_oid:
            keys.add(sig_oid)
        keys.add(sig_tup)

    print(f"backfill: {len(new_rows)} new rows to append")
    if not new_rows:
        return 0

    merged = fifo_pair_pnl(existing + new_rows)
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    JOURNAL.write_text(json.dumps(merged, indent=2))
    print(f"backfill: wrote {len(merged)} rows to {JOURNAL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
