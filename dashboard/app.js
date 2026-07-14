const POLL_MS = 3000;
const tooltip = document.getElementById('tooltip');

const fmtUsd = (v) => {
  const n = Number(v || 0);
  const sign = n < 0 ? '-' : '';
  return `${sign}$${Math.abs(n).toFixed(2)}`;
};
const fmtSigned = (v) => {
  const n = Number(v || 0);
  return `${n >= 0 ? '+' : ''}${n.toFixed(2)}`;
};

function animateValue(el, to, formatter) {
  const from = parseFloat(el.dataset.raw || '0');
  const start = performance.now();
  const dur = 500;
  function step(t) {
    const p = Math.min(1, (t - start) / dur);
    const eased = 1 - Math.pow(1 - p, 3);
    const v = from + (to - from) * eased;
    el.textContent = formatter(v);
    if (p < 1) requestAnimationFrame(step);
    else el.dataset.raw = String(to);
  }
  requestAnimationFrame(step);
}

function showTooltip(x, y, html) {
  tooltip.style.opacity = 1;
  tooltip.style.left = `${x + 14}px`;
  tooltip.style.top = `${y + 10}px`;
  tooltip.innerHTML = html;
}
function hideTooltip() { tooltip.style.opacity = 0; }

// ---------------------------------------------------------------- line chart
function renderLineChart(containerId, points, { valueKey = 'equity', label = 'Equity' } = {}) {
  const container = document.getElementById(containerId);
  if (!points || points.length < 2) {
    container.innerHTML = '<div class="empty">Aun no hay suficientes datos</div>';
    return;
  }
  const W = container.clientWidth || 560, H = 220, PAD = 34;
  const vals = points.map(p => p[valueKey]);
  const min = Math.min(...vals), max = Math.max(...vals);
  const range = (max - min) || 1;
  const x = (i) => PAD + (i / (points.length - 1)) * (W - PAD * 1.5);
  const y = (v) => H - PAD + 4 - ((v - min) / range) * (H - PAD * 1.5);

  let gridSvg = '';
  for (let i = 0; i <= 3; i++) {
    const gy = PAD - 4 + i * ((H - PAD * 1.5) / 3);
    const val = max - (i / 3) * range;
    gridSvg += `<line class="grid-line" x1="${PAD}" x2="${W - 8}" y1="${gy}" y2="${gy}" />`;
    gridSvg += `<text x="4" y="${gy + 3}">${val.toFixed(1)}</text>`;
  }

  const path = points.map((p, i) => `${i === 0 ? 'M' : 'L'} ${x(i).toFixed(1)} ${y(p[valueKey]).toFixed(1)}`).join(' ');
  const areaPath = `${path} L ${x(points.length - 1).toFixed(1)} ${H - PAD + 4} L ${x(0).toFixed(1)} ${H - PAD + 4} Z`;
  const last = points[points.length - 1];

  const svg = `
    <svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
      ${gridSvg}
      <line class="baseline" x1="${PAD}" x2="${W - 8}" y1="${H - PAD + 4}" y2="${H - PAD + 4}" />
      <path d="${areaPath}" fill="var(--series-1-wash)" stroke="none" />
      <path d="${path}" fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />
      <circle cx="${x(points.length - 1)}" cy="${y(last[valueKey])}" r="4" fill="var(--series-1)" stroke="var(--surface-1)" stroke-width="2" />
      ${points.map((p, i) => `<rect x="${x(i) - (W / points.length) / 2}" y="0" width="${W / points.length}" height="${H}" fill="transparent" data-i="${i}" class="hit" />`).join('')}
    </svg>`;
  container.innerHTML = svg;

  container.querySelectorAll('.hit').forEach((el) => {
    el.addEventListener('mousemove', (e) => {
      const i = Number(el.dataset.i);
      const p = points[i];
      const t = new Date(p.ts).toLocaleTimeString();
      showTooltip(e.clientX, e.clientY, `<b>${label}</b><br>${fmtUsd(p[valueKey])}<br><span style="color:var(--text-muted)">${t}</span>`);
    });
    el.addEventListener('mouseleave', hideTooltip);
  });
}

