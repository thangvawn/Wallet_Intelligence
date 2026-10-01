"""Resume-safe collection of rank inputs missing from local SQLite snapshots."""
import re
import time
from typing import Callable, Dict, Iterable, Optional, Sequence

from app.portfolio_pnl.gmgn_client import GmgnPnlClient
from app.portfolio_pnl.service import crawl_wallet
from app.portfolio_pnl.store import PnlStore
from app.wallet_identity import load_message_wallets


DEFAULT_COMPONENTS = ('stats', 'intraday', 'empty_holdings', 'activity')


def _with_provider_backoff(call: Callable, attempts: int = 4):
    """Honor GMGN's reported cooldown and retry without weakening atomic writes."""
    for attempt in range(attempts):
        try:
            return call()
        except Exception as exc:
            message = str(exc)
            if 'giới hạn tốc độ' not in message and 'HTTP 429' not in message:
                raise
            if attempt + 1 >= attempts:
                raise
            match = re.search(r'(?:khoảng|remaining)\s+(\d+)s', message)
            remaining = int(match.group(1)) if match else 10
            # Sleep in short slices so this worker remains interruptible.
            deadline = time.monotonic() + min(120, remaining + 2)
            while time.monotonic() < deadline:
                time.sleep(min(10.0, deadline - time.monotonic()))


def intraday_from_activity(
    wallet: str, name: str, activity: dict, fetched_at: Optional[int] = None,
) -> dict:
    """Reuse the full activity snapshot instead of downloading sell events twice."""
    events = [
        {
            key: event.get(key)
            for key in (
                'timestamp', 'event_type', 'tx_hash', 'token_address',
                'token_symbol', 'amount_usd', 'buy_cost_usd', 'gas_usd',
            )
        }
        for event in (activity.get('events') or [])
        if str(event.get('event_type') or '').lower() in ('sell', 'transferout')
    ]
    return {
        'schema': 'gmgn_intraday_activity_v1',
        'wallet_address': wallet,
        'wallet_name': name,
        # The retained activity JSON may intentionally keep its old payload
        # timestamp when a poll has no new events. Use the current poll time
        # for component freshness while preserving the semantic event data.
        'fetched_at': int(fetched_at or time.time()),
        'days': int(activity.get('days') or 365),
        'status': 'complete',
        'source': 'derived_from_wallet_activity_snapshot',
        'events': events,
    }


def _save_stats_periods(
    store: PnlStore,
    client: GmgnPnlClient,
    wallet: str,
    periods: Sequence[str],
) -> int:
    if not periods:
        return 0
    run_id = store.start_run(wallet, 'gmgn_stats_enrichment', {'periods': list(periods)})
    collected = 0
    try:
        pages = []
        for period in periods:
            payload = _with_provider_backoff(
                lambda period=period: client.fetch_wallet_stats(wallet, period)
            )
            if not payload:
                raise RuntimeError(f'GMGN stats {period} trả payload rỗng.')
            pages.append({
                'params': {'period': period},
                'payload': payload,
                'count': 1,
            })
            collected += 1
        store.append_pages(
            run_id, wallet, 'gmgn_stats_enrichment', 'portfolio/stats', pages,
        )
        store.finish_run(run_id, 'complete', rows=collected)
        store.save_component_watermark(
            wallet, 'stats',
            coverage_days=max(
                (int(str(period).rstrip('d')) for period in periods
                 if str(period).rstrip('d').isdigit()),
                default=None,
            ),
            payload_hash=PnlStore.payload_hash(pages),
            status='complete',
            updated_at=int(time.time()),
        )
        return collected
    except Exception as exc:
        store.finish_run(run_id, 'failed', error=str(exc), rows=collected)
        raise


