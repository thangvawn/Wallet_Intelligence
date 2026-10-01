"""Independently refresh wallet inputs so partial provider failures are resumable."""
import time
from typing import Callable, Dict, Optional

from app.portfolio_pnl.enrichment import _save_stats_periods, intraday_from_activity
from app.portfolio_pnl.gmgn_client import GmgnPnlClient
from app.portfolio_pnl.normalize import normalize_holdings_table, normalize_period_profit, normalize_wallet_summary
from app.portfolio_pnl.single_wallet_pipeline import refresh_wallet_activity
from app.portfolio_pnl.store import PnlStore

COMPONENTS = ('holdings', 'profits', 'calendar', 'stats', 'activity', 'intraday')


def _save_dashboard_component(
    store: PnlStore,
    wallet: str,
    component: str,
    changes: dict,
    wallet_name: str = '',
) -> bool:
    """Merge one component without clobbering data written by another worker.

    ``dashboard_snapshots.fetched_at`` is retained for backward compatibility,
    while ``component_fetched_at`` gives each input its own freshness clock.
    The write is skipped when the provider returned the same semantic payload;
    the component state is still advanced by the caller.
    """
    current = store.load_dashboard_snapshot(wallet) or {}
    candidate = {**current, **changes, 'wallet_address': wallet}
    if wallet_name:
        candidate['wallet_name'] = wallet_name
    component_times = dict(current.get('component_fetched_at') or {})
    now = int(time.time())
    component_times[component] = now
    candidate['component_fetched_at'] = component_times

    candidate_semantic = {
        key: value for key, value in candidate.items()
        if key not in ('fetched_at', 'component_fetched_at')
    }
    current_semantic = {
        key: value for key, value in current.items()
        if key not in ('fetched_at', 'component_fetched_at')
    }

    if candidate_semantic == current_semantic:
        # The provider was contacted successfully even though the semantic
        # payload did not change. Advance the component watermark without
        # rewriting the larger dashboard blob.
        store.save_component_watermark(
            wallet, component,
            payload_hash=PnlStore.payload_hash(changes),
            status='complete',
            updated_at=now,
        )
        return False
    candidate['fetched_at'] = now
    store.save_dashboard_snapshot(wallet, candidate)
    store.save_component_watermark(
        wallet, component,
        payload_hash=PnlStore.payload_hash(changes),
        status='complete',
        updated_at=now,
    )
    return True


def _fresh(timestamp, now: int, ttl_seconds: int) -> bool:
    try:
        age = now - int(timestamp or 0)
    except (TypeError, ValueError):
        return False
    return -300 <= age <= ttl_seconds


def components_needing_refresh(
    store: PnlStore, wallet: str, ttl_seconds: int = 48 * 3600,
    activity_days: int = 1095, now: Optional[int] = None,
    component_ttls: Optional[Dict[str, int]] = None,
    force: bool = False,
) -> list[str]:
    """Inspect both persisted component state and actual database snapshots."""
    if force:
        # An explicit user refresh is an intentional TTL override.  Keep this
        # decision at the component planner so both the initial and final
        # completeness checks use the same semantics.
        return list(COMPONENTS)
    timestamp = int(now or time.time())
    dashboard = store.load_dashboard_snapshot(wallet) or {}
    activity = store.load_activity_snapshot(wallet) or {}
    intraday = store.load_intraday_snapshot(wallet) or {}
    stats = store.load_stats_period_snapshots(wallet)
    states = store.load_refresh_components(wallet)
    watermarks = store.load_component_watermarks(wallet)

    def ttl_for(component: str) -> int:
        value = (component_ttls or {}).get(component, ttl_seconds)
        return max(1, int(value))

    def state_fresh(component: str) -> bool:
        state = states.get(component) or {}
        if state:
            return state.get('status') == 'complete' and _fresh(
                state.get('last_success_at'), timestamp, ttl_for(component),
            )
        watermark = watermarks.get(component) or {}
        if watermark.get('status') == 'complete':
            return _fresh(watermark.get('updated_at'), timestamp, ttl_for(component))
        return False

    component_times = dashboard.get('component_fetched_at') or {}

    def valid(component: str, structure_valid: bool, source_timestamp) -> bool:
        # Once a component has its own state, do not let another component's
        # dashboard timestamp accidentally make a failed component look fresh.
        freshness = (
            state_fresh(component) if component in states
            else _fresh(
                (watermarks.get(component) or {}).get('updated_at')
                or component_times.get(component) or source_timestamp,
                timestamp,
                ttl_for(component),
            )
        )
        return bool(structure_valid and freshness)

    collection_complete = (dashboard.get('collection') or {}).get('complete') is True
    checks = {
        'holdings': valid(
            'holdings', dashboard.get('rows') and collection_complete,
            dashboard.get('fetched_at'),
        ),
        'profits': valid(
            'profits', dashboard.get('summary'), dashboard.get('fetched_at'),
        ),
        'calendar': valid(
            'calendar', dashboard.get('daily_calendar'), dashboard.get('fetched_at'),
        ),
        'stats': valid(
            'stats', all(period in stats for period in ('7d', '30d')),
            min(
                ((stats.get(period) or {}).get('_snapshot_fetched_at') or 0)
                for period in ('7d', '30d')
            ),
        ),
        'activity': valid('activity', bool(
            activity.get('status') == 'complete'
            and (int(activity.get('days') or 0) >= activity_days or activity.get('history_exhausted') is True)
        ), activity.get('fetched_at')),
        'intraday': valid(
            'intraday', intraday.get('status') == 'complete',
            intraday.get('fetched_at'),
        ),
    }
    return [component for component in COMPONENTS if not checks[component]]


