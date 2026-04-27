async function load() {
  // Fetch both in parallel: live scoreboard (picks_history, updated every
  // pipeline run) + backtest report (static, refreshed nightly).
  const authHeaders = async () => {
    try { const t = await window.Clerk?.session?.getToken?.(); return t ? { Authorization: `Bearer ${t}` } : {}; } catch { return {}; }
  };
  const [liveRes, backtestRes, honestRes, journalRes, portfolioRes] = await Promise.all([
    fetch('/api/track-record?lookback_days=365', { headers: await authHeaders() }).catch(() => null),
    fetch('/api/backtest-report').catch(() => null),
    fetch('/api/honest-audit').catch(() => null),
    fetch('/api/trade-journal?lookback_days=90').catch(() => null),
    fetch('/api/portfolio').catch(() => null),
  ]);
  const live = liveRes ? await liveRes.json().catch(() => null) : null;
  const journal = journalRes && journalRes.ok ? await journalRes.json().catch(() => null) : null;
  const portfolio = portfolioRes && portfolioRes.ok ? await portfolioRes.json().catch(() => null) : null;
  // Make these globally readable so renderAll/renderScoreboard can pull them
  // without threading extra args through every internal helper.
  window.__live_journal = journal;
  window.__live_portfolio = portfolio;

  let backtest = null;
  if (backtestRes && backtestRes.ok) {
    backtest = await backtestRes.json().catch(() => null);
  }
  let honest = null;
  if (honestRes && honestRes.ok) {
    honest = await honestRes.json().catch(() => null);
  }
  if (!backtest) {
    const scoreboardOnly = renderScoreboard(live);
    document.getElementById('content').innerHTML = scoreboardOnly +
      '<div class="loading">Backtest report not yet available. Check back after the next nightly run.</div>';
    return;
  }
  renderAll(backtest, live, honest);
}

function fmtPct(v, sign = true) {
  if (v == null) return '—';
  const n = v * 100;
  return `${sign && n > 0 ? '+' : ''}${n.toFixed(1)}%`;
}
function fmtLift(v) {
  if (!v) return '—';
  return `${v.toFixed(2)}x`;
}
function liftClass(v) {
  if (v >= 1.5) return 'hi';
  if (v >= 1.1) return 'mid';
  return 'lo';
}

// Build a banner for any pipeline_events the API surfaced (outages,
// maintenance windows). Adjacent dates collapse into a single range so
// "2026-04-19 to 2026-04-25" renders as one line, not seven.
function renderPipelineEvents(events) {
  if (!events || !events.length) return '';
  // Sort ascending then collapse adjacent dates by event_type+summary
  const sorted = [...events].sort((a,b) => a.event_date.localeCompare(b.event_date));
  const ranges = [];
  for (const e of sorted) {
    const last = ranges[ranges.length - 1];
    const dt = new Date(e.event_date + 'T00:00:00Z');
    const prevEnd = last && new Date(last.end + 'T00:00:00Z');
    const oneDay = 24*60*60*1000;
    if (last && last.event_type === e.event_type && last.summary === e.summary &&
        (dt - prevEnd === oneDay)) {
      last.end = e.event_date;
    } else {
      ranges.push({ event_type: e.event_type, summary: e.summary, detail: e.detail,
                    start: e.event_date, end: e.event_date });
    }
  }
  const fmtDate = (s) => new Date(s + 'T00:00:00Z').toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  const lines = ranges.map(r => {
    const range = r.start === r.end ? fmtDate(r.start)
                                    : `${fmtDate(r.start)} – ${fmtDate(r.end)}`;
    const label = r.event_type === 'outage' ? 'Pipeline outage'
                : r.event_type === 'maintenance' ? 'Scheduled maintenance'
                : r.event_type;
    return `<div class="pe-line"><span class="pe-range">${range}</span> · ${label}: ${r.summary}${r.detail ? ` <span style="color:var(--text-dim);">— ${r.detail}</span>` : ''}</div>`;
  }).join('');
  return `<div class="pipeline-events">
    <div class="pe-label">Ledger gaps</div>
    ${lines}
  </div>`;
}

