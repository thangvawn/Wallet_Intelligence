"""Per-wallet hour/session PnL analytics reconciled to GMGN daily totals."""
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List

from app.runtime import BASE, load_settings
from app.portfolio_pnl.api import load_wallet_pnl
from app.portfolio_pnl.gmgn_client import GmgnPnlClient
from app.portfolio_pnl.store import PnlStore


CACHE_DIR = BASE / 'data' / 'portfolio_pnl' / 'intraday'
WEEKDAYS = ['T2', 'T3', 'T4', 'T5', 'T6', 'T7', 'CN']


def _bucket(label: str) -> dict:
    return {
        'label': label,
        'profit_usd': 0.0,
        'loss_usd': 0.0,
        'net_pnl_usd': 0.0,
        '_tx_hashes': set(),
        '_active_days': set(),
        '_loss_days': set(),
    }


def _finish_bucket(row: dict) -> dict:
    profit = row['profit_usd']
    loss = row['loss_usd']
    return {
        'label': row['label'],
        'profit_usd': round(profit, 2),
        'loss_usd': round(loss, 2),
        'net_pnl_usd': round(profit - loss, 2),
        'tx_count': len(row['_tx_hashes']),
        'active_days': len(row['_active_days']),
        'loss_days': len(row['_loss_days']),
        'loss_day_rate_pct': round(
            100 * len(row['_loss_days']) / len(row['_active_days']),
            1,
        ) if row['_active_days'] else 0.0,
        'profit_factor': round(profit / loss, 2) if loss > 0 else None,
    }


def _session_for_local_hour(hour: int) -> str:
    if 7 <= hour < 15:
        return 'Phiên Á'
    if 15 <= hour < 23:
        return 'Phiên Âu'
    return 'Phiên Mỹ'


def _allocate_amount(amount: float, events: List[dict]) -> List[dict]:
    if amount <= 0 or not events:
        return []
    weights = [max(0.0, float(event.get('amount_usd') or 0)) for event in events]
    total_weight = sum(weights)
    if total_weight <= 0:
        weights = [1.0] * len(events)
        total_weight = float(len(events))
    allocations = []
    remaining = amount
    for index, (event, weight) in enumerate(zip(events, weights)):
        value = remaining if index == len(events) - 1 else amount * weight / total_weight
        remaining -= value
        allocations.append({**event, 'allocated_usd': value})
    return allocations


