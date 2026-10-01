"""Fail-closed pipeline from one Solana wallet address to an auditable label."""
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from app.runtime import load_settings, valid_address
from app.wallet_identity import load_message_wallets
from app.portfolio_pnl.analysis_pipeline import analyze_wallet_from_database
from app.portfolio_pnl.auto_label import DEFAULT_LABEL_JSON
from app.portfolio_pnl.clan_engine import apply_clan_signals
from app.portfolio_pnl.enrichment import intraday_from_activity
from app.portfolio_pnl.gmgn_client import GmgnPnlClient
from app.portfolio_pnl.label_export import write_label_exports
from app.portfolio_pnl.label_projection import project_wallet_label
from app.portfolio_pnl.service import crawl_wallet
from app.portfolio_pnl.store import BASE, PnlStore


DEFAULT_SINGLE_LABEL_JSON = BASE / 'data' / 'single_wallet_label.json'


def _event_key(event: dict) -> tuple:
    return (
        int(event.get('timestamp') or 0),
        str(event.get('tx_hash') or ''),
        str(event.get('event_type') or ''),
        str(event.get('token_address') or ''),
        str(event.get('token_amount') or ''),
        str(event.get('quote_address') or ''),
        str(event.get('quote_amount') or ''),
    )


def _merge_events(existing: list, incoming: list) -> list:
    merged = {_event_key(event): event for event in existing}
    for event in incoming:
        merged[_event_key(event)] = event
    return sorted(
        merged.values(),
        key=lambda row: (
            int(row.get('timestamp') or 0),
            str(row.get('tx_hash') or ''),
            str(row.get('event_type') or ''),
        ),
    )


def refresh_wallet_activity(
    store: PnlStore,
    client: GmgnPnlClient,
    address: str,
    name: str,
    days: int = 1095,
    max_pages: int = 1000,
    overlap_days: int = 2,
    page_delay: float = 0.25,
    now: Optional[int] = None,
) -> dict:
    """Refresh recent activity while preserving already collected deep history."""
    if not 1 <= days <= 3650:
        raise ValueError('activity days must be between 1 and 3650')
    if not 1 <= max_pages <= 1000:
        raise ValueError('activity max_pages must be between 1 and 1000')
    timestamp = int(now or time.time())
    target_cutoff = timestamp - days * 86400
    existing = store.load_activity_snapshot(address) or {}
    reusable_history = bool(
        existing.get('status') == 'complete'
        and int(existing.get('days') or 0) >= days
        and existing.get('events') is not None
    )
    if reusable_history:
        previous_end = int(
            existing.get('end_at') or existing.get('fetched_at') or timestamp
        )
        fetch_cutoff = max(target_cutoff, previous_end - max(1, overlap_days) * 86400)
        mode = 'recent_overlap_refresh'
    else:
        fetch_cutoff = target_cutoff
        mode = 'full_window_refresh'

    run_id = store.start_run(address, 'gmgn_activity_single', {
        'mode': mode,
        'days': days,
        'max_pages': max_pages,
        'fetch_cutoff': fetch_cutoff,
    })
    try:
        segment = client.fetch_wallet_activity_segment(
            address,
            cutoff_timestamp=fetch_cutoff,
            max_pages=max_pages,
            page_delay=page_delay,
        )
        store.append_pages(
            run_id, address, 'gmgn_activity_single', 'portfolio/activity',
            segment.get('pages') or [],
        )
        if not segment.get('reached_target'):
            raise RuntimeError(
                'Activity refresh chưa chạm mốc thời gian yêu cầu; '
                'không phát hành label từ snapshot partial.'
            )

        base_events = existing.get('events') or [] if reusable_history else []
        events = _merge_events(base_events, segment.get('events') or [])
        events = [
            event for event in events
            if int(event.get('timestamp') or 0) >= target_cutoff
        ]
        history_exhausted = bool(
            existing.get('history_exhausted')
            if reusable_history else segment.get('history_exhausted')
        )
        payload = {
            'schema': 'gmgn_wallet_activity_v3',
            'wallet_address': address,
            'wallet_name': name,
            'fetched_at': timestamp,
            'start_at': target_cutoff,
            'end_at': timestamp,
            'days': days,
            'status': 'complete',
            'event_types': ['buy', 'sell', 'transferin', 'transferout', 'add', 'remove'],
            'events': events,
            'history_exhausted': history_exhausted,
            # Keep the provider boundary as an audit/debug watermark. The
            # next realtime pass still uses a small time overlap, so a cursor
            # cannot silently skip events when GMGN reorders pages.
            'history_cursor': segment.get('next_cursor') or segment.get('boundary_cursor'),
            'refresh_mode': mode,
            'recent_pages_fetched': len(segment.get('pages') or []),
        }
        # Keep the full retained window in the snapshot for deterministic
        # analysis, but only normalize the newly fetched overlap.  This avoids
        # rewriting hundreds of thousands of historical rows on every realtime
        # poll.
        changed = store.save_activity_snapshot(
            address, payload, event_delta=segment.get('events') or [],
        )
        store.finish_run(run_id, 'complete', rows=len(segment.get('events') or []))
        return {**payload, '_data_changed': bool(changed)}
    except Exception as exc:
        store.finish_run(run_id, 'failed', error=str(exc))
        raise


