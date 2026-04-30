// Dashboard screen — top-of-scan KPIs, scan table (filterable + sortable),
// compass viz of #1, sector strength, engine log.

const { useState, useMemo } = React;
const D2 = window.STOOL;
const { Icon, ScoreCell, SignalTag, Compass, richText } = window.STOOL_UI;

function StatsRow() {
  return (
    <div className="stats-row">
      <div className="stat-card">
        <div className="stat-label">Scanned today</div>
        <div className="stat-value">287</div>
        <div className="stat-trend neutral">— Universe complete</div>
      </div>
      <div className="stat-card">
        <div className="stat-label">Strong-buy signals</div>
        <div className="stat-value">2</div>
        <div className="stat-trend up">▲ +1 vs yesterday</div>
      </div>
      <div className="stat-card">
        <div className="stat-label">Avg composite</div>
        <div className="stat-value">58.4</div>
        <div className="stat-trend up">▲ +2.1 wk-over-wk</div>
      </div>
      <div className="stat-card">
        <div className="stat-label">Risk events 30d</div>
        <div className="stat-value">14</div>
        <div className="stat-trend down">▼ Earnings cluster</div>
      </div>
    </div>
  );
}

function ScanTable({ rows, onPick, watchlist, onToggleWatch, sortKey, sortDir, onSort }) {
  const cols = [
    { key: 'rank',            label: '#',         right: false },
    { key: 'ticker',          label: 'Ticker',    right: false },
    { key: 'sector',          label: 'Sector',    right: false },
    { key: 'last_price',      label: 'Last',      right: true },
    { key: 'day_pct',         label: 'Day %',     right: true },
    { key: 'composite_score', label: 'Score',     right: true },
    { key: 'signal',          label: 'Signal',    right: false },
    { key: 'watch',           label: '',          right: true, nosort: true },
  ];
  const arrow = (k) => sortKey === k ? <span className="sort-arrow">{sortDir === 'asc' ? '▲' : '▼'}</span> : <span className="sort-arrow">▾</span>;
  return (
    <table className="scan-table">
      <thead>
        <tr>{cols.map(c => (
          <th key={c.key} className={`${c.right ? 'right' : ''} ${sortKey === c.key ? 'sorted' : ''}`}
              onClick={() => !c.nosort && onSort(c.key)}>
            {c.label}{!c.nosort && arrow(c.key)}
          </th>
        ))}</tr>
      </thead>
      <tbody>
        {rows.map(r => (
          <tr key={r.ticker} className={watchlist.has(r.ticker) ? 'in-watchlist' : ''} onClick={() => onPick(r.ticker)}>
            <td><span className="rank">{String(r.rank).padStart(2, '0')}</span></td>
            <td><div className="ticker-cell"><span className="ticker-symbol">{r.ticker}</span><span className="ticker-name-small">{r.name}</span></div></td>
            <td style={{ color: '#3a3a3a' }}>{r.sector}</td>
            <td className="right">${r.last_price.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}</td>
            <td className={`right pct ${r.day_pct >= 0 ? 'up' : 'down'}`}>{(r.day_pct >= 0 ? '+' : '') + r.day_pct.toFixed(2)}%</td>
            <td className="right"><ScoreCell value={r.composite_score} signal={r.signal} /></td>
            <td><SignalTag signal={r.signal} /></td>
            <td className="right" onClick={e => { e.stopPropagation(); onToggleWatch(r.ticker); }}>
              <button className={`heart-btn ${watchlist.has(r.ticker) ? 'on' : ''}`} title="Toggle watchlist"><Icon name="heart" /></button>
            </td>
          </tr>
        ))}
        {rows.length === 0 && <tr><td colSpan="8" style={{ textAlign: 'center', padding: 40, color: '#6b6b6b' }}>No tickers match your filters.</td></tr>}
      </tbody>
    </table>
  );
}

function FilterBar({ signal, setSignal, sector, setSector }) {
  const signals = ['all', 'strong_buy', 'buy', 'hold', 'weak'];
  const labels = { all: 'All', strong_buy: 'Strong Buy', buy: 'Buy', hold: 'Hold', weak: 'Weak' };
  const sectors = ['all', ...new Set(D2.SCAN_RESULTS.map(r => r.sector))];
  return (
    <div className="filter-bar">
      <span className="filter-label">Signal</span>
      {signals.map(s => <button key={s} className={`filter-chip ${signal === s ? 'active' : ''}`} onClick={() => setSignal(s)}>{labels[s]}</button>)}
      <span className="filter-divider" />
      <span className="filter-label">Sector</span>
      {sectors.map(s => <button key={s} className={`filter-chip ${sector === s ? 'active' : ''}`} onClick={() => setSector(s)}>{s === 'all' ? 'All' : s}</button>)}
    </div>
  );
}

