"""Resume-safe Solscan collection for capital-history enrichment."""
import time
from typing import Callable, Iterable, Optional

from app.portfolio_pnl.analysis_pipeline import analyze_wallet_from_database
from app.portfolio_pnl.solscan_client import SolscanClient, SolscanRateLimitError
from app.portfolio_pnl.store import PnlStore


def transfer_key(row: dict) -> tuple:
    """Identify one transfer without depending on list position or page."""
    transaction = row.get('transaction') or {}
    signatures = transaction.get('signatures') or []
    signature = signatures[0] if signatures else ''
    return (
        str(
            row.get('trans_id') or row.get('tx_hash')
            or row.get('signature') or signature
        ),
        int(row.get('block_time') or row.get('blockTime') or row.get('timestamp') or 0),
        str(row.get('token_address') or ''),
        str(row.get('from_address') or ''),
        str(row.get('to_address') or ''),
        str(row.get('amount') or ''),
        str(row.get('flow') or ''),
    )


def merge_transfers(existing: list, incoming: list) -> tuple[list, int]:
    merged = {transfer_key(row): row for row in existing if isinstance(row, dict)}
    before = len(merged)
    for row in incoming:
        if isinstance(row, dict):
            merged[transfer_key(row)] = row
    rows = sorted(
        merged.values(),
        key=lambda row: (transfer_key(row)[1], transfer_key(row)[0]),
    )
    return rows, len(merged) - before


def _positive_timestamp(value) -> int:
    try:
        number = int(float(value or 0))
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def funding_timestamp(payload: dict) -> int:
    """Read common timestamp variants without assuming one response wrapper."""
    if not isinstance(payload, dict):
        return 0
    for key in ('block_time', 'funded_at', 'fund_from_ts', 'timestamp', 'time'):
        value = _positive_timestamp(payload.get(key))
        if value:
            return value
    for key in ('funded_by', 'data', 'source'):
        nested = payload.get(key)
        if isinstance(nested, dict):
            value = funding_timestamp(nested)
            if value:
                return value
    return 0


def _fallback_from_time(store: PnlStore, wallet: str, now: int) -> tuple[int, str]:
    stats = store.load_stats_snapshot(wallet) or {}
    common = stats.get('common') or {}
    timestamp = _positive_timestamp(common.get('fund_from_ts'))
    if timestamp:
        return timestamp, 'gmgn_fund_from_ts'

    activity = store.load_activity_snapshot(wallet) or {}
    timestamps = [
        _positive_timestamp(row.get('timestamp'))
        for row in (activity.get('events') or [])
        if isinstance(row, dict)
    ]
    timestamps = [value for value in timestamps if value]
    if timestamps:
        return min(timestamps), 'earliest_gmgn_activity'

    return now - 5 * 365 * 86400, 'solscan_five_year_window'


def _partial_capital_wallets(
    wallets: Iterable[dict], store: PnlStore, analyzer: Callable,
) -> list[dict]:
    selected = []
    for identity in wallets:
        try:
            report = analyzer(identity['address'], store=store)
        except Exception:
            selected.append(identity)
            continue
        capital = report.get('capital_outcome') or {}
        if capital.get('status') != 'VERIFIED_EQUITY_HISTORY':
            selected.append(identity)
    return selected