// -------------------------------------------------------------------- donut
function renderDonut(containerId, wins, losses) {
  const container = document.getElementById(containerId);
  const total = wins + losses;
  if (total === 0) {
    container.innerHTML = '<div class="empty">Aun no hay trades cerrados</div>';
    return;
  }
  const R = 70, CX = 100, CY = 100, STROKE = 22;
  const circumference = 2 * Math.PI * R;
  const winFrac = wins / total;
  const winLen = circumference * winFrac;
  const gap = 2;

  const svg = `
    <svg width="200" height="200" viewBox="0 0 200 200">
      <circle cx="${CX}" cy="${CY}" r="${R}" fill="none" stroke="var(--gridline)" stroke-width="${STROKE}" />
      <circle cx="${CX}" cy="${CY}" r="${R}" fill="none" stroke="var(--good)" stroke-width="${STROKE}"
        stroke-dasharray="${Math.max(winLen - gap, 0)} ${circumference}"
        stroke-linecap="round" transform="rotate(-90 ${CX} ${CY})" />
      <circle cx="${CX}" cy="${CY}" r="${R}" fill="none" stroke="var(--critical)" stroke-width="${STROKE}"
        stroke-dasharray="${Math.max(circumference - winLen - gap, 0)} ${circumference}"
        stroke-dashoffset="${-winLen}"
        stroke-linecap="round" transform="rotate(-90 ${CX} ${CY})" />
      <text x="${CX}" y="${CY - 4}" text-anchor="middle" style="fill:var(--text-primary); font-size:26px; font-weight:700;">${Math.round(winFrac * 100)}%</text>
      <text x="${CX}" y="${CY + 16}" text-anchor="middle" style="fill:var(--text-muted); font-size:11px;">win rate</text>
    </svg>
    <div class="legend">
      <span class="item"><span class="swatch" style="background:var(--good)"></span>Ganadas (${wins})</span>
      <span class="item"><span class="swatch" style="background:var(--critical)"></span>Perdidas (${losses})</span>
    </div>`;
  container.innerHTML = svg;
}

// ---------------------------------------------------------------- bar chart
function renderBarChart(containerId, rows, { xKey, valueKey = 'pnl' } = {}) {
  const container = document.getElementById(containerId);
  if (!rows || rows.length === 0) {
    container.innerHTML = '<div class="empty">Aun no hay datos</div>';
    return;
  }
  const W = container.clientWidth || 560, H = 220, PAD = 34;
  const vals = rows.map(r => Number(r[valueKey] || 0));
  const maxAbs = Math.max(...vals.map(Math.abs), 0.01);
  const zeroY = H / 2;
  const barW = Math.min(24, (W - PAD * 1.5) / rows.length - 6);
  const step = (W - PAD * 1.5) / rows.length;

  let bars = '';
  rows.forEach((r, i) => {
    const v = Number(r[valueKey] || 0);
    const h = (Math.abs(v) / maxAbs) * (H / 2 - 16);
    const x = PAD + i * step + (step - barW) / 2;
    const color = v >= 0 ? 'var(--good)' : 'var(--critical)';
    const y = v >= 0 ? zeroY - h : zeroY;
    bars += `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(h, 1).toFixed(1)}"
      rx="4" fill="${color}" data-i="${i}" class="bar" />`;
  });

  const svg = `
    <svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
      <line class="baseline" x1="${PAD}" x2="${W - 8}" y1="${zeroY}" y2="${zeroY}" />
      ${bars}
    </svg>`;
  container.innerHTML = svg;

  container.querySelectorAll('.bar').forEach((el) => {
    el.addEventListener('mousemove', (e) => {
      const i = Number(el.dataset.i);
      const r = rows[i];
      showTooltip(e.clientX, e.clientY,
        `<b>${r[xKey]}</b><br>P&amp;L: ${fmtSigned(r[valueKey])}<br>Trades: ${r.trades} (${r.wins} ganadas)`);
    });
    el.addEventListener('mouseleave', hideTooltip);
  });
}

