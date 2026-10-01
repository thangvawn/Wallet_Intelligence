#!/usr/bin/env python3
"""Collect only rank inputs that are absent or have shorter requested coverage."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.portfolio_pnl.enrichment import DEFAULT_COMPONENTS, collect_missing_data
from app.portfolio_pnl.latest_export import export_from_database
from app.runtime import load_settings
from app.wallet_identity import load_message_wallets


def main() -> int:
    parser = argparse.ArgumentParser(description='Resume-safe GMGN missing-data enrichment')
    parser.add_argument('--wallet', help='Chỉ xử lý một wallet')
    parser.add_argument(
        '--component', action='append', choices=DEFAULT_COMPONENTS,
        help='Có thể lặp lại; mặc định chạy mọi component an toàn.',
    )
    parser.add_argument(
        '--activity-days', type=int, default=365,
        help='365 chỉ fill thiếu; đặt 1095/3650 để chủ động mở rộng lịch sử.',
    )
    parser.add_argument('--max-pages', type=int, default=1000)
    parser.add_argument('--delay', type=float, default=0.75)
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--export', action='store_true', help='Tính lại JSON/CSV sau khi thu thập')
    args = parser.parse_args()

    load_settings()
    wallets = load_message_wallets()
    if args.wallet:
        matched = [item for item in wallets if item['address'] == args.wallet]
        wallets = matched or [{'address': args.wallet, 'name': '', 'emoji': ''}]
    if args.limit:
        wallets = wallets[:args.limit]

    def report_progress(state):
        if state['processed'] % 10 == 0 or state['processed'] == state['total']:
            print(json.dumps(state, ensure_ascii=False), flush=True)

    result = collect_missing_data(
        wallets=wallets,
        components=tuple(args.component or DEFAULT_COMPONENTS),
        activity_days=args.activity_days,
        max_pages=args.max_pages,
        delay=max(0.0, args.delay),
        force=args.force,
        progress=report_progress,
    )
    if args.export:
        # Always rebuild the canonical export for the complete tracked-wallet
        # cohort; --wallet only limits collection and must not truncate output.
        result['export'] = export_from_database()
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result['status'] == 'complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())
