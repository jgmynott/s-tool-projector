const TIER_COPY = {
  conservative: { label: 'Conservative', blurb: 'Low-volatility names with positive expected return and quality fundamentals. Built for capital preservation.' },
  moderate:     { label: 'Moderate',     blurb: 'Mid-volatility tercile. Balanced risk with positive expected return. Core allocation.' },
  aggressive:   { label: 'Aggressive',   blurb: 'Highest projected volatility with the largest expected moves either direction. Tactical set — size small per name.' },
};

const SECTOR_HINT = {
  PLTR: 'Software · Analytics', COIN: 'Financials · Crypto', SOFI: 'Financials · Fintech',
  NFLX: 'Media · Streaming', AVGO: 'Semis · Infrastructure', MU: 'Semis · Memory',
  AMD: 'Semis · Compute', INTC: 'Semis · Compute', NVDA: 'Semis · AI',
  GS: 'Financials · Banking', JPM: 'Financials · Banking', BAC: 'Financials · Banking',
  CVX: 'Energy · Integrated', XOM: 'Energy · Integrated',
  AAPL: 'Tech · Consumer', MSFT: 'Tech · Enterprise', GOOGL: 'Tech · Platform',
  META: 'Tech · Platform', AMZN: 'Tech · Platform',
  TSLA: 'Auto · EV', AMC: 'Consumer · Entertainment', GME: 'Consumer · Retail',
  DIS: 'Media · Entertainment', NKE: 'Consumer · Apparel',
};

const fmtPct = (v, sign = true) => v == null ? '—' : `${sign && v >= 0 ? '+' : ''}${(v*100).toFixed(1)}%`;
const fmtMoney = (v) => v == null ? '—' : `$${Number(v).toFixed(2)}`;
const fmtSharpe = (v) => v == null ? '—' : Number(v).toFixed(2);

// ── Tab navigation helpers ──
// `switchTab(key)` swaps the active pill + the visible panel. Active tab
// persists to localStorage so a return visit opens where you left off.
window.switchTab = function(key) {
  document.querySelectorAll('[data-tab-key]').forEach(btn => {
    btn.classList.toggle('active', btn.getAttribute('data-tab-key') === key);
  });
  document.querySelectorAll('[data-panel-key]').forEach(panel => {
    panel.classList.toggle('active', panel.getAttribute('data-panel-key') === key);
  });
  localStorage.setItem('stool_picks_tab', key);
  // Scroll the active tab panel into view — user explicitly asked
  // for a jump to the picks for that tier so they don't have to hunt.
  // Deferred one frame so the active-class toggle has already laid out.
  requestAnimationFrame(() => {
    const panel = document.querySelector(`[data-panel-key="${key}"]`);
    if (panel) {
      panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  });
};

// "Show remaining N picks" toggle — keeps each tab short by default, lets
// the user reveal the long tail inline without leaving the page.
window.toggleShowAll = function(tier) {
  const panel = document.querySelector(`[data-panel-key="${tier}"]`);
  if (!panel) return;
  panel.classList.toggle('show-all');
  const btn = panel.querySelector('.show-all-btn');
  if (btn) {
    const hidden = panel.querySelectorAll('[data-overflow]').length;
    btn.textContent = panel.classList.contains('show-all')
      ? 'Show top 5 only'
      : `Show remaining ${hidden} picks →`;
  }
};

async function authHeaders() {
  try { const t = await window.Clerk?.session?.getToken?.(); return t ? { Authorization: `Bearer ${t}` } : {}; } catch { return {}; }
}

async function startCheckout(tier) {
  tier = tier || 'strategist';
  if (!window.Clerk?.session) return window.Clerk?.openSignIn({ afterSignInUrl: '/picks' });
  try {
    const res = await fetch(`/api/billing/checkout?tier=${encodeURIComponent(tier)}`, { method: 'POST', headers: await authHeaders() });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
    const { checkout_url } = await res.json();
    location.href = checkout_url;
  } catch (e) { alert(`Checkout failed: ${e.message}`); }
}

// Nav pill + dropdown + dark-themed Clerk modal are handled by /shared/nav.js.
// We still need a local refreshNavPill() reference because the load() loop
// below calls it whenever picks data refreshes. Delegate to the shared impl.
async function refreshNavPill() { return window.STNav?.refreshPill?.(); }

function renderTrackRecord(summary) {
  const cards = ['conservative','moderate','aggressive'].map(t => {
    const s = (summary && summary[t]) || { n: 0 };
    if (!s.n) {
      return `<div class="tr-card">
          <div class="tr-tier">${TIER_COPY[t].label}</div>
          <div class="tr-waiting">Track record starts once picks mature (7d+). Cards refresh as they land.</div>
      </div>`;
    }
    const cls = s.median_return >= 0 ? 'pos' : 'neg';
    const hit = `${(s.hit_rate*100).toFixed(0)}%`;
    return `<div class="tr-card">
        <div class="tr-tier">${TIER_COPY[t].label}</div>
        <div class="tr-big ${cls}">${fmtPct(s.median_return)}</div>
        <div class="tr-bigsub">median realized &middot; <span class="hit-pill">${hit} hit</span> &middot; n=${s.n}</div>
    </div>`;
  }).join('');
  return `<section class="trackrecord">
      <div class="section-eyebrow">Live track record</div>
      <h2 class="section-title">How the picks have actually done. <span class="muted">Realized returns on matured names.</span></h2>
      <div class="tr-grid">${cards}</div>
  </section>`;
}

// ── Live paper-portfolio panel ───────────────────────────────
// Renders the Robinhood-style equity hero, sleeve summary, and per-position
// cards for the actively-managed Alpaca paper account. Defensive against
// every degraded state: no creds (503), upstream error (502), no positions
// yet (empty hint), teaser mode (free user), zero equity_history points.
function fmtUSD(v, opts) {
  const o = opts || {};
  const sign = (o.signed && v > 0) ? '+' : '';
  const n = Math.abs(v);
  const digits = (o.digits != null) ? o.digits : (n >= 1000 ? 0 : 2);
  return `${v < 0 ? '-' : sign}$${n.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}
function pfSparkline(eq) {
  if (!eq || eq.length < 2) return '';
  const w = 240, h = 64, pad = 4;
  const xs = eq.length;
  const min = Math.min(...eq), max = Math.max(...eq);
  const range = (max - min) || 1;
  const x = (i) => pad + (i / (xs - 1)) * (w - 2 * pad);
  const y = (v) => pad + (1 - (v - min) / range) * (h - 2 * pad);
  const pts = eq.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`);
  const line = `M ${pts.join(' L ')}`;
  const area = `${line} L ${x(xs - 1).toFixed(1)},${(h - pad).toFixed(1)} L ${x(0).toFixed(1)},${(h - pad).toFixed(1)} Z`;
  const last = eq[eq.length - 1], first = eq[0];
  const stroke = last >= first ? 'var(--pos)' : 'var(--neg)';
  return `<svg class="pf-curve" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <path class="area" d="${area}" fill="${stroke}"/>
    <path class="line" d="${line}" stroke="${stroke}"/>
  </svg>`;
}

// ── Interactive equity chart (D / W / M views) ──
// User selection persists across 30s auto-refreshes via window.__pfChartWindow.
// Default to 1M for the broadest signal on a 30-day daily history.
function pfEquityChart(data) {
  const win = window.__pfChartWindow || '1M';
  let pts, ts, isIntraday = false, spyPts = null;
  // Legend baselines per window. On 1D, both series are measured vs
  // YESTERDAY'S CLOSE so the legend pct matches the headline pill — without
  // this the legend silently excludes the overnight gap and disagrees with
  // every other dollar number on the page. On 1W/1M, baseline is the first
  // visible point (the natural "since X ago" comparison).
  let traderBaseline = null;  // dollars; pfChartSvg uses if set, else falls back to first pt
  let spyBaselineRaw = null;  // raw SPY close that pairs with traderBaseline
  let spyLastRaw = null;      // most recent raw SPY close in the window
  if (win === '1D') {
    // Filter intraday to regular trading hours (13:30-20:00 UTC = 9:30-16:00 ET)
    // — Alpaca's portfolio history with extended_hours=true returns pre-market
    // and after-hours bars that mostly add flat noise to the chart and dwarf
    // the actual trading window visually.
    const rawTs  = data.equity_intraday?.timestamps || [];
    const rawEq  = data.equity_intraday?.equity || [];
    const rawSpy = data.equity_intraday?.spy_close || [];
    let keepIdx = pfFilterRTH(rawTs);
    // Pre-open fallback: if RTH filter strips everything (we're checking
    // before 9:30 ET), use the unfiltered intraday so the chart shows
    // pre-market drift instead of an empty box. The chart automatically
    // re-renders post-open as the RTH filter starts matching.
    if (keepIdx.length < 2 && rawTs.length >= 2) {
      keepIdx = rawTs.map((_, i) => i);
    }
    pts = keepIdx.map(i => rawEq[i]);
    ts  = keepIdx.map(i => rawTs[i]);
    isIntraday = true;
    // Trader baseline = yesterday's close, sourced from acct.last_equity
    // (Alpaca's authoritative prior-day close). This is what the headline
    // pill compares against, so the chart legend now lines up.
    const acctLastEq = data.account?.last_equity;
    if (acctLastEq && acctLastEq > 0) traderBaseline = acctLastEq;
    if (rawSpy.length) {
      const spySlice = keepIdx.map(i => rawSpy[i] ?? null);
      // Normalize SPY to trader's first equity value so both lines start
      // from the same y-coord and the alpha gap reads naturally.
      const traderStart = pts.find(v => v != null);
      const spyAnchor = spySlice.find(v => v != null);
      if (traderStart && spyAnchor) {
        spyPts = spySlice.map(c => c == null ? null : (c / spyAnchor) * traderStart);
      }
      // SPY baseline for legend = yesterday's SPY close. The daily history
      // ends "today" (we re-stamp the last bar to now), so [-2] is the
      // most recent prior-day close — the SPY equivalent of last_equity.
      const histSpy = data.equity_history?.spy_close || [];
      if (histSpy.length >= 2) {
        for (let i = histSpy.length - 2; i >= 0; i--) {
          if (histSpy[i] != null) { spyBaselineRaw = histSpy[i]; break; }
        }
      }
      // Latest raw SPY close from today's intraday series.
      for (let i = rawSpy.length - 1; i >= 0; i--) {
        if (rawSpy[i] != null) { spyLastRaw = rawSpy[i]; break; }
      }
    }
  } else {
    const allPts = data.equity_history?.equity || [];
    const allTs  = data.equity_history?.timestamps || [];
    const allSpy = data.equity_history?.spy_close || [];
    const n = win === '1W' ? 7 : 30;
    pts = allPts.slice(-n);
    ts  = allTs.slice(-n);
    if (allSpy.length) {
      const spySlice = allSpy.slice(-n);
      // Normalize SPY to start at the same equity baseline as the trader
      // so the alpha gap reads naturally on a single $ axis. Anchor on
      // the first non-null SPY close paired with the first pts value.
      const traderStart = pts[0];
      let spyAnchor = null;
      for (let i = 0; i < spySlice.length; i++) {
        if (spySlice[i] != null) { spyAnchor = spySlice[i]; break; }
      }
      if (traderStart && spyAnchor) {
        spyPts = spySlice.map(c => c == null ? null : (c / spyAnchor) * traderStart);
      }
    }
  }
  const tabs = pfChartTabs(win);
  if (!pts || pts.length < 2) {
    // More specific empty-state copy. 1D before market-open is the most
    // common case — say so plainly so the user doesn't think the chart
    // is broken. Other windows (1W/1M) only fall here on day-1 of a
    // brand-new account.
    const msg = (win === '1D')
      ? "Market opens at 9:30 ET — chart populates from there."
      : "Not enough history yet — chart fills in as the trader runs.";
    return `<div class="pf-chart-row">${tabs}
      <div class="pf-chart-empty">${msg}</div>
    </div>`;
  }
  return `<div class="pf-chart-row">${tabs}${pfChartSvg(pts, ts, isIntraday, spyPts, { traderBaseline, spyBaselineRaw, spyLastRaw, win })}</div>`;
}

// Returns the indices of `timestamps` (unix seconds) whose UTC time falls
// inside 13:30-20:00 UTC (= 9:30-16:00 ET, regular cash session).
function pfFilterRTH(timestamps) {
  const out = [];
  const RTH_START = 13 * 60 + 30;   // 13:30 UTC
  const RTH_END   = 20 * 60;        // 20:00 UTC
  for (let i = 0; i < timestamps.length; i++) {
    const t = timestamps[i];
    if (t == null) continue;
    const d = (typeof t === 'number') ? new Date(t * 1000) : new Date(t);
    const min = d.getUTCHours() * 60 + d.getUTCMinutes();
    if (min >= RTH_START && min <= RTH_END) out.push(i);
  }
  return out;
}

function pfChartTabs(active) {
  const tabs = [['1D', '1D'], ['1W', '1W'], ['1M', '1M']];
  return `<div class="pf-chart-tabs">
    ${tabs.map(([v, label]) =>
      `<button class="pf-chart-tab${v === active ? ' active' : ''}" data-pfwin="${v}">${label}</button>`
    ).join('')}
  </div>`;
}

