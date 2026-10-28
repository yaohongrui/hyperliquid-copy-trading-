const $ = (selector) => document.querySelector(selector);
let source;

function money(value) { return `${Number(value || 0).toFixed(2)} USDT`; }
function cls(value) { return Number(value) >= 0 ? 'positive' : 'negative'; }
function td(label, value, klass = '', align = '') {
  const classes = [align, klass].filter(Boolean).join(' ');
  return `<td data-label="${label}"${classes ? ` class="${classes}"` : ''}>${value}</td>`;
}

function sideChip(side) {
  const value = String(side || '').toUpperCase();
  const kind = ['SHORT', 'SELL'].includes(value) ? 'short' : ['LONG', 'BUY'].includes(value) ? 'long' : 'flat';
  return `<span class="side ${kind}">${esc(value || '--')}</span>`;
}

function symbol(name) { return `<span class="sym">${esc(name || '--')}</span>`; }
function empty(text) { return `<p class="empty">${text}</p>`; }

function table(rows, options = {}) {
  if (rows?.length && rows[0]?.error) return `<p class="error">Positions unavailable: ${esc(rows[0].error)}</p>`;
  if (!rows?.length) return empty('No open positions.');
  const action = options.breakeven ? '<th class="act">Action</th>' : '';
  return `<div class="table-wrap"><table class="table"><thead><tr><th>Symbol</th><th>Side</th><th class="num">Size</th><th class="num">Entry</th><th class="num">Mark</th><th class="num">PnL</th><th class="num">ROI</th>${action}</tr></thead><tbody>${rows.map((p) => `<tr>${td('Symbol', symbol(p.symbol))}${td('Side', sideChip(p.side))}${td('Size', Number(p.size || 0).toFixed(5), '', 'num')}${td('Entry', Number(p.entry_price || 0).toFixed(5), '', 'num')}${td('Mark', Number(p.mark_price || 0).toFixed(5), '', 'num')}${td('PnL', money(p.unrealized_pnl), cls(p.unrealized_pnl), 'num')}${td('ROI', `${Number(p.roi || 0).toFixed(2)}%`, cls(p.roi), 'num')}${options.breakeven ? td('Action', `<button class="table-action${p.breakeven ? ' active' : ''}" data-breakeven="${esc(p.symbol || '')}" data-enabled="${p.breakeven ? 'false' : 'true'}">${p.breakeven ? 'Cancel breakeven' : 'Breakeven'}</button>`, '', 'act') : ''}</tr>`).join('')}</tbody></table></div>`;
}

function orders(rows) {
  if (!rows?.length) return empty('No open orders.');
  if (rows[0]?.error) return `<p class="error">Orders unavailable: ${esc(rows[0].error)}</p>`;
  return `<div class="table-wrap"><table class="table"><thead><tr><th>Symbol</th><th>Side</th><th>Type</th><th class="num">Amount</th><th class="num">Price</th><th>Status</th></tr></thead><tbody>${rows.map((o) => { const price = o.triggerPrice ?? o.stopPrice ?? o.price; return `<tr>${td('Symbol', symbol(o.symbol))}${td('Side', sideChip(o.side))}${td('Type', esc(String(o.info?.orderType || o.type || '--').toUpperCase()))}${td('Amount', Number(o.amount || o.remaining || 0).toFixed(5), '', 'num')}${td('Price', Number(price || 0).toFixed(5), '', 'num')}${td('Status', esc(o.status || 'open'))}</tr>`; }).join('')}</tbody></table></div>`;
}

function configForm(leader) {
  const config = leader.config || {};
  const stop = config.position_stop_loss || {};
  const name = leader.name;
  const disabled = config.enabled === false;
  // Binance leaders are identified by portfolio_id and must not expose an
  // editable address; the server also rejects an address for them.
  const hyperliquid = !config.source || config.source === 'hyperliquid';
  const meta = hyperliquid ? esc(config.execution_account || 'default')
    : `${esc(config.source)} · ${esc(config.execution_account || 'default')}`;
  const identity = hyperliquid
    ? `<label>Copy address<input data-k="address" data-name="${esc(name)}" value="${esc(config.address ?? '')}"></label>`
    : `<label>Portfolio ID<input readonly value="${esc(config.portfolio_id ?? '')}"></label>`;
  return `<article class="panel leader config-card${disabled ? ' is-disabled' : ''}"><div class="toolbar"><div class="leader-id"><span class="leader-tag">${esc(name)}</span><span class="meta">${meta}</span>${disabled ? '<span class="badge">Disabled</span>' : ''}</div><button data-save="${esc(name)}">Save changes</button></div><div class="config">${identity}<label>Coefficient<input type="number" min="0" step="any" data-k="coefficient" data-name="${esc(name)}" value="${config.coefficient ?? ''}"></label><label>Total margin<input type="number" min="0" step="any" data-k="total_margin" data-name="${esc(name)}" value="${config.total_margin ?? ''}"></label><label>Late entry %<input type="number" min="0" step="any" data-k="late_entry_offset_pct" data-name="${esc(name)}" value="${config.late_entry_offset_pct ?? ''}"></label><label class="check"><input type="checkbox" data-k="enabled" data-name="${esc(name)}" ${config.enabled ? 'checked' : ''}><span>Enabled</span></label><label class="check"><input type="checkbox" data-k="late_entry_enabled" data-name="${esc(name)}" ${config.late_entry_enabled ? 'checked' : ''}><span>Late entries</span></label><label class="check"><input type="checkbox" data-k="position_stop_loss.enabled" data-name="${esc(name)}" ${stop.enabled ? 'checked' : ''}><span>Position stop loss</span></label><label>Max loss USDT<input type="number" min="0" step="any" data-k="position_stop_loss.max_loss_usdt" data-name="${esc(name)}" value="${stop.max_loss_usdt ?? ''}"></label></div><p class="config-message muted" data-message="${esc(name)}" aria-live="polite"></p></article>`;
}

