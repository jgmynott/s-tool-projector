"""
Two-sleeve active paper-trading on Alpaca.

Capital is split 50/50 across two strategies that share the production
ensemble_score ranking but differ in holding period:

  swing    : ranks 1-10. 5-day hold. 1.5× leverage. Brackets at -7%/+15%.
             Mean +3.1% return at 5d per the horizon scan, comfortably
             above 1.5% round-trip costs.
  daytrade : ranks 11-20. Intraday only — exits forced at 15:55 ET.
             1× leverage. Brackets at -3%/+5%. Per the horizon scan
             this sleeve is net-negative after costs in expectation;
             we run it explicitly as a *trial-by-fire* on the model
             at intraday horizons. Real fills produce attribution data
             we couldn't get from backtest alone.

Why split picks by rank instead of doubling up: a symbol in both sleeves
nets at the broker into one position — we couldn't separate which
sleeve's thesis worked. Disjoint slots gives clean per-sleeve P&L.

Run modes:
  status                 : print account + positions + sleeve attribution
  dry                    : compute proposed orders for both sleeves, no submit
  live --window=open     : at 09:30 ET — swing rebalance + daytrade entries
  live --window=close    : at 15:55 ET — daytrade forced exits only

State file: research/trader_state.json — {symbol: {sleeve, opened_at,
qty, ref_price}}. Source of truth for *when* and *why* we entered;
Alpaca is source of truth for current position size and avg cost.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("trader")

ROOT = Path(__file__).parent.parent
PICKS_PATH = ROOT / "portfolio_picks.json"
STATE_PATH = ROOT / "research" / "trader_state.json"

# ── Sleeve definitions. Edit here, not in the code below.
SLEEVES = {
    "swing": {
        "rank_range": (0, 10),         # picks[0..10) — top-10 by score
        "hold_days": 5,                # exit after 5 trading days
        "stop_pct": -0.07,             # bracket stop
        "target_pct": 0.15,            # bracket target
        "leverage": 1.5,               # of half-equity
        "intraday_only": False,
    },
    "daytrade": {
        "rank_range": (10, 20),        # picks[10..20) — ranks 11-20
        "hold_days": 0,                # exit same day at 15:55 ET
        "stop_pct": -0.03,             # tighter stop, intraday vol is small
        "target_pct": 0.05,             # +5% target — realistic 1d top-tail
        "leverage": 1.0,               # 1x — net-negative EV per backtest
        "intraday_only": True,
    },
}
SLEEVE_NAMES = list(SLEEVES.keys())  # ['swing', 'daytrade']

TRADE_TIERS = {"conservative", "moderate", "aggressive"}
PORTFOLIO_DRAWDOWN_HALT_PCT = -0.03  # close everything if portfolio is down >3% intraday


# ── env loader (no python-dotenv dep) ──

def load_env() -> dict[str, str]:
    env_path = ROOT / ".env"
    out: dict[str, str] = {}
    if not env_path.exists():
        return out
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


# ── Alpaca client (urllib, minimal surface) ──

class Alpaca:
    def __init__(self, env: dict[str, str]):
        self.base = env.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2").rstrip("/")
        self.headers = {
            "APCA-API-KEY-ID": env["ALPACA_API_KEY"],
            "APCA-API-SECRET-KEY": env["ALPACA_API_SECRET"],
            "Content-Type": "application/json",
        }

    def _req(self, method: str, path: str, body: dict | None = None) -> dict | list:
        url = f"{self.base}/{path.lstrip('/')}"
        data = json.dumps(body).encode() if body is not None else None
        req = Request(url, data=data, headers=self.headers, method=method)
        try:
            with urlopen(req, timeout=20) as resp:
                txt = resp.read()
                return json.loads(txt) if txt else {}
        except HTTPError as e:
            body_txt = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"Alpaca {method} {path} → HTTP {e.code}: {body_txt}") from e

    def clock(self) -> dict: return self._req("GET", "clock")
    def account(self) -> dict: return self._req("GET", "account")
    def positions(self) -> list: return self._req("GET", "positions")
    def submit(self, body: dict) -> dict: return self._req("POST", "orders", body)
    def close_position(self, symbol: str) -> dict:
        return self._req("DELETE", f"positions/{symbol}")


# ── State ──

def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"entries": {}}
    try: return json.loads(STATE_PATH.read_text())
    except Exception: return {"entries": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ── Picks ──

def load_picks() -> list[dict]:
    if not PICKS_PATH.exists():
        log.error("portfolio_picks.json missing — run the engine first")
        return []
    d = json.loads(PICKS_PATH.read_text())
    picks = [p for p in (d.get("picks") or []) if p.get("tier") in TRADE_TIERS]
    picks.sort(key=lambda p: p.get("ensemble_score") or p.get("expected_return") or 0, reverse=True)
    return picks


def picks_for_sleeve(picks: list[dict], sleeve_name: str) -> list[dict]:
    lo, hi = SLEEVES[sleeve_name]["rank_range"]
    return picks[lo:hi]


# ── Helpers ──

def trading_days_since(iso_date: str, today: datetime) -> int:
    d0 = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
    n = 0
    cur = d0.date()
    days = (today.date() - d0.date()).days
    if days <= 0: return 0
    for _ in range(days):
        cur = cur + timedelta(days=1)
        if cur.weekday() < 5: n += 1
    return n


def equity_per_sleeve(equity: float) -> float:
    return equity * 0.5  # 50/50 fixed split


# ── Order planning ──

def plan_open_window(picks: list[dict], account: dict, positions: list,
                     state: dict) -> dict:
    """Run at 09:30 ET. Plans the swing rebalance AND the daytrade entries
    in a single batch. Returns {sleeves: {swing: {...}, daytrade: {...}}}."""
    equity = float(account["equity"])
    today = datetime.now(timezone.utc)
    held_by_symbol = {p["symbol"]: p for p in positions}
    entries = state.get("entries", {})

    plans: dict[str, dict] = {}

    for sleeve_name, cfg in SLEEVES.items():
        sleeve_picks = picks_for_sleeve(picks, sleeve_name)
        sleeve_equity = equity_per_sleeve(equity)
        target_capital = sleeve_equity * cfg["leverage"]
        n_slots = cfg["rank_range"][1] - cfg["rank_range"][0]
        per_position_target = target_capital / n_slots
        sleeve_pick_symbols = [p["symbol"] for p in sleeve_picks]

        sells, buys, skipped = [], [], []

        # ── Exits (sleeve-scoped). Only consider positions tagged to THIS sleeve.
        sleeve_held = {sym: pos for sym, pos in held_by_symbol.items()
                       if entries.get(sym, {}).get("sleeve") == sleeve_name}
        for sym, pos in sleeve_held.items():
            entry = entries.get(sym, {})
            entry_iso = entry.get("opened_at")
            days_held = trading_days_since(entry_iso, today) if entry_iso else 999
            upnl_pct = float(pos["unrealized_plpc"])
            reason = None
            if cfg["intraday_only"]:
                # Daytrade overnighter — should never happen given the close-window
                # exits, but if anything escapes, force it out at next open.
                reason = "daytrade overnight escape"
            elif days_held >= cfg["hold_days"]:
                reason = f"held {days_held}d ≥ {cfg['hold_days']}"
            elif upnl_pct <= cfg["stop_pct"]:
                reason = f"stop {upnl_pct:.1%}"
            elif upnl_pct >= cfg["target_pct"]:
                reason = f"target {upnl_pct:.1%}"
            elif sym not in sleeve_pick_symbols and days_held >= 1:
                reason = "no longer ranked"
            if reason:
                sells.append({"symbol": sym, "qty": pos["qty"], "reason": reason,
                              "unrealized_pl": pos["unrealized_pl"], "days_held": days_held})

        # ── Entries.
        selling_set = {s["symbol"] for s in sells}
        for p in sleeve_picks:
            sym = p["symbol"]
            if sym in held_by_symbol and sym not in selling_set:
                # Already in this or another sleeve — never double up.
                skipped.append({"symbol": sym, "reason": "already held"}); continue
            price = p.get("current_price") or 0
            if price <= 0:
                skipped.append({"symbol": sym, "reason": "no current_price"}); continue
            qty = int(per_position_target / price)
            if qty < 1:
                skipped.append({"symbol": sym,
                                "reason": f"price ${price:.2f} > sleeve slot ${per_position_target:.0f}"})
                continue
            stop = round(price * (1 + cfg["stop_pct"]), 2)
            tgt = round(price * (1 + cfg["target_pct"]), 2)
            buys.append({"symbol": sym, "qty": qty, "ref_price": price,
                         "stop_loss": stop, "take_profit": tgt,
                         "tier": p.get("tier"), "rationale": (p.get("rationale") or "")[:100]})

        plans[sleeve_name] = {
            "sleeve": sleeve_name, "config": cfg,
            "equity_allocated": sleeve_equity, "target_capital": target_capital,
            "per_position_target": per_position_target, "n_slots": n_slots,
            "sells": sells, "buys": buys, "skipped": skipped,
        }

    return {"plans": plans, "equity": equity}


def plan_close_window(account: dict, positions: list, state: dict) -> dict:
    """Run at 15:55 ET. Force-closes every daytrade-tagged position."""
    entries = state.get("entries", {})
    sells = []
    for pos in positions:
        sym = pos["symbol"]
        sleeve = entries.get(sym, {}).get("sleeve")
        if sleeve == "daytrade":
            sells.append({
                "symbol": sym, "qty": pos["qty"],
                "reason": "daytrade EOD force-close",
                "unrealized_pl": pos["unrealized_pl"],
            })
    return {"sells": sells, "buys": [], "sleeve": "daytrade"}


# ── Order submission ──

def submit_buy(api: Alpaca, b: dict, sleeve_name: str) -> None:
    """Submit a bracketed market buy. If the bracket itself is rejected
    because the stop/target collides with the current price (overnight
    gap moved the underlying past one of the levels we computed from
    yesterday's close), fall back to a plain market buy and log the
    bracket-loss. Per-symbol "no bracket" positions are still managed
    by the rebalance and close-window logic — they just don't have an
    in-market stop/target sitting at Alpaca.

    Encode sleeve in client_order_id ("<sleeve>-<sym>-<YYYYMMDD>") so
    /api/portfolio can attribute positions to sleeves without needing
    research/trader_state.json deployed to Railway. The 30-day Alpaca
    uniqueness window doesn't conflict because we suffix with the date.
    """
    cfg = SLEEVES[sleeve_name]
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    cid = f"{sleeve_name}-{b['symbol']}-{today}"
    body = {
        "symbol": b["symbol"], "qty": str(b["qty"]),
        "side": "buy", "type": "market", "time_in_force": "day",
        "order_class": "bracket",
        "stop_loss": {"stop_price": str(b["stop_loss"])},
        "take_profit": {"limit_price": str(b["take_profit"])},
        "client_order_id": cid,
    }
    try:
        api.submit(body)
        return
    except RuntimeError as e:
        msg = str(e)
        # Alpaca's bracket-collision rejections all come back HTTP 422
        # with messages mentioning take_profit / stop_loss / base_price.
        # Anything else is a real failure — re-raise.
        if "422" not in msg or not any(t in msg for t in ("take_profit", "stop_loss", "base_price")):
            raise

    # Bracket-collision fallback: submit plain market buy.
    fallback = {
        "symbol": b["symbol"], "qty": str(b["qty"]),
        "side": "buy", "type": "market", "time_in_force": "day",
        "client_order_id": cid + "-nobracket",
    }
    log.warning("bracket rejected for %s (gap moved past stop/target) — submitting plain market buy", b["symbol"])
    api.submit(fallback)


def execute_sleeve_plan(api: Alpaca, plan: dict, state: dict, sleeve_name: str) -> dict:
    executed, failed = [], []
    for s in plan["sells"]:
        try:
            api.close_position(s["symbol"])
            executed.append({"side": "sell", **s})
            state.setdefault("entries", {}).pop(s["symbol"], None)
        except Exception as e:
            failed.append({"side": "sell", "symbol": s["symbol"], "error": str(e)})
    for b in plan["buys"]:
        try:
            submit_buy(api, b, sleeve_name)
            executed.append({"side": "buy", **b})
            state.setdefault("entries", {})[b["symbol"]] = {
                "sleeve": sleeve_name,
                "opened_at": datetime.now(timezone.utc).isoformat(),
                "ref_price": b["ref_price"], "qty": b["qty"],
            }
        except Exception as e:
            failed.append({"side": "buy", "symbol": b["symbol"], "error": str(e)})
    return {"executed": executed, "failed": failed}


# ── Printing ──

def print_open_plan(combined: dict) -> None:
    print(f"\n=== Open-window plan ===  total equity ${combined['equity']:,.0f}")
    for name in SLEEVE_NAMES:
        p = combined["plans"][name]
        print(f"\n  [{name}]  ${p['equity_allocated']:,.0f} allocated ({p['config']['leverage']}× → ${p['target_capital']:,.0f}), {p['n_slots']} slots @ ${p['per_position_target']:,.0f}")
        for s in p["sells"]:
            print(f"    SELL {s['symbol']:>6} qty={s['qty']:>5} held={s.get('days_held','?'):>2}d  upnl=${s['unrealized_pl']:>+9}  {s['reason']}")
        for b in p["buys"]:
            print(f"    BUY  {b['symbol']:>6} qty={b['qty']:>5} @ ${b['ref_price']:>7.2f}  stop=${b['stop_loss']:>7.2f}  tgt=${b['take_profit']:>7.2f}  [{b['tier']}]")
        if p["skipped"]:
            for s in p["skipped"][:4]:
                print(f"    skip {s['symbol']:>6}  {s['reason']}")
    print()


def print_close_plan(plan: dict) -> None:
    print(f"\n=== Close-window plan === [daytrade EOD]")
    if not plan["sells"]:
        print("  no daytrade positions to close.")
    for s in plan["sells"]:
        print(f"  SELL {s['symbol']:>6} qty={s['qty']:>5}  upnl=${s['unrealized_pl']:>+9}  {s['reason']}")
    print()


# ── Subcommands ──

def cmd_status(api: Alpaca) -> int:
    clock = api.clock()
    acct = api.account()
    pos = api.positions()
    state = load_state()
    entries = state.get("entries", {})
    print(f"market.is_open={clock.get('is_open')}  next_open={clock.get('next_open')}  next_close={clock.get('next_close')}")
    eq = float(acct["equity"]); cash = float(acct["cash"]); bp = float(acct["buying_power"])
    print(f"account: equity=${eq:,.0f}  cash=${cash:,.0f}  buying_power=${bp:,.0f}  multiplier={acct.get('multiplier')}×")

    by_sleeve: dict[str, list] = {n: [] for n in SLEEVE_NAMES}
    by_sleeve["unattributed"] = []
    for p in pos:
        sleeve = entries.get(p["symbol"], {}).get("sleeve") or "unattributed"
        by_sleeve.setdefault(sleeve, []).append(p)
    for name in SLEEVE_NAMES + ["unattributed"]:
        plist = by_sleeve.get(name, [])
        if not plist: continue
        mv = sum(float(p["market_value"]) for p in plist)
        upnl = sum(float(p["unrealized_pl"]) for p in plist)
        print(f"\n[{name}] {len(plist)} positions  mv=${mv:,.0f}  upnl=${upnl:+,.0f}")
        for p in plist:
            print(f"  {p['symbol']:>6} qty={p['qty']:>5} avg=${float(p['avg_entry_price']):>7.2f} "
                  f"mkt=${float(p['market_value']):>10,.0f} upnl=${float(p['unrealized_pl']):>+9,.0f} "
                  f"({float(p['unrealized_plpc'])*100:+.1f}%)")
    return 0


def cmd_dry(api: Alpaca, window: str) -> int:
    picks = load_picks()
    if not picks:
        print("no picks — run engine first"); return 1
    acct = api.account(); pos = api.positions(); state = load_state()
    if window == "open":
        plan = plan_open_window(picks, acct, pos, state); print_open_plan(plan)
    elif window == "close":
        plan = plan_close_window(acct, pos, state); print_close_plan(plan)
    print("(dry-run; no orders submitted)")
    return 0


def cmd_live(api: Alpaca, window: str) -> int:
    clock = api.clock()
    if not clock.get("is_open"):
        # Exit 0 (not 1) so the GH-Action issue-on-failure tripwire doesn't
        # fire when the cron lands a few minutes before market open. The
        # trader simply refuses to trade until the market is actually
        # open; that's correct, not an error.
        print(f"market closed — refusing (clean exit). next_open={clock.get('next_open')}")
        return 0
    acct = api.account()
    if acct.get("trading_blocked"):
        print("trading_blocked=true on account"); return 2
    state = load_state()

    # Portfolio drawdown circuit breaker. Looks at today's equity vs
    # last_equity (yesterday close per Alpaca semantics).
    last_eq = float(acct.get("last_equity", 0))
    eq = float(acct["equity"])
    if last_eq > 0 and (eq - last_eq) / last_eq <= PORTFOLIO_DRAWDOWN_HALT_PCT:
        log.error("circuit breaker: portfolio down %.1f%% intraday (eq=%s last=%s) — closing everything",
                  (eq - last_eq) / last_eq * 100, eq, last_eq)
        for p in api.positions():
            try: api.close_position(p["symbol"])
            except Exception as e: log.error("halt-close %s failed: %s", p["symbol"], e)
        state["entries"] = {}; save_state(state)
        return 3

    pos = api.positions()
    if window == "open":
        picks = load_picks()
        if not picks: print("no picks"); return 4
        combined = plan_open_window(picks, acct, pos, state)
        print_open_plan(combined)
        for name in SLEEVE_NAMES:
            p = combined["plans"][name]
            print(f"\n>>> SUBMITTING [{name}] {len(p['sells'])} sells + {len(p['buys'])} buys")
            r = execute_sleeve_plan(api, p, state, name)
            for f in r["failed"]:
                print(f"  FAIL {f['side']} {f['symbol']}: {f['error']}")
        save_state(state)
        return 0
    elif window == "close":
        plan = plan_close_window(acct, pos, state); print_close_plan(plan)
        if plan["sells"]:
            print(f"\n>>> CLOSING {len(plan['sells'])} daytrade positions")
            r = execute_sleeve_plan(api, plan, state, "daytrade")
            for f in r["failed"]:
                print(f"  FAIL {f['side']} {f['symbol']}: {f['error']}")
            save_state(state)
        return 0
    return 5


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["status", "dry", "live"])
    p.add_argument("--window", choices=["open", "close"], default="open",
                   help="open=swing rebalance + daytrade entries; close=daytrade EOD exits")
    args = p.parse_args()
    env = {**os.environ, **load_env()}
    if not env.get("ALPACA_API_KEY"):
        print("ALPACA_API_KEY missing", file=sys.stderr); return 1
    api = Alpaca(env)
    if args.mode == "status": return cmd_status(api)
    if args.mode == "dry":    return cmd_dry(api, args.window)
    if args.mode == "live":   return cmd_live(api, args.window)


if __name__ == "__main__":
    sys.exit(main())
