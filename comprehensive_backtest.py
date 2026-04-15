"""
comprehensive_backtest.py
─────────────────────────
Walk-forward backtest evaluating Public Pulse tilt alongside the existing
sentiment tilt. Builds on sentiment_backtest.py's engine.

Scope note: we only backtest tilts for which we have HISTORICAL data:
  • Sentiment tilt (WSB FinBERT) — from sentiment_data/
  • Public Pulse composite — from public_pulse_data/public_pulse_combined_*.csv

Other live tilts (analyst target, EPS, Finnhub recs, put/call) use CURRENT-only
API data — they cannot be walk-forward-backtested on free tiers. Their forward
skill will be measured by tracking live predictions going forward.

Modes:
  baseline      — no tilts (control)
  sent_only     — sentiment tilt only (reproduces Phase C result as control)
  pp_only       — Public Pulse tilt only
  combined      — sentiment + PP additive tilts
  sweep         — grid sweep over PP tilt weights + sentiment tilt strengths

Usage:
    # Pilot with 5 symbols, fast config
    python3 comprehensive_backtest.py --symbols AAPL MSFT GOOGL NVDA TSLA \\
        --start 2024-01-01 --end 2025-12-01

    # Full sweep on 20 liquid names
    python3 comprehensive_backtest.py --mode sweep --symbols AAPL MSFT ... \\
        --start 2024-01-01 --end 2025-12-01

Output:
    comprehensive_backtest_results.csv — all forecast rows (window × symbol × config)
    comprehensive_backtest_report.md    — MAPE/hit-rate by config, winner tables
"""

from __future__ import annotations

import argparse
import logging
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logger = logging.getLogger("ctest")

# Reuse core engine + helpers from sentiment_backtest
from sentiment_backtest import (
    fetch_price_history,
    get_trailing_sentiment,
    run_single_window,
    SentimentBacktestConfig,
    EVAL_MILESTONES,
    load_sentiment,
)

PP_DATA_DIR = Path("public_pulse_data")


# ─────────────────────────────────────────────────────────────────────
# Public Pulse tilt lookup
# ─────────────────────────────────────────────────────────────────────

def load_public_pulse(start: str, end: str) -> Optional[pd.DataFrame]:
    """Load the pre-built combined PP CSV for the given window."""
    path = PP_DATA_DIR / f"public_pulse_combined_{start}_{end}.csv"
    if not path.exists():
        logger.warning("Public Pulse data not found: %s", path)
        return None
    df = pd.read_csv(path)
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"])
    return df


def get_trailing_pp(
    pp_df: pd.DataFrame, symbol: str, rebalance: pd.Timestamp, lookback_days: int = 21,
) -> Optional[float]:
    """Average PP composite over the `lookback_days` preceding `rebalance`."""
    if pp_df is None:
        return None
    cutoff_start = rebalance - pd.Timedelta(days=lookback_days)
    sub = pp_df[(pp_df.symbol == symbol) &
                (pp_df.date >= cutoff_start) &
                (pp_df.date < rebalance) &
                (pp_df.composite.notna())]
    if sub.empty:
        return None
    return float(sub.composite.mean())


# ─────────────────────────────────────────────────────────────────────
# Config grid
# ─────────────────────────────────────────────────────────────────────

@dataclass
class TiltConfig:
    """A single model configuration to evaluate."""
    name: str
    sent_lookback: int = 0      # 0 = no sentiment tilt
    sent_strength: float = 0.0
    pp_lookback: int = 0        # 0 = no PP tilt
    pp_strength: float = 0.0
    netliq_strength: float = 0.0  # 0 = no net-liq tilt
    form4_strength: float = 0.0   # 0 = no insider-buying tilt
    hy_oas_strength: float = 0.0       # 0 = no HY OAS tilt
    margin_debt_strength: float = 0.0  # 0 = no margin-debt tilt

    def mu_tilt(self, avg_sent: Optional[float], avg_pp: Optional[float],
                netliq_signal: Optional[float] = None,
                form4_signal: Optional[float] = None,
                hy_oas_signal: Optional[float] = None,
                margin_debt_signal: Optional[float] = None) -> float:
        tilt = 0.0
        if self.sent_strength != 0 and avg_sent is not None:
            tilt += self.sent_strength * avg_sent
        if self.pp_strength != 0 and avg_pp is not None:
            tilt += self.pp_strength * avg_pp
        if self.netliq_strength != 0 and netliq_signal is not None:
            tilt += self.netliq_strength * netliq_signal
        if self.form4_strength != 0 and form4_signal is not None:
            tilt += self.form4_strength * form4_signal
        if self.hy_oas_strength != 0 and hy_oas_signal is not None:
            tilt += self.hy_oas_strength * hy_oas_signal
        if self.margin_debt_strength != 0 and margin_debt_signal is not None:
            tilt += self.margin_debt_strength * margin_debt_signal
        # Clamp at ±10% annual drift (same as production engine)
        return max(-0.10, min(0.10, tilt))


