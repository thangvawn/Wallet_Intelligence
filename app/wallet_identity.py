"""Load the wallet identity list while preserving name and emoji."""
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.runtime import BASE, valid_address


MESSAGE_CANDIDATES = (BASE.parent / 'message.txt', BASE / 'message.txt')


def message_file_path() -> Optional[Path]:
    return next((path for path in MESSAGE_CANDIDATES if path.exists()), None)


def load_message_wallets() -> List[Dict[str, Any]]:
    path = message_file_path()
    if path:
        rows = json.loads(path.read_text(encoding='utf-8'))
    else:
        # The checked-in cohort manifest is the durable identity fallback for
        # database-only jobs and older exports which do not have message.txt.
        fallback = BASE / 'data' / 'tracked_wallets.json'
        if not fallback.exists():
            return []
        rows = json.loads(fallback.read_text(encoding='utf-8'))
    wallets: List[Dict[str, Any]] = []
    seen = set()
    for item in rows:
        address = str(item.get('address') or item.get('wallet_address') or '').strip()
        if not address or address in seen or not valid_address(address):
            continue
        seen.add(address)
        wallets.append({
            'address': address,
            'name': str(item.get('name') or item.get('wallet_name') or '').strip(),
            'emoji': str(item.get('emoji', '')).strip(),
        })
    return wallets
