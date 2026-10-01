"""Rate-limited round-robin refresh of wallet data into SQLite."""
import time
from typing import Callable, Dict, Iterable, Optional

from app.portfolio_pnl.component_refresh import refresh_wallet_components
from app.portfolio_pnl.store import PnlStore
from app.wallet_identity import load_message_wallets
from app.portfolio_pnl.canonical_data import load_canonical_summary, sync_canonical_wallet
from app.portfolio_pnl.analysis_pipeline import analyze_wallet_from_database


def run_refresh_batch(
    store: Optional[PnlStore] = None,
    identities: Optional[Iterable[dict]] = None,
    batch_size: int = 4,
    wallet_delay_seconds: float = 15.0,
    activity_days: int = 1095,
    holdings_max_pages: int = 200,
    activity_max_pages: int = 1000,
    retry_after_seconds: int = 900,
    component_ttls: Optional[Dict[str, int]] = None,
    refresher: Callable = refresh_wallet_components,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict:
    """Refresh one small batch; failures are cooled down and never block labels."""
    pnl_store = store or PnlStore()
    wallets = list(identities if identities is not None else load_message_wallets())
    by_address = {
        str(item.get('address') or '').strip(): item
        for item in wallets if str(item.get('address') or '').strip()
    }
    queued_requests = pnl_store.load_refresh_requests(list(by_address))
    # A queued user request is allowed to bypass TTL even if the periodic
    # worker ends up claiming it (for example after a process restart). Timer
    # requests keep the normal due-only behavior.
    forced_addresses = {
        address for address, request in queued_requests.items()
        if int(request.get('priority') or 0) >= 100
        or str(request.get('reason') or '') == 'user_request'
    }
    candidates = pnl_store.claim_refresh_candidates(
        list(by_address), max(1, int(batch_size)),
        component_ttls=component_ttls,
        activity_days=activity_days,
    )
    completed = []
    failed = []
    for index, address in enumerate(candidates):
        identity = by_address[address]
        pnl_store.mark_refresh_attempt(address)
        try:
            result = refresher(
                address,
                wallet_name=identity.get('name', ''),
                holdings_max_pages=holdings_max_pages,
                activity_days=activity_days,
                activity_max_pages=activity_max_pages,
                store=pnl_store,
                component_ttls=component_ttls,
                force=address in forced_addresses,
            )
            if result.get('status') == 'partial':
                errors = result.get('failed_components') or []
                detail = '; '.join(f"{item.get('component')}: {item.get('error')}" for item in errors)
                raise RuntimeError(detail or f"Còn thiếu: {', '.join(result.get('remaining_components') or [])}")
            # Analyze only when a normalized input actually changed. A
            # successful poll with an identical provider payload still advances
            # the component watermark but does not rebuild labels.
            changed_components = result.get('changed_components') or []
            if 'changed_components' not in result or changed_components:
                analyze_wallet_from_database(address, store=pnl_store)
            # The analysis pipeline projects the report exactly once. Read only
            # the manifest; loading all token facts here would add another
            # large database read.
            canonical = load_canonical_summary(pnl_store, address)
            pnl_store.mark_refresh_success(address)
            completed.append({
                'wallet_address': address,
                'updated_at': result.get('updated_at'),
                'completed_components': result.get('completed_components'),
                'changed_components': changed_components,
                'canonical': canonical,
            })
        except Exception as exc:
            try:
                canonical = sync_canonical_wallet(pnl_store, address, error=str(exc))
            except Exception:
                canonical = None
            pnl_store.mark_refresh_failure(
                address, str(exc), retry_after_seconds=retry_after_seconds,
            )
            failed.append({'wallet_address': address, 'error': str(exc), 'canonical': canonical})
        if index + 1 < len(candidates) and wallet_delay_seconds > 0:
            sleeper(wallet_delay_seconds)
    return {
        'status': 'complete' if not failed else 'partial',
        'selected': len(candidates),
        'completed': completed,
        'failed': failed,
    }