def default_configs(mode: str) -> List[TiltConfig]:
    base = TiltConfig("baseline")

    # Winning sentiment config from Phase C: L21_S0.005
    sent_only = TiltConfig("sent_only", sent_lookback=21, sent_strength=0.005)

    # PP-only configs at production weight 0.15 (scaled to match sent_strength scale)
    # PP composite is in [-1, +1], so strength 0.06 maps to ±6% implied drift — same as live engine
    pp_only = TiltConfig("pp_only", pp_lookback=21, pp_strength=0.06)

    # Combined — just add them
    combined = TiltConfig("combined", sent_lookback=21, sent_strength=0.005,
                         pp_lookback=21, pp_strength=0.06)

    if mode == "baseline":
        return [base]
    if mode == "sent_only":
        return [base, sent_only]
    if mode == "pp_only":
        return [base, pp_only]
    if mode == "combined":
        return [base, sent_only, pp_only, combined]
    if mode == "netliq_sweep":
        # Net-liquidity tilt signal is already in [-1, +1] (z-score scaled)
        # Test multiple strengths to find the optimum
        configs = [base]
        for s in [0.02, 0.04, 0.06, 0.08, 0.10]:
            configs.append(TiltConfig(f"netliq_S{s}", netliq_strength=s))
        return configs
    if mode == "form4_sweep":
        # Form-4 insider-buying tilt signal in [-1, +1]
        configs = [base]
        for s in [0.02, 0.04, 0.06, 0.08, 0.10]:
            configs.append(TiltConfig(f"form4_S{s}", form4_strength=s))
        return configs
    if mode == "hy_oas_sweep":
        configs = [base]
        for s in [0.02, 0.04, 0.06, 0.08, 0.10]:
            configs.append(TiltConfig(f"hyoas_S{s}", hy_oas_strength=s))
        return configs
    if mode == "margin_debt_sweep":
        configs = [base]
        for s in [0.02, 0.04, 0.06, 0.08, 0.10]:
            configs.append(TiltConfig(f"mdebt_S{s}", margin_debt_strength=s))
        return configs
    if mode == "macro_combined":
        # Test each macro signal alone + their combination at production-scale strength
        return [
            base,
            TiltConfig("hyoas_S0.06", hy_oas_strength=0.06),
            TiltConfig("mdebt_S0.06", margin_debt_strength=0.06),
            TiltConfig("macro_combo", hy_oas_strength=0.06, margin_debt_strength=0.06),
        ]
    if mode == "sweep":
        configs = [base, sent_only]
        # PP strength sweep — find the calibration that beats sent_only
        for pp_str in [0.02, 0.04, 0.06, 0.08, 0.10]:
            configs.append(TiltConfig(f"pp_S{pp_str}", pp_lookback=21, pp_strength=pp_str))
            configs.append(TiltConfig(f"combo_S{pp_str}", sent_lookback=21,
                                      sent_strength=0.005, pp_lookback=21, pp_strength=pp_str))
        # Lookback sensitivity on the best PP strength
        for lb in [7, 14, 21, 30, 60]:
            configs.append(TiltConfig(f"pp_L{lb}", pp_lookback=lb, pp_strength=0.06))
        return configs
    raise ValueError(f"Unknown mode: {mode}")


# ─────────────────────────────────────────────────────────────────────
# Walk-forward
# ─────────────────────────────────────────────────────────────────────

def get_netliq_signal(netliq_df: Optional[pd.DataFrame],
                      rebalance: pd.Timestamp) -> Optional[float]:
    """Most recent net-liquidity tilt signal at or before `rebalance`."""
    if netliq_df is None:
        return None
    sub = netliq_df[netliq_df.date <= rebalance]
    if sub.empty:
        return None
    val = sub.iloc[-1]["tilt_signal"]
    return float(val) if pd.notna(val) else None


