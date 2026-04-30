// S-Tool mock data — shape matches API contract in README.md.
const SIGNAL_FOR_SCORE = (s) => s >= 80 ? 'strong_buy' : s >= 70 ? 'buy' : s >= 55 ? 'hold' : 'weak';
const SIGNAL_LABEL = { strong_buy: 'Strong Buy', buy: 'Buy', hold: 'Hold', weak: 'Weak' };

const SCAN_RESULTS = [
  { rank: 1,  ticker: 'NVDA',  name: 'NVIDIA Corp',           sector: 'Semiconductors', last_price: 138.42,  day_pct:  2.31, composite_score: 87.4, factors: { momentum: 92, fundamentals: 81, risk: 71, quality: 89, value: 64 }, flags: ['earnings_window','high_beta'] },
  { rank: 2,  ticker: 'META',  name: 'Meta Platforms',        sector: 'Comm Services',  last_price: 612.18,  day_pct:  1.84, composite_score: 81.2, factors: { momentum: 84, fundamentals: 86, risk: 74, quality: 82, value: 71 }, flags: ['earnings_window'] },
  { rank: 3,  ticker: 'AVGO',  name: 'Broadcom Inc',          sector: 'Semiconductors', last_price: 1847.92, day_pct:  3.12, composite_score: 79.6, factors: { momentum: 88, fundamentals: 78, risk: 68, quality: 80, value: 62 }, flags: ['high_beta'] },
  { rank: 4,  ticker: 'MSFT',  name: 'Microsoft Corp',        sector: 'Software',       last_price: 428.51,  day_pct:  0.92, composite_score: 76.5, factors: { momentum: 71, fundamentals: 88, risk: 79, quality: 85, value: 66 }, flags: [] },
  { rank: 5,  ticker: 'GOOGL', name: 'Alphabet Inc',          sector: 'Comm Services',  last_price: 184.27,  day_pct:  1.42, composite_score: 73.8, factors: { momentum: 68, fundamentals: 82, risk: 76, quality: 81, value: 72 }, flags: [] },
  { rank: 6,  ticker: 'AMD',   name: 'Advanced Micro Devices', sector: 'Semiconductors', last_price: 162.84, day_pct:  1.04, composite_score: 72.1, factors: { momentum: 78, fundamentals: 70, risk: 64, quality: 73, value: 60 }, flags: ['high_beta'] },
  { rank: 7,  ticker: 'AMZN',  name: 'Amazon.com Inc',        sector: 'Consumer Disc',  last_price: 218.46,  day_pct:  0.78, composite_score: 71.2, factors: { momentum: 70, fundamentals: 76, risk: 72, quality: 74, value: 58 }, flags: [] },
  { rank: 8,  ticker: 'PLTR',  name: 'Palantir Tech',         sector: 'Software',       last_price:  42.18,  day_pct:  4.62, composite_score: 68.9, factors: { momentum: 89, fundamentals: 60, risk: 52, quality: 68, value: 48 }, flags: ['high_beta','low_float'] },
  { rank: 9,  ticker: 'CRM',   name: 'Salesforce',            sector: 'Software',       last_price: 291.04,  day_pct:  0.51, composite_score: 64.7, factors: { momentum: 60, fundamentals: 71, risk: 70, quality: 72, value: 56 }, flags: [] },
  { rank: 10, ticker: 'AAPL',  name: 'Apple Inc',             sector: 'Hardware',       last_price: 201.83,  day_pct: -0.41, composite_score: 62.8, factors: { momentum: 52, fundamentals: 80, risk: 78, quality: 79, value: 54 }, flags: [] },
  { rank: 11, ticker: 'ORCL',  name: 'Oracle Corp',           sector: 'Software',       last_price: 168.31,  day_pct:  0.22, composite_score: 61.4, factors: { momentum: 64, fundamentals: 70, risk: 71, quality: 68, value: 60 }, flags: [] },
  { rank: 12, ticker: 'TSM',   name: 'Taiwan Semiconductor',  sector: 'Semiconductors', last_price: 178.92,  day_pct:  1.18, composite_score: 60.8, factors: { momentum: 66, fundamentals: 74, risk: 60, quality: 72, value: 58 }, flags: [] },
  { rank: 13, ticker: 'NFLX',  name: 'Netflix Inc',           sector: 'Comm Services',  last_price: 712.04,  day_pct: -0.62, composite_score: 58.2, factors: { momentum: 54, fundamentals: 68, risk: 66, quality: 70, value: 50 }, flags: [] },
  { rank: 14, ticker: 'V',     name: 'Visa Inc',              sector: 'Financials',     last_price: 286.41,  day_pct:  0.18, composite_score: 56.7, factors: { momentum: 50, fundamentals: 78, risk: 80, quality: 78, value: 48 }, flags: [] },
  { rank: 15, ticker: 'JPM',   name: 'JPMorgan Chase',        sector: 'Financials',     last_price: 218.92,  day_pct: -0.31, composite_score: 54.3, factors: { momentum: 48, fundamentals: 74, risk: 76, quality: 72, value: 60 }, flags: [] },
  { rank: 16, ticker: 'UNH',   name: 'UnitedHealth Group',    sector: 'Healthcare',     last_price: 528.14,  day_pct:  0.84, composite_score: 51.9, factors: { momentum: 44, fundamentals: 66, risk: 72, quality: 68, value: 52 }, flags: [] },
  { rank: 17, ticker: 'XOM',   name: 'Exxon Mobil',           sector: 'Energy',         last_price: 116.28,  day_pct: -1.12, composite_score: 46.4, factors: { momentum: 38, fundamentals: 60, risk: 64, quality: 56, value: 70 }, flags: [] },
  { rank: 18, ticker: 'KO',    name: 'Coca-Cola Co',          sector: 'Consumer Disc',  last_price:  68.91,  day_pct:  0.04, composite_score: 44.2, factors: { momentum: 32, fundamentals: 70, risk: 82, quality: 72, value: 48 }, flags: [] },
  { rank: 19, ticker: 'CVX',   name: 'Chevron Corp',          sector: 'Energy',         last_price: 162.54,  day_pct: -0.92, composite_score: 41.7, factors: { momentum: 34, fundamentals: 62, risk: 60, quality: 58, value: 68 }, flags: [] },
  { rank: 20, ticker: 'TSLA',  name: 'Tesla Inc',             sector: 'Consumer Disc',  last_price: 218.43,  day_pct: -2.18, composite_score: 38.4, factors: { momentum: 28, fundamentals: 48, risk: 32, quality: 60, value: 30 }, flags: ['earnings_window','high_beta'] },
].map(r => ({ ...r, signal: SIGNAL_FOR_SCORE(r.composite_score) }));

