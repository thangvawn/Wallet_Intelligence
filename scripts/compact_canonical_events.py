#!/usr/bin/env python3
"""Convert wallet_events from duplicated JSON payloads to typed facts."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.portfolio_pnl.store import PnlStore, decode_payload


def finite(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float('inf') else None


def compact(batch_size: int = 5000, vacuum: bool = True) -> dict:
    store = PnlStore()
    processed = 0
    with store.connect() as db:
        rows = db.execute(
            "SELECT rowid,payload FROM wallet_events WHERE payload IS NOT NULL AND payload != ''"
        ).fetchall()
        for start in range(0, len(rows), max(1, batch_size)):
            updates = []
            for row in rows[start:start + batch_size]:
                event = decode_payload(row['payload'])
                updates.append((
                    str(event.get('token_symbol') or ''), finite(event.get('token_amount')),
                    finite(event.get('amount_usd')), finite(event.get('buy_cost_usd')),
                    finite(event.get('price_usd')), finite(event.get('quote_amount')),
                    str(event.get('quote_address') or ''), finite(event.get('gas_usd')),
                    '', row['rowid'],
                ))
            db.executemany(
                'UPDATE wallet_events SET token_symbol=?,token_amount=?,amount_usd=?,buy_cost_usd=?,'
                'price_usd=?,quote_amount=?,quote_address=?,gas_usd=?,payload=? WHERE rowid=?',
                updates,
            )
            processed += len(updates)
    if vacuum:
        with store.connect() as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute('VACUUM')
            db.execute('PRAGMA optimize')
    with store.connect() as db:
        remaining = db.execute(
            "SELECT COUNT(*) FROM wallet_events WHERE payload IS NOT NULL AND payload != ''"
        ).fetchone()[0]
    return {'processed': processed, 'remaining_payload_rows': remaining, 'vacuum': vacuum}


def main() -> int:
    parser = argparse.ArgumentParser(description='Compact typed canonical wallet events.')
    parser.add_argument('--no-vacuum', action='store_true')
    args = parser.parse_args()
    print(compact(vacuum=not args.no_vacuum))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

