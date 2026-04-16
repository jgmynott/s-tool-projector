# Fintech / Quant SaaS Pricing Research for s-tool.io

*Compiled 2026-04-15. Prices verified from live pricing pages where possible; sites behind Cloudflare or client-side rendering are marked with (~) to indicate the price may have changed since last public snapshot. Always re-verify before quoting externally.*

---

## 1. Koyfin (koyfin.com/pricing) -- VERIFIED

**What they offer:** Financial data terminal -- charting, screeners, portfolio analytics, macro dashboards, company snapshots, filings, transcripts.

| Tier | Monthly | Annual (per mo) | Key gates |
|------|---------|-----------------|-----------|
| Free | $0 | $0 | 2Y financials, 2 watchlists, 2 screens, limited snapshots/news |
| Plus | $49 | $39 | 10Y financials/estimates, ETF holdings, screener, unlimited watchlists/dashboards, premium news |
| Premium | $110 | $79 | Portfolio advanced analytics, custom formulas, custom data, ETF valuation |
| Advisor Core | $239 | $209 | Model portfolios, client proposals (10/mo), custodian integration, mutual funds |
| Advisor Pro | $349 | $299 | Custom report pages, 50 proposals/mo, multiple integrations, priority support |

**Metered elements:** Client proposals are capped per month per tier.

---

## 2. TipRanks (tipranks.com/pricing) (~)

**What they offer:** Analyst consensus ratings, smart score stock ranking, insider trading signals, hedge fund tracking, portfolio analysis.

| Tier | Monthly | Annual (per mo) | Key gates |
|------|---------|-----------------|-----------|
| Free | $0 | $0 | Limited analyst data, basic stock pages |
| Premium | ~$50 | ~$30 | Unlimited Smart Score data, analyst top picks, insider signals, hedge fund portfolio tracking |
| Ultimate | ~$83 | ~$50 | Everything in Premium + AI-powered stock analysis, personalized alerts, portfolio manager tools |

**Metered elements:** None reported; flat subscription tiers.

---

## 3. Seeking Alpha (seekingalpha.com/pricing) (~)

**What they offer:** Crowd-sourced equity analysis, quant ratings, dividend grades, Alpha Picks stock recommendations, earnings analysis.

| Tier | Monthly | Annual (per mo) | Key gates |
|------|---------|-----------------|-----------|
| Free (Basic) | $0 | $0 | Limited articles, basic stock pages |
| Premium | ~$35 | ~$20 | Quant/author/Wall St ratings, dividend grades, earnings transcripts, unlimited articles |
| Alpha Picks | ~$50 add-on | ~$33 add-on | Curated buy/sell picks from their quant model -- this is their "stock recommendation" product |

**Metered elements:** Article views are soft-gated on free tier.

---

## 4. Finviz (finviz.com/elite.ashx) -- VERIFIED

**What they offer:** Stock screener, heat maps, charts with pattern recognition, portfolios, real-time data, alerts, ETF analytics.

| Tier | Monthly | Annual (per mo) | Key gates |
|------|---------|-----------------|-----------|
| Free (Registered) | $0 | $0 | Delayed data, 50 alerts, 50 portfolios, basic screener, ads |
| Elite | $39.50 | $24.96 | Real-time data, advanced screener filters, 200 alerts, 100 portfolios, backtesting, no ads, auto pattern recognition |

**Metered elements:** Alert and portfolio counts are hard caps. Single paid tier keeps it simple.

---

## 5. QuantConnect (quantconnect.com/pricing) -- PARTIAL VERIFY

**What they offer:** Algorithmic trading platform -- backtesting, live trading, research notebooks, data library, compute nodes.

| Tier | Per user/mo | Key gates |
|------|-------------|-----------|
| Free | $0 | Basic data (equity, FX, crypto), unlimited backtesting, 1 live algo |
| Quant Researcher | ~$8-20 | Expanded dataset access, up to 2 compute nodes |
| Team | ~$20-40/user | Team collaboration, shared projects, up to 10 compute nodes |
| Trading Firm | ~$40-80/user | Team project ownership, FIX/professional brokerages, unlimited nodes |
| Institution | Custom | Dedicated infrastructure, custom onboarding |

**Metered elements:** Heavy usage-based model. Compute nodes purchased separately. AI tokens (Ask Mia) sold in packs: 1000 tokens ($10), 3000 ($30), 7500 ($75). QCC credit packs ($20-$250+) for compute and data. 10% bonus on $100+ purchases.

---

## 6. Stock Analysis (stockanalysis.com/pro) (~)

**What they offer:** Stock/ETF research -- financials, estimates, screener, holdings data, IPO calendar, insider trading.

| Tier | Monthly | Annual (per mo) | Key gates |
|------|---------|-----------------|-----------|
| Free | $0 | $0 | Basic financials, limited history, ads |
| Pro | ~$10 | ~$7 | Full financial history (30Y), full ETF holdings, advanced screener, no ads, export data |

**Metered elements:** None; single flat upgrade.

---

## 7. Morningstar Investor (investor.morningstar.com) (~)

**What they offer:** Fund/stock ratings (star system), analyst reports, portfolio X-ray, fair value estimates, economic moat ratings.

| Tier | Monthly | Annual (per mo) | Key gates |
|------|---------|-----------------|-----------|
| Free | $0 | $0 | Basic quotes, limited articles |
| Investor | ~$35 | ~$24 | Full analyst reports, fair value estimates, portfolio X-ray, fund screener, stock/fund star ratings |

**Metered elements:** None reported; single paid tier.

