#!/usr/bin/env python3
"""Optimized one-wallet CLI: database snapshot -> auditable label."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.runtime import load_settings
from app.portfolio_pnl.auto_label import DEFAULT_LABEL_JSON
from app.portfolio_pnl.single_wallet_pipeline import (
    DEFAULT_SINGLE_LABEL_JSON,
    run_single_wallet_pipeline,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Một địa chỉ Solana -> collection -> validation -> label JSON/CSV.',
    )
    parser.add_argument('wallet', help='Địa chỉ ví Solana đầy đủ')
    parser.add_argument('--name', default='', help='Tên hiển thị tùy chọn')
    parser.add_argument('-o', '--output', type=Path, default=DEFAULT_SINGLE_LABEL_JSON)
    parser.add_argument(
        '--refresh', action='store_true',
        help='Cập nhật provider trước khi label (mặc định chỉ đọc database).',
    )
    parser.add_argument('--local-only', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--holdings-max-pages', type=int, default=200)
    parser.add_argument('--activity-days', type=int, default=1095)
    parser.add_argument('--activity-max-pages', type=int, default=1000)
    parser.add_argument('--activity-overlap-days', type=int, default=2)
    parser.add_argument('--no-clan', action='store_true', help='Không so wallet với cohort label hiện tại')
    args = parser.parse_args()
    load_settings()
    try:
        result = run_single_wallet_pipeline(
            args.wallet,
            wallet_name=args.name,
            output_json=args.output,
            refresh=bool(args.refresh and not args.local_only),
            holdings_max_pages=args.holdings_max_pages,
            activity_days=args.activity_days,
            activity_max_pages=args.activity_max_pages,
            activity_overlap_days=args.activity_overlap_days,
            cohort_json=None if args.no_clan else DEFAULT_LABEL_JSON,
        )
    except Exception as exc:
        print(json.dumps({
            'status': 'failed',
            'wallet_address': args.wallet,
            'error': str(exc),
        }, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
