const state = {
  wallet: null,
  report: null,
  intraday: null,
  walletAnalysis: null,
  labelResult: null,
  intradayLoading: false,
  rows: [],
  filtered: [],
  period: 'all',
  calendarMonth: new Date(),
};

const $ = (id) => document.getElementById(id);

const GROUP_DISPLAY_NAMES = {
  G1: 'Early Microcap Specialist',
  G2: 'Whale Conviction',
  G3: 'Narrative Basket',
  G4: 'Unicon Holder',
};

function displayGroupName(value) {
  const raw = String(value || '');
  const code = raw.match(/^G([1-4])(?:_|\s|·|-|$)/)?.[1];
  return code ? GROUP_DISPLAY_NAMES[`G${code}`] : raw;
}

function fmtUsd(n, digits) {
  if (n == null || Number.isNaN(Number(n))) return '—';
  const v = Number(n);
  const abs = Math.abs(v);
  const sign = v < 0 ? '-' : '';
  if (abs >= 1_000_000) return `${sign}$${(abs / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `${sign}$${(abs / 1_000).toFixed(abs >= 10_000 ? 1 : 1)}K`;
  return `${sign}$${abs.toFixed(digits != null ? digits : (abs < 10 ? 2 : 0))}`;
}

function fmtPct(n) {
  if (n == null || Number.isNaN(Number(n))) return '';
  const pct = Number(n);
  const sign = pct >= 0 ? '+' : '';
  return `${sign}${pct.toFixed(1)}%`;
}

function fmtMc(n) {
  if (n == null || Number.isNaN(Number(n))) return '—';
  const v = Number(n);
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(1)}K`;
  return `$${v.toFixed(0)}`;
}

function pnlClass(v) {
  const n = Number(v);
  if (v == null || Number.isNaN(n) || n === 0) return '';
  return n > 0 ? 'pos' : 'neg';
}

function dayHasPnl(row) {
  if (!row) return false;
  return Number(row.realized_pnl_usd || 0) !== 0
    || Number(row.profit_usd || 0) !== 0
    || Number(row.loss_usd || 0) !== 0;
}

function periodCalendarRows(period, entries) {
  const sorted = [...(entries || [])]
    .filter((row) => row && row.date)
    .sort((a, b) => String(a.date).localeCompare(String(b.date)));
  if (!sorted.length || period === 'all') return sorted;
  const span = { '1d': 1, '7d': 7, '30d': 30 }[period];
  if (!span) return sorted;
  const end = sorted[sorted.length - 1].date;
  const startMs = Date.parse(`${end}T00:00:00Z`) - (span - 1) * 86400000;
  const startKey = new Date(startMs).toISOString().slice(0, 10);
  return sorted.filter((row) => row.date >= startKey);
}

function summarizeCalendar(rows) {
  const ordered = [...rows].sort((a, b) => String(a.date).localeCompare(String(b.date)));
  const active = ordered.filter(dayHasPnl);
  const profit = active.reduce((sum, row) => sum + Number(row.profit_usd || 0), 0);
  const loss = active.reduce((sum, row) => sum + Number(row.loss_usd || 0), 0);
  const net = active.reduce((sum, row) => sum + Number(row.realized_pnl_usd || 0), 0);
  let cumulative = 0;
  let peak = 0;
  let mdd = 0;
  for (const row of ordered) {
    cumulative += Number(row.realized_pnl_usd || 0);
    peak = Math.max(peak, cumulative);
    mdd = Math.max(mdd, peak - cumulative);
  }
  const pos = active.filter((row) => Number(row.realized_pnl_usd) > 0).length;
  const neg = active.filter((row) => Number(row.realized_pnl_usd) < 0).length;
  return {
    profit,
    loss,
    net,
    mdd,
    pos,
    neg,
    activeDays: active.length,
    profitFactor: loss > 0 ? profit / loss : null,
    buys: ordered.reduce((sum, row) => sum + Number(row.buy_tx_count || 0), 0),
    sells: ordered.reduce((sum, row) => sum + Number(row.sell_tx_count || 0), 0),
    volume: ordered.reduce(
      (sum, row) => sum + Number(row.buy_volume_usd || 0) + Number(row.sell_volume_usd || 0),
      0,
    ),
  };
}

function looksLikeSolanaAddress(s) {
  return /^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(String(s || '').trim());
}

function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function analyzeRows(rows) {
  const active = rows.filter((r) => (r.bought_usd || 0) > 0 || (r.sold_usd || 0) > 0);
  const wins = active.filter((r) => (r.total_profit_usd || 0) > 0);
  const losses = active.filter((r) => (r.total_profit_usd || 0) < 0);
  const grossP = wins.reduce((s, r) => s + Number(r.total_profit_usd || 0), 0);
  const grossL = Math.abs(losses.reduce((s, r) => s + Number(r.total_profit_usd || 0), 0));
  const pf = grossL > 0 ? grossP / grossL : (grossP > 0 ? null : 0);
  const total = active.reduce((s, r) => s + Number(r.total_profit_usd || 0), 0);
  const bought = rows.reduce((s, r) => s + Number(r.bought_usd || 0), 0);
  return {
    tokenCount: rows.length,
    wins: wins.length,
    losses: losses.length,
    winRate: active.length ? (100 * wins.length) / active.length : 0,
    profitFactor: pf,
    totalProfit: total,
    totalBought: bought,
    hodl: rows.filter((r) => r.is_hodl).length,
    closed: rows.filter((r) => r.is_closed).length,
    winners: [...wins].sort((a, b) => b.total_profit_usd - a.total_profit_usd).slice(0, 10),
    losers: [...losses].sort((a, b) => a.total_profit_usd - b.total_profit_usd).slice(0, 10),
  };
}

function kpi(label, value, cls = '') {
  return `<div class="kpi"><span>${escapeHtml(label)}</span><strong class="${cls}">${value}</strong></div>`;
}

const availabilityLabels = {
  dashboard_present: 'Dashboard',
  holdings_complete: 'Holdings đầy đủ',
  holdings_fresh_48h: 'Holdings ≤ 48h',
  holdings_nonempty: 'Có token sample',
  daily_calendar_present: 'PNL calendar',
  stats_7d_present: 'Stats 7D',
  stats_30d_present: 'Stats 30D',
  stats_7d_fresh_48h: 'Stats 7D ≤ 48h',
  stats_30d_fresh_48h: 'Stats 30D ≤ 48h',
  activity_complete: 'Activity đầy đủ',
  activity_fresh_48h: 'Activity ≤ 48h',
  activity_events_present: 'Có activity events',
  intraday_complete: 'Intraday đầy đủ',
  intraday_fresh_48h: 'Intraday ≤ 48h',
};

function setLabelProgress(mode) {
  const steps = [...document.querySelectorAll('#label-progress [data-step]')];
  steps.forEach((step, index) => {
    step.classList.remove('active', 'done');
    if (mode === 'done') step.classList.add('done');
    if (mode === 'loading' && index === 0) step.classList.add('active');
  });
}

function setLabelLoading(loading) {
  $('create-label-btn').disabled = loading;
  $('label-result').classList.toggle('hidden', loading);
  $('label-actions').classList.add('hidden');
  if (loading) {
    setLabelProgress('loading');
    $('label-status').className = 'label-status loading';
    $('label-status').innerHTML = '<span class="spinner" aria-hidden="true"></span>Đang đọc snapshot SQLite và tính label…';
  }
}

