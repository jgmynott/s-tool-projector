"""
Phase C — Sentiment-Tilted Drift Backtest

Walk-forward validation testing whether WSB sentiment data (score_signed_mean)
improves stock price projections when used to tilt the MC drift parameter.

Compares baseline (MC+MR blend without sentiment) against sentiment-tilted
variants across multiple tilt strengths and lookback windows.

Outputs:
    sentiment_backtest_results.csv  — raw per-window comparison rows
    sentiment_backtest_report.md    — summary table with MAE, RMSE, hit rate, IC
"""

from __future__ import annotations

import argparse
import math
import sys
import warnings
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────

DEFAULT_SYMBOLS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
    "META", "TSLA", "AMD", "JPM", "SPY",
    "QQQ", "BAC", "XOM", "NFLX", "DIS",
]

SENTIMENT_CSV = "sentiment_data/sentiment_combined_2023-01-01_2025-01-01.csv"

@dataclass
class SentimentBacktestConfig:
    train_window: int = 504         # ~2 years training
    horizon: int = 252              # ~1 year forward test
    step: int = 21                  # walk-forward stride (monthly)
    num_paths: int = 1000           # MC + MR each
    sma_period: int = 200
    vol_lookback: int = 252
    blend_mc: float = 0.30          # 30% MC / 70% MR (from sweep)
    rng_seed: int = 42
    tilt_strengths: list[float] = field(default_factory=lambda: [0.005, 0.01, 0.02])
    lookbacks: list[int] = field(default_factory=lambda: [5, 10, 21])
    history_years: int = 10


# ─────────────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────────────

def load_sentiment(csv_path: str) -> pd.DataFrame:
    """Load sentiment CSV and prepare for lookups."""
    df = pd.read_csv(csv_path, parse_dates=["date"])
    df["ticker"] = df["ticker"].str.upper()
    df = df.sort_values(["ticker", "date"]).reset_index(drop=True)
    return df


def fetch_price_history(symbol: str, years: int) -> tuple[np.ndarray, pd.DatetimeIndex] | None:
    """Fetch daily closes from yfinance. Returns (closes, dates) or None."""
    end = pd.Timestamp.today()
    start = end - pd.DateOffset(years=years)
    df = yf.download(symbol, start=start, end=end, progress=False, auto_adjust=True)
    if df is None or len(df) < 600:
        return None
    closes = df["Close"].values.flatten().astype(float)
    return closes, df.index


def get_trailing_sentiment(
    sent_df: pd.DataFrame,
    ticker: str,
    as_of_date: pd.Timestamp,
    lookback_days: int,
) -> float | None:
    """
    Compute average score_signed_mean for `ticker` over the trailing
    `lookback_days` calendar days ending on `as_of_date`.

    Returns None if no sentiment data available in the window.
    """
    start = as_of_date - pd.Timedelta(days=lookback_days)
    mask = (
        (sent_df["ticker"] == ticker)
        & (sent_df["date"] >= start)
        & (sent_df["date"] <= as_of_date)
    )
    sub = sent_df.loc[mask, "score_signed_mean"]
    if sub.empty:
        return None
    return float(sub.mean())


# ─────────────────────────────────────────────────────────────────────
# Math helpers (same as projector_backtest.py)
# ─────────────────────────────────────────────────────────────────────

def sma(arr: np.ndarray, period: int) -> np.ndarray:
    if len(arr) < period:
        return np.full_like(arr, np.mean(arr))
    out = np.full(len(arr), np.nan)
    cs = np.cumsum(arr, dtype=float)
    out[period - 1:] = (cs[period - 1:] - np.concatenate(([0], cs[:-period]))) / period
    out[:period - 1] = out[period - 1]
    return out


def lin_reg(x: np.ndarray, y: np.ndarray):
    mx, my = x.mean(), y.mean()
    den = ((x - mx) ** 2).sum()
    slope = 0.0 if den == 0 else ((x - mx) * (y - my)).sum() / den
    return slope, my - slope * mx


