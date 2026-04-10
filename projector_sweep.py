"""
Hyperparameter sweep for the projection model.

Reuses the backtest functions from projector_backtest.py and runs them
under multiple configurations, producing a side-by-side comparison.
"""

from __future__ import annotations

import sys
import time

import numpy as np
import pandas as pd

from projector_backtest import (
    BacktestConfig, DEFAULT_SYMBOLS, MILESTONES,
    backtest_symbol, fetch_history, fetch_vix_history,
)

# A small grid of configs worth testing.
# vix=False is the original price-history-only model.
# vix=True scales sigma by the historical VIX-regime multiplier at each
# rebalance date (no look-ahead).
SWEEP_CONFIGS = [
    # baseline & VIX-scaled at the dashboard's chosen blend
    {"name": "30%-MC",            "blend": 0.30, "sma": 200, "vol_lookback": 252, "vix": False},
    {"name": "30%-MC + VIX",      "blend": 0.30, "sma": 200, "vol_lookback": 252, "vix": True},
    # 50/50 baseline & VIX-scaled
    {"name": "50/50",             "blend": 0.50, "sma": 200, "vol_lookback": 252, "vix": False},
    {"name": "50/50 + VIX",       "blend": 0.50, "sma": 200, "vol_lookback": 252, "vix": True},
    # Pure variants for reference
    {"name": "pure-MC",           "blend": 1.00, "sma": 200, "vol_lookback": 252, "vix": False},
    {"name": "pure-MC + VIX",     "blend": 1.00, "sma": 200, "vol_lookback": 252, "vix": True},
    {"name": "pure-MR",           "blend": 0.00, "sma": 200, "vol_lookback": 252, "vix": False},
    {"name": "pure-MR + VIX",     "blend": 0.00, "sma": 200, "vol_lookback": 252, "vix": True},
]

def run_sweep(symbols: list[str], years: int = 10, paths: int = 1000):
    print(f"Sweep: {len(symbols)} symbols, {years}y, {paths} paths, "
          f"{len(SWEEP_CONFIGS)} configs", file=sys.stderr)

    # Fetch all symbols once and cache them
    cache = {}
    for sym in symbols:
        print(f"  fetching {sym}...", file=sys.stderr)
        result = fetch_history(sym, years)
        if result is not None:
            cache[sym] = result

    # Fetch ^VIX history once (used by VIX-scaled configs)
    print(f"  fetching ^VIX history...", file=sys.stderr)
    vix_series = fetch_vix_history(years)
    if vix_series is None:
        print("  WARNING: could not fetch VIX history; VIX configs will fall back to mult=1.0",
              file=sys.stderr)
    else:
        print(f"  ^VIX: {len(vix_series)} days, "
              f"{vix_series.index.min().date()} → {vix_series.index.max().date()}, "
              f"min={vix_series.min():.1f} max={vix_series.max():.1f}",
              file=sys.stderr)

    summary_rows = []

    for cfg_dict in SWEEP_CONFIGS:
        name = cfg_dict["name"]
        cfg = BacktestConfig(
            history_years=years,
            num_paths=paths,
            sma_period=cfg_dict["sma"],
            vol_lookback=cfg_dict["vol_lookback"],
            blend_mc=cfg_dict["blend"],
            vix_scale=cfg_dict["vix"],
        )
        t0 = time.time()
        all_rows = []
        for sym, (closes, dates) in cache.items():
            rows = backtest_symbol(sym, closes, dates, cfg, vix_series=vix_series)
            all_rows.extend(rows)
        df = pd.DataFrame(all_rows)
        elapsed = time.time() - t0
        print(f"  [{name}] {len(df)} forecasts in {elapsed:.1f}s", file=sys.stderr)

        # Compute aggregate metrics by horizon
        for label, _ in MILESTONES:
            sub = df[df["horizon"] == label]
            if sub.empty:
                continue
            n = len(sub)
            cov80 = ((sub["realized"] >= sub["p10"]) &
                     (sub["realized"] <= sub["p90"])).mean() * 100
            cov50 = ((sub["realized"] >= sub["p25"]) &
                     (sub["realized"] <= sub["p75"])).mean() * 100
            bias = ((sub["median"] - sub["realized"]) / sub["realized"] * 100).mean()
            mae = (abs(sub["median"] - sub["realized"]) / sub["realized"] * 100).mean()
            dir_acc = ((np.sign(sub["median"] - sub["current_price"]) ==
                        np.sign(sub["realized"] - sub["current_price"])).mean()) * 100
            naive_mae = (abs(sub["current_price"] - sub["realized"]) /
                         sub["realized"] * 100).mean()
            skill = 1 - (mae / naive_mae) if naive_mae > 0 else 0.0
            summary_rows.append({
                "config": name,
                "blend_mc": cfg_dict["blend"],
                "sma": cfg_dict["sma"],
                "vol_lookback": cfg_dict["vol_lookback"],
                "vix": cfg_dict["vix"],
                "horizon": label,
                "n": n,
                "cov80": cov80,
                "cov50": cov50,
                "bias_pct": bias,
                "mae_pct": mae,
                "dir_acc": dir_acc,
                "naive_mae": naive_mae,
                "skill": skill,
            })

    return pd.DataFrame(summary_rows)

