"""Crawl GMGN Recent PnL into the canonical SQLite store."""
import time
from typing import Any, Dict, List, Optional

from app.runtime import load_settings
from app.wallet_identity import load_message_wallets
from app.portfolio_pnl.gmgn_client import GmgnPnlClient
from app.portfolio_pnl.normalize import (
    normalize_holdings_table,
    normalize_period_profit,
    normalize_wallet_summary,
)
from app.portfolio_pnl.store import PnlStore


def crawl_wallet(
    store: PnlStore,
    client: GmgnPnlClient,
    wallet_address: str,
    wallet_name: str = '',
    max_pages: int = 200,
    hide_closed: bool = False,
) -> Dict[str, Any]:
    store.add_wallet(wallet_address, wallet_name)
    run_id = store.start_run(wallet_address, 'gmgn', {
        'endpoint': 'portfolio/holdings',
        'max_pages': max_pages,
        'hide_closed': hide_closed,
        'hide_airdrop': False,
        'hide_abnormal': False,
        'order_by': 'total_profit',
    })
    try:
        raw_rows, pages = client.fetch_holdings(
            wallet_address,
            max_pages=max_pages,
            hide_closed=hide_closed,
            hide_airdrop=False,
            hide_abnormal=False,
        )
        store.append_pages(
            run_id, wallet_address, 'gmgn', 'portfolio/holdings', pages,
        )
        period_rows = {}
        period_raw = {}
        for period in ('1d', '7d', '30d', 'all'):
            raw = client.fetch_wallet_profits(wallet_address, period=period)
            period_raw[period] = raw
            period_rows[period] = normalize_period_profit(raw, period, wallet_address)
            store.append_page(
                run_id,
                wallet_address,
                'gmgn',
                'portfolio/profits',
                {'period': period},
                {'data': {'list': [raw]}},
                1 if raw else 0,
            )
        profits_raw = period_raw['all']
        period_stats = {}
        for period in ('7d', '30d'):
            stats_raw = client.fetch_wallet_stats(wallet_address, period)
            period_stats[period] = stats_raw
            store.append_page(
                run_id,
                wallet_address,
                'gmgn',
                'portfolio/stats',
                {'period': period},
                stats_raw,
                1 if stats_raw else 0,
            )

        daily_calendar, calendar_payload = client.fetch_daily_profit_calendar(
            wallet_address,
            days=365,
        )
        store.append_page(
            run_id,
            wallet_address,
            'gmgn',
            'pf/daily_profits',
            {'days': 365, 'timezone': 'UTC'},
            calendar_payload,
            len(daily_calendar),
        )
        table = normalize_holdings_table(raw_rows, wallet_address, wallet_name)
        summary = normalize_wallet_summary(profits_raw, wallet_address, wallet_name)
        store.save_token_rows(wallet_address, run_id, table)
        store.save_wallet_summary(wallet_address, run_id, {
            **summary,
            'token_count': len(table),
            'fetched_at': int(time.time()),
        })
        store.finish_run(run_id, 'complete', rows=len(table))
        collection = {
            'complete': True,
            'pages_fetched': len(pages),
            'rows_fetched': len(raw_rows),
            'unique_token_rows': len(table),
            'include_open': True,
            'include_closed': not hide_closed,
            'include_airdrops': True,
            'include_abnormal': True,
        }
        save_wallet_snapshot(
            store,
            wallet_address,
            table,
            summary,
            collection,
            period_rows,
            period_stats,
            daily_calendar,
        )
        return {
            'wallet': wallet_address,
            'name': wallet_name,
            'status': 'ok',
            'token_rows': len(table),
            'summary': summary,
            'collection': collection,
            'period_stats': period_stats,
            'daily_calendar': daily_calendar,
        }
    except Exception as exc:
        store.finish_run(run_id, 'failed', error=str(exc))
        return {
            'wallet': wallet_address,
            'name': wallet_name,
            'status': 'failed',
            'error': str(exc),
        }


def save_wallet_snapshot(
    store: PnlStore,
    wallet_address: str,
    table: List[dict],
    summary: dict,
    collection: Optional[dict] = None,
    periods: Optional[dict] = None,
    period_stats: Optional[dict] = None,
    daily_calendar: Optional[List[dict]] = None,
):
    fetched_at = int(time.time())
    payload = {
        'schema': 'gmgn_recent_pnl_v1',
        'percentage_unit': 'percent',
        'wallet_address': wallet_address,
        'wallet_name': summary.get('wallet_name', ''),
        'fetched_at': fetched_at,
        'component_fetched_at': {
            'holdings': fetched_at,
            'profits': fetched_at,
            'calendar': fetched_at,
        },
        'summary': summary,
        'collection': collection or {},
        'periods': periods or {},
        'period_stats': period_stats or {},
        'daily_calendar': daily_calendar or [],
        'rows': table,
    }
    store.save_dashboard_snapshot(wallet_address, payload)
    # Seed the per-component clocks for the initial full crawl. Later partial
    # refreshes advance only the component they actually fetched.
    for component, value in (
        ('holdings', table),
        ('profits', {'summary': summary, 'periods': periods or {}}),
        ('calendar', daily_calendar or []),
    ):
        store.save_component_watermark(
            wallet_address,
            component,
            payload_hash=PnlStore.payload_hash(value),
            status='complete',
            updated_at=fetched_at,
        )
        store.mark_refresh_component(
            wallet_address, component, 'complete', timestamp=fetched_at,
        )
    return payload


def build_master_index(store: PnlStore, wallets: List[dict]) -> dict:
    entries = []
    for w in wallets:
        addr = w['address']
        payload = store.load_dashboard_snapshot(addr) or {}
        summary = payload.get('summary') or store.latest_summary(addr)
        rows = payload.get('rows') or store.latest_token_rows(addr)
        entries.append({
            'address': addr,
            'name': w.get('name', ''),
            'emoji': w.get('emoji', ''),
            'has_pnl_data': bool(rows),
            'token_rows': len(rows),
            'summary': summary,
        })
    index = {
        'schema': 'gmgn_recent_pnl_index_v1',
        'generated_at': int(time.time()),
        'total_wallets': len(entries),
        'with_data': sum(1 for e in entries if e['has_pnl_data']),
        'wallets': entries,
    }
    return index


def crawl_all(
    max_pages: int = 200,
    hide_closed: bool = False,
    resume: bool = True,
    limit: int = 0,
) -> dict:
    load_settings()
    store = PnlStore()
    client = GmgnPnlClient()
    wallets = load_message_wallets()
    if limit:
        wallets = wallets[:limit]

    progress = {
        'started_at': int(time.time()),
        'params': {'max_pages': max_pages, 'hide_closed': hide_closed, 'resume': resume},
        'total': len(wallets),
        'completed': 0,
        'skipped': 0,
        'failed': 0,
        'wallets': {},
    }

    for w in wallets:
        addr = w['address']
        if resume and store.latest_token_rows(addr):
            progress['skipped'] += 1
            progress['wallets'][addr] = {'status': 'skipped', 'reason': 'already_crawled'}
            continue
        result = crawl_wallet(store, client, addr, w.get('name', ''), max_pages, hide_closed)
        if result['status'] == 'ok':
            progress['completed'] += 1
        else:
            progress['failed'] += 1
        progress['wallets'][addr] = result
        progress['updated_at'] = int(time.time())

    index = build_master_index(store, wallets)
    progress['summary'] = {
        'with_data': index['with_data'],
        'storage': str(store.path),
    }
    return progress
