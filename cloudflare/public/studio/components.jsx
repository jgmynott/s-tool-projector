// Shared atoms and small components

const { useState, useMemo, useEffect, useRef } = React;
const D = window.STOOL;

// Brand mark — leaf compass
function BrandMark({ size = 36, color = "#a3d4b3" }) {
  return (
    <svg viewBox="0 0 60 60" width={size} height={size}>
      <circle cx="30" cy="30" r="28" fill="none" stroke={color} strokeWidth="2" />
      <path d="M30 8 L30 52 M8 30 L52 30" stroke={color} strokeWidth="1" opacity="0.4" />
      <path d="M30 12 Q42 22 30 30 Q18 22 30 12 Z" fill={color} opacity="0.9" />
      <circle cx="30" cy="30" r="3" fill={color} />
    </svg>
  );
}

function Icon({ name }) {
  const paths = {
    dashboard: <><path d="M3 3h7v9H3z M14 3h7v5h-7z M14 12h7v9h-7z M3 16h7v5H3z" /></>,
    scan: <><path d="M3 9V3h6 M21 9V3h-6 M3 15v6h6 M21 15v6h-6 M7 12h10" /></>,
    watchlist: <><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.29 1.51 4.04 3 5.5l7 7Z"/></>,
    backtest: <><path d="M3 3v18h18 M7 16l4-4 4 3 5-7"/></>,
    settings: <><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3h.1a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8v.1a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1Z"/></>,
    search: <><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></>,
    bell: <><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9 M10.3 21a1.94 1.94 0 0 0 3.4 0"/></>,
    arrow_right: <><path d="M5 12h14 M12 5l7 7-7 7"/></>,
    refresh: <><path d="M3 12a9 9 0 0 1 15-6.7L21 8 M21 3v5h-5 M21 12a9 9 0 0 1-15 6.7L3 16 M3 21v-5h5"/></>,
    download: <><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4 M7 10l5 5 5-5 M12 15V3"/></>,
    heart: <><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.29 1.51 4.04 3 5.5l7 7Z"/></>,
    play: <><path d="M5 3l14 9-14 9V3z"/></>,
    chevron_left: <><path d="m15 18-6-6 6-6"/></>,
  };
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>
  );
}

function SignalTag({ signal, large }) {
  return <span className={`signal-tag ${signal}`} style={large ? { fontSize: 13, padding: '6px 14px', letterSpacing: '0.16em' } : null}>{D.SIGNAL_LABEL[signal]}</span>;
}

function ScoreCell({ value, signal }) {
  return <span className={`score-cell ${signal}`}>{value.toFixed(1)}</span>;
}

// Compass viz — 3 modes via Tweaks: 'radar', 'polar_bars', 'quadrants'
function Compass({ factors, mode = 'radar' }) {
  const factorNames = ['momentum', 'fundamentals', 'risk', 'quality', 'value'];
  const cx = 150, cy = 150, R = 110;
  const angleFor = (i) => -Math.PI / 2 + (i * 2 * Math.PI / factorNames.length);
  const pointFor = (i, val) => {
    const a = angleFor(i);
    const r = (val / 100) * R;
    return [cx + Math.cos(a) * r, cy + Math.sin(a) * r];
  };
  const labelFor = (i) => {
    const a = angleFor(i);
    return [cx + Math.cos(a) * (R + 18), cy + Math.sin(a) * (R + 18)];
  };

  const polygon = factorNames.map((n, i) => pointFor(i, factors[n]).join(',')).join(' ');

  return (
    <svg className="compass-svg" viewBox="0 0 300 300">
      <defs>
        <radialGradient id="cmp-grad">
          <stop offset="0%" stopColor="#1f6e3f" stopOpacity="0.25" />
          <stop offset="100%" stopColor="#1f6e3f" stopOpacity="0.05" />
        </radialGradient>
      </defs>

      {/* concentric rings */}
      {[0.25, 0.5, 0.75, 1].map(t => (
        <circle key={t} cx={cx} cy={cy} r={R * t} fill="none" stroke="#c4c0b8" strokeWidth="1" strokeDasharray={t < 1 ? "2,3" : "0"} />
      ))}
      {/* axes */}
      {factorNames.map((_, i) => {
        const a = angleFor(i);
        return <line key={i} x1={cx} y1={cy} x2={cx + Math.cos(a) * R} y2={cy + Math.sin(a) * R} stroke="#c4c0b8" strokeWidth="1" strokeDasharray="2,3" />;
      })}

      {mode === 'radar' && (
        <>
          <polygon points={polygon} fill="url(#cmp-grad)" stroke="#1f6e3f" strokeWidth="2" />
          {factorNames.map((n, i) => {
            const [x, y] = pointFor(i, factors[n]);
            return <circle key={n} className="pt" cx={x} cy={y} r="4" fill="#0d4a2c" />;
          })}
        </>
      )}

      {mode === 'polar_bars' && factorNames.map((n, i) => {
        const a = angleFor(i);
        const w = 0.6;
        const inner = 0;
        const outer = (factors[n] / 100) * R;
        const x1 = cx + Math.cos(a - w/2) * inner;
        const y1 = cy + Math.sin(a - w/2) * inner;
        const x2 = cx + Math.cos(a + w/2) * inner;
        const y2 = cy + Math.sin(a + w/2) * inner;
        const x3 = cx + Math.cos(a + w/2) * outer;
        const y3 = cy + Math.sin(a + w/2) * outer;
        const x4 = cx + Math.cos(a - w/2) * outer;
        const y4 = cy + Math.sin(a - w/2) * outer;
        return <polygon key={n} points={`${x1},${y1} ${x2},${y2} ${x3},${y3} ${x4},${y4}`} fill={factors[n] > 70 ? '#0d4a2c' : factors[n] > 50 ? '#1f6e3f' : '#c97a2c'} fillOpacity="0.85" />;
      })}

      {mode === 'quadrants' && factorNames.map((n, i) => {
        const [x, y] = pointFor(i, factors[n]);
        return <g key={n}>
          <line x1={cx} y1={cy} x2={x} y2={y} stroke="#1f6e3f" strokeWidth="2" />
          <circle className="pt" cx={x} cy={y} r="6" fill="#0d4a2c" />
        </g>;
      })}

      {/* labels */}
      {factorNames.map((n, i) => {
        const [lx, ly] = labelFor(i);
        return <text key={n} x={lx} y={ly} fontFamily="JetBrains Mono, monospace" fontSize="9" fill="#6b6b6b" letterSpacing="1.5" textAnchor="middle" alignmentBaseline="middle">{n.slice(0,4).toUpperCase()}</text>;
      })}
    </svg>
  );
}

// Render markdown-ish text (bold + code)
function richText(t) {
  const parts = [];
  let rest = t;
  let key = 0;
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*]+\*)/;
  while (rest.length) {
    const m = rest.match(re);
    if (!m) { parts.push(rest); break; }
    parts.push(rest.slice(0, m.index));
    const tok = m[0];
    if (tok.startsWith('**')) parts.push(<strong key={++key}>{tok.slice(2,-2)}</strong>);
    else if (tok.startsWith('`')) parts.push(<code key={++key}>{tok.slice(1,-1)}</code>);
    else parts.push(<em key={++key}>{tok.slice(1,-1)}</em>);
    rest = rest.slice(m.index + tok.length);
  }
  return parts;
}

window.STOOL_UI = { BrandMark, Icon, SignalTag, ScoreCell, Compass, richText };