// ------------------------------------------------------------------- table
function renderTrades(containerId, trades) {
  const container = document.getElementById(containerId);
  if (!trades || trades.length === 0) {
    container.innerHTML = '<div class="empty">Aun no hay operaciones registradas</div>';
    return;
  }
  const rows = trades.map(t => {
    const pnl = t.pnl_usd;
    const pnlClass = pnl == null ? '' : (pnl >= 0 ? 'pnl up' : 'pnl down');
    const pnlText = pnl == null ? '—' : `${pnl >= 0 ? '▲' : '▼'} ${fmtSigned(pnl)}`;
    const sideClass = t.side === 'BUY' ? 'badge buy' : 'badge sell';
    return `<tr>
      <td>${new Date(t.opened_at).toLocaleString()}</td>
      <td><span class="${sideClass}">${t.side}</span></td>
      <td>${Number(t.lot).toFixed(2)}</td>
      <td>${Number(t.entry_price).toFixed(2)}</td>
      <td>${t.exit_price ? Number(t.exit_price).toFixed(2) : '—'}</td>
      <td>${t.status}</td>
      <td class="${pnlClass}">${pnlText}</td>
    </tr>`;
  }).join('');
  container.innerHTML = `<table>
    <thead><tr><th>Apertura</th><th>Lado</th><th>Lote</th><th>Entrada</th><th>Salida</th><th>Estado</th><th>P&amp;L</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

// ------------------------------------------------------------------ events
function renderEvents(containerId, events) {
  const container = document.getElementById(containerId);
  if (!events || events.length === 0) {
    container.innerHTML = '<div class="empty">Sin eventos todavia</div>';
    return;
  }
  const levelClass = (lvl) => {
    const l = (lvl || '').toLowerCase();
    if (l === 'error') return 'error';
    if (l === 'warn' || l === 'warning') return 'warn';
    return 'info';
  };
  container.innerHTML = events.map(e => `
    <div class="event-row">
      <span class="event-time">${new Date(e.ts).toLocaleString()}</span>
      <span class="event-level ${levelClass(e.level)}">${e.level}</span>
      <span class="event-message">${e.message}</span>
    </div>`).join('');
}

// -------------------------------------------------------------------- poll
async function refresh() {
  try {
    const [status, summary, equity, daily, monthly, trades, events] = await Promise.all([
      fetch('/api/status').then(r => r.json()),
      fetch('/api/summary').then(r => r.json()),
      fetch('/api/equity_curve').then(r => r.json()),
      fetch('/api/pnl_daily').then(r => r.json()),
      fetch('/api/pnl_monthly').then(r => r.json()),
      fetch('/api/trades').then(r => r.json()),
      fetch('/api/events').then(r => r.json()),
    ]);

    document.getElementById('acct-login').textContent = status.login || '—';
    document.getElementById('acct-server').textContent = status.server || '—';

    const modePill = document.getElementById('pill-mode');
    modePill.className = 'pill ' + (status.mode === 'LIVE' ? 'off' : 'dry');
    document.getElementById('pill-mode-text').textContent = status.mode === 'LIVE' ? 'LIVE — ordenes reales' : 'DRY RUN — simulado';

    const connPill = document.getElementById('pill-conn');
    connPill.className = 'pill ' + (status.connected ? 'live' : 'off');
    document.getElementById('pill-conn-text').textContent = status.connected ? 'Motor activo' : 'Motor sin datos recientes';

    document.getElementById('pill-updated').textContent = new Date().toLocaleTimeString();

    const eqEl = document.getElementById('tile-equity');
    animateValue(eqEl, summary.equity || 0, fmtUsd);
    document.getElementById('tile-balance').textContent = fmtUsd(summary.balance);
    document.getElementById('tile-margin').textContent = fmtUsd(summary.free_margin);

    const pnlEl = document.getElementById('tile-pnl');
    pnlEl.textContent = fmtSigned(summary.total_pnl);
    pnlEl.style.color = summary.total_pnl >= 0 ? 'var(--good)' : 'var(--critical)';

    document.getElementById('tile-trades').textContent = summary.total_trades ?? 0;
    const wr = summary.total_trades ? Math.round((summary.wins / summary.total_trades) * 100) : 0;
    document.getElementById('tile-winrate').textContent = `${wr}%`;

    renderLineChart('chart-equity', equity, { valueKey: 'equity', label: 'Equity' });
    renderDonut('chart-donut', summary.wins || 0, summary.losses || 0);
    renderBarChart('chart-daily', [...daily].reverse(), { xKey: 'day', valueKey: 'pnl' });
    renderBarChart('chart-monthly', [...monthly].reverse(), { xKey: 'month', valueKey: 'pnl' });
    renderTrades('trades-table', trades);
    renderEvents('events-list', events);
  } catch (e) {
    document.getElementById('pill-conn').className = 'pill off';
    document.getElementById('pill-conn-text').textContent = 'Sin conexion al motor';
    console.error(e);
  }
}

refresh();
setInterval(refresh, POLL_MS);
window.addEventListener('resize', refresh);
