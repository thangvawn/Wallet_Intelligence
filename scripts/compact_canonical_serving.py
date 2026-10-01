#!/usr/bin/env python3
"""Remove fields duplicated by canonical typed tables and vacuum SQLite."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.portfolio_pnl.store import PnlStore, decode_payload, encode_payload


def compact(vacuum: bool = True) -> dict:
    store = PnlStore()
    with store.connect() as db:
        raw_fields_rows = db.execute(
            "SELECT COUNT(*) FROM canonical_token_facts WHERE raw_fields_json IS NOT NULL AND raw_fields_json != ''"
        ).fetchone()[0]
        db.execute("UPDATE canonical_token_facts SET raw_fields_json=''")
        feature_rows = db.execute(
            'SELECT wallet,payload FROM canonical_features_current'
        ).fetchall()
        for row in feature_rows:
            payload = decode_payload(row['payload'])
            groups = dict(payload.get('group_labels') or {})
            groups.pop('token_observations', None)
            payload['group_labels'] = groups
            db.execute(
                'UPDATE canonical_features_current SET payload=? WHERE wallet=?',
                (encode_payload(payload), row['wallet']),
            )
        label_input_rows = db.execute(
            'SELECT wallet,group_labels_json FROM canonical_label_inputs_current'
        ).fetchall()
        for row in label_input_rows:
            try:
                groups = json.loads(row['group_labels_json'] or '{}')
            except json.JSONDecodeError:
                groups = {}
            groups.pop('token_observations', None)
            db.execute(
                'UPDATE canonical_label_inputs_current SET group_labels_json=? WHERE wallet=?',
                (json.dumps(groups, ensure_ascii=False, sort_keys=True, separators=(',', ':')), row['wallet']),
            )
    if vacuum:
        with store.connect() as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute('VACUUM')
            db.execute('PRAGMA optimize')
    return {
        'raw_fields_rows_cleared': raw_fields_rows,
        'feature_rows_compacted': len(feature_rows),
        'label_input_rows_compacted': len(label_input_rows),
        'vacuum': vacuum,
    }


if __name__ == '__main__':
    print(json.dumps(compact(), ensure_ascii=False))
