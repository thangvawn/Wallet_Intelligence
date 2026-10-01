"""Minimal Solana JSON-RPC client for first-funding evidence."""
import time
from typing import Optional

from app.runtime import ProviderError


STABLECOIN_MINTS = {
    'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v': 'USDC',
    'Es9vMFrzaCERmJfrF4H2FYD8VCX8pK9n3WPG8VnR7pA': 'USDT',
}

JITO_TIP_ACCOUNTS = {
    '96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5',
    'HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe',
    'Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY',
    'ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49',
    'DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh',
    'ADuUkR4vqLUMWXxW9gh6D6L8pWHLnjvnxpmwBXbeZcvP',
    'DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL',
    '3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT',
}

KNOWN_CEX_ADDRESSES = {
    # Binance
    '5tzFkiKscBizbkBDZe52TXxwQ614cqJPrZioN93tKrsC': 'Binance',
    '9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM': 'Binance',
    '2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S': 'Binance',
    '3yFwqWBydLC4GMxGUQZkL6bhn3dC8QYpBmWjZ1FvF1mQ': 'Binance',
    # Coinbase
    'H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS': 'Coinbase',
    '2AQdpHJ2JpcEgPiATUXjQxA8QmafFegfQwSLWSprPicm': 'Coinbase',
    'GJRs4FwHtemZ5ZE9x3FNvJ83gwPx2XDXRczLd6j4g58m': 'Coinbase',
    # OKX
    '5VCwKtCXgCJ6kit5FybXjvriW3xJMsFD8ydK6LocFx95': 'OKX',
    '6H6Wc45Sg7kY9a72K7L3mB812P2B6H54nK3wE4m4uQ': 'OKX',
    # Bybit
    'AC5RDfQFmDS1deCrptYp636Bv66aJ26mQ6fUfH4mEa8u': 'Bybit',
    # Kraken
    'FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5': 'Kraken',
    # Kucoin
    'BmFdpraQhkiDQE6f4hyReJwmgdZ2ahQK5P88488ErLwc': 'Kucoin',
    # MEXC
    'ASTyfSima4LLAdDgoFGkgqoKowG1LZFDr9fAQrg7iaJZ': 'MEXC',
    # Gate.io
    'u6PJ8DtQuPFnfmwHbGFULQ4u4EgsvV8M5i7edA7g69A': 'Gate.io',
}


class SolanaRpcClient:
    def __init__(
        self,
        url: str = 'https://api.mainnet-beta.solana.com',
        request_delay: float = 1.0,
    ):
        self.url = url
        self.request_delay = max(0.0, float(request_delay))
        self._last_request_at: Optional[float] = None
        self._request_id = 0

    def _call(self, method: str, params: list):
        try:
            from curl_cffi import requests
        except ImportError:
            raise ProviderError('Thiếu curl_cffi để gọi Solana RPC.') from None

        for attempt in range(5):
            if self._last_request_at is not None:
                remaining = self.request_delay - (
                    time.monotonic() - self._last_request_at
                )
                if remaining > 0:
                    time.sleep(remaining)
            self._last_request_at = time.monotonic()
            self._request_id += 1
            try:
                response = requests.post(
                    self.url,
                    json={
                        'jsonrpc': '2.0',
                        'id': self._request_id,
                        'method': method,
                        'params': params,
                    },
                    headers={'content-type': 'application/json'},
                    timeout=60,
                )
            except Exception:
                if attempt < 4:
                    time.sleep(min(30, 2 ** attempt))
                    continue
                raise ProviderError('Không kết nối được Solana RPC.') from None
            if response.status_code == 429:
                if attempt < 4:
                    wait = int(response.headers.get('retry-after') or 2 ** (attempt + 1))
                    time.sleep(min(60, max(1, wait)))
                    continue
                raise ProviderError('Solana RPC vẫn HTTP 429 sau nhiều lần retry.')
            if response.status_code != 200:
                raise ProviderError(f'Solana RPC trả HTTP {response.status_code}.')
            payload = response.json()
            if payload.get('error'):
                raise ProviderError(f"Solana RPC lỗi: {payload['error']}")
            return payload.get('result')
        raise ProviderError('Solana RPC không trả dữ liệu.')

    def oldest_signature(
        self, wallet: str, before: Optional[str] = None, max_pages: int = 100,
    ) -> dict:
        cursor = before
        oldest = None
        pages = 0
        signatures = 0
        complete = False
        for _ in range(max_pages):
            config = {'limit': 1000}
            if cursor:
                config['before'] = cursor
            batch = self._call('getSignaturesForAddress', [wallet, config]) or []
            if not isinstance(batch, list):
                raise ProviderError('Solana RPC signature history sai schema.')
            pages += 1
            signatures += len(batch)
            if batch:
                oldest = batch[-1]
                cursor = str(oldest.get('signature') or '') or None
            if len(batch) < 1000:
                complete = True
                break
        return {
            'complete': complete,
            'next_before': None if complete else cursor,
            'oldest': oldest,
            'pages_fetched': pages,
            'signatures_fetched': signatures,
        }

    def transaction(self, signature: str) -> Optional[dict]:
        return self._call('getTransaction', [signature, {
            'encoding': 'jsonParsed',
            'maxSupportedTransactionVersion': 0,
        }])

    def find_initial_funding(
        self, wallet: str, max_pages: int = 20, scan_transactions: int = 50,
    ) -> dict:
        """Scan the oldest transactions for the first measurable capital inflow."""
        cursor = None
        signatures = []
        pages = 0
        complete = False
        for _ in range(max_pages):
            config = {'limit': 1000}
            if cursor:
                config['before'] = cursor
            batch = self._call('getSignaturesForAddress', [wallet, config]) or []
            if not isinstance(batch, list):
                raise ProviderError('Solana RPC signature history sai schema.')
            signatures.extend(batch)
            pages += 1
            if len(batch) < 1000:
                complete = True
                break
            cursor = str(batch[-1].get('signature') or '') or None
        if not complete:
            return {
                'complete': False,
                'pages_fetched': pages,
                'signatures_fetched': len(signatures),
                'funding': None,
            }

        candidates = list(reversed(signatures))[:max(1, scan_transactions)]
        fallback = None
        transactions_scanned = 0
        for row in candidates:
            signature = str(row.get('signature') or '')
            if not signature:
                continue
            tx = self.transaction(signature)
            transactions_scanned += 1
            parsed = parse_initial_funding(wallet, row, tx or {})
            fallback = fallback or parsed
            if parsed.get('fund_amount') or parsed.get('fund_amount_usd'):
                return {
                    'complete': True,
                    'pages_fetched': pages,
                    'signatures_fetched': len(signatures),
                    'transactions_scanned': transactions_scanned,
                    'funding': parsed,
                }
        return {
            'complete': True,
            'pages_fetched': pages,
            'signatures_fetched': len(signatures),
            'transactions_scanned': transactions_scanned,
            'funding': fallback,
        }