function renderDatabaseUpdateStatus(job = {}) {
  const status = job.status || 'not_scheduled';
  const labels = {
    queued: 'DB QUEUED',
    running: 'DB UPDATING',
    complete: 'DB UPDATED',
    partial: 'DB PARTIAL',
    failed: 'DB UPDATE FAILED',
    not_scheduled: 'DB IDLE',
  };
  $('database-update-status').textContent = labels[status] || `DB ${status.toUpperCase()}`;
  $('database-update-status').className = `status-badge db-${status}`;
  return status;
}

async function monitorDatabaseUpdate(address) {
  for (let attempt = 0; attempt < 120 && state.wallet === address; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 3000));
    try {
      const res = await fetch(`/api/pnl/refresh/status/${encodeURIComponent(address)}`);
      if (!res.ok) return;
      const job = await res.json();
      const status = renderDatabaseUpdateStatus(job);
      if (status === 'complete') {
        $('label-helper').textContent = 'Database đã cập nhật ở nền. Label đang hiển thị vẫn giữ nguyên snapshot tại thời điểm xuất.';
        return;
      }
      if (status === 'failed') {
        $('label-helper').textContent = `Cập nhật database nền thất bại: ${job.error || 'provider error'}. Label hiện tại không bị ảnh hưởng.`;
        return;
      }
      if (status === 'partial') {
        const remaining = (job.result?.remaining_components || []).join(', ');
        $('label-helper').textContent = `Database đã lưu các phần thành công; còn thiếu ${remaining || 'một số component'}. Scheduler sẽ tự retry. Label hiện tại không bị ảnh hưởng.`;
        return;
      }
    } catch (_error) {
      return;
    }
  }
}

function labelMetric(title, value, note = '', cls = '') {
  return `<article class="label-metric">
    <span>${escapeHtml(title)}</span>
    <strong class="${cls}">${escapeHtml(value == null || value === '' ? '—' : value)}</strong>
    <small>${escapeHtml(note)}</small>
  </article>`;
}

function renderWalletLabel(result) {
  const item = result.label || {};
  const labels = item.labels || {};
  const metrics = item.metrics || {};
  const availability = result.data_availability || item.data_availability || {};
  const conviction = labels.conviction_v2 || {};
  const priority = item.priority || {};
  const confirmed = labels.rank_status === 'CONFIRMED';
  const ready = availability.status === 'READY';

  state.labelResult = result;
  const updateStatus = renderDatabaseUpdateStatus(result.database_update || {});
  setLabelProgress('done');
  $('label-result').classList.remove('hidden');
  $('label-actions').classList.remove('hidden');
  $('label-status').className = `label-status ${ready ? 'success' : 'warning'}`;
  $('label-status').textContent = ready
    ? 'Dữ liệu đạt toàn bộ điều kiện. Label có thể sử dụng.'
    : `Label tạm thời: ${Number((availability.failed_checks || []).length)} kiểm tra dữ liệu chưa đạt.`;

  $('rank-card').className = `rank-card rank-${escapeHtml(labels.rank || 'unknown').toLowerCase()}`;
  $('label-rank').textContent = labels.rank || '—';
  $('label-rank-status').textContent = labels.rank_status || '—';
  $('label-rank-status').className = `status-badge ${confirmed ? 'confirmed' : 'provisional'}`;
  $('label-wallet-name').textContent = item.name || 'Wallet không tên';
  $('label-address').textContent = item.address || result.wallet_address || '—';
  $('label-rank-rule').textContent = labels.rank_rule || 'Không có rank rule';

  const growth = metrics.portfolio_return_pct == null
    ? 'Không đo được'
    : fmtPct(metrics.portfolio_return_pct);
  const groupLabels = metrics.group_labels || [];
  const groupSummary = groupLabels.length ? groupLabels.map(displayGroupName).join(' · ') : 'Chưa đạt';
  const groupFeatures = metrics.group_features || {};
  const g1Rate = groupFeatures.g1?.early_lowcap_rate_pct;
  $('label-metrics').innerHTML = [
    labelMetric('Overall rank score', metrics.overall_rank_score == null ? '—' : Number(metrics.overall_rank_score).toFixed(1), `/ 100 · ${Math.round(Number(metrics.rank_confidence || 0) * 100)}% data`),
    labelMetric('Performance score', metrics.performance_score == null ? '—' : Number(metrics.performance_score).toFixed(1), '/ 100'),
    labelMetric('Confidence', metrics.confidence || '—', `score ${metrics.confidence_score ?? '—'}`),
    labelMetric('Absolute PnL', fmtUsd(metrics.absolute_pnl_usd), 'all-time', pnlClass(metrics.absolute_pnl_usd)),
    labelMetric('Portfolio growth', growth, metrics.portfolio_return_pct == null ? 'Không dùng ROI thay thế' : 'flow-adjusted'),
    labelMetric('Cluster WR', metrics.cluster_win_rate_pct == null ? '—' : `${Number(metrics.cluster_win_rate_pct).toFixed(1)}%`, metrics.cluster_metric_status || ''),
    labelMetric('Conviction', conviction.label || '—', conviction.status || 'INSUFFICIENT_DATA', conviction.label === 'RISKY' ? 'neg' : (conviction.label === 'POSITIVE' ? 'pos' : '')),
    labelMetric('Groups đạt', groupSummary, g1Rate == null ? 'Thiếu/không đủ dữ liệu G1' : `G1 early microcap ${Number(g1Rate).toFixed(1)}%`, groupLabels.length ? 'pos' : ''),
  ].join('');

  const checks = availability.checks || {};
  $('data-status-badge').textContent = availability.status || 'UNKNOWN';
  $('data-status-badge').className = `status-badge ${ready ? 'confirmed' : 'provisional'}`;
  $('data-checks').innerHTML = Object.entries(checks).map(([key, passed]) => `
    <div class="data-check ${passed ? 'pass' : 'fail'}">
      <span class="check-mark" aria-hidden="true">${passed ? '✓' : '×'}</span>
      <span>${escapeHtml(availabilityLabels[key] || key.replaceAll('_', ' '))}</span>
      <b>${passed ? 'Đạt' : 'Thiếu'}</b>
    </div>
  `).join('');

  const behaviorRows = [
    ['Kết luận conviction', conviction.label || '—'],
    ['Cỡ mẫu', conviction.sample_size == null ? '—' : String(conviction.sample_size)],
    ['Kiểu scale', conviction.scale_style || '—'],
    ['Clan ID', priority.clan_id || 'Không phát hiện'],
    ['Clan size', priority.clan_size == null ? '—' : String(priority.clan_size)],
    ['Clan signal', priority.level || 'NORMAL'],
    ['Priority', priority.action || 'NORMAL'],
    ['Xác minh', priority.verification_status || '—'],
    ['Groups đạt', groupSummary],
  ];
  $('label-behavior').innerHTML = behaviorRows.map(([key, value]) => `
    <div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd></div>
  `).join('');

  const limitations = [
    ...(availability.capital_limitations || []),
    ...(availability.unsupported_metrics || []),
  ];
  $('label-limitations').innerHTML = limitations.length
    ? limitations.slice(0, 8).map((value) => `<li>${escapeHtml(String(value).replaceAll('_', ' '))}</li>`).join('')
    : '<li class="limitation-clear">Không có giới hạn nghiêm trọng được ghi nhận.</li>';
  $('label-helper').textContent = `Formula ${result.formula_version || '—'} · as of ${availability.as_of_date_utc || '—'} · input ${String(result.input_hash || '').slice(0, 12)}… · DB ${updateStatus}`;
  if (['queued', 'running'].includes(updateStatus)) monitorDatabaseUpdate(item.address || result.wallet_address);
}

