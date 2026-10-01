"""Incremental startup fill and deterministic exports from local snapshots."""
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional

from app.runtime import load_settings
from app.wallet_identity import load_message_wallets
from app.portfolio_pnl.analysis_pipeline import analyze_wallet_from_database
from app.portfolio_pnl.auto_label import DEFAULT_LABEL_JSON
from app.portfolio_pnl.gmgn_client import GmgnPnlClient
from app.portfolio_pnl.label_export import write_label_exports
from app.portfolio_pnl.label_projection import project_wallet_label
from app.portfolio_pnl.normalize import normalize_wallet_summary
from app.portfolio_pnl.service import crawl_wallet
from app.portfolio_pnl.store import PnlStore


def snapshot_needs_backfill(payload: Optional[dict]) -> bool:
    """Only absent required sections trigger network collection."""
    return not payload or any(key not in payload for key in ('summary', 'rows', 'daily_calendar'))


def snapshot_needs_incremental_fill(payload: Optional[dict], now: Optional[datetime] = None) -> bool:
    """A structurally valid snapshot needs only dates after its fetch day."""
    if snapshot_needs_backfill(payload):
        return False
    fetched_at = int(payload.get('fetched_at') or 0)
    if not fetched_at:
        return True
    today = (now or datetime.now(tz=timezone.utc)).date()
    return datetime.fromtimestamp(fetched_at, tz=timezone.utc).date() < today


def fill_incremental_wallet(store, client, identity: dict, payload: dict) -> None:
    """Update summary + missing daily dates without recrawling holdings pages."""
    address = identity['address']
    fetched_at = int(payload.get('fetched_at') or 0)
    start_date = (
        datetime.fromtimestamp(fetched_at, tz=timezone.utc).date()
        if fetched_at else datetime.now(tz=timezone.utc).date() - timedelta(days=364)
    )
    raw_summary = client.fetch_wallet_profits(address, period='all')
    summary = normalize_wallet_summary(raw_summary, address, identity.get('name', ''))
    new_daily, _raw = client.fetch_daily_profit_calendar(address, start_date=start_date)
    merged_daily = {
        row.get('date'): row
        for row in (payload.get('daily_calendar') or [])
        if row.get('date')
    }
    merged_daily.update({row['date']: row for row in new_daily if row.get('date')})
    updated_at = int(time.time())
    component_times = dict(payload.get('component_fetched_at') or {})
    component_times.update({'profits': updated_at, 'calendar': updated_at})
    store.save_dashboard_snapshot(address, {
        **payload,
        'wallet_name': identity.get('name', '') or payload.get('wallet_name', ''),
        'fetched_at': updated_at,
        'component_fetched_at': component_times,
        'summary': summary,
        'daily_calendar': [merged_daily[key] for key in sorted(merged_daily)],
    })


def export_from_database(
    output_json: Path = DEFAULT_LABEL_JSON,
    wallets: Optional[Iterable[dict]] = None,
    store: Optional[PnlStore] = None,
    analyzer: Callable = analyze_wallet_from_database,
) -> Dict:
    """Recalculate labels from current SQLite snapshots without network refresh."""
    identities = list(wallets if wallets is not None else load_message_wallets())
    pnl_store = store or PnlStore()
    labels = []
    errors = []
    for identity in identities:
        try:
            report = analyzer(identity['address'], store=pnl_store)
            label = project_wallet_label(
                report,
                name=identity.get('name', ''),
                emoji=identity.get('emoji', ''),
            )
            # Database-only exports must carry the same freshness verdict as
            # the single-wallet API.  ``project_wallet_label`` is intentionally
            # provider-agnostic and cannot inspect snapshot age by itself.
            from app.portfolio_pnl.single_wallet_pipeline import _data_availability
            label['data_availability'] = _data_availability(
                pnl_store,
                identity['address'],
                report,
                refreshed=False,
                as_of=datetime.now(tz=timezone.utc),
                required_activity_days=1095,
            )
            if label['data_availability']['status'] != 'READY':
                label['labels']['rank_status'] = 'PROVISIONAL'
            labels.append(label)
        except Exception as exc:
            errors.append({'address': identity['address'], 'error': str(exc)})
    exports = write_label_exports(labels, Path(output_json))
    return {
        'status': 'complete' if not errors else 'partial',
        'total': len(identities),
        'labels_ready': len(labels),
        'errors': errors,
        'exports': exports,
    }


def backfill_missing_wallets(
    max_pages: int = 200,
    output_json: Path = DEFAULT_LABEL_JSON,
    progress: Optional[Callable[[dict], None]] = None,
    wallets: Optional[Iterable[dict]] = None,
    store: Optional[PnlStore] = None,
    client: Optional[GmgnPnlClient] = None,
    crawler: Callable = crawl_wallet,
    analyzer: Callable = analyze_wallet_from_database,
    incremental_filler: Callable = fill_incremental_wallet,
) -> Dict:
    """Full crawl missing snapshots; incrementally fill only new dates otherwise."""
    load_settings()
    identities = list(wallets if wallets is not None else load_message_wallets())
    pnl_store = store or PnlStore()
    snapshots = {
        identity['address']: pnl_store.load_dashboard_snapshot(identity['address'])
        for identity in identities
    }
    missing = [identity for identity in identities if snapshot_needs_backfill(snapshots[identity['address']])]
    incremental = [
        identity for identity in identities
        if snapshot_needs_incremental_fill(snapshots[identity['address']])
    ]
    if not missing and not incremental:
        exported = export_from_database(output_json, identities, pnl_store, analyzer)
        return {
            **exported, 'missing': 0, 'filled': 0, 'incremental': 0,
            'incremental_filled': 0, 'fill_failed': 0,
        }

    gmgn_client = client or GmgnPnlClient()
    errors = []
    filled = 0
    incremental_filled = 0
    queue = [('full', identity) for identity in missing] + [('incremental', identity) for identity in incremental]
    for index, (mode, identity) in enumerate(queue, start=1):
        try:
            if mode == 'full':
                result = crawler(
                    pnl_store,
                    gmgn_client,
                    identity['address'],
                    identity.get('name', ''),
                    max_pages=max_pages,
                    hide_closed=False,
                )
                if result.get('status') != 'ok':
                    raise RuntimeError(result.get('error') or 'GMGN full backfill failed')
                filled += 1
            else:
                incremental_filler(
                    pnl_store, gmgn_client, identity, snapshots[identity['address']]
                )
                incremental_filled += 1
        except Exception as exc:
            errors.append({
                'address': identity['address'],
                'mode': mode,
                'error': str(exc),
            })
        if progress:
            progress({
                'processed': index,
                'missing': len(missing),
                'incremental': len(incremental),
                'filled': filled,
                'incremental_filled': incremental_filled,
                'fill_failed': len(errors),
            })
    exported = export_from_database(output_json, identities, pnl_store, analyzer)
    return {
        **exported,
        'status': 'complete' if not errors and not exported['errors'] else 'partial',
        'missing': len(missing),
        'filled': filled,
        'incremental': len(incremental),
        'incremental_filled': incremental_filled,
        'fill_failed': len(errors),
        'fill_errors': errors,
    }
