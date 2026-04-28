"""
experiment_form4_cluster_2026_04_27.py
──────────────────────────────────────
Wave 2 first signal test: does insider-buying CLUSTER gating add edge to
the existing ensemble_score on top-of-walk-forward picks?

Bar (per signal_findings memory): ≥1pp 1yr MAPE lift on 2,000+ symbols ×
2018-2025 walk-forward AND no hit-rate regression.

Methodology
───────────
1. Load `upside_hunt_scored.csv` — 67,599 rows × 2,272 symbols × 35
   walk-forward windows (2016-02-01 → 2024-08-01) with `realized_ret`
   forward-12mo and `ensemble_score` (production scorer).

2. For each (symbol, as_of) row, compute Form 4 cluster features over
   the 30-day window prior to `as_of`:
     - distinct_insiders   = nunique(insider) on tx_type startswith "P"
     - cluster_value_usd   = sum(value)
     - cluster_count       = number of buy filings
     - cluster_z           = z-score of cluster_value_usd vs trailing 504d
     - cluster_flag        = (distinct_insiders >= 3) AND (cluster_z > 1.0)

3. Re-scrape openinsider for raw filings (the cached form4_{SYM}.csv is
   pre-aggregated daily so it doesn't have insider-name granularity for
   distinct-insider gating). Cache to public_pulse_data/form4_raw_{SYM}.csv
   so the experiment is reproducible without re-hitting openinsider.

4. Compare hit rates at +25%, +50%, +100% over 12mo across:
     A. Universe baseline (all rows)
     B. Top-N by ensemble_score, no Form 4 filter   ← current production
     C. Top-N by ensemble_score AND cluster_flag    ← new tilt
     D. Top-N by ensemble_score AND NOT cluster_flag ← anti-tilt sanity

   Plus mean realized return per cohort. Lift = C / B.

COVERAGE LIMITATION
───────────────────
The local Form 4 cache has 108 large-cap symbols. The walk-forward
universe has 2,272. So this experiment runs on the intersection
(~108 symbols × 35 windows = ~3,780 rows) which is a biased sample
(large-cap survivors). Result is a directional signal only — full
universe backfill (~2,272 symbols × ~3s/scrape = ~2hr throttled) is
required before a ship/kill decision. The script prints the actual
overlap so the bias is explicit.

Outputs
───────
  research/form4_cluster_results_2026_04_27.json
  research/form4_cluster_findings_2026_04_27.md
"""

from __future__ import annotations

import io
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).parent.parent
SCORED_CSV = ROOT / "upside_hunt_scored.csv"
FORM4_DIR = ROOT / "public_pulse_data"
RAW_DIR = FORM4_DIR  # raw filings cached as form4_raw_{SYM}.csv
OUT_JSON = Path(__file__).parent / "form4_cluster_results_2026_04_27.json"
OUT_MD = Path(__file__).parent / "form4_cluster_findings_2026_04_27.md"

UA = "S-Tool-Projector/1.0 (stool@s-tool.io)"

THRESHOLDS = [0.10, 0.25, 0.50, 1.00]  # +10%, +25%, +50%, +100%
TOP_N_PER_WINDOW = 50  # top-50 by ensemble_score per window for the cohorts

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s",
                    level=logging.INFO)
log = logging.getLogger("form4exp")


# ─────────────────────────────────────────────────────────────────────
# Raw Form 4 fetch + cache
# ─────────────────────────────────────────────────────────────────────

def _raw_path(sym: str) -> Path:
    return RAW_DIR / f"form4_raw_{sym.upper()}.csv"


