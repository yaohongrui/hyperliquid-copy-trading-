const $ = (selector) => document.querySelector(selector);
let source;

function money(value) { return `${Number(value || 0).toFixed(2)} USDT`; }
function cls(value) { return Number(value) >= 0 ? 'positive' : 'negative'; }
function td(label, value, klass = '') { return `<td data-label="${label}"${klass ? ` class="${klass}"` : ''}>${value}</td>`; }

function table(rows, options = {}) {
  if (!rows?.length) return '<p class="muted">No open positions.</p>';
  const action = options.breakeven ? '<th>Action</th>' : '';
  return `<div class="table-wrap"><table class="table"><thead><tr><th>Symbol</th><th>Side</th><th>Size</th><th>Entry</th><th>Mark</th><th>PnL</th><th>ROI</th>${action}</tr></thead><tbody>${rows.map((p) => `<tr>${td('Symbol', p.symbol || '--')}${td('Side', p.side || '--')}${td('Size', Number(p.size || 0).toFixed(5))}${td('Entry', Number(p.entry_price || 0).toFixed(5))}${td('Mark', Number(p.mark_price || 0).toFixed(5))}${td('PnL', money(p.unrealized_pnl), cls(p.unrealized_pnl))}${td('ROI', `${Number(p.roi || 0).toFixed(2)}%`, cls(p.roi))}${options.breakeven ? td('Action', `<button class="table-action${p.breakeven ? ' active' : ''}" data-breakeven="${esc(p.symbol || '')}" data-enabled="${p.breakeven ? 'false' : 'true'}">${p.breakeven ? 'Cancel breakeven' : 'Breakeven'}</button>`) : ''}</tr>`).join('')}</tbody></table></div>`;
}

function orders(rows) {
  if (!rows?.length) return '<p class="muted">No open orders.</p>';
  if (rows[0]?.error) return `<p class="error">Orders unavailable: ${rows[0].error}</p>`;
  return `<div class="table-wrap"><table class="table"><thead><tr><th>Symbol</th><th>Side</th><th>Type</th><th>Amount</th><th>Price</th><th>Status</th></tr></thead><tbody>${rows.map((o) => `<tr>${td('Symbol', o.symbol || '--')}${td('Side', String(o.side || '--').toUpperCase())}${td('Type', String(o.type || '--').toUpperCase())}${td('Amount', Number(o.amount || o.remaining || 0).toFixed(5))}${td('Price', Number(o.price || 0).toFixed(5))}${td('Status', o.status || 'open')}</tr>`).join('')}</tbody></table></div>`;
}

function configForm(leader) {
  const config = leader.config || {};
  const stop = config.position_stop_loss || {};
  const name = leader.name;
  return `<article class="panel leader"><div class="toolbar"><div><strong>${name}</strong><span class="muted"> ${config.execution_account || 'default'}${config.enabled === false ? ' · disabled' : ''}</span></div><button data-save="${name}">Save changes</button></div><div class="config"><label>Copy address<input data-k="address" data-name="${name}" value="${config.address ?? ''}"></label><label>Coefficient<input type="number" min="0" step="any" data-k="coefficient" data-name="${name}" value="${config.coefficient ?? ''}"></label><label>Total margin<input type="number" min="0" step="any" data-k="total_margin" data-name="${name}" value="${config.total_margin ?? ''}"></label><label>Late entry %<input type="number" min="0" step="any" data-k="late_entry_offset_pct" data-name="${name}" value="${config.late_entry_offset_pct ?? ''}"></label><label class="check"><input type="checkbox" data-k="enabled" data-name="${name}" ${config.enabled ? 'checked' : ''}> Enabled</label><label class="check"><input type="checkbox" data-k="late_entry_enabled" data-name="${name}" ${config.late_entry_enabled ? 'checked' : ''}> Late entries</label><label class="check"><input type="checkbox" data-k="position_stop_loss.enabled" data-name="${name}" ${stop.enabled ? 'checked' : ''}> Position stop loss</label><label>Max loss USDT<input type="number" min="0" step="any" data-k="position_stop_loss.max_loss_usdt" data-name="${name}" value="${stop.max_loss_usdt ?? ''}"></label></div><p class="config-message muted" data-message="${name}" aria-live="polite"></p></article>`;
}

