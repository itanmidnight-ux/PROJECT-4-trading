const POLL_MS = 3000;
const THEME_KEY = 'xauusd-dashboard-theme';
const AUTH_TOKEN_KEY = 'xauusd-dashboard-token';
const tooltip = document.getElementById('tooltip');

// Defense in depth: most fields rendered below come from fixed server-side
// enums (side, status, level) and are safe as-is, but event messages can
// embed raw exception text (see core/engine.py's generic error handler,
// f"{type(exc).__name__}: {exc}") - an exception's string form isn't
// bounded to any safe character set. Escaping everything interpolated into
// innerHTML below costs nothing and doesn't rely on that always staying true.
const escapeHtml = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));

const fmtUsd = (v) => {
  const n = Number(v || 0);
  const sign = n < 0 ? '-' : '';
  return `${sign}$${Math.abs(n).toFixed(2)}`;
};
const fmtSigned = (v) => {
  const n = Number(v || 0);
  return `${n >= 0 ? '+' : ''}${n.toFixed(2)}`;
};

const EMPTY_ICON = `<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M7 15l4-5 3 3 5-7"/></svg>`;

// ------------------------------------------------------------------- theme
function effectiveTheme() {
  const saved = localStorage.getItem(THEME_KEY);
  if (saved) return saved;
  return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}
function applyTheme(saved) {
  if (saved) document.documentElement.setAttribute('data-theme', saved);
  else document.documentElement.removeAttribute('data-theme');
  const btn = document.getElementById('theme-toggle');
  if (btn) btn.classList.toggle('is-light', effectiveTheme() === 'light');
}
function initTheme() {
  applyTheme(localStorage.getItem(THEME_KEY));
  document.getElementById('theme-toggle')?.addEventListener('click', () => {
    const next = effectiveTheme() === 'light' ? 'dark' : 'light';
    localStorage.setItem(THEME_KEY, next);
    applyTheme(next);
  });
}

// -------------------------------------------------------------------- auth
// Wraps fetch() for the mutating routes (settings save, pause/resume),
// which require X-Dashboard-Token when DASHBOARD_AUTH_TOKEN is configured
// (see dashboard.py's _check_dashboard_auth). On a 401 - no token stored
// yet, or a stale one - prompts once, stores it, and retries; read-only
// GETs never need this, the backend doesn't gate them.
async function authFetch(path, options = {}) {
  const token = localStorage.getItem(AUTH_TOKEN_KEY);
  const headers = { ...(options.headers || {}) };
  if (token) headers['X-Dashboard-Token'] = token;
  let resp = await fetch(path, { ...options, headers });
  if (resp.status === 401) {
    const entered = window.prompt(
      'Esta accion requiere el token del dashboard (DASHBOARD_AUTH_TOKEN en .env). Ingresalo:');
    if (entered) {
      localStorage.setItem(AUTH_TOKEN_KEY, entered);
      resp = await fetch(path, { ...options, headers: { ...headers, 'X-Dashboard-Token': entered } });
    }
  }
  return resp;
}

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

// A rounded rect that only rounds the corners AT the data end (the end far
// from the baseline) and stays square where it meets the baseline - a bar
// visually "grows from" the baseline, so rounding that edge would make it
// look like it's floating instead of anchored to zero.
function roundedBarPath(x, y, w, h, r, roundTop) {
  r = Math.max(0, Math.min(r, w / 2, h));
  if (roundTop) {
    return `M ${x} ${y + h} L ${x} ${y + r} Q ${x} ${y} ${x + r} ${y} `
         + `L ${x + w - r} ${y} Q ${x + w} ${y} ${x + w} ${y + r} L ${x + w} ${y + h} Z`;
  }
  return `M ${x} ${y} L ${x} ${y + h - r} Q ${x} ${y + h} ${x + r} ${y + h} `
       + `L ${x + w - r} ${y + h} Q ${x + w} ${y + h} ${x + w} ${y + h - r} L ${x + w} ${y} Z`;
}