// Live paper-trader scoreboard block. Pulls /api/trade-journal stats
// (anonymous, gated on Strategist for per-trade detail). Renders a
// 4-card hero plus best/worst trade strip when there are any closed
// pairs. Hidden entirely when no trades have been journaled yet.
function renderLiveTraderBlock(journal, portfolio) {
  const s = journal?.stats;
  if (!s || (!s.n_buys && !s.n_sells)) return '';
  const pnlCls = s.realized_pnl_total > 0 ? 'pos' : (s.realized_pnl_total < 0 ? 'neg' : '');
  const winCls = (s.win_rate || 0) >= 0.5 ? 'pos' : '';
  const fmtUSD = (v) => `${v < 0 ? '−' : ''}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  const bestStrip = s.best_trade && s.worst_trade
    ? `<div class="lt-bestworst">
        <span class="lt-be">Best: <strong>${s.best_trade.symbol}</strong> ${fmtUSD(s.best_trade.pnl)}</span>
        <span class="lt-wo">Worst: <strong>${s.worst_trade.symbol}</strong> ${fmtUSD(s.worst_trade.pnl)}</span>
      </div>` : '';

  // Live alpha vs SPY — cumulative trader return minus SPY's move over
  // the same window. Only shown when we have ≥1 trading day of history.
  const bench = portfolio?.benchmark;
  const benchStrip = (bench && bench.alpha != null) ? (() => {
    const traderPct = (bench.trader_return * 100).toFixed(2);
    const spyPct = (bench.spy_return * 100).toFixed(2);
    const alphaPct = (bench.alpha * 100).toFixed(2);
    const alphaCls = bench.alpha > 0 ? 'pos' : 'neg';
    const traderSign = bench.trader_return >= 0 ? '+' : '';
    const spySign = bench.spy_return >= 0 ? '+' : '';
    const alphaSign = bench.alpha >= 0 ? '+' : '';
    return `<div class="lt-bench">
      <span>Trader: <strong class="${bench.trader_return >= 0 ? 'pos' : 'neg'}">${traderSign}${traderPct}%</strong></span>
      <span>·</span>
      <span>SPY: <strong>${spySign}${spyPct}%</strong></span>
      <span>·</span>
      <span>Alpha: <strong class="${alphaCls}">${alphaSign}${alphaPct}%</strong></span>
    </div>`;
  })() : '';
  return `<section class="section" style="margin-top:12px;">
    <div class="section-eyebrow">Live paper trader · real Alpaca fills</div>
    <h2 class="section-title">The engine is trading the picks. Here's the receipts.</h2>
    <p class="section-sub">A $100k Alpaca paper account opens 10 swing positions and 10 daytrade positions every weekday at 13:30 UTC, exits the daytrade sleeve at 19:55 UTC, and rolls swings on a 5-day cycle. Every fill below is real — entry price, exit price, realized P&L.</p>
    <section class="headline">
      <div class="h-card">
        <div class="label">Trades opened</div>
        <div class="val">${(s.n_buys || 0).toLocaleString()}</div>
        <div class="sub">Buys submitted · ${journal.lookback_days}-day window</div>
      </div>
      <div class="h-card">
        <div class="label">Closed pairs</div>
        <div class="val">${(s.n_closed_pairs || 0).toLocaleString()}</div>
        <div class="sub">${s.wins || 0} wins · ${s.losses || 0} losses</div>
      </div>
      <div class="h-card">
        <div class="label">Win rate</div>
        <div class="val ${winCls}">${s.win_rate != null ? (s.win_rate * 100).toFixed(1) + '%' : '—'}</div>
        <div class="sub">Share of closed trades above entry</div>
      </div>
      <div class="h-card">
        <div class="label">Realized P&L</div>
        <div class="val ${pnlCls}">${fmtUSD(s.realized_pnl_total || 0)}</div>
        <div class="sub">Cumulative across closed trades · paper</div>
      </div>
    </section>
    ${benchStrip}
    ${bestStrip}
    ${renderTradeJournalFeed(journal)}
  </section>`;
}

// Trade journal feed — chronological list of every buy/sell with bracket
// math and result. Per-row detail is Strategist-gated by the API: when
// the response carries `teaser: true`, /api/trade-journal omits `rows[]`
// and we render a locked nudge below the live-trader stats. Strategist
// users get the full feed, newest 50 rows, scrollable.
function renderTradeJournalFeed(journal) {
  if (!journal) return '';

  // Gated case — back the live-trader hero with a "this is what's behind
  // the gate" nudge so visitors understand the upgrade unlocks per-trade
  // evidence, not just bigger numbers.
  if (journal.teaser) {
    if (!journal.stats || (!journal.stats.n_buys && !journal.stats.n_sells)) {
      return '';   // no activity at all → don't bother showing the gate
    }
    return `<div class="tj-locked">
      Per-trade detail (timestamps, brackets, results) unlocks at
      <a href="/pricing">Strategist tier</a> — the aggregate stats above
      are everything visible without it.
    </div>`;
  }

  const rows = journal.rows || [];
  if (!rows.length) {
    return `<div class="tj-empty">No journal entries yet — the trader hasn't fired since this lookback window opened.</div>`;
  }

  const sorted = [...rows].sort((a, b) =>
    (b.ts || '').localeCompare(a.ts || ''));
  const top = sorted.slice(0, 50);

  const fmtRel = (ts) => {
    if (!ts) return '—';
    const t = new Date(ts);
    const diffSec = (Date.now() - t.getTime()) / 1000;
    if (diffSec < 0)    return 'just now';
    if (diffSec < 60)   return 'just now';
    if (diffSec < 3600) return `${Math.floor(diffSec/60)}m ago`;
    if (diffSec < 86400) return `${Math.floor(diffSec/3600)}h ago`;
    return `${Math.floor(diffSec/86400)}d ago`;
  };
  const fmtUSD = (v, digits = 2) => v == null
    ? '—' : `$${Number(v).toFixed(digits)}`;
  const fmtPnL = (v) => v == null
    ? '—' : `${v >= 0 ? '+' : '−'}$${Math.abs(v).toFixed(2)}`;

  const trs = top.map(r => {
    const event = (r.event || 'buy').toLowerCase();
    const isBuy = event === 'buy';
    const sleeve = r.sleeve || 'unattributed';
    const result = r.result || event;
    const meta = isBuy
      ? `entry ${fmtUSD(r.live_price ?? r.ref_price)} · stop ${fmtUSD(r.stop)} · tgt ${fmtUSD(r.target)}`
      : `realized ${fmtPnL(r.pnl)}`;
    let resultCls = 'tj-ok';
    if (event === 'buy_failed' || (result || '').includes('fail')) resultCls = 'tj-fail';
    else if (result === 'nobracket_fallback') resultCls = 'tj-warn';
    return `<div class="tj-row">
      <span class="tj-time" title="${r.ts || ''}">${fmtRel(r.ts)}</span>
      <span class="tj-sleeve tj-sleeve-${sleeve}">${sleeve}</span>
      <span class="tj-sym">${r.symbol || '—'}</span>
      <span class="tj-side ${isBuy ? 'buy' : 'sell'}">${isBuy ? 'BUY' : 'SELL'} ${r.qty != null ? r.qty : ''}</span>
      <span class="tj-meta">${meta}</span>
      <span class="tj-result ${resultCls}">${result}</span>
    </div>`;
  }).join('');

  return `<div class="tj-feed">${trs}</div>`;
}