def collect_solscan_enrichment(
    wallets: Iterable[dict],
    store: Optional[PnlStore] = None,
    client: Optional[SolscanClient] = None,
    analyzer: Callable = analyze_wallet_from_database,
    partial_only: bool = True,
    max_pages: int = 100,
    delay: float = 0.75,
    force: bool = False,
    progress: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Collect funding source and transfers; partial snapshots remain resumable."""
    pnl_store = store or PnlStore()
    identities = list(wallets)
    targets = (
        _partial_capital_wallets(identities, pnl_store, analyzer)
        if partial_only else identities
    )
    # `delay` is a provider-wide minimum interval, including pagination and
    # retry attempts. This prevents a single busy wallet from creating a burst.
    solscan = client or SolscanClient(request_delay=delay)
    now = int(time.time())

    funding_needed = [
        item['address'] for item in targets
        if force or not pnl_store.load_external_snapshot('solscan', 'funded_by', item['address'])
    ]
    funding_fetched = 0
    for offset in range(0, len(funding_needed), 50):
        batch = funding_needed[offset:offset + 50]
        results = solscan.fetch_funded_by(batch)
        for wallet in batch:
            row = results.get(wallet) or {
                'address': wallet,
                'found': False,
                'reason': 'provider_returned_no_funding_source',
            }
            funding_status = (
                'unavailable'
                if row.get('availability') == 'requires_paid_solscan_plan'
                else 'complete'
            )
            payload = {
                'schema': 'solscan_funded_by_v1',
                'wallet_address': wallet,
                'fetched_at': now,
                'status': funding_status,
                'data': row,
            }
            pnl_store.save_external_snapshot(
                'solscan', 'funded_by', wallet, payload, status=funding_status,
            )
            funding_fetched += 1

    completed = checkpointed = skipped = failed = rows_added = pages = 0
    processed = 0
    rate_limited = False
    stopped_reason = None
    errors = []
    for index, identity in enumerate(targets, start=1):
        processed = index
        wallet = identity['address']
        current_error = None
        current_state = 'unknown'
        existing = pnl_store.load_external_snapshot(
            'solscan', 'account_transfers', wallet,
        ) or {}
        if not force and existing.get('status') == 'complete':
            skipped += 1
            current_state = 'skipped'
        else:
            try:
                base_rows = [] if force else (existing.get('rows') or [])
                funded = pnl_store.load_external_snapshot(
                    'solscan', 'funded_by', wallet,
                ) or {}
                from_time = funding_timestamp(funded.get('data') or {})
                from_source = 'solscan_funded_by'
                if not from_time:
                    from_time, from_source = _fallback_from_time(pnl_store, wallet, now)

                if not force and existing.get('status') == 'partial':
                    from_time = int(existing.get('from_time') or from_time)
                    from_source = str(existing.get('from_time_source') or from_source)
                    to_time = int(existing.get('to_time') or now)
                    start_page = int(existing.get('next_page') or 1)
                    start_cursor = existing.get('next_cursor')
                else:
                    to_time = now
                    start_page = 1
                    start_cursor = None

                segment = solscan.fetch_account_transfers(
                    wallet,
                    from_time=from_time,
                    to_time=to_time,
                    start_page=start_page,
                    max_pages=max_pages,
                    start_cursor=start_cursor,
                )
                rows, added = merge_transfers(base_rows, segment['rows'])
                is_complete = bool(segment['complete'])
                payload = {
                    'schema': 'solscan_external_activity_v2',
                    'wallet_address': wallet,
                    'wallet_name': identity.get('name') or '',
                    'fetched_at': int(time.time()),
                    'status': 'complete' if is_complete else 'partial',
                    'from_time': int(segment['from_time']),
                    'from_time_source': from_source,
                    'to_time': int(segment['to_time']),
                    'next_page': segment.get('next_page'),
                    'next_cursor': segment.get('next_cursor'),
                    'data_mode': segment.get('data_mode') or 'pro_account_transfers',
                    'pages_fetched': int(existing.get('pages_fetched') or 0)
                    + int(segment.get('pages_fetched') or 0),
                    'rows': rows,
                }
                pnl_store.save_external_snapshot(
                    'solscan', 'account_transfers', wallet, payload,
                    status=payload['status'],
                )
                pages += int(segment.get('pages_fetched') or 0)
                rows_added += added
                if segment.get('rate_limited'):
                    checkpointed += 1
                    current_state = 'rate_limited'
                    current_error = str(segment.get('error') or 'Solscan HTTP 429')
                    rate_limited = True
                    stopped_reason = current_error
                elif is_complete:
                    completed += 1
                    current_state = 'complete'
                else:
                    checkpointed += 1
                    current_state = 'checkpointed'
            except SolscanRateLimitError as exc:
                current_state = 'rate_limited'
                current_error = str(exc)
                rate_limited = True
                stopped_reason = current_error
            except Exception as exc:
                failed += 1
                current_state = 'failed'
                current_error = str(exc)
                errors.append({'address': wallet, 'error': current_error})

        if progress:
            progress({
                'processed': index,
                'total': len(targets),
                'wallet': wallet,
                'state': current_state,
                'error': current_error,
                'completed': completed,
                'checkpointed': checkpointed,
                'skipped': skipped,
                'failed': failed,
                'rows_added': rows_added,
                'pages': pages,
            })
        if rate_limited:
            break

    return {
        'status': (
            'complete'
            if not failed and not checkpointed and not rate_limited
            else 'partial'
        ),
        'wallets_input': len(identities),
        'wallets_targeted': len(targets),
        'wallets_processed': processed,
        'wallets_remaining': max(0, len(targets) - processed),
        'funding_fetched': funding_fetched,
        'completed': completed,
        'checkpointed': checkpointed,
        'skipped': skipped,
        'failed': failed,
        'rows_added': rows_added,
        'pages': pages,
        'rate_limited': rate_limited,
        'stopped_reason': stopped_reason,
        'errors': errors,
    }
