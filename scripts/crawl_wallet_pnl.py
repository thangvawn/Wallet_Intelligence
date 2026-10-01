"""Batch crawl GMGN Recent PnL (holdings) for message.txt wallets — separate from evidence sync."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.portfolio_pnl.service import crawl_all, crawl_wallet
from app.portfolio_pnl.store import PnlStore
from app.portfolio_pnl.gmgn_client import GmgnPnlClient
from app.runtime import load_settings
from app.wallet_identity import load_message_wallets


def main():
    p = argparse.ArgumentParser(description='Crawl GMGN Recent PnL / holdings.')
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument('--wallet', help='Địa chỉ ví đơn')
    target.add_argument('--all', action='store_true', help='Crawl toàn bộ ví trong message.txt')
    p.add_argument('--max-pages', type=int, default=200, help='Giới hạn an toàn; crawl đến khi hết cursor')
    p.add_argument('--hide-closed', action='store_true', help='Ẩn position đã đóng (mặc định: hiện cả closed)')
    p.add_argument('--resume', action='store_true', help='Bỏ qua ví đã crawl')
    p.add_argument('--force', action='store_true', help='Crawl lại dù đã có data')
    p.add_argument('--limit', type=int, default=0)
    args = p.parse_args()

    load_settings()

    if args.all:
        progress = crawl_all(
            max_pages=args.max_pages,
            hide_closed=args.hide_closed,
            resume=bool(args.resume and not args.force),
            limit=args.limit,
        )
        print(json.dumps(progress.get('summary', progress), ensure_ascii=False, indent=2))
        return 1 if progress.get('failed') else 0

    store = PnlStore()
    client = GmgnPnlClient()
    wallet = args.wallet.strip()
    name = ''
    for w in load_message_wallets():
        if w['address'] == wallet:
            name = w.get('name', '')
            break
    if args.resume and not args.force and store.latest_token_rows(wallet):
        print(json.dumps({'wallet': wallet, 'status': 'skipped'}, ensure_ascii=False))
        return 0
    result = crawl_wallet(store, client, wallet, name, args.max_pages, args.hide_closed)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get('status') == 'failed' else 0


if __name__ == '__main__':
    sys.exit(main())
