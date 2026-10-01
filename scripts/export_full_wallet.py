#!/usr/bin/env python3
"""Export every locally stored component and derived label for one wallet.

This command is database-only: it never calls GMGN or any external provider.
It is intended for audit, backup, and feeding a downstream data pipeline.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.portfolio_pnl.analysis_pipeline import analyze_wallet_from_database
from app.portfolio_pnl.label_projection import project_wallet_label
from app.portfolio_pnl.store import PnlStore, decode_payload


def _payload(conn: sqlite3.Connection, table: str, wallet: str) -> dict:
    row = conn.execute(
        f'SELECT payload FROM {table} WHERE wallet=? LIMIT 1', (wallet,),
    ).fetchone()
    return decode_payload(row[0]) if row else {}


def export_wallet(wallet: str, output: Path) -> Path:
    store = PnlStore()
    conn = sqlite3.connect(store.path)
    conn.row_factory = sqlite3.Row
    dashboard = _payload(conn, 'dashboard_snapshots', wallet)
    if not dashboard:
        raise FileNotFoundError(
            f'Không có dashboard snapshot cho wallet {wallet}. '
            'Hãy refresh/crawl wallet trước.'
        )
    activity = _payload(conn, 'wallet_activity_snapshots', wallet)
    intraday = _payload(conn, 'intraday_snapshots', wallet)
    report = analyze_wallet_from_database(wallet, store=store)

    external = []
    for row in conn.execute(
        'SELECT provider,kind,fetched_at,status,payload '
        'FROM external_data_snapshots WHERE wallet=?', (wallet,),
    ):
        external.append({
            'provider': row['provider'], 'kind': row['kind'],
            'fetched_at': row['fetched_at'], 'status': row['status'],
            'payload': decode_payload(row['payload']),
        })

    def rows(table: str):
        return [dict(row) for row in conn.execute(
            f'SELECT * FROM {table} WHERE wallet=?', (wallet,),
        )]

    def records(table: str):
        result = []
        for row in conn.execute(f'SELECT * FROM {table} WHERE wallet=?', (wallet,)):
            item = dict(row)
            if item.get('payload') is not None:
                item['payload'] = decode_payload(item['payload'])
            for key in ('params',):
                if isinstance(item.get(key), str):
                    try:
                        item[key] = json.loads(item[key])
                    except json.JSONDecodeError:
                        pass
            result.append(item)
        return result

    canonical_state = conn.execute(
        'SELECT * FROM canonical_wallet_state WHERE wallet=?', (wallet,)
    ).fetchone()
    canonical_feature = conn.execute(
        'SELECT * FROM canonical_features_current WHERE wallet=?', (wallet,)
    ).fetchone()
    canonical_label = conn.execute(
        'SELECT * FROM canonical_labels_current WHERE wallet=?', (wallet,)
    ).fetchone()
    canonical_label_inputs = conn.execute(
        'SELECT * FROM canonical_label_inputs_current WHERE wallet=?', (wallet,)
    ).fetchone()
    canonical_token_facts = records('canonical_token_facts')
    for item in canonical_token_facts:
        item['token_launch_time'] = item.get('token_launch_timestamp')
        item['wallet_first_buy_time'] = item.get('first_buy_timestamp')
        item['wallet_first_buy_price'] = item.get('first_buy_price')
        item['symbol'] = item.get('token_symbol')
    result = {
        'schema': 'wallet_full_export_v1',
        'generated_at': int(time.time()),
        'wallet': {
            'address': wallet,
            'name': dashboard.get('wallet_name') or '',
            'dashboard_fetched_at': dashboard.get('fetched_at'),
            'activity_fetched_at': activity.get('fetched_at'),
            'intraday_fetched_at': intraday.get('fetched_at'),
        },
        'storage': {
            'database': str(store.path),
            'database_format': 'SQLite 3 + WAL',
            'payload_format': (
                'UTF-8 JSON; payloads >=512 bytes may be stored as '
                'CTZ1 + zlib binary blobs'
            ),
        },
        'raw': {
            'dashboard_snapshot': dashboard,
            'activity_snapshot': activity,
            'intraday_snapshot': intraday,
            'external_snapshots': external,
            'wallet_summaries': records('wallet_summaries'),
            'raw_pages': records('raw_pages'),
            'token_rows': records('token_rows'),
        },
        'normalized': {
            'token_observations': (report.get('group_labels') or {}).get('token_observations') or [],
            'group_labels': report.get('group_labels') or {},
        },
        'canonical': {
            'wallet_state': dict(canonical_state) if canonical_state else None,
            'token_facts': canonical_token_facts,
            'features_current': (
                {**dict(canonical_feature), 'payload': decode_payload(canonical_feature['payload'])}
                if canonical_feature else None
            ),
            'labels_current': (
                {**dict(canonical_label), 'payload': decode_payload(canonical_label['payload'])}
                if canonical_label else None
            ),
            'label_inputs_current': dict(canonical_label_inputs) if canonical_label_inputs else None,
        },
        'analysis': report,
        'projected_label': project_wallet_label(report),
        'refresh_state': rows('wallet_refresh_state'),
        'refresh_components': rows('wallet_refresh_components'),
        'component_watermarks': rows('wallet_component_watermarks'),
        'crawl_runs': records('crawl_runs'),
        'feature_runs': records('wallet_feature_runs'),
        'label_runs': records('wallet_label_runs'),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n',
        encoding='utf-8',
    )
    conn.close()
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description='Export đầy đủ dữ liệu một wallet từ SQLite.')
    parser.add_argument('wallet', help='Địa chỉ wallet Solana')
    parser.add_argument('-o', '--output', type=Path, required=True, help='File JSON đầu ra')
    args = parser.parse_args()
    try:
        path = export_wallet(args.wallet.strip(), args.output)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({'status': 'ok', 'output': str(path.resolve())}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
