"""Deterministic weighted wallet ranking.

The rank is based on the nine fields selected for the overall ranking model.
Each field is converted to a 0..100 quality score, multiplied by its business
weight, and averaged over the weights that are actually available.  Missing
fields lower confidence, but never silently become zero.  A small set of
explicit risk vetoes still protects the D rank from a high aggregate score.
"""

import math
from typing import Any, Dict, Optional


RANK_WEIGHTS = {
    'money_earned_primary_usd': 18.0,
    'roi_on_trading_cost_pct': 17.0,
    'jackpot_dependency_pct': 12.0,
    'max_realized_pnl_drawdown_usd': 13.0,
    'longest_loss_streak_tokens': 8.0,
    'current_loss_streak_tokens': 12.0,
    'median_outflow_holding_pct': 5.0,
    'median_first_sell_pct': 7.0,
    'averaging_down_token_rate_pct': 8.0,
}

# The score curves are deliberately fixed.  This keeps a rank comparable
# between runs; thresholds can be changed in one place and audited in output.
RANK_THRESHOLDS = {
    'S': 85.0,
    'A': 70.0,
    'B': 55.0,
    'minimum_confidence': 0.60,
    's_min_confidence': 0.80,
}

RANK_RULES = {
    'D': {
        'heavy_loss_max_pnl_usd': -100_000.0,
        'current_loss_streak_tokens': 8.0,
        'severe_drawdown_pct': 90.0,
        'destructive_avg_down_pct': 50.0,
        'destructive_avg_down_streak': 6.0,
    },
    'score': RANK_THRESHOLDS,
    'weights': RANK_WEIGHTS,
}


def _float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _first(*values: Any) -> Any:
    for value in values:
        if _float(value) is not None:
            return value
    return None


def _number(value: Any) -> Optional[float]:
    return _float(value)


def _piecewise(value: Optional[float], points: tuple[tuple[float, float], ...]) -> Optional[float]:
    """Linearly interpolate a bounded score through monotonically ordered points."""
    value = _float(value)
    if value is None:
        return None
    if value <= points[0][0]:
        return points[0][1]
    if value >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if value <= x1:
            fraction = (value - x0) / (x1 - x0)
            return max(0.0, min(100.0, y0 + fraction * (y1 - y0)))
    return points[-1][1]


def _score_metric(name: str, value: Optional[float]) -> Optional[float]:
    # Higher is better for money and ROI.
    if name == 'money_earned_primary_usd':
        return _piecewise(value, ((-100_000, 0), (0, 50), (250_000, 80), (1_000_000, 100)))
    if name == 'roi_on_trading_cost_pct':
        return _piecewise(value, ((-100, 0), (0, 50), (25, 80), (50, 90), (100, 100)))
    # Lower is better for concentration, drawdown and loss behaviour.
    if name == 'jackpot_dependency_pct':
        return _piecewise(value, ((0, 100), (25, 100), (50, 70), (80, 35), (100, 0)))
    if name == 'max_realized_pnl_drawdown_usd':
        # Dollar drawdown is normalized against earned capital when available;
        # the caller supplies the ratio in a private companion field below.
        return _piecewise(value, ((0, 100), (10_000, 85), (50_000, 55), (250_000, 15), (1_000_000, 0)))
    if name == 'longest_loss_streak_tokens':
        return _piecewise(value, ((0, 100), (5, 100), (10, 80), (20, 50), (30, 20), (40, 0)))
    if name == 'current_loss_streak_tokens':
        return _piecewise(value, ((0, 100), (2, 100), (5, 70), (7, 35), (8, 0)))
    if name == 'median_outflow_holding_pct':
        return _piecewise(value, ((0, 100), (10, 100), (40, 75), (70, 40), (90, 0)))
    if name == 'median_first_sell_pct':
        # Taking some profit while keeping a runner is preferred.  A complete
        # first exit receives zero because it removes future upside exposure.
        return _piecewise(value, ((0, 80), (10, 100), (50, 100), (70, 80), (90, 30), (100, 0)))
    if name == 'averaging_down_token_rate_pct':
        return _piecewise(value, ((0, 100), (10, 100), (20, 85), (30, 60), (50, 25), (100, 0)))
    return None


