"""Deterministic wallet group labels (G1--G4).

The legacy label code contains many exploratory personas.  This module keeps the
four business groups requested for production separate from those personas.  It
accepts normalized holdings and raw activity events, derives one canonical
observation per token, and never treats missing evidence as a passing condition.

All percentages are in the inclusive range 0--100.  A metric is ``None`` when it
cannot be measured; the corresponding rule then fails closed.
"""
from __future__ import annotations

import math
import re
import statistics
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional


LOWCAP_MIN_USD = 30_000.0
LOWCAP_MAX_USD = 150_000.0
EARLY_MAX_MINUTES = 60.0
G1_MIN_SAMPLE = 15
G1_MIN_RATE_PCT = 60.0
WHALE_MEDIAN_VOLUME_USD = 25_000.0
WHALE_FALLBACK_VOLUME_USD = 5_000.0
WHALE_MEDIAN_BUY_ORDER_USD = 1_000.0
WHALE_CAPITAL_BASE_USD = 100_000.0
CONVICTION_MIN_PASSES = 3
HOLDER_MIN_SCORE = 2
UNICON_MIN_REALIZED_PNL_USD = 100_000.0
NARRATIVE_WINDOW_SECONDS = 72 * 3600

# Keep output ordering deterministic so exports and UI filters are stable.
GROUP_ORDER = ('g1', 'g2', 'g3', 'g4')

_STOPWORDS = {
    'SOL', 'WSOL', 'USDC', 'USDT', 'COIN', 'TOKEN', 'PUMP', 'THE',
    'PUMPSWAP', 'MEME', 'PROTOCOL', 'SWAP', 'FINANCE', 'LABS',
    'COMMUNITY', 'OFFICIAL', 'REAL', 'NEW', 'INU', 'DAO', 'LP',
}