def _conviction_v2(report: dict) -> dict:
    """Keep conviction outcome separate from scaling and data sufficiency."""
    conviction = report.get('conviction_behavior') or {}
    verdict = conviction.get('conviction_verdict')
    mapping = {
        'POSITIVE_SIZE_EDGE': 'POSITIVE',
        'OVERCONFIDENCE_RISK': 'RISKY',
        'NEUTRAL': 'NEUTRAL',
        'SIZE_INVARIANT': 'NEUTRAL',
    }
    label = mapping.get(verdict)
    scale = ((report.get('behavior_profile') or {}).get('scale') or {})
    return {
        'label': label,
        'status': 'AVAILABLE' if label else 'INSUFFICIENT_DATA',
        'verdict': verdict,
        'sample_size': conviction.get('sample_size'),
        'scale_style': scale.get('style'),
    }


def _data_availability(
    store: PnlStore, address: str, report: dict, refreshed: bool,
    as_of: datetime, required_activity_days: int,
) -> dict:
    dashboard = store.load_dashboard_snapshot(address) or {}
    activity = store.load_activity_snapshot(address) or {}
    intraday = store.load_intraday_snapshot(address) or {}
    stats_periods = store.load_stats_period_snapshots(address)
    component_states = store.load_refresh_components(address)
    component_watermarks = store.load_component_watermarks(address)
    component_times = dashboard.get('component_fetched_at') or {}
    capital = (report.get('performance_profile') or {}).get('capital_outcome') or {}
    as_of_timestamp = int(as_of.astimezone(timezone.utc).timestamp())

    def fresh(timestamp, ttl_hours=48):
        try:
            age = as_of_timestamp - int(timestamp or 0)
        except (TypeError, ValueError):
            return False
        return -300 <= age <= ttl_hours * 3600

    def component_timestamp(component: str, fallback):
        state = component_states.get(component)
        if state:
            return state.get('last_success_at') if state.get('status') == 'complete' else None
        watermark = component_watermarks.get(component)
        if watermark and watermark.get('status') == 'complete':
            return watermark.get('updated_at')
        return component_times.get(component) or fallback

    holdings_fetched_at = component_timestamp('holdings', dashboard.get('fetched_at'))
    profits_fetched_at = component_timestamp('profits', dashboard.get('fetched_at'))
    calendar_fetched_at = component_timestamp('calendar', dashboard.get('fetched_at'))
    activity_fetched_at = component_timestamp('activity', activity.get('fetched_at'))
    intraday_fetched_at = component_timestamp('intraday', intraday.get('fetched_at'))

    activity_window_complete = bool(
        activity.get('status') == 'complete'
        and (
            int(activity.get('days') or 0) >= required_activity_days
            or activity.get('history_exhausted') is True
        )
    )
    checks = {
        'dashboard_present': bool(dashboard),
        'holdings_complete': (dashboard.get('collection') or {}).get('complete') is True,
        'holdings_fresh_48h': fresh(holdings_fetched_at),
        'holdings_nonempty': bool(dashboard.get('rows')),
        'daily_calendar_present': bool(dashboard.get('daily_calendar')),
        'profits_fresh_48h': fresh(profits_fetched_at),
        'calendar_fresh_48h': fresh(calendar_fetched_at),
        'stats_7d_present': '7d' in stats_periods,
        'stats_30d_present': '30d' in stats_periods,
        'stats_7d_fresh_48h': fresh((stats_periods.get('7d') or {}).get('_snapshot_fetched_at')),
        'stats_30d_fresh_48h': fresh((stats_periods.get('30d') or {}).get('_snapshot_fetched_at')),
        'activity_complete': activity_window_complete,
        'activity_fresh_48h': fresh(activity_fetched_at),
        'activity_events_present': bool(activity.get('events')),
        'intraday_complete': intraday.get('status') == 'complete',
        'intraday_fresh_48h': fresh(intraday_fetched_at),
    }
    required = (
        'dashboard_present', 'holdings_complete', 'holdings_fresh_48h',
        'holdings_nonempty', 'profits_fresh_48h', 'daily_calendar_present',
        'calendar_fresh_48h', 'stats_7d_present',
        'stats_30d_present', 'stats_7d_fresh_48h', 'stats_30d_fresh_48h',
        'activity_complete', 'activity_fresh_48h', 'activity_events_present',
        'intraday_complete', 'intraday_fresh_48h',
    )
    ready = all(checks[key] for key in required)
    return {
        'status': 'READY' if ready else 'PROVISIONAL',
        'as_of_date_utc': as_of.astimezone(timezone.utc).date().isoformat(),
        'refreshed_in_this_run': refreshed,
        'checks': checks,
        'failed_checks': [key for key in required if not checks[key]],
        'holdings_fetched_at': holdings_fetched_at,
        'profits_fetched_at': profits_fetched_at,
        'calendar_fetched_at': calendar_fetched_at,
        'activity_fetched_at': activity_fetched_at,
        'intraday_fetched_at': intraday_fetched_at,
        'capital_status': capital.get('status'),
        'capital_limitations': capital.get('limitations') or [],
        'unsupported_metrics': (report.get('data_quality') or {}).get('unsupported_claims') or [],
    }