def collect_missing_data(
    wallets: Optional[Iterable[dict]] = None,
    store: Optional[PnlStore] = None,
    client: Optional[GmgnPnlClient] = None,
    components: Sequence[str] = DEFAULT_COMPONENTS,
    activity_days: int = 365,
    max_pages: int = 1000,
    delay: float = 0.75,
    force: bool = False,
    progress: Optional[Callable[[dict], None]] = None,
    crawler: Callable = crawl_wallet,
) -> Dict:
    """Collect only absent/insufficient inputs and preserve complete snapshots.

    Increasing ``activity_days`` explicitly extends history. With the default
    365 days, an existing complete 365-day snapshot is never downloaded again.
    """
    selected = tuple(dict.fromkeys(components))
    unknown = sorted(set(selected) - set(DEFAULT_COMPONENTS))
    if unknown:
        raise ValueError(f'Unknown enrichment components: {", ".join(unknown)}')
    if not 1 <= activity_days <= 3650:
        raise ValueError('activity_days must be between 1 and 3650')
    if not 1 <= max_pages <= 1000:
        raise ValueError('max_pages must be between 1 and 1000')

    identities = list(wallets if wallets is not None else load_message_wallets())
    pnl_store = store or PnlStore()
    gmgn = client or GmgnPnlClient()
    counts = {
        'stats_periods_collected': 0,
        'intraday_derived': 0,
        'intraday_collected': 0,
        'activity_extended': 0,
        'empty_holdings_refilled': 0,
        'skipped': 0,
    }
    errors = []

    for index, identity in enumerate(identities, start=1):
        wallet = str(identity.get('address') or '').strip()
        name = str(identity.get('name') or '')
        wallet_changed = False
        if not wallet:
            continue

        if 'stats' in selected:
            try:
                existing_periods = pnl_store.load_stats_period_snapshots(wallet)
                missing_periods = [
                    period for period in ('7d', '30d')
                    if force or period not in existing_periods
                ]
                if missing_periods:
                    counts['stats_periods_collected'] += _save_stats_periods(
                        pnl_store, gmgn, wallet, missing_periods,
                    )
                    wallet_changed = True
            except Exception as exc:
                errors.append({'wallet': wallet, 'component': 'stats', 'error': str(exc)})

        activity = pnl_store.load_activity_snapshot(wallet)
        if 'activity' in selected:
            current_days = int((activity or {}).get('days') or 0)
            activity_complete = (activity or {}).get('status') == 'complete'
            if force or not activity_complete or current_days < activity_days:
                try:
                    events, pages = _with_provider_backoff(
                        lambda: gmgn.fetch_wallet_activity(
                            wallet, days=activity_days, max_pages=max_pages,
                        )
                    )
                    now = int(time.time())
                    activity = {
                        'schema': 'gmgn_wallet_activity_v1',
                        'wallet_address': wallet,
                        'wallet_name': name,
                        'fetched_at': now,
                        'start_at': now - activity_days * 86400,
                        'end_at': now,
                        'days': activity_days,
                        'status': 'complete',
                        'event_types': ['buy', 'sell', 'transferin', 'transferout', 'add', 'remove'],
                        'pages': len(pages),
                        'events': events,
                    }
                    pnl_store.save_activity_snapshot(wallet, activity)
                    counts['activity_extended'] += 1
                    wallet_changed = True
                except Exception as exc:
                    errors.append({'wallet': wallet, 'component': 'activity', 'error': str(exc)})

        if 'intraday' in selected:
            intraday = pnl_store.load_intraday_snapshot(wallet)
            needs_intraday = force or not intraday or intraday.get('status') != 'complete'
            if needs_intraday:
                try:
                    if activity and activity.get('status') == 'complete':
                        payload = intraday_from_activity(wallet, name, activity)
                        pnl_store.save_intraday_snapshot(wallet, payload)
                        counts['intraday_derived'] += 1
                    else:
                        events, pages = _with_provider_backoff(
                            lambda: gmgn.fetch_intraday_pnl_events(
                                wallet, days=min(activity_days, 365), max_pages=max_pages,
                            )
                        )
                        payload = {
                            'schema': 'gmgn_intraday_activity_v1',
                            'wallet_address': wallet,
                            'wallet_name': name,
                            'fetched_at': int(time.time()),
                            'days': min(activity_days, 365),
                            'status': 'complete',
                            'source': 'gmgn_portfolio_activity',
                            'pages': len(pages),
                            'events': events,
                        }
                        pnl_store.save_intraday_snapshot(wallet, payload)
                        counts['intraday_collected'] += 1
                    wallet_changed = True
                except Exception as exc:
                    errors.append({'wallet': wallet, 'component': 'intraday', 'error': str(exc)})

        if 'empty_holdings' in selected:
            dashboard = pnl_store.load_dashboard_snapshot(wallet) or {}
            complete = (dashboard.get('collection') or {}).get('complete') is True
            if (force or not complete) and not (dashboard.get('rows') or []):
                try:
                    def crawl_once():
                        crawl_result = crawler(
                            pnl_store, gmgn, wallet, name,
                            max_pages=max_pages, hide_closed=False,
                        )
                        if crawl_result.get('status') != 'ok':
                            raise RuntimeError(
                                crawl_result.get('error') or 'Holdings refill failed'
                            )
                        return crawl_result

                    _with_provider_backoff(crawl_once)
                    counts['empty_holdings_refilled'] += 1
                    wallet_changed = True
                except Exception as exc:
                    errors.append({'wallet': wallet, 'component': 'empty_holdings', 'error': str(exc)})

        if not wallet_changed:
            counts['skipped'] += 1
        if progress:
            progress({
                'processed': index,
                'total': len(identities),
                **counts,
                'failed': len(errors),
            })
        if wallet_changed and delay > 0:
            time.sleep(delay)

    return {
        'status': 'complete' if not errors else 'partial',
        'total': len(identities),
        **counts,
        'failed': len(errors),
        'errors': errors,
        'activity_days_target': activity_days,
    }
