"""Cohort-only shared-opportunity signals; never changes wallet rank."""
import hashlib
from collections import defaultdict
from typing import Iterable, List

from app.portfolio_pnl.store import PnlStore


SIGNAL_ORDER = {'NORMAL': 0, 'CLAN_WATCH': 1, 'CLAN_ALERT': 2, 'PRIORITY_ALERT': 3}
ENTRY_WINDOW_SECONDS = 14 * 86400
EXCLUDED_TOKENS = {
    'So11111111111111111111111111111111111111111',
    'So11111111111111111111111111111111111111112',
    'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v',
    'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB',
}


def _clan_id(token: str, started_at: int) -> str:
    return 'CLAN-' + hashlib.sha1(f'{token}:{started_at}'.encode()).hexdigest()[:8].upper()


def _entry_groups(entries: list) -> list:
    """Split one token into non-overlapping 14-day opportunity windows."""
    groups = []
    for entry in sorted(entries, key=lambda row: row[1]):
        if not groups or entry[1] - groups[-1][0][1] > ENTRY_WINDOW_SECONDS:
            groups.append([entry])
        else:
            groups[-1].append(entry)
    return groups


def apply_clan_signals(labels: Iterable[dict], store: PnlStore = None) -> List[dict]:
    """Detect shared tokens across wallets using local aggregate holdings snapshots."""
    output = [dict(item) for item in labels]
    pnl_store = store or PnlStore()
    by_token = defaultdict(list)
    for item in output:
        address = item.get('address') or ''
        try:
            payload = pnl_store.load_dashboard_snapshot(address) or {}
        except Exception:
            payload = {}
        for row in payload.get('rows') or []:
            token = str(row.get('token_address') or '').strip()
            started_at = int(row.get('start_holding_timestamp') or 0)
            if (
                token and token not in EXCLUDED_TOKENS and started_at
                and float(row.get('bought_usd') or 0) > 0
            ):
                by_token[token].append((address, started_at))

    ranks = {
        item.get('address'): (item.get('labels') or {}).get('rank')
        for item in output
    }
    candidates = defaultdict(list)
    for token, entries in by_token.items():
        for group in _entry_groups(entries):
            addresses = {address for address, _timestamp in group}
            size = len(addresses)
            strong = sum(ranks.get(address) in ('S', 'A') for address in addresses)
            if size >= 5 and strong >= 2:
                level = 'PRIORITY_ALERT'
                action = 'CHECK_FIRST'
            elif size >= 5:
                level = 'CLAN_ALERT'
                action = 'CHECK'
            elif size >= 3:
                level = 'CLAN_WATCH'
                action = 'NORMAL'
            else:
                continue
            signal = {
                'level': level,
                'candidate_level': level,
                'action': action,
                'clan_id': _clan_id(token, group[0][1]),
                'clan_size': size,
                'shared_token': token,
                'strong_wallets': strong,
                'detection_basis': 'SHARED_TOKEN_WITHIN_14D_ENTRY_WINDOW',
                'verification_status': 'CANDIDATE',
                'eligible_for_priority_alert': False,
                'limitations': [
                    'shared_thesis_not_verified',
                    'entry_zone_not_verified',
                    'historical_allocation_not_available',
                    'current_shared_exposure_not_verified',
                ],
                'evidence_status': 'CANDIDATE',
            }
            for address in addresses:
                candidates[address].append(signal)

    for item in output:
        address = item.get('address') or ''
        wallet_candidates = candidates.get(address) or []
        if wallet_candidates:
            item['priority'] = max(
                wallet_candidates,
                key=lambda row: (SIGNAL_ORDER[row['level']], row['clan_size'], row['strong_wallets']),
            )
        elif (item.get('labels') or {}).get('rank') == 'S':
            item['priority'] = {
                **(item.get('priority') or {}),
                'action': 'CHECK',
                'detection_basis': 'STRONG_INDIVIDUAL_S',
            }
    return output
