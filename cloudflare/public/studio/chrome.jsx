// Sidebar + Topbar chrome

const { BrandMark, Icon } = window.STOOL_UI;

function Sidebar({ route, onRoute, watchlistCount }) {
  const items = [
    { id: 'dashboard', label: 'Dashboard', icon: 'dashboard' },
    { id: 'scan',      label: 'Scan',      icon: 'scan' },
    { id: 'watchlist', label: 'Watchlist', icon: 'watchlist', badge: watchlistCount || null },
    { id: 'backtest',  label: 'Backtest',  icon: 'backtest' },
  ];
  const config = [
    { id: 'engine',   label: 'Engine',   icon: 'settings' },
    { id: 'settings', label: 'Settings', icon: 'settings' },
  ];
  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        <BrandMark />
        <span className="word">s-tool</span>
      </div>
      <div className="sidebar-section">Workspace</div>
      {items.map(it => (
        <button key={it.id} className={`nav-item ${route.name === it.id ? 'active' : ''}`} onClick={() => onRoute({ name: it.id })}>
          <Icon name={it.icon} />
          <span>{it.label}</span>
          {it.badge ? <span className="badge">{it.badge}</span> : null}
        </button>
      ))}
      <div className="sidebar-section">Configuration</div>
      {config.map(it => (
        <button key={it.id} className="nav-item" onClick={() => alert('Stub — not wired in this prototype')}>
          <Icon name={it.icon} />
          <span>{it.label}</span>
        </button>
      ))}
      <div className="sidebar-footer">
        <div className="status"><span className="status-dot" /> Engine online</div>
        <div>v1.4.2 · 287 tickers</div>
      </div>
    </aside>
  );
}

function Topbar({ onSearch, query }) {
  return (
    <div className="topbar">
      <div className="search-box">
        <Icon name="search" />
        <input
          placeholder="Search ticker, sector, or factor…"
          value={query}
          onChange={e => onSearch(e.target.value)}
        />
      </div>
      <div className="topbar-right">
        <span className="topbar-meta">Last scan · <strong>05:47 ET</strong></span>
        <button className="btn-icon" title="Notifications"><Icon name="bell" /></button>
        <div className="avatar">M</div>
      </div>
    </div>
  );
}

window.STOOL_CHROME = { Sidebar, Topbar };