def _inputs(report: dict) -> Dict[str, Any]:
    info = report.get('information_value') or {}
    meta = report.get('meta') or {}
    activity = report.get('activity_profile') or {}
    payoff = report.get('payoff_profile') or {}
    performance = (report.get('performance_profile') or {}).get('all_time') or {}
    capital = report.get('capital_outcome') or (report.get('performance_profile') or {}).get('capital_outcome') or {}
    risk = report.get('risk_profile') or {}
    groups = report.get('group_labels') or {}
    g2 = groups.get('g2') or {}
    g4 = groups.get('g4') or {}
    g2_metrics = g2.get('conviction_metrics') or {}
    behavior = report.get('behavior_profile') or {}
    scale = behavior.get('scale') or {}

    pnl = _first(capital.get('absolute_pnl_usd'), performance.get('total_profit_usd'))
    roi = _first(capital.get('roi_on_trading_cost_pct'), performance.get('roi_on_cost_pct'))
    first_sell = _first(payoff.get('median_first_sell_pct'), g4.get('median_first_sell_pct'))
    avg_down = _first(
        payoff.get('averaging_down_token_rate_pct'),
        g2_metrics.get('averaging_down'),
        scale.get('destructive_avg_down_rate_pct'),
    )
    drawdown_usd = _first(risk.get('max_realized_pnl_drawdown_usd'), risk.get('max_drawdown_usd'))
    drawdown_pct = _first(risk.get('realized_pnl_drawdown_to_peak_pnl_pct'))
    if drawdown_pct is None and drawdown_usd is not None and pnl is not None and pnl > 0:
        drawdown_pct = abs(drawdown_usd) / pnl * 100.0

    return {
        'days_since': _number(activity.get('days_since_last_active')),
        'pnl': _number(pnl),
        'roi': _number(roi),
        'jackpot': _number(payoff.get('top1_gross_profit_share_pct')),
        'drawdown_usd': _number(drawdown_usd),
        'drawdown_pct': _number(drawdown_pct),
        'longest_loss': _number(payoff.get('longest_loss_streak')),
        'current_loss': _number(payoff.get('current_loss_streak')),
        'outflow_holding': _number(payoff.get('median_outflow_holding_pct')),
        'first_sell': _number(first_sell),
        'averaging_down': _number(avg_down),
        'score_source': info.get('composite_alpha_score'),
        'ending_equity': _number(capital.get('ending_equity_usd')),
    }