def _identity(address: str, name: str = '') -> dict:
    known = next(
        (item for item in load_message_wallets() if item['address'] == address),
        {},
    )
    return {
        'address': address,
        'name': name.strip() or known.get('name', ''),
        'emoji': known.get('emoji', ''),
    }


def refresh_single_wallet_data(
    wallet_address: str,
    wallet_name: str = '',
    holdings_max_pages: int = 200,
    activity_days: int = 1095,
    activity_max_pages: int = 1000,
    activity_overlap_days: int = 2,
    store: Optional[PnlStore] = None,
    client: Optional[GmgnPnlClient] = None,
    as_of: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Refresh provider data into SQLite without computing or exporting a label."""
    address = wallet_address.strip()
    if not valid_address(address):
        raise ValueError('Địa chỉ Solana phải giải mã base58 thành 32 byte.')
    if not 1 <= holdings_max_pages <= 1000:
        raise ValueError('holdings_max_pages phải nằm trong khoảng 1-1000.')
    identity = _identity(address, wallet_name)
    load_settings()
    pnl_store = store or PnlStore()
    gmgn = client or GmgnPnlClient()
    refresh_time = as_of or datetime.now(tz=timezone.utc)
    if refresh_time.tzinfo is None:
        refresh_time = refresh_time.replace(tzinfo=timezone.utc)

    crawl = crawl_wallet(
        pnl_store, gmgn, address, identity['name'],
        max_pages=holdings_max_pages, hide_closed=False,
    )
    if crawl.get('status') != 'ok':
        raise RuntimeError(crawl.get('error') or 'GMGN wallet crawl failed.')
    activity = refresh_wallet_activity(
        pnl_store, gmgn, address, identity['name'],
        days=activity_days,
        max_pages=activity_max_pages,
        overlap_days=activity_overlap_days,
        now=int(refresh_time.timestamp()),
    )
    intraday = intraday_from_activity(address, identity['name'], activity)
    pnl_store.save_intraday_snapshot(address, intraday)
    return {
        'status': 'ok',
        'wallet_address': address,
        'updated_at': int(refresh_time.timestamp()),
        'collection': crawl.get('collection') or {},
        'activity_events': len(activity.get('events') or []),
        'intraday_events': len(intraday.get('events') or []),
    }


def run_single_wallet_pipeline(
    wallet_address: str,
    wallet_name: str = '',
    output_json: Path = DEFAULT_SINGLE_LABEL_JSON,
    refresh: bool = True,
    holdings_max_pages: int = 200,
    activity_days: int = 1095,
    activity_max_pages: int = 1000,
    activity_overlap_days: int = 2,
    store: Optional[PnlStore] = None,
    client: Optional[GmgnPnlClient] = None,
    as_of: Optional[datetime] = None,
    cohort_json: Optional[Path] = DEFAULT_LABEL_JSON,
) -> Dict[str, Any]:
    """Collect, validate, analyze and export exactly one wallet label."""
    address = wallet_address.strip()
    if not valid_address(address):
        raise ValueError('Địa chỉ Solana phải giải mã base58 thành 32 byte.')
    if not 1 <= holdings_max_pages <= 1000:
        raise ValueError('holdings_max_pages phải nằm trong khoảng 1-1000.')
    identity = _identity(address, wallet_name)
    load_settings()
    pnl_store = store or PnlStore()
    analysis_time = as_of or datetime.now(tz=timezone.utc)
    if analysis_time.tzinfo is None:
        analysis_time = analysis_time.replace(tzinfo=timezone.utc)

    collection = None
    activity_event_count = 0
    if refresh:
        refresh_result = refresh_single_wallet_data(
            address,
            wallet_name=identity['name'],
            holdings_max_pages=holdings_max_pages,
            activity_days=activity_days,
            activity_max_pages=activity_max_pages,
            activity_overlap_days=activity_overlap_days,
            store=pnl_store,
            client=client,
            as_of=analysis_time,
        )
        collection = refresh_result['collection']
        activity_event_count = refresh_result['activity_events']
    elif not pnl_store.load_dashboard_snapshot(address):
        raise FileNotFoundError('Wallet chưa có dashboard snapshot cục bộ.')

    report = analyze_wallet_from_database(
        address, store=pnl_store, as_of=analysis_time,
    )
    label = project_wallet_label(
        report, name=identity['name'], emoji=identity['emoji'],
    )
    label['labels']['conviction_v2'] = _conviction_v2(report)
    label['data_availability'] = _data_availability(
        pnl_store, address, report, refresh, analysis_time, activity_days,
    )
    if label['data_availability']['status'] != 'READY':
        label['labels']['rank_status'] = 'PROVISIONAL'

    cohort = []
    cohort_path = Path(cohort_json) if cohort_json else None
    if cohort_path and cohort_path.exists():
        loaded = json.loads(cohort_path.read_text(encoding='utf-8'))
        if isinstance(loaded, list):
            cohort = [item for item in loaded if item.get('address') != address]
    cohort.append(label)
    cohort_with_clans = apply_clan_signals(cohort, store=pnl_store)
    final_label = next(item for item in cohort_with_clans if item.get('address') == address)
    # Clan was computed against the full cohort above. Do not recompute it with
    # a one-wallet collection while serializing the result.
    exports = write_label_exports(
        [final_label], Path(output_json), apply_clans=False,
    )

    return {
        'status': 'ok',
        'wallet_address': address,
        'feature_version': (report.get('pipeline') or {}).get('feature_version'),
        'formula_version': (report.get('pipeline') or {}).get('formula_version'),
        'input_hash': (report.get('pipeline') or {}).get('input_hash'),
        'refresh': {
            'performed': refresh,
            'collection': collection,
            'activity_events': activity_event_count,
        },
        'data_availability': final_label['data_availability'],
        'exports': exports,
        'label': final_label,
    }