function downloadText(filename, content, type) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function downloadLabel(format) {
  const item = state.labelResult?.label;
  if (!item) return;
  const address = item.address || 'wallet';
  if (format === 'json') {
    downloadText(`${address}_label.json`, `${JSON.stringify([item], null, 2)}\n`, 'application/json');
    return;
  }
  const labels = item.labels || {};
  const metrics = item.metrics || {};
  const priority = item.priority || {};
  const conviction = labels.conviction_v2 || {};
  const fields = ['Wallet', 'Address', 'Rank', 'Perf Score', 'Confidence', 'Start Equity', 'End Equity', 'Growth', 'Abs PnL', 'Cluster WR', 'Conviction', 'Clan ID', 'Clan Size', 'Clan Signal', 'Priority'];
  const values = [item.name || '', address, labels.rank || '', metrics.performance_score ?? '', metrics.confidence || '', metrics.starting_equity_usd ?? '', metrics.ending_equity_usd ?? '', metrics.portfolio_return_pct ?? '', metrics.absolute_pnl_usd ?? '', metrics.cluster_win_rate_pct ?? '', conviction.label || '', priority.clan_id || '', priority.clan_size ?? '', priority.level || 'NORMAL', priority.action || 'NORMAL'];
  const csv = [fields, values].map((row) => row.map((value) => `"${String(value).replaceAll('"', '""')}"`).join(',')).join('\r\n');
  downloadText(`${address}_label_summary.csv`, `\ufeff${csv}\r\n`, 'text/csv;charset=utf-8');
}

async function loadLocalLabel(address) {
  setLabelLoading(true);
  try {
    const res = await fetch('/api/pnl/analyze-and-label', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ wallet_address: address, refresh: false }),
    });
    const payload = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(payload.detail || res.statusText);
    if (state.wallet !== address) return;
    renderWalletLabel(payload);
  } catch (error) {
    if (state.wallet !== address) return;
    $('label-result').classList.add('hidden');
    $('label-status').className = 'label-status error';
    $('label-status').textContent = error.message || 'Không tạo được label từ snapshot local.';
    setLabelProgress('');
  } finally {
    $('create-label-btn').disabled = false;
  }
}

function renderSummary(report, viewStats) {
  const s = report.summary || {};
  $('summary-strip').innerHTML = [
    kpi('Realized PnL', fmtUsd(s.realized_profit_usd), pnlClass(s.realized_profit_usd)),
    kpi('Unrealized PnL', fmtUsd(s.unrealized_profit_usd), pnlClass(s.unrealized_profit_usd)),
    kpi('Total PnL', fmtUsd(s.total_profit_usd), pnlClass(s.total_profit_usd)),
    kpi('Total Cost', fmtUsd(s.total_cost_usd)),
    kpi('Txs Buy / Sell', `${s.buy_count || 0} / ${s.sell_count || 0}`),
    kpi('Token rows', String(report.rows?.length || 0)),
  ].join('');

  const wr = viewStats.winRate;
  const pf = viewStats.profitFactor;
  $('analysis-strip').innerHTML = [
    kpi('Coin lãi (bảng)', String(viewStats.wins), 'pos'),
    kpi('Coin lỗ (bảng)', String(viewStats.losses), 'neg'),
    kpi('Win rate token', `${wr.toFixed(1)}%`),
    kpi('Profit factor', pf == null ? '—' : pf.toFixed(2)),
    kpi('HODL / Sell All', `${viewStats.hodl} / ${viewStats.closed}`),
    kpi('PnL trên bảng lọc', fmtUsd(viewStats.totalProfit), pnlClass(viewStats.totalProfit)),
  ].join('');
}

function distributionRows(items, total, colors) {
  return items.map(([label, count], index) => {
    const pct = total ? (100 * Number(count || 0) / total).toFixed(1) : '0.0';
    return `<div class="distribution-row">
      <span><i class="dot" style="background:${colors[index]}"></i>${escapeHtml(label)}</span>
      <b>${count || 0} (${pct}%)</b>
    </div>`;
  }).join('');
}