function CompassPanel({ row, mode, setMode }) {
  const modes = [
    { id: 'radar',      label: 'Radar' },
    { id: 'polar_bars', label: 'Polar bars' },
    { id: 'quadrants',  label: 'Quadrants' },
  ];
  return (
    <div className="panel">
      <div className="panel-header">
        <div className="panel-title">Today's Compass</div>
        <div className="panel-meta">Rank 01</div>
      </div>
      <div className="compass-wrap">
        <Compass factors={row.factors} mode={mode} />
        <div className="compass-readout">
          <div className="top-score">{row.composite_score.toFixed(1)}</div>
          <div className="top-ticker">{row.ticker} · {row.name}</div>
          <SignalTag signal={row.signal} large />
          <div className="compass-meta-row">
            <div className="compass-meta-cell"><div className="label">Momentum</div><div className="value">{row.factors.momentum}</div></div>
            <div className="compass-meta-cell"><div className="label">Fundamentals</div><div className="value">{row.factors.fundamentals}</div></div>
            <div className="compass-meta-cell"><div className="label">Risk</div><div className="value">{row.factors.risk}</div></div>
          </div>
          {setMode ? (
            <div className="compass-modes">
              {modes.map(m => (
                <button key={m.id} className={`compass-mode ${mode === m.id ? 'active' : ''}`} onClick={() => setMode(m.id)}>{m.label}</button>
              ))}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function SectorPanel() {
  return (
    <div className="panel" style={{ marginTop: 24 }}>
      <div className="panel-header">
        <div className="panel-title">Sector Strength</div>
        <div className="panel-meta">Avg score</div>
      </div>
      <div className="sector-wrap">
        {D2.SECTOR_STRENGTH.map(s => (
          <div key={s.sector} className="sector-bar">
            <div className="sector-name">{s.sector}</div>
            <div className="sector-bar-track"><div className={`sector-bar-fill ${s.tier}`} style={{ width: `${s.avg_score}%` }} /></div>
            <div className="sector-value">{s.avg_score.toFixed(1)}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

function LogPanel() {
  return (
    <div className="panel" style={{ marginTop: 24 }}>
      <div className="panel-header">
        <div className="panel-title">Engine Log</div>
        <div className="panel-meta">Last 24h</div>
      </div>
      <div>
        {D2.ENGINE_LOG.map((e, i) => (
          <div key={i} className="log-entry">
            <div className="log-time">{e.timestamp}</div>
            <div className="log-text">{richText(e.message)}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

function DashboardScreen({ onPick, query, compassMode, setCompassMode, watchlist, onToggleWatch }) {
  const [signal, setSignal] = useState('all');
  const [sector, setSector] = useState('all');
  const [sortKey, setSortKey] = useState('rank');
  const [sortDir, setSortDir] = useState('asc');

  const filtered = useMemo(() => {
    let rows = D2.SCAN_RESULTS.slice();
    if (signal !== 'all') rows = rows.filter(r => r.signal === signal);
    if (sector !== 'all') rows = rows.filter(r => r.sector === sector);
    if (query) {
      const q = query.toLowerCase();
      rows = rows.filter(r => r.ticker.toLowerCase().includes(q) || r.name.toLowerCase().includes(q) || r.sector.toLowerCase().includes(q));
    }
    rows.sort((a, b) => {
      const av = a[sortKey], bv = b[sortKey];
      if (typeof av === 'string') return sortDir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av);
      return sortDir === 'asc' ? av - bv : bv - av;
    });
    return rows;
  }, [signal, sector, query, sortKey, sortDir]);

  const onSort = (k) => {
    if (k === sortKey) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    else { setSortKey(k); setSortDir(k === 'rank' ? 'asc' : 'desc'); }
  };

  return (
    <div>
      <div className="page-header">
        <div>
          <div className="page-eyebrow">Today · 29 April 2026</div>
          <h1 className="page-title">Morning <em>scan</em></h1>
        </div>
        <div className="page-actions">
          <button className="btn btn-secondary"><Icon name="download" /> Export CSV</button>
          <button className="btn btn-primary"><Icon name="refresh" /> Re-run scan</button>
        </div>
      </div>

      <StatsRow />

      <div className="main-grid">
        <div className="panel">
          <div className="panel-header">
            <div>
              <div className="panel-title">Top of scan</div>
            </div>
            <div className="panel-meta">{filtered.length} of {D2.SCAN_RESULTS.length} tickers</div>
          </div>
          <FilterBar signal={signal} setSignal={setSignal} sector={sector} setSector={setSector} />
          <ScanTable rows={filtered} onPick={onPick} watchlist={watchlist} onToggleWatch={onToggleWatch} sortKey={sortKey} sortDir={sortDir} onSort={onSort} />
        </div>
        <div>
          <CompassPanel row={D2.SCAN_RESULTS[0]} mode={compassMode} setMode={setCompassMode} />
          <SectorPanel />
        </div>
      </div>

      <LogPanel />
    </div>
  );
}

window.STOOL_DASHBOARD = { DashboardScreen };