let configsSignature;

// Rebuilding the form on every SSE tick would drop values the user is still
// typing (most visible on mobile), so only replace it when the server-side
// configuration actually changed.
function renderConfigs(leaders) {
  const signature = JSON.stringify(leaders.map((leader) => [leader.name, leader.config]));
  if (signature === configsSignature) return;
  configsSignature = signature;
  $('#configs').innerHTML = leaders.map(configForm).join('') || empty('No leaders configured.');
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
  $('#mode').dataset.state = status.risk_locked ? 'locked' : status.paused ? 'paused' : 'running';
  const hardStop = status.hard_stop || {};
  const stops = status.position_stop_loss || [];
  const stopText = hardStop.max_daily_loss_usdt != null ? `Daily ${hardStop.max_daily_loss_usdt} USDT` : hardStop.max_daily_drawdown_pct != null ? `Daily ${hardStop.max_daily_drawdown_pct}%` : stops.length ? `Position ${stops[0].max_loss_usdt ?? 'configured'} USDT` : 'Off';
  $('#stop-loss').textContent = stopText;
  setLinkState('live', `Live · ${new Date(data.updated_at * 1000).toLocaleTimeString()}`);
  $('#account').innerHTML = `<div class="panel-meta"><strong>${esc(account.name || 'Account')}</strong><span class="sep">·</span><span>${account.positions?.length || 0} open positions</span></div>${table(account.positions, { breakeven: true })}`;
  $('#orders').innerHTML = orders(account.orders);
  $('#leaders').innerHTML = (data.leaders || []).filter(leaderEnabled).map((leader) => `<article class="panel leader"><div class="leader-head"><div class="leader-id"><span class="leader-tag">${esc(leader.name)}</span><span class="addr" title="${esc(leader.address || leader.config?.portfolio_id || '')}">${esc(leader.address || leader.config?.portfolio_id || '')}</span><span class="sep">·</span><span class="meta">${leader.positions?.length || 0} open positions</span></div></div>${table(leader.positions)}</article>`).join('') || empty('No leaders configured.');
  renderConfigs(data.leaders || []);
  $('#toggle').textContent = status.paused ? 'Resume entries' : 'Pause entries';
  if (!$('#performance-page')?.hidden) loadPerformance();
}

const perf = { preset: 'all', since: null, until: null, charts: {}, last: 0 };
const CHART_FONT = 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
const PALETTES = {
  dark: {
    line: '#78a9ee', areaTop: 'rgba(120,169,238,.18)', areaBottom: 'rgba(120,169,238,0)',
    up: '#4ad79b', down: '#ff7a7a', grid: '#202c3b', ink: '#e9eff7', muted: '#93a2b5', surface: '#151e2a',
  },
  light: {
    line: '#2b64bd', areaTop: 'rgba(43,100,189,.13)', areaBottom: 'rgba(43,100,189,0)',
    up: '#0b7d50', down: '#bd3030', grid: '#dae2ec', ink: '#121c28', muted: '#5b6b7d', surface: '#ffffff',
  },
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
    const next = themeName === 'dark' ? 'light' : 'dark';
    button.title = `Switch to ${next} mode`;
    button.setAttribute('aria-label', `Switch to ${next} mode`);
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
  return {
    backgroundColor: CHART.surface,
    borderColor: CHART.grid,
    borderWidth: 1,
    padding: [8, 10],
    textStyle: { color: CHART.ink, fontSize: 12, fontFamily: CHART_FONT },
    extraCssText: 'border-radius:8px;box-shadow:0 12px 32px -12px rgba(0,0,0,.45);',
  };
}