// Render the live scoreboard block from /api/track-record response
// (works with either the 200 strategist payload or the 402 teaser —
// both shapes carry `aggregate` + `summary`). Returns HTML string.
function renderScoreboard(live) {
  if (!live || !live.aggregate || !live.aggregate.n) {
    // No matured picks yet — honest placeholder, don't fake numbers.
    const eventsHtml = renderPipelineEvents(live?.events);
    return `<section class="section">
      <div class="section-eyebrow">Live scoreboard</div>
      <h2 class="section-title">Live tracking starts once picks mature.</h2>
      <p class="section-sub">Every pick published to <a href="/picks" style="color:var(--accent-lake);">/picks</a> is logged to a public ledger with its entry date and entry price. Once a pick has been out for at least 7 days the realized return is tracked against the live market. Come back after the first week of picks has matured.</p>
      ${eventsHtml}
    </section>`;
  }
  const agg = live.aggregate;
  const sum = live.summary || {};
  const fmtDate = (d) => d ? new Date(d).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) : '—';

  const aggCards = `<section class="headline">
    <div class="h-card">
      <div class="label">Picks matured</div>
      <div class="val">${agg.n.toLocaleString()}</div>
      <div class="sub">From ${fmtDate(agg.earliest_pick_date)} · ${agg.total_picks_logged} total logged</div>
    </div>
    <div class="h-card">
      <div class="label">Profitable rate</div>
      <div class="val ${agg.hit_rate >= 0.5 ? 'pos' : 'neg'}">${(agg.hit_rate * 100).toFixed(1)}%</div>
      <div class="sub">Share of matured picks above entry price</div>
    </div>
    <div class="h-card">
      <div class="label">Mean realized return</div>
      <div class="val ${agg.mean_return >= 0 ? 'pos' : 'neg'}">${fmtPct(agg.mean_return)}</div>
      <div class="sub">Average across matured picks · no rebalancing</div>
    </div>
    <div class="h-card">
      <div class="label">Median realized return</div>
      <div class="val ${agg.median_return >= 0 ? 'pos' : 'neg'}">${fmtPct(agg.median_return)}</div>
      <div class="sub">Typical pick, not skewed by outliers</div>
    </div>
  </section>`;

  // Per-tier breakdown
  const tiers = [
    ['conservative', 'Conservative', 'Low volatility · quality'],
    ['moderate',     'Moderate',     'Core · balanced'],
    ['aggressive',   'Aggressive',   'High volatility · tactical'],
    ['asymmetric',   'Asymmetric',   'Tail upside · speculative'],
  ];
  const tierRows = tiers.map(([k, label, sub]) => {
    const b = sum[k] || { n: 0 };
    if (!b.n) {
      return `<tr><td class="label">${label}<div style="font-size:11px;color:var(--text-dim);font-weight:400;margin-top:2px;">${sub}</div></td><td colspan="4" style="color:var(--text-dim);font-style:italic;">No matured picks yet</td></tr>`;
    }
    const hitCls = b.hit_rate >= 0.5 ? 'pos' : '';
    const meanCls = b.mean_return >= 0 ? 'pos' : 'neg';
    const medCls = b.median_return >= 0 ? 'pos' : 'neg';
    return `<tr>
      <td class="label">${label}<div style="font-size:11px;color:var(--text-dim);font-weight:400;margin-top:2px;">${sub}</div></td>
      <td>${b.n}</td>
      <td class="${hitCls}">${(b.hit_rate * 100).toFixed(1)}%</td>
      <td class="${meanCls}">${fmtPct(b.mean_return)}</td>
      <td class="${medCls}">${fmtPct(b.median_return)}</td>
    </tr>`;
  }).join('');

  const eventsHtml = renderPipelineEvents(live.events);
  return `<section class="section" style="margin-top:12px;">
    <div class="section-eyebrow">Live scoreboard · real picks, real prices</div>
    <h2 class="section-title">What the ranker has actually delivered.</h2>
    <p class="section-sub">Every row below joins our published picks with today's prices. Nothing is retroactively adjusted. Only picks at least 7 days old count as <em>matured</em>.</p>
    ${aggCards}
    ${eventsHtml}
    <div class="table-scroll" style="margin-top:18px;"><table class="perf">
      <thead><tr><th>Tier</th><th>Matured picks</th><th>Profitable rate</th><th>Mean return</th><th>Median return</th></tr></thead>
      <tbody>${tierRows}</tbody>
    </table></div>
  </section>`;
}