def _metric_values(values: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Return the nine public metric names used by the rank model."""
    return {
        'money_earned_primary_usd': values['pnl'],
        'roi_on_trading_cost_pct': values['roi'],
        'jackpot_dependency_pct': values['jackpot'],
        'max_realized_pnl_drawdown_usd': values['drawdown_usd'],
        'longest_loss_streak_tokens': values['longest_loss'],
        'current_loss_streak_tokens': values['current_loss'],
        'median_outflow_holding_pct': values['outflow_holding'],
        'median_first_sell_pct': values['first_sell'],
        'averaging_down_token_rate_pct': values['averaging_down'],
    }


def _weighted_score(values: Dict[str, Any], weights: Dict[str, float]) -> dict:
    metrics = _metric_values(values)
    components = {}
    available_weight = 0.0
    weighted_total = 0.0
    for name, weight in weights.items():
        raw = metrics.get(name)
        normalized = _score_metric(name, raw)
        available = normalized is not None
        contribution = normalized * weight / 100.0 if available else None
        if available:
            available_weight += weight
            weighted_total += contribution
        components[name] = {
            'raw': raw,
            'normalized_score': round(normalized, 3) if normalized is not None else None,
            'weight_pct': weight,
            'contribution': round(contribution, 3) if contribution is not None else None,
            'available': available,
        }
    overall = weighted_total * 100.0 / available_weight if available_weight else None
    return {
        'overall_score': round(overall, 2) if overall is not None else None,
        'available_rank_weight': round(available_weight, 2),
        'rank_confidence': round(available_weight / 100.0, 3),
        'missing_rank_metrics': [name for name, item in components.items() if not item['available']],
        'rank_score_components': components,
    }


def _gate(name: str, passed: Optional[bool], evidence: dict) -> dict:
    status = 'UNKNOWN' if passed is None else 'PASS' if passed else 'FAIL'
    return {
        'gate': name,
        'status': status,
        'passed': passed is True,
        'checks': {'condition': passed is True},
        'check_states': {'condition': status},
        'unknown_checks': ['condition'] if passed is None else [],
        'evidence': evidence,
    }


def evaluate_rank(report: dict, rules: Optional[dict] = None) -> Dict[str, Any]:
    """Evaluate a wallet with weighted rank scoring and auditable vetoes."""
    rules = rules or RANK_RULES
    values = _inputs(report)
    weights = rules.get('weights') or RANK_WEIGHTS
    score = _weighted_score(values, weights)
    d = rules.get('D') or RANK_RULES['D']
    gates = {}
    vetoes = []

    def add_veto(code: str, condition: bool, evidence: dict):
        gates[code] = _gate(code, condition, evidence)
        if condition:
            vetoes.append(code)

    add_veto('D2_HEAVY_LOSS', values['pnl'] is not None and values['pnl'] < d['heavy_loss_max_pnl_usd'],
             {'money_earned_primary_usd': values['pnl'], 'threshold_usd': d['heavy_loss_max_pnl_usd']})
    add_veto('D3_CURRENT_LOSS_STREAK', values['current_loss'] is not None and values['current_loss'] >= d['current_loss_streak_tokens'],
             {'current_loss_streak_tokens': values['current_loss'], 'threshold_tokens': d['current_loss_streak_tokens']})
    add_veto('D4_SEVERE_DRAWDOWN', (
        values['drawdown_pct'] is not None and values['drawdown_pct'] >= d['severe_drawdown_pct']
        and (values['pnl'] is None or values['pnl'] <= 0)
    ), {'realized_pnl_drawdown_to_peak_pnl_pct': values['drawdown_pct'], 'money_earned_primary_usd': values['pnl']})
    add_veto('D5_DESTRUCTIVE_AVERAGING_DOWN', (
        values['averaging_down'] is not None and values['averaging_down'] >= d['destructive_avg_down_pct']
        and values['current_loss'] is not None and values['current_loss'] >= d['destructive_avg_down_streak']
    ), {'averaging_down_token_rate_pct': values['averaging_down'], 'current_loss_streak_tokens': values['current_loss']})

    unknown_gates = [name for name, gate in gates.items() if gate['status'] == 'UNKNOWN']
    common = {
        **score,
        'risk_vetoes': vetoes,
        's_gates': gates,
        'unknown_gates': unknown_gates,
        'is_dormant': values['ending_equity'] is not None and values['ending_equity'] < 1000,
    }

    def result(rank: str, rule: str, confirmed: bool = True, **extra) -> Dict[str, Any]:
        return {
            'rank': rank,
            'rule': rule,
            'rank_status': 'CONFIRMED' if confirmed else 'PROVISIONAL',
            **common,
            **extra,
        }

    if values['days_since'] is None:
        return result('C', 'C_INSUFFICIENT_ACTIVITY_DATA', confirmed=False)
    if vetoes:
        return result('D', vetoes[0], confirmed=True)
    if score['overall_score'] is None or score['rank_confidence'] < rules.get('score', RANK_THRESHOLDS)['minimum_confidence']:
        return result('C', 'C_INSUFFICIENT_RANK_DATA', confirmed=False)

    overall = score['overall_score']
    rank_thresholds = rules.get('score', RANK_THRESHOLDS)
    if overall >= rank_thresholds['S'] and score['rank_confidence'] >= rank_thresholds['s_min_confidence']:
        return result('S', 'S_WEIGHTED_SCORE', s_strength='ELITE' if overall >= 92 else 'STRONG')
    if overall >= rank_thresholds['A']:
        return result('A', 'A_WEIGHTED_SCORE')
    if overall >= rank_thresholds['B']:
        return result('B', 'B_WEIGHTED_SCORE')
    return result('C', 'C_WEIGHTED_SCORE')


def rank_report(report: dict, rules: Optional[dict] = None) -> str:
    return evaluate_rank(report, rules)['rank']