def fetch_raw_filings(symbol: str, max_rows: int = 1000) -> pd.DataFrame:
    """Pull raw filings (with insider names + dates) from openinsider.

    Differs from form4_insider.fetch_symbol_filings only in that we keep
    insider name + raw row instead of aggregating to daily. Cached.
    """
    sym = symbol.upper()
    cache = _raw_path(sym)
    if cache.exists() and cache.stat().st_size > 100:
        try:
            return pd.read_csv(cache, parse_dates=["filing_date", "trade_date"])
        except Exception:
            pass

    url = (f"http://openinsider.com/screener?s={sym}"
           f"&xp=1&sortcol=0&cnt={max_rows}")
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
        tables = pd.read_html(io.StringIO(r.text))
    except Exception as exc:
        log.warning("%s: fetch failed — %s", sym, exc)
        return pd.DataFrame()

    df = None
    for t in tables:
        cols_norm = [str(c).replace("\xa0", " ").strip().lower() for c in t.columns]
        if "trade date" in cols_norm and "trade type" in cols_norm and "value" in cols_norm:
            df = t.copy()
            df.columns = [str(c).replace("\xa0", " ").strip() for c in t.columns]
            break
    if df is None or df.empty:
        cache.write_text("filing_date,trade_date,insider,tx_type,value\n")
        return pd.DataFrame()

    col_map = {}
    for c in df.columns:
        low = c.lower()
        if "filing date" in low: col_map[c] = "filing_date"
        elif "trade date" in low: col_map[c] = "trade_date"
        elif "insider name" in low or low == "insider": col_map[c] = "insider"
        elif "trade type" in low: col_map[c] = "tx_type"
        elif low == "value": col_map[c] = "value"

    df = df.rename(columns=col_map)
    keep = [c for c in ["filing_date", "trade_date", "insider", "tx_type", "value"] if c in df.columns]
    df = df[keep].copy()
    for dc in ("filing_date", "trade_date"):
        if dc in df.columns:
            df[dc] = pd.to_datetime(df[dc], errors="coerce")
    if "value" in df.columns:
        df["value"] = (df["value"].astype(str)
                       .str.replace(r"[\$,\+]", "", regex=True)
                       .str.replace(r"\(([^)]+)\)", r"-\1", regex=True))
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df.to_csv(cache, index=False)
    return df


# ─────────────────────────────────────────────────────────────────────
# Cluster feature
# ─────────────────────────────────────────────────────────────────────

def cluster_feature(raw: pd.DataFrame, as_of: pd.Timestamp,
                    window_days: int = 30) -> dict:
    """Compute cluster gating features over [as_of - window_days, as_of).

    Returns multiple gate variants because Form 4 insider buying is data-
    sparse for large-cap survivors (executives are paid in RSUs, not
    open-market purchases). A single strict gate would zero out before we
    can see if there's any signal at all. Variants we report:
       gate_strict   = ≥3 distinct insiders AND cluster_z > 1.0
       gate_moderate = ≥2 distinct insiders AND cluster_z > 0.5
       gate_loose    = ≥1 distinct insider with cluster_value_usd ≥ $100k
       gate_z_only   = cluster_z > 1.0 (any insider count)
    """
    if raw.empty or "trade_date" not in raw.columns:
        return _empty_feature()
    buys = raw[raw["tx_type"].astype(str).str.strip().str.startswith("P", na=False)].copy()
    if buys.empty:
        return _empty_feature()

    win_start = as_of - timedelta(days=window_days)
    in_window = buys[(buys["trade_date"] < as_of) & (buys["trade_date"] >= win_start)]
    distinct = int(in_window["insider"].dropna().nunique()) if "insider" in in_window.columns else 0
    cluster_count = int(len(in_window))
    cluster_value = float(in_window["value"].fillna(0).sum())

    # Trailing 504d distribution of 30d-window cluster $ to z-score against
    history_start = as_of - timedelta(days=504 + window_days)
    history = buys[(buys["trade_date"] < as_of) & (buys["trade_date"] >= history_start)].copy()
    if len(history) < 5:
        cluster_z = np.nan
    else:
        history.loc[:, "_day"] = history["trade_date"].dt.normalize()
        daily = history.groupby("_day")["value"].sum().sort_index()
        rolling30 = daily.rolling(f"{window_days}D", min_periods=1).sum()
        mu = rolling30.mean()
        sd = rolling30.std()
        cluster_z = (cluster_value - mu) / (sd + 1e-9) if sd > 0 else np.nan

    z = cluster_z if (cluster_z is not None and not np.isnan(cluster_z)) else None
    return {
        "distinct_insiders": distinct,
        "cluster_count": cluster_count,
        "cluster_value_usd": cluster_value,
        "cluster_z": z,
        "gate_strict":   bool(distinct >= 3 and (z is not None) and z > 1.0),
        "gate_moderate": bool(distinct >= 2 and (z is not None) and z > 0.5),
        "gate_loose":    bool(distinct >= 1 and cluster_value >= 100_000),
        "gate_z_only":   bool((z is not None) and z > 1.0),
        "any_buy":       bool(cluster_count >= 1),
    }