function renderCalendar() {
  if (!state.report) return;
  const entries = state.report.daily_calendar || [];
  const metadata = state.report.daily_calendar_metadata || {};
  const isOfficial = metadata.is_official_gmgn_calendar === true;
  $('calendar-source').textContent = isOfficial
    ? 'PNL Calendar · GMGN Web'
    : 'Sell Realized PnL · OpenAPI';
  const byDate = new Map(entries.map((row) => [row.date, row]));
  const month = state.calendarMonth;
  const year = month.getUTCFullYear();
  const monthIndex = month.getUTCMonth();
  $('calendar-title').textContent = new Intl.DateTimeFormat('vi-VN', {
    month: 'long', year: 'numeric', timeZone: 'UTC',
  }).format(new Date(Date.UTC(year, monthIndex, 1)));

  const first = new Date(Date.UTC(year, monthIndex, 1));
  const days = new Date(Date.UTC(year, monthIndex + 1, 0)).getUTCDate();
  const offset = (first.getUTCDay() + 6) % 7;
  const monthEntries = entries.filter((row) => {
    const d = new Date(`${row.date}T00:00:00Z`);
    return d.getUTCFullYear() === year && d.getUTCMonth() === monthIndex && dayHasPnl(row);
  });
  const maxAbs = Math.max(1, ...monthEntries.map((row) => Math.abs(Number(row.realized_pnl_usd || 0))));
  const cells = Array.from({ length: offset }, () => '<div class="calendar-day empty"></div>');
  for (let day = 1; day <= days; day += 1) {
    const key = `${year}-${String(monthIndex + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
    const row = byDate.get(key);
    const active = dayHasPnl(row);
    const value = active ? Number(row.realized_pnl_usd || 0) : null;
    const cls = value == null || value === 0 ? '' : (value > 0 ? 'gain' : 'loss');
    const heat = value == null || value === 0 ? 0 : (0.18 + 0.55 * Math.abs(value) / maxAbs).toFixed(2);
    const tooltip = active
      ? `${key} UTC · Profit ${fmtUsd(row.profit_usd)} − Loss ${fmtUsd(row.loss_usd)} = ${fmtUsd(value)} · ${row.buy_tx_count || 0}/${row.sell_tx_count || 0} buy/sell`
      : row
        ? `${key} UTC · không có lãi/lỗ`
        : (isOfficial
          ? `${key} UTC · ngoài phạm vi snapshot GMGN đã tải`
          : `${key} UTC · không có activity`);
    cells.push(`<div class="calendar-day ${cls}"${cls ? ` style="--heat:${heat}"` : ''} title="${escapeHtml(tooltip)}">
      <span class="day-num">${day}</span>
      ${active ? `<span class="day-pnl ${pnlClass(value)}">${fmtUsd(value)}</span>` : ''}
    </div>`);
  }
  $('pnl-calendar').innerHTML = cells.join('');
}

function renderPeriodAnalytics(period = state.period) {
  if (!state.report) return;
  state.period = period;
  document.querySelectorAll('#period-tabs button').forEach((button) => {
    button.classList.toggle('active', button.dataset.period === period);
  });

  const labels = { '1d': '1D', '7d': '7D', '30d': '30D', all: 'All-time' };
  const p = (state.report.periods || {})[period] || {};
  const calendarRows = periodCalendarRows(period, state.report.daily_calendar || []);
  const cal = summarizeCalendar(calendarRows);
  const realized = cal.activeDays ? cal.net : Number(p.realized_profit_usd);
  const hasRealized = Number.isFinite(realized) && realized !== 0;
  const roi = p.realized_roi_pct;
  const officialStats = (state.report.period_stats || {})[period] || {};
  const pnlStat = officialStats.pnl_stat || {};
  const a = state.report.analysis || {};

  $('period-label').textContent = `${labels[period] || period} Realized PnL`;
  $('period-roi').textContent = hasRealized ? fmtUsd(realized) : '—';
  $('period-roi').className = `hero-number ${pnlClass(hasRealized ? realized : 0)}`;
  $('period-profit').textContent = roi == null ? (hasRealized ? 'ROI —' : 'Không có lãi/lỗ trong kỳ này') : `ROI ${fmtPct(roi)}`;
  $('period-profit').className = `period-profit ${pnlClass(hasRealized ? realized : roi)}`;
  $('period-total').textContent = p.total_profit_usd == null ? '—' : fmtUsd(p.total_profit_usd);
  $('period-total').className = pnlClass(p.total_profit_usd);
  $('period-unrealized').textContent = p.unrealized_profit_usd == null ? '—' : fmtUsd(p.unrealized_profit_usd);
  $('period-unrealized').className = pnlClass(p.unrealized_profit_usd);

  const buys = Number(officialStats.buy || cal.buys || 0);
  const sells = Number(officialStats.sell || cal.sells || 0);
  const analysisRows = [
    ['PnL kỳ', hasRealized ? fmtUsd(realized) : '—', pnlClass(hasRealized ? realized : 0)],
    ['Lãi / Lỗ', cal.activeDays ? `${fmtUsd(cal.profit)} / ${fmtUsd(-Math.abs(cal.loss))}` : '—'],
    ['Profit factor', cal.profitFactor == null ? '—' : cal.profitFactor.toFixed(2)],
    ['Max drawdown', cal.activeDays ? fmtUsd(-Math.abs(cal.mdd)) : '—', cal.mdd ? 'neg' : ''],
    ['Ngày lãi / lỗ', `${cal.pos} / ${cal.neg}`],
    ['Tỷ lệ ngày lãi', (cal.pos + cal.neg) ? `${(100 * cal.pos / (cal.pos + cal.neg)).toFixed(1)}%` : '—'],
    ['Buy / Sell', `${buys} / ${sells}`],
    ['Volume', (Number(officialStats.bought_cost || 0) + Number(officialStats.sold_income || 0) || cal.volume)
      ? fmtUsd(Number(officialStats.bought_cost || 0) + Number(officialStats.sold_income || 0) || cal.volume)
      : '—'],
  ];
  $('period-analysis').innerHTML = analysisRows.map(([key, value, cls = '']) =>
    `<div><dt>${key}</dt><dd class="${cls}">${value}</dd></div>`
  ).join('');

  const hasPeriodFlow = cal.activeDays > 0 || buys + sells > 0;
  const usePeriodDist = hasPeriodFlow && Object.keys(pnlStat).length && period !== 'all';
  const rd = usePeriodDist ? {
    gt_500: pnlStat.pnl_gt_5x_num,
    '200_500': pnlStat.pnl_2x_5x_num,
    '0_200': pnlStat.pnl_0x_2x_num,
    minus_50_0: pnlStat.pnl_nd5_0x_num,
    lt_minus_50: pnlStat.pnl_lt_nd5_num,
  } : (a.return_distribution || {});
  const rdItems = [
    ['>500%', rd.gt_500],
    ['200% ~ 500%', rd['200_500']],
    ['0% ~ 200%', rd['0_200']],
    ['-50% ~ 0%', rd.minus_50_0],
    ['<-50%', rd.lt_minus_50],
  ];
  const rdTotal = rdItems.reduce((sum, item) => sum + Number(item[1] || 0), 0);
  $('return-distribution').innerHTML = (!hasPeriodFlow && period !== 'all')
    ? '<div class="intraday-status">Không có lãi/lỗ trong kỳ này</div>'
    : distributionRows(rdItems, rdTotal, ['#3dd68c', '#42a879', '#397b62', '#b95660', '#f6465d']);

  const mc = a.avg_buy_mc_distribution || {};
  const mcItems = [
    ['$0–$100K', mc['0_100k']],
    ['$100K–$500K', mc['100k_500k']],
    ['>$500K', mc.gt_500k],
  ];
  $('mc-distribution').innerHTML = distributionRows(
    mcItems, Number(mc.sample_size || 0), ['#88b4ff', '#638fd4', '#4269a4']
  );

  const safe = a.safety || {};
  $('safety-check').innerHTML = [
    ['Honeypot', safe.honeypot_count],
    ['Token alert', safe.alert_count],
    ["Didn't buy", safe.didnt_buy_count],
    ['Tiny pool', safe.tiny_pool_count],
  ].map(([label, count]) =>
    `<div class="safety-item ${count ? 'bad' : 'ok'}">${label}<b>${count || 0}</b></div>`
  ).join('');

  const calendarMetadata = state.report.daily_calendar_metadata || {};
  $('period-note').textContent = calendarMetadata.is_official_gmgn_calendar
    ? 'Ngày không có lãi/lỗ được để trống, không tô màu và không ghi $0.00. Analysis lấy MDD, profit factor và ngày lãi/lỗ từ PNL Calendar UTC+0.'
    : 'Lịch cũ chỉ có realized PnL từ sell. Hãy bấm Tải GMGN để lấy PNL Calendar đầy đủ từ GMGN web.';
  renderCalendar();
}

function renderIntraday(report) {
  const summary = report.summary || {};
  $('intraday-status').classList.add('hidden');
  $('intraday-summary').innerHTML = [
    kpi('Tổng lãi', fmtUsd(summary.profit_usd), 'pos'),
    kpi('Tổng lỗ', fmtUsd(-Number(summary.loss_usd || 0)), 'neg'),
    kpi('PnL ròng', fmtUsd(summary.net_pnl_usd), pnlClass(summary.net_pnl_usd)),
    kpi('Ngày có timestamp', `${summary.matched_days || 0} / ${summary.trading_days || 0}`),
    kpi('Độ phủ theo ngày', `${Number(summary.coverage_pct || 0).toFixed(1)}%`),
    kpi(
      'Chưa phân bổ lãi / lỗ',
      `${fmtUsd(summary.unallocated_profit_usd)} / ${fmtUsd(-Number(summary.unallocated_loss_usd || 0))}`,
    ),
  ].join('');

  if (report.intraday_data_status === 'NOT_COLLECTED') {
    $('session-stats').innerHTML = '<div class="intraday-status">Chưa có activity timestamp lưu trên máy.</div>';
    $('hour-chart').innerHTML = '<div class="intraday-status">Không tự gọi GMGN khi tra cứu wallet.</div>';
    $('loss-timing').innerHTML = '<div class="intraday-status">Cần cache activity để phân tích theo giờ/phiên.</div>';
    $('weekday-stats').innerHTML = '';
    $('intraday-note').textContent =
      'PnL tổng và calendar vẫn lấy hoàn toàn từ dữ liệu local. Chỉ bấm “Cập nhật hoạt động” khi muốn chủ động tải activity mới từ GMGN.';
    return;
  }

  $('session-stats').innerHTML = (report.sessions || []).map((row) => `
    <div class="timing-row">
      <small>${escapeHtml(row.label)}</small>
      <strong class="${pnlClass(row.net_pnl_usd)}">${fmtUsd(row.net_pnl_usd)}</strong>
      <span class="gross">Lãi <b class="pos">${fmtUsd(row.profit_usd)}</b> · Lỗ <b class="neg">${fmtUsd(-row.loss_usd)}</b> · ${row.loss_days || 0} ngày lỗ · ${row.tx_count || 0} tx</span>
    </div>
  `).join('');

  const hours = report.hourly || [];
  const maxHour = Math.max(1, ...hours.map((row) => Math.abs(Number(row.net_pnl_usd || 0))));
  $('hour-chart').innerHTML = hours.map((row, index) => {
    const height = Math.max(2, 112 * Math.abs(Number(row.net_pnl_usd || 0)) / maxHour);
    const title = `${row.label} · Lãi ${fmtUsd(row.profit_usd)} · Lỗ ${fmtUsd(-row.loss_usd)} · Net ${fmtUsd(row.net_pnl_usd)}`;
    return `<div class="hour-column" title="${title}">
      <div class="hour-bar ${Number(row.net_pnl_usd) < 0 ? 'neg' : ''}" style="--bar-height:${height}px"></div>
      <span class="hour-label">${index % 2 === 0 ? String(index).padStart(2, '0') : ''}</span>
    </div>`;
  }).join('');

  const loss = report.loss_timing || {};
  const lossItems = [
    ['Khung giờ lỗ nhiều', loss.worst_hour, loss.worst_hour ? fmtUsd(-loss.worst_hour.loss_usd) : '—'],
    ['Khung giờ hay lỗ', loss.most_frequent_loss_hour, loss.most_frequent_loss_hour ? `${loss.most_frequent_loss_hour.loss_days} ngày` : '—'],
    ['Phiên lỗ nhiều', loss.worst_session, loss.worst_session ? fmtUsd(-loss.worst_session.loss_usd) : '—'],
    ['Thứ lỗ nhiều', loss.worst_weekday, loss.worst_weekday ? fmtUsd(-loss.worst_weekday.loss_usd) : '—'],
  ];
  $('loss-timing').innerHTML = lossItems.map(([label, row, value]) => `
    <div class="loss-item"><span>${label}<br><small>${escapeHtml(row?.label || '—')}</small></span><b>${value}</b></div>
  `).join('');
  $('weekday-stats').innerHTML = (report.weekdays || []).map((row) => `
    <div class="weekday-cell" title="Lãi ${fmtUsd(row.profit_usd)} · Lỗ ${fmtUsd(-row.loss_usd)}">
      <span>${escapeHtml(row.label)}</span>
      <b class="${pnlClass(row.net_pnl_usd)}">${fmtUsd(row.net_pnl_usd)}</b>
    </div>
  `).join('');
  $('intraday-note').textContent =
    'Tổng lãi/lỗ lấy từ GMGN daily_profits. Khung giờ được phân bổ theo timestamp sell/transfer-out; phần không có hoạt động tương ứng được giữ ở “chưa phân bổ”, không suy đoán thời điểm.';
}

async function loadIntraday(address, refresh = false) {
  if (state.intradayLoading) return;
  state.intradayLoading = true;
  $('intraday-refresh').disabled = true;
  $('intraday-status').classList.remove('hidden');
  $('intraday-status').textContent = refresh
    ? 'Đang cập nhật sell/transfer-out từ GMGN…'
    : 'Đang tải thống kê theo giờ/phiên…';
  $('intraday-summary').innerHTML = '';
  const query = refresh ? '?refresh=true' : '';
  try {
    const res = await fetch(`/api/pnl/intraday/${encodeURIComponent(address)}${query}`);
    const payload = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(payload.detail || res.statusText);
    if (state.wallet !== address) return;
    state.intraday = payload;
    renderIntraday(payload);
  } catch (error) {
    if (state.wallet !== address) return;
    $('intraday-status').textContent = error.message || 'Không tải được thống kê theo giờ';
  } finally {
    state.intradayLoading = false;
    $('intraday-refresh').disabled = false;
  }
}

function detailRows(id, rows) {
  $(id).innerHTML = rows.map(([label, value, cls = '']) => `
    <div class="analysis-detail-row">
      <span>${escapeHtml(label)}</span>
      <b class="${cls}">${escapeHtml(value == null || value === '' ? '—' : value)}</b>
    </div>
  `).join('');
}

function fmtStatRange(range, formatter = fmtUsd) {
  if (!range || range.p25 == null || range.p75 == null) return 'Chưa đủ dữ liệu';
  return `${formatter(range.p25)} – ${formatter(range.p75)} · n=${range.sample_size || 0}`;
}

function renderWalletAnalysis(report) {
  const info = report.information_value || {};
  const meta = report.meta || {};
  const performance = report.performance_profile || {};
  const allTime = performance.all_time || {};
  const daily = performance.daily_calendar || {};
  const setup = report.alpha_setup || {};
  const entry = report.entry_profile || {};
  const conviction = report.conviction_behavior || {};
  const timing = report.timing_profile || {};
  const intraday = timing.intraday || {};
  const quality = report.data_quality || {};
  const prediction = report.new_token_predictive_power || {};
  const risk = report.risk_profile || {};
  const consistency = report.consistency_profile || {};
  const regime = report.regime_profile || {};
  const walletClass = report.wallet_class || {};
  const behavior = report.behavior_profile || {};
  const holding = behavior.holding || {};
  const exitProfile = behavior.exit || {};
  const scale = behavior.scale || {};
  const concentration = behavior.concentration || {};
  const transfer = behavior.transfer || {};
  const session = behavior.session || {};

  $('wallet-analysis-status').classList.add('hidden');
  $('wallet-analysis-grid').classList.remove('hidden');
  $('analysis-source').textContent = 'SQLite · tính khi tra cứu';
  $('analysis-source').classList.add('ok');
  const mdd = risk.max_realized_pnl_drawdown_usd ?? daily.max_drawdown_usd ?? risk.max_drawdown_usd;
  const profitFactor = daily.profit_factor;
  const winRate = allTime.token_win_rate_pct;
  const dayWin = daily.profitable_day_rate_pct;
  $('wallet-analysis-summary').innerHTML = [
    kpi('Total PnL', fmtUsd(allTime.total_profit_usd), pnlClass(allTime.total_profit_usd)),
    kpi('ROI trên cost', allTime.roi_on_cost_pct == null ? '—' : fmtPct(allTime.roi_on_cost_pct), pnlClass(allTime.roi_on_cost_pct)),
    kpi('Sụt giảm PnL đã chốt', mdd == null ? '—' : fmtUsd(-Math.abs(Number(mdd))), 'neg'),
    kpi('Profit factor', profitFactor == null ? '—' : Number(profitFactor).toFixed(2), Number(profitFactor) >= 1 ? 'pos' : 'neg'),
    kpi('Win rate token', winRate == null ? '—' : `${Number(winRate).toFixed(1)}%`, winRate == null ? '' : pnlClass(Number(winRate) - 50)),
    kpi('Ngày có lãi', dayWin == null ? '—' : `${Number(dayWin).toFixed(1)}%`, dayWin == null ? '' : pnlClass(Number(dayWin) - 50)),
    kpi('CVaR 95%', fmtUsd(risk.cvar_95_usd), pnlClass(risk.cvar_95_usd)),
    kpi('Underwater', risk.max_underwater_days == null ? '—' : `${risk.max_underwater_days} ngày`, Number(risk.max_underwater_days || 0) >= 7 ? 'neg' : ''),
  ].join('');

  detailRows('analysis-quality', [
    ['MDD danh mục', 'Chưa xác định — cần lịch sử định giá và điều chỉnh nạp/rút'],
    ['Primary label', walletClass.primary_wallet_value || '—'],
    ['Tier / Grade', `${info.tier || '—'} / ${info.grade || '—'}`],
    ['Điểm lịch sử', info.composite_alpha_score == null ? '—' : `${Number(info.composite_alpha_score).toFixed(1)} / 100`, 'good'],
    ['Hành vi', (walletClass.trading_behavior || []).slice(0, 4).join(', ') || '—'],
    ['Kết luận', info.actionable_assessment || '—'],
  ]);
  detailRows('analysis-performance', [
    ['Realized / Unrealized', `${fmtUsd(allTime.realized_profit_usd)} / ${fmtUsd(allTime.unrealized_profit_usd)}`],
    ['Ngày lãi / lỗ', `${daily.positive_days || 0} / ${daily.negative_days || 0}`],
    ['Tỷ lệ ngày có lãi', daily.profitable_day_rate_pct == null ? '—' : `${Number(daily.profitable_day_rate_pct).toFixed(1)}%`],
    ['Profit factor ngày', daily.profit_factor == null ? '—' : Number(daily.profit_factor).toFixed(2)],
    ['CVaR 95% ngày', fmtUsd(risk.cvar_95_usd), pnlClass(risk.cvar_95_usd)],
    ['Biến động ngày', fmtUsd(risk.daily_volatility_usd)],
    ['Underwater dài nhất', risk.max_underwater_days == null ? '—' : `${risk.max_underwater_days} ngày`],
    ['Win rate token', allTime.token_win_rate_pct == null ? '—' : `${Number(allTime.token_win_rate_pct).toFixed(1)}%`],
    ['Trạng thái token sample', allTime.token_metrics_status || '—'],
  ]);
  detailRows('analysis-setup', [
    ['Trạng thái', setup.status || '—'],
    ['Setup tốt nhất', setup.optimal_setup_name || setup.optimal_setup_archetype || '—'],
    ['Setup nên tránh', setup.avoid_setup_name || setup.avoid_setup_archetype || '—'],
    ['Vùng market cap', fmtStatRange(entry.optimal_market_cap_range)],
    ['Tuổi token lúc vào', fmtStatRange(entry.optimal_token_age_range, (v) => `${Number(v).toFixed(1)}h`)],
    ['Vùng thanh khoản', fmtStatRange(entry.optimal_liquidity_range)],
  ]);
  detailRows('analysis-conviction', [
    ['Kết luận', conviction.conviction_verdict || '—'],
    ['Cỡ mẫu', String(conviction.sample_size || 0)],
    ['Median buy', fmtUsd(conviction.median_buy_usd)],
    ['Tương quan size–ROI', conviction.size_roi_correlation == null ? '—' : Number(conviction.size_roi_correlation).toFixed(3)],
    ['Chênh ROI size lớn/nhỏ', conviction.large_vs_small_median_roi_gap_pct == null ? '—' : fmtPct(conviction.large_vs_small_median_roi_gap_pct)],
    ['Diễn giải', conviction.conviction_analysis || '—'],
  ]);
  const bestDay = (timing.daily || {}).best_weekday;
  const worstDay = (timing.daily || {}).worst_weekday;
  detailRows('analysis-timing', [
    ['Ngày tốt nhất', bestDay ? `${bestDay.label} · ${fmtUsd(bestDay.net_pnl_usd)}` : '—', 'good'],
    ['Ngày xấu nhất', worstDay ? `${worstDay.label} · ${fmtUsd(worstDay.net_pnl_usd)}` : '—', 'bad'],
    ['Khung dữ liệu ngày', `${daily.start_date || '—'} → ${daily.end_date || '—'}`],
    ['Intraday', intraday.status || 'NOT_COLLECTED'],
    ['PnL timestamp quan sát', intraday.exact_attribution_pct == null ? '—' : `${Number(intraday.exact_attribution_pct).toFixed(1)}%`],
    ['Chất lượng timestamp', intraday.attribution_quality || '—'],
    ['Giờ lỗ nhiều', intraday.loss_timing?.worst_hour?.label || 'Chưa có timestamp'],
    ['Phiên lỗ nhiều', intraday.loss_timing?.worst_session?.label || 'Chưa có timestamp'],
    ['Chuỗi ngày lỗ dài nhất', `${consistency.longest_loss_streak_days || 0} ngày`],
    ['PnL 30D so kỳ trước', fmtUsd(regime.pnl_change_usd), pnlClass(regime.pnl_change_usd)],
  ]);
  const sectionStatus = quality.section_status || {};
  detailRows('analysis-data-quality', [
    ['Nguồn pipeline', report.pipeline?.input || '—'],
    ['Holdings / Setup', sectionStatus.token_setup || '—'],
    ['Daily PnL', sectionStatus.daily_performance || '—'],
    ['Predictive power', sectionStatus.predictive_power || prediction.predictive_tier || '—'],
    ['Formula version', report.pipeline?.formula_version || report.analysis_version?.formula_version || '—'],
    ['Phân tích đã lưu', report.pipeline?.analysis_persisted ? 'YES' : 'NO'],
    ['Khuyến nghị sử dụng', prediction.copy_trade_recommendation || '—'],
    ['Claim không hỗ trợ', String((quality.unsupported_claims || []).length)],
  ]);
  detailRows('analysis-behavior', [
    ['Giữ lệnh', holding.style || '—'],
    ['Median hold lãi / lỗ', `${holding.median_winner_hold_hours == null ? '—' : `${Number(holding.median_winner_hold_hours).toFixed(1)}h`} / ${holding.median_loser_hold_hours == null ? '—' : `${Number(holding.median_loser_hold_hours).toFixed(1)}h`}`],
    ['Cắt lỗ sớm', holding.early_cut_loss_rate_pct == null ? '—' : `${Number(holding.early_cut_loss_rate_pct).toFixed(1)}%`],
    ['Thoát lệnh', exitProfile.style || '—'],
    ['MC lúc bán / lúc mua', exitProfile.median_exit_mc_multiple == null ? '—' : `${Number(exitProfile.median_exit_mc_multiple).toFixed(2)}x`],
    ['Scale in/out', scale.style || '—'],
    ['Phiên thiên lệch', session.dominant_session || 'Chưa đủ timestamp'],
  ]);
  detailRows('analysis-structure', [
    ['Tập trung vị thế', concentration.style || '—'],
    ['Top-1 open', concentration.top1_open_pct == null ? '—' : `${Number(concentration.top1_open_pct).toFixed(1)}%`],
    ['Top-5 PnL share', concentration.top5_abs_pnl_share_pct == null ? '—' : `${Number(concentration.top5_abs_pnl_share_pct).toFixed(1)}%`],
    ['Nguồn token', transfer.style || '—'],
    ['Tỷ lệ transfer-related', transfer.transfer_related_token_rate_pct == null ? '—' : `${Number(transfer.transfer_related_token_rate_pct).toFixed(1)}%`],
    ['Tuổi token sở trường', (behavior.token_age || {}).best_band || 'INSUFFICIENT_DATA'],
  ]);
  $('wallet-analysis-note').textContent =
    'Bốn lớp label: hiệu suất lịch sử, hành vi giao dịch, predictive (NOT_MEASURED), và chất lượng dữ liệu. Insight thiếu sample được giữ INSUFFICIENT_DATA, không suy đoán.';
}

async function loadWalletAnalysis(address) {
  $('wallet-analysis-status').classList.remove('hidden');
  $('wallet-analysis-status').textContent = 'Đang đọc snapshot SQLite và chạy thống kê…';
  $('wallet-analysis-grid').classList.add('hidden');
  $('wallet-analysis-summary').innerHTML = '';
  try {
    const res = await fetch(`/api/pnl/analysis/${encodeURIComponent(address)}`);
    const payload = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(payload.detail || res.statusText);
    if (state.wallet !== address) return;
    state.walletAnalysis = payload;
    renderWalletAnalysis(payload);
  } catch (error) {
    if (state.wallet !== address) return;
    $('wallet-analysis-status').textContent = error.message || 'Không phân tích được dữ liệu database';
  }
}

function renderBreakdown(stats) {
  $('top-winners').innerHTML = stats.winners.length
    ? stats.winners.map((r) =>
      `<li><span>$${escapeHtml(r.token_symbol || '?')}</span><span class="val pos">${fmtUsd(r.total_profit_usd)}</span></li>`
    ).join('')
    : '<li class="empty">Không có coin lãi</li>';
  $('top-losers').innerHTML = stats.losers.length
    ? stats.losers.map((r) =>
      `<li><span>$${escapeHtml(r.token_symbol || '?')}</span><span class="val neg">${fmtUsd(r.total_profit_usd)}</span></li>`
    ).join('')
    : '<li class="empty">Không có coin lỗ</li>';
}

function unrealizedCell(r) {
  if (r.unrealized_status === 'sell_all' || r.is_closed) {
    return '<span class="status-sell">Sell All</span>';
  }
  const v = r.unrealized_profit_usd;
  if (v == null) return '—';
  return `<span class="${pnlClass(v)}">${fmtUsd(v)} / ${fmtPct(r.unrealized_profit_pct)}</span>`;
}

function realizedCell(r) {
  if (r.realized_status === 'hodl' || r.is_hodl) {
    return '<span class="status-hodl">HODL</span>';
  }
  const v = r.realized_profit_usd;
  if (v == null) return '—';
  return `<span class="${pnlClass(v)}">${fmtUsd(v)} / ${fmtPct(r.realized_profit_pct)}</span>`;
}

function formatBalance(r) {
  const usd = r.balance_usd;
  const bal = r.balance;
  if ((!usd || usd < 0.01) && (!bal || bal <= 0)) return '$0';
  const balStr = bal >= 1_000_000 ? `${(bal / 1_000_000).toFixed(2)}M` : bal >= 1_000 ? `${(bal / 1_000).toFixed(1)}K` : String(Math.round(bal || 0));
  return `${fmtUsd(usd)}<br><small>${balStr}</small>`;
}

function applyFilters() {
  const q = ($('token-search').value || '').toLowerCase();
  const hideDidnt = $('hide-didnt-buy').checked;
  const hideTiny = $('hide-tiny-pool').checked;
  const hideHoney = $('hide-honeypot').checked;
  const hideClosed = $('hide-closed').checked;
  const sortKey = $('sort-by').value;

  let rows = [...state.rows];
  if (q) {
    rows = rows.filter((r) =>
      (r.token_symbol || '').toLowerCase().includes(q) ||
      (r.token_name || '').toLowerCase().includes(q) ||
      (r.token_address || '').toLowerCase().includes(q)
    );
  }
  if (hideDidnt) rows = rows.filter((r) => !r.didnt_buy);
  if (hideTiny) rows = rows.filter((r) => !r.is_tiny_pool);
  if (hideHoney) rows = rows.filter((r) => !r.is_honeypot);
  if (hideClosed) rows = rows.filter((r) => !r.is_closed);
  rows.sort((a, b) => (Number(b[sortKey]) || 0) - (Number(a[sortKey]) || 0));
  state.filtered = rows;
  renderTable();
  const stats = analyzeRows(rows);
  if (state.report) renderSummary(state.report, stats);
  renderBreakdown(stats);
}

function renderTable() {
  const tbody = $('pnl-tbody');
  if (!state.filtered.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="empty">Không có token phù hợp bộ lọc</td></tr>';
    $('table-foot').textContent = '';
    return;
  }
  tbody.innerHTML = state.filtered.map((r) => {
    const tp = r.total_profit_usd;
    const logo = r.token_logo
      ? `<img src="${escapeHtml(r.token_logo)}" alt="" onerror="this.style.visibility='hidden'">`
      : '<span class="logo-fallback"></span>';
    const warn = r.is_honeypot || r.is_alert ? '<span class="warn" title="honeypot / alert">⚠</span>' : '';
    const boughtMc = r.bought_mc || r.current_mc;
    const soldMc = r.sold_mc || (r.is_closed ? r.current_mc : null);
    return `<tr>
      <td>
        <div class="token-cell">
          ${logo}
          <div>
            <div class="sym">$${escapeHtml(r.token_symbol || '?')} ${warn}</div>
            <div class="last">${escapeHtml(r.last_active_display || '')}</div>
          </div>
        </div>
      </td>
      <td>${unrealizedCell(r)}</td>
      <td>${realizedCell(r)}</td>
      <td class="${pnlClass(tp)}"><strong>${fmtUsd(tp)}</strong><br><small>${fmtPct(r.total_profit_pct)}</small></td>
      <td>${formatBalance(r)}</td>
      <td>${r.position_pct != null ? `${Number(r.position_pct).toFixed(r.position_pct >= 10 ? 0 : 2)}%` : '—'}</td>
      <td>${escapeHtml(r.holding_duration_display || '—')}</td>
      <td>${fmtUsd(r.bought_usd)}<br><small>${fmtMc(boughtMc)}</small></td>
      <td>${fmtUsd(r.sold_usd)}<br><small>${fmtMc(soldMc)}</small></td>
      <td class="txs"><span class="buy">${r.buy_tx_count || 0}</span> / <span class="sell">${r.sell_tx_count || 0}</span></td>
    </tr>`;
  }).join('');
  $('table-foot').textContent = `Hiển thị ${state.filtered.length} / ${state.rows.length} token · nguồn GMGN holdings`;
}

async function lookupWallet(address, exportLabel = false) {
  address = String(address || '').trim();
  if (!looksLikeSolanaAddress(address)) {
    $('address-input').setAttribute('aria-invalid', 'true');
    $('status-pill').textContent = 'Địa chỉ Solana không hợp lệ';
    $('status-pill').classList.remove('ok');
    $('address-input').focus();
    return;
  }
  $('address-input').removeAttribute('aria-invalid');
  state.wallet = address;
  state.labelResult = null;
  $('address-input').value = address;
  const url = new URL(location.href);
  url.searchParams.set('wallet', address);
  history.replaceState(null, '', url);

  $('status-pill').textContent = exportLabel ? 'Đang xuất label từ DB…' : 'Đang tra cứu…';
  $('status-pill').classList.remove('ok');
  $('pnl-tbody').innerHTML = '<tr><td colspan="10" class="empty">Đang tải bảng Recent PnL…</td></tr>';
  $('wallet-board').classList.remove('hidden');
  if (exportLabel) setLabelLoading(true);
  else {
    $('label-result').classList.add('hidden');
    $('label-actions').classList.add('hidden');
    $('label-status').className = 'label-status';
    $('label-status').textContent = 'Đang mở snapshot và chuẩn bị label local…';
    setLabelProgress('');
  }

  let res;
  try {
    res = exportLabel
      ? await fetch('/api/pnl/analyze-and-label', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ wallet_address: address, refresh: false }),
      })
      : await fetch(`/api/pnl/lookup/${encodeURIComponent(address)}`);
  } catch (error) {
    const message = error.message || 'Không kết nối được server.';
    $('status-pill').textContent = message;
    $('label-status').className = 'label-status error';
    $('label-status').textContent = `${message} Kiểm tra server rồi thử lại.`;
    $('create-label-btn').disabled = false;
    setLabelProgress('');
    return;
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    $('status-pill').textContent = err.detail || 'Không tải được ví';
    $('pnl-tbody').innerHTML = `<tr><td colspan="10" class="empty">${escapeHtml(err.detail || res.statusText)}</td></tr>`;
    $('label-status').className = 'label-status error';
    $('label-status').textContent = err.detail || 'Không tạo được label.';
    $('create-label-btn').disabled = false;
    setLabelProgress('');
    return;
  }
  const payload = await res.json();
  const report = payload.dashboard || payload;
  state.report = report;
  state.rows = report.rows || [];
  const calendarRows = report.daily_calendar || [];
  if (calendarRows.length) {
    const latest = calendarRows[calendarRows.length - 1].date;
    state.calendarMonth = new Date(`${latest}T00:00:00Z`);
  }
  $('wallet-title').textContent = report.wallet_name || address.slice(0, 8);
  $('wallet-address').textContent = address;
  $('gmgn-link').href = `https://gmgn.ai/sol/address/${address}`;
  const collection = report.collection || {};
  const tradedTokens = state.rows.filter((row) => (
    Number(row.bought_usd || 0) > 0 || Number(row.sold_usd || 0) > 0
  )).length;
  const pageText = collection.pages_fetched
    ? ` · đã tải hết ${collection.pages_fetched} trang`
    : ' · đã tải hết phân trang';
  $('status-pill').textContent = collection.complete
    ? `${state.rows.length} token lịch sử · ${tradedTokens} có giao dịch${pageText}`
    : `${state.rows.length} token lịch sử · snapshot chưa xác nhận đủ trang`;
  $('status-pill').classList.add('ok');
  if (payload.label) renderWalletLabel(payload);
  else loadLocalLabel(address);
  renderPeriodAnalytics(state.period);
  applyFilters();
  loadWalletAnalysis(address);
}

async function exportCurrent(format) {
  const buttons = [$('export-json-btn'), $('export-csv-btn')];
  buttons.forEach((button) => { button.disabled = true; });
  $('status-pill').classList.remove('ok');
  $('status-pill').textContent = 'Đang tính lại labels từ database…';
  try {
    while (true) {
      const response = await fetch('/api/pnl/backfill/status');
      const job = await response.json();
      if (job.status === 'failed') throw new Error(job.error || 'Backfill data thiếu thất bại');
      if (job.status !== 'running') {
        $('status-pill').textContent = 'Đang tạo file export…';
        window.location.href = `/api/pnl/exports/download?format=${format}&t=${Date.now()}`;
        $('status-pill').textContent = `Đã xuất ${format.toUpperCase()} cho toàn bộ cohort`;
        $('status-pill').classList.add('ok');
        break;
      }
      $('status-pill').textContent = job.missing == null
        ? 'Server đang kiểm tra data thiếu…'
        : `Đang bổ sung ${job.processed || 0}/${Number(job.missing || 0) + Number(job.incremental || 0)} ví có data thiếu/mới…`;
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
  } catch (error) {
    $('status-pill').textContent = error.message || 'Export thất bại';
  } finally {
    buttons.forEach((button) => { button.disabled = false; });
  }
}

function boot() {
  $('export-json-btn').addEventListener('click', () => exportCurrent('json'));
  $('export-csv-btn').addEventListener('click', () => exportCurrent('csv'));
  $('address-form').addEventListener('submit', (e) => {
    e.preventDefault();
    lookupWallet($('address-input').value, false);
  });
  $('create-label-btn').addEventListener('click', () => {
    lookupWallet($('address-input').value, true);
  });
  $('download-label-json').addEventListener('click', () => downloadLabel('json'));
  $('download-label-csv').addEventListener('click', () => downloadLabel('csv'));
  document.querySelectorAll('#period-tabs button:not(:disabled)').forEach((button) => {
    button.addEventListener('click', () => renderPeriodAnalytics(button.dataset.period));
  });
  $('calendar-prev').addEventListener('click', () => {
    state.calendarMonth = new Date(Date.UTC(
      state.calendarMonth.getUTCFullYear(),
      state.calendarMonth.getUTCMonth() - 1,
      1,
    ));
    renderCalendar();
  });
  $('calendar-next').addEventListener('click', () => {
    state.calendarMonth = new Date(Date.UTC(
      state.calendarMonth.getUTCFullYear(),
      state.calendarMonth.getUTCMonth() + 1,
      1,
    ));
    renderCalendar();
  });
  ['token-search', 'hide-didnt-buy', 'hide-tiny-pool', 'hide-honeypot', 'hide-closed', 'sort-by'].forEach((id) => {
    const el = $(id);
    el.addEventListener(el.type === 'search' || el.tagName === 'INPUT' && el.type === 'text' ? 'input' : (el.type === 'checkbox' ? 'change' : 'change'), applyFilters);
  });
  $('token-search').addEventListener('input', applyFilters);

  const w = new URLSearchParams(location.search).get('wallet');
  if (w) lookupWallet(w);
}

document.addEventListener('DOMContentLoaded', boot);
