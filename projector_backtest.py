"""
S-Tool Projector — Walk-Forward Backtester

Evaluates the Monte Carlo + Mean Reversion blend by walking through history,
projecting forward N days from each rebalance point, and comparing percentile
forecasts to actual realized prices.

Outputs:
    backtest_results.csv  — raw forecast vs realized rows
    backtest_report.md    — calibration / MAE / bias summary
"""

from __future__ import annotations

import argparse
import sys
import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────

DEFAULT_SYMBOLS = [
    # Mega-cap tech (high vol, strong drift)
    "AAPL", "MSFT", "GOOGL", "NVDA", "AMZN",
    # Broad-market ETFs (low vol, smooth drift)
    "SPY", "QQQ", "IWM",
    # Sectors (varied profiles)
    "XLF", "XLE", "XLV",
    # Value / dividend names
    "JPM", "JNJ", "PG",
    # High-beta single names
    "TSLA",
]

@dataclass
class BacktestConfig:
    history_years: int = 10
    train_window: int = 504        # ~2 years
    horizon: int = 252             # ~1 year forward projection
    step: int = 21                 # walk-forward stride (monthly)
    num_paths: int = 1000          # MC + MR each
    sma_period: int = 200
    vol_lookback: int = 252        # use last N days for sigma estimate
    blend_mc: float = 0.5          # MC weight; MR = 1 - blend_mc
    rng_seed: int = 42
    vix_scale: bool = False        # if True, scale sigma by VIX-regime multiplier
                                    #   at each rebalance date (uses historical VIX,
                                    #   never today's value)

# ─────────────────────────────────────────────────────────────────────
# VIX history (for regime-scaled backtests)
# ─────────────────────────────────────────────────────────────────────

def vix_regime_mult(vix_value: float) -> float:
    """Map a VIX close to a sigma multiplier — same buckets as the dashboard."""
    if vix_value >= 40:
        return 1.50
    if vix_value >= 25:
        return 1.20
    if vix_value >= 15:
        return 1.00
    return 0.85

def fetch_vix_history(years: int) -> pd.Series | None:
    """Fetch ^VIX daily closes and return a date-indexed Series."""
    end = pd.Timestamp.today()
    start = end - pd.DateOffset(years=years + 1)  # +1y so backtest start always has VIX
    df = yf.download("^VIX", start=start, end=end, progress=False, auto_adjust=False)
    if df is None or df.empty:
        return None
    s = df["Close"]
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    s = s.dropna()
    s.index = pd.to_datetime(s.index).normalize()
    return s

def vix_at(vix_series: pd.Series, date: pd.Timestamp) -> float | None:
    """Return the most recent VIX close on or before `date` (no look-ahead)."""
    if vix_series is None or len(vix_series) == 0:
        return None
    d = pd.Timestamp(date).normalize()
    sub = vix_series.loc[:d]
    if len(sub) == 0:
        return None
    return float(sub.iloc[-1])

# ─────────────────────────────────────────────────────────────────────
# Math helpers (port from JS)
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
    n = len(x)
    mx, my = x.mean(), y.mean()
    den = ((x - mx) ** 2).sum()
    slope = 0.0 if den == 0 else ((x - mx) * (y - my)).sum() / den
    return slope, my - slope * mx

# ─────────────────────────────────────────────────────────────────────
# Models (vectorised port of JS engines)
# ─────────────────────────────────────────────────────────────────────

def run_monte_carlo(closes: np.ndarray, num_paths: int, horizon: int,
                    vol_lookback: int, rng: np.random.Generator,
                    sigma_mult: float = 1.0) -> tuple[np.ndarray, dict]:
    """GBM with annualized drift/vol from log returns."""
    log_returns = np.diff(np.log(closes))
    recent = log_returns[-vol_lookback:] if len(log_returns) > vol_lookback else log_returns
    mu = recent.mean() * 252
    sigma_hist = recent.std(ddof=1) * np.sqrt(252)
    sigma = sigma_hist * sigma_mult
    s0 = closes[-1]
    dt = 1 / 252
    drift = (mu - 0.5 * sigma ** 2) * dt
    diff = sigma * np.sqrt(dt)
    z = rng.standard_normal((num_paths, horizon))
    log_paths = np.cumsum(drift + diff * z, axis=1)
    paths = s0 * np.exp(log_paths)
    return paths, {"mu": mu, "sigma": sigma, "sigma_hist": sigma_hist,
                   "sigma_mult": sigma_mult, "s0": s0}

def run_mean_reversion(closes: np.ndarray, num_paths: int, horizon: int,
                       sma_period: int, rng: np.random.Generator,
                       sigma_mult: float = 1.0) -> tuple[np.ndarray, dict]:
    """Ornstein-Uhlenbeck reversion to a trending SMA equilibrium + momentum overlay."""
    s0 = closes[-1]
    period = sma_period if len(closes) >= sma_period else max(10, len(closes) // 2)
    ma = sma(closes, period)
    current_ma = ma[-1]
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
    sigma_ou_hist = residuals.std(ddof=1) * np.sqrt(252)
    sigma_ou = sigma_ou_hist * sigma_mult

    mom_period = min(20, len(closes) - 1)
    momentum = (s0 / closes[-1 - mom_period] - 1) / mom_period

    dt = 1 / 252
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
        "sigma_ou_hist": sigma_ou_hist, "sigma_mult": sigma_mult,
        "current_ma": current_ma, "trend_ann": trend_ann,
        "sma_period": period, "momentum": momentum,
    }

