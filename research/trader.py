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
#
# Three disjoint sleeves, equity-share = each sleeve's fraction of total
# equity (must sum to ≤ 1). Rank ranges are picks[start:end) and must
# not overlap or Alpaca's one-position-per-symbol model would collapse
# the buys with conflicting brackets. Top-5 (momentum) is the highest
# conviction and gets the tightest hold + brackets; swing takes ranks
# 5-15 with a 5-day cycle; daytrade picks up 15-25 for the intraday
# rotation.
SLEEVES = {
    "momentum": {
        "rank_range": (0, 5),          # picks[0..5) — top 5 by score
        "hold_days": 3,                # tight cycle on highest-conviction
        "stop_pct": -0.05,             # tighter than swing, looser than daytrade
        "target_pct": 0.10,             # quicker take-profit
        "leverage": 1.5,               # of equity_share
        "equity_share": 1/3,           # equal third of total equity
        "intraday_only": False,
    },
    "swing": {
        "rank_range": (5, 15),         # picks[5..15) — ranks 6-15
        "hold_days": 5,                # exit after 5 trading days
        "stop_pct": -0.07,             # bracket stop
        "target_pct": 0.15,            # bracket target
        "leverage": 1.5,               # of equity_share
        "equity_share": 1/3,
        "intraday_only": False,
    },
    "daytrade": {
        "rank_range": (15, 25),        # picks[15..25) — open-window draw, defines slot count
        "rotation_pool_range": (15, 50),  # picks[15..50) — deeper pool for intraday refills
        "hold_days": 0,                # exit same day at 15:55 ET
        "stop_pct": -0.03,             # tighter stop, intraday vol is small
        "target_pct": 0.05,             # +5% target — realistic 1d top-tail
        "leverage": 1.0,               # 1x — net-negative EV per backtest
        "equity_share": 1/3,
        "intraday_only": True,
        # Pyramid scale-out — split each entry into N tranches with
        # progressively higher take-profit targets but a single shared
        # stop. We submit N separate bracket orders, each for 1/N of the
        # position. When price hits target_1, tranche-1 closes and the
        # remaining tranches stay open. When stop hits, all tranches
        # close at the same level. Net effect: capture small wins on
        # weak moves AND let the rest run if it keeps going. Trade
        # count per name goes from 2 → up to N+1 fills.
        "pyramid_targets": [0.015, 0.030, 0.050],   # +1.5% / +3% / +5%
    },
}
SLEEVE_NAMES = list(SLEEVES.keys())  # ['momentum', 'swing', 'daytrade']

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

    def latest_trade(self, symbol: str) -> float | None:
        """Last printed trade price from Alpaca's market-data API. Lives at
        data.alpaca.markets, NOT paper-api.alpaca.markets — same creds work.
        Returns None on any error so the caller can fall back to ref_price."""
        url = f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest"
        req = Request(url, headers=self.headers, method="GET")
        try:
            with urlopen(req, timeout=10) as resp:
                d = json.loads(resp.read())
            return float((d.get("trade") or {}).get("p") or 0) or None
        except (HTTPError, Exception):
            return None


# ── State ──