# ─────────────────────────────────────────────────────────────────────
# Models (from projector_backtest.py, with optional mu tilt)
# ─────────────────────────────────────────────────────────────────────

def run_monte_carlo(
    closes: np.ndarray,
    num_paths: int,
    horizon: int,
    vol_lookback: int,
    rng: np.random.Generator,
    mu_tilt: float = 0.0,
) -> tuple[np.ndarray, dict]:
    """GBM with annualised drift/vol from log returns, plus optional mu_tilt."""
    log_returns = np.diff(np.log(closes))
    recent = log_returns[-vol_lookback:] if len(log_returns) > vol_lookback else log_returns
    mu_base = float(recent.mean() * 252)
    mu = mu_base + mu_tilt
    sigma_hist = float(recent.std(ddof=1) * np.sqrt(252))
    sigma = sigma_hist
    s0 = float(closes[-1])
    dt = 1.0 / 252
    drift = (mu - 0.5 * sigma ** 2) * dt
    diff = sigma * np.sqrt(dt)
    z = rng.standard_normal((num_paths, horizon))
    log_paths = np.cumsum(drift + diff * z, axis=1)
    paths = s0 * np.exp(log_paths)
    return paths, {"mu_base": mu_base, "mu": mu, "mu_tilt": mu_tilt,
                   "sigma": sigma, "sigma_hist": sigma_hist, "s0": s0}


