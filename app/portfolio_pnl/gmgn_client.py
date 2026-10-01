"""GMGN portfolio holdings / profits fetcher (separate from evidence activity sync)."""
import json
import os
import re
import shutil
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.runtime import ProviderError


class GmgnPnlClient:
    source = 'gmgn'

    def __init__(self):
        if not os.getenv('GMGN_API_KEY'):
            raise ProviderError('GMGN_API_KEY chưa được cấu hình.')
        if not shutil.which('gmgn-cli'):
            raise ProviderError('Thiếu gmgn-cli. Cài CLI chính thức theo README.')
        if not os.getenv('GMGN_PRIVATE_KEY'):
            raise ProviderError(
                'GMGN_PRIVATE_KEY bắt buộc cho portfolio holdings (Recent PnL). '
                'Thêm PEM key vào .env — lấy tại GMGN API Management.'
            )

    def _run(self, args: List[str]) -> dict:
        allowed = {
            ('portfolio', 'holdings'),
            ('portfolio', 'profits'),
            ('portfolio', 'stats'),
            ('portfolio', 'activity'),
        }
        if tuple(args[:2]) not in allowed:
            raise ProviderError('Lệnh GMGN PnL không được phép.')
        for attempt in range(5):
            try:
                result = subprocess.run(
                    ['gmgn-cli', *args, '--raw'],
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
            except (subprocess.TimeoutExpired, OSError):
                raise ProviderError('GMGN CLI timeout hoặc không chạy được.') from None
            if not result.returncode:
                break
            detail = (result.stderr or result.stdout or '').strip()
            if 'RATE_LIMIT' in detail or 'HTTP 429' in detail:
                match = re.search(r'~(\d+)s remaining', detail)
                wait_seconds = int(match.group(1)) + 1 if match else 6
                if attempt < 4 and wait_seconds <= 120:
                    deadline = time.monotonic() + wait_seconds
                    while time.monotonic() < deadline:
                        time.sleep(min(10.0, deadline - time.monotonic()))
                    continue
                raise ProviderError(
                    f'GMGN đang giới hạn tốc độ (HTTP 429); thử lại sau khoảng {wait_seconds}s.'
                )
            if 'HTTP 401' in detail or 'HTTP 403' in detail:
                raise ProviderError(
                    'GMGN từ chối xác thực; kiểm tra GMGN_API_KEY + GMGN_PRIVATE_KEY.'
                )
            transient_network_error = any(marker in detail.lower() for marker in (
                'fetch failed', 'timeout', 'econnreset', 'enotfound',
                'socket hang up', 'temporary failure',
            ))
            if transient_network_error and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise ProviderError(f'GMGN CLI thất bại: {detail[:300] or "không có chi tiết"}.')
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            raise ProviderError('GMGN CLI không trả về JSON hợp lệ.') from None
        if isinstance(payload, dict) and (payload.get('error') or payload.get('code') not in (None, 0, 200)):
            raise ProviderError('GMGN trả về lỗi holdings/profits.')
        return payload

    @staticmethod
    def _unwrap_list(payload: dict, list_keys=('list', 'holdings', 'activities')) -> Tuple[List[dict], Optional[str]]:
        body = payload.get('data', payload) if isinstance(payload, dict) else {}
        if isinstance(body, list):
            return body, None
        if not isinstance(body, dict):
            return [], None
        for key in list_keys:
            rows = body.get(key)
            if isinstance(rows, list):
                return rows, body.get('next')
        return [], body.get('next')

    def fetch_holdings(
        self,
        wallet: str,
        max_pages: int = 200,
        limit: int = 50,
        hide_closed: bool = False,
        hide_airdrop: bool = False,
        hide_abnormal: bool = False,
        order_by: str = 'total_profit',
    ) -> Tuple[List[dict], List[dict]]:
        """Paginate the full GMGN Recent PnL table until the cursor is exhausted."""
        cursor = None
        seen = set()
        rows: List[dict] = []
        pages: List[dict] = []
        next_cursor = None
        for _ in range(max_pages):
            args = [
                'portfolio', 'holdings',
                '--chain', 'sol',
                '--wallet', wallet,
                '--limit', str(min(50, max(1, limit))),
                '--order-by', order_by,
                '--direction', 'desc',
                '--hide-closed', 'true' if hide_closed else 'false',
                '--hide-airdrop', 'true' if hide_airdrop else 'false',
                '--hide-abnormal', 'true' if hide_abnormal else 'false',
            ]
            if cursor:
                args += ['--cursor', str(cursor)]
            payload = self._run(args)
            page_rows, next_cursor = self._unwrap_list(payload)
            pages.append({
                'params': {
                    'cursor': cursor,
                    'order_by': order_by,
                    'hide_closed': hide_closed,
                    'hide_airdrop': hide_airdrop,
                    'hide_abnormal': hide_abnormal,
                    'next_cursor': next_cursor,
                },
                'payload': payload,
                'count': len(page_rows),
            })
            rows.extend(page_rows)
            if not next_cursor:
                break
            if str(next_cursor) in seen:
                raise ProviderError('GMGN holdings lặp cursor.')
            seen.add(str(next_cursor))
            cursor = next_cursor

        if next_cursor:
            raise ProviderError(
                f'GMGN holdings vượt giới hạn an toàn {max_pages} trang; '
                'không lưu snapshot thiếu dữ liệu.'
            )

        # A token can appear again at a pagination boundary; keep its latest row once.
        unique_rows: Dict[str, dict] = {}
        anonymous_rows: List[dict] = []
        for row in rows:
            token = row.get('token') or {}
            address = token.get('token_address') or token.get('address')
            if address:
                # Pages are requested in GMGN's descending sort order. Cursor
                # boundaries can repeat a token with a stale/different snapshot;
                # the first occurrence is the row visible in the GMGN table.
                unique_rows.setdefault(str(address), row)
            else:
                anonymous_rows.append(row)
        rows = list(unique_rows.values()) + anonymous_rows
        return rows, pages

    def fetch_wallet_profits(self, wallet: str, period: str = 'all') -> dict:
        payload = self._run([
            'portfolio', 'profits',
            '--chain', 'sol',
            '--wallet', wallet,
            '--period', period,
        ])
        rows, _ = self._unwrap_list(payload)
        return rows[0] if rows else {}

    def fetch_wallet_stats(self, wallet: str, period: str = '7d') -> dict:
        if period not in ('7d', '30d'):
            raise ProviderError('GMGN wallet stats chỉ hỗ trợ 7d hoặc 30d.')
        payload = self._run([
            'portfolio', 'stats',
            '--chain', 'sol',
            '--wallet', wallet,
            '--period', period,
        ])
        body = payload.get('data', payload) if isinstance(payload, dict) else {}
        if isinstance(body, list):
            return body[0] if body else {}
        if isinstance(body, dict):
            rows = body.get('list')
            if isinstance(rows, list):
                return rows[0] if rows else {}
            return body
        return {}

    def fetch_daily_profit_calendar(
        self,
        wallet: str,
        days: int = 365,
        start_date: Optional[date] = None,
    ) -> Tuple[List[dict], dict]:
        """Fetch the same UTC daily-profit snapshots used by the GMGN web calendar."""
        try:
            from curl_cffi import requests as curl_requests
        except ImportError:
            raise ProviderError('Thiếu curl_cffi để tải GMGN PnL Calendar.') from None

        today = datetime.now(tz=timezone.utc).date()
        start = start_date or (today - timedelta(days=max(1, days) - 1))
        end = today + timedelta(days=1)
        body = {
            'wallet_addresses': [wallet],
            'start_at': int(datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc).timestamp()),
            'end_at': int(datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc).timestamp()) - 1,
        }
        try:
            response = curl_requests.post(
                'https://gmgn.ai/pf/api/v1/wallets/sol/daily_profits',
                json=body,
                headers={
                    'Origin': 'https://gmgn.ai',
                    'Referer': f'https://gmgn.ai/sol/address/{wallet}',
                },
                impersonate='chrome',
                timeout=30,
            )
        except Exception:
            raise ProviderError('Không tải được GMGN web PnL Calendar.') from None
        if response.status_code != 200:
            raise ProviderError(f'GMGN web PnL Calendar trả HTTP {response.status_code}.')
        try:
            payload = response.json()
        except ValueError:
            raise ProviderError('GMGN web PnL Calendar không trả JSON hợp lệ.') from None
        if payload.get('code') not in (None, 0) or not isinstance(payload.get('data'), dict):
            raise ProviderError('GMGN web PnL Calendar trả dữ liệu lỗi.')

        raw_rows = payload['data'].get('list') or []
        rows = []
        for raw in raw_rows:
            timestamp = int(raw.get('date') or 0)
            if not timestamp:
                continue
            total = float(raw.get('total_profit') or 0)
            profit = float(raw.get('win_profit') or 0)
            loss = abs(float(raw.get('loss_profit') or 0))
            rows.append({
                'date': datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat(),
                'realized_pnl_usd': round(total, 2),
                'profit_usd': round(profit, 2),
                'loss_usd': round(loss, 2),
                'buy_tx_count': int(raw.get('total_buys') or 0),
                'sell_tx_count': int(raw.get('total_sells') or 0),
                'transfer_in_count': int(raw.get('total_transfer_ins') or 0),
                'transfer_out_count': int(raw.get('total_transfer_outs') or 0),
                'buy_volume_usd': round(float(raw.get('buy_amount_usd') or 0), 2),
                'sell_volume_usd': round(float(raw.get('sell_amount_usd') or 0), 2),
                'is_estimated': False,
                'source': 'gmgn_web_daily_profits',
            })
        return rows, payload

    def fetch_intraday_pnl_events(
        self,
        wallet: str,
        days: int = 365,
        max_pages: int = 200,
    ) -> Tuple[List[dict], List[dict]]:
        """Fetch timestamped sell/transfer-out events used to allocate daily PnL."""
        cursor = None
        seen_cursors = set()
        seen_events = set()
        events: List[dict] = []
        pages: List[dict] = []
        cutoff = int(time.time()) - max(1, days) * 86400

        for _ in range(max_pages):
            args = [
                'portfolio', 'activity',
                '--chain', 'sol',
                '--wallet', wallet,
                '--limit', '50',
                '--type', 'sell',
                '--type', 'transferOut',
            ]
            if cursor:
                args += ['--cursor', str(cursor)]
            payload = self._run(args)
            activities, next_cursor = self._unwrap_list(
                payload,
                list_keys=('activities', 'list'),
            )
            pages.append({
                'params': {'cursor': cursor, 'types': ['sell', 'transferOut'], 'days': days},
                'payload': payload,
                'count': len(activities),
            })

            oldest = None
            for activity in activities:
                timestamp = int(activity.get('timestamp') or 0)
                if timestamp:
                    oldest = timestamp if oldest is None else min(oldest, timestamp)
                if timestamp < cutoff:
                    continue
                token = activity.get('token') or {}
                event_key = (
                    str(activity.get('tx_hash') or ''),
                    str(activity.get('event_type') or activity.get('type') or ''),
                    str(token.get('token_address') or token.get('address') or ''),
                    str(activity.get('token_amount') or ''),
                    str(activity.get('quote_address') or ''),
                    str(activity.get('quote_amount') or ''),
                )
                if event_key in seen_events:
                    continue
                seen_events.add(event_key)
                raw_basis = activity.get('buy_cost_usd')
                events.append({
                    'timestamp': timestamp,
                    'event_type': str(
                        activity.get('event_type') or activity.get('type') or ''
                    ).lower().replace('_', ''),
                    'tx_hash': str(activity.get('tx_hash') or ''),
                    'token_address': str(token.get('token_address') or token.get('address') or ''),
                    'token_symbol': str(token.get('symbol') or ''),
                    'amount_usd': float(activity.get('cost_usd') or 0),
                    'buy_cost_usd': float(raw_basis) if raw_basis is not None else None,
                    'gas_usd': float(activity.get('gas_usd') or 0),
                })

            if not next_cursor or (oldest is not None and oldest < cutoff):
                cursor = None
                break
            if str(next_cursor) in seen_cursors:
                raise ProviderError('GMGN intraday activity lặp cursor.')
            seen_cursors.add(str(next_cursor))
            cursor = next_cursor
            time.sleep(0.25)

        if cursor:
            raise ProviderError('GMGN intraday activity vượt giới hạn phân trang an toàn.')
        events.sort(key=lambda row: row['timestamp'])
        return events, pages

    def fetch_wallet_activity(
        self,
        wallet: str,
        days: int = 365,
        max_pages: int = 200,
    ) -> Tuple[List[dict], List[dict]]:
        """Fetch a complete rolling activity window; reject truncated results."""
        result = self.fetch_wallet_activity_segment(
            wallet,
            cutoff_timestamp=int(time.time()) - max(1, days) * 86400,
            max_pages=max_pages,
        )
        if not result['reached_target']:
            raise ProviderError(
                f'GMGN full activity vượt giới hạn {max_pages} trang; không lưu snapshot partial.'
            )
        return result['events'], result['pages']

    def fetch_wallet_activity_segment(
        self,
        wallet: str,
        cutoff_timestamp: int,
        max_pages: int = 200,
        start_cursor: Optional[str] = None,
        page_delay: float = 0.25,
    ) -> Dict[str, Any]:
        """Fetch one resumable backward segment and expose the next cursor."""
        activity_types = ('buy', 'sell', 'transferIn', 'transferOut', 'add', 'remove')
        cutoff = int(cutoff_timestamp)
        cursor = start_cursor
        seen_cursors = set()
        seen_events = set()
        events: List[dict] = []
        pages: List[dict] = []
        next_cursor = None
        oldest = None

        for _ in range(max_pages):
            args = [
                'portfolio', 'activity', '--chain', 'sol', '--wallet', wallet,
                '--limit', '50',
            ]
            for activity_type in activity_types:
                args += ['--type', activity_type]
            if cursor:
                args += ['--cursor', str(cursor)]
            payload = self._run(args)
            activities, next_cursor = self._unwrap_list(
                payload, list_keys=('activities', 'list'),
            )
            pages.append({
                'params': {
                    'cursor': cursor,
                    'types': list(activity_types),
                    'cutoff_timestamp': cutoff,
                    'next_cursor': next_cursor,
                },
                'payload': payload,
                'count': len(activities),
            })
            oldest = None
            for activity in activities:
                timestamp = int(activity.get('timestamp') or 0)
                if timestamp:
                    oldest = timestamp if oldest is None else min(oldest, timestamp)
                if not timestamp or timestamp < cutoff:
                    continue
                token = activity.get('token') or {}
                token_address = str(token.get('token_address') or token.get('address') or '')
                raw_type = str(activity.get('event_type') or activity.get('type') or '')
                event_type = raw_type.replace('_', '').lower()
                event_key = (
                    str(activity.get('tx_hash') or ''), event_type, token_address,
                    str(activity.get('token_amount') or ''),
                    str(activity.get('quote_address') or ''),
                    str(activity.get('quote_amount') or ''),
                )
                if event_key in seen_events:
                    continue
                seen_events.add(event_key)
                events.append({
                    'timestamp': timestamp,
                    'event_type': event_type,
                    'tx_hash': str(activity.get('tx_hash') or ''),
                    'token_address': token_address,
                    'token_symbol': str(token.get('symbol') or ''),
                    'token_amount': float(activity.get('token_amount') or 0),
                    'price_usd': float(activity.get('price_usd') or 0),
                    'amount_usd': float(activity.get('cost_usd') or 0),
                    'buy_cost_usd': (
                        float(activity['buy_cost_usd'])
                        if activity.get('buy_cost_usd') is not None else None
                    ),
                    'quote_address': str(activity.get('quote_address') or ''),
                    'quote_amount': float(activity.get('quote_amount') or 0),
                    'gas_usd': float(activity.get('gas_usd') or 0),
                })

            reached_target = bool(not next_cursor or (oldest is not None and oldest < cutoff))
            if reached_target:
                break
            if str(next_cursor) in seen_cursors:
                raise ProviderError('GMGN full activity lặp cursor.')
            seen_cursors.add(str(next_cursor))
            cursor = next_cursor
            time.sleep(max(0.0, page_delay))

        events.sort(key=lambda row: (row['timestamp'], row['tx_hash'], row['event_type']))
        return {
            'events': events,
            'pages': pages,
            'next_cursor': next_cursor,
            'boundary_cursor': cursor,
            'reached_target': bool(
                not next_cursor or (oldest is not None and oldest < cutoff)
            ),
            'history_exhausted': not next_cursor,
            'oldest_timestamp': min(
                (row['timestamp'] for row in events), default=None,
            ),
        }

    def fetch_daily_realized_calendar(
        self,
        wallet: str,
        days: int = 365,
        max_pages: int = 200,
    ) -> Tuple[List[dict], List[dict]]:
        """Estimate daily realized PnL from GMGN sell activities for a rolling window."""
        cursor = None
        seen_cursors = set()
        seen_activities = set()
        daily: Dict[str, Dict[str, Any]] = {}
        pages: List[dict] = []
        cutoff = int(time.time()) - max(1, days) * 86400

        for _ in range(max_pages):
            args = [
                'portfolio', 'activity',
                '--chain', 'sol',
                '--wallet', wallet,
                '--limit', '50',
                '--type', 'sell',
            ]
            if cursor:
                args += ['--cursor', str(cursor)]
            payload = self._run(args)
            activities, next_cursor = self._unwrap_list(
                payload,
                list_keys=('activities', 'list'),
            )
            pages.append({
                'params': {'cursor': cursor, 'type': 'sell', 'days': days},
                'payload': payload,
                'count': len(activities),
            })

            oldest = None
            for activity in activities:
                timestamp = int(activity.get('timestamp') or 0)
                if timestamp:
                    oldest = timestamp if oldest is None else min(oldest, timestamp)
                if timestamp < cutoff:
                    continue
                # One swap can contain multiple activity legs with the same tx hash.
                # Keep each distinct leg, while removing exact pagination duplicates.
                token = activity.get('token') or {}
                activity_key = (
                    str(activity.get('tx_hash') or ''),
                    str(token.get('token_address') or ''),
                    str(activity.get('token_amount') or ''),
                    str(activity.get('quote_address') or ''),
                    str(activity.get('quote_amount') or ''),
                )
                if activity_key in seen_activities:
                    continue
                seen_activities.add(activity_key)

                # GMGN omits buy_cost_usd for routing/intermediate assets. Treating
                # the missing basis as zero materially overstates realized profit.
                raw_cost_basis = activity.get('buy_cost_usd')
                if raw_cost_basis is None:
                    continue
                proceeds = float(activity.get('cost_usd') or 0)
                cost_basis = float(raw_cost_basis)
                gas = float(activity.get('gas_usd') or 0)
                day = datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
                bucket = daily.setdefault(day, {
                    'date': day,
                    'realized_pnl_usd': 0.0,
                    'profit_usd': 0.0,
                    'loss_usd': 0.0,
                    'sell_tx_count': 0,
                    'sell_volume_usd': 0.0,
                    '_tx_hashes': set(),
                })
                realized_pnl = proceeds - cost_basis - gas
                bucket['realized_pnl_usd'] += realized_pnl
                if realized_pnl >= 0:
                    bucket['profit_usd'] += realized_pnl
                else:
                    bucket['loss_usd'] += abs(realized_pnl)
                tx_hash = str(activity.get('tx_hash') or '')
                if tx_hash:
                    bucket['_tx_hashes'].add(tx_hash)
                bucket['sell_volume_usd'] += proceeds

            if not next_cursor or (oldest is not None and oldest < cutoff):
                cursor = None
                break
            if str(next_cursor) in seen_cursors:
                raise ProviderError('GMGN activity lặp cursor.')
            seen_cursors.add(str(next_cursor))
            cursor = next_cursor

        if cursor:
            raise ProviderError('GMGN activity vượt giới hạn phân trang an toàn.')

        rows = []
        for row in sorted(daily.values(), key=lambda item: item['date']):
            rows.append({
                'date': row['date'],
                'realized_pnl_usd': round(row['realized_pnl_usd'], 2),
                'profit_usd': round(row['profit_usd'], 2),
                'loss_usd': round(row['loss_usd'], 2),
                'sell_tx_count': len(row['_tx_hashes']),
                'sell_volume_usd': round(row['sell_volume_usd'], 2),
                'is_estimated': True,
                'source': 'gmgn_sell_activity',
            })
        return rows, pages