def blend_percentiles(mc_paths: np.ndarray, mr_paths: np.ndarray,
                      blend_mc: float) -> np.ndarray:
    """Return percentiles array of shape (5, horizon) for [10, 25, 50, 75, 90]."""
    n_mc = int(round(blend_mc * (len(mc_paths) + len(mr_paths))))
    n_mr = (len(mc_paths) + len(mr_paths)) - n_mc
    n_mc = min(n_mc, len(mc_paths))
    n_mr = min(n_mr, len(mr_paths))
    combined = np.vstack([mc_paths[:n_mc], mr_paths[:n_mr]])
    return np.percentile(combined, [10, 25, 50, 75, 90], axis=0)

# ─────────────────────────────────────────────────────────────────────
# Walk-forward backtest
# ─────────────────────────────────────────────────────────────────────

MILESTONES = [("1mo", 21), ("3mo", 63), ("6mo", 126), ("1yr", 252)]

def backtest_symbol(symbol: str, closes: np.ndarray, dates: pd.DatetimeIndex,
                    cfg: BacktestConfig,
                    vix_series: pd.Series | None = None) -> list[dict]:
    """Run walk-forward backtest on one symbol; return list of forecast/realized rows."""
    rng = np.random.default_rng(cfg.rng_seed)
    rows = []
    n = len(closes)
    start = cfg.train_window
    end = n - cfg.horizon
    if end <= start:
        print(f"  [{symbol}] not enough data ({n} days)", file=sys.stderr)
        return rows

    windows = list(range(start, end + 1, cfg.step))
    for wi, t in enumerate(windows):
        train = closes[:t]
        future = closes[t : t + cfg.horizon]
        cur_price = train[-1]
        rebalance_date = dates[t - 1]

        # VIX scaling — use historical VIX value AT the rebalance date (no look-ahead)
        sigma_mult = 1.0
        vix_val = None
        if cfg.vix_scale and vix_series is not None:
            vix_val = vix_at(vix_series, rebalance_date)
            if vix_val is not None:
                sigma_mult = vix_regime_mult(vix_val)

        mc_paths, mc_p = run_monte_carlo(train, cfg.num_paths, cfg.horizon,
                                         cfg.vol_lookback, rng, sigma_mult=sigma_mult)
        mr_paths, mr_p = run_mean_reversion(train, cfg.num_paths, cfg.horizon,
                                            cfg.sma_period, rng, sigma_mult=sigma_mult)
        pct = blend_percentiles(mc_paths, mr_paths, cfg.blend_mc)
        # pct rows: 10, 25, 50, 75, 90

        for label, days in MILESTONES:
            if days > len(future):
                continue
            idx = days - 1
            realized = future[idx]
            row = {
                "symbol": symbol,
                "rebalance_date": rebalance_date.strftime("%Y-%m-%d"),
                "horizon": label,
                "horizon_days": days,
                "current_price": float(cur_price),
                "p10": float(pct[0, idx]),
                "p25": float(pct[1, idx]),
                "median": float(pct[2, idx]),
                "p75": float(pct[3, idx]),
                "p90": float(pct[4, idx]),
                "realized": float(realized),
                "mu": float(mc_p["mu"]),
                "sigma": float(mc_p["sigma"]),
                "sigma_mult": float(sigma_mult),
                "vix": float(vix_val) if vix_val is not None else float("nan"),
                "kappa": float(mr_p["kappa"]),
                "half_life": float(mr_p["half_life"]),
                "trend_ann": float(mr_p["trend_ann"]),
            }
            rows.append(row)

        if (wi + 1) % 10 == 0:
            print(f"  [{symbol}] window {wi + 1}/{len(windows)}", file=sys.stderr)
    return rows

def fetch_history(symbol: str, years: int) -> tuple[np.ndarray, pd.DatetimeIndex] | None:
    end = pd.Timestamp.today()
    start = end - pd.DateOffset(years=years)
    df = yf.download(symbol, start=start, end=end, progress=False, auto_adjust=True)
    if df is None or len(df) < 600:
        return None
    closes = df["Close"].values.flatten().astype(float)
    return closes, df.index

# ─────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────