// ---------------------------------------------------------------- line chart
function renderLineChart(containerId, points, { valueKey = 'equity', label = 'Equity' } = {}) {
  const container = document.getElementById(containerId);
  if (!points || points.length < 2) {
    container.innerHTML = `<div class="empty">${EMPTY_ICON}Aun no hay suficientes datos</div>`;
    return;
  }
  const W = container.clientWidth || 560, H = 220, PAD = 34, plotRight = W - 8;
  const vals = points.map(p => p[valueKey]);
  const min = Math.min(...vals), max = Math.max(...vals);
  const range = (max - min) || 1;
  const x = (i) => PAD + (i / (points.length - 1)) * (plotRight - PAD);
  const y = (v) => H - PAD + 4 - ((v - min) / range) * (H - PAD * 1.5);

  let gridSvg = '';
  for (let i = 0; i <= 3; i++) {
    const gy = PAD - 4 + i * ((H - PAD * 1.5) / 3);
    const val = max - (i / 3) * range;
    gridSvg += `<line class="grid-line" x1="${PAD}" x2="${plotRight}" y1="${gy}" y2="${gy}" />`;
    gridSvg += `<text x="4" y="${gy + 3}">${val.toFixed(1)}</text>`;
  }

  const path = points.map((p, i) => `${i === 0 ? 'M' : 'L'} ${x(i).toFixed(1)} ${y(p[valueKey]).toFixed(1)}`).join(' ');
  const areaPath = `${path} L ${x(points.length - 1).toFixed(1)} ${H - PAD + 4} L ${x(0).toFixed(1)} ${H - PAD + 4} Z`;
  const last = points[points.length - 1];

  const svg = `
    <svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
      ${gridSvg}
      <line class="baseline" x1="${PAD}" x2="${plotRight}" y1="${H - PAD + 4}" y2="${H - PAD + 4}" />
      <path d="${areaPath}" fill="var(--series-1-wash)" stroke="none" />
      <path d="${path}" fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />
      <circle cx="${x(points.length - 1)}" cy="${y(last[valueKey])}" r="4" fill="var(--series-1)" stroke="var(--surface-1)" stroke-width="2" />
      <line class="crosshair" x1="0" x2="0" y1="0" y2="${H}" />
      <circle class="crosshair-dot" r="5" fill="var(--series-1)" stroke="var(--surface-1)" stroke-width="2" />
      <rect class="hit-area" x="${PAD}" y="0" width="${Math.max(plotRight - PAD, 1)}" height="${H}" fill="transparent" />
    </svg>`;
  container.innerHTML = svg;

  // A single full-width hit area with one mousemove handler that snaps to
  // the nearest data index, instead of one tiny hit-rect per point - with
  // up to 300 equity snapshots in a ~600px chart, per-point boxes would be
  // only a couple of px wide each, well under any reasonable hit target.
  const svgEl = container.querySelector('svg');
  const crosshair = container.querySelector('.crosshair');
  const crosshairDot = container.querySelector('.crosshair-dot');
  const hitArea = container.querySelector('.hit-area');

  hitArea.addEventListener('mousemove', (e) => {
    const rect = svgEl.getBoundingClientRect();
    const scaleX = W / rect.width;
    const px = (e.clientX - rect.left) * scaleX;
    const frac = (px - PAD) / (plotRight - PAD);
    const i = Math.max(0, Math.min(points.length - 1, Math.round(frac * (points.length - 1))));
    const p = points[i];
    const cx = x(i), cy = y(p[valueKey]);

    crosshair.setAttribute('x1', cx.toFixed(1));
    crosshair.setAttribute('x2', cx.toFixed(1));
    crosshair.style.opacity = 1;
    crosshairDot.setAttribute('cx', cx.toFixed(1));
    crosshairDot.setAttribute('cy', cy.toFixed(1));
    crosshairDot.style.opacity = 1;

    const t = new Date(p.ts).toLocaleTimeString();
    showTooltip(e.clientX, e.clientY,
      `<div class="tt-value">${escapeHtml(fmtUsd(p[valueKey]))}</div>`
      + `<div class="tt-label">${escapeHtml(label)}</div>`
      + `<div class="tt-time">${escapeHtml(t)}</div>`);
  });
  hitArea.addEventListener('mouseleave', () => {
    crosshair.style.opacity = 0;
    crosshairDot.style.opacity = 0;
    hideTooltip();
  });
}

