/* Cohort monitor: lazy canonical detail loading keeps the 192-wallet table fast. */
(function cohortMonitor() {
  'use strict';

  const state = {
    rows: [],
    filtered: [],
    details: new Map(),
    pending: new Map(),
    selected: null,
    hovered: null,
    lastLoadedAt: 0,
  };

  const $ = (id) => document.getElementById(id);
  const text = (value) => String(value == null ? '' : value)
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
  const n = (value) => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  };
  const has = (value) => n(value) != null;
  const fmtUsd = (value) => {
    const valueNumber = n(value);
    if (valueNumber == null) return '—';
    const sign = valueNumber < 0 ? '-' : '';
    const abs = Math.abs(valueNumber);
    if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(2)}M`;
    if (abs >= 1e3) return `${sign}$${(abs / 1e3).toFixed(abs >= 1e4 ? 1 : 2)}K`;
    return `${sign}$${abs.toFixed(abs < 10 ? 2 : 0)}`;
  };
  const fmtPct = (value) => {
    const valueNumber = n(value);
    return valueNumber == null ? '—' : `${valueNumber.toFixed(1)}%`;
  };
  const fmtNumber = (value) => {
    const valueNumber = n(value);
    return valueNumber == null ? '—' : valueNumber.toLocaleString('en-US', { maximumFractionDigits: 2 });
  };
  const fmtDate = (value) => {
    const timestamp = n(value);
    if (timestamp == null || timestamp <= 0) return '—';
    return new Intl.DateTimeFormat('vi-VN', {
      day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Ho_Chi_Minh',
    }).format(new Date(timestamp * 1000));
  };

  function canonicalView(payload) {
    const wallet = payload?.wallet || payload || {};
    const labelsCurrent = wallet.labels_current || {};
    const labelPayload = labelsCurrent.payload || {};
    const inputs = wallet.label_inputs_current || {};
    const metrics = labelPayload.metrics || {};
    const groups = {
      ...(metrics.group_features || {}),
      labels: metrics.group_labels || [],
    };
    const labels = labelPayload.labels || {};
    return {
      state: wallet.state || {},
      labelsCurrent,
      labelPayload,
      inputs,
      metrics,
      labels,
      groups,
    };
  }

  // The cohort index already contains every label input and projected label.  Keeping
  // a normalized view here makes hover/focus instant and avoids 192 detail requests.
  function cohortIndexView(row) {
    const labelPayload = row?.label || {};
    const inputs = row?.features || {};
    const metrics = labelPayload.metrics || {};
    return {
      state: {
        ...(row || {}),
        data_status: row?.data_status || 'MISSING',
      },
      labelsCurrent: { payload: labelPayload },
      labelPayload,
      inputs,
      metrics,
      labels: labelPayload.labels || {},
      groups: row?.group_labels || {},
    };
  }

  const groupMeta = {
    g1: { code: 'G1', title: 'Early Microcap Specialist', accent: 'g1', summary: 'Vào microcap $30K–$150K trong ≤60 phút' },
    g2: { code: 'G2', title: 'Whale Conviction', accent: 'g2', summary: 'Vol lớn và conviction đạt tối thiểu 3/5' },
    g3: { code: 'G3', title: 'Narrative Basket', accent: 'g3', summary: 'Mua nhiều CA cùng một narrative trong một cửa sổ thời gian' },
    g4: { code: 'G4', title: 'Unicon Holder', accent: 'g4', summary: 'Có token lời thực nhận ≥$100K và hành vi holder' },
  };

  function groupLabel(group) {
    return group?.label || null;
  }

  function activeGroups(view) {
    const output = [];
    Object.keys(groupMeta).forEach((key) => {
      const group = view.groups?.[key] || {};
      const label = groupLabel(group);
      if (group.qualified && label) {
        output.push({ key, ...groupMeta[key], label, raw: group });
      }
    });
    return output;
  }

  function groupChips(view) {
    const groups = activeGroups(view);
    if (!groups.length) return '<span class="cohort-chip muted">Chưa đạt</span>';
    return groups.map((group) => `<span class="cohort-chip ${group.accent}">${text(group.title)}</span>`).join('');
  }

  function input(view, field) {
    const direct = view.inputs?.[field];
    if (has(direct)) return n(direct);
    const metrics = view.metrics || {};
    const aliases = {
      money_earned_primary_usd: 'absolute_pnl_usd',
      current_portfolio_usd: 'ending_equity_usd',
      capital_base_usd: 'starting_equity_usd',
      max_realized_pnl_drawdown_usd: 'max_drawdown_usd',
    };
    return has(metrics[aliases[field]]) ? n(metrics[aliases[field]]) : null;
  }

  function bucket(value, ranges) {
    if (!has(value)) return { label: 'Chưa đủ dữ liệu', threshold: 'Không có giá trị số' };
    for (const range of ranges) {
      if (range.when(value)) return { label: range.label, threshold: range.threshold };
    }
    return { label: 'Chưa phân loại', threshold: 'Ngoài miền ngưỡng đã cấu hình' };
  }
  const lt = (limit, label, threshold) => ({ when: (value) => value < limit, label, threshold });
  const between = (low, high, label, threshold) => ({ when: (value) => value >= low && value < high, label, threshold });
  const atLeast = (limit, label, threshold) => ({ when: (value) => value >= limit, label, threshold });

  const LABEL_RULES = [
    { field: 'money_earned_primary_usd', title: 'money_earned_primary_usd', format: fmtUsd, rules: [lt(-100000, 'Lỗ nặng', '< -$100K'), between(-100000, 0, 'C · Lỗ', '-$100K → <$0'), between(0, 50000, 'B · Lãi nhỏ', '$0 → <$50K'), between(50000, 250000, 'A- · Lãi vừa', '$50K → <$250K'), between(250000, 1000000, 'A+ · Lãi lớn', '$250K → <$1M'), atLeast(1000000, 'S · Lãi cực lớn', '≥ $1M')] },
    { field: 'capital_base_usd', title: 'capital_base_usd', format: fmtUsd, rules: [lt(10000, 'Vốn nhỏ', '< $10K'), between(10000, 100000, 'Vốn vừa', '$10K → <$100K'), between(100000, 1000000, 'Vốn lớn', '$100K → <$1M'), atLeast(1000000, 'Vốn cực lớn', '≥ $1M')] },
    { field: 'current_portfolio_usd', title: 'current_portfolio_usd', format: fmtUsd, rules: [lt(1000, 'D · Rỗng / Dormant', '< $1K'), between(1000, 50000, 'C · Active nhỏ', '$1K → <$50K'), between(50000, 500000, 'A · Active lớn', '$50K → <$500K'), atLeast(500000, 'S · Major Active', '≥ $500K')] },
    { field: 'roi_on_trading_cost_pct', title: 'roi_on_trading_cost_pct', format: fmtPct, rules: [lt(0, 'ROI âm', '< 0%'), between(0, 10, 'C · ROI mỏng', '0% → <10%'), between(10, 25, 'B · ROI khá', '10% → <25%'), between(25, 50, 'A · ROI tốt', '25% → <50%'), atLeast(50, 'S · ROI xuất sắc', '≥50%')] },
    { field: 'pnl_over_deposits_pct', title: 'pnl_over_deposits_pct', format: fmtPct, special: (value, view) => input(view, 'capital_base_usd') != null && input(view, 'capital_base_usd') < 10000 ? { label: 'NA', threshold: 'capital_base < $10K' } : null, rules: [lt(0, 'Âm vốn', '<0%'), between(0, 100, 'B · Lãi dưới 1x vốn', '0% → <100%'), between(100, 500, 'A · Lãi 1–5x vốn', '100% → <500%'), atLeast(500, 'S · Lãi ≥5x vốn', '≥500%')] },
    { field: 'jackpot_dependency_pct', title: 'jackpot_dependency_pct', format: fmtPct, rules: [lt(25, 'S · Đều tay', '<25%'), between(25, 50, 'A · Cân bằng', '25% → <50%'), between(50, 80, 'C · Phụ thuộc kèo lớn', '50% → <80%'), atLeast(80, 'D · One-hit', '≥80%')] },
    { field: 'portfolio_max_drawdown_pct', title: 'portfolio_max_drawdown_pct', format: fmtPct, rules: [lt(10, 'S · Kiểm soát drawdown rất tốt', '<10%'), between(10, 30, 'A · Drawdown ổn', '10% → <30%'), between(30, 90, 'C · Drawdown nặng', '30% → <90%'), { when: (value, view) => value >= 90 && (input(view, 'money_earned_primary_usd') || 0) > 0, label: 'D · Trả lại gần hết lợi nhuận', 'threshold': '≥90% & PnL > 0' }, atLeast(90, 'E · Chưa tạo được profit peak', '≥90% & PnL ≤ 0')] },
    { field: 'max_realized_pnl_drawdown_usd', title: 'max_realized_pnl_drawdown_usd', format: fmtUsd, rules: [lt(10000, 'LOW · Sụt nhỏ', '< $10K'), between(10000, 100000, 'MODERATE · Sụt vừa', '$10K → <$100K'), between(100000, 500000, 'HIGH · Sụt lớn', '$100K → <$500K'), atLeast(500000, 'EXTREME · Sụt rất lớn', '≥ $500K')] },
    { field: 'median_volume_last_30_tokens_usd', title: 'median_volume_last_30_tokens_usd', format: fmtUsd, rules: [lt(1000, 'D · Micro Position', '< $1K'), between(1000, 5000, 'C · Medium Position', '$1K → <$5K'), between(5000, 25000, 'A · Large Position', '$5K → <$25K'), atLeast(25000, 'S · Whale Position', '≥ $25K')] },
    { field: 'median_buy_order_last_30_tokens_usd', title: 'median_buy_order_last_30_tokens_usd', format: fmtUsd, rules: [lt(500, 'D · Lệnh nhỏ', '< $500'), between(500, 1000, 'C · Lệnh vừa', '$500 → <$1K'), between(1000, 3000, 'A · Lệnh lớn', '$1K → <$3K'), atLeast(3000, 'S · Lệnh rất lớn', '≥ $3K')] },
    { field: 'longest_loss_streak_tokens', title: 'longest_loss_streak_tokens', format: fmtNumber, rules: [lt(10, 'S · Chuỗi thua ngắn', '<10'), between(10, 20, 'A · Bình thường', '10 → <20'), between(20, 30, 'B · Chuỗi thua dài', '20 → <30'), atLeast(30, 'C · Chuỗi thua rất dài', '≥30')] },
    { field: 'current_loss_streak_tokens', title: 'current_loss_streak_tokens', format: fmtNumber, rules: [lt(1, 'STABLE · Không có chuỗi thua', '0'), between(1, 3, 'WATCH · Thua nhẹ', '1–2'), between(3, 8, 'WARNING · Phong độ xấu', '3–7'), atLeast(8, 'ALERT · Chuỗi thua bất thường', '≥8')] },
    { field: 'withdrawal_count', title: 'withdrawal_count', format: fmtNumber, rules: [lt(1, 'Chưa rút', '0'), between(1, 10, 'Rút lẻ tẻ', '1–9'), between(10, 100, 'Rút thường xuyên', '10–99'), atLeast(100, 'Automation / Treasury-like', '≥100')] },
    { field: 'total_withdrawals_usd', title: 'total_withdrawals_usd', format: fmtUsd, rules: [lt(10000, 'Rút ít', '< $10K'), between(10000, 100000, 'Rút vừa', '$10K → <$100K'), between(100000, 1000000, 'Rút nhiều', '$100K → <$1M'), atLeast(1000000, 'Rút rất nhiều', '≥ $1M')] },
    { field: 'median_outflow_holding_pct', title: 'median_outflow_holding_pct', format: fmtPct, rules: [lt(10, 'Micro-exit / Bot-like', '<10%'), between(10, 40, 'Chốt rải / Scale-out', '10% → <40%'), between(40, 90, 'Thoát mạnh', '40% → <90%'), atLeast(90, 'Xả gần sạch', '≥90%')] },
    { field: 'median_transfer_holding_pct', title: 'median_transfer_holding_pct', format: fmtPct, special: (value) => { const numeric = n(value); return numeric == null || numeric === 0 ? { label: 'Không chuyển', threshold: 'Không có transfer' } : null; }, rules: [lt(10, 'Chuyển nhỏ giọt', '<10%'), between(10, 50, 'Chuyển vừa', '10% → <50%'), between(50, 100, 'Chuyển lớn', '50% → <100%'), { when: (value) => value === 100, label: 'Chuyển sạch', threshold: '=100%' }] },
    { field: 'sell_100pct_rate', title: 'sell_100pct_rate', format: fmtPct, rules: [lt(25, 'Hiếm bán sạch', '<25%'), between(25, 50, 'Thỉnh thoảng bán sạch', '25% → <50%'), atLeast(50, 'Thường xuyên bán sạch', '≥50%')] },
    { field: 'transfer_100pct_rate', title: 'transfer_100pct_rate', format: fmtPct, special: (value) => { const numeric = n(value); return numeric == null || numeric === 0 ? { label: 'NA · Không chuyển', threshold: 'Không có transfer' } : null; }, rules: [lt(25, 'LOW · Hiếm chuyển sạch', '<25%'), between(25, 50, 'WATCH · Thỉnh thoảng chuyển sạch', '25% → <50%'), between(50, 100, 'HIGH · Thường xuyên chuyển sạch', '50% → <100%'), { when: (value) => value === 100, label: 'CRITICAL · Luôn chuyển sạch', threshold: '=100%' }] },
    { field: 'multi_buy_token_rate_pct', title: 'multi_buy_token_rate_pct', format: fmtPct, rules: [lt(40, 'One-shot / Sniper', '<40%'), between(40, 60, 'Mixed Entry', '40% → <60%'), between(60, 70, 'Scale-in', '60% → <70%'), atLeast(70, 'Strong Scale-in', '≥70%')] },
    { field: 'median_position_action_span_hours', title: 'median_position_action_span_hours', format: (value) => `${fmtNumber(value)}h`, rules: [lt(1, 'Scalper', '<1h'), between(1, 12, 'Intraday', '1h → <12h'), between(12, 72, 'Swing', '12h → <72h'), atLeast(72, 'Hold', '≥72h')] },
    { field: 'median_first_sell_pct', title: 'median_first_sell_pct', format: fmtPct, rules: [lt(50, 'Scale-out', '<50%'), between(50, 90, 'Bán phần lớn', '50% → <90%'), between(90, 100, 'Gần sạch', '90% → <100%'), { when: (value) => value === 100, label: 'Sạch một nhát', threshold: '=100%' }] },
    { field: 'averaging_down_token_rate_pct', title: 'averaging_down_token_rate_pct', format: fmtPct, rules: [lt(10, 'LOW · Hiếm DCA xuống', '<10%'), between(10, 20, 'WATCH · DCA nhẹ', '10% → <20%'), between(20, 30, 'HIGH · DCA cao', '20% → <30%'), atLeast(30, 'RED FLAG · DCA xuống rất thường xuyên', '≥30%')] },
    { field: 'max_realized_pnl_single_token_usd', title: 'max_realized_pnl_single_token_usd', format: fmtUsd, rules: [lt(50000, 'Small Winner', '<$50K'), between(50000, 100000, 'Big Winner', '$50K–<$100K'), between(100000, 250000, 'Unicorn', '$100K–<$250K'), between(250000, 500000, 'Unicorn+', '$250K–<$500K'), atLeast(500000, 'Mega Unicorn', '≥$500K')] },
  ];

  function classify(rule, value, view) {
    const special = rule.special?.(value, view);
    if (special) return special;
    if (!has(value)) return { label: 'Chưa đủ dữ liệu', threshold: 'Không có giá trị số' };
    return special || bucket(value, rule.rules.map((item) => ({ ...item, when: (candidate) => item.when(candidate, view) })));
  }

  function labelRows(view) {
    return LABEL_RULES.map((rule) => {
      const value = input(view, rule.field);
      const result = classify(rule, value, view);
      return {
        ...rule, value, result, formatted: value == null ? '—' : rule.format(value),
      };
    });
  }

  function rankClass(rank) {
    return String(rank || 'unknown').toLowerCase().replace(/[^a-z0-9_-]/g, '');
  }

  function rankInfo(view) {
    const labels = view.labels || {};
    const metrics = view.metrics || {};
    return {
      rank: labels.rank || '—',
      profile: labels.profile || metrics.primary_wallet_value || '—',
      performance: labels.performance || '—',
      status: labels.rank_status || view.state?.data_status || '—',
    };
  }

  async function loadDetail(address) {
    if (state.details.has(address)) return state.details.get(address);
    if (state.pending.has(address)) return state.pending.get(address);
    const promise = fetch(`/api/pnl/canonical/${encodeURIComponent(address)}?include_tokens=false`)
      .then((response) => response.ok ? response.json() : response.json().then((payload) => Promise.reject(new Error(payload.detail || response.statusText))))
      .then((payload) => {
        const view = canonicalView(payload);
        state.details.set(address, view);
        state.pending.delete(address);
        renderCohortTable();
        return view;
      })
      .catch((error) => {
        state.pending.delete(address);
        throw error;
      });
    state.pending.set(address, promise);
    return promise;
  }

  function renderPreview(view, row, loading = false) {
    const empty = $('cohort-hover-empty');
    const content = $('cohort-hover-content');
    const tooltip = $('cohort-tooltip');
    if (!view) {
      empty.classList.remove('hidden');
      content.classList.add('hidden');
      content.innerHTML = '';
      if (tooltip && !tooltip.classList.contains('hidden')) {
        tooltip.innerHTML = '<span class="eyebrow">LABEL PREVIEW</span><div class="cohort-hover-empty">Đang nạp canonical label…</div>';
      }
      return;
    }
    const info = rankInfo(view);
    const labels = view.labels || {};
    const metrics = view.metrics || {};
    const feature = labelRows(view).filter((item) => item.value != null).slice(0, 4);
    empty.classList.add('hidden');
    content.classList.remove('hidden');
    const html = `
      <div class="cohort-preview-identity"><span class="cohort-preview-rank rank-${text(rankClass(info.rank))}">${text(info.rank)}</span><div><strong>${text(row.wallet_emoji || '◌')} ${text(row.wallet_name || row.wallet)}</strong><small>${text(info.status)} · ${text(info.profile)}</small></div></div>
      <div class="cohort-preview-groups">${groupChips(view)}</div>
      <div class="cohort-preview-list">${feature.map((item) => `<div><span>${text(item.title)}</span><b>${text(item.result.label)}</b></div>`).join('')}</div>
      ${loading ? '<small class="cohort-loading-note">Đang nạp canonical label…</small>' : '<small class="cohort-loading-note">Click để ghim chi tiết xuống dưới.</small>'}
    `;
    content.innerHTML = html;
    if (tooltip && !tooltip.classList.contains('hidden')) {
      tooltip.innerHTML = `<span class="eyebrow">LABEL PREVIEW</span>${html}`;
    }
  }

  function positionTooltip(event, rowElement) {
    const tooltip = $('cohort-tooltip');
    if (!tooltip || tooltip.classList.contains('hidden')) return;
    const rect = rowElement?.getBoundingClientRect();
    const x = event?.clientX || (rect ? rect.right : 0);
    const y = event?.clientY || (rect ? rect.top : 0);
    const width = Math.min(360, window.innerWidth - 24);
    const left = Math.max(12, Math.min(x + 14, window.innerWidth - width - 12));
    const top = Math.max(12, Math.min(y + 12, window.innerHeight - tooltip.offsetHeight - 12));
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
  }

  async function inspect(row, rowElement, event, select = false) {
    if (!row?.wallet) return;
    state.hovered = row.wallet;
    if (select) {
      state.selected = row.wallet;
      $('cohort-detail-panel').classList.remove('hidden');
      $('cohort-detail-title').textContent = `${row.wallet_emoji || '◌'} ${row.wallet_name || 'Wallet'}`;
      $('cohort-detail-address').textContent = row.wallet;
      $('cohort-detail-status').textContent = row.data_status || '—';
      $('cohort-open-wallet').dataset.wallet = row.wallet;
    }
    const tooltip = $('cohort-tooltip');
    tooltip.classList.remove('hidden');
    tooltip.setAttribute('aria-hidden', 'false');
    const cached = state.details.get(row.wallet);
    renderPreview(cached, row, !cached);
    positionTooltip(event, rowElement);
    try {
      const view = cached || await loadDetail(row.wallet);
      if (state.hovered === row.wallet) {
        renderPreview(view, row, false);
        positionTooltip(event, rowElement);
        // Keep the full feature table synchronized with the row under the
        // pointer. A click still pins the row visually, but hover alone is
        // enough to inspect every G1–G4 and threshold label below the table.
        if (!state.selected) {
          $('cohort-detail-panel').classList.remove('hidden');
          $('cohort-open-wallet').dataset.wallet = row.wallet;
          renderDetailPanel(view, row);
        }
      }
      if (select || state.selected === row.wallet) renderDetailPanel(view, row);
    } catch (error) {
      if (state.hovered === row.wallet) {
        $('cohort-hover-empty').classList.remove('hidden');
        $('cohort-hover-empty').textContent = `Không tải được label: ${error.message || 'canonical unavailable'}`;
        $('cohort-hover-content').classList.add('hidden');
      }
    }
  }

  function hideTooltip() {
    state.hovered = null;
    const tooltip = $('cohort-tooltip');
    tooltip?.classList.add('hidden');
    tooltip?.setAttribute('aria-hidden', 'true');
  }

  function renderDetailPanel(view, row) {
    if (!view) return;
    const info = rankInfo(view);
    $('cohort-detail-title').textContent = `${row.wallet_emoji || '◌'} ${row.wallet_name || 'Wallet'} · Rank ${info.rank}`;
    $('cohort-detail-status').textContent = `${info.status} · ${view.state?.data_status || row.data_status || '—'}`;
    $('cohort-group-detail').innerHTML = Object.keys(groupMeta).map((key) => {
      const meta = groupMeta[key];
      const group = view.groups?.[key] || {};
      const label = groupLabel(group);
      const qualified = Boolean(group.qualified && label);
      const status = qualified ? 'ĐẠT ĐIỀU KIỆN' : 'CHƯA ĐẠT';
      const important = key === 'g1'
        ? `early lowcap ${fmtPct(group.early_lowcap_rate_pct)} · sample ${fmtNumber(group.token_sample)}`
        : key === 'g2'
          ? `median vol ${fmtUsd(group.median_volume_usd ?? group.median_volume)} · conviction ${fmtNumber(group.conviction_pass_count)}/5`
          : key === 'g3'
            ? `${group.primary_narrative_keyword || 'chưa xác định'} · ${fmtNumber(group.same_theme_ca_count)} CA cùng theme`
            : `winner ${fmtUsd(group.max_realized_pnl_single_token_usd)} · holder ${fmtNumber(group.holder_score)}/3`;
      return `<article class="cohort-group-card ${meta.accent} ${qualified ? 'qualified' : ''}"><div><strong>${text(meta.title)}</strong></div><span class="cohort-group-status">${status}</span><small>${text(meta.summary)}</small><b>${text(important)}</b></article>`;
    }).join('');
    $('cohort-feature-tbody').innerHTML = labelRows(view).map((item) => `
      <tr>
        <th scope="row"><code>${text(item.title)}</code></th>
        <td class="cohort-value">${text(item.formatted)}</td>
        <td><span class="cohort-final-label ${item.value == null ? 'missing' : ''}">${text(item.result.label)}</span></td>
        <td class="cohort-threshold">${text(item.result.threshold)}</td>
      </tr>
    `).join('');
  }

  function matches(row, view) {
    const query = ($('cohort-search').value || '').trim().toLowerCase();
    const rank = $('cohort-rank-filter').value;
    const group = $('cohort-group-filter').value;
    const haystack = `${row.wallet_name || ''} ${row.wallet || ''} ${row.wallet_emoji || ''}`.toLowerCase();
    if (query && !haystack.includes(query)) return false;
    if (rank && rankInfo(view || {}).rank !== rank) return false;
    if (group && !(activeGroups(view || {}).some((item) => item.code === group))) return false;
    return true;
  }

  function renderCohortTable() {
    const tbody = $('cohort-tbody');
    if (!tbody) return;
    const filtered = state.rows.filter((row) => matches(row, state.details.get(row.wallet)));
    state.filtered = filtered;
    if (!filtered.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="empty">Không có wallet phù hợp bộ lọc</td></tr>';
      $('cohort-foot').textContent = '0 wallet';
      return;
    }
    tbody.innerHTML = filtered.map((row) => {
      const view = state.details.get(row.wallet);
      const info = view ? rankInfo(view) : { rank: '…', profile: 'Đang nạp label', status: row.data_status || '—' };
      const rank = text(info.rank);
      return `<tr class="cohort-row ${state.selected === row.wallet ? 'selected' : ''}" tabindex="0" data-wallet="${text(row.wallet)}" aria-label="${text(`${row.wallet_name || 'Wallet'} ${info.rank}`)}" aria-describedby="cohort-tooltip">
        <td><div class="cohort-wallet-cell"><span class="cohort-emoji">${text(row.wallet_emoji || '◌')}</span><div><strong>${text(row.wallet_name || 'Wallet không tên')}</strong><code>${text(`${row.wallet.slice(0, 6)}…${row.wallet.slice(-4)}`)}</code></div></div></td>
        <td><span class="cohort-rank rank-${text(rankClass(info.rank))}">${rank}</span><small>${text(info.profile)}</small><em>${text(info.performance)}</em></td>
        <td><div class="cohort-chip-list">${view ? groupChips(view) : '<span class="cohort-chip muted">Đang nạp…</span>'}</div></td>
        <td><span class="cohort-data-status ${String(row.data_status || '').toLowerCase()}">${text(row.data_status || '—')}</span></td>
        <td><time datetime="${row.updated_at ? new Date(Number(row.updated_at) * 1000).toISOString() : ''}">${text(fmtDate(row.updated_at))}</time></td>
      </tr>`;
    }).join('');
    $('cohort-foot').textContent = `Hiển thị ${filtered.length} / ${state.rows.length} wallet · hover để xem label · click để ghim chi tiết`;
    tbody.querySelectorAll('.cohort-row').forEach((rowElement) => {
      const wallet = rowElement.dataset.wallet;
      const row = state.rows.find((item) => item.wallet === wallet);
      rowElement.addEventListener('mouseenter', (event) => inspect(row, rowElement, event));
      rowElement.addEventListener('mousemove', (event) => positionTooltip(event, rowElement));
      rowElement.addEventListener('mouseleave', hideTooltip);
      rowElement.addEventListener('focusin', (event) => inspect(row, rowElement, event));
      rowElement.addEventListener('focusout', hideTooltip);
      rowElement.addEventListener('click', (event) => inspect(row, rowElement, event, true));
    });
  }

  async function loadIndex(clearDetails = false) {
    const button = $('cohort-refresh-btn');
    if (button) button.disabled = true;
    try {
      // One compact SQL-backed request returns identity, all label features, group
      // decisions and projected labels for the entire 192-wallet cohort.
      const response = await fetch(`/api/pnl/cohort-labels?t=${Date.now()}`, { cache: 'no-store' });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || response.statusText);
      const incoming = payload.wallets || [];
      if (clearDetails) state.details.clear();
      state.rows = incoming;
      state.details = new Map(incoming.map((row) => [row.wallet, cohortIndexView(row)]));
      state.lastLoadedAt = Date.now();
      const ready = payload.ready_wallets == null ? incoming.filter((row) => row.data_status === 'READY').length : payload.ready_wallets;
      $('cohort-updated').textContent = `${ready}/${payload.total_wallets || incoming.length} READY · ${new Intl.DateTimeFormat('vi-VN', { hour: '2-digit', minute: '2-digit' }).format(new Date())}`;
      $('cohort-updated').classList.add('ok');
      renderCohortTable();
      if (state.selected) {
        const row = state.rows.find((item) => item.wallet === state.selected);
        if (row) renderDetailPanel(state.details.get(row.wallet), row);
      }
    } catch (error) {
      $('cohort-updated').textContent = `Không tải được cohort: ${error.message || 'server error'}`;
      $('cohort-updated').classList.remove('ok');
      $('cohort-tbody').innerHTML = `<tr><td colspan="5" class="empty">${text(error.message || 'Không tải được canonical wallet index')}</td></tr>`;
    } finally {
      if (button) button.disabled = false;
    }
  }

  function boot() {
    if (!$('cohort-monitor')) return;
    $('cohort-refresh-btn').addEventListener('click', () => loadIndex(true));
    $('cohort-search').addEventListener('input', renderCohortTable);
    $('cohort-rank-filter').addEventListener('change', renderCohortTable);
    $('cohort-group-filter').addEventListener('change', renderCohortTable);
    $('cohort-open-wallet').addEventListener('click', () => {
      const wallet = $('cohort-open-wallet').dataset.wallet;
      if (!wallet) return;
      const input = document.getElementById('address-input');
      if (input) input.value = wallet;
      if (typeof window.lookupWallet === 'function') window.lookupWallet(wallet, false);
      else window.location.search = `?wallet=${encodeURIComponent(wallet)}`;
    });
    loadIndex();
    window.setInterval(() => {
      if (document.visibilityState === 'visible') loadIndex(false);
    }, 60000);
  }

  document.addEventListener('DOMContentLoaded', boot);
}());
