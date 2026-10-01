"""Statistical analysis from GMGN portfolio PnL (holdings) dashboard data."""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional, Tuple

from app.portfolio_pnl.api import load_master_index, load_wallet_pnl
from app.portfolio_pnl.store import PnlStore

SCHEMA_WALLET = 'pnl_wallet_stats_v1'
SCHEMA_COHORT = 'pnl_cohort_stats_v1'


def _num(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _median(values: List[float]) -> Optional[float]:
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return float(statistics.median(clean))


def _percentile(sorted_values: List[float], pct: float) -> Optional[float]:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def _token_roi_pct(row: dict) -> Optional[float]:
    bought = _num(row.get('bought_usd'))
    profit = row.get('total_profit_usd')
    if profit is None or bought <= 0:
        return None
    return (_num(profit) / bought) * 100.0


def _pick_extreme(rows: List[dict], key: str = 'total_profit_usd', best: bool = True) -> Optional[Dict[str, Any]]:
    candidates = [r for r in rows if r.get(key) is not None]
    if not candidates:
        return None
    row = max(candidates, key=lambda r: _num(r.get(key))) if best else min(candidates, key=lambda r: _num(r.get(key)))
    return {
        'token_symbol': row.get('token_symbol') or '?',
        'token_address': row.get('token_address') or '',
        'total_profit_usd': _num(row.get('total_profit_usd')),
        'realized_profit_usd': row.get('realized_profit_usd'),
        'bought_usd': row.get('bought_usd'),
    }


def compute_wallet_stats(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Aggregate per-token holdings into wallet-level statistics."""
    rows = payload.get('rows') or []
    summary = payload.get('summary') or {}
    address = payload.get('wallet_address') or summary.get('wallet_address') or ''

    open_positions = sum(1 for r in rows if _num(r.get('balance_usd')) > 0.01)
    closed_positions = sum(1 for r in rows if r.get('is_closed'))
    active_tokens = [r for r in rows if _num(r.get('bought_usd')) > 0 or _num(r.get('sold_usd')) > 0]

    wins = sum(1 for r in active_tokens if _num(r.get('total_profit_usd')) > 0)
    losses = sum(1 for r in active_tokens if _num(r.get('total_profit_usd')) < 0)
    breakeven = len(active_tokens) - wins - losses

    closed_with_realized = [r for r in rows if r.get('is_closed') and r.get('realized_profit_usd') is not None]
    realized_wins = sum(1 for r in closed_with_realized if _num(r.get('realized_profit_usd')) > 0)

    gross_profit = sum(_num(r.get('total_profit_usd')) for r in active_tokens if _num(r.get('total_profit_usd')) > 0)
    gross_loss = abs(sum(_num(r.get('total_profit_usd')) for r in active_tokens if _num(r.get('total_profit_usd')) < 0))
    profit_factor = round(gross_profit / gross_loss, 3) if gross_loss > 0 else (None if gross_profit == 0 else 999.0)

    total_bought = sum(_num(r.get('bought_usd')) for r in rows)
    total_sold = sum(_num(r.get('sold_usd')) for r in rows)
    total_cost = _num(summary.get('total_cost_usd'))
    total_profit = summary.get('total_profit_usd')
    if total_profit is None:
        total_profit = sum(_num(r.get('total_profit_usd')) for r in rows)

    roi_on_cost_pct = None
    if total_cost > 0 and total_profit is not None:
        roi_on_cost_pct = round((_num(total_profit) / total_cost) * 100.0, 2)

    rois = [_token_roi_pct(r) for r in active_tokens]
    rois_clean = [r for r in rois if r is not None]

    holding_secs = [
        _num(r.get('holding_duration_seconds'))
        for r in closed_with_realized
        if r.get('holding_duration_seconds')
    ]
    median_holding_hours = None
    if holding_secs:
        median_holding_hours = round(statistics.median(holding_secs) / 3600.0, 2)

    profits_sorted = sorted(
        [_num(r.get('total_profit_usd')) for r in active_tokens if r.get('total_profit_usd') is not None],
        reverse=True,
    )
    top5_share = None
    if profits_sorted and _num(total_profit) != 0:
        top5 = sum(profits_sorted[:5])
        top5_share = round((top5 / _num(total_profit)) * 100.0, 2)

    buy_tx = sum(int(r.get('buy_tx_count') or 0) for r in rows)
    sell_tx = sum(int(r.get('sell_tx_count') or 0) for r in rows)
    summary_buy = int(summary.get('buy_count') or 0)
    summary_sell = int(summary.get('sell_count') or 0)

    return {
        'schema': SCHEMA_WALLET,
        'wallet_address': address,
        'wallet_name': payload.get('wallet_name') or summary.get('wallet_name') or '',
        'token_count': len(rows),
        'active_token_count': len(active_tokens),
        'open_positions': open_positions,
        'closed_positions': closed_positions,
        'token_wins': wins,
        'token_losses': losses,
        'token_breakeven': breakeven,
        'token_win_rate_pct': round(100.0 * wins / len(active_tokens), 2) if active_tokens else None,
        'realized_win_rate_pct': round(100.0 * realized_wins / len(closed_with_realized), 2) if closed_with_realized else None,
        'gross_profit_usd': round(gross_profit, 2),
        'gross_loss_usd': round(gross_loss, 2),
        'profit_factor': profit_factor,
        'total_bought_usd': round(total_bought, 2),
        'total_sold_usd': round(total_sold, 2),
        'total_cost_usd': round(total_cost, 2) if total_cost else None,
        'realized_profit_usd': summary.get('realized_profit_usd'),
        'unrealized_profit_usd': summary.get('unrealized_profit_usd'),
        'total_profit_usd': total_profit,
        'roi_on_cost_pct': roi_on_cost_pct,
        'median_token_roi_pct': round(_median(rois_clean), 2) if rois_clean else None,
        'median_holding_hours': median_holding_hours,
        'top5_profit_share_pct': top5_share,
        'buy_tx_count': summary_buy or buy_tx,
        'sell_tx_count': summary_sell or sell_tx,
        'buy_sell_ratio': round(buy_tx / sell_tx, 3) if sell_tx else None,
        'best_token': _pick_extreme(active_tokens, best=True),
        'worst_token': _pick_extreme(active_tokens, best=False),
        'performance_tier': _performance_tier(_num(total_profit), profit_factor, wins, len(active_tokens)),
        'hodl_count': sum(1 for r in rows if r.get('is_hodl')),
        'sell_all_count': sum(1 for r in rows if r.get('is_closed')),
        'didnt_buy_count': sum(1 for r in rows if r.get('didnt_buy')),
        'honeypot_count': sum(1 for r in rows if r.get('is_honeypot')),
        'winners': [_token_brief(r) for r in sorted(
            [r for r in active_tokens if _num(r.get('total_profit_usd')) > 0],
            key=lambda x: _num(x.get('total_profit_usd')),
            reverse=True,
        )[:10]],
        'losers': [_token_brief(r) for r in sorted(
            [r for r in active_tokens if _num(r.get('total_profit_usd')) < 0],
            key=lambda x: _num(x.get('total_profit_usd')),
        )[:10]],
    }


def _token_brief(row: dict) -> Dict[str, Any]:
    return {
        'token_symbol': row.get('token_symbol') or '?',
        'token_address': row.get('token_address') or '',
        'total_profit_usd': _num(row.get('total_profit_usd')),
        'realized_profit_usd': row.get('realized_profit_usd'),
        'bought_usd': row.get('bought_usd'),
        'is_closed': row.get('is_closed'),
        'is_hodl': row.get('is_hodl'),
    }


def _performance_tier(total_profit: float, profit_factor: Optional[float], wins: int, n: int) -> str:
    if n < 3:
        return 'INSUFFICIENT_DATA'
    if total_profit > 10_000 and profit_factor and profit_factor >= 1.5:
        return 'ELITE'
    if total_profit > 0 and profit_factor and profit_factor >= 1.2:
        return 'PROFITABLE'
    if total_profit >= -1_000 and (profit_factor or 0) >= 0.9:
        return 'NEUTRAL'
    return 'UNDERPERFORMER'


def _load_all_payloads() -> List[Dict[str, Any]]:
    # One connection/query instead of reopening and checking the schema per wallet.
    return PnlStore().list_dashboard_snapshots()


def compute_cohort_stats(include_wallets: bool = True) -> Dict[str, Any]:
    """Cohort-level statistics across all crawled wallets."""
    index = load_master_index()
    payloads = _load_all_payloads()
    wallet_stats = [compute_wallet_stats(p) for p in payloads]

    profits = sorted(_num(s.get('total_profit_usd')) for s in wallet_stats)
    win_rates = sorted(s['token_win_rate_pct'] for s in wallet_stats if s.get('token_win_rate_pct') is not None)
    factors = sorted(s['profit_factor'] for s in wallet_stats if s.get('profit_factor') is not None and s['profit_factor'] < 100)

    profitable = sum(1 for p in profits if p > 0)
    breakeven = sum(1 for p in profits if -500 <= p <= 0)
    losing = sum(1 for p in profits if p < -500)

    tier_counts: Dict[str, int] = {}
    for s in wallet_stats:
        tier = s.get('performance_tier') or 'UNKNOWN'
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

    ranked = sorted(wallet_stats, key=lambda s: _num(s.get('total_profit_usd')), reverse=True)
    for i, s in enumerate(ranked):
        s['cohort_rank'] = i + 1
        s['cohort_percentile'] = round(100.0 * (1 - i / max(len(ranked) - 1, 1)), 1) if ranked else None

    top10 = [
        {
            'rank': s['cohort_rank'],
            'wallet_address': s['wallet_address'],
            'wallet_name': s.get('wallet_name'),
            'total_profit_usd': s.get('total_profit_usd'),
            'token_win_rate_pct': s.get('token_win_rate_pct'),
            'profit_factor': s.get('profit_factor'),
        }
        for s in ranked[:10]
    ]
    bottom10 = [
        {
            'rank': s['cohort_rank'],
            'wallet_address': s['wallet_address'],
            'wallet_name': s.get('wallet_name'),
            'total_profit_usd': s.get('total_profit_usd'),
            'token_win_rate_pct': s.get('token_win_rate_pct'),
        }
        for s in ranked[-10:][::-1]
    ]

    result: Dict[str, Any] = {
        'schema': SCHEMA_COHORT,
        'total_wallets': index.get('total_wallets') or len(wallet_stats),
        'wallets_with_payload': len(wallet_stats),
        'wallets_with_token_rows': sum(1 for s in wallet_stats if s.get('token_count', 0) > 0),
        'aggregate': {
            'total_profit_usd': round(sum(profits), 2),
            'median_profit_usd': round(_median(profits), 2) if profits else None,
            'mean_profit_usd': round(statistics.mean(profits), 2) if profits else None,
            'profit_p25_usd': round(_percentile(profits, 25), 2) if profits else None,
            'profit_p50_usd': round(_percentile(profits, 50), 2) if profits else None,
            'profit_p75_usd': round(_percentile(profits, 75), 2) if profits else None,
            'profit_p90_usd': round(_percentile(profits, 90), 2) if profits else None,
            'median_token_win_rate_pct': round(_median(win_rates), 2) if win_rates else None,
            'median_profit_factor': round(_median(factors), 3) if factors else None,
        },
        'distribution': {
            'profitable_wallets': profitable,
            'breakeven_wallets': breakeven,
            'losing_wallets': losing,
            'profitable_pct': round(100.0 * profitable / len(profits), 1) if profits else None,
        },
        'performance_tiers': tier_counts,
        'top10': top10,
        'bottom10': bottom10,
    }
    if include_wallets:
        result['wallets'] = ranked
    return result


def build_wallet_report(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Single response for FE: summary + GMGN rows + analysis from those rows only."""
    from app.portfolio_pnl.normalize import enrich_cached_rows

    rows = enrich_cached_rows(list(payload.get('rows') or []))
    daily_calendar = list(payload.get('daily_calendar') or [])
    calendar_is_official = bool(daily_calendar) and all(
        row.get('source') == 'gmgn_web_daily_profits'
        for row in daily_calendar
    )
    payload = {**payload, 'rows': rows}
    analysis = compute_wallet_stats(payload)
    active = [r for r in rows if _num(r.get('bought_usd')) > 0]
    returns = [r for r in active if r.get('total_profit_pct') is not None]
    return_bands = [
        ('gt_500', lambda value: value > 500),
        ('200_500', lambda value: 200 < value <= 500),
        ('0_200', lambda value: 0 <= value <= 200),
        ('minus_50_0', lambda value: -50 <= value < 0),
        ('lt_minus_50', lambda value: value < -50),
    ]
    analysis['return_distribution'] = {
        key: sum(1 for row in returns if predicate(_num(row.get('total_profit_pct'))))
        for key, predicate in return_bands
    }
    mc_values = [_num(r.get('bought_mc')) for r in active if r.get('bought_mc') is not None]
    analysis['avg_buy_mc_distribution'] = {
        '0_100k': sum(1 for value in mc_values if value < 100_000),
        '100k_500k': sum(1 for value in mc_values if 100_000 <= value <= 500_000),
        'gt_500k': sum(1 for value in mc_values if value > 500_000),
        'sample_size': len(mc_values),
    }
    analysis['safety'] = {
        'honeypot_count': sum(1 for r in rows if r.get('is_honeypot')),
        'alert_count': sum(1 for r in rows if r.get('is_alert')),
        'didnt_buy_count': sum(1 for r in rows if r.get('didnt_buy')),
        'tiny_pool_count': sum(1 for r in rows if r.get('is_tiny_pool')),
    }
    return {
        'schema': 'gmgn_recent_pnl_lookup_v1',
        'wallet_address': payload.get('wallet_address'),
        'wallet_name': payload.get('wallet_name') or '',
        'fetched_at': payload.get('fetched_at'),
        'collection': payload.get('collection') or {
            'complete': False,
            'reason': 'legacy_snapshot_without_completeness_metadata',
        },
        'summary': payload.get('summary') or {},
        'periods': payload.get('periods') or {},
        'period_stats': payload.get('period_stats') or {},
        'daily_calendar': daily_calendar,
        'daily_calendar_metadata': {
            'metric': 'daily_realized_pnl',
            'source': 'gmgn_web_daily_profits' if calendar_is_official else 'gmgn_sell_activity',
            'is_official_gmgn_calendar': calendar_is_official,
            'missing_day_value': 0 if calendar_is_official else None,
        },
        'unsupported_periods': {
            '1y': 'GMGN portfolio profits chỉ hỗ trợ 1d, 7d, 30d và all.',
        },
        'rows': rows,
        'analysis': analysis,
    }


def get_wallet_stats(wallet_address: str, cohort_cache: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    payload = load_wallet_pnl(wallet_address)
    if not payload:
        return None
    stats = compute_wallet_stats(payload)
    cohort = cohort_cache if cohort_cache is not None else compute_cohort_stats(include_wallets=True)
    extra = next((w for w in cohort.get('wallets', []) if w.get('wallet_address') == wallet_address), None)
    if extra:
        stats['cohort_rank'] = extra.get('cohort_rank')
        stats['cohort_percentile'] = extra.get('cohort_percentile')
        stats['cohort_total'] = cohort.get('wallets_with_payload')
    return stats
