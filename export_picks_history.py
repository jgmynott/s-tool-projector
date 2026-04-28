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


def _load_spy_series() -> dict[str, float]:
    """Date-string → SPY Close map from the cached daily bars.
    Loaded once per export so per-row lookups are O(1)."""
    csv_path = PRICES_DIR / "SPY.csv"
    if not csv_path.exists():
        return {}
    out: dict[str, float] = {}
    try:
        with csv_path.open() as fh:
            for row in csv.DictReader(fh):
                d = (row.get("Date") or "")[:10]
                c = row.get("Close")
                if d and c:
                    try:
                        out[d] = float(c)
                    except ValueError:
                        pass
    except Exception:
        return {}
    return out


def _spy_close_on_or_before(spy: dict[str, float], iso_date: str) -> float | None:
    """SPY Close on the requested date, or the nearest prior trading day.
    Picks made on weekends/holidays look up the previous Friday's close."""
    if not spy:
        return None
    target = iso_date[:10]
    if target in spy:
        return spy[target]
    # Walk back up to 7 days for weekends + market holidays.
    from datetime import date, timedelta
    try:
        d = date.fromisoformat(target)
    except Exception:
        return None
    for _ in range(7):
        d = d - timedelta(days=1)
        key = d.isoformat()
        if key in spy:
            return spy[key]
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
    spy_series = _load_spy_series()
    # 'spy_today' = the rightmost anchor we can compare picks against. If
    # SPY.csv is fresh (slow pipeline ran today) we use today's close;
    # otherwise we fall back to the latest cached close so alpha numbers
    # are still computable when the data pipeline has hiccupped — better
    # to ship a slightly-stale alpha than no alpha at all.
    spy_today = _spy_close_on_or_before(spy_series, today.isoformat()) if spy_series else None
    spy_today_date = today.isoformat()
    if spy_today is None and spy_series:
        latest = max(spy_series.keys())
        spy_today = spy_series[latest]
        spy_today_date = latest
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
        # SPY-relative — same window [pick_date, today]. Alpha is just
        # realized − SPY return, so the track-record page can show how
        # much the pick beat (or trailed) a passive index buy on the
        # same day. None if either anchor is missing.
        spy_entry = _spy_close_on_or_before(spy_series, r["pick_date"]) if spy_series else None
        spy_return = (spy_today - spy_entry) / spy_entry if (spy_today and spy_entry) else None
        alpha_vs_spy = (realized - spy_return) if (realized is not None and spy_return is not None) else None
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
            "spy_return": spy_return,
            "alpha_vs_spy": alpha_vs_spy,
        })

    # Aggregate vs-SPY summary so /api/track-record can render alpha
    # without re-iterating the full row set on every request. Tier-level
    # because the product UX surfaces conservative/moderate/aggressive/
    # asymmetric separately and lumping all picks together hides the
    # signal — asymmetric tier should beat SPY by far more than the
    # broad universe scan.
    def _agg(rows: list[dict]) -> dict:
        wins = [r for r in rows if r.get("alpha_vs_spy") is not None]
        if not wins:
            return {"n": 0}
        n = len(wins)
        alphas = sorted(r["alpha_vs_spy"] for r in wins)
        realized = [r["realized_return"] for r in wins if r.get("realized_return") is not None]
        spys = [r["spy_return"] for r in wins if r.get("spy_return") is not None]
        return {
            "n": n,
            "mean_alpha": sum(alphas) / n,
            "median_alpha": alphas[n // 2],
            "win_rate_vs_spy": sum(1 for a in alphas if a > 0) / n,
            "mean_realized": (sum(realized) / len(realized)) if realized else None,
            "mean_spy_return": (sum(spys) / len(spys)) if spys else None,
        }

    by_tier: dict[str, list[dict]] = {}
    for r in out_rows:
        by_tier.setdefault(r["tier"] or "unknown", []).append(r)

    payload = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "lookback_days": lookback_days,
        "n_rows": len(out_rows),
        "earliest_pick_date": out_rows[-1]["pick_date"] if out_rows else None,
        "latest_pick_date": out_rows[0]["pick_date"] if out_rows else None,
        "spy_anchor_date": spy_today_date if spy_today else None,
        "spy_anchor_close": spy_today,
        "vs_spy_overall": _agg(out_rows),
        "vs_spy_by_tier": {tier: _agg(rs) for tier, rs in by_tier.items()},
        "rows": out_rows,
    }
    OUT.write_text(json.dumps(payload, default=str, separators=(",", ":")))
    print(f"Wrote {len(out_rows)} rows to {OUT.relative_to(ROOT)} "
          f"(span: {payload['earliest_pick_date']} → {payload['latest_pick_date']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