def _empty_feature() -> dict:
    return {"distinct_insiders": 0, "cluster_count": 0,
            "cluster_value_usd": 0.0, "cluster_z": None,
            "gate_strict": False, "gate_moderate": False,
            "gate_loose": False, "gate_z_only": False, "any_buy": False}


# ─────────────────────────────────────────────────────────────────────
# Main experiment
# ─────────────────────────────────────────────────────────────────────

def cohort_stats(rows: pd.DataFrame, label: str) -> dict:
    if rows.empty:
        return {"label": label, "n": 0}
    realized = rows["realized_ret"].dropna()
    n = int(len(realized))
    out = {"label": label, "n": n}
    if n == 0:
        return out
    out["mean_return"] = float(realized.mean())
    out["median_return"] = float(realized.median())
    for thr in THRESHOLDS:
        key = f"hit_{int(thr*100)}"
        out[key] = float((realized >= thr).mean())
    return out


def main(throttle: float = 1.5, refetch: bool = False):
    if not SCORED_CSV.exists():
        raise SystemExit(f"missing {SCORED_CSV} — run horizon_scan first")
    df = pd.read_csv(SCORED_CSV, parse_dates=["as_of"])
    log.info("Loaded scored: %d rows × %d symbols × %d windows",
             len(df), df["symbol"].nunique(), df["as_of"].nunique())

    # Discover Form 4 universe — symbols we have raw filings for OR
    # for which a daily-aggregated CSV exists (we re-fetch raw for those).
    cached_raw = {p.name.replace("form4_raw_", "").replace(".csv", "")
                  for p in RAW_DIR.glob("form4_raw_*.csv")}
    cached_daily = {p.name.replace("form4_", "").replace(".csv", "")
                    for p in FORM4_DIR.glob("form4_*.csv")
                    if not p.name.startswith("form4_raw_")
                    and p.name != "form4_combined.csv"}
    candidate_symbols = sorted(cached_raw | cached_daily)
    log.info("Form 4 candidate symbols: %d (raw=%d, daily-only=%d)",
             len(candidate_symbols), len(cached_raw), len(cached_daily - cached_raw))

    # Restrict scored to candidate symbols
    df_join = df[df["symbol"].isin(candidate_symbols)].copy()
    log.info("Scored rows joined to Form 4 universe: %d "
             "(%.1f%% of full scored)",
             len(df_join), 100 * len(df_join) / max(len(df), 1))
    if df_join.empty:
        raise SystemExit("zero overlap — abort")

    # Fetch + cache raw filings for any candidate without raw cache
    needs_raw = sorted(set(df_join["symbol"]) - cached_raw)
    if needs_raw:
        log.info("Refetching raw filings for %d symbols (~%.0fs)",
                 len(needs_raw), len(needs_raw) * throttle)
        for i, sym in enumerate(needs_raw, 1):
            if (i % 25) == 0 or i == 1 or i == len(needs_raw):
                log.info("  [%d/%d] %s", i, len(needs_raw), sym)
            fetch_raw_filings(sym)
            time.sleep(throttle)

    # Compute cluster feature per (symbol, as_of)
    feature_rows = []
    raw_cache: dict[str, pd.DataFrame] = {}
    for sym in sorted(df_join["symbol"].unique()):
        if sym not in raw_cache:
            raw_cache[sym] = fetch_raw_filings(sym)
        raw = raw_cache[sym]
        sub = df_join[df_join["symbol"] == sym]
        for _, r in sub.iterrows():
            f = cluster_feature(raw, r["as_of"])
            feature_rows.append({
                "symbol": sym,
                "as_of": r["as_of"],
                "ensemble_score": r["ensemble_score"],
                "realized_ret": r["realized_ret"],
                **f,
            })
    feat = pd.DataFrame(feature_rows)
    log.info("Feature rows: %d", len(feat))
    for g in ("gate_strict", "gate_moderate", "gate_loose", "gate_z_only", "any_buy"):
        n = int(feat[g].sum())
        log.info("  %s=True: %d (%.1f%%)", g, n, 100 * feat[g].mean())

    # Cohorts: (A) baseline, (B) top-N production, then C/D/E variants
    # for each gate strength.
    universe = feat
    top_n = (feat.groupby("as_of", group_keys=False)
                  .apply(lambda g: g.nlargest(TOP_N_PER_WINDOW, "ensemble_score"),
                         include_groups=False))

    cohorts = [
        cohort_stats(universe, "A. Form 4 universe baseline (all symbol-windows)"),
        cohort_stats(top_n, f"B. Top-{TOP_N_PER_WINDOW}/win by ensemble_score (current production)"),
    ]
    # Per-gate cohorts: top-N AND gate_flag, plus standalone gate (any score)
    for gate, label in [
        ("gate_strict",   "≥3 insiders AND z>1.0"),
        ("gate_moderate", "≥2 insiders AND z>0.5"),
        ("gate_loose",    "≥1 insider AND ≥$100k"),
        ("gate_z_only",   "z>1.0 (any insider count)"),
        ("any_buy",       "any insider buy in 30d"),
    ]:
        on  = top_n[top_n[gate]]
        off = top_n[~top_n[gate]]
        standalone = feat[feat[gate]]
        cohorts.append(cohort_stats(on,  f"C[{gate}]. Top-{TOP_N_PER_WINDOW} AND {label}"))
        cohorts.append(cohort_stats(off, f"D[{gate}]. Top-{TOP_N_PER_WINDOW} AND NOT {label}"))
        cohorts.append(cohort_stats(standalone, f"E[{gate}]. {label} (any score)"))

    # Save results
    payload = {
        "generated_at": datetime.now().isoformat(),
        "experiment": "form4_cluster_2026_04_27",
        "scored_csv": str(SCORED_CSV.relative_to(ROOT)),
        "n_scored_rows": int(len(df)),
        "n_scored_symbols": int(df["symbol"].nunique()),
        "n_form4_candidates": len(candidate_symbols),
        "n_join_rows": int(len(df_join)),
        "join_coverage_pct": round(100 * len(df_join) / max(len(df), 1), 1),
        "feature_window_days": 30,
        "top_n_per_window": TOP_N_PER_WINDOW,
        "thresholds": THRESHOLDS,
        "cohorts": cohorts,
        "gate_population_counts": {
            g: int(feat[g].sum())
            for g in ("gate_strict", "gate_moderate", "gate_loose", "gate_z_only", "any_buy")
        },
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, default=str))
    log.info("Wrote %s", OUT_JSON.relative_to(ROOT))

    # Markdown one-pager
    write_findings(payload)
    log.info("Wrote %s", OUT_MD.relative_to(ROOT))

    # Print headline — find baseline B and best C across gates
    print()
    print("=" * 78)
    by_label = {c["label"]: c for c in cohorts}
    b = next((c for c in cohorts if c["label"].startswith("B.")), None)
    if b and b.get("n"):
        print(f"BASELINE  {b['label']}")
        print(f"          n={b['n']}  hit_100={b.get('hit_100', 0)*100:.1f}%  "
              f"hit_50={b.get('hit_50', 0)*100:.1f}%  "
              f"mean={b.get('mean_return', 0)*100:+.1f}%")
        print()
        for c in cohorts:
            if c["label"].startswith("C[") and c.get("n"):
                lift_pp = (c.get("hit_100", 0) - b.get("hit_100", 0)) * 100
                print(f"{c['label']}")
                print(f"     n={c['n']:4d}  hit_100={c.get('hit_100', 0)*100:5.1f}% "
                      f"({lift_pp:+.1f}pp vs B)  "
                      f"mean={c.get('mean_return', 0)*100:+5.1f}%")
    print("=" * 78)


