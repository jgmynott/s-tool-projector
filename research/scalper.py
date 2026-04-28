#!/usr/bin/env python3
"""
Scalper sleeve — high-frequency intraday signal-driven entries.

Distinct from the trader.py sleeves (momentum/swing/daytrade) because:
  * Driven by INTRADAY price/volume signals, not the nightly picks list
  * 5-min cron cadence (vs 30-min rotate window for daytrade)
  * Tight stops/targets (-0.7% / +1.0%) for fast in-and-out
  * Hard $/position cap and max concurrent positions to bound blast radius
    while we learn whether the signals print

Signals implemented:
  1. Opening Range Breakout (ORB) — price closes above the high of the first
     30 min after market open on rising volume. Classic intraday momentum.

Future signals (commented out, ship after backtest validates):
  2. Gap Fade — gap > 1.5% on no news → fade
  3. Relative-Volume Spike — last 5-min volume > 2× 20-day avg same window

Operations:
  * Reuses trader.py's Alpaca client and trade journal helpers
  * Tags positions with sleeve='scalper' via client_order_id encoding so
    /api/portfolio displays them in their own sleeve card
  * Force-closes any scalper position older than MAX_HOLD_MIN
  * Refuses to trade if equity < SCALPER_MIN_EQUITY (won't churn a tiny
    account into commissions even on paper)
  * Respects a kill-switch flag in trader_state.json (state.scalper_disabled)

Run:
  python3 research/scalper.py dry         # plan only
  python3 research/scalper.py live        # plan + submit
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

# Reuse trader's Alpaca client + journal helpers — same auth, same data API.
sys.path.insert(0, str(Path(__file__).parent))
from trader import (  # type: ignore
    Alpaca,
    load_state, save_state,
    load_journal, save_journal, _prune_journal,
    journal_entry_buy,
)

log = logging.getLogger("scalper")
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s %(message)s",
                    level=logging.INFO)

REPO = Path(__file__).resolve().parent.parent
STATE_PATH = REPO / "research" / "trader_state.json"

# ── Config ──────────────────────────────────────────────────────────────

# Universe = high-volume tickers where scalping signals have a fighting
# chance. S&P 500 mega-caps + a few leveraged ETFs (intraday tradeable
# vol). Keep small until we know which signals print — fewer tickers =
# fewer rate-limit headaches and easier to debug.
UNIVERSE = [
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO",
    # Semis (highest intraday vol in the index)
    "AMD", "TSM", "MU", "INTC", "ASML", "MRVL",
    # Financials
    "JPM", "BAC", "GS", "MS", "C", "BLK",
    # Industrials/energy
    "XOM", "CVX", "BA", "GE", "CAT", "DE",
    # Consumer
    "NFLX", "DIS", "COST", "WMT", "HD",
    # Leveraged ETFs (high beta intraday)
    "TQQQ", "SQQQ", "SOXL", "SOXS", "TNA", "TZA",
    # Index ETFs
    "SPY", "QQQ", "IWM",
]

# Signal params. Tight = many trades, fewer false positives but smaller
# wins. Loose = fewer trades, bigger wins per fire but more whipsaws.
ORB_OPENING_MIN = 30          # measure opening range over first 30 min of cash session
ORB_BREAKOUT_PCT = 0.0015     # need price > opening_high * (1 + this) — 0.15% buffer
ORB_VOL_MULTIPLE = 1.3        # 5-min volume must be ≥ 1.3× the avg of the opening range bars

# Risk caps. Hard-coded so a runaway scalper run can't blow up the account.
MAX_POSITIONS = 8             # never more than N scalper positions at once
PER_POSITION_NOTIONAL = 500   # dollars per scalper entry
MIN_EQUITY = 5000             # refuse to scalp if equity below this
MAX_HOLD_MIN = 25             # force-close any scalper position older than this
STOP_PCT = -0.007             # -0.7% stop
TARGET_PCT = 0.010            # +1.0% target

# Market session in UTC (DST). The cron only fires during these hours;
# this is an extra guard so a misfired cron in ST doesn't trade pre-market.
RTH_START_UTC = dtime(13, 30)   # 09:30 ET (DST)
RTH_END_UTC = dtime(20, 0)      # 16:00 ET (DST)
SCAN_LOCKOUT_UTC = dtime(14, 0) # don't scan ORB until 30 min after open

# ── Data ────────────────────────────────────────────────────────────────

def load_dotenv() -> dict:
    env = os.environ.copy()
    env_path = REPO / ".env"
    if not env_path.exists():
        return env
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def fetch_5min_bars(api: Alpaca, symbols: list[str], start_iso: str) -> dict:
    """Pull 5-min bars for a list of symbols since `start_iso` from Alpaca's
    market-data API. Returns {symbol: [{t,o,h,l,c,v}, ...]}.

    Alpaca's bars endpoint accepts comma-separated symbols, so we hit it
    with the full universe in one or two calls — well under the 200/min
    free-tier limit even at 5-min cadence.
    """
    out: dict[str, list] = {}
    base = "https://data.alpaca.markets/v2/stocks/bars"
    syms_param = ",".join(symbols)
    # Replace '+00:00' with 'Z' — query params don't url-encode '+' so
    # Alpaca sees a space in the middle of the timestamp and 400s with
    # "extra text: T13:30:00 00:00".
    start_safe = start_iso.replace("+00:00", "Z")
    url = (f"{base}?symbols={syms_param}&timeframe=5Min&start={start_safe}"
           f"&limit=10000&adjustment=raw&feed=iex")
    req = Request(url, headers=api.headers, method="GET")
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except HTTPError as e:
        log.error("bars fetch HTTP %s: %s", e.code, e.read().decode("utf-8", "replace")[:200])
        return out
    except Exception as e:
        log.error("bars fetch failed: %s", e)
        return out
    bars_by = data.get("bars") or {}
    for sym, rows in bars_by.items():
        out[sym] = rows or []
    return out


# ── Signal: Opening Range Breakout ──────────────────────────────────────

def orb_signal(bars: list[dict]) -> dict | None:
    """Return a signal dict if the ORB pattern triggers on the most recent
    bar, else None.

    Pattern: bars[0..N) form the opening range (first 30 min). The most
    recent bar (bars[-1]) closes above range_high*(1+buffer) on volume
    ≥ 1.3× the avg of the opening range bars.
    """
    if len(bars) < 7:  # need 6 opening-range + 1 current
        return None
    # First N bars after the open (each bar is 5 min, so 30 min = 6 bars).
    n_open = ORB_OPENING_MIN // 5
    if len(bars) < n_open + 1:
        return None
    opening = bars[:n_open]
    current = bars[-1]
    range_high = max(b["h"] for b in opening)
    range_low = min(b["l"] for b in opening)
    avg_open_vol = sum(b["v"] for b in opening) / max(1, len(opening))
    cur_close = current["c"]
    cur_vol = current["v"]
    # Already inside opening window → no signal yet.
    if len(bars) <= n_open:
        return None
    # Don't fire on the same range twice — require the breakout bar to be
    # one of the most recent 2 bars (so we don't keep buying every 5 min
    # when price stays above the range).
    breakout_at = None
    for i, b in enumerate(bars[n_open:], start=n_open):
        if b["c"] > range_high * (1 + ORB_BREAKOUT_PCT):
            breakout_at = i
            break
    if breakout_at is None or breakout_at < len(bars) - 2:
        return None
    if cur_vol < avg_open_vol * ORB_VOL_MULTIPLE:
        return None
    return {
        "signal": "orb",
        "ref_price": cur_close,
        "range_high": range_high,
        "range_low": range_low,
        "avg_open_vol": avg_open_vol,
        "cur_vol": cur_vol,
    }


# ── Planner ─────────────────────────────────────────────────────────────

def scalper_state_key(state: dict) -> dict:
    """Sub-namespace inside trader_state.json so scalper bookkeeping
    doesn't collide with the existing entries/traded_today fields."""
    return state.setdefault("scalper", {
        "positions": {},     # symbol -> {opened_at, entry_price, signal}
        "fired_today": [],   # signals already fired this UTC date — dedupe
        "last_scan": None,
    })


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def in_session(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    t = now.time()
    return RTH_START_UTC <= t <= RTH_END_UTC


def plan(api: Alpaca, state: dict) -> dict:
    now = now_utc()
    sc = scalper_state_key(state)
    today_iso = now.date().isoformat()

    # Reset daily caches if it's a new UTC date.
    if sc.get("last_scan_date") != today_iso:
        sc["positions"] = {}
        sc["fired_today"] = []
        sc["last_scan_date"] = today_iso

    if state.get("scalper_disabled"):
        return {"reason": "kill_switch", "entries": [], "exits": []}
    if not in_session(now):
        return {"reason": "out_of_session", "entries": [], "exits": []}
    if now.time() < SCAN_LOCKOUT_UTC:
        return {"reason": "pre_lockout", "entries": [], "exits": []}

    acct = api.account()
    eq = float(acct.get("equity") or 0)
    if eq < MIN_EQUITY:
        return {"reason": "min_equity", "entries": [], "exits": []}

    # Pull bars from the start of today's RTH session — covers the
    # opening range plus everything since.
    session_start = datetime.combine(now.date(), RTH_START_UTC, tzinfo=timezone.utc)
    bars_by_sym = fetch_5min_bars(api, UNIVERSE, session_start.isoformat())

    open_positions = sc.get("positions", {})
    # Drop stale positions whose underlying isn't actually open at Alpaca
    # (e.g. bracket exited and we missed the update).
    live_syms = {p["symbol"] for p in api.positions()}
    for sym in list(open_positions.keys()):
        if sym not in live_syms:
            open_positions.pop(sym, None)

    exits: list[dict] = []
    for sym, info in list(open_positions.items()):
        opened_at = datetime.fromisoformat(info["opened_at"])
        age_min = (now - opened_at).total_seconds() / 60
        if age_min >= MAX_HOLD_MIN:
            exits.append({"symbol": sym, "reason": "max_hold"})

    entries: list[dict] = []
    if len(open_positions) - len(exits) >= MAX_POSITIONS:
        return {"reason": "at_max_positions", "entries": [], "exits": exits}

    for sym in UNIVERSE:
        if sym in open_positions:
            continue
        # Per-day signal dedupe — don't keep buying the same name on
        # the same setup repeatedly inside one session.
        if any(s["symbol"] == sym for s in sc["fired_today"]):
            continue
        bars = bars_by_sym.get(sym) or []
        sig = orb_signal(bars)
        if not sig:
            continue
        qty = max(1, int(PER_POSITION_NOTIONAL // sig["ref_price"]))
        entries.append({
            "symbol": sym,
            "qty": qty,
            "ref_price": sig["ref_price"],
            "stop_pct": STOP_PCT,
            "target_pct": TARGET_PCT,
            "signal": sig["signal"],
            "diag": {k: sig[k] for k in ("range_high", "range_low", "cur_vol")},
        })
        if len(open_positions) + len(entries) - len(exits) >= MAX_POSITIONS:
            break

    return {
        "reason": "ok",
        "entries": entries,
        "exits": exits,
        "open": list(open_positions.keys()),
        "scanned": len(bars_by_sym),
    }


# ── Executor ────────────────────────────────────────────────────────────

def submit_scalper_buy(api: Alpaca, e: dict, state: dict, journal_rows: list) -> bool:
    sym = e["symbol"]
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    cid = f"scalper-{sym}-{today}-{datetime.now(timezone.utc).strftime('%H%M%S')}"
    ref = e["ref_price"]
    stop_price = round(ref * (1 + e["stop_pct"]), 2)
    take_price = round(ref * (1 + e["target_pct"]), 2)
    body = {
        "symbol": sym, "qty": str(e["qty"]),
        "side": "buy", "type": "market", "time_in_force": "day",
        "order_class": "bracket",
        "stop_loss": {"stop_price": str(stop_price)},
        "take_profit": {"limit_price": str(take_price)},
        "client_order_id": cid,
    }
    try:
        api.submit(body)
    except RuntimeError as err:
        msg = str(err)
        if "422" in msg and any(t in msg for t in ("take_profit", "stop_loss", "base_price")):
            log.warning("scalper bracket rejected for %s — fallback market buy", sym)
            api.submit({
                "symbol": sym, "qty": str(e["qty"]),
                "side": "buy", "type": "market", "time_in_force": "day",
                "client_order_id": cid + "-nb",
            })
        else:
            log.error("scalper buy %s failed: %s", sym, err)
            return False
    sc = scalper_state_key(state)
    sc["positions"][sym] = {
        "symbol": sym, "qty": e["qty"], "entry_price": ref,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "signal": e["signal"], "stop": stop_price, "target": take_price,
    }
    sc["fired_today"].append({"symbol": sym, "signal": e["signal"],
                               "ts": datetime.now(timezone.utc).isoformat()})
    journal_rows.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "buy", "sleeve": "scalper", "symbol": sym, "qty": e["qty"],
        "ref_price": ref, "live_price": ref,
        "stop": stop_price, "target": take_price,
        "tier": e["signal"], "rationale": json.dumps(e.get("diag") or {}),
        "result": "submitted",
    })
    return True


def submit_scalper_exit(api: Alpaca, x: dict, state: dict, journal_rows: list) -> bool:
    sym = x["symbol"]
    try:
        api.close_position(sym)
    except RuntimeError as e:
        log.error("scalper exit %s failed: %s", sym, e)
        return False
    sc = scalper_state_key(state)
    info = sc["positions"].pop(sym, {})
    journal_rows.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "sell", "sleeve": "scalper", "symbol": sym,
        "qty": info.get("qty"), "buy_price": info.get("entry_price"),
        "sell_price": None, "pnl": None,
        "exit_reason": x.get("reason") or "manual",
    })
    return True