// -------------------------------------------------------------------- donut
function renderDonut(containerId, wins, losses) {
  const container = document.getElementById(containerId);
  const total = wins + losses;
  if (total === 0) {
    container.innerHTML = `<div class="empty">${EMPTY_ICON}Aun no hay trades cerrados</div>`;
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
    container.innerHTML = `<div class="empty">${EMPTY_ICON}Aun no hay datos</div>`;
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
    const isPositive = v >= 0;
    const h = Math.max((Math.abs(v) / maxAbs) * (H / 2 - 16), 1);
    const barX = PAD + i * step + (step - barW) / 2;
    const barY = isPositive ? zeroY - h : zeroY;
    const color = isPositive ? 'var(--good)' : 'var(--critical)';
    const d = roundedBarPath(barX, barY, Math.max(barW, 1), h, 4, isPositive);
    bars += `<path d="${d}" fill="${color}" data-i="${i}" class="bar" />`;
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
      const v = Number(r[valueKey] || 0);
      const valueColor = v >= 0 ? 'var(--delta-good)' : 'var(--delta-critical)';
      showTooltip(e.clientX, e.clientY,
        `<div class="tt-value" style="color:${valueColor}">${escapeHtml(fmtSigned(v))}</div>`
        + `<div class="tt-label">${escapeHtml(String(r[xKey]))}</div>`
        + `<div class="tt-time">${escapeHtml(String(r.trades))} trades · ${escapeHtml(String(r.wins))} ganadas</div>`);
    });
    el.addEventListener('mouseleave', hideTooltip);
  });
}

