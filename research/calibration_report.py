"""Daily calibration report — predicted vs realized P&L per closed trade.

This is the "falsification harness" piece of the day-trading learning
loop. For every closed trade in trade_journal.json:

  1. Match the SELL row to its prior BUY row (by symbol, FIFO).
  2. Compute realized return: (sell_price - buy_price) / buy_price.
  3. Compute predicted return at entry: (target - buy_price) / buy_price
     where `target` is the bracket take_profit (model's projected upside
     at entry time, from picks_history).
  4. Bucket by sleeve. Report:
       n trades, win-rate, mean realized, mean predicted, residual
       (mean predicted - mean realized), p10/p50/p90 of residuals

The residual distribution is the falsification signal. If realized
is consistently below predicted, the model is over-promising — its
target prices are too aggressive. If realized > predicted, it's
under-promising — could size up. Drift over time (rolling 30-day
mean residual) tells us when the signal is decaying.

This is data-poor today (most positions are still open) but the
script is idempotent — running it nightly accumulates a time-series
of calibration_<date>.json files we can chart later.

Output: runtime_data/calibration_<YYYY-MM-DD>.json
        runtime_data/calibration_latest.json (pointer copy)
"""

from __future__ import annotations

import json
import logging
import statistics
import sys
import time
from collections import defaultdict, deque
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("calibration")

JOURNAL = ROOT / "runtime_data" / "trade_journal.json"
OUT_DIR = ROOT / "runtime_data"


def load_journal_rows() -> list[dict]:
    if not JOURNAL.exists():
        return []
    try:
        d = json.loads(JOURNAL.read_text())
    except Exception as e:
        log.error("trade journal unreadable: %s", e)
        return []
    if isinstance(d, dict) and "rows" in d:
        return d.get("rows") or []
    if isinstance(d, list):
        return d
    return []


def match_buys_to_sells(rows: list[dict]) -> list[dict]:
    """FIFO-match every SELL to a prior BUY for the same symbol. Returns
    one merged trade record per matched sell. Unmatched sells (sells
    with no prior buy in the journal) are skipped — they're typically
    backfilled from Alpaca activity for trades opened before the
    journaling was wired up."""
    by_sym_buys: dict[str, deque] = defaultdict(deque)
    trades: list[dict] = []
    chrono = sorted(rows, key=lambda r: r.get("ts") or "")
    for r in chrono:
        sym = r.get("symbol")
        ev = r.get("event")
        if not sym:
            continue
        if ev == "buy":
            by_sym_buys[sym].append(r)
        elif ev == "sell":
            buys = by_sym_buys.get(sym)
            if not buys:
                continue
            buy = buys.popleft()
            buy_price = (buy.get("ref_price") or buy.get("live_price")
                         or buy.get("buy_price") or r.get("buy_price"))
            sell_price = r.get("sell_price")
            target = buy.get("target")
            stop = buy.get("stop")
            if not buy_price or not sell_price:
                continue
            try:
                bp = float(buy_price)
                sp = float(sell_price)
            except (TypeError, ValueError):
                continue
            if bp <= 0:
                continue
            realized_ret = (sp - bp) / bp
            predicted_ret = (float(target) - bp) / bp if target else None
            stop_ret = (float(stop) - bp) / bp if stop else None
            sleeve = r.get("sleeve") or buy.get("sleeve") or "unattributed"
            trades.append({
                "symbol": sym,
                "sleeve": sleeve,
                "buy_ts": buy.get("ts"),
                "sell_ts": r.get("ts"),
                "buy_price": bp,
                "sell_price": sp,
                "target": float(target) if target else None,
                "stop": float(stop) if stop else None,
                "realized_ret": realized_ret,
                "predicted_ret": predicted_ret,
                "stop_ret": stop_ret,
                "residual": (predicted_ret - realized_ret) if predicted_ret is not None else None,
                "win": realized_ret > 0,
                "exit_reason": r.get("exit_reason"),
                "tier": buy.get("tier"),
            })
    return trades


def aggregate_by_sleeve(trades: list[dict]) -> dict:
    """Bucket merged trades by sleeve. For each bucket: n, wins, mean
    realized, mean predicted, residual stats. Residual = predicted -
    realized; positive means model over-promised."""
    by_sleeve: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_sleeve[t["sleeve"]].append(t)
    out: dict[str, dict] = {}
    for sleeve, ts in by_sleeve.items():
        rets = [t["realized_ret"] for t in ts]
        preds = [t["predicted_ret"] for t in ts if t["predicted_ret"] is not None]
        resids = [t["residual"] for t in ts if t["residual"] is not None]
        wins = sum(1 for t in ts if t["win"])
        bucket: dict = {
            "n": len(ts),
            "wins": wins,
            "win_rate": round(wins / len(ts), 4) if ts else 0.0,
            "mean_realized": round(statistics.fmean(rets), 5) if rets else None,
            "median_realized": round(statistics.median(rets), 5) if rets else None,
            "mean_predicted": round(statistics.fmean(preds), 5) if preds else None,
            "n_with_prediction": len(preds),
        }
        if resids:
            bucket["mean_residual"] = round(statistics.fmean(resids), 5)
            bucket["median_residual"] = round(statistics.median(resids), 5)
            if len(resids) >= 5:
                rs = sorted(resids)
                bucket["residual_p10"] = round(rs[len(rs) // 10], 5)
                bucket["residual_p90"] = round(rs[len(rs) * 9 // 10], 5)
        out[sleeve] = bucket
    return out


def main() -> None:
    rows = load_journal_rows()
    log.info("loaded %d journal rows", len(rows))
    trades = match_buys_to_sells(rows)
    log.info("matched %d closed trades", len(trades))
    by_sleeve = aggregate_by_sleeve(trades)
    n_with_pred = sum(b.get("n_with_prediction", 0) for b in by_sleeve.values())
    log.info("trades with bracket-target prediction: %d / %d", n_with_pred, len(trades))
    for sleeve, b in sorted(by_sleeve.items()):
        line = (f"  {sleeve:14} n={b['n']:>3}  win={b['win_rate']:.0%}  "
                f"realized={b.get('mean_realized')}  pred={b.get('mean_predicted')}  "
                f"resid_mean={b.get('mean_residual')}")
        log.info(line)
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_journal_rows": len(rows),
        "n_closed_trades": len(trades),
        "n_trades_with_prediction": n_with_pred,
        "by_sleeve": by_sleeve,
        "trades": trades,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today_iso = date.today().isoformat()
    (OUT_DIR / f"calibration_{today_iso}.json").write_text(json.dumps(out, indent=2))
    (OUT_DIR / "calibration_latest.json").write_text(json.dumps(out, indent=2))
    log.info("wrote runtime_data/calibration_%s.json (and _latest)", today_iso)


if __name__ == "__main__":
    main()
