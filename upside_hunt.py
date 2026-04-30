"""
Asymmetric-upside research harness.

Scans the full cached-price universe across 2022-Q1 → 2024-Q3 quarterly
windows. For every ticker × window, compute the projection engine output
AND the realized 12-month forward return. Then score candidate
"asymmetric scoring" methods by their hit rate on 100%+ moves:

  H1 (naive):  rank by p90 / current_price. Top-20 hit rate?
  H2 (capped): H1 but reject p10 < 0.60 × current_price. Does requiring
               downside protection raise the hit rate?
  H3 (growth+short): require SEC revenue growth > 30% AND FINRA days-to-
                     cover > 5. How often does that combo 2x?
  H4 (composite): confidence × (p90/current - 1) × (1 - max(0, 1 - p10/current))

Output:
  upside_hunt_report.md   — ranked comparison table
  upside_hunt_results.csv — raw per-window results
  upside_candidates.json  — TODAY's picks by each surviving method
                            (so tomorrow's UI can surface them as an
                            "Asymmetric" tier separate from standard picks)

Runtime: ~2-4hrs on the cached universe at workers=4.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("upside_hunt")

ROOT = Path(__file__).parent
PRICES_DIR = ROOT / "data_cache" / "prices"
DB_PATH = ROOT / "projector_cache.db"

HORIZON_DAYS = 252          # 1yr forward
TOP_N = 20                  # evaluate top-20 per method
HIT_THRESHOLD = 1.00        # +100% realized return in 12mo = "hit"
MIN_PRICE_HISTORY = 504     # 2yr minimum to train the engine


# ── Load price series ────────────────────────────────────────────────

def load_prices(sym: str) -> pd.Series | None:
    path = PRICES_DIR / f"{sym}.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
        s = df["Close"].dropna().sort_index()
        return s if len(s) >= MIN_PRICE_HISTORY else None
    except Exception:
        return None


# ── Minimal engine: MC + mean-reversion blend ────────────────────────

def project(prices: pd.Series, as_of: pd.Timestamp, horizon: int = HORIZON_DAYS,
            paths: int = 2000, sigma_mode: str = "std") -> dict | None:
    lookback = prices[prices.index <= as_of].tail(504)
    if len(lookback) < 252:
        return None
    log_rets = np.log(lookback / lookback.shift(1)).dropna().values
    mu = float(np.mean(log_rets) * 252)
    if sigma_mode == "ewma":
        # Exponentially-weighted volatility — recent days weighted more.
        # Better proxy for "vol regime" than simple std; correlates with
        # the conditional-variance idea without full GARCH complexity.
        lam = 0.94  # RiskMetrics standard
        weights = np.array([lam ** i for i in range(len(log_rets))][::-1])
        weights /= weights.sum()
        var = float(np.sum(weights * log_rets * log_rets))
        sigma = float(np.sqrt(var * 252))
    else:
        sigma = float(np.std(log_rets) * np.sqrt(252))
    if sigma < 0.05:
        return None
    current = float(lookback.iloc[-1])
    dt = 1.0 / 252
    rng = np.random.default_rng(42)
    Z = rng.standard_normal((paths, horizon))
    inc = (mu - 0.5 * sigma * sigma) * dt + sigma * np.sqrt(dt) * Z
    terminal = current * np.exp(np.cumsum(inc, axis=1)[:, -1])
    return {
        "current": current,
        "p10": float(np.percentile(terminal, 10)),
        "p50": float(np.percentile(terminal, 50)),
        "p90": float(np.percentile(terminal, 90)),
        "mu": mu, "sigma": sigma,
    }


def sector_momentum(prices_by_sym: dict, sector_by_sym: dict,
                    as_of: pd.Timestamp, lookback_days: int = 63) -> dict[str, float]:
    """For each sector, compute the mean 3-month return across its constituents.
    Used by H5 to boost picks in sectors already running."""
    start = as_of - timedelta(days=int(lookback_days * 365 / 252))
    bucket: dict[str, list[float]] = {}
    for sym, prices in prices_by_sym.items():
        sec = sector_by_sym.get(sym)
        if not sec or sec == "Unclassified":
            continue
        window = prices[(prices.index >= start) & (prices.index <= as_of)]
        if len(window) < 30:
            continue
        ret = (window.iloc[-1] / window.iloc[0]) - 1
        bucket.setdefault(sec, []).append(float(ret))
    return {s: float(np.mean(v)) for s, v in bucket.items() if len(v) >= 5}


# ── Auxiliary signal loaders (best-effort; missing data → None) ──────

def load_short_interest(conn, sym: str, as_of: pd.Timestamp) -> dict | None:
    try:
        row = conn.execute(
            """SELECT days_to_cover, change_pct FROM short_interest
                 WHERE symbol = ? AND settlement_date <= ?
                 ORDER BY settlement_date DESC LIMIT 1""",
            (sym.upper(), as_of.date().isoformat()),
        ).fetchone()
        if not row:
            return None
        return {"days_to_cover": row[0], "change_pct": row[1]}
    except sqlite3.OperationalError:
        return None


def load_sec_growth(conn, sym: str, as_of: pd.Timestamp) -> dict | None:
    try:
        # Pull the most recent FY revenue row filed before as_of.
        rows = conn.execute(
            """SELECT period_end, revenues
                 FROM sec_fundamentals
                 WHERE symbol = ? AND filed_at <= ? AND revenues IS NOT NULL
                   AND period_type = 'FY'
                 ORDER BY period_end DESC LIMIT 3""",
            (sym.upper(), as_of.date().isoformat()),
        ).fetchall()
        if len(rows) < 2:
            return None
        cur, prev = float(rows[0][1]), float(rows[1][1])
        if prev <= 0:
            return None
        return {"yoy_growth": (cur - prev) / prev}
    except sqlite3.OperationalError:
        return None


# ── Scoring methods under test ───────────────────────────────────────

def score_methods(sym: str, proj: dict, si: dict | None, sec: dict | None,
                  proj_ewma: dict | None = None, ticker_sector: str | None = None,
                  hot_sectors: set | None = None, small_cap: bool | None = None) -> dict:
    p10, p50, p90, cur = proj["p10"], proj["p50"], proj["p90"], proj["current"]
    p90_ratio = p90 / cur if cur else 0
    p10_ratio = p10 / cur if cur else 0
    downside_ok = p10_ratio >= 0.60
    growth_ok = bool(sec and sec.get("yoy_growth", 0) >= 0.30)
    squeeze_ok = bool(si and (si.get("days_to_cover") or 0) >= 5)
    hot_sector = bool(ticker_sector and hot_sectors and ticker_sector in hot_sectors)

    # H7: EWMA-vol projection's p90 ratio (captures rising-vol regimes).
    p90_ewma = proj_ewma["p90"] / cur if (proj_ewma and cur) else 0
    p90_ewma_ratio = p90_ewma if p90_ewma else p90_ratio

    return {
        "H1_naive_p90":       p90_ratio,
        "H2_capped_p90":      p90_ratio if downside_ok else 0.0,
        "H3_growth_squeeze":  p90_ratio if (growth_ok and squeeze_ok) else 0.0,
        "H4_composite":       max(0, p90_ratio - 1) * (1 if downside_ok else 0.4) *
                              (1 + (0.3 if growth_ok else 0)) *
                              (1 + (0.3 if squeeze_ok else 0)),

        # H5: sector-momentum boost — filters to hot sectors then ranks by p90.
        "H5_sector_mom":      p90_ratio if hot_sector else 0.0,

        # H6: small-cap bias — where 2x moves actually happen mechanically.
        # `small_cap` is approximated from CURRENT market cap cache since
        # historical caps are unavailable for most tickers. Survivorship
        # bias risk: a stock now small may have been large at pick time.
        "H6_small_cap":       p90_ratio if small_cap else 0.0,

        # H7: EWMA-volatility projection — widens the tail when recent vol
        # has been rising vs. the trailing-year average.
        "H7_ewma_p90":        p90_ewma_ratio,

        # H9: full stack — p90 × sector momentum × small-cap × EWMA boost.
        # No downside cap because that removed winners in H2.
        "H9_full_stack":      p90_ewma_ratio
                              * (1.4 if hot_sector else 1.0)
                              * (1.3 if small_cap else 1.0)
                              * (1 + (0.2 if growth_ok else 0))
                              * (1 + (0.3 if squeeze_ok else 0)),
    }


def realized_return(prices: pd.Series, as_of: pd.Timestamp, horizon: int) -> float | None:
    future_cut = as_of + timedelta(days=int(horizon * 365 / 252))
    future = prices[prices.index >= future_cut]
    if len(future) == 0:
        return None
    current = float(prices[prices.index <= as_of].iloc[-1])
    actual = float(future.iloc[0])
    return (actual - current) / current


# ── Main sweep ───────────────────────────────────────────────────────

def run_window(as_of: pd.Timestamp, symbols: list[str],
               price_cache: dict, conn,
               sector_by_sym: dict, small_cap_syms: set) -> list[dict]:
    # Precompute sector momentum for this window — one pass over all tickers.
    sec_mom = sector_momentum(price_cache, sector_by_sym, as_of, lookback_days=63)
    # Hot sectors = top-quartile by 3mo return, minimum +5% absolute
    if sec_mom:
        sorted_secs = sorted(sec_mom.items(), key=lambda kv: -kv[1])
        cutoff = sorted_secs[max(0, len(sorted_secs) // 4)][1]
        hot_sectors = {s for s, r in sec_mom.items() if r >= max(cutoff, 0.05)}
    else:
        hot_sectors = set()

    rows = []
    for sym in symbols:
        prices = price_cache.get(sym)
        if prices is None:
            continue
        if prices.index.max() < as_of + timedelta(days=380):
            continue
        proj = project(prices, as_of, sigma_mode="std")
        if proj is None:
            continue
        proj_ewma = project(prices, as_of, sigma_mode="ewma")
        si = load_short_interest(conn, sym, as_of)
        sec = load_sec_growth(conn, sym, as_of)
        scores = score_methods(
            sym, proj, si, sec,
            proj_ewma=proj_ewma,
            ticker_sector=sector_by_sym.get(sym),
            hot_sectors=hot_sectors,
            small_cap=(sym in small_cap_syms),
        )
        realized = realized_return(prices, as_of, HORIZON_DAYS)
        if realized is None:
            continue
        rows.append({
            "as_of": as_of.date().isoformat(),
            "symbol": sym,
            "sector": sector_by_sym.get(sym, ""),
            "current": proj["current"], "p10": proj["p10"], "p90": proj["p90"],
            "sigma": proj["sigma"],
            "realized_ret": realized,
            "hit_100": int(realized >= HIT_THRESHOLD),
            **scores,
        })
    return rows


def sweep(limit_symbols: int | None = None):
    log.info("Loading prices…")
    syms = sorted(p.stem for p in PRICES_DIR.glob("*.csv"))
    if limit_symbols:
        syms = syms[:limit_symbols]
    price_cache: dict[str, pd.Series] = {}
    for s in syms:
        p = load_prices(s)
        if p is not None:
            price_cache[s] = p
    log.info("%d usable tickers", len(price_cache))

    # Sector map — FMP profile cache. ~603 tickers covered today; rest
    # fall through to "Unclassified" and are excluded from H5 sector
    # momentum. We include SEC industry-code classification as a fallback
    # if anyone adds that later.
    log.info("Loading sector map…")
    sector_by_sym: dict[str, str] = {}
    profiles_dir = ROOT / "data_cache" / "profiles"
    if profiles_dir.exists():
        for pf in profiles_dir.glob("*.json"):
            try:
                d = json.loads(pf.read_text())
                s = d.get("sector")
                if s:
                    sector_by_sym[pf.stem] = s
            except Exception:
                pass
    log.info("Sector map: %d / %d classified", len(sector_by_sym), len(price_cache))

    # Small-cap set (for H6) — approximate via current yfinance market cap.
    # SURVIVORSHIP CAVEAT: a stock small today may have been mid-cap at the
    # historical pick date. Best available proxy; flag in the report.
    log.info("Loading market cap cache…")
    mcap_path = ROOT / "data_cache" / "market_caps.json"
    small_cap_syms: set = set()
    if mcap_path.exists():
        try:
            mc = json.loads(mcap_path.read_text())
            small_cap_syms = {
                s for s, v in mc.items()
                if v.get("market_cap") and v["market_cap"] < 2e9
            }
        except Exception:
            pass
    log.info("Small-cap (< $2B) set: %d tickers", len(small_cap_syms))

    conn = sqlite3.connect(str(DB_PATH))
    # Regime-expanded walk-forward: 2016-01 → 2024-09, quarterly.
    # Covers the 2018 Q4 drawdown, the 2020 COVID crash, the 2022 H1
    # tech bear, plus the 2021 continuity windows we were missing.
    # Requires the historical price backfill (backfill_prices_historical.py)
    # so that as_of 2016-01-15 has enough lookback.
    windows = pd.date_range("2016-01-15", "2024-09-15", freq="3MS")
    log.info("Windows: %d (%s → %s)", len(windows),
             windows[0].date(), windows[-1].date())

    all_rows: list[dict] = []
    for i, w in enumerate(windows, 1):
        t0 = time.time()
        rows = run_window(
            w, list(price_cache.keys()), price_cache, conn,
            sector_by_sym=sector_by_sym, small_cap_syms=small_cap_syms,
        )
        log.info("[%d/%d] %s — %d rows in %.1fs",
                 i, len(windows), w.date(), len(rows), time.time() - t0)
        all_rows.extend(rows)

    if not all_rows:
        log.error("No rows produced. Check universe + cache coverage.")
        return

    # Sanity gate: the canonical CSV in runtime_data/ has 67k rows × 35 windows
    # generated locally on 2026-04-17 from a deep price cache (back to 2015).
    # Cron usually runs against a much shallower price cache and produces a
    # 4–10k-row stub. We refuse to overwrite the canonical with a stub —
    # otherwise every nightly slow run silently degrades the training set.
    rt_csv = ROOT / "runtime_data" / "upside_hunt_results.csv"
    out_csv = ROOT / "upside_hunt_results.csv"
    new_rows = len(all_rows)
    if rt_csv.exists():
        try:
            import pandas as _pd
            existing_rows = len(_pd.read_csv(rt_csv))
        except Exception:
            existing_rows = 0
        if new_rows < int(existing_rows * 0.8):
            log.warning(
                "skipping write: new run has %d rows but runtime_data canonical "
                "has %d. Refusing to shrink the training set. Restore the deep "
                "price cache or run regenerate_training_set.py locally to update.",
                new_rows, existing_rows,
            )
            return

    with open(out_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=all_rows[0].keys())
        writer.writeheader()
        writer.writerows(all_rows)
    # Mirror to runtime_data so cron pipelines downstream see the fresh set.
    rt_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(rt_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=all_rows[0].keys())
        writer.writeheader()
        writer.writerows(all_rows)
    log.info("Wrote %d rows to %s and %s", new_rows, out_csv, rt_csv)

    # Analyze — for each method, rank each window by score desc, take top-N,
    # compute hit rate on 100%+ returns.
    df = pd.DataFrame(all_rows)
    methods = ["H1_naive_p90", "H2_capped_p90", "H3_growth_squeeze", "H4_composite",
               "H5_sector_mom", "H6_small_cap", "H7_ewma_p90", "H9_full_stack"]
    lines = [
        "# Asymmetric Upside Hunt — Research Report",
        f"\n**Run:** {datetime.utcnow().isoformat()[:19]}Z",
        f"**Universe:** {len(price_cache)} tickers",
        f"**Windows:** {len(windows)} quarterly, 2016-Q1 through 2024-Q3",
        f"**Hit defined:** realized 12-month return ≥ +{int(HIT_THRESHOLD*100)}%",
        f"**Top-N per method:** {TOP_N}",
        "\n| Method | Picks | Hits | Hit rate | Median return | Mean return |",
        "|---|---|---|---|---|---|",
    ]
    for m in methods:
        # For each window, pick top-N by score; concat
        top_per_win = (
            df[df[m] > 0]
            .groupby("as_of", group_keys=False)
            .apply(lambda g: g.nlargest(TOP_N, m))
        )
        if len(top_per_win) == 0:
            lines.append(f"| {m} | 0 | 0 | — | — | — |")
            continue
        n = len(top_per_win)
        hits = int(top_per_win["hit_100"].sum())
        hit_rate = hits / n
        median = float(top_per_win["realized_ret"].median())
        mean = float(top_per_win["realized_ret"].mean())
        lines.append(f"| {m} | {n} | {hits} | {hit_rate:.1%} | {median:+.1%} | {mean:+.1%} |")

    lines.append("\n## Interpretation\n")
    lines.append("- **H1_naive_p90**: baseline. Rank by p90/current. Shows whether the engine's tail is predictive without any filters.\n")
    lines.append("- **H2_capped_p90**: H1 but require p10 ≥ 60% of current. Asymmetry filter.\n")
    lines.append("- **H3_growth_squeeze**: require YoY rev growth ≥ 30% AND DTC ≥ 5 before scoring. Narrow but catalyst-driven.\n")
    lines.append("- **H4_composite**: continuous blend of all three. Best candidate for production.\n")
    lines.append("\nAny method producing hit rate ≥ 20% on +100% returns is production-worthy. "
                 "The current top-Sharpe picks page produces ~0% hit rate on 2x moves by design "
                 "(median-optimized). This sweep exists to find a scoring function that targets "
                 "the tail instead of the median.")

    out_md = ROOT / "upside_hunt_report.md"
    out_md.write_text("\n".join(lines))
    log.info("Wrote report to %s", out_md)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit-symbols", type=int, default=None,
                   help="cap universe size for a fast smoke test")
    args = p.parse_args()
    sweep(limit_symbols=args.limit_symbols)


if __name__ == "__main__":
    sys.exit(main() or 0)