def refresh_wallet_components(
    wallet: str, wallet_name: str = '', holdings_max_pages: int = 200,
    activity_days: int = 1095, activity_max_pages: int = 1000,
    store: Optional[PnlStore] = None, client: Optional[GmgnPnlClient] = None,
    ttl_seconds: int = 48 * 3600,
    component_ttls: Optional[Dict[str, int]] = None,
    force: bool = False,
) -> dict:
    """Refresh stale components and commit each success independently.

    ``force=True`` is reserved for an explicit user request and intentionally
    bypasses component TTLs; the periodic worker leaves it false so fresh
    wallets are never sent to the provider.
    """
    pnl_store = store or PnlStore()
    now = int(time.time())
    needed = components_needing_refresh(
        pnl_store, wallet, ttl_seconds=ttl_seconds,
        activity_days=activity_days, now=now,
        component_ttls=component_ttls,
        force=force,
    )
    completed = []
    failed = []
    changed_components = []

    # Do not construct the provider client for a no-op refresh or an
    # intraday-only rebuild.  This keeps DB reads available when credentials or
    # the CLI are temporarily unavailable and removes needless process setup
    # from the realtime scheduler.
    gmgn = client

    def provider():
        nonlocal gmgn
        if gmgn is None:
            gmgn = GmgnPnlClient()
        return gmgn

    def execute(component: str, operation: Callable[[], None]):
        pnl_store.mark_refresh_component(wallet, component, 'running')
        try:
            operation()
            pnl_store.mark_refresh_component(wallet, component, 'complete')
            completed.append(component)
        except Exception as exc:
            pnl_store.mark_refresh_component(wallet, component, 'failed', str(exc))
            failed.append({'component': component, 'error': str(exc)})

    if 'holdings' in needed:
        def holdings():
            rows, pages = provider().fetch_holdings(wallet, max_pages=holdings_max_pages)
            run_id = pnl_store.start_run(wallet, 'gmgn_holdings_refresh', {'max_pages': holdings_max_pages})
            try:
                pnl_store.append_pages(
                    run_id, wallet, 'gmgn_holdings_refresh',
                    'portfolio/holdings', pages,
                )
                table = normalize_holdings_table(rows, wallet, wallet_name)
                pnl_store.save_token_rows(wallet, run_id, table)
                changed = _save_dashboard_component(
                    pnl_store, wallet, 'holdings', {
                        'schema': 'gmgn_recent_pnl_v1',
                        'percentage_unit': 'percent',
                        'rows': table,
                        'collection': {
                            'complete': True,
                            'pages_fetched': len(pages),
                            'rows_fetched': len(rows),
                            'unique_token_rows': len(table),
                            'include_open': True,
                            'include_closed': True,
                            'include_airdrops': True,
                            'include_abnormal': True,
                        },
                    }, wallet_name,
                )
                if changed:
                    changed_components.append('holdings')
                pnl_store.finish_run(run_id, 'complete', rows=len(table))
            except Exception as exc:
                pnl_store.finish_run(run_id, 'failed', error=str(exc))
                raise
        execute('holdings', holdings)

    if 'profits' in needed:
        def profits():
            # ``all`` feeds the canonical summary; 7d/30d feed the displayed
            # period cards.  Keep a cached 1d value instead of paying for it on
            # every realtime pass (the initial full crawl still collects it).
            periods_to_fetch = ('all', '7d', '30d')
            run_id = pnl_store.start_run(wallet, 'gmgn_profits_refresh', {'periods': list(periods_to_fetch)})
            raw = {}
            try:
                for period in periods_to_fetch:
                    raw[period] = provider().fetch_wallet_profits(wallet, period)
                pnl_store.append_pages(
                    run_id, wallet, 'gmgn_profits_refresh', 'portfolio/profits',
                    [
                        {
                            'params': {'period': period},
                            'payload': {'data': {'list': [raw[period]]}},
                            'count': 1,
                        }
                        for period in periods_to_fetch
                    ],
                )
                current = pnl_store.load_dashboard_snapshot(wallet) or {}
                periods = dict(current.get('periods') or {})
                periods.update({period: normalize_period_profit(value, period, wallet) for period, value in raw.items()})
                summary = normalize_wallet_summary(raw['all'], wallet, wallet_name)
                pnl_store.save_wallet_summary(wallet, run_id, {**summary, 'fetched_at': int(time.time())})
                changed = _save_dashboard_component(
                    pnl_store, wallet, 'profits',
                    {'summary': summary, 'periods': periods}, wallet_name,
                )
                if changed:
                    changed_components.append('profits')
                pnl_store.finish_run(run_id, 'complete', rows=len(raw))
            except Exception as exc:
                pnl_store.finish_run(run_id, 'failed', error=str(exc), rows=len(raw))
                raise
        execute('profits', profits)

    if 'calendar' in needed:
        def calendar():
            run_id = pnl_store.start_run(wallet, 'gmgn_calendar_refresh', {'days': 365, 'timezone': 'UTC'})
            try:
                rows, raw = provider().fetch_daily_profit_calendar(wallet, days=365)
                pnl_store.append_page(
                    run_id, wallet, 'gmgn_calendar_refresh', 'pf/daily_profits',
                    {'days': 365, 'timezone': 'UTC'}, raw, len(rows),
                )
                current = pnl_store.load_dashboard_snapshot(wallet) or {}
                existing_calendar = {
                    row.get('date'): row
                    for row in (current.get('daily_calendar') or [])
                    if row.get('date')
                }
                existing_calendar.update({row['date']: row for row in rows if row.get('date')})
                changed = _save_dashboard_component(
                    pnl_store, wallet, 'calendar',
                    {'daily_calendar': [existing_calendar[key] for key in sorted(existing_calendar)]},
                    wallet_name,
                )
                if changed:
                    changed_components.append('calendar')
                pnl_store.finish_run(run_id, 'complete', rows=len(rows))
            except Exception as exc:
                pnl_store.finish_run(run_id, 'failed', error=str(exc))
                raise
        execute('calendar', calendar)

    if 'stats' in needed:
        def stats():
            before = (pnl_store.load_component_watermarks(wallet).get('stats') or {}).get('payload_hash')
            _save_stats_periods(pnl_store, provider(), wallet, ('7d', '30d'))
            after = (pnl_store.load_component_watermarks(wallet).get('stats') or {}).get('payload_hash')
            if before != after:
                changed_components.append('stats')
        execute('stats', stats)

    if 'activity' in needed:
        def activity_refresh():
            result = refresh_wallet_activity(
                pnl_store, provider(), wallet, wallet_name, days=activity_days,
                max_pages=activity_max_pages,
            )
            if result.get('_data_changed'):
                changed_components.append('activity')
        execute('activity', activity_refresh)

    # Intraday has no provider dependency, but it must not be marked fresh when
    # the activity refresh it depends on just failed.
    if 'activity' in needed and 'activity' not in completed:
        error = 'Bị hoãn vì activity chưa cập nhật thành công.'
        pnl_store.mark_refresh_component(wallet, 'intraday', 'failed', error)
        failed.append({'component': 'intraday', 'error': error})
    elif 'intraday' in needed or 'activity' in completed:
        def intraday():
            activity = pnl_store.load_activity_snapshot(wallet) or {}
            if activity.get('status') != 'complete':
                raise RuntimeError('Activity chưa hoàn chỉnh để dựng intraday.')
            changed = pnl_store.save_intraday_snapshot(
                wallet,
                intraday_from_activity(
                    wallet, wallet_name, activity, fetched_at=int(time.time()),
                ),
            )
            if changed:
                changed_components.append('intraday')
        execute('intraday', intraday)

    remaining = components_needing_refresh(
        pnl_store, wallet, ttl_seconds=ttl_seconds, activity_days=activity_days,
        component_ttls=component_ttls,
        force=False,
    )
    return {
        'status': 'complete' if not remaining else 'partial',
        'wallet_address': wallet,
        'requested_components': needed,
        'completed_components': completed,
        'failed_components': failed,
        'changed_components': list(dict.fromkeys(changed_components)),
        'no_op': not needed,
        'remaining_components': remaining,
        'updated_at': int(time.time()),
    }