function pfChartSvg(pts, ts, isIntraday, spyPts, opts = {}) {
  // viewBox at 1200×400 (3:1) so the data ratio matches typical desktop
  // container shape and stretching is minimal. preserveAspectRatio="none"
  // still does the final fit; non-scaling-stroke on the line keeps stroke
  // weight crisp regardless of stretch. Generous padding on the y-axis
  // for label legibility at 4K.
  const w = 1200, h = 400, padL = 72, padR = 24, padT = 22, padB = 44;
  const innerW = w - padL - padR;
  const innerH = h - padT - padB;

  // Y-range covers BOTH series so SPY's curve doesn't clip out of frame
  // when the trader has dramatically out- or under-performed.
  const allVals = pts.slice();
  if (spyPts && spyPts.length) {
    for (const v of spyPts) if (v != null) allVals.push(v);
  }
  const min = Math.min(...allVals), max = Math.max(...allVals);
  const span = (max - min) || 1;
  const yPad = span * 0.06;
  const yMin = min - yPad, yMax = max + yPad;
  const yRange = yMax - yMin;

  const x = (i) => padL + (i / (pts.length - 1)) * innerW;
  const y = (v) => padT + (1 - (v - yMin) / yRange) * innerH;

  const last = pts[pts.length - 1], first = pts[0];
  const stroke = last >= first ? 'var(--pos)' : 'var(--neg)';
  const strokeRaw = last >= first ? '#6EE7B7' : '#FCA5A5';   // for fill (CSS var won't resolve in fill attr)

  const linePts = pts.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`);
  const linePath = `M ${linePts.join(' L ')}`;
  const areaPath = `${linePath} L ${x(pts.length - 1).toFixed(1)},${(padT + innerH).toFixed(1)} L ${padL.toFixed(1)},${(padT + innerH).toFixed(1)} Z`;

  // SPY comparison line — neutral gray, dashed, drawn UNDER the trader
  // line so the trader curve always reads as the headline. Skips null
  // entries (forward-fill gaps from weekends/holidays).
  let spyPath = '';
  let spyLast = null;
  if (spyPts && spyPts.length) {
    const segs = [];
    let cur = '';
    for (let i = 0; i < spyPts.length; i++) {
      const v = spyPts[i];
      if (v == null) {
        if (cur) { segs.push(cur); cur = ''; }
        continue;
      }
      const xs = x(i).toFixed(1), ys = y(v).toFixed(1);
      cur += cur ? ` L ${xs},${ys}` : `M ${xs},${ys}`;
      spyLast = v;
    }
    if (cur) segs.push(cur);
    spyPath = segs.join(' ');
  }

  // Y-axis: 4 evenly-spaced ticks
  const yTickN = 4;
  let yLabels = '';
  for (let i = 0; i <= yTickN; i++) {
    const v = yMin + (yRange * i / yTickN);
    const yPos = y(v);
    yLabels += `<line x1="${padL}" y1="${yPos.toFixed(1)}" x2="${(w - padR).toFixed(1)}" y2="${yPos.toFixed(1)}" class="pf-chart-grid"/>
      <text x="${(padL - 8).toFixed(1)}" y="${(yPos + 3).toFixed(1)}" class="pf-chart-y-label">$${Math.round(v).toLocaleString('en-US')}</text>`;
  }

  // X-axis ticks. For 1W, label every trading day (skip weekends — they
  // appear as flat segments because Alpaca forward-fills closed-market
  // bars). For 1M, evenly space ~6 ticks. For 1D, evenly space ~6 ticks.
  // The previous "evenly space 6 ticks across N points" rule on 1W silently
  // skipped Tuesday: 7 points × 6 ticks → indices [0,1,2,3,4,6].
  const xTickIndices = [];
  if (opts.win === '1W') {
    for (let i = 0; i < ts.length; i++) {
      const t = ts[i];
      if (t == null) continue;
      const d = (typeof t === 'number') ? new Date(t * 1000) : new Date(t);
      const dow = d.getUTCDay();   // 0 = Sun, 6 = Sat
      if (dow >= 1 && dow <= 5) xTickIndices.push(i);
    }
    // Always include the last point even if it falls on a weekend (rare,
    // would only happen if Alpaca returns a Sat/Sun timestamp for today).
    if (xTickIndices.length && xTickIndices[xTickIndices.length - 1] !== pts.length - 1) {
      xTickIndices.push(pts.length - 1);
    }
  } else {
    const tickN = Math.min(6, pts.length);
    for (let i = 0; i < tickN; i++) {
      xTickIndices.push(Math.floor((pts.length - 1) * i / (tickN - 1)));
    }
  }
  let xLabels = '';
  for (const idx of xTickIndices) {
    const xPos = x(idx);
    xLabels += `<text x="${xPos.toFixed(1)}" y="${(h - 10).toFixed(1)}" class="pf-chart-x-label">${pfFmtTickLabel(ts[idx], isIntraday)}</text>`;
  }

  // Hover hit-rects — invisible, one per data point, capture mouseenter/leave.
  // Encode SPY % return at this point (vs chart-window start) so the tooltip
  // can render SPY as a benchmark percentage rather than a meaningless
  // normalized dollar amount. SPY is rescaled to start at the trader baseline
  // so its raw $ would be misleading as a level — its % return is the right
  // primitive. Also encode the trader's own % so the tooltip can show both
  // sides as apples-to-apples.
  const traderBase = pts[0] || 1;
  const halfStep = pts.length > 1 ? innerW / (pts.length - 1) / 2 : innerW / 2;
  let hitRects = '';
  for (let i = 0; i < pts.length; i++) {
    const cx = x(i);
    const cy = y(pts[i]);
    const traderPct = ((pts[i] / traderBase) - 1) * 100;
    const spyPct = (spyPts && spyPts[i] != null)
      ? ((spyPts[i] / traderBase) - 1) * 100
      : null;
    hitRects += `<rect x="${(cx - halfStep).toFixed(1)}" y="${padT}" width="${(halfStep * 2).toFixed(1)}" height="${innerH}" fill="transparent" class="pf-chart-hit" data-cx="${cx.toFixed(1)}" data-cy="${cy.toFixed(1)}" data-val="${pts[i].toFixed(2)}" data-trader-pct="${traderPct.toFixed(2)}" data-spy-pct="${spyPct == null ? '' : spyPct.toFixed(2)}" data-date="${pfFmtTooltipDate(ts[i], isIntraday)}"/>`;
  }

  // Legend — only when SPY is on. Positioned in the chart row (below)
  // so it doesn't crowd the SVG canvas itself.
  let legend = '';
  if (spyPath) {
    // Compute pct deltas in raw units (not normalized chart units) so
    // the legend matches the dollar headline. On 1D both series compare
    // vs PRIOR DAY CLOSE — including the overnight gap, which is what
    // every other dollar number on the page reflects. On 1W/1M, baseline
    // is the chart's first point.
    const traderRef = (opts.traderBaseline && opts.traderBaseline > 0) ? opts.traderBaseline : first;
    const traderDelta = ((last - traderRef) / traderRef) * 100;
    let spyDelta = null;
    if (opts.spyBaselineRaw && opts.spyLastRaw && opts.spyBaselineRaw > 0) {
      spyDelta = ((opts.spyLastRaw - opts.spyBaselineRaw) / opts.spyBaselineRaw) * 100;
    } else {
      const spyFirst = spyPts.find(v => v != null);
      spyDelta = spyLast != null && spyFirst ? ((spyLast - spyFirst) / spyFirst) * 100 : null;
    }
    const traderCls = traderDelta >= 0 ? 'pos' : 'neg';
    const spyCls = (spyDelta != null && spyDelta >= 0) ? 'pos' : 'neg';
    const winLabel = opts.win === '1D' ? 'today'
                    : opts.win === '1W' ? '1 week'
                    : opts.win === '1M' ? '1 month' : '';
    legend = `<div class="pf-chart-legend">
      <span class="pf-chart-legend-item">
        <span class="pf-chart-legend-swatch trader" style="background:${strokeRaw};"></span>
        <span class="pf-chart-legend-name"><strong>S-Tool</strong></span>
        <span class="${traderCls}">${traderDelta >= 0 ? '+' : ''}${traderDelta.toFixed(2)}%</span>
      </span>
      <span class="pf-chart-legend-item">
        <span class="pf-chart-legend-swatch spy"></span>
        <span class="pf-chart-legend-name"><strong>S&amp;P 500</strong></span>
        ${spyDelta != null ? `<span class="${spyCls}">${spyDelta >= 0 ? '+' : ''}${spyDelta.toFixed(2)}%</span>` : ''}
      </span>
      ${winLabel ? `<span class="pf-chart-legend-window">vs ${winLabel === 'today' ? 'prior close' : `${winLabel} ago`}</span>` : ''}
    </div>`;
  }

  return `<div class="pf-chart" id="pfChart">
    <svg class="pf-chart-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      ${yLabels}
      ${xLabels}
      <path class="pf-chart-area" d="${areaPath}" fill="${strokeRaw}"/>
      ${spyPath ? `<path class="pf-chart-spy-line" d="${spyPath}"/>` : ''}
      <path class="pf-chart-line" d="${linePath}" stroke="${stroke}"/>
      ${hitRects}
      <line class="pf-chart-cursor" x1="0" y1="${padT}" x2="0" y2="${(padT + innerH).toFixed(1)}" style="display:none;"/>
      <circle class="pf-chart-dot" cx="0" cy="0" r="4" fill="${strokeRaw}" stroke="var(--bg-surface)" stroke-width="2" style="display:none;"/>
    </svg>
    ${legend}
    <div class="pf-chart-tooltip" style="display:none;">
      <span class="pf-chart-tt-val"></span>
      <span class="pf-chart-tt-spy" style="display:none;"></span>
      <span class="pf-chart-tt-date"></span>
    </div>
  </div>`;
}

function pfFmtTickLabel(t, isIntraday) {
  if (t == null) return '';
  const d = (typeof t === 'number') ? new Date(t * 1000) : new Date(t);
  if (isIntraday) {
    // Minutes-precision so two ticks inside the same hour (e.g., 9:45
    // and 10:15) don't both render as "10 AM" and look duplicated.
    return d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit', hour12: true });
  }
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function pfFmtTooltipDate(t, isIntraday) {
  if (t == null) return '';
  const d = (typeof t === 'number') ? new Date(t * 1000) : new Date(t);
  if (isIntraday) {
    return d.toLocaleString(undefined, { hour: 'numeric', minute: '2-digit', hour12: true, month: 'short', day: 'numeric' });
  }
  return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
}

// Wire chart-tab clicks (delegated, page-wide). Stashes window selection,
// then re-renders ONLY the chart container so the rest of the panel stays
// stable. Also wire hover on hit rects to position cursor + tooltip.
document.addEventListener('click', (e) => {
  const btn = e.target.closest('.pf-chart-tab');
  if (!btn) return;
  const win = btn.dataset.pfwin;
  if (!win || !window.__pfData) return;
  window.__pfChartWindow = win;
  const row = document.querySelector('.pf-chart-row');
  if (!row) return;
  // Replace just the chart row in place
  const wrapper = document.createElement('div');
  wrapper.innerHTML = pfEquityChart(window.__pfData);
  row.replaceWith(wrapper.firstElementChild);
});

// Hover on hit rects — show cursor line, dot at data point, and floating
// tooltip with date + value. mouseleave on the SVG hides everything.
document.addEventListener('mouseover', (e) => {
  const rect = e.target.closest('.pf-chart-hit');
  if (!rect) return;
  const chart = rect.closest('.pf-chart');
  if (!chart) return;
  const cx = parseFloat(rect.dataset.cx);
  const cy = parseFloat(rect.dataset.cy);
  const cursor = chart.querySelector('.pf-chart-cursor');
  const dot    = chart.querySelector('.pf-chart-dot');
  const tip    = chart.querySelector('.pf-chart-tooltip');
  if (cursor) {
    cursor.setAttribute('x1', cx); cursor.setAttribute('x2', cx);
    cursor.style.display = '';
  }
  if (dot) {
    dot.setAttribute('cx', cx); dot.setAttribute('cy', cy);
    dot.style.display = '';
  }
  if (tip) {
    // Position tooltip in client coords. Read viewBox so the math stays
    // correct if we ever resize the SVG canvas — hardcoding 600/200 once
    // bit us when the viewBox grew to 1200/400.
    const svg = chart.querySelector('.pf-chart-svg');
    const bbox = svg.getBoundingClientRect();
    const chartBox = chart.getBoundingClientRect();
    const vb = svg.viewBox?.baseVal;
    const vbW = vb?.width || 1200, vbH = vb?.height || 400;
    const px = (cx / vbW) * bbox.width + (bbox.left - chartBox.left);
    const py = (cy / vbH) * bbox.height + (bbox.top - chartBox.top);
    // S-Tool: dollar amount (the real account equity) with its own %
    // alongside, so the comparison to SPY's % is apples-to-apples.
    // SPY: % return only — the line is rescaled to the trader baseline,
    // so its level is meaningless; only the slope matters.
    const fmtSignedPct = (v) => (v >= 0 ? '+' : '−') + Math.abs(v).toFixed(2) + '%';
    const traderUsd = parseFloat(rect.dataset.val).toLocaleString(undefined, { maximumFractionDigits: 2 });
    const traderPct = parseFloat(rect.dataset.traderPct);
    const traderPctStr = isFinite(traderPct) ? ` <span class="pf-chart-tt-pct ${traderPct >= 0 ? 'pos' : 'neg'}">${fmtSignedPct(traderPct)}</span>` : '';
    tip.querySelector('.pf-chart-tt-val').innerHTML = `<span class="pf-chart-tt-name">S-Tool</span> $${traderUsd}${traderPctStr}`;
    const spyEl = tip.querySelector('.pf-chart-tt-spy');
    if (spyEl && rect.dataset.spyPct) {
      const spyPct = parseFloat(rect.dataset.spyPct);
      spyEl.innerHTML = `<span class="pf-chart-tt-name">SPY</span> <span class="pf-chart-tt-pct ${spyPct >= 0 ? 'pos' : 'neg'}">${fmtSignedPct(spyPct)}</span>`;
      spyEl.style.display = '';
    } else if (spyEl) {
      spyEl.style.display = 'none';
    }
    tip.querySelector('.pf-chart-tt-date').textContent = rect.dataset.date;
    tip.style.left = `${px}px`;
    tip.style.top  = `${py}px`;
    tip.style.display = '';
  }
});
document.addEventListener('mouseout', (e) => {
  const chart = e.target.closest('.pf-chart');
  if (!chart) return;
  // Only hide if we're actually leaving the chart area
  const related = e.relatedTarget;
  if (related && chart.contains(related)) return;
  ['.pf-chart-cursor', '.pf-chart-dot', '.pf-chart-tooltip'].forEach(sel => {
    const el = chart.querySelector(sel);
    if (el) el.style.display = 'none';
  });
});
function pfSleeveCard(name, summary, label) {
  const upnl = summary?.upnl || 0;
  const realized = summary?.realized_today || 0;
  const upnlCls = upnl > 0 ? 'pos' : (upnl < 0 ? 'neg' : '');
  const n = summary?.n || 0;
  // Surface realized P&L when the sleeve has had any closes today —
  // otherwise it'd just clutter the card with "+$0". Always shown for
  // daytrade after 19:55 UTC since EVERY daytrade closes by EOD.
  const realizedRow = Math.abs(realized) > 0.5
    ? `<div class="pf-sleeve-realized ${realized > 0 ? 'pos' : 'neg'}">+ ${fmtUSD(realized, { signed: true, digits: 0 })} realized today</div>`
    : '';
  // Rotation cycle indicator — only on the daytrade card. The rotator
  // fires every 30 min during 14:00-19:30 UTC weekdays; outside that
  // window we either say "complete for today" or "weekday only".
  let rotationRow = '';
  if (name === 'daytrade') {
    const tick = pfNextRotationTick();
    if (tick && tick.mins != null) {
      const ago = tick.mins === 0 ? 'now' : `~${tick.mins}m`;
      rotationRow = `<div class="pf-sleeve-rotation"><span class="live">●</span>next rotate ${ago}</div>`;
    } else if (tick && tick.note) {
      rotationRow = `<div class="pf-sleeve-rotation">${tick.note}</div>`;
    }
  }
  return `<div class="pf-sleeve">
    <div class="pf-sleeve-name ${name}">${label}</div>
    <div class="pf-sleeve-mv">${fmtUSD(summary?.mv || 0, { digits: 0 })}</div>
    <div class="pf-sleeve-meta ${upnlCls}">${n} pos · ${fmtUSD(upnl, { signed: true, digits: 0 })}</div>
    ${realizedRow}
    ${rotationRow}
  </div>`;
}

// Pending-order strip — renders open_orders[] from /api/portfolio as a
// thin "in-flight" banner above positions. Filters to side=buy entries
// whose client_order_id matches the sleeve naming convention; that
// excludes auto-generated stop/target child legs which would otherwise
// double-count the same position.
function pfPendingOrdersStrip(orders) {
  if (!orders || !orders.length) return '';
  const SLEEVE_PREFIXES = ['momentum-', 'swing-', 'daytrade-'];
  const pending = orders.filter(o => {
    if ((o.side || '').toLowerCase() !== 'buy') return false;
    const cid = o.client_order_id || '';
    return SLEEVE_PREFIXES.some(p => cid.startsWith(p));
  });
  if (!pending.length) return '';
  const items = pending.slice(0, 6).map(o => {
    const sleeve = (o.client_order_id || '').split('-')[0] || '';
    return `<span class="pf-pending-item">
      <span class="pf-pending-sym">${o.symbol}</span>
      <span class="pf-pending-meta">${o.qty} qty · ${sleeve}</span>
    </span>`;
  }).join('');
  const more = pending.length > 6
    ? `<span class="pf-pending-more">+ ${pending.length - 6} more</span>` : '';
  const noun = pending.length === 1 ? 'entry' : 'entries';
  return `<div class="pf-pending">
    <span class="pf-pending-icon">↻</span>
    <span class="pf-pending-label">${pending.length} ${noun} pending fill</span>
    ${items}${more}
  </div>`;
}

// =============================================================
// Live activity strip — "something to watch" panel.
// =============================================================
//
// Renders above the donut. Two pieces stacked:
//   1. Status row — pulsing dot, last action summary, countdown to the
//      next scheduled bot fire, pending-fill count.
//   2. Vertical ticker — last 15 events (buys + sells), newest at top.
//      New events (vs last render) animate in via CSS keyframe.
//
// Polls happen via the existing 30s refreshPortfolioOnly(). For tighter
// refresh, drop the interval in startPortfolioRefresh(). Data sources:
//   buys  ← positions[] where days_held===0 (opened_at gives ts)
//   sells ← closed_today[] (closed_at gives ts)
//   pending ← open_orders[] count
//
// "Next cron" is computed from the CF Worker schedule in worker.js, kept
// in sync manually here. If you change cron expressions there, update
// PF_CRON_FIRES below.
const PF_CRON_FIRES = [
  // [hourUTC, minuteUTC, label] — only DST-window fires; we'll detect
  // ST and shift +60min if needed via the comment below. For now the
  // user is in DST through early November.
  [13, 25, 'pre-open sweep'],
  [13, 30, 'open print'],
  [14, 0, 'rotate'], [14, 30, 'rotate'],
  [15, 0, 'rotate'], [15, 30, 'rotate'],
  [16, 0, 'rotate'], [16, 30, 'rotate'],
  [17, 0, 'rotate'], [17, 30, 'rotate'],
  [18, 0, 'rotate'], [18, 30, 'rotate'],
  [19, 0, 'rotate'], [19, 30, 'rotate'],
  [19, 55, 'close'],
  [20, 30, 'EOD report'],
];
function pfNextCronFire() {
  const now = new Date();
  const hUTC = now.getUTCHours();
  const mUTC = now.getUTCMinutes();
  const sUTC = now.getUTCSeconds();
  // Scalper fires every 5 min during 14:00–19:55 UTC. Compute the next
  // 5-min boundary if we're in that window.
  let nextScalper = null;
  if (hUTC >= 14 && hUTC < 20) {
    const nextM = Math.ceil((mUTC + (sUTC > 0 ? 0.0001 : 0)) / 5) * 5;
    if (nextM < 60) nextScalper = [hUTC, nextM, 'scalper scan'];
    else if (hUTC < 19) nextScalper = [hUTC + 1, 0, 'scalper scan'];
  }
  // Find the next scheduled non-scalper fire today.
  const nowMin = hUTC * 60 + mUTC;
  let nextFire = null;
  for (const [h, m, label] of PF_CRON_FIRES) {
    if (h * 60 + m > nowMin || (h * 60 + m === nowMin && sUTC === 0)) {
      nextFire = [h, m, label];
      break;
    }
  }
  // Pick whichever fires sooner.
  let pick = null;
  for (const cand of [nextScalper, nextFire].filter(Boolean)) {
    if (!pick || (cand[0] * 60 + cand[1]) < (pick[0] * 60 + pick[1])) pick = cand;
  }
  if (!pick) return { label: 'after hours', countdown: '—' };
  // Compute countdown to pick time today (UTC).
  const target = new Date(now);
  target.setUTCHours(pick[0], pick[1], 0, 0);
  const ms = target.getTime() - now.getTime();
  if (ms <= 0) return { label: pick[2], countdown: 'now' };
  const totalSec = Math.floor(ms / 1000);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  const countdown = m >= 60
    ? `${Math.floor(m / 60)}h ${m % 60}m`
    : m >= 1
      ? `${m}m ${String(s).padStart(2, '0')}s`
      : `${s}s`;
  return { label: pick[2], countdown };
}

function pfLiveActivityStrip(data) {
  const closed = data.closed_today || [];
  const positions = data.positions || [];
  const openOrders = data.open_orders || [];

  // Build combined event feed (buys + sells, newest first).
  const events = [];
  for (const c of closed) {
    if (!c.symbol) continue;
    const qty = parseFloat(c.qty) || 0;
    const buyPx = parseFloat(c.buy_price) || 0;
    const sellPx = parseFloat(c.sell_price) || 0;
    const pnl = parseFloat(c.pnl) || 0;
    // pct return = pnl / cost basis
    const cost = qty * buyPx;
    const pct = cost > 0 ? (pnl / cost) * 100 : 0;
    events.push({
      kind: 'sell',
      symbol: c.symbol,
      sleeve: c.sleeve || 'unattributed',
      time: c.closed_at,
      pnl,
      pct,
      qty,
      entryPx: buyPx,
      exitPx: sellPx,
      id: `sell-${c.symbol}-${c.closed_at || ''}`,
    });
  }
  for (const p of positions) {
    if (typeof p.days_held !== 'number' || p.days_held !== 0) continue;
    const ts = p.opened_at || p.entry_time || p.filled_at || null;
    if (!ts) continue;
    const qty = parseFloat(p.qty) || 0;
    const entryPx = parseFloat(p.avg_entry_price) || 0;
    const curPx = parseFloat(p.current_price) || entryPx;
    const upl = parseFloat(p.unrealized_pl) || 0;
    // unrealized_plpc is already a fraction (e.g. 0.0473 = 4.73%)
    const uplPct = (parseFloat(p.unrealized_plpc) || 0) * 100;
    events.push({
      kind: 'buy',
      symbol: p.symbol,
      sleeve: p.sleeve || 'unattributed',
      time: ts,
      pnl: upl,
      pct: uplPct,
      qty,
      entryPx,
      exitPx: curPx,
      id: `buy-${p.symbol}-${ts}`,
    });
  }
  events.sort((a, b) => {
    const ta = new Date(a.time || 0).getTime();
    const tb = new Date(b.time || 0).getTime();
    return tb - ta;
  });

  // Track which event IDs we've rendered before, so brand-new ones can
  // animate in. Using a Set keyed on stable IDs (symbol + timestamp).
  const seen = window.__pfTickerSeen = window.__pfTickerSeen || new Set();
  const isFirstRender = seen.size === 0;

  const fmtTime = (iso) => {
    if (!iso) return '—';
    const d = new Date(iso);
    return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false });
  };
  const SLEEVE_LABEL = { momentum: 'Momentum', swing: 'Swing', daytrade: 'Daytrade', scalper: 'Scalper', unattributed: 'Manual' };

  // Format a price as $X.XX or $X (no decimals if ≥$100, two if <$100).
  // Penny stocks (<$10) get two decimals so the move is visible.
  const fmtPx = (v) => {
    if (!isFinite(v)) return '—';
    if (v >= 100) return '$' + v.toFixed(0);
    return '$' + v.toFixed(2);
  };
  const fmtUsd = (v) => {
    const a = Math.abs(v);
    const sign = v >= 0 ? '+' : '−';
    if (a >= 1000) return `${sign}$${(a / 1000).toFixed(1)}k`;
    return `${sign}$${a.toFixed(0)}`;
  };
  const fmtPct = (v) => {
    const sign = v >= 0 ? '+' : '−';
    return `${sign}${Math.abs(v).toFixed(2)}%`;
  };

  const rows = events.slice(0, 15).map(ev => {
    // Only flag as "fresh" on subsequent renders (prevents the entire
    // initial list from animating in at once on page load).
    const fresh = !seen.has(ev.id) && !isFirstRender;
    seen.add(ev.id);
    const sleeveLbl = SLEEVE_LABEL[ev.sleeve] || ev.sleeve;
    const time = fmtTime(ev.time);
    const isSell = ev.kind === 'sell';
    const sideLbl = isSell ? 'SELL' : 'BUY';

    // Color/sign rules. Sells are realized; buys are live unrealized.
    // 0.05% threshold filters noise so freshly opened positions don't
    // flicker green/red on tick-level micro moves.
    const plCls = ev.pct > 0.05 ? 'pos' : ev.pct < -0.05 ? 'neg' : 'flat';
    const pctStr = fmtPct(ev.pct);
    const usdStr = fmtUsd(ev.pnl);
    const qtyStr = `${Math.round(ev.qty)}sh`;
    const moveStr = `${fmtPx(ev.entryPx)}→${fmtPx(ev.exitPx)}`;
    const liveTag = isSell ? '' : '<span class="pf-tk-livedot" title="live mark"></span>';

    return `<div class="pf-tk-row ${ev.kind} ${plCls} ${fresh ? 'fresh' : ''}" data-id="${ev.id}">
      <span class="pf-tk-time">${time}</span>
      <span class="pf-tk-action ${ev.kind}">${sideLbl}</span>
      <span class="pf-tk-sym">${ev.symbol}</span>
      <span class="pf-tk-sleeve ${ev.sleeve}">${sleeveLbl}</span>
      <span class="pf-tk-trade">
        <span class="pf-tk-qty">${qtyStr}</span>
        <span class="pf-tk-move">${moveStr}${liveTag}</span>
      </span>
      <span class="pf-tk-pct ${plCls}">${pctStr}</span>
      <span class="pf-tk-pl ${plCls}">${usdStr}</span>
    </div>`;
  }).join('');

  // Status strip — what just happened, what's next.
  const lastEv = events[0];
  const lastStr = lastEv
    ? `last <b>${lastEv.kind === 'sell' ? 'SELL' : 'BUY'} ${lastEv.symbol}</b> · ${fmtTime(lastEv.time)}`
    : 'awaiting first trade today';
  const next = pfNextCronFire();
  const nextStr = `next <b>${next.label}</b> in ${next.countdown}`;
  const buys = openOrders.filter(o => (o.side || '').toLowerCase() === 'buy').length;
  const pendingStr = buys > 0 ? `· ${buys} pending fill${buys === 1 ? '' : 's'}` : '';

  return `<div class="pf-live-strip">
    <div class="pf-live-status">
      <span class="pf-live-dot"></span>
      <span class="pf-live-label">LIVE</span>
      <span class="pf-live-bit">${lastStr}</span>
      <span class="pf-live-bit">${nextStr}</span>
      ${pendingStr ? `<span class="pf-live-bit">${pendingStr}</span>` : ''}
    </div>
    <div class="pf-tk-list">
      ${rows || '<div class="pf-tk-empty">Awaiting first trade today.</div>'}
    </div>
  </div>`;
}

// Closed-trades section — renders the chronological list of trades that
// closed today (paired buy + sell from Alpaca FILL activities). Sits
// directly below the live positions block so the eye flows
// open → closed for cross-reference. closed_today comes pre-paired from
// /api/portfolio: {symbol, qty, buy_price, sell_price, pnl, sleeve, closed_at}.
// Non-strategist sees teaser-truncated 3 rows + upgrade nudge.
function pfClosedTradesPanel(closedToday, isStrategist, totalRealizedToday) {
  const rows = closedToday || [];
  // When the trader hasn't fired any closes yet today, show an honest
  // empty state rather than hiding the section entirely — keeps the
  // structural placeholder visible so visitors know to expect it later.
  if (!rows.length) {
    return `<div class="pf-closed-section">
      <div class="pf-closed-head">
        <span class="pf-closed-title">Closed today</span>
        <span class="pf-closed-summary">No closes yet · daytrade EOD at 19:55 UTC</span>
      </div>
      <div class="pf-closed-empty">
        <strong>The trader hasn't closed any positions today.</strong>
        <span class="small">Daytrade sleeve force-closes at market close (15:55 ET / 19:55 UTC). Bracket stops/targets close earlier if hit.</span>
      </div>
    </div>`;
  }

  // Aggregate header: count + summed P&L
  const total = totalRealizedToday != null
    ? totalRealizedToday
    : rows.reduce((s, r) => s + (r.pnl || 0), 0);
  const totalCls = total > 0.5 ? 'pos' : (total < -0.5 ? 'neg' : '');
  const totalSign = total >= 0 ? '+' : '−';
  const totalStr = `${totalSign}${fmtUSD(Math.abs(total), { digits: 0 }).replace(/^[\+\-−]/, '')}`;

  const fmtTime = (iso) => {
    if (!iso) return '—';
    const d = new Date(iso);
    return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false });
  };
  const fmtMoney = (v) => v == null ? '—'
    : (v >= 100 ? `$${Number(v).toFixed(0)}` : `$${Number(v).toFixed(2)}`);

  // Display label for the sleeve pill — "unattributed" reads as opaque
  // jargon to anyone not staring at the codebase, so render it as
  // "Manual" with a hover explanation. Other sleeves use their natural name.
  const SLEEVE_DISPLAY = {
    momentum: 'Momentum', swing: 'Swing', daytrade: 'Day trade',
    scalper: 'Scalper', unattributed: 'Manual',
  };
  const SLEEVE_TITLE = {
    unattributed: 'Manual or legacy fill — opened outside the engine, or before sleeve-encoded order IDs went live',
  };
  const items = rows.map(r => {
    const sleeve = r.sleeve || 'unattributed';
    const sleeveLabel = SLEEVE_DISPLAY[sleeve] || sleeve;
    const sleeveTitle = SLEEVE_TITLE[sleeve] || `Sleeve: ${sleeveLabel}`;
    const tier = r.source_tier || null;
    const tierLabel = tier ? tier[0].toUpperCase() + tier.slice(1) : '';
    const tierPill = tier
      ? `<span class="pf-pos-tier ${tier}" title="From the ${tierLabel} section of /picks">${tierLabel}</span>`
      : '';
    const pnl = r.pnl || 0;
    const pnlCls = pnl > 0 ? 'pos' : (pnl < 0 ? 'neg' : '');
    const pnlSign = pnl >= 0 ? '+' : '−';
    const pnlStr = `${pnlSign}$${Math.abs(pnl).toFixed(2)}`;
    // Pct = (sell - buy) / buy. Skip when buy_price = 0 (defensive).
    let pctStr = '';
    if (r.buy_price && r.buy_price > 0) {
      const pct = ((r.sell_price - r.buy_price) / r.buy_price) * 100;
      pctStr = `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
    }
    return `<div class="pf-closed-row">
      <span class="pf-closed-time">${fmtTime(r.closed_at)}</span>
      <span class="pf-closed-tag ${sleeve}" title="${sleeveTitle}">${sleeveLabel}</span>
      ${tierPill}
      <span class="pf-closed-sym">${r.symbol || '—'}</span>
      <span class="pf-closed-flow">
        ${Math.round(r.qty || 0)} qty · ${fmtMoney(r.buy_price)}<span class="arrow">→</span>${fmtMoney(r.sell_price)}
      </span>
      <span class="pf-closed-pnl ${pnlCls}">${pnlStr}</span>
      <span class="pf-closed-pct">${pctStr}</span>
    </div>`;
  }).join('');

  const teaserCta = !isStrategist
    ? `<div class="pf-closed-cta">Showing ${rows.length} most recent — <a href="/pricing">unlock the full ledger</a> to see every fill, sleeve, and outcome from the past 90 days.</div>`
    : '';

  return `<div class="pf-closed-section">
    <div class="pf-closed-head">
      <span class="pf-closed-title">Closed today</span>
      <span class="pf-closed-summary">
        ${rows.length} ${rows.length === 1 ? 'trade' : 'trades'} ·
        <span class="${totalCls}">${totalStr} realized</span>
      </span>
    </div>
    <div class="pf-closed-list">${items}</div>
    ${teaserCta}
  </div>`;
}