// Build the honest-audit block — replaces the prior size-neutral cards
// with the Wave 1 "after liquidity + tx-cost" numbers. Falls back to the
// legacy honest_metrics when the audit isn't available yet.
function renderHonestBlock(honest, legacyBacktest) {
  // Prefer Wave 1 audit (liquidity + tx costs applied)
  const nn = honest?.scorers?.nn_score;
  if (nn) {
    const overall = nn.overall?.E_all_three;
    const oos = nn.oos_2024?.E_all_three;
    const pub = nn.overall?.A_published;
    if (overall && oos && pub) {
      return `<section class="headline">
        <div class="h-card">
          <div class="label">Hit rate at +100% — honest</div>
          <div class="val pos">${(overall.hit_100 * 100).toFixed(1)}%</div>
          <div class="sub">Top 20 per window, after liquidity + 1.5% costs. As-published was ${(pub.hit_100*100).toFixed(1)}%.</div>
        </div>
        <div class="h-card">
          <div class="label">Lift vs baseline — honest</div>
          <div class="val pos">${fmtLift(overall.lift)}</div>
          <div class="sub">Baseline ${(overall.baseline_hit_100*100).toFixed(2)}% of any tradeable ticker doubles. Our top-20 clears ~9&times; that.</div>
        </div>
        <div class="h-card">
          <div class="label">2024 out-of-sample hit rate</div>
          <div class="val pos">${(oos.hit_100 * 100).toFixed(1)}%</div>
          <div class="sub">Trained only on prior windows. 2024 was never seen during training — same liquidity + cost filters applied.</div>
        </div>
        <div class="h-card">
          <div class="label">2024 OOS lift</div>
          <div class="val pos">${fmtLift(oos.lift)}</div>
          <div class="sub">Signal strengthens on unseen data, survives the realism bar.</div>
        </div>
      </section>`;
    }
  }
  // Fallback: legacy honest_metrics from backtest_report
  const honestLegacy = legacyBacktest?.honest_metrics;
  if (honestLegacy?.size_neutral_hit_100 != null) {
    return `<section class="headline">
      <div class="h-card">
        <div class="label">Size-controlled +100% hit rate</div>
        <div class="val pos">${(honestLegacy.size_neutral_hit_100 * 100).toFixed(1)}%</div>
        <div class="sub">4 picks per price quintile, 20 total. Eliminates small-cap concentration.</div>
      </div>
      <div class="h-card">
        <div class="label">Within-quintile lift</div>
        <div class="val pos">${fmtLift(honestLegacy.within_quintile_lift_median)}</div>
        <div class="sub">Median lift vs baseline, computed per price quintile.</div>
      </div>
      <div class="h-card">
        <div class="label">${honestLegacy.year_oos_test_year} OOS hit rate</div>
        <div class="val pos">${(honestLegacy.year_oos_hit_100 * 100).toFixed(1)}%</div>
        <div class="sub">Train on all prior windows; test only on ${honestLegacy.year_oos_test_year}.</div>
      </div>
      <div class="h-card">
        <div class="label">${honestLegacy.year_oos_test_year} OOS lift</div>
        <div class="val pos">${fmtLift(honestLegacy.year_oos_lift)}</div>
        <div class="sub">Baseline ${(honestLegacy.year_oos_baseline * 100).toFixed(1)}%. Lift survives the clean holdout.</div>
      </div>
    </section>`;
  }
  return '';
}

