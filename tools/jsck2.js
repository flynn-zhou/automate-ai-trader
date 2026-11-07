const fs = require('fs');
const src = fs.readFileSync('ai_trader/web/static/board.js', 'utf8');
let captured = '';
const el = { set innerHTML(v) { captured = v; }, get innerHTML() { return captured; } };
global.document = { getElementById: (id) => (id === 'causalChain' ? el : null) };
const start = src.indexOf('function renderTrend');
const end = src.indexOf('function renderStats');
const fmt = (v, n) => (v === null || v === undefined || isNaN(v)) ? '--' : Number(v).toFixed(n);
const renderTrend = eval('(' + src.slice(start, end).trim() + ')');
const baseTrend = { mode: 'trend', ready: true, bars: 419, lookback: 200, sma: 82337.4,
  price: 78090.0, dist_pct: -5.16, above: false, hold: false, need_pct: 5.44,
  next_bar_h: 1.2, target_leverage: 1.0, plan_notional: 71.7, plan_qty: 0.00092 };
const cases = [
  ['healthy', { guard: { can_open: true, drawdown_pct: 0, rules: { max_drawdown_pct: 60 } },
               executor: { name: 'spot', halted: false, reason: '' } }],
  ['executor halted', { guard: { can_open: true, drawdown_pct: 0, rules: { max_drawdown_pct: 60 } },
               executor: { name: 'spot', halted: true, reason: 'balance check failed: expected 0.00128 got 0.0' } }]
];
for (const [name, extra] of cases) {
  renderTrend(Object.assign({ trend: baseTrend }, extra));
  const txt = captured.replace(/<[^>]+>/g, ' | ').replace(/\s*\|\s*(\|\s*)+/g, ' | ').trim();
  console.log('--- ' + name + ' ---');
  console.log(txt);
  console.log();
}