// Next rotation tick during US market hours (14:00-19:30 UTC weekdays,
// every 30 min). Returns { mins } when there's an upcoming tick today,
// { note } for off-hours messaging, or null if we shouldn't render.
function pfNextRotationTick() {
  const now = new Date();
  const dow = now.getUTCDay();
  if (dow === 0 || dow === 6) {
    return { note: 'rotation: weekday market hours' };
  }
  const minutesNow = now.getUTCHours() * 60 + now.getUTCMinutes();
  const WINDOW_START = 14 * 60;          // 14:00 UTC
  const WINDOW_END = 19 * 60 + 30;       // 19:30 UTC (last rotate; close fires 19:55)
  if (minutesNow >= WINDOW_END) {
    return { note: 'rotation: complete for today' };
  }
  let next;
  if (minutesNow < WINDOW_START) {
    next = WINDOW_START;
  } else {
    // Round up to next 30-min mark, but if we're exactly on one, push to next
    next = Math.ceil((minutesNow + 0.5) / 30) * 30;
  }
  if (next > WINDOW_END) {
    return { note: 'rotation: complete for today' };
  }
  return { mins: Math.max(0, next - minutesNow) };
}
// Sleeve color palette — mirrored in CSS `.pf-pos-tag.<sleeve>` rules so
// the donut, the weight bars, and the cards all read as one system.
const PF_SLEEVE_COLOR = {
  momentum: '#ba8cff',
  swing: '#5FAAC5',
  daytrade: '#e8b86a',
  scalper: '#6ee7b7',
  unattributed: '#9aa0a6',
};
// Sleeve thesis copy — surfaced inside each expanded sleeve so a viewer
// understands what the lane is *for* before they read the position list.
// Phrased to sit between scan-grade ("Momentum lane") and recipe-grade
// (no thresholds, no signal names) per the public-methodology rule.
function pfSleeveThesis(name, summary) {
  const map = {
    momentum: {
      rule: 'Multi-day · bracketed',
      body: '<b>Trend continuation lane.</b> Top-ranked names that just confirmed a multi-session breakout. Each entry ships with a stop and target on submission so a sudden reversal exits without supervision.',
    },
    swing: {
      rule: '5-day hold · -7% / +15%',
      body: '<b>Asymmetric swing lane.</b> Top-ranked picks held for one trading week with a tight stop and roughly twice that as upside target. Designed for an expectancy edge even if hit-rate sits below 50%.',
    },
    daytrade: {
      rule: 'Intraday · -3% / +5%',
      body: '<b>Intraday rotation lane.</b> Opens at the bell, exits before close. The rotator fires every 30 minutes through the cash session and force-closes any survivor at 19:55 UTC so the lane starts each day flat.',
    },
    scalper: {
      rule: '5-min cadence · ≤25 min hold',
      body: '<b>Intraday signal lane — separate engine from the nightly picks.</b> Scans high-volume names every five minutes during the cash session for short-lived volume + range setups. Each entry is small, time-boxed (auto-exit inside ~25 min), and protected by a tight bracket. Concurrent positions are hard-capped and a kill-switch flag in trader state can disable the lane mid-session if drawdown gets noisy. Currently learning whether the signals print on real fills before sizing up.',
    },
    unattributed: {
      rule: 'Legacy fills',
      body: '<b>Unattributed legacy positions.</b> Entries opened before the sleeve-encoded order IDs went live, or manual fills outside the engine. Liquidated naturally as their existing brackets resolve — no new entries land here.',
    },
  };
  const cfg = map[name] || { rule: '', body: '' };
  const realized = summary?.realized_today || 0;
  const realizedNote = Math.abs(realized) > 0.5
    ? ` <span style="color:${realized > 0 ? 'var(--pos)' : 'var(--neg)'};">${realized > 0 ? '+' : '−'}${fmtUSD(Math.abs(realized), { digits: 0 }).replace(/^[-+]/, '')} realized today.</span>`
    : '';
  return `<div class="pf-pdl-thesis">
    ${cfg.rule ? `<span class="ts-rule">${cfg.rule}</span>` : ''}
    ${cfg.body}${realizedNote}
  </div>`;
}