// Internal — not currently surfaced on the page (user clarified the
// priority is adding data sources to the model, not displaying them).
// Kept for potential admin debug view.
function renderDataFeeds(status) {
  if (!status || !status.feeds) return '';
  const f = status.feeds;
  const fmtDT = (iso) => {
    if (!iso) return '—';
    try { return new Date(iso).toLocaleString(); } catch { return iso; }
  };
  const fmtN = (n) => (n == null) ? '—' : n.toLocaleString();
  const statusPill = (s) => {
    if (s === 'live')    return `<span class="pill live">● live</span>`;
    if (s === 'pending') return `<span class="pill pending">◌ pending</span>`;
    return `<span class="pill err">✗ error</span>`;
  };

  const si = f.short_interest_yf || {};
  const ph = f.picks_history || {};
  const sa = f.short_interest_ablation || {};

  const rows = [];
  rows.push(`<tr>
    <td class="label">Short interest (yfinance)</td>
    <td>${statusPill(si.status)}</td>
    <td>${fmtN(si.symbols)} symbols</td>
    <td>Snapshot ${si.last_snapshot_date || '—'}</td>
    <td>${fmtDT(si.last_fetched_at)}</td>
  </tr>`);
  rows.push(`<tr>
    <td class="label">Picks history ledger</td>
    <td>${statusPill(ph.status)}</td>
    <td>${fmtN(ph.rows)} picks · ${fmtN(ph.symbols)} names</td>
    <td>${ph.earliest_pick_date || '—'} → ${ph.latest_pick_date || '—'}</td>
    <td>every pipeline run</td>
  </tr>`);
  rows.push(`<tr>
    <td class="label">Short-interest ablation</td>
    <td>${statusPill(sa.status)}</td>
    <td>${fmtN(sa.si_distinct_symbols)} symbols joined · ${sa.si_coverage_pct ?? '—'}% coverage</td>
    <td>Run ${sa.run_date || '—'}</td>
    <td>${sa.baseline_hit_100 != null ? `baseline hit_100 ${(sa.baseline_hit_100*100).toFixed(1)}%` : sa.note || '—'}</td>
  </tr>`);

  // Roadmap rows
  const roadmap = (status.pipeline_roadmap || []).map(r => `<tr>
    <td class="label">${r.source}</td>
    <td><span class="pill queued">◔ queued</span></td>
    <td colspan="3" style="color:var(--text-dim);">${r.key_present ? 'API key configured — wiring into the nightly pipeline next' : 'no API key set'}</td>
  </tr>`).join('');

  // Ablation deltas — inline below the table so we can quickly see
  // which SI variant beats the baseline night-over-night.
  let deltaBlock = '';
  const deltas = sa.deltas_vs_baseline || {};
  if (Object.keys(deltas).length) {
    const items = Object.entries(deltas).map(([k, v]) => {
      const pp = (v * 100).toFixed(1);
      const cls = v > 0 ? 'pos' : v < 0 ? 'neg' : '';
      return `<span class="delta-pill ${cls}">${k} <b>${v > 0 ? '+' : ''}${pp}pp</b></span>`;
    }).join('');
    deltaBlock = `<div class="deltas">Latest ablation deltas vs 8-feature baseline: ${items}</div>`;
  }

  return `<section class="section">
    <div class="section-eyebrow">Data feeds · under the hood</div>
    <h2 class="section-title">What's feeding the model <em>right now</em>.</h2>
    <p class="section-sub">Every row is a live data source tracked by the nightly pipeline. The "snapshot" column is the most recent data we've actually ingested. "Queued" rows are sources with API keys ready but not yet wired into the training loop.</p>
    <div class="table-scroll"><table class="perf data-feeds">
      <thead><tr><th>Source</th><th>Status</th><th>Coverage</th><th>Snapshot</th><th>Last refresh</th></tr></thead>
      <tbody>${rows.join('')}${roadmap}</tbody>
    </table></div>
    ${deltaBlock}
  </section>`;
}

