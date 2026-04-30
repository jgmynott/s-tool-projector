// Watchlist + Backtest stub screens

const Dw = window.STOOL;
const { Icon: IconW, ScoreCell: ScoreCellW, SignalTag: SignalTagW } = window.STOOL_UI;

function WatchlistScreen({ watchlist, onPick, onToggleWatch }) {
  const rows = Dw.SCAN_RESULTS.filter(r => watchlist.has(r.ticker));
  return (
    <div>
      <div className="page-header">
        <div>
          <div className="page-eyebrow">Saved positions</div>
          <h1 className="page-title">Watch <em>list</em></h1>
        </div>
        <div className="page-actions">
          <button className="btn btn-secondary"><IconW name="download" /> Export</button>
        </div>
      </div>

      {rows.length === 0 ? (
        <div className="panel">
          <div className="empty-state">
            <h2>No tickers yet</h2>
            <p>Add tickers from the dashboard scan or any ticker detail page. Saved tickers appear here, with a live score readout each morning.</p>
            <button className="btn btn-primary" onClick={() => onPick(null)}>Browse the scan</button>
          </div>
        </div>
      ) : (
        <div className="panel">
          <div className="panel-header"><div className="panel-title">{rows.length} saved · current scan</div><div className="panel-meta">Updated 05:47 ET</div></div>
          <table className="scan-table">
            <thead><tr>
              <th>#</th><th>Ticker</th><th>Sector</th><th className="right">Last</th>
              <th className="right">Day %</th><th className="right">Score</th><th>Signal</th><th></th>
            </tr></thead>
            <tbody>
              {rows.map(r => (
                <tr key={r.ticker} className="in-watchlist" onClick={() => onPick(r.ticker)}>
                  <td><span className="rank">{String(r.rank).padStart(2,'0')}</span></td>
                  <td><div className="ticker-cell"><span className="ticker-symbol">{r.ticker}</span><span className="ticker-name-small">{r.name}</span></div></td>
                  <td>{r.sector}</td>
                  <td className="right">${r.last_price.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}</td>
                  <td className={`right pct ${r.day_pct >= 0 ? 'up' : 'down'}`}>{(r.day_pct >= 0 ? '+' : '') + r.day_pct.toFixed(2)}%</td>
                  <td className="right"><ScoreCellW value={r.composite_score} signal={r.signal} /></td>
                  <td><SignalTagW signal={r.signal} /></td>
                  <td className="right" onClick={e => { e.stopPropagation(); onToggleWatch(r.ticker); }}>
                    <button className="heart-btn on"><IconW name="heart" /></button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function BacktestScreen() {
  const stats = [
    ['CAGR (3y)', '+24.6%', 'up'],
    ['Max drawdown', '-18.2%', 'down'],
    ['Sharpe', '1.42'],
    ['Hit rate', '62.4%'],
    ['Avg hold', '14d'],
    ['Vs. SPX', '+8.4%', 'up'],
  ];
  return (
    <div>
      <div className="page-header">
        <div>
          <div className="page-eyebrow">Strategy validation</div>
          <h1 className="page-title">Back <em>test</em></h1>
        </div>
        <div className="page-actions">
          <button className="btn btn-primary"><IconW name="play" /> Run backtest</button>
        </div>
      </div>
      <div className="backtest-grid">
        <div className="panel">
          <div className="panel-header"><div className="panel-title">Strong Buy strategy · 3 years</div><div className="panel-meta">Daily rebalance</div></div>
          <div style={{ padding: 24 }}>
            <svg viewBox="0 0 600 240" style={{ width: '100%', height: 240 }}>
              <defs><linearGradient id="bt-grad" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stopColor="#1f6e3f" stopOpacity="0.3"/><stop offset="100%" stopColor="#1f6e3f" stopOpacity="0"/></linearGradient></defs>
              {(() => {
                const pts = [];
                let v = 100;
                for (let i = 0; i < 80; i++) { v *= 1 + (Math.sin(i / 6) * 0.02 + 0.004 + (Math.random() - 0.5) * 0.018); pts.push(v); }
                const min = Math.min(...pts), max = Math.max(...pts);
                const x = i => 30 + (i / (pts.length - 1)) * 540;
                const y = p => 20 + (1 - (p - min) / (max - min)) * 200;
                const line = pts.map((p, i) => `${i ? 'L' : 'M'} ${x(i)} ${y(p)}`).join(' ');
                return <>
                  <line x1="30" y1="220" x2="570" y2="220" stroke="#c4c0b8" />
                  <path d={`${line} L ${x(pts.length - 1)} 220 L ${x(0)} 220 Z`} fill="url(#bt-grad)" />
                  <path d={line} fill="none" stroke="#0d4a2c" strokeWidth="2" />
                  {[0, 0.25, 0.5, 0.75, 1].map(t => (
                    <text key={t} x="20" y={20 + t * 200 + 4} fontSize="9" fontFamily="JetBrains Mono, monospace" fill="#6b6b6b" textAnchor="end">{(max - (max - min) * t).toFixed(0)}</text>
                  ))}
                </>;
              })()}
            </svg>
          </div>
        </div>
        <div>
          <div className="backtest-stats">
            {stats.map(([l, v, t]) => (
              <div key={l} className="backtest-stat">
                <div className="label">{l}</div>
                <div className={`val ${t === 'down' ? 'down' : ''}`}>{v}</div>
                <div className="sub">{t === 'up' ? 'vs benchmark' : t === 'down' ? 'peak-to-trough' : 'sharpe ratio'}</div>
              </div>
            ))}
          </div>
          <div className="panel" style={{ marginTop: 24 }}>
            <div className="panel-header"><div className="panel-title">Notes</div><div className="panel-meta">Stub</div></div>
            <div style={{ padding: 24, fontSize: 13, color: '#3a3a3a', lineHeight: 1.6 }}>
              Backtest engine wiring is out of scope for this prototype. The chart and stats above are illustrative — the
              UI shape is what to build against.
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

window.STOOL_OTHER = { WatchlistScreen, BacktestScreen };
