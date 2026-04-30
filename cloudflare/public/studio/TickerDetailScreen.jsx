// Ticker detail screen — large symbol, factor breakdown, thesis, key data, history, sizing.

const { useMemo: useMemoTD } = React;
const Dt = window.STOOL;
const { Icon: IconTD, ScoreCell: ScoreCellTD, SignalTag: SignalTagTD, Compass: CompassTD, richText: richTextTD } = window.STOOL_UI;

function FactorBreakdown({ factors, median }) {
  const items = ['momentum', 'fundamentals', 'risk', 'quality', 'value'];
  const labels = { momentum: 'Momentum', fundamentals: 'Fundamentals', risk: 'Risk', quality: 'Quality', value: 'Value' };
  return (
    <div className="panel">
      <div className="panel-header"><div className="panel-title">Factor breakdown</div><div className="panel-meta">vs sector median</div></div>
      <div className="factor-list">
        {items.map(k => {
          const v = factors[k];
          const med = median[k];
          const cls = v >= 75 ? 'strong' : v >= 55 ? '' : 'weak';
          return (
            <div key={k} className="factor-row">
              <div className="factor-head">
                <span className="factor-name">{labels[k]}</span>
                <span className="factor-score">{v}</span>
              </div>
              <div className="factor-bar">
                <div className={`factor-fill ${cls}`} style={{ width: `${v}%` }} />
                <div className="factor-median" style={{ left: `calc(${med}% - 1px)` }} title={`Sector median ${med}`} />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function PriceMiniChart({ trend }) {
  // synthesize a simple line based on trend direction
  const w = 600, h = 200, pad = 20;
  const points = [];
  let v = 50;
  for (let i = 0; i < 60; i++) {
    v += (Math.sin(i / 4) + (trend === 'up' ? 0.6 : -0.4)) * 1.6 + (Math.random() - 0.5) * 1.2;
    points.push(v);
  }
  const min = Math.min(...points), max = Math.max(...points);
  const xs = (i) => pad + (i / (points.length - 1)) * (w - pad * 2);
  const ys = (p) => pad + (1 - (p - min) / (max - min)) * (h - pad * 2);
  const lineD = points.map((p, i) => `${i === 0 ? 'M' : 'L'} ${xs(i)} ${ys(p)}`).join(' ');
  const areaD = `${lineD} L ${xs(points.length - 1)} ${h - pad} L ${xs(0)} ${h - pad} Z`;
  return (
    <svg className="chart-svg" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
      <defs>
        <linearGradient id="chart-grad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#1f6e3f" stopOpacity="0.25" />
          <stop offset="100%" stopColor="#1f6e3f" stopOpacity="0" />
        </linearGradient>
      </defs>
      <line className="axis" x1={pad} y1={h - pad} x2={w - pad} y2={h - pad} />
      <path className="area" d={areaD} />
      <path className="line" d={lineD} />
    </svg>
  );
}

function TickerDetailScreen({ ticker, onBack, onPick, watchlist, onToggleWatch }) {
  const d = useMemoTD(() => Dt.getDetail(ticker), [ticker]);
  if (!d) return <div>Ticker not found.</div>;
  const inWatch = watchlist.has(ticker);
  return (
    <div>
      <div className="crumb">
        <a onClick={onBack}>Dashboard</a>
        <span className="sep">/</span>
        <a onClick={onBack}>Scan</a>
        <span className="sep">/</span>
        <span className="current">{d.ticker}</span>
      </div>

      <div className="ticker-header">
        <div>
          <div className="ticker-id">
            <span className="ticker-symbol-large">{d.ticker}</span>
            <span className="ticker-name">{d.name}</span>
          </div>
          <div className="ticker-meta-row">
            <span>Sector · <strong>{d.sector}</strong></span>
            <span>Mkt cap · <strong>{d.market_cap_str}</strong></span>
            <span>Beta · <strong>{d.beta}</strong></span>
            <span>Earnings · <strong>{d.earnings_days_until}d</strong></span>
            <span>Rank · <strong>#{String(d.rank).padStart(2,'0')}</strong></span>
          </div>
        </div>
        <div className="price-block">
          <div className="price-current">${d.last_price.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}</div>
          <div className={`price-change ${d.day_pct >= 0 ? 'up' : 'down'}`}>
            {(d.day_pct >= 0 ? '+' : '') + d.day_pct.toFixed(2)}% today
          </div>
          <div className="price-meta">As of 16:00 ET</div>
          <div style={{ display: 'flex', gap: 8, marginTop: 14, justifyContent: 'flex-end' }}>
            <button className="btn btn-secondary" onClick={() => onToggleWatch(ticker)}>
              <IconTD name="heart" /> {inWatch ? 'In watchlist' : 'Add to watchlist'}
            </button>
            <button className="btn btn-primary"><IconTD name="play" /> Backtest</button>
          </div>
        </div>
      </div>

      <div className="detail-grid">
        <div className="panel score-display">
          <div>
            <div className="score-number">{d.composite_score.toFixed(1)}</div>
            <div className="score-out-of">/ 100 · composite score</div>
            <div className="score-signal"><SignalTagTD signal={d.signal} large /></div>
          </div>
        </div>
        <FactorBreakdown factors={d.factors} median={Dt.SECTOR_MEDIAN} />
      </div>

      <div className="detail-grid">
        <div className="panel">
          <div className="panel-header"><div className="panel-title">Engine thesis</div><div className="panel-meta">29 Apr · auto-generated</div></div>
          <div className="thesis">
            {d.thesis.map((p, i) => <p key={i}>{richTextTD(p)}</p>)}
          </div>
          <div className="tag-cloud" style={{ borderTop: '1px solid var(--gray-300)' }}>
            {d.tags.map((t, i) => <span key={i} className={`tag ${t.warn ? 'warn' : ''}`}>{t.label}</span>)}
          </div>
        </div>
        <div className="panel">
          <div className="panel-header"><div className="panel-title">Key data</div><div className="panel-meta">From last close</div></div>
          <table className="key-data-table"><tbody>
            {d.key_data.map((row, i) => (
              <tr key={i}>
                <td className="label">{row[0]}</td>
                <td className={`value ${row[2] || ''}`}>{row[1]}</td>
              </tr>
            ))}
          </tbody></table>
        </div>
      </div>

      <div className="detail-grid">
        <div className="panel">
          <div className="panel-header"><div className="panel-title">Price · 60d</div><div className="panel-meta">Daily close</div></div>
          <div className="price-chart"><PriceMiniChart trend={d.day_pct >= 0 ? 'up' : 'down'} /></div>
        </div>
        <div className="panel">
          <div className="panel-header"><div className="panel-title">Score history</div><div className="panel-meta">Last 6 sessions</div></div>
          <div>
            {d.score_history.map((h, i) => (
              <div key={i} className={`history-row ${i === 0 ? 'current' : ''}`}>
                <span className="history-date">{h.date}</span>
                <span className="history-score">{h.score.toFixed(1)}</span>
                <span><SignalTagTD signal={h.signal || (h.score >= 80 ? 'strong_buy' : h.score >= 70 ? 'buy' : h.score >= 55 ? 'hold' : 'weak')} /></span>
                <span className="history-note">{h.note}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="panel" style={{ marginBottom: 24 }}>
        <div className="panel-header"><div className="panel-title">Position sizing</div><div className="panel-meta">Risk-parity model</div></div>
        <div className="sizing-block">
          <div className="sizing-meter">
            <span className="sizing-pct">{d.sizing.pct_of_portfolio.toFixed(1)}%</span>
            <span className="sizing-of">of portfolio · cap {d.sizing.cap_pct}%</span>
          </div>
          <div className="sizing-track">
            <div className="sizing-fill" style={{ width: `${(d.sizing.pct_of_portfolio / d.sizing.cap_pct) * 100}%` }} />
            <div className="sizing-cap" style={{ left: '100%' }} />
          </div>
          <p className="sizing-rationale">{d.sizing.rationale}</p>
        </div>
      </div>
    </div>
  );
}

window.STOOL_DETAIL = { TickerDetailScreen };