def get_form4_signal(form4_df: Optional[pd.DataFrame], symbol: str,
                     rebalance: pd.Timestamp) -> Optional[float]:
    """Form-4 insider-buying tilt signal for `symbol` at or before `rebalance`."""
    if form4_df is None:
        return None
    sub = form4_df[(form4_df.symbol == symbol) & (form4_df.date <= rebalance)]
    if sub.empty:
        return None
    val = sub.iloc[-1]["tilt_signal"]
    return float(val) if pd.notna(val) else None


def get_macro_signal(macro_df: Optional[pd.DataFrame],
                     rebalance: pd.Timestamp) -> Optional[float]:
    """Most recent macro (HY OAS / margin debt) tilt signal at or before `rebalance`."""
    if macro_df is None:
        return None
    sub = macro_df[macro_df.date <= rebalance]
    if sub.empty:
        return None
    val = sub.iloc[-1]["tilt_signal"]
    return float(val) if pd.notna(val) else None


def backtest_symbol(
    symbol: str, closes: np.ndarray, dates: pd.DatetimeIndex,
    sent_df: Optional[pd.DataFrame], pp_df: Optional[pd.DataFrame],
    configs: List[TiltConfig], cfg: SentimentBacktestConfig,
    netliq_df: Optional[pd.DataFrame] = None,
    form4_df: Optional[pd.DataFrame] = None,
    hy_oas_df: Optional[pd.DataFrame] = None,
    margin_debt_df: Optional[pd.DataFrame] = None,
) -> List[dict]:
    rows: List[dict] = []
    n = len(closes)
    start_idx = cfg.train_window
    end_idx = n - cfg.horizon
    if end_idx <= start_idx:
        logger.warning("[%s] not enough data (%d days)", symbol, n)
        return rows

    windows = list(range(start_idx, end_idx + 1, cfg.step))
    for wi, t in enumerate(windows):
        train = closes[:t]
        future = closes[t:t + cfg.horizon]
        rebalance = pd.Timestamp(dates[t - 1])

        # Precompute tilt inputs once per window
        avg_sents = {}
        avg_pps = {}
        for c in configs:
            if c.sent_lookback and c.sent_lookback not in avg_sents and sent_df is not None:
                avg_sents[c.sent_lookback] = get_trailing_sentiment(
                    sent_df, symbol, rebalance, c.sent_lookback)
            if c.pp_lookback and c.pp_lookback not in avg_pps and pp_df is not None:
                avg_pps[c.pp_lookback] = get_trailing_pp(
                    pp_df, symbol, rebalance, c.pp_lookback)
        netliq_sig = get_netliq_signal(netliq_df, rebalance)
        form4_sig = get_form4_signal(form4_df, symbol, rebalance)
        hy_oas_sig = get_macro_signal(hy_oas_df, rebalance)
        margin_debt_sig = get_macro_signal(margin_debt_df, rebalance)

        for c in configs:
            sent = avg_sents.get(c.sent_lookback) if c.sent_lookback else None
            pp = avg_pps.get(c.pp_lookback) if c.pp_lookback else None
            mu_tilt = c.mu_tilt(sent, pp, netliq_signal=netliq_sig,
                                form4_signal=form4_sig,
                                hy_oas_signal=hy_oas_sig,
                                margin_debt_signal=margin_debt_sig)
            # Same RNG seed across configs for apples-to-apples comparison
            rng = np.random.default_rng(cfg.rng_seed + wi)
            result = run_single_window(train, future, cfg, rng, mu_tilt=mu_tilt)
            rows.append({
                "symbol": symbol,
                "rebalance_date": rebalance.strftime("%Y-%m-%d"),
                "config": c.name,
                "sent_lookback": c.sent_lookback,
                "sent_strength": c.sent_strength,
                "pp_lookback": c.pp_lookback,
                "pp_strength": c.pp_strength,
                "netliq_strength": c.netliq_strength,
                "form4_strength": c.form4_strength,
                "hy_oas_strength": c.hy_oas_strength,
                "margin_debt_strength": c.margin_debt_strength,
                "avg_sent": sent if sent is not None else float("nan"),
                "avg_pp": pp if pp is not None else float("nan"),
                "netliq_sig": netliq_sig if netliq_sig is not None else float("nan"),
                "form4_sig": form4_sig if form4_sig is not None else float("nan"),
                "hy_oas_sig": hy_oas_sig if hy_oas_sig is not None else float("nan"),
                "margin_debt_sig": margin_debt_sig if margin_debt_sig is not None else float("nan"),
                "mu_tilt": mu_tilt,
                **result,
            })

        if (wi + 1) % 10 == 0:
            logger.info("  [%s] window %d/%d", symbol, wi + 1, len(windows))

    return rows