def print_plan(p: dict) -> None:
    print(f"=== Scalper plan ({p.get('reason')}) ===")
    print(f"  open: {p.get('open')}")
    print(f"  scanned: {p.get('scanned')} symbols")
    print(f"  ENTRIES ({len(p.get('entries', []))}):")
    for e in p.get("entries", []):
        print(f"    BUY  {e['symbol']:>5} qty={e['qty']:>3} @ ${e['ref_price']:>7.2f}  "
              f"sig={e['signal']:>4}  stop {e['stop_pct']*100:+.2f}%  tgt {e['target_pct']*100:+.2f}%")
    print(f"  EXITS ({len(p.get('exits', []))}):")
    for x in p.get("exits", []):
        print(f"    SELL {x['symbol']:>5}  reason={x.get('reason')}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["dry", "live"])
    args = p.parse_args()

    env = load_dotenv()
    if not env.get("ALPACA_API_KEY"):
        print("missing ALPACA_API_KEY", file=sys.stderr)
        return 2

    api = Alpaca(env)
    clock = api.clock()
    if not clock.get("is_open"):
        print(f"market closed — refusing. next_open={clock.get('next_open')}")
        return 0

    state = load_state()
    plan_out = plan(api, state)
    print_plan(plan_out)
    if args.mode == "dry":
        return 0
    if not plan_out.get("entries") and not plan_out.get("exits"):
        save_state(state)  # persist last_scan_date / dedupes
        return 0

    journal_rows = load_journal()
    new_rows: list[dict] = []
    for x in plan_out.get("exits", []):
        submit_scalper_exit(api, x, state, new_rows)
    for e in plan_out.get("entries", []):
        submit_scalper_buy(api, e, state, new_rows)
    save_state(state)
    if new_rows:
        save_journal(_prune_journal(journal_rows + new_rows))
        print(f"\njournal: appended {len(new_rows)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