def _num(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _positive(value: Any) -> Optional[float]:
    result = _num(value)
    return result if result is not None and result > 0 else None


def _timestamp(value: Any) -> Optional[int]:
    """Normalize seconds/milliseconds timestamps to Unix seconds."""
    result = _num(value)
    if result is None or result <= 0:
        return None
    if result > 100_000_000_000:  # milliseconds (and larger common units)
        result /= 1000.0
    return int(result)


def _pct(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(max(0.0, min(100.0, numerator * 100.0 / denominator)), 2)


def _median(values: Iterable[Any]) -> Optional[float]:
    cleaned = [float(v) for v in values if _num(v) is not None]
    return round(statistics.median(cleaned), 2) if cleaned else None


def _first(mapping: dict, *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != '':
            return value
    return None


def _token_key(value: Any) -> str:
    return str(value or '').strip()


def _event_type(event: dict) -> str:
    return str(event.get('event_type') or event.get('type') or '').strip().lower()


def _event_usd(event: dict) -> Optional[float]:
    return _positive(_first(event, 'amount_usd', 'cost_usd', 'buy_cost_usd', 'sell_income_usd'))


def _event_token_amount(event: dict) -> Optional[float]:
    return _positive(_first(event, 'token_amount', 'amount_token', 'amount'))


def _keywords(symbol: Any, name: Any) -> List[str]:
    text = f'{symbol or ""} {name or ""}'.upper()
    words = re.findall(r'[A-Z0-9]{3,}', text)
    return sorted({word for word in words if word not in _STOPWORDS and not word.isdigit()})


def _canonical_tokens(holdings: List[dict], events: List[dict]) -> Dict[str, dict]:
    """Build one normalized token record from both sources.

    Activity is authoritative for first buy and order statistics.  Holdings are
    the fallback for closed tokens and historical market-cap/launch metadata.
    """
    tokens: Dict[str, dict] = {}
    for row in holdings or []:
        address = _token_key(_first(row, 'token_address', 'address'))
        if not address:
            continue
        tokens[address] = {
            'token_address': address,
            'symbol': _first(row, 'token_symbol', 'symbol') or '',
            'name': _first(row, 'token_name', 'name') or '',
            'launch_ts': _timestamp(_first(row, 'token_launch_time', 'token_creation_timestamp', 'creation_timestamp')),
            'first_buy_ts': _timestamp(_first(row, 'wallet_first_buy_time', 'first_buy_timestamp', 'start_holding_timestamp')),
            'first_buy_price': _positive(_first(row, 'wallet_first_buy_price', 'first_buy_price', 'avg_buy_price')),
            'market_cap': _positive(_first(row, 'market_cap_at_first_buy_usd', 'bought_mc', 'buy_market_cap')),
            'first_buy_amount_usd': _positive(_first(row, 'first_buy_amount_usd', 'wallet_first_buy_amount_usd')),
            'position_usd': _positive(_first(row, 'total_position_usd', 'bought_usd')) or 0.0,
            'realized_pnl_usd': _num(_first(row, 'realized_profit_usd', 'realized_pnl_usd')),
            'events': [],
        }
    for event in events or []:
        address = _token_key(event.get('token_address'))
        if not address:
            continue
        record = tokens.setdefault(address, {
            'token_address': address, 'symbol': '', 'name': '',
            'launch_ts': None, 'first_buy_ts': None, 'first_buy_price': None,
            'market_cap': None, 'first_buy_amount_usd': None,
            'position_usd': 0.0, 'realized_pnl_usd': None, 'events': [],
        })
        if event.get('token_symbol') and not record['symbol']:
            record['symbol'] = event.get('token_symbol')
        if _event_type(event) in {'buy', 'sell', 'transferin', 'transferout'}:
            record['events'].append(event)

    for record in tokens.values():
        token_events = sorted(
            record['events'],
            key=lambda event: (_timestamp(event.get('timestamp')) or 0, str(event.get('tx_hash') or '')),
        )
        buys = [event for event in token_events if _event_type(event) == 'buy']
        if buys:
            first = buys[0]
            record['first_buy_ts'] = _timestamp(first.get('timestamp')) or record['first_buy_ts']
            record['first_buy_price'] = _positive(first.get('price_usd')) or record['first_buy_price']
            record['first_buy_amount_usd'] = _event_usd(first) or record['first_buy_amount_usd']
            record['position_usd'] = sum((_event_usd(event) or 0.0) for event in buys) or record['position_usd']
        record['buy_events'] = buys
        record['sell_events'] = [event for event in token_events if _event_type(event) == 'sell']
        record['all_events'] = token_events
        if record['first_buy_ts'] and record['launch_ts']:
            age = (record['first_buy_ts'] - record['launch_ts']) / 60.0
            record['entry_age_minutes'] = age if age >= 0 else None
        else:
            # token_age_at_entry_hours is the already normalized fallback.
            age_hours = _num(next(
                (row.get('token_age_at_entry_hours') for row in holdings or []
                 if _token_key(_first(row, 'token_address', 'address')) == record['token_address']),
                None,
            ))
            record['entry_age_minutes'] = age_hours * 60.0 if age_hours is not None and age_hours >= 0 else None
        record['position_action_span_hours'] = _position_span_hours(record)
        record['first_sell_pct'] = _first_sell_pct(record)
        record['sell_100pct'] = _sold_all(record)
        record['averaging_down'] = _averaging_down(record)
        record['volume_usd'] = sum((_event_usd(event) or 0.0) for event in record['all_events'])
        if record['volume_usd'] <= 0:
            record['volume_usd'] = record['position_usd'] or 0.0
        record['buy_order_usd'] = [value for value in (_event_usd(event) for event in record['buy_events']) if value is not None]
        # Public canonical field names requested for the normalized token table.
        record['token_launch_time'] = record.get('launch_ts')
        record['wallet_first_buy_time'] = record.get('first_buy_ts')
        record['wallet_first_buy_price'] = record.get('first_buy_price')
        record['market_cap_at_first_buy_usd'] = record.get('market_cap')
        record['total_position_usd'] = record.get('position_usd')
    return tokens


def _position_span_hours(record: dict) -> Optional[float]:
    timestamps = [_timestamp(event.get('timestamp')) for event in record.get('all_events', [])]
    timestamps = [value for value in timestamps if value is not None]
    if len(timestamps) >= 2:
        return round(max(0, max(timestamps) - min(timestamps)) / 3600.0, 4)
    return None


def _first_sell_pct(record: dict) -> Optional[float]:
    sells = record.get('sell_events') or []
    buys = record.get('buy_events') or []
    if not sells or not buys:
        return None
    total_bought = sum((_event_token_amount(event) or 0.0) for event in buys)
    first_sold = _event_token_amount(sells[0])
    if not total_bought or first_sold is None:
        return None
    return round(max(0.0, min(100.0, first_sold * 100.0 / total_bought)), 2)


def _sold_all(record: dict) -> Optional[bool]:
    buys = sum((_event_token_amount(event) or 0.0) for event in record.get('buy_events') or [])
    sells = sum((_event_token_amount(event) or 0.0) for event in record.get('sell_events') or [])
    if buys <= 0 or not record.get('sell_events'):
        return None
    return sells >= buys * (1.0 - 1e-6)


def _averaging_down(record: dict) -> Optional[bool]:
    buys = record.get('buy_events') or []
    if len(buys) < 2:
        return False if buys else None
    prices = [_positive(event.get('price_usd')) for event in buys]
    prices = [price for price in prices if price is not None]
    if len(prices) < 2:
        return None
    return prices[1] < prices[0]


def _g1(tokens: Dict[str, dict]) -> dict:
    sample = len(tokens)
    lowcap = [r for r in tokens.values() if r.get('market_cap') is not None and LOWCAP_MIN_USD <= r['market_cap'] <= LOWCAP_MAX_USD]
    early = [r for r in tokens.values() if r.get('entry_age_minutes') is not None and r['entry_age_minutes'] <= EARLY_MAX_MINUTES]
    early_lowcap = [r for r in lowcap if r.get('entry_age_minutes') is not None and r['entry_age_minutes'] <= EARLY_MAX_MINUTES]
    result = {
        'token_sample': sample,
        'lowcap_entry_count': len(lowcap),
        'lowcap_entry_rate_pct': _pct(len(lowcap), sample),
        'early_entry_count': len(early),
        'early_entry_rate_pct': _pct(len(early), sample),
        'early_lowcap_count': len(early_lowcap),
        'early_lowcap_rate_pct': _pct(len(early_lowcap), sample),
        'median_entry_market_cap_usd': _median(r.get('market_cap') for r in tokens.values()),
        'median_entry_age_minutes': _median(r.get('entry_age_minutes') for r in tokens.values()),
    }
    result['qualified'] = bool(
        sample >= G1_MIN_SAMPLE and
        result['early_lowcap_rate_pct'] is not None and
        result['early_lowcap_rate_pct'] >= G1_MIN_RATE_PCT
    )
    result['label'] = 'G1_EARLY_MICROCAP_SPECIALIST' if result['qualified'] else None
    return result


def _g2(tokens: Dict[str, dict], capital_base_usd: Optional[float]) -> dict:
    records = list(tokens.values())
    volumes = [r['volume_usd'] for r in records if _positive(r.get('volume_usd')) is not None]
    buy_orders = [value for r in records for value in (r.get('buy_order_usd') or []) if _positive(value) is not None]
    median_volume = _median(volumes)
    median_buy_order = _median(buy_orders)
    capital = _positive(capital_base_usd)
    size_edge = bool(
        median_volume is not None and (
            median_volume >= WHALE_MEDIAN_VOLUME_USD or
            (median_volume >= WHALE_FALLBACK_VOLUME_USD and median_buy_order is not None and
             median_buy_order >= WHALE_MEDIAN_BUY_ORDER_USD and capital is not None and
             capital >= WHALE_CAPITAL_BASE_USD)
        )
    )
    conviction_values = {
        'multi_buy': _pct(sum(1 for r in records if len(r.get('buy_events') or []) >= 2), len(records)),
        'holding': _median(r.get('position_action_span_hours') for r in records),
        'first_sell': _median(r.get('first_sell_pct') for r in records),
        'sell_100pct': _pct(sum(1 for r in records if r.get('sell_100pct') is True), sum(1 for r in records if r.get('sell_100pct') is not None)),
        'averaging_down': _pct(sum(1 for r in records if r.get('averaging_down') is True), sum(1 for r in records if r.get('averaging_down') is not None)),
    }
    conviction_checks = {
        'multi_buy': conviction_values['multi_buy'] is not None and conviction_values['multi_buy'] >= 60.0,
        'holding': conviction_values['holding'] is not None and conviction_values['holding'] >= 12.0,
        'first_sell': conviction_values['first_sell'] is not None and conviction_values['first_sell'] < 90.0,
        'sell_100pct': conviction_values['sell_100pct'] is not None and conviction_values['sell_100pct'] < 50.0,
        'averaging_down': conviction_values['averaging_down'] is not None and conviction_values['averaging_down'] < 20.0,
    }
    passed = sum(conviction_checks.values())
    result = {
        'median_volume_usd': median_volume,
        'median_buy_order_usd': median_buy_order,
        # Short aliases mirror the business rule wording.
        'median_volume': median_volume,
        'median_buy_order': median_buy_order,
        'capital_base_usd': capital,
        'whale_size': size_edge,
        'whale': size_edge,
        'is_whale': size_edge,
        'conviction_metrics': conviction_values,
        'conviction_checks': conviction_checks,
        'conviction_pass_count': passed,
        'conviction': passed >= CONVICTION_MIN_PASSES,
        'qualified': size_edge and passed >= CONVICTION_MIN_PASSES,
    }
    result['is_conviction'] = result['conviction']
    result['label'] = 'G2_WHALE_CONVICTION' if result['qualified'] else None
    return result


def _g3(tokens: Dict[str, dict]) -> dict:
    records = list(tokens.values())
    clusters = []
    for anchor in records:
        anchor_ts = anchor.get('first_buy_ts')
        if anchor_ts is None:
            continue
        window = [r for r in records if r.get('first_buy_ts') is not None and anchor_ts <= r['first_buy_ts'] <= anchor_ts + NARRATIVE_WINDOW_SECONDS]
        groups: Dict[str, List[dict]] = defaultdict(list)
        for record in window:
            for keyword in _keywords(record.get('symbol'), record.get('name')):
                groups[keyword].append(record)
        for keyword, members in groups.items():
            unique = {r['token_address']: r for r in members}
            if len(unique) < 2:
                continue
            unique_values = list(unique.values())
            volumes = [r.get('volume_usd') or 0.0 for r in unique_values]
            total = sum(volumes)
            clusters.append({
                'primary_narrative_keyword': keyword,
                'same_theme_ca_count': len(unique_values),
                'total_ca_in_window': len(window),
                'same_theme_ca_share_pct': round(len(unique_values) * 100.0 / len(window), 2) if window else None,
                'theme_buy_window_hours': round((max(r['first_buy_ts'] for r in unique_values) - min(r['first_buy_ts'] for r in unique_values)) / 3600.0, 2),
                'largest_ca_share_in_theme': round(max(volumes) * 100.0 / total, 2) if total > 0 else None,
                'total_theme_volume_usd': round(total, 2),
            })
    best = max(clusters, key=lambda x: (x['same_theme_ca_count'], x['same_theme_ca_share_pct'] or 0), default=None)
    if best is None:
        return {
            'qualified': False, 'label': None, 'is_basket_narrative': False,
            'primary_narrative_keyword': None, 'same_theme_ca_count': 0,
            'total_ca_in_window': 0, 'same_theme_ca_share_pct': None,
            'theme_buy_window_hours': None, 'largest_ca_share_in_theme': None,
            'total_theme_volume_usd': 0.0,
        }
    best = dict(best)
    best['is_basket_narrative'] = bool(best.get('largest_ca_share_in_theme') is not None and best['largest_ca_share_in_theme'] <= 50.0)
    best['qualified'] = True
    best['label'] = 'G3_NARRATIVE_BASKET'
    return best


def _g4(tokens: Dict[str, dict]) -> dict:
    records = list(tokens.values())
    realized_values = []
    for record in records:
        realized = _realized_pnl(record)
        record['_realized_pnl_usd'] = realized
        realized_values.append(realized)
    max_realized = max(realized_values, default=0.0)
    spans = [r.get('position_action_span_hours') for r in records if r.get('position_action_span_hours') is not None]
    first_sells = [r.get('first_sell_pct') for r in records if r.get('first_sell_pct') is not None]
    sell_flags = [r.get('sell_100pct') for r in records if r.get('sell_100pct') is not None]
    median_span = _median(spans)
    median_first_sell = _median(first_sells)
    sell_100_rate = _pct(sum(1 for value in sell_flags if value), len(sell_flags))
    holder_checks = {
        'holding_span': median_span is not None and median_span >= 12.0,
        'scale_out': median_first_sell is not None and median_first_sell < 90.0,
        'sell_100pct_rate': sell_100_rate is not None and sell_100_rate < 50.0,
    }
    score = sum(holder_checks.values())
    qualified = max_realized >= UNICON_MIN_REALIZED_PNL_USD and score >= HOLDER_MIN_SCORE
    return {
        'max_realized_pnl_single_token_usd': round(max_realized, 2),
        'median_position_action_span_hours': median_span,
        'median_first_sell_pct': median_first_sell,
        'sell_100pct_rate': sell_100_rate,
        'holder_checks': holder_checks,
        'holder_score': score,
        'qualified': qualified,
        'is_holder': score >= HOLDER_MIN_SCORE,
        'label': 'G4_UNICON_HOLDER' if qualified else None,
    }


def _realized_pnl(record: dict) -> float:
    """Estimate realized PnL from event cost basis, with holdings fallback.

    The activity feed has execution proceeds and token quantities but not a
    position id.  Proportional cost-basis allocation is deterministic and matches
    the portfolio cluster engine's semantics closely enough for a group label.
    """
    events = record.get('all_events') or []
    running_amount = 0.0
    remaining_basis = 0.0
    realized = 0.0
    saw_sell = False
    for event in events:
        kind = _event_type(event)
        amount = _event_token_amount(event) or 0.0
        usd = _event_usd(event) or 0.0
        gas = _positive(event.get('gas_usd')) or 0.0
        if kind == 'buy' and amount > 0:
            running_amount += amount
            remaining_basis += usd + gas
        elif kind == 'sell' and amount > 0 and running_amount > 0:
            saw_sell = True
            fraction = min(1.0, amount / running_amount)
            allocated = remaining_basis * fraction
            realized += usd - allocated - gas
            running_amount = max(0.0, running_amount - amount)
            remaining_basis = max(0.0, remaining_basis - allocated)
    if saw_sell:
        return round(realized, 2)
    return round(_num(record.get('realized_pnl_usd')) or 0.0, 2)


def qualified_group_labels(group_payload: Optional[dict]) -> List[str]:
    """Return all qualified labels from the canonical per-group results."""
    groups = group_payload if isinstance(group_payload, dict) else {}
    labels: List[str] = []
    for key in GROUP_ORDER:
        group = groups.get(key) or {}
        if not isinstance(group, dict) or not group.get('qualified'):
            continue
        label = group.get('label')
        if label:
            labels.append(str(label))
    return labels


def classify_wallet_groups(
    holdings: Optional[List[dict]] = None,
    activity_events: Optional[List[dict]] = None,
    capital_base_usd: Optional[float] = None,
) -> dict:
    """Return normalized token observations and the requested G1--G4 labels."""
    tokens = _canonical_tokens(holdings or [], activity_events or [])
    g1 = _g1(tokens)
    g2 = _g2(tokens, capital_base_usd)
    g3 = _g3(tokens)
    g4 = _g4(tokens)
    group_results = {'g1': g1, 'g2': g2, 'g3': g3, 'g4': g4}
    labels = qualified_group_labels(group_results)
    return {
        'schema': 'wallet_group_labels_v4_multi_group',
        'labels': labels,
        'group_count': len(labels),
        'qualified_labels': list(labels),
        'qualified_group_count': len(labels),
        'g1': g1,
        'g2': g2,
        'g3': g3,
        'g4': g4,
        'token_observations': [
            {key: value for key, value in record.items() if key not in {'events', 'all_events', 'buy_events', 'sell_events', '_realized_pnl_usd'} }
            for record in tokens.values()
        ],
    }