def build_intraday_report(
    wallet: str,
    events: Iterable[dict],
    daily_calendar: Iterable[dict],
    tz_offset_hours: int = 7,
) -> dict:
    """Allocate official daily gross profit/loss to timestamped GMGN events."""
    tz_offset_hours = max(-12, min(14, int(tz_offset_hours)))
    event_days: Dict[str, List[dict]] = defaultdict(list)
    for event in events:
        timestamp = int(event.get('timestamp') or 0)
        if timestamp:
            day = datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
            event_days[day].append(event)

    hourly = {hour: _bucket(f'{hour:02d}:00') for hour in range(24)}
    sessions = {
        'Phiên Á': _bucket('Phiên Á · 07:00–15:00 UTC+7'),
        'Phiên Âu': _bucket('Phiên Âu · 15:00–23:00 UTC+7'),
        'Phiên Mỹ': _bucket('Phiên Mỹ · 23:00–07:00 UTC+7'),
    }
    weekdays = {index: _bucket(label) for index, label in enumerate(WEEKDAYS)}
    official_profit = official_loss = unallocated_profit = unallocated_loss = 0.0
    observed_profit = observed_loss = 0.0
    matched_days = trading_days = 0

    for day in daily_calendar:
        gross_profit = max(0.0, float(day.get('profit_usd') or 0))
        gross_loss = max(0.0, float(day.get('loss_usd') or 0))
        if gross_profit <= 0 and gross_loss <= 0:
            continue
        trading_days += 1
        official_profit += gross_profit
        official_loss += gross_loss
        candidates = event_days.get(str(day.get('date')), [])
        if not candidates:
            unallocated_profit += gross_profit
            unallocated_loss += gross_loss
            continue

        exact_positive = []
        exact_negative = []
        missing_basis = []
        for event in candidates:
            basis = event.get('buy_cost_usd')
            event_type = event.get('event_type')
            if event_type == 'sell' and basis is not None:
                pnl = (
                    float(event.get('amount_usd') or 0)
                    - float(basis)
                    - float(event.get('gas_usd') or 0)
                )
                (exact_positive if pnl >= 0 else exact_negative).append({
                    **event,
                    'amount_usd': abs(pnl),
                })
            else:
                missing_basis.append(event)
        observed_profit += min(
            gross_profit,
            sum(float(event.get('amount_usd') or 0) for event in exact_positive),
        )
        observed_loss += min(
            gross_loss,
            sum(float(event.get('amount_usd') or 0) for event in exact_negative),
        )

        positive_events = exact_positive or missing_basis or candidates
        negative_events = exact_negative or missing_basis or candidates
        allocations = [
            *({**row, 'sign': 1} for row in _allocate_amount(gross_profit, positive_events)),
            *({**row, 'sign': -1} for row in _allocate_amount(gross_loss, negative_events)),
        ]
        if allocations:
            matched_days += 1
        else:
            unallocated_profit += gross_profit
            unallocated_loss += gross_loss

        for allocation in allocations:
            timestamp = int(allocation['timestamp'])
            utc_dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            local_dt = utc_dt + timedelta(hours=tz_offset_hours)
            amount = float(allocation['allocated_usd'])
            sign = int(allocation['sign'])
            local_day = local_dt.date().isoformat()
            session_key = _session_for_local_hour(local_dt.hour)
            targets = [
                hourly[local_dt.hour],
                sessions[session_key],
                weekdays[local_dt.weekday()],
            ]
            for target in targets:
                if sign > 0:
                    target['profit_usd'] += amount
                else:
                    target['loss_usd'] += amount
                if allocation.get('tx_hash'):
                    target['_tx_hashes'].add(allocation['tx_hash'])
                target['_active_days'].add(local_day)
                if sign < 0:
                    target['_loss_days'].add(local_day)

    hourly_rows = [_finish_bucket(hourly[hour]) for hour in range(24)]
    session_rows = [_finish_bucket(row) for row in sessions.values()]
    weekday_rows = [_finish_bucket(weekdays[index]) for index in range(7)]
    official_net = official_profit - official_loss
    attributed_profit = official_profit - unallocated_profit
    attributed_loss = official_loss - unallocated_loss
    attributed_gross = attributed_profit + attributed_loss
    observed_gross = observed_profit + observed_loss
    exact_attribution_pct = (
        100 * observed_gross / attributed_gross
        if attributed_gross else 0.0
    )

    return {
        'schema': 'wallet_intraday_pnl_v1',
        'wallet_address': wallet,
        'timezone': f'UTC{tz_offset_hours:+d}',
        'source': {
            'daily_totals': 'gmgn_web_daily_profits',
            'timestamps': 'gmgn_portfolio_activity',
            'method': 'Official daily Profit/Loss allocated to sell/transfer-out timestamps',
        },
        'summary': {
            'profit_usd': round(official_profit, 2),
            'loss_usd': round(official_loss, 2),
            'net_pnl_usd': round(official_net, 2),
            'attributed_profit_usd': round(attributed_profit, 2),
            'attributed_loss_usd': round(attributed_loss, 2),
            'observed_sell_profit_usd': round(observed_profit, 2),
            'observed_sell_loss_usd': round(observed_loss, 2),
            'allocated_profit_usd': round(max(0.0, attributed_profit - observed_profit), 2),
            'allocated_loss_usd': round(max(0.0, attributed_loss - observed_loss), 2),
            'exact_attribution_pct': round(exact_attribution_pct, 1),
            'attribution_quality': (
                'OBSERVED_DOMINANT'
                if exact_attribution_pct >= 80
                else 'MIXED'
                if exact_attribution_pct >= 30
                else 'ALLOCATED_DOMINANT'
            ),
            'unallocated_profit_usd': round(unallocated_profit, 2),
            'unallocated_loss_usd': round(unallocated_loss, 2),
            'trading_days': trading_days,
            'matched_days': matched_days,
            'coverage_pct': round(100 * matched_days / trading_days, 1) if trading_days else 0.0,
        },
        'hourly': hourly_rows,
        'sessions': session_rows,
        'weekdays': weekday_rows,
        'loss_timing': {
            'worst_hour': max(hourly_rows, key=lambda row: row['loss_usd'], default=None),
            'most_frequent_loss_hour': max(
                hourly_rows,
                key=lambda row: (row['loss_days'], row['loss_usd']),
                default=None,
            ),
            'worst_session': max(session_rows, key=lambda row: row['loss_usd'], default=None),
            'worst_weekday': max(weekday_rows, key=lambda row: row['loss_usd'], default=None),
        },
    }


def load_intraday_report(
    wallet: str,
    days: int = 365,
    tz_offset_hours: int = 7,
    refresh: bool = False,
) -> dict:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f'{wallet}.json'
    cached = json.loads(cache_path.read_text(encoding='utf-8')) if cache_path.exists() else None

    refresh_error = None
    if refresh:
        try:
            load_settings()
            client = GmgnPnlClient()
            events, _pages = client.fetch_intraday_pnl_events(wallet, days=days)
            cached = {
                'wallet_address': wallet,
                'fetched_at': int(time.time()),
                'days': days,
                'events': events,
            }
            cache_path.write_text(
                json.dumps(cached, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
            PnlStore().save_intraday_snapshot(wallet, cached)
        except Exception as exc:
            refresh_error = str(exc)
            if not cached:
                raise

    payload = load_wallet_pnl(wallet)
    if not payload:
        raise ValueError('Không có PnL Calendar cho wallet này.')
    report = build_intraday_report(
        wallet,
        (cached or {}).get('events') or [],
        payload.get('daily_calendar') or [],
        tz_offset_hours=tz_offset_hours,
    )
    report['fetched_at'] = (cached or {}).get('fetched_at')
    report['intraday_data_status'] = 'AVAILABLE' if cached else 'NOT_COLLECTED'
    if refresh_error:
        report['refresh_error'] = refresh_error
        report['served_from_cache'] = True
    return report