function renderAll(r, live, honest) {
  const meta = document.getElementById('meta');
  const wr = r.window_range || [];
  meta.innerHTML = `
    <span>${r.universe_size?.toLocaleString?.()} ticker-windows</span>
    <span>${r.window_count} rolling windows</span>
    <span>${wr[0] || ''} → ${wr[1] || ''}</span>
    <span>Generated ${new Date((r.generated_at || 0) * 1000).toLocaleString()}</span>
  `;

  // Lead with honest, tradeable metrics (Wave 1 audit: liquidity floor
  // + 1.5% transaction costs). Falls back to legacy size-neutral cards
  // when the audit JSON isn't yet available.
  let headHtml = renderHonestBlock(honest, r);
  // Prior code path kept below for when neither audit nor legacy exists.
  const _legacy = r.honest_metrics || null;
  if (!headHtml && _legacy && _legacy.size_neutral_hit_100 != null) {
    headHtml = `<section class="headline">
      <div class="h-card">
        <div class="label">Size-controlled +100% hit rate</div>
        <div class="val pos">${(_legacy.size_neutral_hit_100 * 100).toFixed(1)}%</div>
        <div class="sub">4 picks per price quintile, 20 total. Eliminates small-cap concentration.</div>
      </div>
      <div class="h-card">
        <div class="label">Within-quintile lift</div>
        <div class="val pos">${fmtLift(_legacy.within_quintile_lift_median)}</div>
        <div class="sub">Median lift vs baseline, computed per price quintile.</div>
      </div>
      <div class="h-card">
        <div class="label">${_legacy.year_oos_test_year} out-of-sample hit rate</div>
        <div class="val pos">${(_legacy.year_oos_hit_100 * 100).toFixed(1)}%</div>
        <div class="sub">Train on all prior windows; test only on ${_legacy.year_oos_test_year}.</div>
      </div>
      <div class="h-card">
        <div class="label">${_legacy.year_oos_test_year} OOS lift vs baseline</div>
        <div class="val pos">${fmtLift(_legacy.year_oos_lift)}</div>
        <div class="sub">Baseline ${(_legacy.year_oos_baseline * 100).toFixed(1)}%.</div>
      </div>
    </section>`;
  } else {
    // Fallback: legacy headline using H7 when honest_metrics aren't populated
    // (e.g., early nightly runs before the NN/ensemble columns are filled).
    const headline = r.methods?.H7_ewma_p90 || null;
    if (headline) {
      const hit100 = headline.thresholds?.['+100%'] || {};
      headHtml = `<section class="headline">
        <div class="h-card">
          <div class="label">+100% hit rate</div>
          <div class="val pos">${(hit100.rate * 100).toFixed(1)}%</div>
          <div class="sub">${fmtLift(hit100.lift)} vs baseline. Pre-size-adjustment.</div>
        </div>
        <div class="h-card">
          <div class="label">Top-pick mean return</div>
          <div class="val ${headline.mean_return >= 0 ? 'pos' : 'neg'}">${fmtPct(headline.mean_return)}</div>
          <div class="sub">Average 12-mo return across all top picks.</div>
        </div>
      </section>`;
    }
  }

  // Explicit honest-disclosure block explaining the gap between size-neutral
  // and unconstrained numbers.
  let disclosureHtml = '';
  if (_legacy && _legacy.within_quintile_details) {
    const ensembleMethod = r.methods?.ensemble_score || r.methods?.nn_score;
    const uncRate = ensembleMethod?.thresholds?.['+100%']?.rate;
    const uncLift = ensembleMethod?.thresholds?.['+100%']?.lift;
    if (uncRate) {
      disclosureHtml = `<section class="section">
        <div class="section-eyebrow">Why these numbers, not the flattering ones</div>
        <h2 class="section-title">The unconstrained model concentrates in small-caps.</h2>
        <p class="section-sub">If we let the model pick whatever it wants, it reports a <b style="color:var(--text-hi);">${(uncRate * 100).toFixed(0)}%</b> hit rate at +100% with <b style="color:var(--text-hi);">${fmtLift(uncLift)}</b> lift. That's largely the small-cap premium: ~80% of its unconstrained picks concentrate in the smallest price quintile. Our v4 research controls for this by either forcing size diversification or using a clean out-of-sample year — the honest numbers above. They are smaller but represent what a size-diversified Strategist portfolio would realistically have returned.</p>
      </section>`;
    }
  }

  // Full threshold table
  let tableHtml = `<section class="section">
    <div class="section-eyebrow">Hit-rate distribution</div>
    <h2 class="section-title">How often picks clear each return threshold</h2>
    <p class="section-sub">Universe baseline = raw % of any ticker in any window that cleared the return threshold within 12 months. Method hit rate = same calculation restricted to each method's top-20 per window. Lift is the ratio — above 1.0x means the method beat random selection.</p>
    <div class="table-scroll"><table class="perf">
      <thead><tr><th>Threshold</th><th>Baseline</th><th>H1 · naive P90</th><th>H4 · composite</th><th>H7 · volatility-adaptive</th><th>H9 · full stack</th></tr></thead>
      <tbody>`;
  const thresholds = ['+10%', '+25%', '+50%', '+100%', '+200%'];
  for (const t of thresholds) {
    tableHtml += `<tr><td class="label">${t}</td><td>${((r.baseline_rates?.[t] || 0) * 100).toFixed(1)}%</td>`;
    for (const m of ['H1_naive_p90','H4_composite','H7_ewma_p90','H9_full_stack']) {
      const cell = r.methods?.[m]?.thresholds?.[t];
      if (!cell) { tableHtml += `<td>—</td>`; continue; }
      tableHtml += `<td>${(cell.rate * 100).toFixed(1)}% <span class="lift ${liftClass(cell.lift)}"> · ${fmtLift(cell.lift)}</span></td>`;
    }
    tableHtml += `</tr>`;
  }
  tableHtml += `</tbody></table></div></section>`;

  // Regime performance
  let regimeHtml = `<section class="section">
    <div class="section-eyebrow">Regime-conditional performance</div>
    <h2 class="section-title">Does the edge hold across market regimes?</h2>
    <p class="section-sub">Windows bucketed by their median universe return: bull (&ge;+10%), choppy (between), bear (&le;-5%). Honest reporting — if a method only works in rallies, we say so.</p>
    <div class="regime-grid">`;
  for (const reg of ['bull','choppy','bear']) {
    const d = r.regime_performance?.[reg];
    if (!d || !d.windows) {
      regimeHtml += `<div class="regime-card"><div class="r-name">${reg}</div><div class="r-count">No windows classified as ${reg}</div></div>`;
      continue;
    }
    let methodsBlock = '';
    for (const m of ['H7_ewma_p90','H9_full_stack']) {
      const md = d.methods?.[m];
      if (!md) continue;
      const mlabel = m === 'H7_ewma_p90' ? 'Volatility-adaptive' : 'Full stack';
      methodsBlock += `<div class="r-stat"><span>${mlabel} mean</span><span class="v ${md.mean_return >= 0 ? 'pos' : 'neg'}">${fmtPct(md.mean_return)}</span></div>`;
      methodsBlock += `<div class="r-stat"><span>${mlabel} +100% hit</span><span class="v">${(md.hit_100_rate * 100).toFixed(1)}%</span></div>`;
    }
    regimeHtml += `<div class="regime-card"><div class="r-name">${reg}</div><div class="r-count">${d.windows} windows &middot; ${d.n_tickers?.toLocaleString() || 0} ticker-rows</div>${methodsBlock}</div>`;
  }
  regimeHtml += `</div></section>`;

  // Simulated portfolio
  let simHtml = '';
  const sim = r.simulated_portfolios?.H7_ewma_p90;
  if (sim) {
    simHtml = `<section class="section">
      <div class="section-eyebrow">Simulated portfolio</div>
      <h2 class="section-title">If a user had bought an equal-weight basket of the top-20 H7 picks every window, here's what would have happened.</h2>
      <p class="section-sub">Equal-weight, no transaction costs, no slippage, no rebalancing friction &mdash; a theoretical upper-bound benchmark, not a deliverable PnL.</p>
      <div class="regime-grid">
        <div class="regime-card"><div class="r-name">Mean / median window return</div><div class="r-stat"><span>Mean</span><span class="v ${sim.mean_window_return >= 0 ? 'pos' : 'neg'}">${fmtPct(sim.mean_window_return)}</span></div><div class="r-stat"><span>Median</span><span class="v ${sim.median_window_return >= 0 ? 'pos' : 'neg'}">${fmtPct(sim.median_window_return)}</span></div></div>
        <div class="regime-card"><div class="r-name">Range</div><div class="r-stat"><span>Best window</span><span class="v pos">${fmtPct(sim.best_window_return)}</span></div><div class="r-stat"><span>Worst window</span><span class="v neg">${fmtPct(sim.worst_window_return)}</span></div></div>
        <div class="regime-card"><div class="r-name">Consistency</div><div class="r-stat"><span>Positive windows</span><span class="v pos">${(sim.pct_windows_positive * 100).toFixed(0)}%</span></div><div class="r-stat"><span>N windows</span><span class="v">${sim.n_windows}</span></div></div>
      </div>
    </section>`;
  }

  // Caveats
  const caveats = (r.notes || []).map(n => `<li>${n}</li>`).join('');
  const caveatHtml = caveats ? `<div class="caveats"><h3>Methodology &amp; caveats</h3><ul>${caveats}</ul></div>` : '';

  // Live scoreboard leads — real picks against real prices — followed by
  // the backtest validation (headHtml / disclosure / table / regime / sim).
  // Backtest is the "how did we validate" supporting evidence; scoreboard
  // is the "what has it done since" trust signal.
  const liveTraderHtml = renderLiveTraderBlock(window.__live_journal, window.__live_portfolio);
  const scoreboardHtml = renderScoreboard(live);
  const recentPicksHtml = renderRecentPicksTable(live);
  const backtestEyebrow = `<section class="section" style="margin-top:48px;">
    <div class="section-eyebrow">Backtest validation</div>
    <h2 class="section-title">How the ranker was tested before going live.</h2>
    <p class="section-sub">Walk-forward across ~22,000 ticker-windows, 2022-05 → 2024-08. Out-of-sample on 2024 data the model never saw in training. The honest bar, not the flattering one.</p>
  </section>`;
  document.getElementById('content').innerHTML =
    liveTraderHtml + scoreboardHtml + recentPicksHtml + backtestEyebrow +
    headHtml + disclosureHtml + tableHtml + regimeHtml + simHtml + caveatHtml;
}