def parse_initial_funding(wallet: str, signature_row: dict, tx: dict) -> dict:
    """Extract native SOL received by the wallet in its oldest transaction."""
    transaction = (tx or {}).get('transaction') or {}
    message = transaction.get('message') or {}
    keys = message.get('accountKeys') or []
    addresses = [
        str(item.get('pubkey') or '') if isinstance(item, dict) else str(item)
        for item in keys
    ]
    source = None
    lamports = 0
    instructions = list(message.get('instructions') or [])
    for group in ((tx or {}).get('meta') or {}).get('innerInstructions') or []:
        instructions.extend(group.get('instructions') or [])
    for instruction in instructions:
        parsed = instruction.get('parsed') if isinstance(instruction, dict) else None
        if not isinstance(parsed, dict):
            continue
        info = parsed.get('info') or {}
        if not isinstance(info, dict):
            continue
        if str(info.get('destination') or '') != wallet:
            continue
        amount = int(info.get('lamports') or 0)
        if amount > 0:
            lamports += amount
            source = source or str(info.get('source') or '') or None

    # Some RPC nodes omit parsed instructions. Balance delta is still direct
    # evidence for native SOL received by a non-fee-paying destination.
    if lamports <= 0 and wallet in addresses:
        index = addresses.index(wallet)
        meta = (tx or {}).get('meta') or {}
        pre = meta.get('preBalances') or []
        post = meta.get('postBalances') or []
        if index < len(pre) and index < len(post):
            lamports = max(0, int(post[index]) - int(pre[index]))
    if not source:
        for item in keys:
            if isinstance(item, dict) and item.get('signer'):
                candidate = str(item.get('pubkey') or '')
                if candidate and candidate != wallet:
                    source = candidate
                    break

    stablecoin_usd = 0.0
    stablecoin_symbol = None
    meta = (tx or {}).get('meta') or {}

    def token_totals(rows):
        totals = {}
        for row in rows or []:
            if str(row.get('owner') or '') != wallet:
                continue
            mint = str(row.get('mint') or '')
            if mint not in STABLECOIN_MINTS:
                continue
            amount = row.get('uiTokenAmount') or {}
            try:
                raw = int(amount.get('amount') or 0)
                decimals = int(amount.get('decimals') or 0)
                value = raw / (10 ** decimals)
            except (TypeError, ValueError, ZeroDivisionError):
                value = 0.0
            totals[mint] = totals.get(mint, 0.0) + value
        return totals

    pre_tokens = token_totals(meta.get('preTokenBalances'))
    post_tokens = token_totals(meta.get('postTokenBalances'))
    for mint, post_amount in post_tokens.items():
        increase = post_amount - pre_tokens.get(mint, 0.0)
        if increase > stablecoin_usd:
            stablecoin_usd = increase
            stablecoin_symbol = STABLECOIN_MINTS[mint]

    return {
        'address': wallet,
        'funded_by': source,
        'block_time': int(
            (tx or {}).get('blockTime')
            or (signature_row or {}).get('blockTime')
            or 0
        ),
        'fund_amount': round(lamports / 1_000_000_000, 9) if lamports > 0 else None,
        'fund_amount_usd': round(stablecoin_usd, 6) if stablecoin_usd > 0 else None,
        'fund_asset': 'SOL' if lamports > 0 else stablecoin_symbol,
        'signature': str((signature_row or {}).get('signature') or ''),
        'availability': 'solana_rpc_oldest_transaction',
    }


