"""Small read-only Solscan Pro client for enrichment data."""
import os
import time
from typing import Dict, List, Optional

from app.runtime import ProviderError


class SolscanPlanError(ProviderError):
    """The key is valid, but its subscription cannot access a Pro endpoint."""


class SolscanRateLimitError(ProviderError):
    """The provider exhausted retries and the caller should pause the batch."""


class SolscanClient:
    base_url = 'https://pro-api.solscan.io/v2.0'
    playground_url = 'https://pro-api.solscan.io/playground'

    def __init__(
        self, api_key: Optional[str] = None, request_delay: float = 0.0,
    ):
        self.api_key = api_key or os.getenv('SOLSCAN_API_KEY')
        if not self.api_key:
            raise ProviderError('SOLSCAN_API_KEY chưa được cấu hình.')
        self.pro_available: Optional[bool] = None
        self.request_delay = max(0.0, float(request_delay))
        self._last_request_at: Optional[float] = None

    def _throttle(self) -> None:
        """Keep every HTTP attempt at least request_delay seconds apart."""
        if self._last_request_at is not None and self.request_delay > 0:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.request_delay:
                time.sleep(self.request_delay - elapsed)
        self._last_request_at = time.monotonic()

    def _get(self, path: str, params: dict, base_url: Optional[str] = None) -> dict:
        try:
            from curl_cffi import requests
        except ImportError:
            raise ProviderError('Thiếu curl_cffi để gọi Solscan Pro.') from None
        is_playground = (base_url or self.base_url) == self.playground_url
        max_attempts = 7 if is_playground else 5
        for attempt in range(max_attempts):
            self._throttle()
            try:
                response = requests.get(
                    f'{base_url or self.base_url}{path}',
                    params=params,
                    headers={'token': self.api_key, 'accept': 'application/json'},
                    impersonate='chrome',
                    timeout=60,
                )
            except Exception:
                if attempt < max_attempts - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise ProviderError('Không kết nối được Solscan Pro.') from None
            if response.status_code == 429:
                if attempt < max_attempts - 1:
                    fallback = 15 * (2 ** attempt) if is_playground else 5
                    retry_after = int(response.headers.get('retry-after') or fallback)
                    time.sleep(min(120, max(1, retry_after)))
                    continue
                raise SolscanRateLimitError(
                    'Solscan Playground vẫn HTTP 429 sau nhiều lần retry; '
                    'dừng các crawler song song và chạy lại sau.'
                    if is_playground else
                    'Solscan Pro vẫn HTTP 429 sau nhiều lần retry.'
                )
            if response.status_code in (401, 403):
                message = ''
                try:
                    error_payload = response.json()
                    errors = error_payload.get('errors') or {}
                    message = str(errors.get('message') or error_payload.get('message') or '')
                except (TypeError, ValueError):
                    pass
                if 'upgrade' in message.lower():
                    raise SolscanPlanError(
                        'Solscan API key hợp lệ nhưng gói hiện tại không có quyền Pro.'
                    )
                raise ProviderError('Solscan từ chối API key; hãy tạo/rotate API key mới.')
            if response.status_code != 200:
                raise ProviderError(f'Solscan trả HTTP {response.status_code}.')
            try:
                payload = response.json()
            except ValueError:
                raise ProviderError('Solscan không trả JSON hợp lệ.') from None
            if not isinstance(payload, dict) or payload.get('success') is False:
                raise ProviderError(f'Solscan trả dữ liệu lỗi: {str(payload)[:200]}')
            return payload
        raise ProviderError('Solscan đang giới hạn tốc độ.')

    def fetch_funded_by(self, wallets: List[str]) -> Dict[str, dict]:
        result: Dict[str, dict] = {}
        for offset in range(0, len(wallets), 50):
            batch = wallets[offset:offset + 50]
            if getattr(self, 'pro_available', None) is False:
                for address in batch:
                    result[address] = {
                        'address': address,
                        'funded_by': None,
                        'block_time': None,
                        'availability': 'requires_paid_solscan_plan',
                    }
                continue
            try:
                payload = self._get('/account/funded-by', {'address': batch})
            except SolscanPlanError:
                self.pro_available = False
                for address in batch:
                    result[address] = {
                        'address': address,
                        'funded_by': None,
                        'block_time': None,
                        'availability': 'requires_paid_solscan_plan',
                    }
                continue
            self.pro_available = True
            rows = payload.get('data') or []
            if isinstance(rows, dict):
                rows = [rows]
            for row in rows:
                address = str(row.get('address') or '')
                if address:
                    result[address] = row
        return result

    def fetch_account_transfers(
        self,
        wallet: str,
        from_time: int,
        to_time: Optional[int] = None,
        start_page: int = 1,
        max_pages: int = 100,
        start_cursor: Optional[str] = None,
    ) -> dict:
        """Fetch a stable descending transfer window with page checkpointing."""
        fixed_to = int(to_time or time.time())
        rows = []
        page = max(1, int(start_page))
        pages_fetched = 0
        complete = False
        for _ in range(max_pages):
            if getattr(self, 'pro_available', None) is False:
                return self.fetch_enhanced_transactions(
                    wallet,
                    from_time=from_time,
                    to_time=fixed_to,
                    start_cursor=start_cursor,
                    max_pages=max_pages,
                )
            try:
                payload = self._get('/account/transfer', {
                    'address': wallet,
                    'from_time': int(from_time),
                    'to_time': fixed_to,
                    'page': page,
                    'page_size': 100,
                    'sort_by': 'block_time',
                    'sort_order': 'desc',
                    'exclude_amount_zero': 'true',
                })
            except SolscanPlanError:
                self.pro_available = False
                return self.fetch_enhanced_transactions(
                    wallet,
                    from_time=from_time,
                    to_time=fixed_to,
                    start_cursor=start_cursor,
                    max_pages=max_pages,
                )
            except SolscanRateLimitError as exc:
                if not pages_fetched:
                    raise
                return {
                    'rows': rows,
                    'complete': False,
                    'next_page': page,
                    'from_time': int(from_time),
                    'to_time': fixed_to,
                    'pages_fetched': pages_fetched,
                    'next_cursor': None,
                    'data_mode': 'pro_account_transfers',
                    'rate_limited': True,
                    'error': str(exc),
                }
            self.pro_available = True
            batch = payload.get('data') or []
            if not isinstance(batch, list):
                raise ProviderError('Solscan account/transfer có schema không hợp lệ.')
            rows.extend(batch)
            pages_fetched += 1
            if len(batch) < 100:
                complete = True
                break
            page += 1
        return {
            'rows': rows,
            'complete': complete,
            'next_page': None if complete else page,
            'from_time': int(from_time),
            'to_time': fixed_to,
            'pages_fetched': pages_fetched,
            'next_cursor': None,
            'data_mode': 'pro_account_transfers',
        }

    def fetch_enhanced_transactions(
        self,
        wallet: str,
        from_time: int,
        to_time: Optional[int] = None,
        start_cursor: Optional[str] = None,
        max_pages: int = 100,
    ) -> dict:
        """Use Solscan's free playground endpoint when Pro access is unavailable."""
        fixed_to = int(to_time or time.time())
        cursor = start_cursor
        rows = []
        pages_fetched = 0
        complete = False
        for _ in range(max_pages):
            params = {
                'address': wallet,
                'from_time': int(from_time),
                'to_time': fixed_to,
                'encoding': 'jsonParsed',
            }
            if cursor:
                params['cursor'] = cursor
            try:
                payload = self._get(
                    '/account/transactions/enhanced', params,
                    base_url=self.playground_url,
                )
            except SolscanRateLimitError as exc:
                if not pages_fetched:
                    raise
                return {
                    'rows': rows,
                    'complete': False,
                    'next_page': None,
                    'next_cursor': cursor,
                    'from_time': int(from_time),
                    'to_time': fixed_to,
                    'pages_fetched': pages_fetched,
                    'data_mode': 'playground_enhanced_transactions',
                    'rate_limited': True,
                    'error': str(exc),
                }
            data = payload.get('data') or {}
            if not isinstance(data, dict):
                raise ProviderError('Solscan enhanced transactions có schema không hợp lệ.')
            batch = data.get('transactions') or []
            if not isinstance(batch, list):
                raise ProviderError('Solscan enhanced transactions thiếu danh sách transaction.')
            rows.extend(batch)
            pages_fetched += 1
            next_cursor = data.get('cursor')
            if not next_cursor or not batch:
                complete = True
                cursor = None
                break
            if str(next_cursor) == str(cursor):
                raise ProviderError('Solscan enhanced transactions lặp cursor.')
            cursor = str(next_cursor)
        return {
            'rows': rows,
            'complete': complete,
            'next_page': None,
            'next_cursor': cursor,
            'from_time': int(from_time),
            'to_time': fixed_to,
            'pages_fetched': pages_fetched,
            'data_mode': 'playground_enhanced_transactions',
        }