const SECTOR_MEDIAN = { momentum: 58, fundamentals: 62, risk: 65, quality: 64, value: 58 };

const TICKER_DETAILS = {
  NVDA: {
    market_cap_str: '$3.42T', beta: 1.68, earnings_days_until: 14,
    thesis: [
      "NVDA tops today's scan on the strength of *sustained momentum* across multiple windows. The 60-day return slope sits in the 94th percentile of the semiconductor cohort, and the 20-day RSI confirms continuation rather than mean-reversion risk.",
      "Fundamentals remain in the strong band. Gross margin expansion has been steady for four consecutive quarters, and free cash flow yield is rising despite the price appreciation. The data center segment continues to drive revenue growth that outpaces the sector median by a wide margin.",
      "Risk is the softest factor today. Realized volatility runs above the sector average, and earnings sit two weeks out, which the engine flags as a confidence-band reduction rather than a score deduction. *Position sizing should account for this.*",
    ],
    key_data: [
      ['52w High','$152.89'],['52w Low','$76.12'],['YTD Return','+38.4%','up'],
      ['P/E (TTM)','52.8'],['Fwd P/E','38.2'],['P/S','28.4'],
      ['Gross Margin','75.2%','up'],['FCF Yield','2.1%'],['Beta (1Y)','1.68'],
      ['Realized Vol (30d)','38.2%'],['Avg Volume (30d)','218.4M'],
    ],
    score_history: [
      { date: '29 Apr', score: 87.4, note: 'Strong Buy — top of scan' },
      { date: '28 Apr', score: 85.1, note: 'Strong Buy — held rank' },
      { date: '25 Apr', score: 82.6, note: 'Buy — momentum confirmed' },
      { date: '22 Apr', score: 76.2, note: 'Buy — entered top decile' },
      { date: '15 Apr', score: 68.4, note: 'Hold — risk band increased' },
      { date: '08 Apr', score: 64.1, note: 'Hold — fundamentals refresh' },
    ],
    sizing: { pct_of_portfolio: 3.8, cap_pct: 5, rationale: 'Sized down from the 5% cap due to elevated realized volatility and the upcoming earnings window. Re-evaluate after the May 13 release.' },
    tags: [
      { label: 'Top decile momentum' },
      { label: 'Margin expansion' },
      { label: 'FCF positive' },
      { label: 'Earnings in 14d', warn: true },
      { label: 'High beta', warn: true },
    ],
  },
};

