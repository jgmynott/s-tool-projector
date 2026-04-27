"""Export picks_history from the runner's projector_cache.db to a JSON
file that ships to Railway. The DB itself isn't deployable (it's huge
and lives in GH Actions cache), so api.py reads this JSON when it
needs the historical pick ledger.

Schema mirrors the picks_history columns plus a few derived fields the
track-record page wants to render — current price (best-effort from
data_cache/prices) and the realized return at export time. The export
is point-in-time; the page recomputes "now" against fresher prices in
the API layer when those become available.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
DB = ROOT / "projector_cache.db"
OUT = ROOT / "runtime_data" / "picks_history.json"
PRICES_DIR = ROOT / "data_cache" / "prices"


def latest_close(sym: str) -> float | None:
    """Pull the most recent Close from the cached daily-bar CSV. Returns
    None if the CSV is missing or unreadable — caller falls back."""
    csv_path = PRICES_DIR / f"{sym}.csv"
    if not csv_path.exists():
        return None
    try:
        with csv_path.open() as fh:
            last = None
            for row in csv.DictReader(fh):
                last = row
            if last and last.get("Close"):
                return float(last["Close"])
    except Exception:
        return None
    return None


def main(lookback_days: int = 365) -> int:
    if not DB.exists():
        print(f"DB not found at {DB} — nothing to export")
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)

    since = (datetime.now(timezone.utc).date() - timedelta(days=lookback_days)).isoformat()
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT pick_date, symbol, tier, entry_price, p50_target,
                  expected_return, risk, sharpe_proxy, horizon_days,
                  rationale, sec_fundamentals_json, created_at
             FROM picks_history
            WHERE pick_date >= ?
         ORDER BY pick_date DESC, tier ASC, expected_return DESC""",
        (since,),
    ).fetchall()
    con.close()

    today = datetime.now(timezone.utc).date()
    out_rows = []
    for r in rows:
        sym = r["symbol"]
        cur = latest_close(sym)
        entry = r["entry_price"]
        realized = (cur - entry) / entry if (cur and entry) else None
        toward_target = None
        if cur and entry and r["p50_target"] and r["p50_target"] != entry:
            toward_target = (cur - entry) / (r["p50_target"] - entry)
        try:
            pd = datetime.fromisoformat(r["pick_date"][:10]).date()
            days_held = (today - pd).days
        except Exception:
            days_held = None
        sec = None
        if r["sec_fundamentals_json"]:
            try:
                sec = json.loads(r["sec_fundamentals_json"])
            except (TypeError, ValueError):
                sec = None
        out_rows.append({
            "pick_date": r["pick_date"],
            "symbol": sym,
            "tier": r["tier"],
            "entry_price": entry,
            "p50_target": r["p50_target"],
            "expected_return": r["expected_return"],
            "risk": r["risk"],
            "sharpe_proxy": r["sharpe_proxy"],
            "horizon_days": r["horizon_days"],
            "rationale": r["rationale"],
            "sec_fundamentals": sec,
            "created_at": r["created_at"],
            "current_price": cur,
            "realized_return": realized,
            "toward_target_pct": toward_target,
            "days_held": days_held,
        })

    payload = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "lookback_days": lookback_days,
        "n_rows": len(out_rows),
        "earliest_pick_date": out_rows[-1]["pick_date"] if out_rows else None,
        "latest_pick_date": out_rows[0]["pick_date"] if out_rows else None,
        "rows": out_rows,
    }
    OUT.write_text(json.dumps(payload, default=str, separators=(",", ":")))
    print(f"Wrote {len(out_rows)} rows to {OUT.relative_to(ROOT)} "
          f"(span: {payload['earliest_pick_date']} → {payload['latest_pick_date']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
