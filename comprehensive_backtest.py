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

    def mu_tilt(self, avg_sent: Optional[float], avg_pp: Optional[float]) -> float:
        tilt = 0.0
        if self.sent_strength != 0 and avg_sent is not None:
            tilt += self.sent_strength * avg_sent
        if self.pp_strength != 0 and avg_pp is not None:
            tilt += self.pp_strength * avg_pp
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

def backtest_symbol(
    symbol: str, closes: np.ndarray, dates: pd.DatetimeIndex,
    sent_df: Optional[pd.DataFrame], pp_df: Optional[pd.DataFrame],
    configs: List[TiltConfig], cfg: SentimentBacktestConfig,
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

        for c in configs:
            sent = avg_sents.get(c.sent_lookback) if c.sent_lookback else None
            pp = avg_pps.get(c.pp_lookback) if c.pp_lookback else None
            mu_tilt = c.mu_tilt(sent, pp)
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
                "avg_sent": sent if sent is not None else float("nan"),
                "avg_pp": pp if pp is not None else float("nan"),
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
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--mode", default="sweep",
                   choices=["baseline", "sent_only", "pp_only", "combined", "sweep"])
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

    configs = default_configs(args.mode)
    logger.info("Mode '%s' → %d configs: %s",
                args.mode, len(configs), [c.name for c in configs])

    cfg = SentimentBacktestConfig(
        num_paths=args.num_paths,
        horizon=args.horizon,
        train_window=args.train_window,
        step=args.step,
    )

    # Walk-forward each symbol
    all_rows: List[dict] = []
    for i, sym in enumerate(args.symbols):
        logger.info("── [%d/%d] %s ──", i + 1, len(args.symbols), sym)
        hist = fetch_price_history(sym, years=5)
        if hist is None:
            logger.error("  skip %s: no price history", sym)
            continue
        closes, dates = hist
        rows = backtest_symbol(sym, closes, dates, sent_df, pp_df, configs, cfg)
        all_rows.extend(rows)

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