// Line-style position row — eight columns aligned under the labeled
// header above. Ticker is plain text (the prior dot-pattern mask was
// removed when users called out that they couldn't see the symbols).
function pfPositionRow(p) {
  const pct = (p.unrealized_plpc || 0) * 100;
  const pctCls = pct > 0.005 ? 'pos' : (pct < -0.005 ? 'neg' : 'flat');
  const plRaw = p.unrealized_pl || 0;
  const plCls = plRaw > 0.5 ? 'pos' : (plRaw < -0.5 ? 'neg' : 'flat');
  const sleeve = p.sleeve || 'unattributed';
  // Tier column — where this position came from on /picks. When the
  // backend can't attribute (older fills, manual entries), the label
  // becomes "MANUAL" with a hover tooltip explaining what that means
  // rather than the opaque "UNATTRIBUTED" badge users couldn't decode.
  const tier = p.source_tier || sleeve;
  const TIER_ABBR = {
    conservative: 'CONS', moderate: 'MOD', aggressive: 'AGG', asymmetric: 'ASYM',
    momentum: 'MOM', swing: 'SWG', daytrade: 'DAY', scalper: 'SCP',
    unattributed: 'MANUAL',
  };
  const tierLabel = TIER_ABBR[tier] || (tier || '').toUpperCase().slice(0, 4) || '—';
  const tierTitle = p.source_tier
    ? `${p.source_tier[0].toUpperCase() + p.source_tier.slice(1)} risk tier on /picks`
    : tier === 'unattributed'
      ? 'Manual or legacy fill — opened before sleeve-encoded order IDs went live, or executed outside the engine'
      : `Strategy: ${sleeve}`;
  const sym = p.symbol || '';

  // Cost basis — prefer the Alpaca-supplied figure, fall back to
  // qty × avg_entry_price if for some reason it's zero.
  const qty = parseFloat(p.qty) || 0;
  const avgEntry = parseFloat(p.avg_entry_price) || 0;
  const costBasis = parseFloat(p.cost_basis) || (qty * avgEntry) || 0;

  // Acquired date — derive from days_held against today rather than
  // requiring a backend change. "Apr 21" reads cleanly in the column.
  let acquiredLabel = '—';
  let acquiredTitle = '';
  if (typeof p.days_held === 'number' && p.days_held >= 0) {
    const d = new Date();
    d.setUTCHours(0, 0, 0, 0);
    d.setUTCDate(d.getUTCDate() - p.days_held);
    acquiredLabel = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    acquiredTitle = `Acquired ${p.days_held === 0 ? 'today' : p.days_held + ' day' + (p.days_held === 1 ? '' : 's') + ' ago'}`;
  }

  // Trend cell — color-intensity-scaled arrow.
  //   <2%   → 0.40 alpha (dim)
  //   2–5%  → 0.65 alpha
  //   5–10% → 0.85 alpha
  //   ≥10%  → 1.00 alpha (full)
  // Combined with sleeve green/red, runners and stops jump out of a
  // long list at a glance without forcing the eye to read every %.
  const absPct = Math.abs(pct);
  let intensity = 0.40;
  if (absPct >= 10) intensity = 1.00;
  else if (absPct >= 5) intensity = 0.85;
  else if (absPct >= 2) intensity = 0.65;
  const arrowGlyph = pct > 0.005 ? '▲' : (pct < -0.005 ? '▼' : '◆');
  const arrowColor = pct > 0.005 ? `rgba(110,231,183,${intensity})`
                   : pct < -0.005 ? `rgba(252,165,165,${intensity})`
                   : `rgba(155,161,185,${intensity})`;
  // Tooltip carries the bracket context that USED to live in its own
  // column, plus total return since acquisition. The arrow's color
  // already encodes the day move — duplicating it as text added no
  // information, and the user called that out as useless.
  const lifetimePct = (avgEntry > 0 && p.current_price)
    ? ((p.current_price - avgEntry) / avgEntry) * 100 : null;
  const sinceLine = (lifetimePct != null && typeof p.days_held === 'number')
    ? `Since acquired (${p.days_held}d): ${lifetimePct >= 0 ? '+' : ''}${lifetimePct.toFixed(2)}%`
    : '';
  const priceLine = (p.current_price && avgEntry)
    ? `Now $${parseFloat(p.current_price).toFixed(p.current_price < 10 ? 2 : 0)} · Entry $${avgEntry.toFixed(avgEntry < 10 ? 2 : 0)}`
    : '';
  const bracketLine = (p.stop_price != null && p.target_price != null)
    ? `Stop $${parseFloat(p.stop_price).toFixed(p.stop_price < 10 ? 2 : 0)} · Target $${parseFloat(p.target_price).toFixed(p.target_price < 10 ? 2 : 0)}`
    : '';
  const arrowTitle = [sinceLine, priceLine, bracketLine].filter(Boolean).join('\n')
    || `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}% today`;

  return `<div class="pf-pdl-line" data-pf-row-sym="${sym}">
    <span class="pf-rl-sym">${sym}</span>
    <span class="pf-rl-tier ${tier}" title="${tierTitle}">${tierLabel}</span>
    <span class="pf-rl-mv">${fmtUSD(p.market_value, { digits: 0 })}</span>
    <span class="pf-rl-cost">${fmtUSD(costBasis, { digits: 0 })}</span>
    <span class="pf-rl-pct ${pctCls}">${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%</span>
    <span class="pf-rl-pl ${plCls}">${fmtUSD(plRaw, { signed: true, digits: 0 })}</span>
    <span class="pf-rl-acquired" title="${acquiredTitle}">${acquiredLabel}</span>
    <span class="pf-rl-trend" style="color:${arrowColor};" title="${arrowTitle}">${arrowGlyph}</span>
  </div>`;
}

// Capital-allocation donut for open positions. TWO concentric rings —
// outer = source_tier (conservative/moderate/aggressive/asymmetric),
// inner = sleeve (momentum/swing/daytrade/scalper). Sleeve color encodes
// trade type at a glance; the outer band layers on the /picks tier the
// position came from, so a viewer can read both dimensions without a
// separate chart. Center: total deployed + position count. Hover/tap a
// slice (either ring) → tooltip via wirePortfolioTooltips().
function pfPositionsDonut(positions, totalMv) {
  if (!positions.length || totalMv <= 0) return '';
  // Group by sleeve order first, then mv desc within sleeve. The sleeve
  // grouping is what fixes the "random green slice in the blue section"
  // bug — without it, a scalper position with high MV could land between
  // two swing slices and read as a discontinuity. Now the inner ring
  // shows a clean violet-blue-orange-green sweep.
  const SLEEVE_ORDER = { momentum: 0, swing: 1, daytrade: 2, scalper: 3, unattributed: 4 };
  const sorted = [...positions].sort((a, b) => {
    const sa = SLEEVE_ORDER[a.sleeve || 'unattributed'] ?? 5;
    const sb = SLEEVE_ORDER[b.sleeve || 'unattributed'] ?? 5;
    if (sa !== sb) return sa - sb;
    const d = (b.market_value || 0) - (a.market_value || 0);
    return d !== 0 ? d : (a.symbol || '').localeCompare(b.symbol || '');
  });
  // Tight gaps — wide separators turned 36 positions into a wagon-wheel
  // and made the rings feel cheap. Keep enough whitespace to read the
  // slice boundary, no more. Background color shows through the gap.
  const GAP = 0.10;
  // Per-sleeve max so opacity TRACES (not screams) sleeve weight —
  // largest position is full brightness, smallest is 0.78. Subtle
  // enough to keep the donut reading as one cohesive visual.
  const sleeveMax = {};
  for (const p of sorted) {
    const s = p.sleeve || 'unattributed';
    const mv = p.market_value || 0;
    if (mv > (sleeveMax[s] || 0)) sleeveMax[s] = mv;
  }
  // Tier color resolution. Falls back to the sleeve color when a
  // position has no source_tier (manual/legacy fills) so the outer ring
  // stays continuous instead of dropping to a gray gap.
  const TIER_COLOR = {
    conservative: 'var(--tier-conservative)',
    moderate:     'var(--tier-moderate)',
    aggressive:   'var(--tier-aggressive)',
    asymmetric:   'var(--tier-asymmetric)',
    unattributed: PF_SLEEVE_COLOR.unattributed,
  };
  let offset = 0;
  const arcsPnl = [];
  const arcsOuter = [];
  const arcsInner = [];
  // Day P&L aggregate for the centerpiece (book-level performance).
  let totalPl = 0;
  for (const p of sorted) {
    const mv = p.market_value || 0;
    const pct = (mv / totalMv) * 100;
    if (pct <= 0) continue;
    const visiblePct = Math.max(0.05, pct - GAP);
    const dashArr = `${visiblePct.toFixed(3)} ${(100 - visiblePct).toFixed(3)}`;
    const dashOff = (-offset).toFixed(3);
    const sleeve = p.sleeve || 'unattributed';
    const tier = p.source_tier || sleeve;
    const sleeveCol = PF_SLEEVE_COLOR[sleeve] || PF_SLEEVE_COLOR.unattributed;
    const tierCol = TIER_COLOR[tier] || TIER_COLOR.unattributed;
    const sm = sleeveMax[sleeve] || mv;
    // Subtle weight tracing — biggest position 1.0, smallest 0.78. Old
    // 0.55 floor created a too-busy dim/bright stripe pattern.
    const op = (0.78 + 0.22 * (mv / sm)).toFixed(2);
    // Per-position P&L color for the outer corona. Strong floor (0.65)
    // so even flat positions have a visible green/red whisper; full
    // saturation kicks in at ~10%+ moves.
    const plPct = (p.unrealized_plpc || 0) * 100;
    const plRaw = p.unrealized_pl || 0;
    totalPl += plRaw;
    const plColor = plPct > 0.5 ? 'var(--pos)'
                  : plPct < -0.5 ? 'var(--neg)'
                  : 'rgba(155,161,185,0.55)';
    const plOp = Math.min(1, 0.65 + Math.abs(plPct) / 15).toFixed(2);
    const dataAttrs = `data-pf-sym="${p.symbol}" data-pf-sleeve="${sleeve}" data-pf-tier="${tier}"`
      + ` data-pf-amt="${mv.toFixed(2)}" data-pf-pct="${pct.toFixed(2)}"`
      + ` data-pf-pl="${plRaw.toFixed(2)}"`
      + ` data-pf-plpc="${plPct.toFixed(2)}"`;
    // Three rings with a clear hierarchy:
    //   Corona (P&L)   — r=19.5, thin    (a halo of green/red)
    //   Tier ring      — r=16.5, chunky  (the "outer" data band)
    //   Strategy ring  — r=11.5, chunky  (the "inner" data band)
    // 1-unit air-gap between corona/tier and tier/strategy so the rings
    // don't read as a solid block — they breathe.
    arcsPnl.push(`<circle cx="21" cy="21" r="19.5" pathLength="100" fill="transparent"
      stroke="${plColor}" stroke-width="0.9"
      stroke-dasharray="${dashArr}" stroke-dashoffset="${dashOff}"
      stroke-opacity="${plOp}"
      transform="rotate(-90 21 21)" ${dataAttrs}></circle>`);
    arcsOuter.push(`<circle cx="21" cy="21" r="16.5" pathLength="100" fill="transparent"
      stroke="${tierCol}" stroke-width="3.4"
      stroke-dasharray="${dashArr}" stroke-dashoffset="${dashOff}"
      stroke-opacity="${op}"
      transform="rotate(-90 21 21)" ${dataAttrs}></circle>`);
    arcsInner.push(`<circle cx="21" cy="21" r="11.5" pathLength="100" fill="transparent"
      stroke="${sleeveCol}" stroke-width="4.4"
      stroke-dasharray="${dashArr}" stroke-dashoffset="${dashOff}"
      stroke-opacity="${op}"
      transform="rotate(-90 21 21)" ${dataAttrs}></circle>`);
    offset += pct;
  }
  const arcs = arcsPnl.join('') + arcsOuter.join('') + arcsInner.join('');

  // Center stack — total deployed (largest), DEPLOYED label, position
  // count, and a colored day-P&L line so the user reads the donut's
  // performance without having to scan slices. The P&L line is the
  // first place the eye lands after the dollar total.
  // Sizing is calibrated so the entire stack fits inside the donut's
  // clear inner area (~46% of the donut diameter) at every breakpoint.
  // If you change ring proportions, re-verify these numbers.
  const totalStr = fmtUSD(totalMv, { digits: 0 });
  const _len = totalStr.replace(/[^\d$.,]/g, '').length;
  const _vw = (typeof window !== 'undefined' && window.innerWidth) || 1200;
  // Donut is 320px desktop, ~46% clear hole = 147px wide, so the
  // dollar amount must fit there — _baseMax 26 keeps "$132,944" at
  // ~22px Crimson Text, comfortably inside the hole.
  const _baseMax = _vw < 380 ? 16 : _vw < 540 ? 20 : _vw < 920 ? 22 : 26;
  const _scale = _len > 11 ? 0.55 : _len > 9 ? 0.68 : _len > 7 ? 0.82 : 1.00;
  const _font = Math.max(13, Math.round(_baseMax * _scale));
  const totalPlPct = totalMv > 0 ? (totalPl / totalMv) * 100 : 0;
  const plCenterCls = totalPl > 0.5 ? 'pos' : totalPl < -0.5 ? 'neg' : 'flat';
  const plSign = totalPl > 0 ? '+' : (totalPl < 0 ? '−' : '');
  // No parens — saves 2 chars so the line fits the hole at 240–320px.
  const plCenterStr = `${plSign}${fmtUSD(Math.abs(totalPl), { digits: 0 }).replace(/^[-]/, '')}  ${plSign}${Math.abs(totalPlPct).toFixed(2)}%`;

  // Sleeve-totals legend below the donut. Each row is a <details> group:
  // summary = the dot/name/count/$/pct line; opening it reveals the
  // sleeve thesis + a column-labeled list of every position assigned to
  // that sleeve. This is what makes the cards "nest" inside the donut
  // sections rather than floating below as a separate grid.
  const bySleeve = {};
  const positionsBySleeve = {};
  const sleeveSummaries = window.__pfSleeveSummaries || {};
  for (const p of sorted) {
    const s = p.sleeve || 'unattributed';
    bySleeve[s] = (bySleeve[s] || 0) + (p.market_value || 0);
    (positionsBySleeve[s] = positionsBySleeve[s] || []).push(p);
  }
  const ORDER = ['momentum', 'swing', 'daytrade', 'scalper', 'unattributed'];
  const LABELS = { momentum: 'Momentum', swing: 'Swing', daytrade: 'Day trade', scalper: 'Scalper', unattributed: 'Manual / legacy' };
  // Persist open/closed state across 30s refresh — without this, every
  // refresh would auto-collapse expanded sleeves mid-read.
  const openSet = window.__pfOpenSleeves = window.__pfOpenSleeves || new Set();

  const legend = ORDER
    .filter(s => (bySleeve[s] || 0) > 0 || (positionsBySleeve[s] || []).length > 0)
    .map(s => {
      const amt = bySleeve[s] || 0;
      const pct = totalMv > 0 ? ((amt / totalMv) * 100).toFixed(1) : '0.0';
      const sleevePositions = positionsBySleeve[s] || [];
      const n = sleevePositions.length;
      const isOpen = openSet.has(s);
      const emptyCls = n === 0 ? ' pf-pdl-empty' : '';
      // Column headers — only render once per sleeve group, above the rows.
      const colHeader = `<div class="pf-pdl-cols" aria-hidden="true">
        <span>Ticker</span>
        <span>Risk tier</span>
        <span>Market value</span>
        <span class="col-cost">Cost basis</span>
        <span>Day %</span>
        <span class="col-pl">Unrealized</span>
        <span class="col-acquired">Acquired</span>
        <span>Trend</span>
      </div>`;
      const lines = sleevePositions.map(pfPositionRow).join('');
      const body = n > 0
        ? `<div class="pf-pdl-body">
             ${pfSleeveThesis(s, sleeveSummaries[s])}
             ${colHeader}
             ${lines}
           </div>`
        : `<div class="pf-pdl-body">
             ${pfSleeveThesis(s, sleeveSummaries[s])}
           </div>`;
      const openAttr = isOpen ? ' open' : '';
      return `<details class="pf-pdl-group${emptyCls}" data-sleeve="${s}"${openAttr}>
        <summary class="pf-pdl-summary">
          <span class="pf-pdl-dot" style="background:${PF_SLEEVE_COLOR[s]}"></span>
          <span class="pf-pdl-name">${LABELS[s] || s}</span>
          <span class="pf-pdl-count">${n} pos</span>
          <span class="pf-pdl-amt">${fmtUSD(amt, { digits: 0 })}</span>
          <span class="pf-pdl-pct">${pct}%</span>
          <span class="pf-pdl-chev" aria-hidden="true"></span>
        </summary>
        ${body}
      </details>`;
    }).join('');

  // Tier mini-key — explains the OUTER ring at a glance. The sleeve
  // legend below it already explains the inner ring (each row's left dot
  // is the sleeve color), so this strip only needs to teach tier colors.
  // Naming: outer = "Risk tier" (who this trade is sized for), inner =
  // "Strategy" (how this trade executes — covered by the sleeve dots).
  const tierKey = `<div class="pf-pdl-tier-key" aria-label="Outer ring colors by risk tier">
    <span class="pf-pdl-tk-label">Outer · Risk tier</span>
    <span class="pf-pdl-tk-chip" style="background:var(--tier-conservative)"></span>
    <span class="pf-pdl-tk-name">Conservative</span>
    <span class="pf-pdl-tk-chip" style="background:var(--tier-moderate)"></span>
    <span class="pf-pdl-tk-name">Moderate</span>
    <span class="pf-pdl-tk-chip" style="background:var(--tier-aggressive)"></span>
    <span class="pf-pdl-tk-name">Aggressive</span>
    <span class="pf-pdl-tk-chip" style="background:var(--tier-asymmetric)"></span>
    <span class="pf-pdl-tk-name">Asymmetric</span>
    <span class="pf-pdl-tk-divider"></span>
    <span class="pf-pdl-tk-label">Inner · Strategy</span>
    <span class="pf-pdl-tk-chip" style="background:${PF_SLEEVE_COLOR.momentum}"></span>
    <span class="pf-pdl-tk-name">Momentum</span>
    <span class="pf-pdl-tk-chip" style="background:${PF_SLEEVE_COLOR.swing}"></span>
    <span class="pf-pdl-tk-name">Swing</span>
    <span class="pf-pdl-tk-chip" style="background:${PF_SLEEVE_COLOR.daytrade}"></span>
    <span class="pf-pdl-tk-name">Day trade</span>
    <span class="pf-pdl-tk-chip" style="background:${PF_SLEEVE_COLOR.scalper}"></span>
    <span class="pf-pdl-tk-name">Scalper</span>
  </div>`;

  // Aggregate-P&L glow class (panel-level halo color).
  const panelGlowCls = totalPl > 0.5 ? 'panel-glow-pos' : totalPl < -0.5 ? 'panel-glow-neg' : '';
  return `<div class="pf-pos-donut-wrap ${panelGlowCls}">
    <div class="pf-pos-donut alloc-donut">
      <svg viewBox="0 0 42 42" aria-label="Open positions by capital allocation, risk tier, and P&L">
        <defs>
          <!-- Center glow: faint lake bloom inside the donut hole. -->
          <radialGradient id="pf-donut-center-glow" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stop-color="rgba(95,170,197,0.07)"/>
            <stop offset="100%" stop-color="transparent"/>
          </radialGradient>
          <!-- Dome lighting: top-left highlight + bottom-right shadow.
               Blended at low opacity over the rings to fake 3D curvature. -->
          <radialGradient id="pf-donut-dome" cx="32%" cy="22%" r="62%">
            <stop offset="0%"   stop-color="rgba(255,255,255,0.22)"/>
            <stop offset="40%"  stop-color="rgba(255,255,255,0.04)"/>
            <stop offset="100%" stop-color="rgba(0,0,0,0.28)"/>
          </radialGradient>
          <!-- Top rim highlight on the corona only. -->
          <linearGradient id="pf-rim-top" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="rgba(255,255,255,0.20)"/>
            <stop offset="55%" stop-color="rgba(255,255,255,0.00)"/>
          </linearGradient>
        </defs>
        <circle cx="21" cy="21" r="9.5" fill="url(#pf-donut-center-glow)"/>
        <circle cx="21" cy="21" r="19.5" fill="transparent" stroke="rgba(155,161,185,0.04)" stroke-width="0.9"/>
        <circle cx="21" cy="21" r="16.5" fill="transparent" stroke="rgba(155,161,185,0.05)" stroke-width="3.4"/>
        <circle cx="21" cy="21" r="11.5" fill="transparent" stroke="rgba(155,161,185,0.07)" stroke-width="4.4"/>
        ${arcs}
        <!-- 3D dome overlay: drawn on top of the data rings, blended via
             CSS mix-blend-mode so it lights/shades each colored slice
             instead of painting over them. The pointer-events:none means
             it doesn't block hover on the rings beneath. -->
        <circle class="pf-donut-dome-overlay" cx="21" cy="21" r="20.4"
                fill="url(#pf-donut-dome)" pointer-events="none"/>
        <!-- Top rim: subtle white highlight on the upper half of the
             corona ring only — gives the rings a "polished metal" cap. -->
        <circle cx="21" cy="21" r="19.5" fill="transparent"
                stroke="url(#pf-rim-top)" stroke-width="0.9"
                pointer-events="none" opacity="0.6"/>
      </svg>
      <div class="dt-center">
        <div class="dt-inner">
          <div class="dt-eyebrow"><span class="dt-live"></span>LIVE · BOOK</div>
          <div class="dt-total" style="font-size:${_font}px;">${totalStr}</div>
          <div class="dt-pl ${plCenterCls}">${plCenterStr}</div>
          <div class="dt-caption">${positions.length} open</div>
        </div>
      </div>
    </div>
    <div class="pf-pos-donut-legend">
      ${tierKey}
      ${legend}
    </div>
  </div>`;
}
// Returns "Mon 09:30 ET (in 14h 32m)" style copy for the next trader fire.
// We don't have a clock from /api/portfolio so we infer from the user's
// local time + the trader's known schedule (13:30 UTC open, 19:55 UTC
// close, Mon-Fri).
function pfNextEntryWindow() {
  const now = new Date();
  // Build candidate Date objects for the next open + close in UTC.
  const nextWeekdayAt = (h, m) => {
    for (let i = 0; i < 7; i++) {
      const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + i, h, m));
      const dow = d.getUTCDay();  // 0=Sun, 6=Sat
      if (dow === 0 || dow === 6) continue;
      if (d.getTime() > now.getTime()) return d;
    }
    return null;
  };
  const nextOpen = nextWeekdayAt(13, 30);
  if (!nextOpen) return null;
  const ms = nextOpen.getTime() - now.getTime();
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  // Render in viewer's local time so the countdown is meaningful regardless of TZ.
  const local = nextOpen.toLocaleString(undefined, { weekday: 'short', hour: '2-digit', minute: '2-digit' });
  const inStr = h >= 1 ? `${h}h ${m}m` : `${m}m`;
  return { iso: nextOpen.toISOString(), local, inStr };
}
function renderPortfolio(data) {
  if (!data || data.error) return '';  // 503/502 → render nothing, just keep the rest of the page
  const acct = data.account || {};
  const eq = acct.equity || 0;
  const dc = acct.day_change || 0, dcp = acct.day_change_pct || 0;
  const dcCls = dc > 0 ? 'pos' : (dc < 0 ? 'neg' : 'flat');
  const sign = dc > 0 ? '+' : (dc < 0 ? '−' : '');
  const dcAbs = Math.abs(dc), dcpAbs = Math.abs(dcp * 100);
  const paperTag = acct.is_paper ? `<span class="pf-paper-tag">Paper · ${acct.multiplier || 1}× margin</span>` : '';
  // Stash full payload so the chart's tab buttons can re-render without
  // re-fetching. Picked up by the document-level click handler.
  window.__pfData = data;
  // Pick a default chart window if none set: 1D when intraday has signal,
  // else 1M (the daily 30-day history).
  if (!window.__pfChartWindow) {
    const intradayPts = data.equity_intraday?.equity || [];
    const intradayHasSignal = intradayPts.length > 1
      && intradayPts.some(v => Math.abs((v || 0) - (intradayPts[0] || 0)) > 0.01);
    window.__pfChartWindow = intradayHasSignal ? '1D' : '1M';
  }
  const equityChart = pfEquityChart(data);
  const sleeves = data.sleeves || {};
  const positions = data.positions || [];
  const realizedToday = data.realized_today || 0;
  const closedToday = data.closed_today || [];
  // Sleeve totals reflect the entire book; positions[] may be teaser-truncated.
  const totalMv = Object.values(sleeves).reduce((s, m) => s + (m?.mv || 0), 0)
    || positions.reduce((s, p) => s + (p.market_value || 0), 0);
  const totalPositions = Object.values(sleeves).reduce((s, m) => s + (m?.n || 0), 0)
    || positions.length;
  // Three-sleeve layout (momentum / swing / daytrade). "unattributed"
  // surfaces only when there are unknown-sleeve positions — historically
  // pre-CID-encoding entries; should be empty for any new account.
  const hasUnattributed = (sleeves.unattributed?.n || 0) > 0;
  // Show scalper card whenever the sleeve has activity today (positions
  // open, realized PnL, or unrealized PnL non-zero) — keeps it hidden
  // when scalper.yml hasn't found any signals yet so the dashboard
  // doesn't render an empty 5th card.
  const sc = sleeves.scalper || {};
  const hasScalper = (sc.n || 0) > 0 || (sc.realized_today || 0) !== 0 || (sc.upnl || 0) !== 0;
  const extras = (hasScalper ? 1 : 0) + (hasUnattributed ? 1 : 0);
  const layoutCls = extras === 2 ? ' five' : extras === 1 ? ' four' : '';
  const sleeveBlock = `
    <div class="pf-sleeves${layoutCls}">
      ${pfSleeveCard('momentum', sleeves.momentum, '3-day momentum')}
      ${pfSleeveCard('swing', sleeves.swing, '5-day swing')}
      ${pfSleeveCard('daytrade', sleeves.daytrade, 'Day trade')}
      ${hasScalper ? pfSleeveCard('scalper', sleeves.scalper, 'Scalper · 5-min') : ''}
      ${hasUnattributed ? pfSleeveCard('unattributed', sleeves.unattributed, 'Other') : ''}
    </div>`;
  let positionsBlock;
  if (totalPositions === 0) {
    // Empty state — most viewers see this outside trading hours. Show a
    // live countdown to the next entry window so the panel still feels
    // active rather than dormant. The strategy strip below the count
    // (-7%/+15% swing brackets, -3%/+5% daytrade brackets) is general
    // enough not to give away methodology specifics.
    const nxt = pfNextEntryWindow();
    const countdown = nxt
      ? `<div class="pf-countdown">
           <span class="pf-countdown-label">Next entry window</span>
           <span class="pf-countdown-time">${nxt.local}</span>
           <span class="pf-countdown-in">in ${nxt.inStr}</span>
         </div>`
      : '';
    positionsBlock = `<div class="pf-empty">
      <strong>Idle between sessions.</strong>
      The book holds 20 positions during the trading day. Two sleeves: a 5-day swing on top-ranked names with an asymmetric reward profile, and an intraday lane that opens at the bell and exits at the close.
      ${countdown}
    </div>`;
  } else {
    // The donut block now embeds positions inside expandable sleeve
    // groups (no separate cards grid). Use positions-derived MV since
    // teaser-truncated payloads exclude some positions and we don't want
    // arc weights summing to >100% of the visible ring.
    const visibleMv = positions.reduce((s, p) => s + (p.market_value || 0), 0);
    // Stash sleeve summaries so the embedded thesis blocks can surface
    // realized-today figures without re-deriving them per row.
    window.__pfSleeveSummaries = sleeves;
    positionsBlock = pfPositionsDonut(positions, visibleMv);
    if (data.teaser) {
      positionsBlock += `<div class="pf-teaser-cta">Showing ${positions.length} of total — <a href="/pricing">unlock the full book</a> to see every fill, sleeve allocation, and intraday rebalance.</div>`;
    }
  }
  // Utilization = fraction of buying power currently in market value of
  // open positions. Useful at a glance: empty between sessions = 0%,
  // mid-day fully deployed = ~60-65% (because we run 1.5× swing + 1×
  // daytrade across half-equity each, on a 2× margin account).
  // Utilization denominator is *total* deployable buying power (equity ×
  // margin multiplier), NOT the live buying_power field — that field is
  // remaining headroom, so dividing by it overstates utilization once
  // we're partly deployed. With $100k equity and 2× margin the engine
  // can put up to $200k in the market; mid-day we run ~57% (1.5×
  // swing on half-equity + 1× daytrade on half-equity).
  const totalCapacity = (parseFloat(acct.equity) || 0) * (parseFloat(acct.multiplier) || 1);
  const utilization = totalCapacity > 0 ? Math.min(1, totalMv / totalCapacity) : 0;
  const utilStr = `${(utilization * 100).toFixed(0)}%`;

  return `<section class="pf-panel" id="pf-panel">
    <div class="pf-head">
      <div class="pf-eyebrow"><span class="pf-live-dot"></span> Live paper portfolio</div>
      ${paperTag}
    </div>
    <div class="pf-equity-row">
      <div>
        <div class="pf-equity-num">${fmtUSD(eq, { digits: 2 })}</div>
        <div class="pf-equity-sub">
          <span class="pf-day-pill ${dcCls}">${sign}${fmtUSD(dcAbs, { digits: 0 }).replace(/^[-]/, '')} · ${sign}${dcpAbs.toFixed(2)}%</span>
          <span class="pf-day-label">today</span>
          ${Math.abs(realizedToday) > 0.5 ? (() => {
            const rCls = realizedToday > 0 ? 'pos' : 'neg';
            const rSign = realizedToday > 0 ? '+' : '−';
            const rAbs = fmtUSD(Math.abs(realizedToday), { digits: 0 });
            return `<span class="pf-realized-pill ${rCls}" title="Realized P&L from today's closed positions">${rSign}${rAbs} realized</span>`;
          })() : ''}
          ${totalPositions > 0 ? `<span class="pf-util">${utilStr} deployed · ${fmtUSD(totalMv, { digits: 0 })} of ${fmtUSD(totalCapacity, { digits: 0 })} cap</span>` : ''}
        </div>
      </div>
    </div>
    ${equityChart}
    ${pfLiveActivityStrip(data)}
    ${pfPendingOrdersStrip(data.open_orders)}
    <div class="pf-positions-head">
      <span class="pf-positions-title">Open positions · expand a sleeve to see the names</span>
      <span class="pf-positions-count">
        ${(() => {
          if (totalPositions === 0) return '0';
          const wins = positions.filter(p => (p.unrealized_pl || 0) > 0).length;
          const losses = positions.filter(p => (p.unrealized_pl || 0) < 0).length;
          const flat = positions.length - wins - losses;
          if (data.teaser) return `${positions.length} of ${totalPositions} shown`;
          return `<span class="pf-tally-w">${wins}</span> in green · <span class="pf-tally-l">${losses}</span> in red${flat ? ` · ${flat} flat` : ''}`;
        })()}
      </span>
    </div>
    ${positionsBlock}
    ${pfClosedTradesPanel(closedToday, !data.teaser, realizedToday)}
    ${sleeveBlock}
  </section>`;
}