# ─────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────

def summarize(df: pd.DataFrame) -> pd.DataFrame:
    """Per (config × horizon): MAE, MAPE, hit rate, skill vs baseline."""
    out_rows = []
    baseline = df[df.config == "baseline"]
    for horizon_lbl, _days in EVAL_MILESTONES:
        med = f"{horizon_lbl}_median"
        real = f"{horizon_lbl}_realized"
        abs_err = f"{horizon_lbl}_abs_err"
        hit = f"{horizon_lbl}_dir_hit"
        pct_err = f"{horizon_lbl}_pct_err"

        # Skip horizons without data (e.g. --horizon shorter than this milestone)
        if real not in df.columns or med not in df.columns:
            continue

        # Baseline MAE for skill normalization
        b_sub = baseline.dropna(subset=[real, med])
        base_mae = (b_sub[med] - b_sub[real]).abs().mean() if len(b_sub) else float("nan")
        base_mape = b_sub[pct_err].abs().mean() if len(b_sub) else float("nan")

        for cfg_name, grp in df.groupby("config"):
            sub = grp.dropna(subset=[real, med])
            if sub.empty:
                continue
            mae = (sub[med] - sub[real]).abs().mean()
            mape = sub[pct_err].abs().mean()
            hit_rate = sub[hit].mean() * 100
            skill = 1 - mae / base_mae if base_mae else float("nan")
            mape_impr = (base_mape - mape) / base_mape * 100 if base_mape else float("nan")
            out_rows.append({
                "config": cfg_name,
                "horizon": horizon_lbl,
                "n": len(sub),
                "mae": round(mae, 2),
                "mape_pct": round(mape, 2),
                "mape_delta_vs_base_pct": round(mape_impr, 2),
                "hit_rate_pct": round(hit_rate, 1),
                "skill_vs_base": round(skill, 4),
            })
    return pd.DataFrame(out_rows)