def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"entries": {}}
    try: return json.loads(STATE_PATH.read_text())
    except Exception: return {"entries": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ── Trade journal (evidence ledger) ──
#
# Accumulates one row per trader event so the track-record page can show
# real fills with sleeve attribution, P&L per close, and (eventually)
# regime/NN annotations for ablation. Lives in runtime_data/ rather than
# research/ because runtime_data/ ships to Railway without gitignore
# tricks and the api layer reads from there.

JOURNAL_PATH = ROOT / "runtime_data" / "trade_journal.json"


# ── Discord webhook (optional alerts) ──
#
# Set DISCORD_WEBHOOK_URL in Railway env (and mirror to GH Actions
# secrets so trader.yml has it). Best-effort delivery — any failure
# is logged and swallowed so a webhook outage never blocks a trade.

def discord_post(content: str, *, embed: dict | None = None) -> None:
    """Send a single message to the configured Discord webhook. Silent
    no-op when the env var isn't set, so trader.py runs fine without
    Discord configured at all."""
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        return
    body: dict = {"content": content}
    if embed:
        body["embeds"] = [embed]
    try:
        req = Request(url, data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"},
                      method="POST")
        with urlopen(req, timeout=8):
            pass
    except (HTTPError, Exception) as e:
        log.warning("discord webhook failed: %s", e)


def discord_alert_open_summary(executed_per_sleeve: dict, failed: list[dict],
                                acct: dict) -> None:
    """Posted right after the open window finishes submitting orders.
    One embed per sleeve plus a roll-up of any rejections."""
    eq = float(acct.get("equity") or 0)
    last_eq = float(acct.get("last_equity") or eq)
    fields = []
    for sleeve, exec_list in executed_per_sleeve.items():
        if not exec_list:
            continue
        syms = ", ".join(sorted(b["symbol"] for b in exec_list if b.get("side") == "buy"))[:1024]
        fields.append({
            "name": f"{sleeve} ({sum(1 for b in exec_list if b.get('side') == 'buy')} buys)",
            "value": syms or "—",
            "inline": False,
        })
    if failed:
        fields.append({
            "name": f"⚠ {len(failed)} rejected",
            "value": "\n".join(f"{f['symbol']}: {f.get('error','')[:90]}" for f in failed)[:1024],
            "inline": False,
        })
    embed = {
        "title": "Open window · paper trader",
        "description": f"Equity ${eq:,.0f} · prior close ${last_eq:,.0f}",
        "color": 0x6ee7b7 if not failed else 0xfca5a5,
        "fields": fields,
        "footer": {"text": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")},
    }
    discord_post("📈 Trader open-window complete", embed=embed)


def discord_alert_close_summary(realized_total: float, n_closed: int,
                                 acct: dict) -> None:
    """Posted at the end of the close window. realized_total is the
    sum of (sell - buy) * qty over today's closes; n_closed is how
    many distinct daytrade exits fired."""
    eq = float(acct.get("equity") or 0)
    last_eq = float(acct.get("last_equity") or eq)
    day_chg = eq - last_eq
    color = 0x6ee7b7 if (day_chg or 0) >= 0 else 0xfca5a5
    sign = "+" if day_chg >= 0 else "−"
    rs = "+" if realized_total >= 0 else "−"
    embed = {
        "title": "Close window · paper trader",
        "description": (
            f"Day change: **{sign}${abs(day_chg):,.0f}**  "
            f"({((day_chg / last_eq * 100) if last_eq else 0):+.2f}%)\n"
            f"Realized today: **{rs}${abs(realized_total):,.0f}** across {n_closed} closes\n"
            f"Equity: ${eq:,.0f}"
        ),
        "color": color,
        "footer": {"text": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")},
    }
    discord_post("📊 Trader close-window complete", embed=embed)


def load_journal() -> list[dict]:
    if not JOURNAL_PATH.exists():
        return []
    try:
        d = json.loads(JOURNAL_PATH.read_text())
        return d.get("rows", []) if isinstance(d, dict) else (d if isinstance(d, list) else [])
    except Exception:
        return []


def save_journal(rows: list[dict]) -> None:
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "n_rows": len(rows),
        "rows": rows,
    }
    JOURNAL_PATH.write_text(json.dumps(payload, indent=2, default=str))


def journal_entry_buy(b: dict, sleeve: str, *, live_price: float | None,
                      stop: float, target: float, result: str) -> dict:
    """Build a single 'buy' row. result ∈ {submitted, nobracket_fallback, failed}."""
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "buy",
        "sleeve": sleeve,
        "symbol": b["symbol"],
        "qty": b["qty"],
        "ref_price": b.get("ref_price"),
        "live_price": live_price,
        "stop": stop,
        "target": target,
        "tier": b.get("tier"),
        "rationale": (b.get("rationale") or "")[:200],
        "result": result,
    }


def journal_alpaca_fills(api: "Alpaca", entries: dict) -> list[dict]:
    """At close-window time, walk today's FILL activities and synthesise
    journal rows for sells. We FIFO-pair sells with the buys recorded in
    state.entries to compute per-trade P&L. Bracket child fills (stop/
    target hits during the day) are captured here too — they don't pass
    through trader.py's submit_sell path so this is the only place they
    surface in the journal."""
    today_iso = datetime.now(timezone.utc).date().isoformat()
    try:
        url = f"{api.base}/account/activities/FILL?date={today_iso}"
        req = Request(url, headers=api.headers, method="GET")
        with urlopen(req, timeout=15) as resp:
            activities = json.loads(resp.read())
    except (HTTPError, Exception) as e:
        log.warning("journal: failed to fetch FILL activities: %s", e)
        return []

    if not isinstance(activities, list):
        return []

    chrono = sorted(
        (a for a in activities if a.get("type") in ("FILL", "PARTIAL_FILL")
         and (a.get("side") or "").lower() == "sell"),
        key=lambda a: a.get("transaction_time") or a.get("submitted_at") or "",
    )
    rows: list[dict] = []
    # We don't know the original buy-side fill price for symbols that
    # opened today and exit today, so use state.entries.ref_price as the
    # cost-basis proxy. For positions opened on prior days we don't have
    # the entry in state any more (popped on rebalance), so use Alpaca's
    # filled_avg_price from the buy activity if present in the same list.
    buy_rows = [a for a in activities if (a.get("side") or "").lower() == "buy"]
    buy_by_sym: dict = {}
    for b in buy_rows:
        s = b.get("symbol")
        if s and s not in buy_by_sym:
            buy_by_sym[s] = float(b.get("price") or 0)
    for a in chrono:
        sym = a.get("symbol")
        if not sym:
            continue
        try:
            qty = float(a.get("qty") or 0)
            sell_price = float(a.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0 or sell_price <= 0:
            continue
        entry = entries.get(sym, {})
        cost = buy_by_sym.get(sym) or entry.get("ref_price") or 0
        pnl = (sell_price - cost) * qty if cost else None
        rows.append({
            "ts": a.get("transaction_time"),
            "event": "sell",
            "sleeve": entry.get("sleeve") or "unattributed",
            "symbol": sym,
            "qty": qty,
            "buy_price": cost or None,
            "sell_price": sell_price,
            "pnl": pnl,
            "exit_reason": a.get("order_type") or "unknown",
            "order_id": a.get("order_id"),
        })
    return rows


# ── Picks ──

def load_picks() -> list[dict]:
    if not PICKS_PATH.exists():
        log.error("portfolio_picks.json missing — run the engine first")
        return []
    d = json.loads(PICKS_PATH.read_text())
    picks = [p for p in (d.get("picks") or []) if p.get("tier") in TRADE_TIERS]
    picks.sort(key=lambda p: p.get("ensemble_score") or p.get("expected_return") or 0, reverse=True)
    return picks


def load_rotation_pool() -> list[dict]:
    """Wider candidate pool for the daytrade rotator. Combines the scored
    `picks` (top-10 per tier = 30) with the `asymmetric_picks` (top-20 by
    moonshot score) deduped by symbol, then re-sorts by score. The picks
    file caps each tier at 10 names, so a naive picks[15:50] slice
    returns only 15 candidates — and after the same-day re-entry guard
    cycles through them once the rotator runs dry. Combining tiers gives
    ~45-50 unique candidates per session, enough to keep the 12-tick
    rotation cron supplied.

    Asymmetric-tier picks may also be tier-tagged conservative/moderate/
    aggressive (the `in_asymmetric` flag is what makes them asymmetric),
    so the TRADE_TIERS filter still applies."""
    if not PICKS_PATH.exists():
        log.error("portfolio_picks.json missing — run the engine first")
        return []
    d = json.loads(PICKS_PATH.read_text())
    base = [p for p in (d.get("picks") or []) if p.get("tier") in TRADE_TIERS]
    asym = [p for p in (d.get("asymmetric_picks") or []) if p.get("tier") in TRADE_TIERS]
    seen = {p["symbol"] for p in base}
    extra = [p for p in asym if p["symbol"] not in seen]
    combined = base + extra
    combined.sort(key=lambda p: p.get("ensemble_score") or p.get("expected_return") or 0, reverse=True)
    return combined


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


def equity_per_sleeve(equity: float, sleeve_name: str = "swing") -> float:
    """Each sleeve's share is configured in SLEEVES[*]['equity_share']
    (a fraction of total equity). Defaults to even-split when a sleeve
    forgets to declare a share — better than crashing the planner."""
    cfg = SLEEVES.get(sleeve_name, {})
    share = cfg.get("equity_share")
    if share is None:
        share = 1 / max(1, len(SLEEVES))
    return equity * share


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
        sleeve_equity = equity_per_sleeve(equity, sleeve_name)
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
        # Cap new buys at n_slots − (held positions in this sleeve that
        # AREN'T being sold this run). Without this cap, sleeve transitions
        # (e.g. swing's rank range moving from 0-10 to 5-15) would buy the
        # entire new range on top of unexpired old positions, double-
        # deploying the sleeve. The cap means we only fill up to the
        # configured slot count; old positions just bleed off naturally
        # as their hold cycles complete.
        selling_set = {s["symbol"] for s in sells}
        held_after_sells = sum(1 for sym in sleeve_held if sym not in selling_set)
        buy_budget = max(0, n_slots - held_after_sells)
        if buy_budget < n_slots:
            skipped.append({"symbol": "(cap)",
                            "reason": f"sleeve at {held_after_sells}/{n_slots} after exits — buy_budget={buy_budget}"})
        for p in sleeve_picks:
            if len(buys) >= buy_budget:
                break
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


def plan_rotate_window(picks: list[dict], account: dict, positions: list,
                        state: dict) -> dict:
    """Fires every 30 min during US market hours (14:00-19:30 UTC). Daytrade
    sleeve only — refills any empty slots from a deeper pool than the open
    window draws (`rotation_pool_range` vs `rank_range`). Brackets handle
    exits in-market; we don't issue sells from the rotator. Same-day re-entry
    guard via `state.traded_today` prevents the rotator from cycling back
    into a name that already filled and stopped/took-profit earlier today.
    """
    cfg = SLEEVES["daytrade"]
    equity = float(account["equity"])
    sleeve_equity = equity_per_sleeve(equity, "daytrade")
    target_capital = sleeve_equity * cfg["leverage"]
    n_slots = cfg["rank_range"][1] - cfg["rank_range"][0]
    per_position_target = target_capital / n_slots

    held_by_symbol = {p["symbol"]: p for p in positions}
    entries = state.get("entries", {})
    sleeve_held = {sym: pos for sym, pos in held_by_symbol.items()
                   if entries.get(sym, {}).get("sleeve") == "daytrade"}
    n_held = len(sleeve_held)
    n_free = max(0, n_slots - n_held)

    today_iso = datetime.now(timezone.utc).date().isoformat()
    traded_map = state.setdefault("traded_today", {})
    # Prune state to today's date only — yesterday's traded list is stale
    # (positions force-closed at 19:55 UTC; symbols are eligible again today).
    for k in list(traded_map.keys()):
        if k != today_iso:
            del traded_map[k]
    traded_today = set(traded_map.setdefault(today_iso, []))

    pool_lo, pool_hi = cfg.get("rotation_pool_range", cfg["rank_range"])
    pool = picks[pool_lo:pool_hi]

    skipped: list[dict] = []
    candidates: list[dict] = []
    held_syms = set(sleeve_held.keys())
    for p in pool:
        sym = p["symbol"]
        if sym in held_syms:
            continue  # silent — sleeve already holds this name
        if sym in held_by_symbol:
            skipped.append({"symbol": sym, "reason": "held in another sleeve"})
            continue
        if sym in traded_today:
            skipped.append({"symbol": sym, "reason": "already traded today"})
            continue
        if (p.get("current_price") or 0) <= 0:
            skipped.append({"symbol": sym, "reason": "no current_price"})
            continue
        candidates.append(p)

    buys: list[dict] = []
    for p in candidates:
        if len(buys) >= n_free:
            break
        price = p["current_price"]
        qty = int(per_position_target / price)
        if qty < 1:
            skipped.append({"symbol": p["symbol"],
                            "reason": f"price ${price:.2f} > slot ${per_position_target:.0f}"})
            continue
        stop = round(price * (1 + cfg["stop_pct"]), 2)
        tgt  = round(price * (1 + cfg["target_pct"]), 2)
        buys.append({"symbol": p["symbol"], "qty": qty, "ref_price": price,
                     "stop_loss": stop, "take_profit": tgt,
                     "tier": p.get("tier")})

    return {
        "config": cfg,
        "equity_allocated": sleeve_equity,
        "target_capital": target_capital,
        "n_slots": n_slots,
        "per_position_target": per_position_target,
        "n_held": n_held,
        "n_free": n_free,
        "sells": [],   # rotator never sells — brackets at Alpaca handle exits
        "buys": buys,
        "skipped": skipped,
    }


def _record_traded_today(state: dict, executed: list) -> None:
    """Append every successful daytrade buy symbol to state.traded_today
    keyed by today's UTC date, so the rotator's same-day re-entry guard
    catches names already filled (and possibly bracket-exited) earlier."""
    today_iso = datetime.now(timezone.utc).date().isoformat()
    traded_map = state.setdefault("traded_today", {})
    for k in list(traded_map.keys()):
        if k != today_iso:
            del traded_map[k]
    bucket = traded_map.setdefault(today_iso, [])
    for x in executed:
        if x.get("side") == "buy" and x.get("symbol") and x["symbol"] not in bucket:
            bucket.append(x["symbol"])


# ── Order submission ──

def submit_buy(api: Alpaca, b: dict, sleeve_name: str,
               journal_rows: list | None = None) -> None:
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

    journal_rows is a list the caller mutates; we append a buy row
    after submission so the trade journal captures each entry with
    bracket math (live vs ref price) and result (submitted /
    nobracket_fallback).
    """
    cfg = SLEEVES[sleeve_name]
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    cid = f"{sleeve_name}-{b['symbol']}-{today}"

    # Refresh bracket math against the live quote. The plan was built off
    # current_price from portfolio_picks.json which reflects yesterday's
    # close — gap-ups/downs put the precomputed stop/target on the wrong
    # side of the live price and Alpaca 422s. Re-anchoring on the live
    # last-trade price closes that window. If the data API call fails we
    # fall through to the original (yesterday's-close-based) levels.
    live = api.latest_trade(b["symbol"])
    ref = live or float(b["ref_price"])
    stop_price = round(ref * (1 + cfg["stop_pct"]), 2)
    take_price = round(ref * (1 + cfg["target_pct"]), 2)

    # Pyramid scale-out: if this sleeve is configured with multiple
    # take-profit targets, split the position across N bracket orders
    # with shared stop but progressively higher targets. Each bracket is
    # a separate Alpaca order so a target hit on tranche 1 doesn't
    # cancel the open tranches. Any 422 here falls back to the single-
    # bracket path below — pyramid is best-effort.
    pyramid = cfg.get("pyramid_targets")
    if pyramid and len(pyramid) > 1:
        full_qty = int(b["qty"])
        n = len(pyramid)
        tranches: list[int] = [full_qty // n] * n
        tranches[-1] += full_qty - sum(tranches)  # remainder on last tranche
        all_ok = True
        for i, (tr_qty, tr_target_pct) in enumerate(zip(tranches, pyramid)):
            if tr_qty <= 0:
                continue
            tr_take = round(ref * (1 + tr_target_pct), 2)
            tr_body = {
                "symbol": b["symbol"], "qty": str(tr_qty),
                "side": "buy", "type": "market", "time_in_force": "day",
                "order_class": "bracket",
                "stop_loss": {"stop_price": str(stop_price)},
                "take_profit": {"limit_price": str(tr_take)},
                "client_order_id": f"{cid}-p{i}",
            }
            try:
                api.submit(tr_body)
                if journal_rows is not None:
                    row = journal_entry_buy(
                        b, sleeve_name, live_price=live, stop=stop_price,
                        target=tr_take, result="submitted")
                    row["qty"] = tr_qty
                    row["tranche"] = i
                    journal_rows.append(row)
            except RuntimeError as e:
                msg = str(e)
                if "422" in msg and any(t in msg for t in ("take_profit", "stop_loss", "base_price")):
                    log.warning("pyramid tranche %d for %s rejected — will fall back to single bracket", i, b["symbol"])
                    all_ok = False
                    break
                raise
        if all_ok:
            return
        # Fall through to single-bracket path below.

    body = {
        "symbol": b["symbol"], "qty": str(b["qty"]),
        "side": "buy", "type": "market", "time_in_force": "day",
        "order_class": "bracket",
        "stop_loss": {"stop_price": str(stop_price)},
        "take_profit": {"limit_price": str(take_price)},
        "client_order_id": cid,
    }
    try:
        api.submit(body)
        if journal_rows is not None:
            journal_rows.append(journal_entry_buy(
                b, sleeve_name, live_price=live, stop=stop_price,
                target=take_price, result="submitted"))
        return
    except RuntimeError as e:
        msg = str(e)
        # Alpaca's bracket-collision rejections all come back HTTP 422
        # with messages mentioning take_profit / stop_loss / base_price.
        # Anything else is a real failure — re-raise.
        if "422" not in msg or not any(t in msg for t in ("take_profit", "stop_loss", "base_price")):
            raise

    # Bracket-collision fallback: submit plain market buy. Reachable when
    # latest_trade() returned None or the live price moved between the
    # quote and the submit (fast tape).
    fallback = {
        "symbol": b["symbol"], "qty": str(b["qty"]),
        "side": "buy", "type": "market", "time_in_force": "day",
        "client_order_id": cid + "-nobracket",
    }
    log.warning("bracket rejected for %s (live=%s ref=%s stop=%s tgt=%s) — submitting plain market buy",
                b["symbol"], live, b["ref_price"], stop_price, take_price)
    api.submit(fallback)
    if journal_rows is not None:
        journal_rows.append(journal_entry_buy(
            b, sleeve_name, live_price=live, stop=stop_price,
            target=take_price, result="nobracket_fallback"))


def execute_sleeve_plan(api: Alpaca, plan: dict, state: dict, sleeve_name: str,
                         journal_rows: list | None = None) -> dict:
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
            submit_buy(api, b, sleeve_name, journal_rows=journal_rows)
            executed.append({"side": "buy", **b})
            state.setdefault("entries", {})[b["symbol"]] = {
                "sleeve": sleeve_name,
                "opened_at": datetime.now(timezone.utc).isoformat(),
                "ref_price": b["ref_price"], "qty": b["qty"],
            }
        except Exception as e:
            failed.append({"side": "buy", "symbol": b["symbol"], "error": str(e)})
            if journal_rows is not None:
                journal_rows.append({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "event": "buy_failed",
                    "sleeve": sleeve_name,
                    "symbol": b["symbol"],
                    "qty": b.get("qty"),
                    "error": str(e)[:200],
                })
    return {"executed": executed, "failed": failed}


# ── Printing ──

def print_open_plan(combined: dict) -> None:
    print(f"\n=== Open-window plan ===  total equity ${combined['equity']:,.0f}")
    for name in SLEEVE_NAMES:
        p = combined["plans"][name]
        print(f"\n  [{name}]  ${p['equity_allocated']:,.0f} allocated ({p['config']['leverage']}× → ${p['target_capital']:,.0f}), {p['n_slots']} slots @ ${p['per_position_target']:,.0f}")
        for s in p["sells"]:
            print(f"    SELL {s['symbol']:>6} qty={s['qty']:>5} held={s.get('days_held','?'):>2}d  upnl=${float(s['unrealized_pl']):>+9.2f}  {s['reason']}")
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
        print(f"  SELL {s['symbol']:>6} qty={s['qty']:>5}  upnl=${float(s['unrealized_pl']):>+9.2f}  {s['reason']}")
    print()


def print_rotate_plan(plan: dict) -> None:
    print(f"\n=== Rotate-window plan === [daytrade]")
    print(f"  slots: {plan['n_held']}/{plan['n_slots']} filled, {plan['n_free']} free  "
          f"(${plan['per_position_target']:,.0f} per slot)")
    if not plan["buys"]:
        if plan["n_free"] == 0:
            print("  sleeve full — no rotation entries this tick")
        else:
            print(f"  {plan['n_free']} free slots but no eligible candidates from pool")
    for b in plan["buys"]:
        print(f"    BUY  {b['symbol']:>6} qty={b['qty']:>5} @ ${b['ref_price']:>7.2f}  "
              f"stop=${b['stop_loss']:>7.2f}  tgt=${b['take_profit']:>7.2f}  [{b['tier']}]")
    if plan["skipped"]:
        for s in plan["skipped"][:6]:
            print(f"    skip {s['symbol']:>6}  {s['reason']}")
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
    elif window == "rotate":
        # Rotator uses a wider candidate pool than the open window —
        # picks + asymmetric_picks deduped — so 12 ticks/day don't dry up.
        rotation_picks = load_rotation_pool()
        plan = plan_rotate_window(rotation_picks, acct, pos, state)
        print_rotate_plan(plan)
    print("(dry-run; no orders submitted)")
    return 0


def cmd_live(api: Alpaca, window: str) -> int:
    clock = api.clock()
    if not clock.get("is_open"):
        # Exit 0 (not 1) so the GH-Action issue-on-failure tripwire doesn't
        # fire when the cron lands a few minutes before market open. The
        # trader simply refuses to trade until the market is actually
        # open; that's correct, not an error.
        # Exception: a close-window run that fires after 16:00 ET because
        # of GH cron slippage still needs to pull today's FILL activities
        # so bracket child fills + EOD closes reach the journal. Without
        # this the track-record page silently misses every close-window
        # that landed past EOD.
        if window == "close":
            try:
                state = load_state()
                journal_rows = load_journal()
                sell_rows_today = journal_alpaca_fills(api, state.get("entries", {}))
                if sell_rows_today:
                    print(f"market closed but journaling {len(sell_rows_today)} late FILL rows")
                    save_journal(_prune_journal(journal_rows + sell_rows_today))
            except Exception as e:
                log.warning("late-close journal pull failed: %s", e)
        print(f"market closed — refusing (clean exit). next_open={clock.get('next_open')}")
        return 0
    acct = api.account()
    if acct.get("trading_blocked"):
        print("trading_blocked=true on account"); return 2
    state = load_state()

    # Portfolio drawdown circuit breaker. Looks at today's equity vs
    # last_equity (yesterday close per Alpaca semantics).
    #
    # One-shot per day: once the breaker fires and liquidates, equity is
    # already realized at the loss. Without this guard, every subsequent
    # window (rotate, close, next open) re-evaluates eq vs last_eq, sees
    # the same drawdown, and skips trading for the rest of the session.
    # That means a single overnight gap nukes trading for the full day.
    # After firing once, mark today; subsequent runs skip the gate so the
    # rotator/swing can rebuild positions with the smaller post-breaker
    # equity. The breaker re-arms tomorrow because last_equity rolls.
    today_iso_breaker = datetime.now(timezone.utc).date().isoformat()
    breaker_fired_today = state.get("breaker_fired_date") == today_iso_breaker
    last_eq = float(acct.get("last_equity", 0))
    eq = float(acct["equity"])
    if not breaker_fired_today and last_eq > 0 and (eq - last_eq) / last_eq <= PORTFOLIO_DRAWDOWN_HALT_PCT:
        drawdown_pct = (eq - last_eq) / last_eq * 100
        log.error("circuit breaker: portfolio down %.1f%% intraday (eq=%s last=%s) — closing everything",
                  drawdown_pct, eq, last_eq)
        # Snapshot positions BEFORE closing so we can journal each one. Without
        # this, breaker liquidations vanish from /api/portfolio.closed_today
        # and the dashboard shows "0 closed today" while equity drops 3%+.
        snapshot = api.positions()
        breaker_rows: list[dict] = []
        ts = datetime.now(timezone.utc).isoformat()
        for p in snapshot:
            sym = p.get("symbol")
            qty = p.get("qty")
            avg = p.get("avg_entry_price")
            cur = p.get("current_price") or p.get("last_price")
            upl = p.get("unrealized_pl")
            sleeve = (state.get("entries", {}).get(sym, {}) or {}).get("sleeve") or "unattributed"
            close_result = "submitted"
            try:
                api.close_position(sym)
            except Exception as e:
                log.error("halt-close %s failed: %s", sym, e)
                close_result = f"failed: {str(e)[:60]}"
            breaker_rows.append({
                "ts": ts, "event": "sell", "sleeve": sleeve, "symbol": sym,
                "qty": qty, "ref_price": avg, "live_price": cur,
                "realized_pl": upl, "reason": "circuit_breaker",
                "result": close_result,
            })
        if breaker_rows:
            save_journal(_prune_journal(load_journal() + breaker_rows))
        state["entries"] = {}
        state["breaker_fired_date"] = today_iso_breaker
        save_state(state)
        # Loud Discord ping so this doesn't become a "where's my portfolio?"
        # surprise. The default Trader-complete embed only fires after the
        # normal open/close window — breaker exits before either. ntfy.sh
        # gets the GH issue via alert-on-watchdog-issue.yml.
        try:
            discord_post(
                f"🚨 **Circuit breaker fired** — portfolio down {drawdown_pct:.1f}% intraday "
                f"(eq=${eq:,.0f} vs last_close=${last_eq:,.0f}). "
                f"Closed {len(breaker_rows)} position(s): {', '.join(r['symbol'] for r in breaker_rows)}.",
                embed=None,
            )
        except Exception:
            pass
        return 3

    pos = api.positions()

    # Trade journal — accumulates buy entries during the open window and
    # picks up Alpaca FILL activity for sells during close. Persisted at
    # the end of each run; rows older than ~365 days are pruned to keep
    # the file from growing without bound.
    journal_rows = load_journal()
    new_rows: list[dict] = []

    if window == "open":
        picks = load_picks()
        if not picks: print("no picks"); return 4
        combined = plan_open_window(picks, acct, pos, state)
        print_open_plan(combined)
        executed_by_sleeve: dict[str, list] = {}
        all_failed: list[dict] = []
        for name in SLEEVE_NAMES:
            p = combined["plans"][name]
            print(f"\n>>> SUBMITTING [{name}] {len(p['sells'])} sells + {len(p['buys'])} buys")
            r = execute_sleeve_plan(api, p, state, name, journal_rows=new_rows)
            executed_by_sleeve[name] = r["executed"]
            for f in r["failed"]:
                print(f"  FAIL {f['side']} {f['symbol']}: {f['error']}")
                all_failed.append(f)
            # Tag daytrade entries into the same-day re-entry guard so the
            # rotator (running every 30 min after) won't re-buy any name
            # that opened in this batch and then bracket-exited.
            if name == "daytrade":
                _record_traded_today(state, r["executed"])
        save_state(state)
        if new_rows:
            print(f"\njournal: appended {len(new_rows)} rows")
            save_journal(_prune_journal(journal_rows + new_rows))
        discord_alert_open_summary(executed_by_sleeve, all_failed, acct)
        return 0
    elif window == "close":
        plan = plan_close_window(acct, pos, state); print_close_plan(plan)
        if plan["sells"]:
            print(f"\n>>> CLOSING {len(plan['sells'])} daytrade positions")
            r = execute_sleeve_plan(api, plan, state, "daytrade", journal_rows=new_rows)
            for f in r["failed"]:
                print(f"  FAIL {f['side']} {f['symbol']}: {f['error']}")
            save_state(state)
        # Always pull today's Alpaca FILL activities at close — captures
        # bracket child fills (stop-outs, target hits) that didn't pass
        # through trader.py's flow during the day, plus the EOD daytrade
        # closes we just submitted.
        sell_rows_today = journal_alpaca_fills(api, state.get("entries", {}))
        new_rows.extend(sell_rows_today)
        if new_rows:
            print(f"\njournal: appended {len(new_rows)} rows")
            save_journal(_prune_journal(journal_rows + new_rows))
        # Refresh acct after closes so day_change reflects realized P&L.
        try:
            acct = api.account()
        except Exception:
            pass
        realized_total = sum(float(r.get("pnl") or 0) for r in sell_rows_today)
        discord_alert_close_summary(realized_total, len(sell_rows_today), acct)
        return 0
    elif window == "rotate":
        picks = load_rotation_pool()
        if not picks: print("no picks"); return 4
        plan = plan_rotate_window(picks, acct, pos, state)
        print_rotate_plan(plan)
        if not plan["buys"]:
            # No-op runs are normal during rotation (sleeve full, no new
            # candidates, prices ineligible). Save state so the pruned
            # `traded_today` map persists, then exit clean.
            save_state(state)
            return 0
        print(f"\n>>> ROTATING [daytrade] {len(plan['buys'])} buys")
        r = execute_sleeve_plan(api, plan, state, "daytrade", journal_rows=new_rows)
        for f in r["failed"]:
            print(f"  FAIL {f['side']} {f['symbol']}: {f['error']}")
        _record_traded_today(state, r["executed"])
        save_state(state)
        if new_rows:
            print(f"\njournal: appended {len(new_rows)} rows")
            save_journal(_prune_journal(journal_rows + new_rows))
        return 0
    return 5


def _prune_journal(rows: list[dict], keep_days: int = 365) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    return [r for r in rows if (r.get("ts") or "") >= cutoff]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["status", "dry", "live"])
    p.add_argument("--window", choices=["open", "close", "rotate"], default="open",
                   help="open=swing rebalance + daytrade entries; "
                        "close=daytrade EOD exits; "
                        "rotate=intraday daytrade refill (every 30 min)")
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