def parse_transaction_enrichment(wallet: str, tx: dict) -> dict:
    """Extract complete fee breakdown, pre/post balances, and counterparty transfer details."""
    if not tx or not isinstance(tx, dict):
        return {}
    meta = tx.get('meta') or {}
    transaction = tx.get('transaction') or {}
    message = transaction.get('message') or {}
    account_keys = message.get('accountKeys') or []
    pubkeys = [
        str(k.get('pubkey') or '') if isinstance(k, dict) else str(k)
        for k in account_keys
    ]

    # 1. Base + Priority Fees
    fee_lamports = int(meta.get('fee') or 0)
    fee_sol = fee_lamports / 1e9

    # 2. Jito MEV Tips & Transfers
    jito_tip_lamports = 0
    instructions = list(message.get('instructions') or [])
    for inner in meta.get('innerInstructions') or []:
        instructions.extend(inner.get('instructions') or [])

    # Map account pubkey to owner if known from token balances
    account_owners = {wallet: wallet}
    for tb in (meta.get('preTokenBalances') or []) + (meta.get('postTokenBalances') or []):
        owner = str(tb.get('owner') or '')
        idx = tb.get('accountIndex')
        if owner and idx is not None and idx < len(pubkeys):
            account_owners[pubkeys[idx]] = owner

    transfer_details = None
    for ix in instructions:
        parsed = ix.get('parsed') if isinstance(ix, dict) else None
        if not isinstance(parsed, dict):
            continue
        ix_type = parsed.get('type')
        info = parsed.get('info') or {}
        if not isinstance(info, dict):
            continue

        # Check Jito Tip
        if ix_type == 'transfer':
            destination = str(info.get('destination') or '')
            if destination in JITO_TIP_ACCOUNTS:
                jito_tip_lamports += int(info.get('lamports') or 0)

        # Check Token or Native Transfer concerning wallet
        if ix_type in ('transfer', 'transferChecked'):
            source_acc = str(info.get('source') or '')
            dest_acc = str(info.get('destination') or '')
            authority = str(info.get('authority') or '')
            source_owner = account_owners.get(source_acc, source_acc)
            dest_owner = account_owners.get(dest_acc, dest_acc)
            amount = float(info.get('amount') or info.get('lamports') or 0)
            token_mint = str(info.get('mint') or '')

            # Check if this transfer relates to our wallet (as owner, authority, or account)
            if wallet in (source_acc, dest_acc, source_owner, dest_owner, authority):
                is_deposit = (dest_acc == wallet or dest_owner == wallet)
                is_withdrawal = (source_acc == wallet or source_owner == wallet or authority == wallet)
                
                counterparty = source_owner if is_deposit else dest_owner
                is_cex_deposit = is_deposit and (counterparty in KNOWN_CEX_ADDRESSES or authority in KNOWN_CEX_ADDRESSES)
                is_cex_withdrawal = is_withdrawal and counterparty in KNOWN_CEX_ADDRESSES
                cex_name = (
                    KNOWN_CEX_ADDRESSES.get(counterparty)
                    or KNOWN_CEX_ADDRESSES.get(authority)
                )
                transfer_details = {
                    'source': source_owner,
                    'destination': dest_owner,
                    'source_account': source_acc,
                    'destination_account': dest_acc,
                    'authority': authority,
                    'amount': amount,
                    'mint': token_mint,
                    'is_deposit': is_deposit,
                    'is_withdrawal': is_withdrawal,
                    'is_cex_deposit': bool(is_cex_deposit),
                    'is_cex_withdrawal': bool(is_cex_withdrawal),
                    'cex_name': cex_name,
                    'corroborated': bool(counterparty and counterparty != wallet),
                }

    jito_tip_sol = jito_tip_lamports / 1e9
    total_fee_sol = fee_sol + jito_tip_sol


    # 3. Pre & Post Balances of wallet
    pre_balance_sol = None
    post_balance_sol = None
    if wallet in pubkeys:
        idx = pubkeys.index(wallet)
        pre_bals = meta.get('preBalances') or []
        post_bals = meta.get('postBalances') or []
        if idx < len(pre_bals) and idx < len(post_bals):
            pre_balance_sol = pre_bals[idx] / 1e9
            post_balance_sol = post_bals[idx] / 1e9

    signatures = transaction.get('signatures') or []
    signature = signatures[0] if signatures else ''

    return {
        'signature': signature,
        'block_time': int(tx.get('blockTime') or 0),
        'fee_sol': round(fee_sol, 9),
        'jito_tip_sol': round(jito_tip_sol, 9),
        'total_fee_sol': round(total_fee_sol, 9),
        'pre_balance_sol': round(pre_balance_sol, 9) if pre_balance_sol is not None else None,
        'post_balance_sol': round(post_balance_sol, 9) if post_balance_sol is not None else None,
        'transfer': transfer_details,
        'balance_verified': pre_balance_sol is not None,
    }