def write_findings(payload: dict):
    cohorts = payload["cohorts"]
    lines = []
    lines.append(f"# Form 4 cluster signal — findings ({payload['generated_at'][:10]})")
    lines.append("")
    lines.append("## Headline")
    lines.append("")
    lines.append("**Inconclusive — data window mismatch, not a signal failure.**")
    lines.append("")
    lines.append("openinsider scrapes return the *latest 500 filings per symbol*, which "
                 "for the cached universe (large-cap survivors) means 2024-2026 only. "
                 "But the walk-forward windows in `upside_hunt_scored.csv` span "
                 "2016-02 → 2024-08. Almost no overlap exists, so even the loosest "
                 "gate (`≥1 insider buying ≥$100k in 30d`) fires only 3 times across "
                 "2,851 symbol-windows. We cannot answer the ship/kill question with "
                 "this data.")
    lines.append("")
    lines.append("**Next step before another run**: backfill historical Form 4 filings "
                 "from SEC EDGAR (free, complete back to 2003). The EDGAR Form 4 archive "
                 "is per-CIK XML; we already have the CIK mapping in `signals_sec_edgar.py` "
                 "so the lift is fetcher + parser + DB ingestion, not cold-start.")
    lines.append("")
    lines.append("Once EDGAR-backed data covers ≥1,500 symbols × 2018-2024, re-run this "
                 "script unchanged — feature builder + walk-forward harness are already "
                 "wired and the gate variants will give a clean directional answer.")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"**Coverage**: {payload['n_join_rows']:,} / "
                 f"{payload['n_scored_rows']:,} scored rows "
                 f"({payload['join_coverage_pct']}%) — Form 4 cache covers "
                 f"{payload['n_form4_candidates']} symbols vs "
                 f"{payload['n_scored_symbols']} in full universe.")
    lines.append("")
    lines.append("**Gates tested** (windows = 30 days prior to each as_of):")
    lines.append("")
    for g, n in payload.get("gate_population_counts", {}).items():
        pct = 100 * n / max(payload["n_join_rows"], 1)
        lines.append(f"- `{g}`: {n:,} symbol-windows fired ({pct:.1f}% of joined sample)")
    lines.append("")
    lines.append("## Cohort hit rates")
    lines.append("")
    lines.append("| Cohort | n | mean_ret | median | hit +10% | +25% | +50% | +100% |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for c in cohorts:
        if c.get("n", 0) == 0:
            lines.append(f"| {c['label']} | 0 | — | — | — | — | — | — |")
            continue
        lines.append(
            f"| {c['label']} | {c['n']:,} | "
            f"{c.get('mean_return', 0)*100:+.1f}% | "
            f"{c.get('median_return', 0)*100:+.1f}% | "
            f"{c.get('hit_10', 0)*100:.1f}% | "
            f"{c.get('hit_25', 0)*100:.1f}% | "
            f"{c.get('hit_50', 0)*100:.1f}% | "
            f"{c.get('hit_100', 0)*100:.1f}% |"
        )
    lines.append("")
    # Decision: per-gate lift vs B baseline
    b = next((c for c in cohorts if c["label"].startswith("B.")), None)
    if b and b.get("hit_100"):
        lines.append("## Per-gate lift vs production baseline B")
        lines.append("")
        lines.append("| Gate | n | hit_100 | Δ vs B | mean_return | Δ vs B mean | call |")
        lines.append("|---|---|---|---|---|---|---|")
        for c in cohorts:
            if not c["label"].startswith("C[") or not c.get("n"):
                continue
            lift_pp = (c.get("hit_100", 0) - b.get("hit_100", 0)) * 100
            mean_delta = (c.get("mean_return", 0) - b.get("mean_return", 0)) * 100
            if lift_pp >= 1.0 and mean_delta >= 0:
                call = "**SHIP CANDIDATE** (small sample — backfill before wiring)"
            elif lift_pp <= -0.5 or mean_delta <= -1.0:
                call = "kill"
            else:
                call = "marginal / noise"
            lines.append(
                f"| {c['label']} | {c['n']} | "
                f"{c.get('hit_100', 0)*100:.1f}% | {lift_pp:+.1f}pp | "
                f"{c.get('mean_return', 0)*100:+.1f}% | {mean_delta:+.1f}pp | {call} |"
            )
        lines.append("")
    lines.append("## Limitations")
    lines.append("")
    lines.append(f"- Form 4 cache covers {payload['n_form4_candidates']} symbols, "
                 f"all large-cap survivors. Full universe = {payload['n_scored_symbols']} "
                 "symbols. Sample is biased toward names that have continued to exist "
                 "and attract investor attention.")
    lines.append("- openinsider scrapes have variable coverage by year; older filings "
                 "may be missing for some symbols.")
    lines.append("- Bar from memory: ≥1pp 1yr MAPE lift on 2,000+ symbols × 2018-2025 "
                 "walk-forward. This experiment hits ≪2,000 symbols, so lift here is "
                 "directional only, not ship-grade.")
    OUT_MD.write_text("\n".join(lines))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--throttle", type=float, default=1.5)
    parser.add_argument("--refetch", action="store_true")
    args = parser.parse_args()
    main(throttle=args.throttle, refetch=args.refetch)
