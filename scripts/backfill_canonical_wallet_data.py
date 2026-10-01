#!/usr/bin/env python3
"""Backfill and validate the canonical serving layer for the wallet cohort.

Database-only. Existing raw snapshots are preserved. Activity snapshots are
replayed through ``save_activity_snapshot`` so the idempotent ``wallet_events``
table is populated before features and labels are synchronized.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.portfolio_pnl.analysis_pipeline import analyze_wallet_from_database
from app.portfolio_pnl.canonical_data import sync_canonical_wallet
from app.portfolio_pnl.label_projection import project_wallet_label
from app.portfolio_pnl.store import PnlStore


def _legacy_feature_map() -> dict:
    """Read the completed 192-wallet feature export when available.

    This is a migration aid only.  Raw snapshots remain the source of truth;
    future refreshes overwrite measurable fields from the live report and
    retain unsupported legacy fields with their source marker.
    """
    # Optional legacy migration input.  Keep it inside the deploy tree when
    # supplied; the live SQLite snapshots remain the source of truth.
    path = ROOT / 'data' / 'classified_wallets_192.csv'
    if not path.exists():
        return {}
    result = {}
    with path.open(newline='', encoding='utf-8-sig') as handle:
        for row in csv.DictReader(handle):
            address = str(row.get('wallet_address') or '').strip()
            if not address:
                continue
            values = {}
            for key, value in row.items():
                if key.startswith('val_'):
                    target = key[4:]
                    if value not in ('', None, 'nan', 'NaN'):
                        try:
                            values[target] = float(value)
                        except ValueError:
                            pass
                elif key.startswith('slug_') or key.startswith('label_'):
                    if value not in ('', None):
                        values[key] = value
            result[address] = values
    return result


def backfill(limit: int = 0, wallet: str = '') -> dict:
    store = PnlStore()
    legacy = _legacy_feature_map()
    addresses = [
        str(row.get('wallet_address') or '').strip()
        for row in store.list_dashboard_snapshots()
        if str(row.get('wallet_address') or '').strip()
    ]
    addresses = [wallet] if wallet else sorted(set(addresses))
    if limit > 0:
        addresses = addresses[:limit]
    completed = []
    failed = []
    for address in addresses:
        try:
            activity = store.load_activity_snapshot(address) or {}
            if activity:
                # Existing snapshots created before wallet_events was enabled
                # are safely replayed using the same event-id upsert path.
                store.save_activity_snapshot(address, activity)
            report = analyze_wallet_from_database(address, store=store)
            result = sync_canonical_wallet(
                store, address, report=report,
                projected_label=project_wallet_label(report),
                legacy_inputs=legacy.get(address),
            )
            completed.append(result)
        except Exception as exc:
            try:
                result = sync_canonical_wallet(store, address, error=str(exc))
            except Exception:
                result = None
            failed.append({'wallet': address, 'error': str(exc), 'canonical': result})
    with store.connect() as db:
        canonical_count = db.execute('SELECT COUNT(*) FROM canonical_wallet_state').fetchone()[0]
        event_count = db.execute('SELECT COUNT(*) FROM wallet_events').fetchone()[0]
        feature_count = db.execute('SELECT COUNT(*) FROM canonical_features_current').fetchone()[0]
        label_count = db.execute('SELECT COUNT(*) FROM canonical_labels_current').fetchone()[0]
    return {
        'status': 'complete' if not failed else 'partial',
        'selected': len(addresses),
        'completed': len(completed),
        'failed': failed,
        'canonical_wallet_count': canonical_count,
        'wallet_event_count': event_count,
        'canonical_feature_count': feature_count,
        'canonical_label_count': label_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Backfill canonical data for all wallets.')
    parser.add_argument('--wallet', default='', help='Chỉ xử lý một wallet')
    parser.add_argument('--limit', type=int, default=0, help='Giới hạn số wallet, 0 = toàn bộ')
    args = parser.parse_args()
    result = backfill(limit=max(0, args.limit), wallet=args.wallet.strip())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())
