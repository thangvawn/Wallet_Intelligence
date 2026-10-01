"""Canonical, typed inputs consumed by the wallet label rules.

The label input row is deliberately separate from both raw provider payloads
and the richer feature report.  It gives downstream jobs one stable record per
wallet while retaining a source marker and the original label text/slug maps.
Values which cannot be supported by the current evidence stay ``None``; they
are never replaced with zero or a fabricated default.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Optional


LEGACY_FIELDS = (
    'money_earned_primary_usd', 'capital_base_usd', 'current_portfolio_usd',
    'roi_on_trading_cost_pct', 'pnl_over_deposits_pct',
    'jackpot_dependency_pct', 'portfolio_max_drawdown_pct',
    'max_realized_pnl_drawdown_usd', 'median_volume_last_30_tokens_usd',
    'median_buy_order_last_30_tokens_usd', 'longest_loss_streak_tokens',
    'current_loss_streak_tokens', 'withdrawal_count', 'total_withdrawals_usd',
    'median_outflow_holding_pct', 'median_transfer_holding_pct',
    'sell_100pct_rate', 'transfer_100pct_rate',
    'multi_buy_token_rate_pct', 'median_position_action_span_hours',
    'median_first_sell_pct', 'averaging_down_token_rate_pct',
    'max_realized_pnl_single_token_usd',
)

GROUP_FIELDS = (
    'lowcap_entry_count', 'lowcap_entry_rate_pct', 'early_entry_count',
    'early_entry_rate_pct', 'early_lowcap_count', 'early_lowcap_rate_pct',
    'median_entry_market_cap_usd', 'median_entry_age_minutes', 'token_sample',
    'median_volume', 'median_buy_order', 'whale', 'multi_buy', 'holding',
    'first_sell', 'sell_100pct_conviction', 'averaging_down', 'conviction',
    'conviction_pass_count', 'same_theme_ca_count', 'same_theme_ca_share_pct',
    'largest_ca_share_in_theme', 'theme_buy_window_hours',
    'is_basket_narrative', 'holder_score',
)

ALL_FIELDS = LEGACY_FIELDS + GROUP_FIELDS


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float('inf') else None


def _first(*values: Any):
    for value in values:
        if value is not None:
            return value
    return None


def _bool_value(*values: Any) -> Optional[int]:
    value = _first(*values)
    return None if value is None else int(bool(value))


def _group(report: dict, key: str) -> dict:
    return ((report or {}).get('group_labels') or {}).get(key) or {}


def _count_transfer_events(events: Iterable[dict]) -> Optional[int]:
    count = 0
    seen = False
    for event in events or []:
        kind = str(event.get('event_type') or event.get('type') or '').lower()
        if kind in {'transferout', 'withdraw', 'withdrawal'}:
            seen = True
            count += 1
    return count if seen else None


def _report_values(report: dict, events: Iterable[dict] = ()) -> Dict[str, Any]:
    """Map the report's evidence-backed values to the legacy input names."""
    performance = report.get('performance_profile') or {}
    all_time = performance.get('all_time') or {}
    capital = performance.get('capital_outcome') or {}
    if not capital:
        capital = report.get('capital_outcome') or {}
    risk = report.get('risk_profile') or {}
    payoff = report.get('payoff_profile') or {}
    behavior = report.get('behavior_profile') or {}
    scale = behavior.get('scale') or {}
    transfer = behavior.get('transfer') or {}
    g2 = _group(report, 'g2')
    g4 = _group(report, 'g4')
    deposits = _number(capital.get('total_deposits_usd'))
    pnl = _number(_first(all_time.get('total_profit_usd'), capital.get('absolute_pnl_usd')))
    # The report does not claim a full equity curve.  A deposits denominator is
    # only emitted when it is explicitly available and positive.
    pnl_over_deposits = (100.0 * pnl / deposits) if pnl is not None and deposits and deposits > 0 else None
    out = {
        'money_earned_primary_usd': pnl,
        'capital_base_usd': _number(g2.get('capital_base_usd')),
        'current_portfolio_usd': _number(_first(
            capital.get('current_open_positions_value_usd'),
            capital.get('ending_equity_usd'),
        )),
        'roi_on_trading_cost_pct': _number(_first(
            capital.get('roi_on_trading_cost_pct'), all_time.get('roi_on_cost_pct'),
        )),
        'pnl_over_deposits_pct': pnl_over_deposits,
        'jackpot_dependency_pct': _number(payoff.get('top1_gross_profit_share_pct')),
        'portfolio_max_drawdown_pct': _number(risk.get('portfolio_max_drawdown_pct')),
        'max_realized_pnl_drawdown_usd': _number(risk.get('max_realized_pnl_drawdown_usd')),
        # G2 uses the same per-token medians; the report marks these as the
        # available volume/order evidence when a dedicated 30-token slice is
        # unavailable.
        'median_volume_last_30_tokens_usd': _number(_first(
            g2.get('median_volume_usd'), g2.get('median_volume'),
        )),
        'median_buy_order_last_30_tokens_usd': _number(_first(
            g2.get('median_buy_order_usd'), g2.get('median_buy_order'),
        )),
        'longest_loss_streak_tokens': _number(payoff.get('longest_loss_streak')),
        'current_loss_streak_tokens': _number(payoff.get('current_loss_streak')),
        'withdrawal_count': _count_transfer_events(events),
        'total_withdrawals_usd': _number(capital.get('total_withdrawals_usd')),
        'median_outflow_holding_pct': _number(payoff.get('median_outflow_holding_pct')),
        'median_transfer_holding_pct': None,
        'sell_100pct_rate': _number(_first(g4.get('sell_100pct_rate'), g2.get('conviction_metrics', {}).get('sell_100pct'))),
        'transfer_100pct_rate': None,
        'multi_buy_token_rate_pct': _number(_first(
            scale.get('multi_buy_rate_pct'),
            g2.get('conviction_metrics', {}).get('multi_buy'),
        )),
        'median_position_action_span_hours': _number(g4.get('median_position_action_span_hours')),
        'median_first_sell_pct': _number(g4.get('median_first_sell_pct')),
        'averaging_down_token_rate_pct': _number(_first(
            g2.get('conviction_metrics', {}).get('averaging_down'),
            scale.get('destructive_avg_down_rate_pct'),
        )),
        'max_realized_pnl_single_token_usd': _number(g4.get('max_realized_pnl_single_token_usd')),
    }
    return out