const SECTOR_STRENGTH = [
  { sector: 'Semis',      avg_score: 82.1, tier: 'high' },
  { sector: 'Comm Svc',   avg_score: 74.3, tier: 'high' },
  { sector: 'Software',   avg_score: 68.5, tier: 'med'  },
  { sector: 'Cons Disc',  avg_score: 61.2, tier: 'med'  },
  { sector: 'Hardware',   avg_score: 56.8, tier: 'med'  },
  { sector: 'Healthcare', avg_score: 49.1, tier: 'med'  },
  { sector: 'Financials', avg_score: 42.7, tier: 'low'  },
  { sector: 'Energy',     avg_score: 36.4, tier: 'low'  },
  { sector: 'REITs',      avg_score: 31.9, tier: 'low'  },
];

const ENGINE_LOG = [
  { timestamp: '05:47 ET', category: 'scan',     message: '**Scan complete.** 287 tickers processed. Top score `NVDA` at 87.4.' },
  { timestamp: '05:43 ET', category: 'risk',     message: 'Risk module flagged `TSLA` entering 5-day earnings window.' },
  { timestamp: '05:38 ET', category: 'data',     message: 'Fundamentals refresh from Tiingo. 282 of 287 returned clean data.' },
  { timestamp: '05:32 ET', category: 'momentum', message: 'Momentum recalc on rolling 60-day window. **+8 new entries** to top decile.' },
  { timestamp: '05:21 ET', category: 'data',     message: 'Quote feed reconnect. Latency back under 200ms.' },
  { timestamp: '05:00 ET', category: 'system',   message: 'Daily scan started. Engine v1.4.2.' },
];

const SCAN_META = { date: '2026-04-29', scanned_count: 287, engine_version: '1.4.2', completed_at: '05:47 ET' };

// Synthesize detail from row when not explicitly defined
function getDetail(ticker) {
  const row = SCAN_RESULTS.find(r => r.ticker === ticker);
  if (!row) return null;
  const explicit = TICKER_DETAILS[ticker];
  if (explicit) return { ...row, ...explicit };
  return {
    ...row,
    market_cap_str: '—', beta: 1.0, earnings_days_until: 30,
    thesis: [
      `${ticker} ranks ${row.rank} in today's scan with a composite of ${row.composite_score}. The factor profile leans on ${strongestFactor(row.factors)}, with the engine reading this as a *${SIGNAL_LABEL[row.signal].toLowerCase()}* signal.`,
      `Sector context (${row.sector}) is consistent with the average score, and there are no flagged risk events in the next 30 days.`,
      `Standard sizing applies. *Re-evaluate after the next scan refresh.*`,
    ],
    key_data: [
      ['52w High', '$' + (row.last_price * 1.18).toFixed(2)],
      ['52w Low',  '$' + (row.last_price * 0.62).toFixed(2)],
      ['YTD Return', '+12.4%', 'up'],
      ['P/E (TTM)', '24.6'], ['P/S', '4.2'],
      ['Gross Margin', '48.0%'], ['FCF Yield', '3.1%'],
      ['Beta (1Y)', '1.05'], ['Realized Vol (30d)', '24.1%'],
    ],
    score_history: [
      { date: '29 Apr', score: row.composite_score, note: `${SIGNAL_LABEL[row.signal]} — current` },
      { date: '28 Apr', score: +(row.composite_score - 1.4).toFixed(1), note: 'Held rank' },
      { date: '25 Apr', score: +(row.composite_score - 3.8).toFixed(1), note: 'Mid-week refresh' },
      { date: '22 Apr', score: +(row.composite_score - 6.2).toFixed(1), note: 'Entered cohort' },
    ],
    sizing: { pct_of_portfolio: 2.4, cap_pct: 5, rationale: 'Standard risk-parity weight given the current factor mix and sector context.' },
    tags: [
      { label: SIGNAL_LABEL[row.signal] },
      ...(row.flags.includes('earnings_window') ? [{ label: 'Earnings window', warn: true }] : []),
      ...(row.flags.includes('high_beta') ? [{ label: 'High beta', warn: true }] : []),
    ],
  };
}

function strongestFactor(f) {
  return Object.entries(f).sort((a,b) => b[1]-a[1])[0][0];
}

window.STOOL = {
  SCAN_RESULTS, SCAN_META, SECTOR_STRENGTH, SECTOR_MEDIAN,
  ENGINE_LOG, SIGNAL_LABEL, getDetail,
};