def write_report(summary: pd.DataFrame, path: Path, meta: dict) -> None:
    lines = [
        "# Comprehensive Backtest Report",
        "",
        f"**Run:** {meta['run_at']}  ",
        f"**Symbols:** {meta['n_symbols']} · **Windows:** {meta['n_windows']} · **Total rows:** {meta['n_rows']}  ",
        f"**Period:** {meta['start']} → {meta['end']}",
        "",
    ]
    for horizon_lbl, _days in EVAL_MILESTONES:
        sub = summary[summary.horizon == horizon_lbl].sort_values("mape_delta_vs_base_pct", ascending=False)
        lines.append(f"## {horizon_lbl.upper()} horizon")
        lines.append("")
        lines.append("| Config | N | MAPE | ΔMAPE vs base | Hit % | Skill |")
        lines.append("|---|--:|--:|--:|--:|--:|")
        for _, r in sub.iterrows():
            lines.append(
                f"| {r.config} | {r.n} | {r.mape_pct}% | {r.mape_delta_vs_base_pct:+.2f}% | "
                f"{r.hit_rate_pct}% | {r.skill_vs_base:+.4f} |"
            )
        lines.append("")
    path.write_text("\n".join(lines))
    logger.info("Wrote report → %s", path)


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="+")
    p.add_argument("--all-symbols", action="store_true",
                   help="Use full 107-symbol DEFAULT_UNIVERSE (sidesteps shell quoting)")
    p.add_argument("--full-universe", action="store_true",
                   help="Use the 537-symbol S&P 500 + Nasdaq 100 + WSB universe (from worker.py)")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--mode", default="sweep",
                   choices=["baseline", "sent_only", "pp_only", "combined", "sweep",
                            "netliq_sweep", "form4_sweep",
                            "hy_oas_sweep", "margin_debt_sweep", "macro_combined"])
    p.add_argument("--sentiment-csv",
                   default="sentiment_data/sentiment_combined_2023-01-01_2026-04-15.csv")
    p.add_argument("--num-paths", type=int, default=1000)
    p.add_argument("--horizon", type=int, default=252)
    p.add_argument("--train-window", type=int, default=504)
    p.add_argument("--step", type=int, default=21)
    p.add_argument("--out", default="comprehensive_backtest_results.csv")
    p.add_argument("--report", default="comprehensive_backtest_report.md")

    args = p.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    # Load tilt data sources
    try:
        sent_df = load_sentiment(args.sentiment_csv)
        logger.info("Loaded sentiment: %d rows", len(sent_df))
    except Exception as e:
        logger.warning("No sentiment data (%s); continuing without", e)
        sent_df = None

    pp_df = load_public_pulse(args.start, args.end)
    if pp_df is not None:
        logger.info("Loaded PP data: %d rows, %d symbols", len(pp_df), pp_df.symbol.nunique())

    # Net liquidity time series (macro — applies to all symbols)
    netliq_df = None
    try:
        import net_liquidity
        netliq_df = net_liquidity.tilt_timeseries()
        logger.info("Loaded net-liq: %d rows, %s to %s",
                    len(netliq_df), netliq_df.date.min().date(), netliq_df.date.max().date())
    except Exception as e:
        logger.warning("Net-liquidity load failed: %s", e)

    # Form 4 insider buying (stock-level — per-symbol tilt)
    form4_df = None
    try:
        import form4_insider
        form4_df = form4_insider.tilt_timeseries()
        if form4_df is not None:
            logger.info("Loaded Form 4: %d rows, %d symbols",
                        len(form4_df), form4_df.symbol.nunique())
    except Exception as e:
        logger.warning("Form 4 load failed: %s", e)

    # Macro signals (HY OAS + margin debt — macro, applies to all symbols)
    hy_oas_df = None
    margin_debt_df = None
    try:
        import macro_signals
        hy_oas_df = macro_signals.hy_oas_tilt_timeseries()
        if hy_oas_df is not None:
            logger.info("Loaded HY OAS: %d rows, %s to %s",
                        len(hy_oas_df), hy_oas_df.date.min().date(), hy_oas_df.date.max().date())
        margin_debt_df = macro_signals.margin_debt_tilt_timeseries()
        if margin_debt_df is not None:
            logger.info("Loaded margin debt: %d rows, %s to %s",
                        len(margin_debt_df), margin_debt_df.date.min().date(),
                        margin_debt_df.date.max().date())
    except Exception as e:
        logger.warning("Macro signals load failed: %s", e)

    configs = default_configs(args.mode)
    logger.info("Mode '%s' → %d configs: %s",
                args.mode, len(configs), [c.name for c in configs])

    cfg = SentimentBacktestConfig(
        num_paths=args.num_paths,
        horizon=args.horizon,
        train_window=args.train_window,
        step=args.step,
    )

    # Resolve symbol list
    if args.full_universe:
        from worker import FULL_UNIVERSE
        symbols = FULL_UNIVERSE
    elif args.all_symbols:
        from public_pulse_backfill import DEFAULT_UNIVERSE
        symbols = DEFAULT_UNIVERSE
    elif args.symbols:
        symbols = args.symbols
    else:
        logger.error("Must pass --symbols or --all-symbols")
        sys.exit(1)

    # Walk-forward each symbol
    all_rows: List[dict] = []
    for i, sym in enumerate(symbols):
        logger.info("── [%d/%d] %s ──", i + 1, len(symbols), sym)
        hist = fetch_price_history(sym, years=5)
        if hist is None:
            logger.error("  skip %s: no price history", sym)
            continue
        closes, dates = hist
        try:
            rows = backtest_symbol(sym, closes, dates, sent_df, pp_df, configs, cfg,
                                   netliq_df=netliq_df, form4_df=form4_df,
                                   hy_oas_df=hy_oas_df, margin_debt_df=margin_debt_df)
            all_rows.extend(rows)
        except Exception as e:
            logger.error("  [%s] skipped: %s", sym, e)
            continue

    if not all_rows:
        logger.error("No results"); sys.exit(1)

    df = pd.DataFrame(all_rows)
    df.to_csv(args.out, index=False)
    logger.info("Wrote %d rows → %s", len(df), args.out)

    summary = summarize(df)
    meta = {
        "run_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n_symbols": df.symbol.nunique(),
        "n_windows": df.rebalance_date.nunique(),
        "n_rows": len(df),
        "start": args.start,
        "end": args.end,
    }
    write_report(summary, Path(args.report), meta)

    # Print leaderboard at 1yr horizon
    print("\n── 1yr leaderboard (MAPE improvement vs baseline) ──")
    lead = summary[summary.horizon == "1yr"].sort_values("mape_delta_vs_base_pct", ascending=False)
    for _, r in lead.iterrows():
        print(f"  {r.config:22s}  ΔMAPE {r.mape_delta_vs_base_pct:+7.2f}%  hit {r.hit_rate_pct:.1f}%")


if __name__ == "__main__":
    main()
