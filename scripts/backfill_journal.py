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


class AlpacaError(RuntimeError):
    """Raised on any non-2xx Alpaca response so callers can decide whether
    to retry, skip, or abort. Prior version sys.exit'd on first HTTPError
    which made a single 429 fatal across a 365-day backfill walk."""
    def __init__(self, code: int, body: str):
        super().__init__(f"HTTP {code}: {body}")
        self.code = code
        self.body = body


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
        raise AlpacaError(e.code, body)


def fetch_fills(days_back: int) -> list[dict]:
    """One request per day. Alpaca paper rate-limits at 200 req/min so we
    sleep 0.4s between requests = 150 req/min — well under the cap. A
    365-day backfill takes ~2.5 min; keeps us comfortably below Alpaca's
    rate-limit ceiling. Empty days are skipped silently (weekends, market
    holidays — Alpaca returns []).

    Also short-circuits on a streak of empty days: once we've seen 60
    consecutive empty days, we assume the account had no activity prior
    and stop walking back. Saves ~15 min on a fresh account."""
    import time
    today = datetime.now(timezone.utc).date()
    out: list[dict] = []
    empty_streak = 0
    for i in range(days_back + 1):
        d = (today - timedelta(days=i)).isoformat()
        if i > 0:
            time.sleep(0.4)
        try:
            rows = alpaca_get(f"/account/activities/FILL?date={d}")
        except AlpacaError as e:
            # On 429 sleep longer and retry once. Other 4xx/5xx skip.
            if e.code == 429:
                time.sleep(15)
                try:
                    rows = alpaca_get(f"/account/activities/FILL?date={d}")
                except AlpacaError as e2:
                    print(f"backfill: skipping {d} after 429 retry: {e2.code}", flush=True)
                    continue
            else:
                print(f"backfill: skipping {d}: {e}", flush=True)
                continue
        if not isinstance(rows, list):
            continue
        if rows:
            out.extend(rows)
            empty_streak = 0
        else:
            empty_streak += 1
            if empty_streak >= 60 and i >= 60:
                print(f"backfill: 60 consecutive empty days at {d}, stopping early")
                break
    return out


def load_journal() -> list[dict]:
    if not JOURNAL.exists():
        return []
    raw = json.loads(JOURNAL.read_text())
    return raw if isinstance(raw, list) else (raw.get("rows") or [])


def _ts_to_epoch(ts: str) -> float:
    """Parse an ISO ts (with or without TZ / Z / fractional seconds) into
    epoch seconds. Returns 0 if unparseable."""
    if not ts:
        return 0.0
    try:
        s = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


def existing_index(rows: list[dict]) -> tuple[set, list]:
    """Return (oid_keys, time_index) where time_index is a list of
    (epoch_ts, symbol, event, qty) tuples. We dedup new rows against
    BOTH order_id AND a fuzzy time match (±90s) so a 'submitted' intent
    row written by trader.py and the corresponding Alpaca FILL row pulled
    by backfill don't both end up in the journal as separate buys."""
    oid_keys: set = set()
    time_index: list = []
    for r in rows:
        oid = r.get("order_id")
        if oid:
            oid_keys.add(("oid", oid))
        time_index.append((
            _ts_to_epoch(r.get("ts") or ""),
            r.get("symbol"),
            r.get("event"),
            float(r.get("qty") or 0),
        ))
    return oid_keys, time_index


def fuzzy_dupe(time_index: list, t: float, sym: str, ev: str, qty: float) -> bool:
    for ts2, sym2, ev2, qty2 in time_index:
        if sym != sym2 or ev != ev2:
            continue
        if abs(qty - qty2) > 1e-6:
            continue
        if abs(t - ts2) <= 90:
            return True
    return False


