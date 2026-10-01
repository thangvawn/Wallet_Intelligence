#!/usr/bin/env python3
"""Extend GMGN wallet activity backward without re-downloading recent history."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.runtime import load_settings
from app.wallet_identity import load_message_wallets
from app.portfolio_pnl.gmgn_client import GmgnPnlClient
from app.portfolio_pnl.store import PnlStore


EVENT_TYPES = ['buy', 'sell', 'transferin', 'transferout', 'add', 'remove']


def event_key(event: dict) -> tuple:
    """Stable key for overlap at page and coverage boundaries."""
    return (
        int(event.get('timestamp') or 0),
        str(event.get('tx_hash') or ''),
        str(event.get('event_type') or ''),
        str(event.get('token_address') or ''),
        str(event.get('token_amount') or ''),
        str(event.get('quote_address') or ''),
        str(event.get('quote_amount') or ''),
    )


def merge_events(existing: list, incoming: list) -> tuple[list, int]:
    merged = {event_key(event): event for event in existing}
    before = len(merged)
    for event in incoming:
        merged[event_key(event)] = event
    rows = sorted(
        merged.values(),
        key=lambda row: (
            int(row.get('timestamp') or 0),
            str(row.get('tx_hash') or ''),
            str(row.get('event_type') or ''),
        ),
    )
    return rows, len(merged) - before


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Resume-safe backward extension of GMGN wallet activity',
    )
    parser.add_argument('--wallet', help='Chỉ xử lý một wallet')
    parser.add_argument(
        '--days', type=int, default=1095,
        help='Mục tiêu lịch sử tính theo ngày; mặc định 1095 ngày.',
    )
    parser.add_argument(
        '--max-pages', type=int, default=1000,
        help='Số trang tối đa cho mỗi wallet trong một lần chạy.',
    )
    parser.add_argument('--page-delay', type=float, default=0.75)
    parser.add_argument('--wallet-delay', type=float, default=1.0)
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--export', action='store_true', help='Tính lại JSON/CSV sau crawl')
    parser.add_argument(
        '--restart', action='store_true',
        help='Bỏ cursor/checkpoint và crawl lại từ trang mới nhất.',
    )
    args = parser.parse_args()
    if not 1 <= args.days <= 3650:
        parser.error('--days must be between 1 and 3650')
    if not 1 <= args.max_pages <= 1000:
        parser.error('--max-pages must be between 1 and 1000')

    load_settings()
    store = PnlStore()
    client = GmgnPnlClient()
    wallets = load_message_wallets()
    if args.wallet:
        wallets = [item for item in wallets if item['address'] == args.wallet]
        if not wallets:
            wallets = [{'address': args.wallet, 'name': '', 'emoji': ''}]
    if args.limit:
        wallets = wallets[:args.limit]

    target_cutoff = int(time.time()) - args.days * 86400
    completed = partial = skipped = failed = total_added = total_pages = 0

    for index, identity in enumerate(wallets, start=1):
        wallet = identity['address']
        existing = store.load_activity_snapshot(wallet) or {}
        current_days = int(existing.get('days') or 0)
        if (
            not args.restart
            and existing.get('status') == 'complete'
            and (current_days >= args.days or existing.get('history_exhausted') is True)
        ):
            skipped += 1
            print(f'[{index}/{len(wallets)}] skipped {wallet} ({current_days}d)', flush=True)
            continue

        checkpoint = existing.get('extension_checkpoint') or {}
        checkpoint_matches = int(checkpoint.get('target_days') or 0) == args.days
        if args.restart:
            start_cursor = None
        elif checkpoint_matches:
            start_cursor = checkpoint.get('next_cursor')
        else:
            start_cursor = (
                existing.get('history_cursor')
                or store.load_activity_resume_cursor(wallet)
            )

        run_id = store.start_run(wallet, 'gmgn_activity', {
            'mode': 'restart' if args.restart else 'extend',
            'target_days': args.days,
            'previous_days': current_days,
            'start_cursor': start_cursor,
            'max_pages': args.max_pages,
        })
        try:
            segment = client.fetch_wallet_activity_segment(
                wallet,
                cutoff_timestamp=target_cutoff,
                max_pages=args.max_pages,
                start_cursor=start_cursor,
                page_delay=args.page_delay,
            )
            store.append_pages(
                run_id, wallet, 'gmgn_activity', 'portfolio/activity',
                segment['pages'],
            )

            base_events = [] if args.restart else (existing.get('events') or [])
            events, added = merge_events(base_events, segment['events'])
            now = int(time.time())
            reached = bool(segment['reached_target'])
            history_exhausted = bool(segment['history_exhausted'])
            payload = {
                **existing,
                'schema': 'gmgn_wallet_activity_v2',
                'wallet_address': wallet,
                'wallet_name': identity.get('name') or existing.get('wallet_name') or '',
                'fetched_at': now,
                'start_at': target_cutoff if reached else int(existing.get('start_at') or target_cutoff),
                'end_at': int(existing.get('end_at') or now),
                'days': args.days if reached else current_days,
                # A checkpointed extension has not reached the requested
                # coverage yet.  Marking it complete makes downstream readers
                # treat partial history as authoritative.
                'status': 'complete' if reached else 'partial',
                'event_types': EVENT_TYPES,
                'pages': int(existing.get('pages') or 0) + len(segment['pages']),
                'events': events,
                'history_exhausted': history_exhausted,
                'history_cursor': (
                    None if history_exhausted
                    else segment.get('boundary_cursor') if reached
                    else existing.get('history_cursor')
                ),
                'extension_checkpoint': (
                    None if reached else {
                        'target_days': args.days,
                        'target_cutoff': target_cutoff,
                        'next_cursor': segment.get('next_cursor'),
                        'oldest_timestamp': segment.get('oldest_timestamp'),
                        'updated_at': now,
                    }
                ),
            }
            store.save_activity_snapshot(wallet, payload)
            store.finish_run(
                run_id, 'complete' if reached else 'partial', rows=added,
            )
            total_added += added
            total_pages += len(segment['pages'])
            if reached:
                completed += 1
                state = 'complete'
            else:
                partial += 1
                state = 'checkpointed'
            print(
                f'[{index}/{len(wallets)}] {state} {wallet}: '
                f'+{added} events, {len(segment["pages"])} pages, '
                f'{len(events)} total',
                flush=True,
            )
        except Exception as exc:
            store.finish_run(run_id, 'failed', error=str(exc))
            failed += 1
            print(f'[{index}/{len(wallets)}] failed {wallet}: {exc}', flush=True)
        time.sleep(max(0.0, args.wallet_delay))

    result = {
        'total': len(wallets),
        'completed': completed,
        'checkpointed': partial,
        'skipped': skipped,
        'failed': failed,
        'events_added': total_added,
        'pages': total_pages,
        'target_days': args.days,
    }
    if args.export:
        from app.portfolio_pnl.latest_export import export_from_database
        result['export'] = export_from_database()
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