---

## 8. Simply Wall St (simplywall.st/pricing) (~)

**What they offer:** Visual stock analysis (snowflake diagrams), portfolio tracking, valuation models (DCF), dividend analysis, ownership breakdowns.

| Tier | Monthly | Annual (per mo) | Key gates |
|------|---------|-----------------|-----------|
| Free (Wall St) | $0 | $0 | 5 company reports/mo, 1 portfolio |
| Starter | ~$10 | ~$7 | 30 company reports/mo, 3 portfolios, watchlist alerts |
| Investor | ~$20 | ~$14 | Unlimited reports, 10 portfolios, full valuation data, dividend data |
| Unlimited | ~$30 | ~$20 | Everything unlimited, priority support, global market coverage |

**Metered elements:** Company report views are hard-gated on lower tiers -- core monetization lever.

---

## Key Patterns Across Competitors

1. **Free tier exists everywhere** -- the funnel entry point is non-negotiable.
2. **Sweet spot for retail paid tier is $20-40/mo** (annual) or $30-50/mo (monthly). Below $20 is "nice to have"; above $50 is "professional tool."
3. **Single-feature upsells work.** Seeking Alpha charges separately for Alpha Picks (curated recommendations). Finviz has one paid tier. Stock Analysis has one.
4. **Usage/metered pricing is rare in retail** but common in infra-heavy products (QuantConnect compute nodes, AI tokens).
5. **Advisor/professional tiers create 5-10x revenue uplift** (Koyfin jumps from $79 to $299).
6. **Report/view gating is effective** for visual analysis tools (Simply Wall St, Finviz alerts).
7. **Annual discounts of 20-40%** are standard and drive LTV.

---

## Proposed s-tool.io Tiering Models

### Model A: "Clean Three-Tier" (Recommended for MVP)

| Tier | Price | What they get |
|------|-------|---------------|
| **Free** | $0 | 3 projections/day, basic MC bands, single ticker view |
| **Pro** | $8/mo | 10 projections/day, save projections, projection history, email alerts when price crosses band |
| **Strategist** | $29/mo | Unlimited projections, portfolio risk dashboard (conservative/moderate/aggressive picks), weekly top-10 picks list, multi-ticker comparison view |

*Why $29:* It sits in the "serious retail" zone without reaching "professional" pricing. The jump from $8 to $29 is gated by the portfolio picks -- the thing they cannot get anywhere else cheaply. The weekly picks list creates a habit loop that reduces churn.

### Model B: "Picks as Add-On" (Seeking Alpha model)

| Tier | Price | What they get |
|------|-------|---------------|
| **Free** | $0 | 3 projections/day |
| **Pro** | $8/mo | 10/day + history + alerts |
| **Pro + Picks** | $8 + $19/mo add-on ($27 total) | Everything in Pro + risk-bucketed weekly picks, portfolio recommendations |

*Why:* Decouples the projection tool from the intelligence product. Users who want a simple projection tool stay at $8. Users who want picks pay the premium. Clean upgrade path to charge more for picks later as signal quality improves.

### Model C: "Metered Intelligence" (QuantConnect-inspired)

| Tier | Price | What they get |
|------|-------|---------------|
| **Free** | $0 | 3 projections/day |
| **Pro** | $8/mo | 25 projections/day, basic portfolio view |
| **Pro+** | $8/mo + credit packs | Credits buy: deep-dive reports ($1/report), backtest runs ($2/run), custom signal scans ($5/scan) |

*Why:* Lets power users spend more without forcing casual users into expensive tiers. Revenue scales with engagement. Risk: adds billing complexity at MVP stage.

### Model D: "Aspiration Ladder" (Long-term vision, phased rollout)

| Tier | Price | Gates | Ship when |
|------|-------|-------|-----------|
| **Free** | $0 | 3 projections/day | Now |
| **Pro** | $8/mo | 10/day, alerts, history | Now |
| **Strategist** | $29/mo | Portfolio picks, risk buckets, weekly report | Q3 2026 |
| **Quant** | $49/mo | Custom signal builder, backtesting, personalized portfolio | Q1 2027 |
| **Advisor API** | $199/mo | API access, white-label projections, bulk endpoints | Q2 2027 |

*Why:* Maps directly to the product roadmap. Each tier launches only when the feature is ready. The Advisor/API tier captures the 5-10x professional uplift seen at Koyfin.

---

## Final Recommendation

**Ship Model A now. Migrate to Model D over 12 months.**

Rationale:
- The $8 Pro is committed and correctly priced for a projection tool. Do not change it.
- Add a **$29/mo Strategist tier** as the first premium upsell. Gate it on portfolio picks and the risk-bucketed recommendations. This is the feature with the widest moat -- no competitor offers MC + mean-reversion projections combined with risk-tiered picks at this price.
- The $29 price point is below the psychological "do I really need this" threshold ($50+) while being high enough to qualify the user as engaged.
- Do NOT add metered pricing at MVP. It adds friction, billing complexity, and support burden. Revisit credits/tokens only when you ship backtesting or custom signals.
- Plan for $49 Quant tier and $199 Advisor/API tier on the roadmap, but do not pre-announce them. Ship when features are ready.
- Offer a **20% annual discount** from day one ($8/mo becomes $77/yr; $29/mo becomes $278/yr). This is table stakes.
- The single most important thing that makes a retail trader pay $29-50/mo is **curated, actionable picks they can act on immediately**. Projections are a feature; picks are a product. The Strategist tier should feel like "someone did the homework for me."
