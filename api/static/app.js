const $ = (selector) => document.querySelector(selector);
let source;

function money(value) { return `${Number(value || 0).toFixed(2)} USDT`; }
function cls(value) { return Number(value) >= 0 ? 'positive' : 'negative'; }

function table(rows) {
  if (!rows?.length) return '<p class="muted">No open positions.</p>';
  return `<div class="table-wrap"><table class="table"><thead><tr><th>Symbol</th><th>Side</th><th>Size</th><th>Leverage</th><th>Entry</th><th>Mark</th><th>PnL</th><th>ROI</th></tr></thead><tbody>${rows.map((p) => `<tr><td>${p.symbol || '--'}</td><td>${p.side || '--'}</td><td>${Number(p.size || 0).toFixed(5)}</td><td>${Number(p.leverage || 0).toFixed(1)}x</td><td>${Number(p.entry_price || 0).toFixed(5)}</td><td>${Number(p.mark_price || 0).toFixed(5)}</td><td class="${cls(p.unrealized_pnl)}">${money(p.unrealized_pnl)}</td><td class="${cls(p.roi)}">${Number(p.roi || 0).toFixed(2)}%</td></tr>`).join('')}</tbody></table></div>`;
}

function orders(rows) {
  if (!rows?.length) return '<p class="muted">No open orders.</p>';
  if (rows[0]?.error) return `<p class="error">Orders unavailable: ${rows[0].error}</p>`;
  return `<div class="table-wrap"><table class="table"><thead><tr><th>Symbol</th><th>Side</th><th>Type</th><th>Amount</th><th>Price</th><th>Status</th></tr></thead><tbody>${rows.map((o) => `<tr><td>${o.symbol || '--'}</td><td>${String(o.side || '--').toUpperCase()}</td><td>${String(o.type || '--').toUpperCase()}</td><td>${Number(o.amount || o.remaining || 0).toFixed(5)}</td><td>${Number(o.price || 0).toFixed(5)}</td><td>${o.status || 'open'}</td></tr>`).join('')}</tbody></table></div>`;
}

function configForm(leader) {
  const config = leader.config || {};
  const stop = config.position_stop_loss || {};
  const name = leader.name;
  return `<article class="panel leader"><div class="toolbar"><div><strong>${name}</strong><span class="muted"> ${config.execution_account || 'default'}</span></div><button data-save="${name}">Save changes</button></div><div class="config"><label>Copy address<input data-k="address" data-name="${name}" value="${config.address ?? ''}"></label><label>Coefficient<input type="number" min="0" step="any" data-k="coefficient" data-name="${name}" value="${config.coefficient ?? ''}"></label><label>Total margin<input type="number" min="0" step="any" data-k="total_margin" data-name="${name}" value="${config.total_margin ?? ''}"></label><label>Late entry %<input type="number" min="0" step="any" data-k="late_entry_offset_pct" data-name="${name}" value="${config.late_entry_offset_pct ?? ''}"></label><label class="check"><input type="checkbox" data-k="enabled" data-name="${name}" ${config.enabled ? 'checked' : ''}> Enabled</label><label class="check"><input type="checkbox" data-k="late_entry_enabled" data-name="${name}" ${config.late_entry_enabled ? 'checked' : ''}> Late entries</label><label class="check"><input type="checkbox" data-k="position_stop_loss.enabled" data-name="${name}" ${stop.enabled ? 'checked' : ''}> Position stop loss</label><label>Max loss USDT<input type="number" min="0" step="any" data-k="position_stop_loss.max_loss_usdt" data-name="${name}" value="${stop.max_loss_usdt ?? ''}"></label></div><p class="config-message muted" data-message="${name}" aria-live="polite"></p></article>`;
}

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
  $('#account').innerHTML = `<p class="muted">${account.name || 'Account'} · ${account.positions?.length || 0} open positions</p>${table(account.positions)}`;
  $('#orders').innerHTML = orders(account.orders);
  $('#leaders').innerHTML = (data.leaders || []).map((leader) => `<article class="panel leader"><div class="toolbar"><div><strong>${leader.name}</strong> <span class="muted">${leader.address} · ${leader.positions?.length || 0} open positions</span></div></div>${table(leader.positions)}</article>`).join('') || '<p class="muted">No leaders configured.</p>';
  $('#configs').innerHTML = (data.leaders || []).map(configForm).join('') || '<p class="muted">No leaders configured.</p>';
  $('#toggle').textContent = status.paused ? 'Resume entries' : 'Pause entries';
  loadPerformance();
}

async function loadPerformance() {
  const node = $('#performance');
  if (!node) return;
  try {
    const p = await (await fetch('/api/v1/performance/overview')).json();
    node.innerHTML = `<div class="metrics"><div><small>Net PnL</small><strong class="${cls(p.net_pnl)}">${money(p.net_pnl)}</strong></div><div><small>Return</small><strong>${Number(p.return_pct || 0).toFixed(2)}%</strong></div><div><small>Max drawdown</small><strong class="negative">${money(p.max_drawdown)} (${Number(p.max_drawdown_pct || 0).toFixed(2)}%)</strong></div><div><small>Fees / funding</small><strong>${money(p.fees)} / ${money(p.funding)}</strong></div></div><h3>By trader</h3>${p.leaders?.length ? `<div class="table-wrap"><table class="table"><thead><tr><th>Trader</th><th>Net PnL</th><th>Contribution</th><th>Win rate</th><th>Trades</th></tr></thead><tbody>${p.leaders.map(x => `<tr><td>${x.leader}</td><td class="${cls(x.net_pnl)}">${money(x.net_pnl)}</td><td>${Number(x.contribution_pct).toFixed(2)}%</td><td>${(Number(x.win_rate) * 100).toFixed(1)}%</td><td>${x.trades}</td></tr>`).join('')}</tbody></table></div>` : '<p class="muted">No realized trades yet.</p>'}`;
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
    if (tab.dataset.page === 'performance-page') loadPerformance();
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

load().then(connect).catch(() => { $('#status').textContent = 'Dashboard unavailable'; });
