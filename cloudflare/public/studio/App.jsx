// App shell — route state, watchlist, search, compass mode.
// Adapted from Claude Design handoff: TweaksPanel stripped (debug-only),
// compass-mode toggle inlined into the dashboard CompassPanel.

const { useState: useStateApp } = React;
const Da = window.STOOL;
const { Sidebar, Topbar } = window.STOOL_CHROME;
const { DashboardScreen } = window.STOOL_DASHBOARD;
const { TickerDetailScreen } = window.STOOL_DETAIL;
const { WatchlistScreen, BacktestScreen } = window.STOOL_OTHER;

function App() {
  const [route, setRoute] = useStateApp({ name: 'dashboard' });
  const [query, setQuery] = useStateApp('');
  const [watchlist, setWatchlist] = useStateApp(() => new Set(['NVDA', 'META']));
  const [compassMode, setCompassMode] = useStateApp('radar');

  const onPick = (ticker) => {
    if (!ticker) { setRoute({ name: 'dashboard' }); return; }
    setRoute({ name: 'detail', ticker });
    window.scrollTo(0, 0);
  };
  const onToggleWatch = (t) => {
    setWatchlist(prev => {
      const next = new Set(prev);
      if (next.has(t)) next.delete(t); else next.add(t);
      return next;
    });
  };

  let body;
  if (route.name === 'dashboard' || route.name === 'scan') {
    body = <DashboardScreen onPick={onPick} query={query} compassMode={compassMode} setCompassMode={setCompassMode} watchlist={watchlist} onToggleWatch={onToggleWatch} />;
  } else if (route.name === 'detail') {
    body = <TickerDetailScreen ticker={route.ticker} onBack={() => setRoute({ name: 'dashboard' })} onPick={onPick} watchlist={watchlist} onToggleWatch={onToggleWatch} />;
  } else if (route.name === 'watchlist') {
    body = <WatchlistScreen watchlist={watchlist} onPick={onPick} onToggleWatch={onToggleWatch} />;
  } else if (route.name === 'backtest') {
    body = <BacktestScreen />;
  }

  return (
    <>
      <div className="studio-banner">
        <div className="studio-banner-left">
          <span className="studio-banner-pill">Studio</span>
          <span>Interactive prototype · mock data</span>
        </div>
        <a className="studio-banner-link" href="/">← Back to s-tool.io</a>
      </div>
      <div className="app" data-screen-label={route.name === 'detail' ? `Ticker · ${route.ticker}` : route.name}>
        <Sidebar route={route} onRoute={setRoute} watchlistCount={watchlist.size} />
        <main className="content">
          <Topbar query={query} onSearch={setQuery} />
          <div key={route.name + (route.ticker || '')}>{body}</div>
        </main>
      </div>
    </>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