def aggregate_by_order(activities: list[dict]) -> list[dict]:
    """Alpaca returns one activity per partial fill; the same order_id can
    appear 20+ times. Aggregate to one row per order_id with summed qty
    and qty-weighted avg price."""
    by_oid: dict[str, dict] = {}
    for a in activities:
        atype = a.get("activity_type") or a.get("type")
        if atype not in ("FILL", "PARTIAL_FILL"):
            continue
        side = (a.get("side") or "").lower()
        if side not in ("buy", "sell"):
            continue
        oid = a.get("order_id") or f"_{a.get('id')}"
        try:
            qty = float(a.get("qty") or 0)
            price = float(a.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0 or price <= 0:
            continue
        sym = a.get("symbol")
        if not sym:
            continue
        ts = a.get("transaction_time") or a.get("submitted_at") or ""
        agg = by_oid.get(oid)
        if agg is None:
            by_oid[oid] = {
                "order_id": oid,
                "side": side,
                "symbol": sym,
                "qty_total": qty,
                "notional": qty * price,
                "ts_first": ts,
                "ts_last": ts,
            }
        else:
            agg["qty_total"] += qty
            agg["notional"] += qty * price
            if ts and ts < agg["ts_first"]:
                agg["ts_first"] = ts
            if ts and ts > agg["ts_last"]:
                agg["ts_last"] = ts
    return list(by_oid.values())


def order_to_row(o: dict) -> dict:
    avg_price = o["notional"] / o["qty_total"] if o["qty_total"] else 0
    if o["side"] == "buy":
        return {
            "ts": o["ts_last"],
            "event": "buy",
            "sleeve": "unattributed",
            "symbol": o["symbol"],
            "qty": o["qty_total"],
            "ref_price": avg_price,
            "live_price": avg_price,
            "stop": None,
            "target": None,
            "tier": None,
            "rationale": "",
            "result": "backfilled",
            "order_id": o["order_id"],
        }
    return {
        "ts": o["ts_last"],
        "event": "sell",
        "sleeve": "unattributed",
        "symbol": o["symbol"],
        "qty": o["qty_total"],
        "buy_price": None,
        "sell_price": avg_price,
        "pnl": None,
        "exit_reason": "backfilled",
        "order_id": o["order_id"],
    }


def drop_orphaned_stubs(rows: list[dict]) -> list[dict]:
    """trader.py writes 'submitted' stub rows the moment it submits a sell
    order — sell_price=None, pnl=None — and the actual fill row lands 7-9
    minutes later via the synchronous fill-poll OR the next backfill. The
    stubs duplicate the fills (same symbol + qty + side, ts within ~30
    min) so they double-count in /history and the closed-pair stats.

    Strategy: for each (symbol, qty, side) group with at least one
    sell_price-bearing fill, drop every stub (sell_price=None) within
    ±30 min of any fill. Stubs that survive (no matching fill) stay
    because they're either still-pending submissions or fills the
    backfill genuinely missed."""
    from datetime import datetime as _dt
    def _epoch(ts: str) -> float:
        if not ts: return 0.0
        try: return _dt.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception: return 0.0
    fills_by_key: dict = {}
    for r in rows:
        if r.get("event") != "sell": continue
        if not r.get("sell_price"): continue
        try: qty = float(r.get("qty") or 0)
        except (TypeError, ValueError): continue
        key = (r.get("symbol"), round(qty, 4))
        fills_by_key.setdefault(key, []).append(_epoch(r.get("ts") or ""))
    n_dropped = 0
    out: list[dict] = []
    for r in rows:
        is_stub = (r.get("event") == "sell"
                   and r.get("result") == "submitted"
                   and not r.get("sell_price"))
        if is_stub:
            try: qty = float(r.get("qty") or 0)
            except (TypeError, ValueError): qty = 0
            key = (r.get("symbol"), round(qty, 4))
            stub_t = _epoch(r.get("ts") or "")
            fill_times = fills_by_key.get(key, [])
            if any(abs(stub_t - ft) <= 1800 for ft in fill_times):
                n_dropped += 1
                continue
        out.append(r)
    if n_dropped:
        print(f"backfill: dropped {n_dropped} orphan stub sells (deduped against fills)")
    return out


def fifo_pair_pnl(rows: list[dict]) -> list[dict]:
    """Walk chronologically and FIFO-pair sells against open buys per symbol
    so backfilled sells get a buy_price + pnl. Without this every backfilled
    sell shows pnl=None and the track-record realized totals stay wrong."""
    rows = drop_orphaned_stubs(rows)
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
    if os.environ.get("BACKFILL_RESET") == "1":
        before = len(existing)
        existing = [r for r in existing if r.get("result") != "backfilled"
                    and r.get("exit_reason") != "backfilled"]
        print(f"backfill: BACKFILL_RESET stripped {before - len(existing)} prior backfilled rows")
    oid_keys, time_index = existing_index(existing)

    orders = aggregate_by_order(activities)
    print(f"backfill: aggregated to {len(orders)} unique orders")

    new_rows: list[dict] = []
    for o in orders:
        row = order_to_row(o)
        sig_oid = ("oid", row["order_id"])
        if sig_oid in oid_keys:
            continue
        t = _ts_to_epoch(row.get("ts") or "")
        if fuzzy_dupe(time_index, t, row["symbol"], row["event"], float(row["qty"])):
            continue
        new_rows.append(row)
        oid_keys.add(sig_oid)
        time_index.append((t, row["symbol"], row["event"], float(row["qty"])))

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
