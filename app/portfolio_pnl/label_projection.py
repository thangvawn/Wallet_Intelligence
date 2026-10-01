"""Public output schema projected from the deterministic statistical report."""
import math
from typing import Any, Dict

from app.portfolio_pnl.rank_engine import evaluate_rank


def _json_safe(value: Any) -> Any:
    """Replace non-finite floats before writing a JSON-facing label payload.

    Provider data can contain an infinite profit factor when a wallet has no
    recorded losses.  The ranking engine can use that value internally, but
    JSON cannot represent ``NaN`` or infinities.  Keep the public payload
    standards-compliant and represent an unavailable metric as ``None``.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_json_safe(item) for item in value)
    return value


def _conviction_label(report: dict) -> str:
    conviction = report.get('conviction_behavior') or {}
    scale = ((report.get('behavior_profile') or {}).get('scale') or {}).get('style')
    verdict = conviction.get('conviction_verdict')
    if verdict == 'POSITIVE_SIZE_EDGE' and scale == 'SCALES_IN':
        return 'HIGH'
    if verdict == 'OVERCONFIDENCE_RISK':
        return 'RISKY'
    if verdict == 'INSUFFICIENT_DATA':
        return 'UNKNOWN'
    return 'MEDIUM' if scale == 'SCALES_IN' or verdict == 'POSITIVE_SIZE_EDGE' else 'LOW'


def _metrics(report: dict) -> dict:
    meta = report.get('meta') or {}
    info = report.get('information_value') or {}
    performance = report.get('performance_profile') or {}
    all_time = performance.get('all_time') or {}
    capital = performance.get('capital_outcome') or {}
    daily = performance.get('daily_calendar') or {}
    activity = report.get('activity_profile') or {}
    risk = report.get('risk_profile') or {}
    cluster = report.get('position_cluster_profile') or {}
    payoff = report.get('payoff_profile') or {}
    groups = report.get('group_labels') or {}
    group_labels = list(groups.get('labels') or [])
    return _json_safe({
        'performance_score': info.get('composite_alpha_score'),
        'confidence': meta.get('confidence') or 'NONE',
        'confidence_score': meta.get('confidence_score'),
        'starting_equity_usd': capital.get('starting_equity_usd'),
        'ending_equity_usd': capital.get('ending_equity_usd'),
        'peak_equity_usd': capital.get('peak_equity_usd'),
        'average_equity_usd': capital.get('average_equity_usd'),
        'portfolio_growth_usd': capital.get('portfolio_growth_usd'),
        'portfolio_return_pct': capital.get('portfolio_return_pct'),
        'external_capital_flow_usd': capital.get('external_capital_flow_usd'),
        'total_deposits_usd': capital.get('total_deposits_usd'),
        'total_withdrawals_usd': capital.get('total_withdrawals_usd'),
        'absolute_pnl_usd': capital.get('absolute_pnl_usd', all_time.get('total_profit_usd')),
        'money_earned_primary_usd': capital.get('absolute_pnl_usd', all_time.get('total_profit_usd')),
        'roi_on_trading_cost_pct': capital.get('roi_on_trading_cost_pct', all_time.get('roi_on_cost_pct')),
        'capital_tier_proxy': capital.get('capital_tier_proxy'),
        'capital_status': capital.get('status'),
        'capital_verification_status': capital.get('verification_status') or 'UNVERIFIED',
        'capital_limitations': capital.get('limitations') or [],
        'funding_verification_status': capital.get('funding_verification_status') or 'UNVERIFIED',
        'historical_profit_factor': daily.get('profit_factor'),
        'historical_profit_factor_status': daily.get('profit_factor_status'),
        'profit_factor_30d': activity.get('profit_factor_30d'),
        'profit_factor_30d_status': activity.get('profit_factor_30d_status'),
        'pnl_30d_usd': activity.get('pnl_30d_usd'),
        'token_win_rate_pct': all_time.get(
            'historical_token_win_rate_pct', all_time.get('token_win_rate_pct')
        ),
        'token_win_rate_basis': all_time.get(
            'historical_token_win_rate_basis', all_time.get('win_rate_basis')
        ),
        'recent_30d_token_win_rate_pct': all_time.get('recent_30d_token_win_rate_pct'),
        'profitable_day_rate_pct': daily.get('profitable_day_rate_pct'),
        'active_days': daily.get('active_days'),
        'active_days_30d': activity.get('active_days_30d'),
        'days_since_last_active': activity.get('days_since_last_active'),
        'token_sample': meta.get('token_sample_size'),
        'max_drawdown_usd': risk.get('max_drawdown_usd'),
        'max_drawdown_to_peak_pct': risk.get('max_drawdown_to_peak_pct'),
        'max_realized_pnl_drawdown_usd': risk.get('max_realized_pnl_drawdown_usd', risk.get('max_drawdown_usd')),
        'jackpot_dependency_pct': payoff.get('top1_gross_profit_share_pct'),
        'longest_loss_streak_tokens': payoff.get('longest_loss_streak'),
        'current_loss_streak_tokens': payoff.get('current_loss_streak'),
        'median_outflow_holding_pct': payoff.get('median_outflow_holding_pct'),
        'median_first_sell_pct': (payoff.get('median_first_sell_pct')
                                  if payoff.get('median_first_sell_pct') is not None
                                  else (groups.get('g4') or {}).get('median_first_sell_pct')),
        'averaging_down_token_rate_pct': (payoff.get('averaging_down_token_rate_pct')
                                          if payoff.get('averaging_down_token_rate_pct') is not None
                                          else ((groups.get('g2') or {}).get('conviction_metrics') or {}).get('averaging_down')),
        'realized_pnl_drawdown_to_peak_pnl_pct': risk.get('realized_pnl_drawdown_to_peak_pnl_pct', risk.get('max_drawdown_to_peak_pct')),
        'portfolio_max_drawdown_pct': risk.get('portfolio_max_drawdown_pct'),
        'portfolio_drawdown_status': risk.get('portfolio_drawdown_status', 'NOT_RECONSTRUCTED'),
        'drawdown_used_for_ranking': False,
        'drawdown_basis': risk.get('drawdown_basis'),
        'cluster_win_rate_pct': cluster.get('cluster_win_rate_pct'),
        'cluster_metric_status': cluster.get('status') or 'NOT_AVAILABLE',
        'conviction': _conviction_label(report),
        'payoff_style': payoff.get('style'),
        'one_hit_dependent': payoff.get('style') == 'ONE_HIT_DEPENDENT',
        'abnormal_current_loss_streak': bool(payoff.get('current_loss_streak_abnormal')),
        'group_labels': group_labels,
        'group_count': len(group_labels),
        'qualified_group_labels': group_labels,
        'qualified_group_count': len(group_labels),
        'group_features': {
            key: groups.get(key) or {}
            for key in ('g1', 'g2', 'g3', 'g4')
        },
    })


def project_wallet_label(report: dict, name: str = '', emoji: str = '') -> Dict[str, Any]:
    """Expose rank, priority placeholder, auditable metrics and ranking audit trail."""
    wallet_class = report.get('wallet_class') or {}
    setup = report.get('alpha_setup') or {}
    activity = report.get('activity_profile') or {}
    payoff = report.get('payoff_profile') or {}
    behavior = [
        item for item in (wallet_class.get('trading_behavior') or [])
        if item and item != 'HABITS_NOT_LABELLED'
    ]
    groups = report.get('group_labels') or {}
    group_labels = list(groups.get('labels') or [])
    for label in group_labels:
        if label not in behavior:
            behavior.append(label)
    for label in (activity.get('status'), payoff.get('style')):
        if label and label not in behavior:
            behavior.append(label)
    if payoff.get('current_loss_streak_abnormal'):
        behavior.append('ABNORMAL_CURRENT_LOSS_STREAK')

    decision = evaluate_rank(report)
    metrics = _metrics(report)
    metrics.update({
        'overall_rank_score': decision.get('overall_score'),
        'rank_confidence': decision.get('rank_confidence'),
        'available_rank_weight': decision.get('available_rank_weight'),
        'missing_rank_metrics': decision.get('missing_rank_metrics') or [],
        'rank_score_components': decision.get('rank_score_components') or {},
    })
    # Keep the nine rank inputs visible at the same level as the score.  The
    # rank engine is the canonical extractor because some reports store
    # capital/payoff data in alternate sections.
    for field, component in (decision.get('rank_score_components') or {}).items():
        metrics[field] = component.get('raw')
    return _json_safe({
        'schema': 'wallet_rank_output_v6',
        'address': report.get('address') or '',
        'name': name or report.get('name') or '',
        'emoji': emoji or report.get('emoji') or '',
        'labels': {
            'rank': decision['rank'],
            'rank_rule': decision['rule'],
            'rank_status': decision['rank_status'],
            's_strength': decision.get('s_strength'),
            'performance': (report.get('information_value') or {}).get('tier') or 'INSUFFICIENT_SAMPLE',
            'profile': wallet_class.get('primary_wallet_value') or 'RESEARCH_CANDIDATE',
            'setup': setup.get('optimal_setup_archetype') if setup.get('status') == 'AVAILABLE' else None,
            'behavior': behavior,
        },
        'priority': {
            'level': 'NORMAL',
            'action': 'NORMAL',
            'clan_id': None,
            'clan_size': None,
            'shared_token': None,
            'strong_wallets': 0,
            'detection_basis': 'NO_SHARED_OPPORTUNITY_DETECTED',
            'verification_status': 'NOT_APPLICABLE',
            'eligible_for_priority_alert': False,
        },
        'metrics': metrics,
        'data_availability': report.get('data_availability') or {
            'status': 'READY',
            'capital_status': ((report.get('performance_profile') or {}).get('capital_outcome') or {}).get('status'),
            'capital_verified': ((report.get('performance_profile') or {}).get('capital_outcome') or {}).get('status') == 'FULL_VERIFIED_EQUITY',
            'funding_verified': ((report.get('performance_profile') or {}).get('capital_outcome') or {}).get('funding_verification_status') == 'VERIFIED',
            'capital_corroborated': ((report.get('performance_profile') or {}).get('capital_outcome') or {}).get('status') in (
                'FULL_VERIFIED_EQUITY', 'TRANSACTION_CORROBORATED_EQUITY_HISTORY',
            ),
            'mark_to_market_curve_verified': False,
        },
        'rank_audit': {
            's_gates': decision.get('s_gates') or {},
            'unknown_gates': decision.get('unknown_gates') or [],
            'risk_vetoes': decision.get('risk_vetoes') or [],
            'overall_score': decision.get('overall_score'),
            'rank_confidence': decision.get('rank_confidence'),
            'available_rank_weight': decision.get('available_rank_weight'),
            'missing_rank_metrics': decision.get('missing_rank_metrics') or [],
            'rank_score_components': decision.get('rank_score_components') or {},
        },
    })