let configsSignature;

// Rebuilding the form on every SSE tick would drop values the user is still
// typing (most visible on mobile), so only replace it when the server-side
// configuration actually changed.
function renderConfigs(leaders) {
  const signature = JSON.stringify(leaders.map((leader) => [leader.name, leader.config]));
  if (signature === configsSignature) return;
  configsSignature = signature;
  $('#configs').innerHTML = leaders.map(configForm).join('') || '<p class="muted">No leaders configured.</p>';
}

function leaderEnabled(leader) { return leader?.enabled !== false && leader?.config?.enabled !== false; }

function render(data) {
  const account = data.accounts?.[0] || {};
  const status = data.status || {};
  $('#equity').textContent = money(account.balance ?? account.equity);
  $('#unrealized').textContent = money(account.unrealized_pnl);
  $('#unrealized').className = cls(account.unrealized_pnl);
  const positionCount = $('#position-count');
  if (positionCount) positionCount.textContent = (account.positions || []).length;
  $('#mode').textContent = status.risk_locked ? 'Risk locked' : status.paused ? 'Paused' : 'Running';
  const hardStop = status.hard_stop || {};
  const stops = status.position_stop_loss || [];
  const stopText = hardStop.max_daily_loss_usdt != null ? `Daily ${hardStop.max_daily_loss_usdt} USDT` : hardStop.max_daily_drawdown_pct != null ? `Daily ${hardStop.max_daily_drawdown_pct}%` : stops.length ? `Position ${stops[0].max_loss_usdt ?? 'configured'} USDT` : 'Off';
  $('#stop-loss').textContent = stopText;
  $('#status').textContent = `Live · ${new Date(data.updated_at * 1000).toLocaleTimeString()}`;
  $('#account').innerHTML = `<p class="muted">${account.name || 'Account'} · ${account.positions?.length || 0} open positions</p>${table(account.positions, { breakeven: true })}`;
  $('#orders').innerHTML = orders(account.orders);
  $('#leaders').innerHTML = (data.leaders || []).filter(leaderEnabled).map((leader) => `<article class="panel leader"><div class="toolbar"><div><strong>${leader.name}</strong> <span class="muted">${leader.address} · ${leader.positions?.length || 0} open positions</span></div></div>${table(leader.positions)}</article>`).join('') || '<p class="muted">No leaders configured.</p>';
  renderConfigs(data.leaders || []);
  $('#toggle').textContent = status.paused ? 'Resume entries' : 'Pause entries';
  if (!$('#performance-page')?.hidden) loadPerformance();
}

const perf = { preset: 'all', since: null, until: null, charts: {}, last: 0 };
const PALETTES = {
  dark: { line: '#3987e5', up: '#48d597', down: '#ff7373', grid: '#2b3748', ink: '#e8edf5', muted: '#91a0b5', surface: '#171e29' },
  light: { line: '#2f6fd0', up: '#0b7d50', down: '#c83838', grid: '#d5dde7', ink: '#1b2430', muted: '#5f7085', surface: '#ffffff' },
};
let themeName = document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
const CHART = { ...PALETTES[themeName] };

function setTheme(name) {
  themeName = name === 'light' ? 'light' : 'dark';
  document.documentElement.dataset.theme = themeName;
  try { localStorage.setItem('theme', themeName); } catch (_) { /* storage unavailable */ }
  Object.assign(CHART, PALETTES[themeName]);
  const button = $('#theme');
  if (button) {
    button.textContent = themeName === 'dark' ? '☀️' : '🌙';
    button.title = themeName === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';
  }
  if (!$('#performance-page')?.hidden) loadPerformance(true);
}

