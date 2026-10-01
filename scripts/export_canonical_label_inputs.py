#!/usr/bin/env python3
"""Export the current typed label-input cohort without touching raw data."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.portfolio_pnl.store import PnlStore


JSON_COLUMNS = {'group_labels_json', 'legacy_labels_json', 'value_sources_json'}


def export(output_json: Path, output_csv: Path) -> dict:
    store = PnlStore()
    with store.connect() as db:
        rows = [dict(row) for row in db.execute(
            'SELECT * FROM canonical_label_inputs_current ORDER BY wallet'
        )]
    for row in rows:
        for column in JSON_COLUMNS:
            try:
                row[column[:-5]] = json.loads(row.pop(column) or '{}')
            except (TypeError, json.JSONDecodeError):
                row[column[:-5]] = {}
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps({
            'schema': 'canonical_label_inputs_cohort_v1',
            'wallet_count': len(rows),
            'wallets': rows,
        }, ensure_ascii=False, indent=2, allow_nan=False) + '\n',
        encoding='utf-8',
    )
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    csv_rows = []
    for row in rows:
        flat = dict(row)
        for column in ('group_labels', 'legacy_labels', 'value_sources'):
            flat[column + '_json'] = json.dumps(
                flat.pop(column) or {}, ensure_ascii=False, sort_keys=True,
            )
        csv_rows.append(flat)
    fieldnames = list(csv_rows[0]) if csv_rows else []
    with output_csv.open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(csv_rows)
    return {'wallet_count': len(rows), 'json': str(output_json.resolve()), 'csv': str(output_csv.resolve())}


def main() -> int:
    parser = argparse.ArgumentParser(description='Export canonical label inputs for the wallet cohort.')
    parser.add_argument('--json', type=Path, default=ROOT / 'data' / 'canonical_label_inputs_192.json')
    parser.add_argument('--csv', type=Path, default=ROOT / 'data' / 'canonical_label_inputs_192.csv')
    args = parser.parse_args()
    print(json.dumps(export(args.json, args.csv), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