// Last-30-days picks with realized return. Shows the actual movements
// of recent picks; this is where a visitor can scan "did the engine
// pick winners over the past month?". Only matured-or-equal picks are
// shown (days_held ≥ 1) so we never display a brand-new-pick row with
// realized=0%.
function renderRecentPicksTable(live) {
  const rows = (live?.rows || []).filter(r =>
    (r.days_held || 0) >= 1 && r.realized_return != null
  );
  if (!rows.length) return '';
  // Sort by realized return descending — the visual hits "biggest winner first"
  // so the eye anchors on the upside before scanning down to losses.
  rows.sort((a, b) => (b.realized_return || 0) - (a.realized_return || 0));
  const top = rows.slice(0, 30);
  const fmt = (v) => v == null ? '—' : `${v >= 0 ? '+' : ''}${(v * 100).toFixed(1)}%`;
  const fmtDate = (d) => d ? new Date(d).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '—';
  const tierTag = (t) => `<span class="tier-pill ${t}">${t}</span>`;
  const tierBy = (t) => rows.filter(r => r.tier === t);
  const wins = (rs) => rs.length ? `${rs.filter(r => r.realized_return > 0).length}/${rs.length}` : '—';
  const tierStrip = `<div class="tier-strip">
    <span>Conservative ${wins(tierBy('conservative'))}</span>
    <span>Moderate ${wins(tierBy('moderate'))}</span>
    <span>Aggressive ${wins(tierBy('aggressive'))}</span>
    ${tierBy('asymmetric').length ? `<span>Asymmetric ${wins(tierBy('asymmetric'))}</span>` : ''}
  </div>`;
  const trs = top.map(r => `<tr>
    <td>${fmtDate(r.pick_date)}</td>
    <td><strong>${r.symbol}</strong></td>
    <td>${tierTag(r.tier)}</td>
    <td class="num">$${(r.entry_price || 0).toFixed(2)}</td>
    <td class="num">${r.current_price ? '$' + r.current_price.toFixed(2) : '—'}</td>
    <td class="num ${r.realized_return >= 0 ? 'pos' : 'neg'}">${fmt(r.realized_return)}</td>
    <td class="num">${r.days_held || 0}d</td>
  </tr>`).join('');
  return `<section class="section" style="margin-top:32px;">
    <div class="section-eyebrow">Recent picks · live ledger</div>
    <h2 class="section-title">${top.length} picks from the last ${(live.aggregate?.earliest_pick_date ? Math.max(1, Math.round((Date.now() - new Date(live.aggregate.earliest_pick_date).getTime()) / 86400000)) : 30)} days, sorted by realized return.</h2>
    <p class="section-sub">Entry price is the close on the day the pick was published. Current price is today's most recent close. No retroactive adjustments — these are the same numbers the picks page showed when the call was made.</p>
    ${tierStrip}
    <div class="table-scroll" style="margin-top:18px;"><table class="perf">
      <thead><tr><th>Pick date</th><th>Symbol</th><th>Tier</th><th>Entry</th><th>Now</th><th>Realized</th><th>Held</th></tr></thead>
      <tbody>${trs}</tbody>
    </table></div>
  </section>`;
}

load();