def run_mean_reversion(
    closes: np.ndarray,
    num_paths: int,
    horizon: int,
    sma_period: int,
    rng: np.random.Generator,
) -> tuple[np.ndarray, dict]:
    """OU reversion to trending SMA equilibrium + momentum overlay."""
    s0 = float(closes[-1])
    period = sma_period if len(closes) >= sma_period else max(10, len(closes) // 2)
    ma = sma(closes, period)
    current_ma = float(ma[-1])
    recent_ma = ma[-period:]
    slope_d, _ = lin_reg(np.arange(len(recent_ma), dtype=float), recent_ma)
    trend_ann = slope_d * 252 / current_ma if current_ma > 0 else 0.0

    log_closes = np.log(closes)
    log_ma = np.log(np.maximum(ma, 1e-9))
    devs = log_closes - log_ma
    delta_devs = np.diff(devs)
    prev_devs = devs[:-1]
    ou_slope, ou_intercept = lin_reg(prev_devs, delta_devs)
    kappa = -ou_slope
    half_life = np.log(2) / kappa if kappa > 0 else 252.0
    kappa = max(np.log(2) / 252, min(np.log(2) / 10, kappa))

    predicted = ou_intercept + ou_slope * prev_devs
    residuals = delta_devs - predicted
    sigma_ou = float(residuals.std(ddof=1) * np.sqrt(252))

    mom_period = min(20, len(closes) - 1)
    momentum = (s0 / float(closes[-1 - mom_period]) - 1) / mom_period

    dt = 1.0 / 252
    z = rng.standard_normal((num_paths, horizon))
    paths = np.empty((num_paths, horizon))
    x = np.full(num_paths, np.log(s0))
    sqrt_dt = np.sqrt(dt)
    for t in range(horizon):
        eq = current_ma + slope_d * (t + 1)
        x_eq = np.log(eq) if eq > 0 else float(x[0])
        mom_decay = momentum * np.exp(-t / 20) if t < 60 else 0.0
        x = x + kappa * (x_eq - x) * dt + mom_decay * dt + sigma_ou * sqrt_dt * z[:, t]
        paths[:, t] = np.exp(x)
    return paths, {
        "kappa": kappa, "half_life": half_life, "sigma_ou": sigma_ou,
        "current_ma": current_ma, "trend_ann": trend_ann,
        "sma_period": period, "momentum": momentum,
    }


def blend_percentiles(mc_paths: np.ndarray, mr_paths: np.ndarray,
                      blend_mc: float) -> np.ndarray:
    """Return percentiles array (5, horizon) for [10, 25, 50, 75, 90]."""
    n_mc = int(round(blend_mc * (len(mc_paths) + len(mr_paths))))
    n_mr = (len(mc_paths) + len(mr_paths)) - n_mc
    n_mc = min(n_mc, len(mc_paths))
    n_mr = min(n_mr, len(mr_paths))
    combined = np.vstack([mc_paths[:n_mc], mr_paths[:n_mr]])
    return np.percentile(combined, [10, 25, 50, 75, 90], axis=0)


# ─────────────────────────────────────────────────────────────────────
# Walk-forward backtest with sentiment tilt
# ─────────────────────────────────────────────────────────────────────

EVAL_MILESTONES = [("1mo", 21), ("3mo", 63), ("6mo", 126), ("1yr", 252)]


def run_single_window(
    closes_train: np.ndarray,
    closes_future: np.ndarray,
    cfg: SentimentBacktestConfig,
    rng: np.random.Generator,
    mu_tilt: float = 0.0,
) -> dict:
    """
    Run MC+MR projection on training closes and measure error against future.

    Returns dict with median forecast, realized, errors at each milestone.
    """
    mc_paths, mc_p = run_monte_carlo(
        closes_train, cfg.num_paths, cfg.horizon,
        cfg.vol_lookback, rng, mu_tilt=mu_tilt,
    )
    mr_paths, mr_p = run_mean_reversion(
        closes_train, cfg.num_paths, cfg.horizon,
        cfg.sma_period, rng,
    )
    pct = blend_percentiles(mc_paths, mr_paths, cfg.blend_mc)
    # pct shape: (5, horizon) for [p10, p25, p50, p75, p90]

    cur_price = float(closes_train[-1])
    result = {"current_price": cur_price, "mu_base": mc_p["mu_base"], "mu": mc_p["mu"]}

    for label, days in EVAL_MILESTONES:
        if days > len(closes_future):
            continue
        idx = days - 1
        realized = float(closes_future[idx])
        median_fc = float(pct[2, idx])  # p50
        result[f"{label}_median"] = median_fc
        result[f"{label}_realized"] = realized
        result[f"{label}_err"] = median_fc - realized
        result[f"{label}_abs_err"] = abs(median_fc - realized)
        result[f"{label}_pct_err"] = (median_fc - realized) / realized * 100
        # Direction: did model predict correct direction?
        model_dir = np.sign(median_fc - cur_price)
        actual_dir = np.sign(realized - cur_price)
        result[f"{label}_dir_hit"] = 1 if model_dir == actual_dir else 0

    return result


def backtest_symbol(
    symbol: str,
    closes: np.ndarray,
    dates: pd.DatetimeIndex,
    sent_df: pd.DataFrame,
    cfg: SentimentBacktestConfig,
) -> list[dict]:
    """Walk-forward backtest for one symbol: baseline + all tilt configs."""
    rows = []
    n = len(closes)
    start_idx = cfg.train_window
    end_idx = n - cfg.horizon
    if end_idx <= start_idx:
        print(f"  [{symbol}] not enough data ({n} days)", file=sys.stderr)
        return rows

    windows = list(range(start_idx, end_idx + 1, cfg.step))
    total_windows = len(windows)

    for wi, t in enumerate(windows):
        train = closes[:t]
        future = closes[t : t + cfg.horizon]
        rebalance_date = dates[t - 1]
        rebalance_ts = pd.Timestamp(rebalance_date)

        # --- Baseline (no sentiment tilt) ---
        rng = np.random.default_rng(cfg.rng_seed + wi)
        baseline = run_single_window(train, future, cfg, rng, mu_tilt=0.0)

        base_row = {
            "symbol": symbol,
            "rebalance_date": rebalance_ts.strftime("%Y-%m-%d"),
            "model": "baseline",
            "tilt_strength": 0.0,
            "lookback_days": 0,
            "sentiment_score": float("nan"),
            "mu_tilt": 0.0,
            **baseline,
        }
        rows.append(base_row)

        # --- Sentiment-tilted variants ---
        for lookback in cfg.lookbacks:
            avg_sent = get_trailing_sentiment(sent_df, symbol, rebalance_ts, lookback)
            if avg_sent is None:
                # No sentiment data for this window — skip tilted models
                continue

            for tilt_str in cfg.tilt_strengths:
                # mu_tilted = mu_base + tilt_strength * avg_sentiment_score
                mu_tilt = tilt_str * avg_sent

                # Use same seed so only the tilt differs
                rng = np.random.default_rng(cfg.rng_seed + wi)
                tilted = run_single_window(train, future, cfg, rng, mu_tilt=mu_tilt)

                tilt_row = {
                    "symbol": symbol,
                    "rebalance_date": rebalance_ts.strftime("%Y-%m-%d"),
                    "model": f"tilt_L{lookback}_S{tilt_str}",
                    "tilt_strength": tilt_str,
                    "lookback_days": lookback,
                    "sentiment_score": avg_sent,
                    "mu_tilt": mu_tilt,
                    **tilted,
                }
                rows.append(tilt_row)

        if (wi + 1) % 10 == 0 or (wi + 1) == total_windows:
            print(f"  [{symbol}] window {wi + 1}/{total_windows}", file=sys.stderr)

    return rows


# ─────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────

def compute_metrics(df: pd.DataFrame, horizon_label: str) -> dict:
    """Compute MAE, RMSE, hit rate, IC for a given horizon from a model group."""
    med_col = f"{horizon_label}_median"
    real_col = f"{horizon_label}_realized"
    err_col = f"{horizon_label}_err"
    dir_col = f"{horizon_label}_dir_hit"

    sub = df.dropna(subset=[med_col, real_col])
    if sub.empty:
        return {"mae": float("nan"), "rmse": float("nan"),
                "hit_rate": float("nan"), "ic": float("nan"), "n": 0}

    errors = sub[med_col] - sub[real_col]
    abs_errors = errors.abs()
    mae = float(abs_errors.mean())
    rmse = float(np.sqrt((errors ** 2).mean()))
    hit_rate = float(sub[dir_col].mean()) if dir_col in sub.columns else float("nan")

    # Information coefficient: rank correlation between forecast return and realized return
    fc_ret = sub[med_col] / sub["current_price"] - 1
    act_ret = sub[real_col] / sub["current_price"] - 1
    if len(fc_ret) > 2:
        try:
            ic = float(fc_ret.corr(act_ret, method="spearman"))
        except (ImportError, ModuleNotFoundError):
            # scipy not installed — fall back to Pearson
            ic = float(fc_ret.corr(act_ret))
    else:
        ic = float("nan")

    return {"mae": mae, "rmse": rmse, "hit_rate": hit_rate, "ic": ic, "n": len(sub)}


# ─────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────

def build_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Build summary table: one row per (model, horizon) with aggregated metrics."""
    summary_rows = []
    models = df["model"].unique()

    for model in sorted(models):
        mdf = df[df["model"] == model]
        for label, _ in EVAL_MILESTONES:
            metrics = compute_metrics(mdf, label)
            if metrics["n"] == 0:
                continue
            # Extract tilt config from the model group
            if model == "baseline":
                tilt_str = 0.0
                lookback = 0
            else:
                tilt_str = mdf["tilt_strength"].iloc[0]
                lookback = int(mdf["lookback_days"].iloc[0])

            summary_rows.append({
                "model": model,
                "tilt_strength": tilt_str,
                "lookback_days": lookback,
                "horizon": label,
                "n": metrics["n"],
                "mae": metrics["mae"],
                "rmse": metrics["rmse"],
                "hit_rate": metrics["hit_rate"],
                "ic": metrics["ic"],
            })

    return pd.DataFrame(summary_rows)


def generate_report(df: pd.DataFrame, summary: pd.DataFrame) -> str:
    """Generate markdown report."""
    lines = ["# Phase C: Sentiment-Tilted Drift Backtest Report\n"]
    lines.append(f"**Total rows:** {len(df):,}  ")
    lines.append(f"**Symbols:** {df['symbol'].nunique()} ({', '.join(sorted(df['symbol'].unique()))})  ")
    lines.append(f"**Date range:** {df['rebalance_date'].min()} to {df['rebalance_date'].max()}  ")
    lines.append(f"**Models tested:** {df['model'].nunique()}\n")

    # --- Baseline vs tilted comparison per horizon ---
    for label, _ in EVAL_MILESTONES:
        hs = summary[summary["horizon"] == label]
        if hs.empty:
            continue

        lines.append(f"\n## {label.upper()} Horizon\n")
        lines.append("| Model | Tilt | Lookback | N | MAE ($) | RMSE ($) | Hit Rate | IC |")
        lines.append("|---|---|---|---|---|---|---|---|")

        baseline_row = hs[hs["model"] == "baseline"]
        baseline_mae = float(baseline_row["mae"].iloc[0]) if not baseline_row.empty else float("nan")

        for _, row in hs.iterrows():
            delta = ""
            if row["model"] != "baseline" and not math.isnan(baseline_mae):
                pct_chg = (row["mae"] - baseline_mae) / baseline_mae * 100
                delta = f" ({pct_chg:+.1f}%)"

            lines.append(
                f"| {row['model']} "
                f"| {row['tilt_strength']:.3f} "
                f"| {int(row['lookback_days'])}d "
                f"| {int(row['n'])} "
                f"| {row['mae']:.2f}{delta} "
                f"| {row['rmse']:.2f} "
                f"| {row['hit_rate']:.1%} "
                f"| {row['ic']:.3f} |"
            )

    # --- Best sentiment config ---
    tilted = summary[summary["model"] != "baseline"]
    if not tilted.empty:
        lines.append("\n## Best Sentiment Configuration\n")
        for label, _ in EVAL_MILESTONES:
            hs = tilted[tilted["horizon"] == label]
            if hs.empty:
                continue
            baseline_row = summary[(summary["model"] == "baseline") & (summary["horizon"] == label)]
            if baseline_row.empty:
                continue
            b_mae = float(baseline_row["mae"].iloc[0])
            b_hit = float(baseline_row["hit_rate"].iloc[0])
            b_ic = float(baseline_row["ic"].iloc[0])

            best_mae = hs.loc[hs["mae"].idxmin()]
            best_hit = hs.loc[hs["hit_rate"].idxmax()]
            best_ic = hs.loc[hs["ic"].idxmax()] if not hs["ic"].isna().all() else None

            lines.append(f"**{label.upper()}:**")
            mae_delta = (best_mae["mae"] - b_mae) / b_mae * 100
            lines.append(f"- Lowest MAE: {best_mae['model']} -- MAE ${best_mae['mae']:.2f} ({mae_delta:+.1f}% vs baseline)")
            hit_delta = best_hit["hit_rate"] - b_hit
            lines.append(f"- Best Hit Rate: {best_hit['model']} -- {best_hit['hit_rate']:.1%} ({hit_delta:+.1%} vs baseline {b_hit:.1%})")
            if best_ic is not None and not math.isnan(best_ic["ic"]):
                ic_delta = best_ic["ic"] - b_ic
                lines.append(f"- Best IC: {best_ic['model']} -- {best_ic['ic']:.3f} ({ic_delta:+.3f} vs baseline {b_ic:.3f})")
            lines.append("")

    # --- Verdict ---
    lines.append("\n## Verdict\n")
    # Compare best tilted MAE at 1yr to baseline
    yr_tilted = tilted[tilted["horizon"] == "1yr"]
    yr_baseline = summary[(summary["model"] == "baseline") & (summary["horizon"] == "1yr")]
    if not yr_tilted.empty and not yr_baseline.empty:
        b_mae = float(yr_baseline["mae"].iloc[0])
        best = yr_tilted.loc[yr_tilted["mae"].idxmin()]
        if best["mae"] < b_mae:
            pct = (b_mae - best["mae"]) / b_mae * 100
            lines.append(
                f"Sentiment tilt **improves** 1-year MAE by {pct:.1f}% "
                f"(best config: {best['model']})."
            )
        else:
            pct = (best["mae"] - b_mae) / b_mae * 100
            lines.append(
                f"Sentiment tilt does **not** improve 1-year MAE (best tilted "
                f"is {pct:.1f}% worse). The baseline MC+MR blend remains superior "
                f"at this horizon."
            )
    else:
        lines.append("Insufficient data for 1-year verdict.")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Phase C: Sentiment-tilted drift backtest"
    )
    parser.add_argument(
        "--symbols", nargs="*", default=DEFAULT_SYMBOLS,
        help="Symbols to test (default: 15 original backtest symbols)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be tested without running",
    )
    parser.add_argument(
        "--tilt-strengths", type=str, default="0.005,0.01,0.02",
        help="Comma-separated annual drift tilt per unit sentiment (default: 0.005,0.01,0.02)",
    )
    parser.add_argument(
        "--lookbacks", type=str, default="5,10,21",
        help="Comma-separated trailing days for sentiment averaging (default: 5,10,21)",
    )
    parser.add_argument(
        "--years", type=int, default=10,
        help="Years of price history to fetch (default: 10)",
    )
    parser.add_argument(
        "--paths", type=int, default=1000,
        help="Number of MC/MR paths per engine (default: 1000)",
    )
    parser.add_argument(
        "--blend", type=float, default=0.30,
        help="MC blend weight 0-1 (default: 0.30 = 30%% MC, 70%% MR)",
    )
    parser.add_argument(
        "--sentiment-csv", type=str, default=SENTIMENT_CSV,
        help=f"Path to sentiment CSV (default: {SENTIMENT_CSV})",
    )
    parser.add_argument(
        "--out-csv", type=str, default="sentiment_backtest_results.csv",
        help="Output CSV path (default: sentiment_backtest_results.csv)",
    )
    parser.add_argument(
        "--out-report", type=str, default="sentiment_backtest_report.md",
        help="Output report path (default: sentiment_backtest_report.md)",
    )
    args = parser.parse_args()

    tilt_strengths = [float(x.strip()) for x in args.tilt_strengths.split(",")]
    lookbacks = [int(x.strip()) for x in args.lookbacks.split(",")]

    cfg = SentimentBacktestConfig(
        history_years=args.years,
        num_paths=args.paths,
        blend_mc=args.blend,
        tilt_strengths=tilt_strengths,
        lookbacks=lookbacks,
    )

    n_configs = len(tilt_strengths) * len(lookbacks)
    print("=" * 70, file=sys.stderr)
    print("Phase C: Sentiment-Tilted Drift Backtest", file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    print(f"Symbols:         {len(args.symbols)} ({', '.join(args.symbols)})", file=sys.stderr)
    print(f"History:         {args.years} years", file=sys.stderr)
    print(f"Train window:    {cfg.train_window} days (~{cfg.train_window // 252}yr)", file=sys.stderr)
    print(f"Test horizon:    {cfg.horizon} days (~{cfg.horizon // 252}yr)", file=sys.stderr)
    print(f"Step:            {cfg.step} days", file=sys.stderr)
    print(f"Paths:           {cfg.num_paths} per engine", file=sys.stderr)
    print(f"Blend:           {cfg.blend_mc:.0%} MC / {1 - cfg.blend_mc:.0%} MR", file=sys.stderr)
    print(f"Tilt strengths:  {tilt_strengths}", file=sys.stderr)
    print(f"Lookbacks:       {lookbacks}", file=sys.stderr)
    print(f"Tilt configs:    {n_configs} (+ baseline)", file=sys.stderr)
    print(f"Sentiment CSV:   {args.sentiment_csv}", file=sys.stderr)
    print("=" * 70, file=sys.stderr)

    if args.dry_run:
        print("\n[DRY RUN] Would test the following configurations:\n", file=sys.stderr)
        print(f"  Baseline (no tilt)", file=sys.stderr)
        for lb in lookbacks:
            for ts in tilt_strengths:
                print(f"  tilt_L{lb}_S{ts}  (lookback={lb}d, strength={ts})", file=sys.stderr)
        print(f"\nTotal per symbol per window: {n_configs + 1} projections", file=sys.stderr)
        print("Exiting (dry run).", file=sys.stderr)
        sys.exit(0)

    # Load sentiment
    print(f"\nLoading sentiment data from {args.sentiment_csv}...", file=sys.stderr)
    try:
        sent_df = load_sentiment(args.sentiment_csv)
    except FileNotFoundError:
        print(f"ERROR: Sentiment CSV not found at {args.sentiment_csv}", file=sys.stderr)
        sys.exit(1)
    print(f"  Loaded {len(sent_df):,} rows, "
          f"{sent_df['ticker'].nunique()} tickers, "
          f"{sent_df['date'].min().date()} to {sent_df['date'].max().date()}",
          file=sys.stderr)

    # Run backtest per symbol
    all_rows = []
    for sym in args.symbols:
        print(f"\nFetching price history for {sym}...", file=sys.stderr)
        result = fetch_price_history(sym, args.years)
        if result is None:
            print(f"  [{sym}] SKIPPED -- insufficient price data (<600 days)", file=sys.stderr)
            continue
        closes, dates = result
        print(f"  [{sym}] {len(closes)} price days, running backtest...", file=sys.stderr)

        rows = backtest_symbol(sym, closes, dates, sent_df, cfg)
        all_rows.extend(rows)
        n_baseline = sum(1 for r in rows if r["model"] == "baseline")
        n_tilted = len(rows) - n_baseline
        print(f"  [{sym}] {n_baseline} baseline + {n_tilted} tilted forecasts", file=sys.stderr)

    if not all_rows:
        print("\nERROR: No forecasts generated. Check symbols and data.", file=sys.stderr)
        sys.exit(1)

    # Compile results
    df = pd.DataFrame(all_rows)
    df.to_csv(args.out_csv, index=False)
    print(f"\nWrote {len(df):,} rows to {args.out_csv}", file=sys.stderr)

    # Summary and report
    summary = build_summary(df)

    # Print summary table to stdout
    print("\n" + "=" * 70)
    print("SUMMARY: Baseline vs Sentiment-Tilted Models")
    print("=" * 70)
    for label, _ in EVAL_MILESTONES:
        hs = summary[summary["horizon"] == label]
        if hs.empty:
            continue
        print(f"\n--- {label.upper()} Horizon ---")
        print(f"{'Model':<25s} {'N':>5s} {'MAE':>10s} {'RMSE':>10s} {'HitRate':>9s} {'IC':>8s}")
        print("-" * 70)
        for _, row in hs.iterrows():
            print(
                f"{row['model']:<25s} "
                f"{int(row['n']):>5d} "
                f"{row['mae']:>10.2f} "
                f"{row['rmse']:>10.2f} "
                f"{row['hit_rate']:>8.1%} "
                f"{row['ic']:>8.3f}"
            )

    # Write report
    report = generate_report(df, summary)
    with open(args.out_report, "w") as f:
        f.write(report)
    print(f"\nWrote report to {args.out_report}", file=sys.stderr)


if __name__ == "__main__":
    main()