function esc(value) { return String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
function signed(value) { const n = Number(value || 0); return `${n >= 0 ? '+' : ''}${n.toFixed(2)}`; }
function compact(value) { const n = Number(value || 0); return Math.abs(n) >= 1000 ? `${(n / 1000).toFixed(1).replace(/\.0$/, '')}k` : String(Math.round(n)); }

function toDateInput(epoch) {
  const d = new Date(epoch * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function dayToEpoch(value, endOfDay) {
  if (!value) return null;
  const d = new Date(`${value}T00:00:00`);
  if (Number.isNaN(d.getTime())) return null;
  if (endOfDay) d.setHours(23, 59, 59, 999);
  return Math.floor(d.getTime() / 1000);
}

function presetRange(preset) {
  const now = Math.floor(Date.now() / 1000);
  if (preset === 'today') { const d = new Date(); d.setHours(0, 0, 0, 0); return { since: Math.floor(d.getTime() / 1000), until: now }; }
  if (preset === '7d') return { since: now - 7 * 86400, until: now };
  if (preset === '30d') return { since: now - 30 * 86400, until: now };
  return { since: null, until: null };
}

function setPreset(name) {
  perf.preset = name;
  ({ since: perf.since, until: perf.until } = presetRange(name));
  document.querySelectorAll('#perf-presets .chip').forEach((chip) => chip.classList.toggle('active', chip.dataset.preset === name));
  const since = $('#perf-since'); const until = $('#perf-until');
  if (since) since.value = perf.since ? toDateInput(perf.since) : '';
  if (until) until.value = perf.until ? toDateInput(perf.until) : '';
  loadPerformance(true);
}

function perfMetrics(p) {
  const tile = (label, value, klass = '') => `<div><small>${label}</small><strong class="${klass}">${value}</strong></div>`;
  return `<div class="kpis">${tile('Net PnL', money(p.net_pnl), cls(p.net_pnl))}${tile('Return', `${Number(p.return_pct || 0).toFixed(2)}%`, cls(p.return_pct))}${tile('Max drawdown', `${money(p.max_drawdown)} (${Number(p.max_drawdown_pct || 0).toFixed(2)}%)`, 'negative')}${tile('Gross / fees', `${money(p.gross_pnl)} / ${money(p.fees)}`)}</div>`;
}

function leaderTable(leaders) {
  if (!leaders?.length) return '<p class="muted">No realized trades in this period.</p>';
  const rows = leaders.map((x) => `<tr class="${x.leader === 'unattributed' ? 'muted' : ''}">${td('Trader', esc(x.leader))}${td('Net PnL', money(x.net_pnl), cls(x.net_pnl))}${td('Gross', money(x.gross_pnl))}${td('Fees', money(x.fees))}${td('Contribution', `${Number(x.contribution_pct).toFixed(2)}%`)}</tr>`).join('');
  return `<div class="table-wrap"><table class="table"><thead><tr><th>Trader</th><th>Net PnL</th><th>Gross</th><th>Fees</th><th>Contribution</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function positionTable(rows) {
  if (!rows?.length) return '<p class="muted">No open positions.</p>';
  const body = rows.map((p) => `<tr>${td('Symbol', esc(p.symbol))}${td('Side', esc(p.side))}${td('Size', Number(p.size || 0).toFixed(5))}${td('Leverage', `${Number(p.leverage || 0).toFixed(1)}x`)}${td('Entry', Number(p.entry_price || 0).toFixed(5))}${td('Mark', Number(p.mark_price || 0).toFixed(5))}${td('Unrealized PnL', money(p.unrealized_pnl), cls(p.unrealized_pnl))}${td('ROI', `${Number(p.roi || 0).toFixed(2)}%`, cls(p.roi))}${td('Trader', p.leader === 'unattributed' ? '<span class="muted">unattributed</span>' : esc(p.leader))}</tr>`).join('');
  return `<div class="table-wrap"><table class="table"><thead><tr><th>Symbol</th><th>Side</th><th>Size</th><th>Leverage</th><th>Entry</th><th>Mark</th><th>Unrealized PnL</th><th>ROI</th><th>Trader</th></tr></thead><tbody>${body}</tbody></table></div>`;
}

function disposeCharts() {
  Object.values(perf.charts).forEach((chart) => { try { chart.dispose(); } catch (_) { /* already gone */ } });
  perf.charts = {};
}

function tooltipShell() {
  return { backgroundColor: CHART.surface, borderColor: CHART.grid, textStyle: { color: CHART.ink } };
}

function renderEquity(node, curve) {
  if (typeof echarts === 'undefined' || !node || !node.clientWidth) return;
  const chart = perf.charts.equity = echarts.getInstanceByDom(node) || echarts.init(node);
  const rows = curve || [];
  chart.setOption({
    backgroundColor: 'transparent',
    grid: { left: 8, right: 16, top: 16, bottom: 8, containLabel: true },
    tooltip: { trigger: 'axis', ...tooltipShell(), axisPointer: { type: 'line', lineStyle: { color: CHART.grid } },
      formatter: (items) => { const row = rows[items[0]?.dataIndex] || {}; return `<strong>${money(row.adjusted_equity)}</strong> adjusted equity<br><span class="muted">equity</span> ${money(row.equity)} · <span class="muted">drawdown</span> ${money(row.drawdown)}<br><span class="muted">${new Date((row.timestamp || 0) * 1000).toLocaleString()}</span>`; } },
    xAxis: { type: 'time', axisLine: { lineStyle: { color: CHART.grid } }, axisLabel: { color: CHART.muted }, splitLine: { show: false } },
    yAxis: { type: 'value', scale: true, axisLabel: { color: CHART.muted }, splitLine: { lineStyle: { color: CHART.grid, type: 'solid' } } },
    series: [{ type: 'line', data: rows.map((x) => [Number(x.timestamp) * 1000, Number(x.adjusted_equity)]), showSymbol: false, lineStyle: { color: CHART.line, width: 2 }, itemStyle: { color: CHART.line }, areaStyle: { color: 'rgba(57,135,229,0.10)' } }],
  }, true);
  chart.resize();
}

function renderLeaderBars(node, leaders) {
  if (typeof echarts === 'undefined' || !node || !node.clientWidth) return;
  const chart = perf.charts.leaders = echarts.getInstanceByDom(node) || echarts.init(node);
  const rows = (leaders || []).slice().reverse();
  const data = rows.map((x) => {
    const positive = Number(x.net_pnl || 0) >= 0;
    return { value: Number(x.net_pnl || 0), itemStyle: { color: positive ? CHART.up : CHART.down, borderRadius: positive ? [0, 4, 4, 0] : [4, 0, 0, 4] }, label: { position: positive ? 'right' : 'left' } };
  });
  chart.setOption({
    backgroundColor: 'transparent',
    grid: { left: 8, right: 64, top: 8, bottom: 8, containLabel: true },
    tooltip: { trigger: 'item', ...tooltipShell(), formatter: (item) => `<strong>${money(item.value)}</strong> · ${esc(rows[item.dataIndex]?.leader)}` },
    xAxis: { type: 'value', splitNumber: 3, axisLabel: { color: CHART.muted, formatter: compact }, splitLine: { lineStyle: { color: CHART.grid, type: 'solid' } } },
    yAxis: { type: 'category', data: rows.map((x) => x.leader), axisLine: { lineStyle: { color: CHART.grid } }, axisLabel: { color: CHART.muted }, axisTick: { show: false } },
    series: [{ type: 'bar', data, barMaxWidth: 24, label: { show: true, color: CHART.ink, formatter: (item) => signed(item.value) } }],
  }, true);
  chart.resize();
}

function renderPerformance(p) {
  const node = $('#performance');
  disposeCharts();
  const charts = typeof echarts === 'undefined' ? '<p class="muted">Charts unavailable (chart library failed to load).</p>' : '<div class="charts"><div class="panel"><h3>Account equity</h3><div id="chart-equity" class="chart"></div></div><div class="panel"><h3>Realized PnL by trader</h3><div id="chart-leaders" class="chart"></div></div></div>';
  const w = p.window || {};
  const windowText = w.since || w.until ? `${w.since ? toDateInput(w.since) : 'start'} → ${w.until ? toDateInput(w.until) : 'now'}` : 'All time';
  node.innerHTML = `${perfMetrics(p)}${charts}<h3>By trader</h3>${leaderTable(p.leaders)}<p class="muted">Trader PnL is attributed by matching exchange fills to local orders; records that cannot be matched stay under <em>unattributed</em>.</p><h3>Open positions by trader</h3>${positionTable(p.positions)}<p class="muted">Ownership comes from the engine's symbol claims; positions it never claimed show as <em>unattributed</em>. Window: ${windowText}.</p>`;
  if (typeof echarts !== 'undefined') { renderEquity($('#chart-equity'), p.curve); renderLeaderBars($('#chart-leaders'), p.leaders); }
}

async function loadPerformance(force) {
  const page = $('#performance-page');
  const node = $('#performance');
  if (!page || page.hidden || !node) return;
  const now = Date.now();
  if (!force && now - perf.last < 15000) return;
  perf.last = now;
  const params = new URLSearchParams();
  if (perf.since != null) params.set('since', perf.since);
  if (perf.until != null) params.set('until', perf.until);
  const query = params.toString();
  try {
    renderPerformance(await (await fetch(`/api/v1/performance/overview${query ? `?${query}` : ''}`)).json());
  } catch (_) { node.innerHTML = '<p class="error">Performance unavailable.</p>'; }
}

async function load() {
  const response = await fetch('/api/v1/dashboard');
  if (!response.ok) throw Error(`Dashboard request failed: ${response.status}`);
  render(await response.json());
}

function connect() {
  if (source) source.close();
  source = new EventSource('/api/v1/events');
  source.onmessage = (event) => render(JSON.parse(event.data));
  source.onerror = () => { $('#status').textContent = 'Reconnecting'; };
}

$('#toggle').onclick = async () => {
  const paused = $('#toggle').textContent.startsWith('Pause');
  await fetch(`/api/v1/control/${paused ? 'pause' : 'resume'}`, { method: 'POST' });
  await load();
};

document.addEventListener('click', async (event) => {
  const tab = event.target.closest('[data-page]');
  if (tab) {
    document.querySelectorAll('.tab').forEach((item) => item.classList.remove('active'));
    tab.classList.add('active');
    document.querySelectorAll('#monitor-page,#performance-page,#config-page').forEach((page) => { page.hidden = page.id !== tab.dataset.page; });
    if (tab.dataset.page === 'performance-page') loadPerformance(true);
    return;
  }
  const breakeven = event.target.closest('[data-breakeven]');
  if (breakeven) {
    const symbol = breakeven.dataset.breakeven;
    const enabled = breakeven.dataset.enabled === 'true';
    breakeven.disabled = true;
    try {
      const response = await fetch('/api/v1/positions/breakeven', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol, enabled }),
      });
      if (!response.ok) throw Error((await response.text()).trim() || `request failed: ${response.status}`);
      await load();
    } catch (error) {
      breakeven.disabled = false;
      alert(`Breakeven failed for ${symbol}: ${error.message}`);
    }
    return;
  }
  const button = event.target.closest('[data-save]');
  if (!button) return;
  const name = button.dataset.save;
  const payload = {};
  document.querySelectorAll(`[data-name="${name}"]`).forEach((input) => {
    const key = input.dataset.k;
    const value = input.type === 'checkbox' ? input.checked : key === 'address' ? input.value.trim() : Number(input.value);
    if (key.startsWith('position_stop_loss.')) {
      payload.position_stop_loss = payload.position_stop_loss || {};
      payload.position_stop_loss[key.split('.')[1]] = value;
    } else payload[key] = value;
  });
  const message = document.querySelector(`[data-message="${name}"]`);
  const response = await fetch(`/api/v1/leaders/${encodeURIComponent(name)}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
  if (!response.ok) { message.textContent = await response.text(); message.className = 'config-message error'; return; }
  await load();
  const updatedMessage = document.querySelector(`[data-message="${name}"]`);
  updatedMessage.textContent = 'Saved and applied to the running engine.';
  updatedMessage.className = 'config-message positive';
});

document.querySelectorAll('#perf-presets .chip').forEach((chip) => { chip.onclick = () => setPreset(chip.dataset.preset); });

const perfApply = $('#perf-apply');
if (perfApply) perfApply.onclick = () => {
  perf.preset = 'custom';
  perf.since = dayToEpoch($('#perf-since').value, false);
  perf.until = dayToEpoch($('#perf-until').value, true);
  document.querySelectorAll('#perf-presets .chip').forEach((chip) => chip.classList.remove('active'));
  loadPerformance(true);
};

window.addEventListener('resize', () => Object.values(perf.charts).forEach((chart) => chart.resize()));

const themeButton = $('#theme');
if (themeButton) themeButton.onclick = () => setTheme(themeName === 'dark' ? 'light' : 'dark');
setTheme(themeName);

load().then(connect).catch(() => { $('#status').textContent = 'Dashboard unavailable'; });