// Per-pick footer line. Rotates between useful unique facts rather than
// repeating "Source: SEC EDGAR" on every card. Order of preference:
//   1. 80% projection band ($P10 → $P90) — most scannable at-a-glance
//   2. SEC filing period-end + form type — shows fundamentals freshness
//   3. Market cap, if we got it from FMP
//   4. Absolute fallback (should never hit in normal data)
function footerText(p) {
  const fmtMoney = (v) => v == null ? '—' : (v >= 100 ? `$${Math.round(v)}` : `$${Number(v).toFixed(2)}`);
  // Prefer the 80% band as percent moves from current price — more
  // scannable than raw $ and more honest than ±% of P50.
  if (p.p10 != null && p.p90 != null && p.current_price) {
    const lo = (p.p10 - p.current_price) / p.current_price;
    const hi = (p.p90 - p.current_price) / p.current_price;
    const fp = (v) => `${v >= 0 ? '+' : ''}${Math.round(v*100)}%`;
    return `1y 80% band &nbsp;<b style="color:var(--text-hi);font-weight:500;">${fp(lo)} → ${fp(hi)}</b> &nbsp;(${fmtMoney(p.p10)}–${fmtMoney(p.p90)})`;
  }
  const sec = p.sec_fundamentals || {};
  if (sec.as_of) {
    const form = sec.form || 'filing';
    return `Fundamentals as of ${sec.as_of} · ${form}`;
  }
  const mc = p.fundamentals?.market_cap;
  if (mc) {
    const bn = mc / 1e9;
    return `Market cap ~$${bn >= 10 ? bn.toFixed(0) : bn.toFixed(1)}B`;
  }
  return 'Ranked by projection-engine Sharpe proxy';
}

function sizeTier(mktCap) {
  if (!mktCap) return null;
  const bn = mktCap / 1e9;
  if (bn >= 200) return 'Mega';
  if (bn >= 10)  return 'Large';
  if (bn >= 2)   return 'Mid';
  if (bn >= 0.25) return 'Small';
  return 'Micro';
}

function formatMktCap(mktCap) {
  if (!mktCap) return null;
  const bn = mktCap / 1e9;
  if (bn >= 100) return `$${Math.round(bn)}B`;
  if (bn >= 10)  return `$${bn.toFixed(1)}B`;
  if (bn >= 1)   return `$${bn.toFixed(2)}B`;
  if (bn >= 0.01) return `$${Math.round(bn*1000)}M`;
  return `$${Math.round(mktCap/1e6)}M`;
}

function renderPickCard(p) {
  const sec = p.sec_fundamentals || {};
  const erClass = (p.expected_return ?? 0) >= 0 ? 'pos' : 'neg';
  const yoy = sec.revenue_yoy_growth;
  const companyName = p.company_name || (SECTOR_HINT[p.symbol]?.split(' · ')[0]) || '';
  const sector = p.sector || 'Equity';
  const industry = p.industry || '';
  // Prefer top-level market_cap (yfinance cache, high coverage) with
  // legacy fundamentals.market_cap as fallback.
  const mktCap = p.market_cap ?? p.fundamentals?.market_cap;
  const size = sizeTier(mktCap);
  const capStr = formatMktCap(mktCap);
  const avgVol = p.avg_volume;
  const thesis = p.rationale ? `${p.rationale}.` : 'Ranked on projection-engine signal.';
  const thesisClass = '';
  const stats = [
    { l: 'Rev YoY',       v: fmtPct(sec.revenue_yoy_growth), pos: (sec.revenue_yoy_growth ?? 0) >= 0 },
    { l: 'Gross margin',  v: fmtPct(sec.gross_margin, false) },
    { l: 'Op margin',     v: fmtPct(sec.operating_margin, false) },
    { l: 'FCF / sales',   v: fmtPct(sec.fcf_to_revenue, false) },
    { l: 'Buyback / sales', v: fmtPct(sec.buyback_intensity, false) },
    { l: 'Net debt Δ',    v: fmtPct(sec.net_debt_change_pct), pos: (sec.net_debt_change_pct ?? 0) < 0 /* deleveraging good */ },
    { l: 'Sharpe proxy',  v: fmtSharpe(p.sharpe_proxy) },
    { l: 'Proj. vol',     v: fmtPct(p.risk, false) },
  ];
  const statsHtml = stats.map(s => `
      <div class="pc-stat">
        <div class="s-label">${s.l}</div>
        <div class="s-val ${s.v === '—' ? 'dim' : (s.pos === true ? 'pos' : s.pos === false ? 'neg' : '')}">${s.v}</div>
      </div>`).join('');

  // Meta row: sector · industry · size-tier · market cap.
  // Market cap is the liquidity proxy — a Strategist buying a micro-cap
  // needs to know they'll move the price; a mega-cap can absorb size.
  const metaBits = [];
  if (sector && sector !== 'Equity') metaBits.push(sector);
  if (industry && industry !== sector) metaBits.push(industry);
  let sizeBadge = '';
  if (size) {
    const tone = size === 'Micro' ? 'warn' : size === 'Small' ? 'caution' : 'neutral';
    sizeBadge = `<span class="size-pill ${tone}" title="${size}-cap${capStr ? ' · ' + capStr : ''}">${size}${capStr ? ` &middot; ${capStr}` : ''}</span>`;
  }
  const metaHtml = (metaBits.length || sizeBadge)
    ? `<div class="pc-meta">
         ${metaBits.map((b, i) => `${i > 0 ? '<span class="dot-sep">·</span>' : ''}<span>${b}</span>`).join('')}
         ${sizeBadge ? (metaBits.length ? '<span class="dot-sep">·</span>' : '') + sizeBadge : ''}
       </div>`
    : '';

  const conf = Math.max(0, Math.min(100, Number(p.confidence ?? 0)));
  const confClass = conf >= 70 ? 'conf-high' : conf >= 45 ? 'conf-mid' : 'conf-low';
  const confLabel = p.confidence_label || (conf >= 70 ? 'High conviction' : conf >= 45 ? 'Solid' : 'Speculative');
  const comp = p.confidence_components || {};
  const confBreakdown = `
    <div class="pc-conf-break">
      <div class="brk-row"><span>Risk-adjusted</span><span>${(comp.risk_adjusted ?? 0).toFixed(0)}/40</span></div>
      <div class="brk-row"><span>Fundamentals</span><span>${(comp.fundamentals ?? 0).toFixed(0)}/35</span></div>
      <div class="brk-row"><span>Model tightness</span><span>${(comp.model_tightness ?? 0).toFixed(0)}/25</span></div>
    </div>`;

  return `<div class="pick-card">
    <div class="pc-head">
      <div class="pc-id">
        <div class="pc-ticker-row">
          <span class="pc-sym">${p.symbol}</span>
          ${companyName ? `<span class="pc-name">${companyName}</span>` : ''}
        </div>
        ${metaHtml}
      </div>
      <div class="pc-conf">
        <div class="pc-conf-num ${confClass}">${conf}</div>
        <div class="pc-conf-label">${confLabel}</div>
        <div class="pc-conf-bar"><span style="width:${conf}%;"></span></div>
        ${confBreakdown}
      </div>
    </div>
    <div class="pc-hero">
      <div class="h-item"><div class="h-label">Current</div><div class="h-val">${fmtMoney(p.current_price)}</div></div>
      <div class="h-item"><div class="h-label">P50 target · 1y</div><div class="h-val">${fmtMoney(p.p50_target)}</div></div>
      <div class="h-item"><div class="h-label">Expected</div><div class="h-val ${erClass}">${fmtPct(p.expected_return)}</div></div>
    </div>
    <div class="pc-thesis">
      <div class="pc-thesis-eyebrow">Thesis</div>
      <div class="pc-thesis-body ${thesisClass}">${thesis}</div>
    </div>
    <div class="pc-stats">${statsHtml}</div>
    ${(() => {
      const allocKey = `${p.symbol}|${p.tier}`;
      const amt = ALLOC.byPick?.[allocKey];
      return amt ? `<div class="pc-alloc">Allocate ${fmtUsd(amt)}</div>` : '';
    })()}
    <div class="pc-foot">
      <span>${footerText(p)}</span>
      <a href="/app?ticker=${encodeURIComponent(p.symbol)}">Open full projection &rarr;</a>
    </div>
  </div>`;
}