def compute_report(df: pd.DataFrame) -> str:
    """Compute calibration, bias, MAE, directional accuracy by horizon."""
    out = ["# S-Tool Projector Backtest Report\n"]
    out.append(f"**Total forecasts:** {len(df):,}  ")
    out.append(f"**Symbols:** {df['symbol'].nunique()}  ")
    out.append(f"**Date range:** {df['rebalance_date'].min()} → {df['rebalance_date'].max()}\n")

    # Overall by horizon
    out.append("\n## Calibration & Accuracy by Horizon\n")
    out.append("| Horizon | N | 80% Coverage | 50% Coverage | Median Bias % | Median MAE % | Directional Accuracy |")
    out.append("|---|---|---|---|---|---|---|")

    for label, _ in MILESTONES:
        sub = df[df["horizon"] == label]
        if sub.empty:
            continue
        n = len(sub)
        # 80% band coverage = realized in [p10, p90]
        cov80 = ((sub["realized"] >= sub["p10"]) & (sub["realized"] <= sub["p90"])).mean() * 100
        cov50 = ((sub["realized"] >= sub["p25"]) & (sub["realized"] <= sub["p75"])).mean() * 100
        bias_pct = ((sub["median"] - sub["realized"]) / sub["realized"] * 100).mean()
        mae_pct = (abs(sub["median"] - sub["realized"]) / sub["realized"] * 100).mean()
        dir_acc = ((np.sign(sub["median"] - sub["current_price"]) ==
                    np.sign(sub["realized"] - sub["current_price"])).mean()) * 100
        out.append(f"| {label} | {n} | {cov80:.1f}% (target 80%) | {cov50:.1f}% (target 50%) | {bias_pct:+.2f}% | {mae_pct:.2f}% | {dir_acc:.1f}% |")

    # Skill score vs naive (price stays flat)
    out.append("\n## Skill Score vs Naive (price-stays-flat) Baseline\n")
    out.append("Skill = 1 − (model MAE / naive MAE).  Positive = model beats naive.\n")
    out.append("| Horizon | Model MAE % | Naive MAE % | Skill Score |")
    out.append("|---|---|---|---|")
    for label, _ in MILESTONES:
        sub = df[df["horizon"] == label]
        if sub.empty:
            continue
        model_mae = (abs(sub["median"] - sub["realized"]) / sub["realized"] * 100).mean()
        naive_mae = (abs(sub["current_price"] - sub["realized"]) / sub["realized"] * 100).mean()
        skill = 1 - (model_mae / naive_mae) if naive_mae > 0 else 0
        out.append(f"| {label} | {model_mae:.2f}% | {naive_mae:.2f}% | {skill:+.3f} |")

    # Per-symbol summary at 1-year horizon
    out.append("\n## Per-Symbol Summary (1-year horizon)\n")
    out.append("| Symbol | N | 80% Cov | Bias % | MAE % | Dir Acc |")
    out.append("|---|---|---|---|---|---|")
    yr = df[df["horizon"] == "1yr"]
    for sym, g in yr.groupby("symbol"):
        if g.empty:
            continue
        cov80 = ((g["realized"] >= g["p10"]) & (g["realized"] <= g["p90"])).mean() * 100
        bias = ((g["median"] - g["realized"]) / g["realized"] * 100).mean()
        mae = (abs(g["median"] - g["realized"]) / g["realized"] * 100).mean()
        dir_acc = ((np.sign(g["median"] - g["current_price"]) ==
                    np.sign(g["realized"] - g["current_price"])).mean()) * 100
        out.append(f"| {sym} | {len(g)} | {cov80:.1f}% | {bias:+.2f}% | {mae:.2f}% | {dir_acc:.1f}% |")

    return "\n".join(out)

# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS)
    p.add_argument("--years", type=int, default=10)
    p.add_argument("--paths", type=int, default=1000)
    p.add_argument("--sma", type=int, default=200)
    p.add_argument("--vol-lookback", type=int, default=252)
    p.add_argument("--blend", type=float, default=0.5, help="MC weight (0-1)")
    p.add_argument("--out-csv", default="backtest_results.csv")
    p.add_argument("--out-report", default="backtest_report.md")
    args = p.parse_args()

    cfg = BacktestConfig(
        history_years=args.years,
        num_paths=args.paths,
        sma_period=args.sma,
        vol_lookback=args.vol_lookback,
        blend_mc=args.blend,
    )

    print(f"Backtesting {len(args.symbols)} symbols, {args.years}y history, "
          f"{cfg.num_paths} paths, blend {cfg.blend_mc:.0%}/{1-cfg.blend_mc:.0%}",
          file=sys.stderr)

    all_rows = []
    for sym in args.symbols:
        print(f"Fetching {sym}...", file=sys.stderr)
        result = fetch_history(sym, args.years)
        if result is None:
            print(f"  [{sym}] skipped — insufficient data", file=sys.stderr)
            continue
        closes, dates = result
        print(f"  [{sym}] {len(closes)} days, running backtest...", file=sys.stderr)
        rows = backtest_symbol(sym, closes, dates, cfg)
        all_rows.extend(rows)
        print(f"  [{sym}] {len(rows)} forecasts", file=sys.stderr)

    if not all_rows:
        print("No forecasts generated", file=sys.stderr)
        sys.exit(1)

    df = pd.DataFrame(all_rows)
    df.to_csv(args.out_csv, index=False)
    print(f"\nWrote {len(df)} rows → {args.out_csv}", file=sys.stderr)

    report = compute_report(df)
    with open(args.out_report, "w") as f:
        f.write(report)
    print(f"Wrote report → {args.out_report}", file=sys.stderr)

    print("\n" + report)

if __name__ == "__main__":
    main()
