"""Small shared runtime helpers for the deterministic PnL pipeline."""
import os
from pathlib import Path


BASE = Path(__file__).resolve().parents[1]
ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


class ProviderError(RuntimeError):
    """A read-only upstream provider could not return usable data."""


def load_settings() -> None:
    """Load the read-only provider settings used by this project."""
    path = BASE / '.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            if '=' not in line or line.lstrip().startswith('#'):
                continue
            key, value = line.split('=', 1)
            key = key.strip()
            if key in ('GMGN_API_KEY', 'GMGN_PRIVATE_KEY', 'SOLSCAN_API_KEY', 'HELIUS_API_KEY') or key.startswith('CT_WALLET_'):
                os.environ.setdefault(key, value.strip().strip('"\''))
    local_bin = BASE / '.tools' / 'node_modules' / '.bin'
    if local_bin.exists():
        os.environ['PATH'] = str(local_bin) + os.pathsep + os.environ.get('PATH', '')


def valid_base58(value: str, size: int) -> bool:
    if not isinstance(value, str) or not value or len(value) > 90:
        return False
    number = 0
    for char in value:
        if char not in ALPHABET:
            return False
        number = number * 58 + ALPHABET.index(char)
    decoded_size = (number.bit_length() + 7) // 8 + len(value) - len(value.lstrip('1'))
    return decoded_size == size


def valid_address(value: str) -> bool:
    return valid_base58(value, 32)