// Sector normalization — collapse GICS variants so the pills stay tight.
const SECTOR_SHORT = {
  'Financial Services': 'Financials',
  'Communication Services': 'Communications',
  'Consumer Cyclical': 'Consumer Cyclical',
  'Consumer Defensive': 'Consumer Staples',
  'Basic Materials': 'Materials',
  'Real Estate': 'Real Estate',
};

function shortSector(s) {
  if (!s) return 'Unclassified';
  return SECTOR_SHORT[s] || s;
}

function tallySectors(picks) {
  const counts = new Map();
  for (const p of picks) {
    const s = shortSector(p.sector);
    counts.set(s, (counts.get(s) || 0) + 1);
  }
  // Sort descending by count, then alpha for ties.
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

function renderSectorStrip(picks, heavyThreshold = 0.40) {
  const tally = tallySectors(picks);
  if (!tally.length) return '';
  const classified = tally.filter(([s]) => s !== 'Unclassified');
  const classifiedTotal = classified.reduce((a, [, c]) => a + c, 0);
  // If fewer than a third of this tier's picks are classified, hide the
  // sector mix entirely — a row of "Unclassified" pills is worse than
  // nothing. Falls back gracefully as FMP coverage fills nightly.
  if (classifiedTotal < picks.length * 0.33) return '';
  const pills = classified.map(([sector, count]) => {
    const share = count / classifiedTotal;
    const cls = share >= heavyThreshold ? 'sp-heavy' : '';
    return `<span class="sector-pill ${cls}"><span>${sector}</span><span class="sp-count">${count}</span></span>`;
  }).join('');
  const topShare = classified[0][1] / classifiedTotal;
  const topSector = classified[0][0];
  const warn = topShare >= heavyThreshold
    ? `<div class="sector-warn"><b>Concentration flag:</b> ${Math.round(topShare*100)}% of this tier is in ${topSector}. A taker of all ${classifiedTotal} classified picks would be overweight — trim or substitute if your portfolio is already heavy there.</div>`
    : '';
  return `<div class="sector-strip"><span class="sector-label">Sector mix</span>${pills}</div>${warn}`;
}

function renderPortfolioSignal(allPicks, picksData) {
  if (!allPicks.length) return '';
  const total = allPicks.length;
  // Always-available engine data — no external API dependencies, so these
  // stats never render in a "data pending" state.
  const returns = allPicks.map(p => p.expected_return).filter(v => v != null).sort((a, b) => a - b);
  const median = returns.length ? returns[Math.floor(returns.length / 2)] : 0;
  const best = allPicks.reduce((b, p) => (p.expected_return || 0) > (b.expected_return || 0) ? p : b, allPicks[0]);
  const topConf = allPicks.reduce((b, p) => (p.confidence || 0) > (b.confidence || 0) ? p : b, allPicks[0]);
  const topUpside = allPicks.reduce((b, p) => {
    const r = p.asymmetric?.p90_ratio || (p.p90 ? p.p90 / p.current_price : 0);
    const br = b.asymmetric?.p90_ratio || (b.p90 ? b.p90 / b.current_price : 0);
    return r > br ? p : b;
  }, allPicks[0]);
  const topUpsideRatio = topUpside?.asymmetric?.p90_ratio
    || (topUpside?.p90 && topUpside?.current_price ? topUpside.p90 / topUpside.current_price : null);

  const fmtPctS = (v) => v == null ? '—' : `${v >= 0 ? '+' : ''}${(v*100).toFixed(1)}%`;
  // Title + sub only. The per-tier distribution viz lives in the donut
  // below; the tab pills show pick counts. No redundant bar here.
  return `<div class="diversification" style="grid-template-columns:1fr;">
    <div>
      <div class="div-title">Today's book: <em style="color:var(--accent-lake);font-style:normal;">${total}</em> picks &middot; median expected <em style="color:#6ee7b7;font-style:normal;">${fmtPctS(median)}</em></div>
      <div class="div-sub">Top conviction: <b style="color:var(--text-hi);">${topConf?.symbol || '—'}</b> at ${topConf?.confidence || 0}/100. Biggest projected upside: <b style="color:var(--text-hi);">${topUpside?.symbol || '—'}</b> &middot; ${topUpsideRatio ? topUpsideRatio.toFixed(1) + 'x P90' : '—'}. Best expected return: <b style="color:var(--text-hi);">${best?.symbol || '—'}</b> &middot; ${fmtPctS(best?.expected_return)}.</div>
    </div>
  </div>`;
}

function renderMethodology(data) {
  return `<section class="methodology">
    <div class="kicker">Methodology</div>
    <h3>How the picks work</h3>
    <p><b>Universe.</b> Nightly scan of the broad US equity universe. Mega-caps and index constituents are filtered &mdash; this list surfaces names you may not already own.</p>
    <p><b>Ranking.</b> Each ticker runs through our proprietary stochastic projection model, then through a scoring function calibrated to the tier. Conservative/Moderate/Aggressive bucket by projected volatility with a positive-return + quality filter. Asymmetric picks are ranked separately under our <a href="/how">volatility-regime-adaptive model</a> to target the upside tail.</p>
    <p><b>Thesis.</b> Each pick carries a point-in-time context line derived from the latest regulatory filings &mdash; growth, margin, free-cash-flow, and balance-sheet direction. When nothing stands out in the filings, the thesis reflects the price dynamics instead, honestly.</p>
    <p><b>Track record.</b> Every pick is logged to an immutable ledger on pick date. Realized return is computed against the entry price every night. Only picks that have had at least 7 days to age count toward the stats above &mdash; we'd rather show a small honest sample than an impressive one you can't trust.</p>
  </section>`;
}

function renderGate(data) {
  const teaserRows = (data.teaser || []).reduce((acc, t) => {
    (acc[t.tier] = acc[t.tier] || []).push(t.symbol); return acc;
  }, {});
  const teaserHtml = Object.entries(teaserRows).map(([tier, syms]) => `
    <div style="margin-bottom:18px;">
      <div style="font:600 11px 'Inter';color:var(--accent-lake);letter-spacing:0.2em;text-transform:uppercase;margin-bottom:10px;">${TIER_COPY[tier]?.label || tier}</div>
      <div class="teaser" style="color:var(--text-hi);font-family:'Crimson Text',serif;font-size:22px;">${syms.join(' · ')}</div>
    </div>`).join('');
  const trackSummary = data.summary ? renderTrackRecord(data.summary) : '';
  return `
    ${trackSummary}
    <div class="gate">
      <div class="eyebrow" style="margin-bottom:14px;">Strategist &middot; $29/mo</div>
      <h2>The full list, the full <em>thesis</em>, and the track record.</h2>
      <p>Strategist unlocks the ranked list across all three risk tiers, with every pick's SEC-filed fundamentals, 1-year P50 target, expected return, and the daily realized-return ledger that makes the engine falsifiable.</p>
      <div class="gate-cta-row">
        <button class="btn" onclick="startCheckout('strategist')">Upgrade &mdash; $29/mo</button>
        <a href="/pricing" class="btn ghost">Compare tiers</a>
      </div>
      <div style="margin-top:40px;padding-top:28px;border-top:1px solid var(--border);">
        <div style="font:600 11px 'Inter';color:var(--text-dim);letter-spacing:0.14em;text-transform:uppercase;margin-bottom:16px;">Today's picks — top 3 per tier</div>
        ${teaserHtml}
      </div>
    </div>`;
}

function riskBand(p10Ratio) {
  if (p10Ratio == null) return { cls: 'meaningful', label: 'Downside uncertain' };
  if (p10Ratio >= 0.70) return { cls: 'limited',     label: `Limited downside — P10 at ${Math.round((1-p10Ratio)*100)}% loss` };
  if (p10Ratio >= 0.40) return { cls: 'meaningful',  label: `Meaningful downside — P10 at ${Math.round((1-p10Ratio)*100)}% loss` };
  if (p10Ratio >= 0.20) return { cls: 'severe',      label: `Severe downside — P10 at ${Math.round((1-p10Ratio)*100)}% loss` };
  return { cls: 'binary', label: `Binary outcome — P10 at ${Math.round((1-p10Ratio)*100)}% loss` };
}

function renderAsymCard(p, rank) {
  const asym = p.asymmetric || {};
  const p90 = asym.p90_ratio; const p10 = asym.p10_ratio; const p50 = asym.p50_ratio;
  const fmtMult = (v) => v == null ? '—' : `${v.toFixed(2)}x`;
  const fmtMove = (v) => v == null ? '—' : `${v >= 1 ? '+' : ''}${Math.round((v-1)*100)}%`;
  const risk = riskBand(p10);
  const sector = p.sector || 'Equity';
  const mcap = p.market_cap ?? p.fundamentals?.market_cap;
  const size = sizeTier(mcap);
  const sizeStr = size ? `${size}-cap${formatMktCap(mcap) ? ` · ${formatMktCap(mcap)}` : ''}` : '';
  const thesis = p.rationale ? `${p.rationale}.` : 'Price dynamics carry the case — no standout SEC metric this quarter.';

  return `<div class="asym-card">
    <div class="asym-card-head">
      <div class="asym-card-ticker">
        <span class="asym-card-sym">${p.symbol}</span>
        <span class="asym-card-name">${p.company_name || ''}</span>
      </div>
      <span class="asym-card-rank">#${rank} &middot; Upside score ${fmtMult(p90)}</span>
    </div>
    <div class="asym-band">
      <div class="bcol">
        <div class="lbl">Downside · P10</div>
        <div class="val down">${fmtMove(p10)}</div>
        <div class="sub">${fmtMult(p10)} of current</div>
      </div>
      <div class="bcol mid">
        <div class="lbl">Median · P50</div>
        <div class="val mid">${fmtMove(p50)}</div>
        <div class="sub">${fmtMult(p50)} of current</div>
      </div>
      <div class="bcol hi">
        <div class="lbl">Upside · P90</div>
        <div class="val up">${fmtMove(p90)}</div>
        <div class="sub">${fmtMult(p90)} of current</div>
      </div>
    </div>
    <div class="asym-thesis">
      <div class="asym-thesis-eyebrow">Thesis</div>
      ${thesis}
    </div>
    <div class="asym-risk ${risk.cls}">${risk.label}</div>
    ${(() => {
      const amt = ALLOC.byPick?.[`${p.symbol}|asymmetric`];
      return amt ? `<div class="asym-alloc">Allocate ${fmtUsd(amt)}</div>` : '';
    })()}
    <div class="asym-foot">
      <span>${sector}${sizeStr ? ' · ' + sizeStr : ''}</span>
      <a href="/app?ticker=${encodeURIComponent(p.symbol)}">Full projection &rarr;</a>
    </div>
  </div>`;
}

// ── Allocation engine ──
// Risk profile = tier-level weights. Within each tier, weight by
// (confidence × 1/vol) so conviction-rich, low-vol picks get more capital.
const RISK_PROFILES = {
  defensive:  { conservative: 0.60, moderate: 0.30, aggressive: 0.10, asymmetric: 0.00, label: 'Defensive',  sub: 'Capital preservation — no asymmetric allocation' },
  balanced:   { conservative: 0.40, moderate: 0.35, aggressive: 0.20, asymmetric: 0.05, label: 'Balanced',   sub: 'Core 75/25 with a small 5% asymmetric allocation' },
  aggressive: { conservative: 0.20, moderate: 0.30, aggressive: 0.30, asymmetric: 0.20, label: 'Aggressive', sub: 'Tilt to volatility and tail — 20% asymmetric' },
};

// In-memory allocation state; read from localStorage on load.
// Legacy 'wild' profile was removed 2026-04-17 — migrate to 'aggressive'.
const _storedProfile = localStorage.getItem('stool_alloc_profile');
const _profile = (_storedProfile === 'wild') ? 'aggressive' : (_storedProfile || 'balanced');
if (_storedProfile === 'wild') localStorage.setItem('stool_alloc_profile', 'aggressive');
let ALLOC = {
  totalUsd: Number(localStorage.getItem('stool_alloc_total') || 10000),
  profile:  _profile,
  byPick:   {},      // sym → dollars
  byTier:   {},      // tier → dollars
};

function tierWeight(pick) {
  // Intra-tier weight: confidence × (1 / vol) with a floor so zero-conf
  // picks still get a non-zero share.
  const c = Math.max(5, pick.confidence ?? 30);
  const v = Math.max(0.05, pick.risk ?? 0.3);
  return c / v;
}

function computeAllocations(picksData) {
  const profile = RISK_PROFILES[ALLOC.profile] || RISK_PROFILES.balanced;
  const dollars = Math.max(0, Number(ALLOC.totalUsd) || 0);
  const byPick = {};
  const byTier = { conservative: 0, moderate: 0, aggressive: 0, asymmetric: 0 };

  const grouped = { conservative: [], moderate: [], aggressive: [] };
  for (const p of picksData.picks) if (grouped[p.tier]) grouped[p.tier].push(p);
  for (const t of ['conservative','moderate','aggressive']) grouped[t] = grouped[t].slice(0, 10);
  const asymPicks = (picksData.asymmetric_picks || []).slice(0, 10);

  const tiers = [
    { name: 'conservative', picks: grouped.conservative },
    { name: 'moderate',     picks: grouped.moderate },
    { name: 'aggressive',   picks: grouped.aggressive },
    { name: 'asymmetric',   picks: asymPicks },
  ];
  for (const t of tiers) {
    const share = profile[t.name] || 0;
    const tierBudget = dollars * share;
    byTier[t.name] = tierBudget;
    if (tierBudget <= 0 || !t.picks.length) continue;
    const weights = t.picks.map(tierWeight);
    const wsum = weights.reduce((a, b) => a + b, 0) || 1;
    t.picks.forEach((p, i) => {
      // Key asymmetric by `symbol + _asym` to avoid collision with same-symbol
      // standard pick. Display reads `${sym}|${tier}`.
      const key = `${p.symbol}|${t.name}`;
      byPick[key] = (byPick[key] || 0) + tierBudget * (weights[i] / wsum);
    });
  }
  ALLOC.byPick = byPick;
  ALLOC.byTier = byTier;
}

function fmtUsd(v) {
  if (v == null || !isFinite(v)) return '—';
  if (v >= 1000) return `$${Math.round(v).toLocaleString()}`;
  return `$${v.toFixed(0)}`;
}

// Tier → palette colour. Shared by donut, composition rows, tab dots.
const TIER_PALETTE = {
  conservative: '#6ee7b7',
  moderate:     '#5FAAC5',
  aggressive:   '#f5d58f',
  asymmetric:   '#d2ddea',
};

// Master donut: one arc per individual ticker across ALL risk tiers.
// Arcs are grouped contiguously by tier (conservative → moderate →
// aggressive → asymmetric) so tier colors form clean bands, and the
// intra-tier stroke-opacity variation shows which ticker inside a band
// carries the most weight. r=15.9155 makes the circumference exactly
// 100, so each arc's stroke-dasharray segment in percent maps 1:1 to
// its share of the total portfolio.
function renderMasterDonut(allTickers, total, center) {
  const GAP = 0.25;  // tiny gap between adjacent slices for visual separation
  let offset = 0;
  const safeTotal = Math.max(1e-6, total);

  // Precompute per-tier max so opacity scales inside each tier.
  const tierMax = {};
  for (const tk of allTickers) {
    if (tk.amt > (tierMax[tk.tier] || 0)) tierMax[tk.tier] = tk.amt;
  }

  const arcs = allTickers.map(tk => {
    const pct = (tk.amt / safeTotal) * 100;
    if (pct <= 0) return '';
    const visiblePct = Math.max(0.05, pct - GAP);
    const dashArr = `${visiblePct.toFixed(3)} ${(100 - visiblePct).toFixed(3)}`;
    const dashOff = (-offset).toFixed(3);
    const tMax = tierMax[tk.tier] || tk.amt;
    const op = (0.55 + 0.45 * (tk.amt / tMax)).toFixed(2);
    // Data attrs feed the custom hover tooltip. No <title> — native SVG
    // tooltips are slow and OS-styled, the custom one is instant.
    const arc = `<circle cx="21" cy="21" r="15.9155" fill="transparent"
      stroke="${TIER_PALETTE[tk.tier]}" stroke-width="7"
      stroke-dasharray="${dashArr}" stroke-dashoffset="${dashOff}"
      stroke-opacity="${op}"
      transform="rotate(-90 21 21)"
      data-sym="${tk.sym}" data-tier="${tk.tier}"
      data-amt="${tk.amt.toFixed(2)}"
      data-pct-total="${pct.toFixed(2)}"></circle>`;
    offset += pct;
    return arc;
  }).join('');

  // Wrap center text in a width-constrained inner div so long profile
  // captions never overflow behind the donut ring. The .dt-inner at 58%
  // sits entirely inside the donut's negative space.
  // Center total scales down when the formatted string gets long AND
  // when the viewport is small. "$10,000,000" at 46pt overflows the
  // inner 58%-wide zone on desktop; same string at 40pt overflows the
  // 240px mobile donut even harder. Combine length + viewport scaling.
  const _totalStr = (center && center.total) || '';
  const _totalLen = _totalStr.replace(/[^\d$.,]/g, '').length;
  const _vw = (typeof window !== 'undefined' && window.innerWidth) || 1200;
  const _baseMax = _vw < 540 ? 32 : _vw < 920 ? 38 : 46;
  const _scale = _totalLen > 11 ? 0.55
               : _totalLen > 9  ? 0.68
               : _totalLen > 7  ? 0.82
               : 1.00;
  const _totalFontPx = Math.max(18, Math.round(_baseMax * _scale));
  const centerHtml = center
    ? `<div class="dt-center">
         <div class="dt-inner">
           <div class="dt-total" style="font-size:${_totalFontPx}px;">${center.total}</div>
           <div class="dt-sub">${center.label}</div>
           ${center.caption ? `<div class="dt-caption">${center.caption}</div>` : ''}
         </div>
       </div>`
    : '';

  return `<div class="alloc-donut master">
    <svg viewBox="0 0 42 42" aria-label="Portfolio composition by ticker">
      <circle cx="21" cy="21" r="15.9155" fill="transparent"
        stroke="rgba(155,161,185,0.08)" stroke-width="7"/>
      ${arcs}
    </svg>
    ${centerHtml}
  </div>`;
}

// Mini pie for a single tier — shows the intra-tier distribution across
// that tier's ~10 tickers. Same r=15.9155 trick; thicker stroke because
// the pie is small and each slice needs physical weight to read at 140px.
function renderTierPie(tier, entries) {
  if (!entries.length) return '';
  const tierTotal = entries.reduce((s, e) => s + e.amt, 0);
  if (tierTotal <= 0) return '';
  const maxAmt = Math.max(...entries.map(e => e.amt), 1e-6);
  const GAP = 0.5;
  let offset = 0;
  const arcs = entries.map(e => {
    const pct = (e.amt / tierTotal) * 100;
    if (pct <= 0) return '';
    const visiblePct = Math.max(0.1, pct - GAP);
    const dashArr = `${visiblePct.toFixed(3)} ${(100 - visiblePct).toFixed(3)}`;
    const dashOff = (-offset).toFixed(3);
    const op = (0.5 + 0.5 * (e.amt / maxAmt)).toFixed(2);
    const arc = `<circle cx="21" cy="21" r="15.9155" fill="transparent"
      stroke="${TIER_PALETTE[tier]}" stroke-width="9"
      stroke-dasharray="${dashArr}" stroke-dashoffset="${dashOff}"
      stroke-opacity="${op}"
      transform="rotate(-90 21 21)"
      data-sym="${e.sym}" data-tier="${tier}"
      data-amt="${e.amt.toFixed(2)}"
      data-pct-tier="${pct.toFixed(2)}"></circle>`;
    offset += pct;
    return arc;
  }).join('');
  return `<svg class="mini-pie" viewBox="0 0 42 42" aria-label="${TIER_LABELS[tier]} allocation">
    <circle cx="21" cy="21" r="15.9155" fill="transparent"
      stroke="rgba(155,161,185,0.06)" stroke-width="9"/>
    ${arcs}
  </svg>`;
}

const TIER_LABELS = {
  conservative: 'Conservative',
  moderate: 'Moderate',
  aggressive: 'Aggressive',
  asymmetric: 'Asymmetric',
};

function renderAllocWidget(picksData) {
  const profile = RISK_PROFILES[ALLOC.profile] || RISK_PROFILES.balanced;
  const total = ALLOC.totalUsd || 0;
  const tiers = ['conservative','moderate','aggressive','asymmetric'];

  const byTier = { conservative: [], moderate: [], aggressive: [] };
  for (const p of picksData.picks || []) {
    if (byTier[p.tier] && byTier[p.tier].length < 10) byTier[p.tier].push(p);
  }
  const asymPicks = (picksData.asymmetric_picks || []).slice(0, 10);
  const tierPicks = {
    conservative: byTier.conservative,
    moderate:     byTier.moderate,
    aggressive:   byTier.aggressive,
    asymmetric:   asymPicks,
  };

  const SUBS = {
    conservative: 'Low vol · quality',
    moderate:     'Core · balanced',
    aggressive:   'High vol · tactical',
    asymmetric:   'Tail upside · speculative',
  };

  // Assemble every per-ticker allocation into a single flat list, grouped
  // by tier in canonical order (conservative → asymmetric) and sorted by
  // $ descending within each tier. The master donut reads this directly:
  // one arc per ticker, tier colors forming contiguous bands.
  const allTickers = [];
  const tierEntries = {};
  for (const t of tiers) {
    const share = profile[t] || 0;
    const dollars = total * share;
    const picks = tierPicks[t] || [];
    const entries = picks.map(p => ({
      sym: p.symbol, tier: t,
      amt: ALLOC.byPick?.[`${p.symbol}|${t}`] || 0,
    })).sort((a, b) => b.amt - a.amt);
    tierEntries[t] = { share, dollars, entries, pickN: picks.length };
    for (const e of entries) if (e.amt > 0) allTickers.push(e);
  }

  const opts = Object.entries(RISK_PROFILES).map(([key, cfg]) =>
    `<option value="${key}" ${ALLOC.profile === key ? 'selected' : ''}>${cfg.label}</option>`).join('');

  // Center caption: the PROFILE name gets a "profile" qualifier so it
  // can't be mistaken for the aggressive/conservative TIER of the same
  // name. Sub-copy kept short — anything longer would bleed outside the
  // donut's inner clear-zone regardless of the width constraint.
  const masterDonutHtml = renderMasterDonut(allTickers, total, {
    total: fmtUsd(total),
    label: `${profile.label} profile`,
    caption: `${allTickers.length} names`,
  });

  // Per-tier cards: mini pie + header with tier name/%/$ + the top tickers
  // listed below the pie. Every tier gets its own chart so the user can
  // actually see intra-tier distribution, not just tier-level totals.
  const tierCards = tiers.map(t => {
    const { share, dollars, entries, pickN } = tierEntries[t];
    const emptyCls = share === 0 ? ' tp-empty' : '';

    let bodyHtml;
    if (share === 0) {
      bodyHtml = `<div class="tp-msg">Not allocated in the ${profile.label} profile.</div>`;
    } else if (!pickN) {
      bodyHtml = `<div class="tp-msg">No picks in today's scan.</div>`;
    } else {
      const pieHtml = renderTierPie(t, entries);
      // Legend: every ticker, rows sorted by $. Compact enough that all
      // 10 fit without overflow. Dollar column is tabular-num aligned.
      const legendRows = entries.map(e => {
        const pctTier = dollars > 0 ? (e.amt / dollars) * 100 : 0;
        return `<div class="tp-legend-row">
          <span class="tp-lg-sym">${e.sym}</span>
          <span class="tp-lg-bar" style="--w:${pctTier.toFixed(1)}%;--c:${TIER_PALETTE[t]};"></span>
          <span class="tp-lg-amt">${fmtUsd(e.amt)}</span>
        </div>`;
      }).join('');
      bodyHtml = `<div class="tp-pie-wrap">${pieHtml}</div>
                  <div class="tp-legend">${legendRows}</div>`;
    }

    return `<div class="tp-card${emptyCls}">
      <div class="tp-head">
        <div class="tp-head-left">
          <span class="tp-dot" style="background:${TIER_PALETTE[t]};"></span>
          <div>
            <div class="tp-name">${TIER_LABELS[t]}</div>
            <div class="tp-sub">${SUBS[t]}</div>
          </div>
        </div>
        <div class="tp-head-right">
          <div class="tp-pct">${Math.round(share * 100)}%</div>
          <div class="tp-dollar">${share > 0 ? fmtUsd(dollars) : '$0'}</div>
        </div>
      </div>
      ${bodyHtml}
    </div>`;
  }).join('');

  return `<div class="alloc-widget">
    <div class="alloc-top">
      ${masterDonutHtml}
      <div class="alloc-intro">
        <div class="aw-title">Allocation &middot; build your book</div>
        <div class="aw-sub"><b>${profile.label}</b>: ${profile.sub}. Every arc above is one ticker, sized by its dollar allocation and colored by risk tier. Per-tier detail below.</div>
      </div>
      <div class="alloc-controls">
        <div class="alloc-control">
          <label>Portfolio size</label>
          <input type="number" id="allocTotal" min="100" step="500" value="${total}" />
        </div>
        <div class="alloc-control">
          <label>Risk profile</label>
          <select id="allocProfile">${opts}</select>
        </div>
      </div>
    </div>
    <div class="tier-pies-grid">${tierCards}</div>
  </div>`;
}

function wireAllocControls(picksData, trackData) {
  const total = document.getElementById('allocTotal');
  const profile = document.getElementById('allocProfile');
  const onChange = () => {
    ALLOC.totalUsd = Math.max(0, Number(total.value) || 0);
    ALLOC.profile = profile.value;
    localStorage.setItem('stool_alloc_total', String(ALLOC.totalUsd));
    localStorage.setItem('stool_alloc_profile', ALLOC.profile);
    computeAllocations(picksData);
    document.getElementById('content').innerHTML = renderPage(picksData, trackData);
    wireAllocControls(picksData, trackData);
    wireChartTooltips();
  };
  total?.addEventListener('change', onChange);
  total?.addEventListener('input', onChange);
  profile?.addEventListener('change', onChange);
}

// Custom hover tooltip for pie/donut slices. Shows instantly on hover
// with the ticker, tier, $ amount, % of portfolio, % of tier. Replaces
// browser-native <title> which is slow and OS-styled.
function wireChartTooltips() {
  let tip = document.getElementById('chartTip');
  if (!tip) {
    tip = document.createElement('div');
    tip.id = 'chartTip';
    tip.className = 'chart-tooltip';
    tip.innerHTML = `
      <div class="ct-head">
        <span class="ct-dot"></span>
        <span class="ct-sym"></span>
        <span class="ct-tier"></span>
      </div>
      <div class="ct-rows">
        <div class="ct-row"><span class="ct-label">Amount</span><span class="ct-value big ct-amt"></span></div>
        <div class="ct-row"><span class="ct-label">% of portfolio</span><span class="ct-value ct-pct-total"></span></div>
        <div class="ct-row"><span class="ct-label">% of tier</span><span class="ct-value ct-pct-tier"></span></div>
      </div>`;
    document.body.appendChild(tip);
  }

  const tierTotals = ALLOC.byTier || {};
  const portfolioTotal = ALLOC.totalUsd || 0;

  const show = (circle, evt) => {
    const sym   = circle.getAttribute('data-sym');
    const tier  = circle.getAttribute('data-tier');
    const amt   = parseFloat(circle.getAttribute('data-amt') || '0');
    const tierTotal = tierTotals[tier] || 0;
    const pctTotal = portfolioTotal > 0 ? (amt / portfolioTotal) * 100 : 0;
    const pctTier  = tierTotal     > 0 ? (amt / tierTotal)     * 100 : 0;
    const tierColor = TIER_PALETTE[tier] || '#5faac5';

    tip.querySelector('.ct-dot').style.background = tierColor;
    tip.querySelector('.ct-sym').textContent = sym;
    tip.querySelector('.ct-tier').textContent = TIER_LABELS[tier] || tier;
    tip.querySelector('.ct-amt').textContent = fmtUsd(amt);
    tip.querySelector('.ct-pct-total').textContent = pctTotal.toFixed(2) + '%';
    tip.querySelector('.ct-pct-tier').textContent  = pctTier.toFixed(1)  + '%';
    circle.classList.add('hover');
    tip.classList.add('visible');
    positionTip(evt);
  };
  const hide = (circle) => { circle?.classList.remove('hover'); tip.classList.remove('visible'); };
  const positionTip = (evt) => {
    const w = tip.offsetWidth || 220;
    const h = tip.offsetHeight || 140;
    const x = Math.min(evt.clientX + 16, window.innerWidth - w - 12);
    const y = Math.min(evt.clientY + 16, window.innerHeight - h - 12);
    tip.style.left = x + 'px';
    tip.style.top  = y + 'px';
  };

  document.querySelectorAll('circle[data-sym]').forEach(c => {
    c.addEventListener('mouseenter', e => show(c, e));
    c.addEventListener('mousemove',  e => positionTip(e));
    c.addEventListener('mouseleave', () => hide(c));
  });
}

// Tier section that opens a tab — splits picks into first-5 (always
// visible) and overflow (data-overflow="1", hidden until "Show remaining"
// is clicked). Matches the existing pick-grid layout.
function renderTabTierBody(tier, picks) {
  if (!picks.length) {
    return `<div class="loading">No ${tier} picks in today's scan.</div>`;
  }
  const visible = picks.slice(0, 5);
  const overflow = picks.slice(5);
  const sectorStrip = renderSectorStrip(picks);
  const visibleCards = visible.map(renderPickCard).join('');
  const overflowCards = overflow.map(p => {
    // Mark overflow pick cards with data-overflow so CSS can hide them by
    // default. Same card shape as the visible ones.
    return renderPickCard(p).replace(
      '<div class="pick-card">',
      '<div class="pick-card" data-overflow="1">'
    );
  }).join('');
  const showAll = overflow.length
    ? `<div class="show-all-row">
         <button class="show-all-btn" type="button" onclick="toggleShowAll('${tier}')">
           Show remaining ${overflow.length} pick${overflow.length === 1 ? '' : 's'} &rarr;
         </button>
       </div>`
    : '';
  return `<section class="tier-section" data-tier="${tier}">
    <div class="tier-head">
      <div style="min-width:0;flex:1;">
        <div class="tier-name">${TIER_COPY[tier].label} <span class="tier-count">${picks.length} ${picks.length === 1 ? 'pick' : 'picks'}</span></div>
        ${sectorStrip}
      </div>
      <div class="tier-blurb">${TIER_COPY[tier].blurb}</div>
    </div>
    <div class="pick-grid">${visibleCards}${overflowCards}</div>
    ${showAll}
  </section>`;
}

// Same idea for the asymmetric tab — 5 visible cards, rest hidden until
// the toggle is flipped. Uses the existing asym card design.
function renderTabAsymBody(picks) {
  if (!picks || !picks.length) {
    return `<div class="loading">No asymmetric picks in today's scan.</div>`;
  }
  const visible = picks.slice(0, 5);
  const overflow = picks.slice(5);
  const avgP90 = picks.reduce((s, p) => s + (p.asymmetric?.p90_ratio || 0), 0) / picks.length;
  const avgP10 = picks.reduce((s, p) => s + (p.asymmetric?.p10_ratio || 0), 0) / picks.length;
  const visibleCards = visible.map((p, i) => renderAsymCard(p, i + 1)).join('');
  const overflowCards = overflow.map((p, i) => {
    return renderAsymCard(p, i + 6).replace(
      '<div class="asym-card">',
      '<div class="asym-card" data-overflow="1">'
    );
  }).join('');
  const showAll = overflow.length
    ? `<div class="show-all-row">
         <button class="show-all-btn" type="button" onclick="toggleShowAll('asymmetric')">
           Show remaining ${overflow.length} pick${overflow.length === 1 ? '' : 's'} &rarr;
         </button>
       </div>`
    : '';
  return `<section class="asym-section">
    <div class="asym-head">
      <div>
        <div style="font:600 11px 'Inter';color:var(--accent-lake);letter-spacing:0.24em;text-transform:uppercase;margin-bottom:14px;">Asymmetric Upside</div>
        <h2 class="asym-tier">Names the engine thinks could <em>multiply</em>.</h2>
        <p class="asym-blurb">Ranked by our <b>self-training asymmetric-upside model</b> &mdash; a scoring function that <b>retrains every night</b> on the most recent realized-return data, picking whichever signal combination has been identifying 100%+ movers out-of-sample in the current regime. Filtered for real liquidity so every name is actually investable. Every pick carries its own <b>honest P10 downside</b>. High-variance &mdash; <b>size each name small</b>.</p>
      </div>
      <div class="asym-stats">
        <div><div class="s-val">${avgP90.toFixed(1)}x</div><div class="s-label">Avg P90</div></div>
        <div><div class="s-val">${avgP10.toFixed(2)}x</div><div class="s-label">Avg P10</div></div>
        <div><div class="s-val">${picks.length}</div><div class="s-label">Names</div></div>
      </div>
    </div>
    <div class="asym-grid">${visibleCards}${overflowCards}</div>
    ${showAll}
  </section>`;
}

// Pill tab row — shows tier name, pick count, allocated dollars. Every
// tab itself is numeric so the nav earns its space.
function renderPicksTabs(picksData) {
  const tiers = [
    { key: 'conservative', label: 'Conservative' },
    { key: 'moderate',     label: 'Moderate' },
    { key: 'aggressive',   label: 'Aggressive' },
    { key: 'asymmetric',   label: 'Asymmetric' },
  ];
  const grouped = {};
  for (const p of picksData.picks || []) (grouped[p.tier] = grouped[p.tier] || []).push(p);
  const asymPicks = picksData.asymmetric_picks || [];

  const stored = localStorage.getItem('stool_picks_tab');
  const firstAvailable = tiers.find(t => {
    if (t.key === 'asymmetric') return asymPicks.length > 0;
    return (grouped[t.key] || []).length > 0;
  })?.key || 'conservative';
  const activeKey = tiers.some(t => t.key === stored) ? stored : firstAvailable;

  const btns = tiers.map(t => {
    const count = t.key === 'asymmetric'
      ? Math.min(10, asymPicks.length)
      : Math.min(10, (grouped[t.key] || []).length);
    const budget = ALLOC.byTier?.[t.key] || 0;
    const active = t.key === activeKey ? ' active' : '';
    const dollarStr = budget > 0 ? fmtUsd(budget) : '$0';
    return `<button class="pt-btn${active}" type="button"
      data-tab-key="${t.key}" onclick="switchTab('${t.key}')">
      <div class="pt-head">
        <span class="pt-dot" style="background:${TIER_PALETTE[t.key]};"></span>
        <span class="pt-name">${t.label}</span>
      </div>
      <div class="pt-meta">${count} pick${count === 1 ? '' : 's'} &middot; ${dollarStr}</div>
    </button>`;
  }).join('');
  return { html: `<nav class="picks-tabs" role="tablist">${btns}</nav>`, activeKey };
}

function renderPage(picksData, trackData, portfolioData) {
  const grouped = {};
  for (const p of picksData.picks) (grouped[p.tier] = grouped[p.tier] || []).push(p);
  const tiered = ['conservative','moderate','aggressive']
    .flatMap(t => (grouped[t] || []).slice(0, 10));
  computeAllocations(picksData);

  // Only show live Track record if we have at least one matured tier — the
  // placeholder-only version was clutter. Backtest evidence lives on the
  // dedicated /track-record page, so we just link there compactly.
  const hasLiveTrack = trackData?.summary &&
    Object.values(trackData.summary).some(s => s && s.n);
  const trackBlock = hasLiveTrack
    ? renderTrackRecord(trackData.summary)
    : `<div class="track-link-row">
         <div class="tl-copy">
           <span class="tl-eyebrow">Evidence</span>
           <span class="tl-title">Live tracking starts once picks mature (7d+).</span>
         </div>
         <a class="tl-cta" href="/track-record">See full backtest &rarr;</a>
       </div>`;

  const tabs = renderPicksTabs(picksData);
  const tierPanels = ['conservative','moderate','aggressive'].map(t => {
    const picks = (grouped[t] || []).slice(0, 10);
    const activeCls = t === tabs.activeKey ? ' active' : '';
    return `<div class="tab-panel${activeCls}" data-panel-key="${t}">${renderTabTierBody(t, picks)}</div>`;
  }).join('');
  const asymActive = tabs.activeKey === 'asymmetric' ? ' active' : '';
  const asymPanel = `<div class="tab-panel${asymActive}" data-panel-key="asymmetric">${renderTabAsymBody(picksData.asymmetric_picks || [])}</div>`;

  const portfolioBlock = portfolioData ? renderPortfolio(portfolioData) : '';

  return `${portfolioBlock}
          <section class="hero-block">
            ${renderPortfolioSignal(tiered, picksData)}
            ${renderAllocWidget(picksData)}
          </section>
          ${trackBlock}
          ${tabs.html}
          ${tierPanels}
          ${asymPanel}
          ${renderMethodology(picksData)}`;
}

// Auto-refresh handle for the live portfolio panel. Cleared on tab hide
// to stop hammering Alpaca when the user isn't looking.
let _pfRefreshTimer = null;
async function refreshPortfolioOnly() {
  try {
    const res = await fetch('/api/portfolio', { headers: await authHeaders() });
    if (!res.ok) return;
    const data = await res.json();
    const node = document.getElementById('pf-panel');
    if (!node) return;
    const html = renderPortfolio(data);
    if (html) {
      node.outerHTML = html;
      // outerHTML replaces the slice circles — re-bind tooltips on the
      // fresh nodes. Without this, the donut is silent on hover after the
      // first refresh.
      wirePortfolioTooltips();
    }
  } catch (_) { /* swallow — next tick will retry */ }
}

// Track which sleeve groups the user has expanded so the 30s refresh
// re-opens them after replacing the panel. Delegated handler so we don't
// need to re-bind on every refresh.
document.addEventListener('toggle', (e) => {
  const det = e.target;
  if (!det || !det.classList || !det.classList.contains('pf-pdl-group')) return;
  const sleeve = det.dataset.sleeve;
  if (!sleeve) return;
  const set = (window.__pfOpenSleeves = window.__pfOpenSleeves || new Set());
  if (det.open) set.add(sleeve);
  else set.delete(sleeve);
}, true);

// Hover tooltip for portfolio donut slices. Distinct from wireChartTooltips
// because the data shape differs: portfolio slices carry sleeve + P&L
// (no tier), and we want the popup to surface live unrealized P&L next to
// the dollar amount. Same .chart-tooltip element styling for visual unity.
function wirePortfolioTooltips() {
  let tip = document.getElementById('pfTip');
  if (!tip) {
    tip = document.createElement('div');
    tip.id = 'pfTip';
    tip.className = 'chart-tooltip';
    tip.innerHTML = `
      <div class="ct-head">
        <span class="ct-dot"></span>
        <span class="ct-sym"></span>
        <span class="ct-badges">
          <span class="ct-tier ct-sleeve-lbl"></span>
          <span class="ct-tier ct-tier-lbl"></span>
        </span>
      </div>
      <div class="ct-pl-hero"><span class="ct-pl-arrow"></span><span class="ct-pl-num"></span></div>
      <div class="ct-rows">
        <div class="ct-row"><span class="ct-label">Market value</span><span class="ct-value ct-amt"></span></div>
        <div class="ct-row"><span class="ct-label">% of book</span><span class="ct-value ct-pct"></span></div>
      </div>`;
    document.body.appendChild(tip);
  }
  const SLEEVE_LABEL = { momentum: 'MOMENTUM', swing: 'SWING', daytrade: 'DAY TRADE', scalper: 'SCALPER', unattributed: 'MANUAL / LEGACY' };
  const TIER_LABEL = { conservative: 'CONSERVATIVE', moderate: 'MODERATE', aggressive: 'AGGRESSIVE', asymmetric: 'ASYMMETRIC' };
  const TIER_VAR = {
    conservative: 'var(--tier-conservative)',
    moderate: 'var(--tier-moderate)',
    aggressive: 'var(--tier-aggressive)',
    asymmetric: 'var(--tier-asymmetric)',
  };

  const positionTip = (evt) => {
    const w = tip.offsetWidth || 220;
    const h = tip.offsetHeight || 140;
    const x = Math.min(evt.clientX + 16, window.innerWidth - w - 12);
    const y = Math.min(evt.clientY + 16, window.innerHeight - h - 12);
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  };
  const show = (circle, evt) => {
    const sym = circle.getAttribute('data-pf-sym');
    const sleeve = circle.getAttribute('data-pf-sleeve') || 'unattributed';
    const tier = circle.getAttribute('data-pf-tier') || '';
    const amt = parseFloat(circle.getAttribute('data-pf-amt') || '0');
    const pct = parseFloat(circle.getAttribute('data-pf-pct') || '0');
    const pl = parseFloat(circle.getAttribute('data-pf-pl') || '0');
    const plpc = parseFloat(circle.getAttribute('data-pf-plpc') || '0');
    const color = PF_SLEEVE_COLOR[sleeve] || PF_SLEEVE_COLOR.unattributed;
    const sign = (n) => (n > 0 ? '+' : (n < 0 ? '−' : ''));
    const plCls = pl > 0 ? 'pos' : (pl < 0 ? 'neg' : '');

    tip.querySelector('.ct-dot').style.background = color;
    tip.querySelector('.ct-sym').textContent = sym;
    // Two badges, color-coded to match the donut rings: sleeve = inner
    // (strategy), tier = outer (risk tier). Tier badge hides when no
    // real /picks tier was attributed (avoids "SWING · SWING").
    const sleeveTxt = SLEEVE_LABEL[sleeve] || sleeve.toUpperCase();
    const sleeveLbl = tip.querySelector('.ct-sleeve-lbl');
    sleeveLbl.textContent = sleeveTxt;
    sleeveLbl.style.color = color;
    const tierLbl = tip.querySelector('.ct-tier-lbl');
    if (TIER_LABEL[tier]) {
      tierLbl.textContent = TIER_LABEL[tier];
      tierLbl.style.color = TIER_VAR[tier] || 'var(--text-dim)';
      tierLbl.style.display = '';
    } else {
      tierLbl.style.display = 'none';
    }
    tip.querySelector('.ct-amt').textContent = fmtUSD(amt, { digits: 0 });
    tip.querySelector('.ct-pct').textContent = pct.toFixed(2) + '%';
    // P&L hero: green/red arrow + the dollar+percent number, large.
    // This is the first thing the eye reads in the card — the model's
    // verdict on this position made unmissable.
    const heroEl = tip.querySelector('.ct-pl-hero');
    heroEl.className = 'ct-pl-hero ' + plCls;
    const arrowGlyph = pl > 0.5 ? '▲' : (pl < -0.5 ? '▼' : '◆');
    tip.querySelector('.ct-pl-arrow').textContent = arrowGlyph;
    tip.querySelector('.ct-pl-num').textContent = `${sign(pl)}${fmtUSD(Math.abs(pl), { digits: 0 }).replace(/^[-]/, '')}  ${sign(plpc)}${Math.abs(plpc).toFixed(2)}%`;
    // Highlight BOTH rings for this symbol (outer + inner share the
    // same data-pf-sym), so a hover lights up the whole position.
    document.querySelectorAll(`circle[data-pf-sym="${CSS.escape(sym)}"]`)
      .forEach(c => c.classList.add('hover'));
    tip.classList.add('visible');
    positionTip(evt);
  };
  const hide = (sym) => {
    if (sym) {
      document.querySelectorAll(`circle[data-pf-sym="${CSS.escape(sym)}"]`)
        .forEach(c => c.classList.remove('hover'));
    } else {
      document.querySelectorAll('circle[data-pf-sym].hover')
        .forEach(c => c.classList.remove('hover'));
    }
    tip.classList.remove('visible');
  };

  document.querySelectorAll('circle[data-pf-sym]').forEach(c => {
    const sym = c.getAttribute('data-pf-sym');
    c.addEventListener('mouseenter', e => show(c, e));
    c.addEventListener('mousemove', e => positionTip(e));
    c.addEventListener('mouseleave', () => hide(sym));
    // Touch — single-tap to peek (Mobile QC: never absolute-position
    // interactive UI in collapsibles, but the tooltip is body-attached
    // and z-9999 so it's safe).
    c.addEventListener('touchstart', e => show(c, e.touches[0]), { passive: true });
  });

  // Reciprocal: hovering a position row in the table highlights the
  // matching slices (both rings) and shows the same tooltip. Uses the
  // first matching circle as the data source — both rings carry
  // identical data attributes for the same symbol.
  document.querySelectorAll('.pf-pdl-line').forEach(row => {
    const sym = row.getAttribute('data-pf-row-sym');
    if (!sym) return;
    const refCircle = document.querySelector(`circle[data-pf-sym="${CSS.escape(sym)}"]`);
    if (!refCircle) return;
    row.addEventListener('mouseenter', e => show(refCircle, e));
    row.addEventListener('mousemove', e => positionTip(e));
    row.addEventListener('mouseleave', () => hide(sym));
  });
}
function startPortfolioRefresh() {
  if (_pfRefreshTimer) return;
  // 15s polls — was 30s but the live activity ticker reads stale at half
  // a minute. Trade fills happen at 5min cron intervals (scalper) and
  // 30min (trader), so 15s catches new events same-minute and the ticker
  // animation reads fresh. Bandwidth cost is ~2KB per pull.
  _pfRefreshTimer = setInterval(() => {
    if (document.visibilityState === 'visible') refreshPortfolioOnly();
  }, 15000);
  // Also tick the next-cron countdown every second, independent of API
  // polling. The countdown DOM is small enough to swap as a string.
  if (!window.__pfCountdownTimer) {
    window.__pfCountdownTimer = setInterval(() => {
      const el = document.querySelector('.pf-live-status');
      if (!el) return;
      const next = pfNextCronFire();
      const bits = el.querySelectorAll('.pf-live-bit');
      // Second .pf-live-bit is "next X in MM:SS" — rebuild it in place.
      if (bits.length >= 2) {
        bits[1].innerHTML = `next <b>${next.label}</b> in ${next.countdown}`;
      }
    }, 1000);
  }
}
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') refreshPortfolioOnly();
});