def build_label_input_record(
    wallet: str,
    report: Optional[dict] = None,
    projected_label: Optional[dict] = None,
    *,
    wallet_name: str = '',
    wallet_emoji: str = '',
    data_status: str = 'MISSING',
    feature_version: Optional[str] = None,
    formula_version: Optional[str] = None,
    input_hash: Optional[str] = None,
    events: Iterable[dict] = (),
    existing: Optional[dict] = None,
    legacy_inputs: Optional[dict] = None,
) -> dict:
    report = report or {}
    groups = dict(report.get('group_labels') or {})
    # Token-level rows have their own typed serving table.  Keeping them in
    # the wallet-level input JSON would duplicate hundreds of thousands of
    # facts and make every 192-wallet export unnecessarily large.
    groups.pop('token_observations', None)
    g1, g2, g3, g4 = (_group(report, k) for k in ('g1', 'g2', 'g3', 'g4'))
    values = {field: None for field in ALL_FIELDS}
    values.update(_report_values(report, events))
    value_sources = {field: 'report' for field, value in values.items() if value is not None}
    existing_sources = {}
    if existing:
        try:
            existing_sources = json.loads(existing.get('value_sources_json') or '{}')
        except (TypeError, json.JSONDecodeError):
            existing_sources = {}
    # A legacy feature export is the authoritative source for the old 22-rule
    # fields.  It is used during initial migration and then retained when a
    # fresh report cannot measure that particular field.
    for field, value in (legacy_inputs or {}).items():
        if field in values and value is not None:
            values[field] = _number(value)
            value_sources[field] = 'legacy_feature_export'
    if existing:
        for field in ALL_FIELDS:
            # The legacy export defines the contract for the old 22-rule
            # fields.  A later report may expose a similarly named but
            # differently scoped metric (for example all-history median volume
            # instead of the legacy last-30-token slice); never silently replace
            # that canonical value during a routine refresh.
            if (
                not legacy_inputs
                and existing_sources.get(field) == 'legacy_feature_export'
                and existing.get(field) is not None
            ):
                values[field] = existing[field]
                value_sources[field] = 'legacy_feature_export'
            elif values.get(field) is None and existing.get(field) is not None:
                values[field] = existing[field]
                value_sources[field] = existing.get('source', 'previous_canonical')

    group_values = {
        'lowcap_entry_count': _number(g1.get('lowcap_entry_count')),
        'lowcap_entry_rate_pct': _number(g1.get('lowcap_entry_rate_pct')),
        'early_entry_count': _number(g1.get('early_entry_count')),
        'early_entry_rate_pct': _number(g1.get('early_entry_rate_pct')),
        'early_lowcap_count': _number(g1.get('early_lowcap_count')),
        'early_lowcap_rate_pct': _number(g1.get('early_lowcap_rate_pct')),
        'median_entry_market_cap_usd': _number(g1.get('median_entry_market_cap_usd')),
        'median_entry_age_minutes': _number(g1.get('median_entry_age_minutes')),
        'token_sample': _number(g1.get('token_sample')),
        'median_volume': _number(_first(g2.get('median_volume'), g2.get('median_volume_usd'))),
        'median_buy_order': _number(_first(g2.get('median_buy_order'), g2.get('median_buy_order_usd'))),
        'whale': _bool_value(g2.get('whale'), g2.get('is_whale')),
        'multi_buy': _number((g2.get('conviction_metrics') or {}).get('multi_buy')),
        'holding': _number((g2.get('conviction_metrics') or {}).get('holding')),
        'first_sell': _number((g2.get('conviction_metrics') or {}).get('first_sell')),
        'sell_100pct_conviction': _number((g2.get('conviction_metrics') or {}).get('sell_100pct')),
        'averaging_down': _number((g2.get('conviction_metrics') or {}).get('averaging_down')),
        'conviction': _bool_value(g2.get('conviction'), g2.get('is_conviction')),
        'conviction_pass_count': _number(g2.get('conviction_pass_count')),
        'same_theme_ca_count': _number(g3.get('same_theme_ca_count')),
        'same_theme_ca_share_pct': _number(g3.get('same_theme_ca_share_pct')),
        'largest_ca_share_in_theme': _number(g3.get('largest_ca_share_in_theme')),
        'theme_buy_window_hours': _number(g3.get('theme_buy_window_hours')),
        'is_basket_narrative': _bool_value(g3.get('is_basket_narrative')),
        'holder_score': _number(g4.get('holder_score')),
    }
    for field, value in group_values.items():
        if value is not None:
            values[field] = value
        elif values.get(field) is None and existing and existing.get(field) is not None:
            values[field] = existing[field]
    for field in GROUP_FIELDS:
        if values.get(field) is not None:
            value_sources.setdefault(field, 'group_labels')

    label_json = projected_label or {}
    group_labels_json = json.dumps(groups, ensure_ascii=False, sort_keys=True, allow_nan=False)
    legacy_labels_json = json.dumps({
        key: value for key, value in (legacy_inputs or {}).items()
        if key.startswith('slug_') or key.startswith('label_')
    }, ensure_ascii=False, sort_keys=True, allow_nan=False)
    value_sources_json = json.dumps(value_sources, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if existing and not legacy_inputs and not report:
        # Preserve the last complete payload for manifest-only/partial syncs
        # where no new report was computed.  A report always replaces the
        # group-engine payload above.
        group_labels_json = existing.get('group_labels_json') or group_labels_json
        legacy_labels_json = existing.get('legacy_labels_json') or legacy_labels_json
        value_sources_json = existing.get('value_sources_json') or value_sources_json
    existing_source = (existing or {}).get('source') or ''
    source = (
        'report+legacy_feature_export'
        if legacy_inputs or 'legacy_feature_export' in existing_source
        else 'report'
        if report
        else existing_source or 'derived'
    )
    return {
        'wallet': wallet,
        'wallet_name': wallet_name or report.get('name') or '',
        'wallet_emoji': wallet_emoji or report.get('emoji') or '',
        'data_status': data_status,
        'feature_version': feature_version or (existing or {}).get('feature_version'),
        'formula_version': formula_version or (existing or {}).get('formula_version'),
        'input_hash': input_hash or (existing or {}).get('input_hash'),
        'source': source,
        **values,
        'group_labels_json': group_labels_json,
        'legacy_labels_json': legacy_labels_json,
        'value_sources_json': value_sources_json,
        'projected_label': label_json,
    }
