# Alpaca integration plan

> Scoped 2026-04-16 evening. Goal: take the daily `portfolio_picks.json`
> output and trade it on Alpaca — paper first, live later. Estimated
> **10–14 focused workdays to swing paper trading**, then 4–8 weeks of
> paper-live comparison before real money.

## What this is (and isn't)

**Is:** swing / short-position trading based on the existing NN picks
(horizon: weeks to ~12 months). Trade = buy top-N picks, hold, rebalance
on a fixed cadence.

**Isn't:** intraday day trading. The NN was trained on 12-month realized
returns — it's the wrong tool for intraday moves. Day trading is a
separate project needing minute-bar data, intraday features, and a
different model entirely.

## Phases

### Phase 0 — fix backtest caveats (blocks paper trading)

Must be done before trading **anything**, because current backtest
numbers are inflated by these issues.

- [ ] **Split-adjustment bug** — HTZ/RUN show post-reverse-split prices
      in the cache. Retroactively apply split history to `data_cache/prices/`.
- [ ] **Survivorship bias** — delisted tickers are absent from the price
      cache, so the universe is artificially filtered to "survivors". Pull
      a historical IWV constituents table and flag delisted names in
      `upside_hunt.py`.
- [ ] **Out-of-sample holdout** — reserve the most recent window (or
      the next new window when it arrives) as a **never-touched** holdout.
      No feature decisions, no hyperparameter choices, nothing. Validate
      the NN's lift there before risking real orders.
- [ ] **Transaction cost model** — add 5 bps slippage + 1 bp commission
      to the simulated portfolio in `overnight_backtest.py`. Verify lift
      holds after cost drag.

### Phase 1 — Alpaca paper trading MVP (5 workdays)

Minimal viable trading loop against Alpaca paper.

- [ ] **Alpaca account** (paper is free). Generate API key + secret.
      Store as `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` on Railway
      (never commit).
- [ ] **SDK selection**: use `alpaca-py` (official, modern), not the
      older `alpaca-trade-api`.
- [ ] **New module** `alpaca_broker.py`:
      - Thin wrapper over `alpaca.trading.client.TradingClient`
      - Account info, positions, order submission, order status
      - Paper/live flag controlled by env var
- [ ] **New module** `trade_executor.py`:
      - Reads today's `portfolio_picks.json`
      - Computes target portfolio: top-N picks, equal-weight
      - Diffs against current Alpaca positions
      - Submits market-on-open (MOO) orders for entries/exits
- [ ] **Scheduling**: new GitHub Action `alpaca-paper.yml` running
      Monday–Friday at 13:25 UTC (5 min before NYSE open), gated on
      `PAPER_TRADING_ENABLED=true` env var
- [ ] **Logging**: every order + fill + position change written to
      `data_cache/alpaca_log.jsonl` (append-only)

### Phase 2 — safety rails (3 workdays)

Before the paper loop runs unattended, add the things that prevent
catastrophic behavior when the model does something unexpected.

- [ ] **Position sizing caps**: max 10% of portfolio in any single
      ticker, max 40% in any single sector
- [ ] **Portfolio drawdown halt**: if paper equity drops >15% from
      peak, halt new entries until manual re-enable
- [ ] **Ticker blocklist**: any ticker under $3/share, ADV < $500k, or
      in pre-market halt doesn't get an order (already partially
      enforced in `portfolio_scanner.py` but needs to be enforced at
      order-time too, since the picks list can be stale)
- [ ] **Preflight sanity check**: if today's picks list has fewer than
      5 tickers, or any pick has confidence < 30, skip the day
- [ ] **Manual kill switch**: `ALPACA_EMERGENCY_STOP=true` env var that
      cancels all open orders and exits all positions on next run

### Phase 3 — performance tracking (2 workdays)

The whole point of paper trading is to validate backtest predictions
against reality. Need instrumentation to do this.

- [ ] **Daily reconciliation job**: computes paper portfolio return,
      compares to the backtest's simulated return for the same
      rebalance. Writes to `data_cache/paper_vs_backtest.jsonl`
- [ ] **Slippage attribution**: every fill's actual price vs expected
      (prev-close or open-print). Surfaces where the paper market
      prints diverged from what the backtest assumed
- [ ] **Public dashboard page** `/track-record/live` showing paper
      equity curve alongside the backtest curve. Updated daily.
      (User-facing proof that the model works — or doesn't.)

### Phase 4 — live trading gate (only after 4–8 weeks of paper)

**Only proceed if:**
- Paper returns track backtest within reasonable tolerance (e.g.
  monthly returns within ±30% of predicted)
- No safety-rail trip events during paper run
- At least one full drawdown-and-recovery cycle observed
- Legal/tax posture understood (S-corp trading account? Personal?
  Disclosure for SaaS that publishes the same picks you trade?)

Live-specific work (**2–3 workdays** on top of paper):
- [ ] Swap `paper=True` → `paper=False` in Alpaca client
- [ ] Fund live account (minimum for swing: $25k would be comfortable;
      below that PDT rules start mattering if intraday activity creeps in)
- [ ] Tighter drawdown halt (5% instead of 15%)
- [ ] Daily reconciliation must pass before next day's orders submit

## Open decisions you need to make

1. **Rebalance cadence**: daily, weekly (Monday), or monthly?
   - Daily = closest to backtest, highest turnover/cost
   - Weekly = smoother, cheaper, but lags signal decay
   - **Recommended**: weekly for paper, revisit after seeing cost drag
2. **Universe**: Russell 3000 is the training universe. Paper should
   match, but live might want to restrict to S&P 1500 for liquidity.
3. **Position count**: backtest uses top-20. Paper should match.
4. **Capital allocation**: how much of the paper account in the model
   portfolio vs cash? Backtest assumes 100% equity. Recommend 100% for
   paper to match; for live, discuss 80/20 equity/cash as a drawdown
   buffer.
5. **Legal / disclosure**: you publish these picks to paying users. If
   you also trade them, you should probably (a) document that you do,
   (b) trade *after* picks go live so you're not front-running, and
   (c) talk to a lawyer about whether this needs RIA registration.
   Not my lane — flag for counsel.

## Concrete first-week tasks (ordered)

1. Day 1: Sign up for Alpaca paper account. Generate API keys.
   Add `alpaca-py` to requirements.txt.
2. Day 2: Build `alpaca_broker.py` wrapper. Hand-test against paper:
   get account, place a market order, cancel it, check positions.
3. Day 3: Build `trade_executor.py`. Dry-run mode that prints intended
   orders without submitting. Run against today's picks.json, eyeball
   output for sanity.
4. Day 4: Fix top-priority backtest caveat (split adjustment). This is
   Phase 0 work interleaved with integration.
5. Day 5: Wire `alpaca-paper.yml` workflow. Run it once manually with
   `workflow_dispatch`. Verify paper account state updates.

## Files this will touch

- **New**: `alpaca_broker.py`, `trade_executor.py`,
  `docs/alpaca_integration_plan.md` (this file),
  `.github/workflows/alpaca-paper.yml`
- **Modified**: `requirements.txt` (+`alpaca-py`),
  `portfolio_scanner.py` (add hard liquidity gate for orders),
  `overnight_backtest.py` (transaction cost model),
  `preflight.py` (block deploy if paper account is unreachable)

## What this does NOT include

- Options trading (Alpaca supports it, but our signal is for equities)
- Margin / leverage (default to cash-only to match backtest assumption)
- Short selling (the NN finds upside; doesn't predict downside)
- Intraday entries/exits (separate project — would need new model)