async function load() {
  const content = document.getElementById('content');
  const scanMeta = document.getElementById('scanMeta');

  const [picksRes, trackRes, pfRes] = await Promise.all([
    fetch('/api/picks', { headers: await authHeaders() }),
    fetch('/api/track-record?lookback_days=90', { headers: await authHeaders() }),
    fetch('/api/portfolio', { headers: await authHeaders() }).catch(() => null),
  ]);
  const picksData = await picksRes.json().catch(() => null);
  const trackData = await trackRes.json().catch(() => null);
  // Portfolio is best-effort: 503 (no creds), 502 (Alpaca down), or
  // strategist-gated all return a JSON body we can pass through to
  // renderPortfolio, which renders nothing on error.
  let portfolioData = null;
  if (pfRes && pfRes.ok) portfolioData = await pfRes.json().catch(() => null);

  if (picksRes.status === 402 && picksData?.error === 'strategist_required') {
    scanMeta.innerHTML = `<span class="meta-item"><span class="dot waiting"></span><span>Scan last updated ${picksData.scan_age_hours != null ? picksData.scan_age_hours.toFixed(1) + 'h ago' : 'recently'}</span></span>
      <span class="meta-item">Upgrade to see the full list</span>`;
    // Render the live portfolio panel ABOVE the gate as proof-of-life
    // for anonymous / non-strategist visitors. The /api/portfolio
    // response in this case is gated to a 3-position teaser.
    const portfolioBlock = portfolioData ? renderPortfolio(portfolioData) : '';
    content.innerHTML = portfolioBlock + renderGate({ ...picksData, summary: trackData?.summary });
    if (portfolioData && !portfolioData.error) startPortfolioRefresh();
    return;
  }
  if (picksRes.status === 404) {
    scanMeta.innerHTML = '';
    content.innerHTML = `<div class="gate"><h2>Overnight scan hasn't run yet.</h2><p>Picks refresh after the daily cron at 10:00 UTC (6am ET). Check back then — or <a href="/app" style="color:var(--accent-lake);">try an individual projection</a> in the meantime.</p></div>`;
    return;
  }
  if (!picksRes.ok || !picksData) {
    scanMeta.innerHTML = '';
    content.innerHTML = `<div class="gate"><h2>Couldn't load picks.</h2><p>Refresh in a moment.</p></div>`;
    return;
  }

  const scanAge = picksData.scan_age_hours != null ? `${picksData.scan_age_hours.toFixed(1)}h ago` : 'recent';
  scanMeta.innerHTML = `
    <span class="meta-item"><span class="dot"></span><span>Last scan ${scanAge}</span></span>
    <span class="meta-item">${picksData.count} picks across 3 tiers</span>
    <span class="meta-item">1-year horizon · nightly refresh</span>`;

  content.innerHTML = renderPage(picksData, trackData, portfolioData);
  wireAllocControls(picksData, trackData);
  wireChartTooltips();
  wirePortfolioTooltips();
  if (portfolioData && !portfolioData.error) startPortfolioRefresh();
}

// Hook the picks-specific load() onto Clerk's session lifecycle. nav.js
// auto-initialises on its own and exposes STNav.ready() as a single
// promise that resolves once Clerk has loaded. We await that rather than
// calling Clerk.load() ourselves — calling it twice with different
// appearance configs has caused dark-modal regressions in the past.
(function () {
  const wait = () => {
    if (!window.STNav?.ready) return setTimeout(wait, 40);
    // Idempotent — nav.js has already auto-initialised. We're just
    // setting the explicit redirect so a sign-in from /picks returns here.
    window.STNav.init({ signInRedirect: '/picks' });
    window.STNav.ready().then((clerk) => {
      load();
      try { clerk?.addListener?.(load); } catch (_) {}
    });
  };
  wait();
})();