function renderEquity(node, curve) {
  if (typeof echarts === 'undefined' || !node || !node.clientWidth) return;
  const chart = perf.charts.equity = echarts.getInstanceByDom(node) || echarts.init(node);
  const rows = curve || [];
  chart.setOption({
    backgroundColor: 'transparent',
    animationDuration: 280,
    animationEasing: 'cubicOut',
    aria: { enabled: true },
    textStyle: { fontFamily: CHART_FONT },
    grid: { left: 8, right: 16, top: 16, bottom: 8, containLabel: true },
    tooltip: { trigger: 'axis', ...tooltipShell(), axisPointer: { type: 'line', lineStyle: { color: CHART.grid } },
      formatter: (items) => { const row = rows[items[0]?.dataIndex] || {}; return `<strong>${money(row.adjusted_equity)}</strong> adjusted equity<br><span class="muted">equity</span> ${money(row.equity)} · <span class="muted">drawdown</span> ${money(row.drawdown)}<br><span class="muted">${new Date((row.timestamp || 0) * 1000).toLocaleString()}</span>`; } },
    xAxis: { type: 'time', axisLine: { lineStyle: { color: CHART.grid } }, axisTick: { show: false }, axisLabel: { color: CHART.muted, fontSize: 11 }, splitLine: { show: false } },
    yAxis: { type: 'value', scale: true, axisLabel: { color: CHART.muted, fontSize: 11 }, splitLine: { lineStyle: { color: CHART.grid, type: 'dashed' } } },
    series: [{
      type: 'line',
      data: rows.map((x) => [Number(x.timestamp) * 1000, Number(x.adjusted_equity)]),
      showSymbol: false,
      lineStyle: { color: CHART.line, width: 2 },
      itemStyle: { color: CHART.line },
      areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: CHART.areaTop }, { offset: 1, color: CHART.areaBottom }]) },
    }],
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
    animationDuration: 280,
    animationEasing: 'cubicOut',
    aria: { enabled: true },
    textStyle: { fontFamily: CHART_FONT },
    grid: { left: 8, right: 64, top: 8, bottom: 8, containLabel: true },
    tooltip: { trigger: 'item', ...tooltipShell(), formatter: (item) => `<strong>${money(item.value)}</strong> · ${esc(rows[item.dataIndex]?.leader)}` },
    xAxis: { type: 'value', splitNumber: 3, axisLabel: { color: CHART.muted, fontSize: 11, formatter: compact }, splitLine: { lineStyle: { color: CHART.grid, type: 'dashed' } } },
    yAxis: { type: 'category', data: rows.map((x) => x.leader), axisLine: { lineStyle: { color: CHART.grid } }, axisLabel: { color: CHART.muted, fontSize: 11 }, axisTick: { show: false } },
    series: [{ type: 'bar', data, barMaxWidth: 22, label: { show: true, color: CHART.ink, fontSize: 11, formatter: (item) => signed(item.value) } }],
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

function setLinkState(state, text) {
  const pill = $('#status');
  if (pill) pill.dataset.state = state;
  const label = $('#status-text');
  if (label) label.textContent = text;
}

let toastTimer;
function toast(message, tone = 'error') {
  const node = $('#toast');
  if (!node) return;
  node.textContent = message;
  node.dataset.tone = tone;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, 6000);
}

function connect() {
  if (source) source.close();
  source = new EventSource('/api/v1/events');
  source.onopen = () => setLinkState('live', 'Live');
  source.onmessage = (event) => render(JSON.parse(event.data));
  source.onerror = () => setLinkState('reconnecting', 'Reconnecting');
}

$('#toggle').onclick = async () => {
  const button = $('#toggle');
  const paused = button.textContent.startsWith('Pause');
  button.disabled = true;
  try {
    const response = await fetch(`/api/v1/control/${paused ? 'pause' : 'resume'}`, { method: 'POST' });
    if (!response.ok) throw Error((await response.text()).trim() || `request failed: ${response.status}`);
    await load();
  } catch (error) {
    toast(`Could not ${paused ? 'pause' : 'resume'} entries: ${error.message}`);
  } finally {
    button.disabled = false;
  }
};

document.addEventListener('click', async (event) => {
  const tab = event.target.closest('[data-page]');
  if (tab) {
    document.querySelectorAll('.tab').forEach((item) => {
      const selected = item === tab;
      item.classList.toggle('active', selected);
      item.setAttribute('aria-selected', String(selected));
    });
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
      toast(`Breakeven failed for ${symbol}: ${error.message}`);
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
  button.disabled = true;
  try {
    const message = document.querySelector(`[data-message="${name}"]`);
    const response = await fetch(`/api/v1/leaders/${encodeURIComponent(name)}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    if (!response.ok) { message.textContent = await response.text(); message.className = 'config-message error'; return; }
    await load();
    const updatedMessage = document.querySelector(`[data-message="${name}"]`);
    updatedMessage.textContent = 'Saved and applied to the running engine.';
    updatedMessage.className = 'config-message positive';
  } catch (error) {
    toast(`Could not save ${name}: ${error.message}`);
  } finally {
    button.disabled = false;
  }
});

const tablist = document.querySelector('.tabs');
if (tablist) tablist.addEventListener('keydown', (event) => {
  if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
  const tabs = [...tablist.querySelectorAll('.tab')];
  const index = tabs.indexOf(document.activeElement);
  if (index < 0) return;
  event.preventDefault();
  const next = tabs[(index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length];
  next.focus();
  next.click();
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

load().then(connect).catch(() => { setLinkState('offline', 'Dashboard unavailable'); });