def write_report(summary: pd.DataFrame, out_path: str):
    out = ["# S-Tool Projector — Hyperparameter Sweep (with VIX scaling)\n"]
    out.append(f"Configs tested: {summary['config'].nunique()}  ")
    out.append(f"Total rows in sweep summary: {len(summary)}\n")

    for label, _ in MILESTONES:
        out.append(f"\n## Horizon: {label}\n")
        sub = summary[summary["horizon"] == label].copy()
        sub = sub.sort_values("skill", ascending=False)
        out.append("| Config | Blend (MC%) | VIX | 80% Cov | Bias % | MAE % | Naive MAE % | Skill | Dir Acc |")
        out.append("|---|---|---|---|---|---|---|---|---|")
        for _, r in sub.iterrows():
            vix_marker = "yes" if r["vix"] else "no"
            out.append(
                f"| {r['config']} | {int(r['blend_mc']*100)}% | {vix_marker} | "
                f"{r['cov80']:.1f}% | {r['bias_pct']:+.2f}% | "
                f"{r['mae_pct']:.2f}% | {r['naive_mae']:.2f}% | {r['skill']:+.3f} | "
                f"{r['dir_acc']:.1f}% |"
            )

    # Head-to-head: each blend with vs without VIX
    out.append("\n## Head-to-head: VIX scaling impact\n")
    out.append("Δ(metric) = VIX-scaled minus baseline. "
               "Negative MAE Δ / positive coverage Δ / positive skill Δ = VIX helps.\n")
    out.append("| Horizon | Blend | Cov80 base → VIX (Δ) | MAE base → VIX (Δ) | Skill base → VIX (Δ) | Bias base → VIX |")
    out.append("|---|---|---|---|---|---|")
    blends = sorted(summary["blend_mc"].unique())
    for label, _ in MILESTONES:
        for b in blends:
            base = summary[(summary["horizon"] == label) & (summary["blend_mc"] == b) & (~summary["vix"])]
            scaled = summary[(summary["horizon"] == label) & (summary["blend_mc"] == b) & (summary["vix"])]
            if base.empty or scaled.empty:
                continue
            br = base.iloc[0]; sr = scaled.iloc[0]
            d_cov = sr["cov80"] - br["cov80"]
            d_mae = sr["mae_pct"] - br["mae_pct"]
            d_skill = sr["skill"] - br["skill"]
            out.append(
                f"| {label} | {int(b*100)}% MC | "
                f"{br['cov80']:.1f}% → {sr['cov80']:.1f}% ({d_cov:+.1f}) | "
                f"{br['mae_pct']:.2f}% → {sr['mae_pct']:.2f}% ({d_mae:+.2f}) | "
                f"{br['skill']:+.3f} → {sr['skill']:+.3f} ({d_skill:+.3f}) | "
                f"{br['bias_pct']:+.2f}% → {sr['bias_pct']:+.2f}% |"
            )

    # Best config per horizon by skill
    out.append("\n## Best Config per Horizon (by skill score)\n")
    out.append("| Horizon | Best Config | Skill | MAE % | Bias % |")
    out.append("|---|---|---|---|---|")
    for label, _ in MILESTONES:
        sub = summary[summary["horizon"] == label]
        if sub.empty:
            continue
        best = sub.loc[sub["skill"].idxmax()]
        out.append(f"| {label} | {best['config']} | {best['skill']:+.3f} | {best['mae_pct']:.2f}% | {best['bias_pct']:+.2f}% |")

    text = "\n".join(out)
    with open(out_path, "w") as f:
        f.write(text)
    return text

if __name__ == "__main__":
    summary = run_sweep(DEFAULT_SYMBOLS, years=10, paths=800)
    summary.to_csv("sweep_results.csv", index=False)
    report = write_report(summary, "sweep_report.md")
    print("\n" + report)
