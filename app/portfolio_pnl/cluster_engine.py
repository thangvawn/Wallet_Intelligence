"""Pure deterministic position cluster and conviction behavior engine."""
from collections import defaultdict
import math
import statistics
from typing import Any, Dict, List, Optional, Tuple


def _num(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        f = float(val)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def build_position_clusters(events: List[dict]) -> List[dict]:
    """Reconstruct continuous exposure cycles with proportional cost basis.

    Only cycles with an observable basis and a sell close are eligible for PnL
    metrics. Transfers still change exposure, but an unpriced transfer-in marks
    that cycle as incomplete instead of fabricating a zero cost basis.
    """
    if not events:
        return []

    sorted_events = sorted(events, key=lambda e: (_num(e.get('timestamp')), e.get('tx_hash', '')))
    tokens = defaultdict(list)
    for event in sorted_events:
        event_type = str(event.get('event_type') or '').lower()
        if event_type in ('buy', 'sell', 'transferin', 'transferout'):
            token_address = str(event.get('token_address') or '')
            if token_address:
                tokens[token_address].append(event)

    clusters: List[dict] = []
    cluster_seq = 1

    for token_address, token_events in tokens.items():
        running_tokens = 0.0
        remaining_basis = 0.0
        current: Optional[dict] = None

        def start_cluster(event: dict, timestamp: int) -> dict:
            nonlocal cluster_seq
            cluster = {
                'cluster_id': f'CLUSTER-{cluster_seq:04d}',
                'token_address': token_address,
                'token_symbol': event.get('token_symbol') or '',
                'start_timestamp': timestamp,
                'end_timestamp': timestamp,
                'buys_count': 0,
                'sells_count': 0,
                'transfer_in_count': 0,
                'transfer_out_count': 0,
                'capital_deployed_usd': 0.0,
                'sell_proceeds_usd': 0.0,
                'realized_cost_basis_usd': 0.0,
                'fees_usd': 0.0,
                'realized_pnl_usd': 0.0,
                'buy_events': [],
                'sell_events': [],
                'transfer_events': [],
                'max_exposure_usd': 0.0,
                'cost_basis_complete': True,
                'is_closed': False,
                'closed_by': None,
            }
            cluster_seq += 1
            return cluster

        def finish_cluster(closed_by: str) -> None:
            nonlocal current, running_tokens, remaining_basis
            if current is None:
                return
            current['is_closed'] = True
            current['closed_by'] = closed_by
            current['holding_period_seconds'] = max(0, current['end_timestamp'] - current['start_timestamp'])
            deployed = current['capital_deployed_usd']
            pnl = current['realized_pnl_usd']
            current['roi_pct'] = round(pnl / deployed * 100.0, 2) if deployed > 0 else None
            current['is_win'] = pnl > 0.0
            current['outcome_eligible'] = bool(
                closed_by == 'sell'
                and current['sells_count'] > 0
                and current['cost_basis_complete']
                and current['transfer_out_count'] == 0
                and deployed > 0
            )
            current['remaining_token_amount'] = 0.0
            current['remaining_cost_basis_usd'] = 0.0
            current['realized_pnl_usd'] = round(pnl, 2)
            clusters.append(current)
            current = None
            running_tokens = 0.0
            remaining_basis = 0.0

        for event in token_events:
            event_type = str(event.get('event_type') or '').lower()
            amount = abs(_num(event.get('token_amount')))
            usd = abs(_num(event.get('amount_usd') or event.get('cost_usd')))
            price = _num(event.get('price_usd'))
            timestamp = int(_num(event.get('timestamp')))
            gas = abs(_num(event.get('gas_usd')))

            if event_type == 'buy':
                if amount <= 0:
                    continue
                if current is None or running_tokens <= 0:
                    current = start_cluster(event, timestamp)
                    running_tokens = remaining_basis = 0.0
                running_tokens += amount
                remaining_basis += usd + gas
                current['buys_count'] += 1
                current['capital_deployed_usd'] += usd + gas
                current['fees_usd'] += gas
                current['end_timestamp'] = max(current['end_timestamp'], timestamp)
                current['buy_events'].append({
                    'timestamp': timestamp, 'amount_usd': usd,
                    'price_usd': price, 'token_amount': amount,
                })
                exposure = running_tokens * price if price > 0 else remaining_basis
                current['max_exposure_usd'] = max(current['max_exposure_usd'], exposure)

            elif event_type == 'transferin':
                if amount <= 0:
                    continue
                if current is None or running_tokens <= 0:
                    current = start_cluster(event, timestamp)
                    running_tokens = remaining_basis = 0.0
                running_tokens += amount
                current['transfer_in_count'] += 1
                current['end_timestamp'] = max(current['end_timestamp'], timestamp)
                current['transfer_events'].append({
                    'timestamp': timestamp, 'type': event_type,
                    'token_amount': amount, 'amount_usd': usd,
                })
                if usd > 0:
                    remaining_basis += usd
                    current['capital_deployed_usd'] += usd
                else:
                    current['cost_basis_complete'] = False

            elif event_type == 'sell' and current is not None and running_tokens > 0 and amount > 0:
                quantity_before = running_tokens
                sold_quantity = min(amount, quantity_before)
                sold_fraction = min(1.0, sold_quantity / quantity_before)
                allocated_basis = remaining_basis * sold_fraction
                current['realized_pnl_usd'] += usd - allocated_basis - gas
                current['realized_cost_basis_usd'] += allocated_basis
                current['sell_proceeds_usd'] += usd
                current['fees_usd'] += gas
                current['sells_count'] += 1
                current['end_timestamp'] = max(current['end_timestamp'], timestamp)
                current['sell_events'].append({
                    'timestamp': timestamp, 'amount_usd': usd,
                    'price_usd': price, 'token_amount': amount,
                    'allocated_cost_basis_usd': allocated_basis,
                })
                if amount > quantity_before * 1.000001:
                    current['cost_basis_complete'] = False
                running_tokens = max(0.0, quantity_before - sold_quantity)
                remaining_basis = max(0.0, remaining_basis - allocated_basis)
                if sold_fraction >= 0.999999 or running_tokens <= max(quantity_before * 1e-9, 1e-18):
                    finish_cluster('sell')

            elif event_type == 'transferout' and current is not None and running_tokens > 0 and amount > 0:
                quantity_before = running_tokens
                removed_fraction = min(1.0, amount / quantity_before)
                remaining_basis *= 1.0 - removed_fraction
                running_tokens = max(0.0, quantity_before - min(amount, quantity_before))
                current['transfer_out_count'] += 1
                current['end_timestamp'] = max(current['end_timestamp'], timestamp)
                current['transfer_events'].append({
                    'timestamp': timestamp, 'type': event_type,
                    'token_amount': amount, 'amount_usd': usd,
                })
                if removed_fraction >= 0.999999 or running_tokens <= max(quantity_before * 1e-9, 1e-18):
                    finish_cluster('transferout')

        if current is not None:
            current['is_closed'] = False
            current['holding_period_seconds'] = max(0, current['end_timestamp'] - current['start_timestamp'])
            current['realized_pnl_usd'] = round(current['realized_pnl_usd'], 2)
            current['remaining_token_amount'] = running_tokens
            current['remaining_cost_basis_usd'] = round(remaining_basis, 2)
            current['roi_pct'] = None
            current['is_win'] = None
            current['outcome_eligible'] = False
            clusters.append(current)

    return clusters


def compute_cluster_profile(clusters: List[dict]) -> dict:
    """Compute wallet-level cluster statistics (Section 9)."""
    if not clusters:
        return {
            'status': 'NO_ACTIVITY_DATA',
            'cluster_definition': 'continuous exposure cycles reconstructed from activity stream',
            'total_clusters': 0,
            'closed_clusters': 0,
            'winning_clusters': 0,
            'cluster_win_rate_pct': None,
            'average_holding_period_hours': None,
            'median_cluster_capital_usd': None,
            'limitation': 'No activity events available.',
        }

    closed = [c for c in clusters if c.get('is_closed')]
    eligible = [c for c in closed if c.get('outcome_eligible')]
    wins = [c for c in eligible if c.get('is_win')]
    win_rate = (len(wins) / len(eligible) * 100.0) if eligible else None

    holding_hours = [c.get('holding_period_seconds', 0) / 3600.0 for c in closed if c.get('holding_period_seconds')]
    capitals = [c.get('capital_deployed_usd', 0.0) for c in clusters if c.get('capital_deployed_usd', 0.0) > 0]

    scale_in_count = 0
    destructive_avg_down_count = 0
    for c in clusters:
        buy_evs = c.get('buy_events') or []
        if len(buy_evs) >= 2:
            p0 = buy_evs[0].get('price_usd') or 0.0
            p1 = buy_evs[1].get('price_usd') or 0.0
            pnl = c.get('realized_pnl_usd', 0.0)
            if p0 > 0:
                ratio = p1 / p0
                if ratio >= 0.90:
                    scale_in_count += 1
                elif ratio < 0.50 and pnl < -1000.0 and (c.get('roi_pct') or 0.0) < -50.0:
                    destructive_avg_down_count += 1

    return {
        'status': 'TRUE_MULTI_CYCLE_CLUSTERS',
        'cluster_definition': 'continuous exposure cycles reconstructed from activity stream',
        'total_clusters': len(clusters),
        'closed_clusters': len(closed),
        'outcome_eligible_clusters': len(eligible),
        'winning_clusters': len(wins),
        'scale_in_clusters': scale_in_count,
        'destructive_avg_down_clusters': destructive_avg_down_count,
        'cluster_win_rate_pct': round(win_rate, 1) if win_rate is not None else None,
        'average_holding_period_hours': round(statistics.median(holding_hours), 1) if holding_hours else None,
        'median_cluster_capital_usd': round(statistics.median(capitals), 2) if capitals else None,
        'limitation': None,
    }


def analyze_conviction_and_scaling(clusters: List[dict]) -> Tuple[dict, dict]:
    """Analyze scale style, destructive averaging down, and conviction verdict (Section 10)."""
    if not clusters:
        scale_dict = {
            'status': 'INSUFFICIENT_DATA',
            'style': 'INSUFFICIENT_DATA',
            'median_buy_tx': None,
            'multi_buy_rate_pct': None,
            'average_buy_interval_seconds': None,
            'initial_buy_median_usd': None,
        }
        conviction_dict = {
            'conviction_verdict': 'INSUFFICIENT_DATA',
            'sample_size': 0,
            'conviction_analysis': 'Chưa có dữ liệu activity clusters để đánh giá.',
        }
        return scale_dict, conviction_dict

    multi_buy_clusters = [c for c in clusters if c.get('buys_count', 0) >= 2]
    multi_buy_rate = (len(multi_buy_clusters) / len(clusters) * 100.0) if clusters else 0.0

    all_buy_counts = [c.get('buys_count', 0) for c in clusters]
    median_buys = statistics.median(all_buy_counts) if all_buy_counts else 1.0

    intervals: List[int] = []
    initial_buys: List[float] = []
    scale_in_count = 0
    destructive_avg_down_count = 0

    for c in clusters:
        buy_evs = c.get('buy_events') or []
        if buy_evs:
            initial_buys.append(buy_evs[0]['amount_usd'])
            for i in range(1, len(buy_evs)):
                diff = max(0, buy_evs[i]['timestamp'] - buy_evs[i - 1]['timestamp'])
                intervals.append(diff)

        if len(buy_evs) >= 2:
            p0 = buy_evs[0]['price_usd']
            p1 = buy_evs[1]['price_usd']
            pnl = c.get('realized_pnl_usd', 0.0)
            if p0 > 0:
                ratio = p1 / p0
                if ratio >= 0.90:
                    scale_in_count += 1
                elif ratio < 0.50 and pnl < -1000.0 and (c.get('roi_pct') or 0.0) < -50.0:
                    destructive_avg_down_count += 1

    # Determine scale style
    # D5 is defined over all reconstructed position/trade clusters: more than
    # three destructive averaging-down episodes comprising at least 10%.
    destructive_rate = (destructive_avg_down_count / len(clusters)) if clusters else 0.0
    if destructive_avg_down_count >= 3 and destructive_rate >= 0.20:
        style = 'DESTRUCTIVE_AVERAGING_DOWN'
    elif len(multi_buy_clusters) >= 3 and scale_in_count >= len(multi_buy_clusters) * 0.4:
        style = 'SCALES_IN'
    elif multi_buy_rate >= 30.0:
        style = 'UNIFORM_ADD'
    else:
        style = 'SINGLE_ENTRY_BIAS'

    destructive_flag = bool(
        destructive_avg_down_count > 3 and destructive_rate >= 0.10
    )

    scale_dict = {
        'status': 'AVAILABLE',
        'style': style,
        'destructive_averaging_down': destructive_flag,
        'median_buy_tx': round(median_buys, 1),
        'multi_buy_rate_pct': round(multi_buy_rate, 1),
        'average_buy_interval_seconds': int(statistics.median(intervals)) if intervals else None,
        'initial_buy_median_usd': round(statistics.median(initial_buys), 2) if initial_buys else None,
        'scale_in_clusters_count': scale_in_count,
        'destructive_avg_down_count': destructive_avg_down_count,
        'destructive_avg_down_rate_pct': round(destructive_rate * 100.0, 1),
    }

    # Analyze Conviction: Compare high-capital clusters vs low-capital clusters
    closed_clusters = [
        c for c in clusters
        if c.get('outcome_eligible') and c.get('capital_deployed_usd', 0) > 0
    ]
    if len(closed_clusters) < 5:
        conviction_dict = {
            'conviction_verdict': 'INSUFFICIENT_DATA',
            'sample_size': len(closed_clusters),
            'conviction_analysis': 'Cần tối thiểu 5 position clusters đóng để đánh giá conviction.',
        }
    else:
        median_cap = statistics.median(c['capital_deployed_usd'] for c in closed_clusters)
        large = [c for c in closed_clusters if c['capital_deployed_usd'] >= median_cap]
        small = [c for c in closed_clusters if c['capital_deployed_usd'] < median_cap]

        large_win_rate = (sum(1 for c in large if c.get('is_win')) / len(large) * 100.0) if large else 0.0
        small_win_rate = (sum(1 for c in small if c.get('is_win')) / len(small) * 100.0) if small else 0.0

        large_pnl = sum(c.get('realized_pnl_usd', 0.0) for c in large)
        small_pnl = sum(c.get('realized_pnl_usd', 0.0) for c in small)

        if large_win_rate >= small_win_rate + 5.0 and large_pnl > small_pnl and large_pnl > 0:
            verdict = 'POSITIVE_SIZE_EDGE'
            analysis = 'Nhóm cluster phân bổ vốn lớn có Win Rate và PnL vượt trội, chứng minh có lợi thế quy mô (Conviction edge).'
        elif large_win_rate < small_win_rate - 15.0 or (large_pnl < 0 and small_pnl > 0):
            verdict = 'OVERCONFIDENCE_RISK'
            analysis = 'Nhóm cluster vốn lớn có kết quả kém hơn rõ rệt; thể hiện rủi ro over-sizing.'
        else:
            verdict = 'NEUTRAL'
            analysis = 'Kết quả giữa nhóm vị thế lớn và nhỏ không có sự chênh lệch đáng kể.'

        conviction_dict = {
            'conviction_verdict': verdict,
            'sample_size': len(closed_clusters),
            'median_cluster_capital_usd': round(median_cap, 2),
            'large_cluster_win_rate_pct': round(large_win_rate, 1),
            'small_cluster_win_rate_pct': round(small_win_rate, 1),
            'large_cluster_total_pnl_usd': round(large_pnl, 2),
            'small_cluster_total_pnl_usd': round(small_pnl, 2),
            'conviction_analysis': analysis,
        }

    return scale_dict, conviction_dict