// ------------------------------------------------------------------- table
function renderTrades(containerId, trades) {
  const container = document.getElementById(containerId);
  if (!trades || trades.length === 0) {
    container.innerHTML = `<div class="empty">${EMPTY_ICON}Aun no hay operaciones registradas</div>`;
    return;
  }
  const rows = trades.map(t => {
    const pnl = t.pnl_usd;
    const pnlClass = pnl == null ? '' : (pnl >= 0 ? 'pnl up' : 'pnl down');
    const pnlText = pnl == null ? '—' : `${pnl >= 0 ? '▲' : '▼'} ${fmtSigned(pnl)}`;
    const sideClass = t.side === 'BUY' ? 'badge buy' : 'badge sell';
    return `<tr>
      <td>${new Date(t.opened_at).toLocaleString()}</td>
      <td><span class="${sideClass}">${escapeHtml(t.side)}</span></td>
      <td>${Number(t.lot).toFixed(2)}</td>
      <td>${Number(t.entry_price).toFixed(2)}</td>
      <td>${t.exit_price ? Number(t.exit_price).toFixed(2) : '—'}</td>
      <td>${escapeHtml(t.status)}</td>
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
    container.innerHTML = `<div class="empty">${EMPTY_ICON}Sin eventos todavia</div>`;
    return;
  }
  const levelClass = (lvl) => {
    const l = (lvl || '').toLowerCase();
    if (l === 'critical' || l === 'error') return 'error';
    if (l === 'warn' || l === 'warning') return 'warn';
    return 'info';
  };
  container.innerHTML = events.map(e => `
    <div class="event-row">
      <span class="event-time">${new Date(e.ts).toLocaleString()}</span>
      <span class="event-level ${levelClass(e.level)}">${escapeHtml(e.level)}</span>
      <span class="event-message">${escapeHtml(e.message)}</span>
    </div>`).join('');
}

// -------------------------------------------------------------------- tabs
function initTabs() {
  document.querySelectorAll('.tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab-btn').forEach((b) => b.classList.remove('active'));
      document.querySelectorAll('.tab-panel').forEach((p) => p.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById(`tab-${btn.dataset.tab}`)?.classList.add('active');
      if (btn.dataset.tab === 'settings') loadSettings();
    });
  });
}

// ---------------------------------------------------------- pause / resume
function updatePauseButton(paused) {
  // Single source of truth for both the button AND the "Pausado" pill, so
  // a manual click updates them together immediately instead of the pill
  // lagging up to POLL_MS behind until the next scheduled refresh() call.
  const pausedPill = document.getElementById('pill-paused');
  if (pausedPill) pausedPill.hidden = !paused;

  const btn = document.getElementById('btn-pause-resume');
  if (!btn) return;
  btn.textContent = paused ? 'Reanudar bot' : 'Detener bot';
  btn.classList.toggle('is-paused', paused);
  btn.dataset.paused = paused ? '1' : '0';
}

function initPauseButton() {
  const btn = document.getElementById('btn-pause-resume');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    const isPaused = btn.dataset.paused === '1';
    const endpoint = isPaused ? '/api/bot/resume' : '/api/bot/pause';
    btn.disabled = true;
    try {
      const resp = await authFetch(endpoint, { method: 'POST' });
      if (resp.ok) {
        const result = await resp.json();
        updatePauseButton(!!result.paused);
      }
    } catch (e) {
      console.error('No se pudo cambiar el estado del bot:', e);
    } finally {
      btn.disabled = false;
    }
  });
}

// ------------------------------------------------------------- settings tab
async function loadSettings() {
  const data = await fetchJson('/api/settings');
  if (!data) return;
  document.getElementById('set-login').value = data.mt5_login || '';
  document.getElementById('set-server').value = data.mt5_server || '';
  const pwEl = document.getElementById('set-password');
  pwEl.value = '';
  pwEl.placeholder = data.has_password ? '•••••••• (sin cambios)' : '(sin configurar)';
  document.getElementById('set-is-demo').checked = !!data.mt5_is_demo;
  document.getElementById('set-dry-run').checked = !!data.dry_run;
  document.getElementById('set-risk').value = data.risk_per_trade_usd ?? '';
  document.getElementById('set-max-loss').value = data.max_daily_loss_usd ?? '';
  document.getElementById('set-max-dd').value = data.max_daily_drawdown_pct ?? '';
  document.getElementById('set-max-trades').value = data.max_trades_per_day ?? '';
}

function initSettingsForm() {
  const form = document.getElementById('settings-form');
  if (!form) return;
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const msgEl = document.getElementById('settings-message');
    msgEl.textContent = '';
    msgEl.className = 'settings-message';

    const payload = {
      mt5_login: document.getElementById('set-login').value.trim(),
      mt5_server: document.getElementById('set-server').value.trim(),
      mt5_is_demo: document.getElementById('set-is-demo').checked,
      dry_run: document.getElementById('set-dry-run').checked,
      risk_per_trade_usd: Number(document.getElementById('set-risk').value),
      max_daily_loss_usd: Number(document.getElementById('set-max-loss').value),
      max_daily_drawdown_pct: Number(document.getElementById('set-max-dd').value),
      max_trades_per_day: Number(document.getElementById('set-max-trades').value),
    };
    const password = document.getElementById('set-password').value;
    if (password) payload.mt5_password = password;

    try {
      const resp = await authFetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const result = await resp.json();
      if (resp.ok && result.ok) {
        msgEl.textContent = result.message || 'Guardado.';
        msgEl.className = 'settings-message ok';
        loadSettings();
      } else {
        msgEl.textContent = result.error || `Error al guardar (HTTP ${resp.status}).`;
        msgEl.className = 'settings-message error';
      }
    } catch (err) {
      msgEl.textContent = 'No se pudo conectar con el dashboard.';
      msgEl.className = 'settings-message error';
      console.error(err);
    }
  });
}

// -------------------------------------------------------------------- poll

// Resolves to the parsed JSON body, or null on any failure (network error,
// non-2xx status, invalid JSON) - never throws, so one bad endpoint can't
// take the rest of the dashboard down with it.
async function fetchJson(path) {
  try {
    const r = await fetch(path);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  } catch (e) {
    console.error(`No se pudo cargar ${path}:`, e);
    return null;
  }
}

async function refresh() {
  const [status, summary, equity, daily, monthly, trades, events] = await Promise.all([
    fetchJson('/api/status'),
    fetchJson('/api/summary'),
    fetchJson('/api/equity_curve'),
    fetchJson('/api/pnl_daily'),
    fetchJson('/api/pnl_monthly'),
    fetchJson('/api/trades'),
    fetchJson('/api/events'),
  ]);

  const connPill = document.getElementById('pill-conn');
  if (status === null) {
    // /api/status failing means the dashboard's own local Flask server
    // didn't respond at all - nothing else can be trusted either.
    connPill.className = 'pill off';
    document.getElementById('pill-conn-text').textContent = 'Sin conexion al motor';
    return;
  }

  document.getElementById('acct-login').textContent = status.login || '—';
  document.getElementById('acct-server').textContent = status.server || '—';

  const modePill = document.getElementById('pill-mode');
  modePill.className = 'pill ' + (status.mode === 'LIVE' ? 'off' : 'dry');
  document.getElementById('pill-mode-text').textContent = status.mode === 'LIVE' ? 'LIVE — ordenes reales' : 'DRY RUN — simulado';

  connPill.className = 'pill ' + (status.connected ? 'live' : 'off');
  document.getElementById('pill-conn-text').textContent = status.connected ? 'Motor activo' : 'Motor sin datos recientes';

  document.getElementById('pill-updated').textContent = new Date().toLocaleTimeString();

  updatePauseButton(!!status.paused);

  // Each section below only updates if ITS OWN fetch succeeded - a single
  // failing endpoint leaves that one panel showing its last good state
  // instead of blanking the entire dashboard.
  if (summary) {
    const eqEl = document.getElementById('tile-equity');
    animateValue(eqEl, summary.equity || 0, fmtUsd);
    document.getElementById('tile-balance').textContent = fmtUsd(summary.balance);
    document.getElementById('tile-margin').textContent = fmtUsd(summary.free_margin);

    const pnlEl = document.getElementById('tile-pnl');
    pnlEl.textContent = fmtSigned(summary.total_pnl);
    pnlEl.style.color = summary.total_pnl >= 0 ? 'var(--delta-good)' : 'var(--delta-critical)';

    document.getElementById('tile-trades').textContent = summary.total_trades ?? 0;
    const wr = summary.total_trades ? Math.round((summary.wins / summary.total_trades) * 100) : 0;
    document.getElementById('tile-winrate').textContent = `${wr}%`;

    renderDonut('chart-donut', summary.wins || 0, summary.losses || 0);
  }

  if (equity) {
    renderLineChart('chart-equity', equity, { valueKey: 'equity', label: 'Equity' });
    const deltaEl = document.getElementById('tile-equity-delta');
    if (deltaEl && equity.length >= 2) {
      const delta = equity[equity.length - 1].equity - equity[0].equity;
      deltaEl.textContent = `${delta >= 0 ? '▲' : '▼'} ${fmtSigned(delta)} vs. inicio del grafico`;
      deltaEl.className = 'delta ' + (delta >= 0 ? 'up' : 'down');
    } else if (deltaEl) {
      deltaEl.textContent = '';
      deltaEl.className = 'delta';
    }
  }
  if (daily) renderBarChart('chart-daily', [...daily].reverse(), { xKey: 'day', valueKey: 'pnl' });
  if (monthly) renderBarChart('chart-monthly', [...monthly].reverse(), { xKey: 'month', valueKey: 'pnl' });
  if (trades) renderTrades('trades-table', trades);
  if (events) renderEvents('events-list', events);
}

initTheme();
initTabs();
initPauseButton();
initSettingsForm();
refresh();
setInterval(refresh, POLL_MS);
window.addEventListener('resize', refresh);
