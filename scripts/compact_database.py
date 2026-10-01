#!/usr/bin/env python3
"""Compress legacy plain-JSON SQLite payloads without deleting raw evidence."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.portfolio_pnl.store import PnlStore


def main() -> int:
    parser = argparse.ArgumentParser(description='Nén payload JSON trong SQLite.')
    parser.add_argument('--database', type=Path, default=None)
    parser.add_argument(
        '--vacuum', action='store_true',
        help='Thu hồi dung lượng file sau khi nén (cần thêm dung lượng trống tạm thời).',
    )
    parser.add_argument(
        '--prune', action='store_true',
        help='Xóa raw pages cũ theo các mốc retention (mặc định chỉ nén).',
    )
    parser.add_argument(
        '--prune-raw-days', type=int, default=90,
        help='Giữ raw pages của run complete trong số ngày này (mặc định 90).',
    )
    parser.add_argument(
        '--prune-failed-days', type=int, default=14,
        help='Giữ raw pages của run failed/partial trong số ngày này (mặc định 14).',
    )
    args = parser.parse_args()
    store = PnlStore(args.database)
    before = store.path.stat().st_size
    result = store.compact_payloads(vacuum=args.vacuum)
    if args.prune:
        result['prune'] = store.prune_raw_pages(
            keep_days=args.prune_raw_days,
            failed_days=args.prune_failed_days,
            dry_run=False,
        )
    else:
        result['prune'] = {
            'dry_run': True,
            'skipped': True,
            'reason': 'pass --prune to delete old raw pages',
        }
    result.update({
        'database': str(store.path),
        'size_before_bytes': before,
        'size_after_bytes': store.path.stat().st_size,
        'vacuum': args.vacuum,
    })
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
