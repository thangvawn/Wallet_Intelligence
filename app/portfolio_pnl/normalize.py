"""Normalize GMGN holdings rows into Recent PnL table schema (matches GMGN UI)."""
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


TINY_POOL_USD = 5_000.0


def _float(value: Any) -> Optional[float]:
    if value is None or value == '':
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _first(row: dict, *keys: str):
    for key in keys:
        if row.get(key) is not None:
            return row.get(key)
    return None


def _duration_display(seconds: Optional[int]) -> str:
    if seconds is None or seconds <= 0:
        return ''
    if seconds < 3600:
        return f'{max(1, seconds // 60)}m'
    if seconds < 86400:
        return f'{max(1, seconds // 3600)}h'
    return f'{max(1, seconds // 86400)}d'


def _last_active_display(ts: Optional[int]) -> str:
    if not ts:
        return ''
    delta = max(0, int(datetime.now(tz=timezone.utc).timestamp()) - int(ts))
    return _duration_display(delta)


def _ratio_to_pct(value: Optional[float]) -> Optional[float]:
    """GMGN *_profit_pnl fields are ratios (6.896 = 689.6%)."""
    if value is None:
        return None
    return value * 100.0


def apply_gmgn_display_flags(row: Dict[str, Any]) -> Dict[str, Any]:
    """Derive Sell All / HODL / filter flags from a normalized (or cached) row."""
    balance = float(row.get('balance') or 0)
    balance_usd = float(row.get('balance_usd') or 0)
    buy_tx = int(row.get('buy_tx_count') or 0)
    sell_tx = int(row.get('sell_tx_count') or 0)
    sold_usd = float(row.get('sold_usd') or 0)
    bought_usd = float(row.get('bought_usd') or 0)
    liq = row.get('liquidity_usd')

    is_closed = bool(row.get('is_closed')) or (balance <= 0 and sold_usd > 0 and balance_usd <= 0.01)
    didnt_buy = buy_tx <= 0 and bought_usd <= 0
    is_hodl = (not is_closed) and sell_tx <= 0 and buy_tx > 0
    is_tiny_pool = liq is not None and float(liq) < TINY_POOL_USD

    row['is_closed'] = is_closed
    row['didnt_buy'] = didnt_buy
    row['is_hodl'] = is_hodl
    row['is_tiny_pool'] = is_tiny_pool
    row['unrealized_status'] = 'sell_all' if is_closed else 'open'
    row['realized_status'] = 'hodl' if is_hodl else 'realized'
    return row


def normalize_holdings_row(raw: dict, wallet_address: str, wallet_name: str = '') -> Dict[str, Any]:
    token = raw.get('token') or {}
    token_address = _first(token, 'token_address', 'address') or ''
    symbol = token.get('symbol') or ''
    name = token.get('name') or symbol

    bought_usd = _float(_first(raw, 'history_bought_cost', 'accu_cost'))
    sold_usd = _float(_first(raw, 'history_sold_income'))
    realized_usd = _float(_first(raw, 'realized_profit'))
    unrealized_usd = _float(_first(raw, 'unrealized_profit'))
    total_usd = _float(_first(raw, 'total_profit'))
    if total_usd is None and realized_usd is not None and unrealized_usd is not None:
        total_usd = realized_usd + unrealized_usd

    realized_pct = _ratio_to_pct(_float(_first(raw, 'realized_profit_pnl', 'realized_pnl')))
    unrealized_pct = _ratio_to_pct(_float(_first(raw, 'unrealized_profit_pnl')))
    total_pct = _ratio_to_pct(_float(_first(raw, 'total_profit_pnl', 'profit_change')))

    balance = _float(raw.get('balance')) or 0.0
    balance_usd = _float(raw.get('usd_value')) or 0.0
    buy_tx = _int(_first(raw, 'history_total_buys', 'buy_tx_count'))
    sell_tx = _int(_first(raw, 'history_total_sells', 'sell_tx_count'))
    transfer_in = _int(_first(raw, 'history_total_transfer_ins'))
    transfer_out = _int(_first(raw, 'history_total_transfer_outs'))

    start_ts = _int(_first(raw, 'start_holding_at'))
    end_ts = _int(_first(raw, 'end_holding_at'))
    creation_ts = _int(_first(token, 'creation_timestamp', 'open_timestamp'))
    last_active_ts = _int(_first(raw, 'last_active_timestamp')) or end_ts or start_ts
    token_age_at_entry_hours = None
    if start_ts and creation_ts and start_ts >= creation_ts:
        token_age_at_entry_hours = (start_ts - creation_ts) / 3600.0
    holding_seconds = None
    if start_ts and end_ts and end_ts >= start_ts:
        holding_seconds = end_ts - start_ts
    elif start_ts and balance_usd > 0:
        holding_seconds = max(0, int(datetime.now(tz=timezone.utc).timestamp()) - start_ts)

    bought_mc = _float(_first(raw, 'avg_buy_market_cap', 'buy_market_cap', 'history_bought_market_cap'))
    sold_mc = _float(_first(raw, 'avg_sell_market_cap', 'sell_market_cap', 'history_sold_market_cap'))
    first_buy_timestamp = _int(_first(raw, 'first_buy_timestamp', 'wallet_first_buy_time', 'start_holding_at'))
    first_buy_amount_usd = _float(_first(
        raw, 'first_buy_amount_usd', 'wallet_first_buy_amount_usd',
        'history_first_bought_cost', 'first_buy_cost_usd',
    ))
    first_buy_price = _float(_first(raw, 'first_buy_price', 'wallet_first_buy_price', 'avg_buy_price'))
    price = _float(token.get('price'))
    supply = _float(_first(token, 'total_supply', 'max_supply'))
    current_mc = None
    if price is not None and supply is not None and supply > 0:
        current_mc = price * supply
    if bought_mc is None:
        bought_mc = _float(token.get('avg_buy_market_cap'))
    if sold_mc is None:
        sold_mc = _float(token.get('avg_sell_market_cap'))

    # GMGN does not expose avg MC directly in every response. Derive the
    # historical average execution price from cost/amount, then multiply supply.
    bought_amount = _float(raw.get('history_bought_amount'))
    sold_amount = _float(raw.get('history_sold_amount'))
    if bought_mc is None and supply and bought_amount and bought_amount > 0 and bought_usd is not None:
        bought_mc = (bought_usd / bought_amount) * supply
    if sold_mc is None and supply and sold_amount and sold_amount > 0 and sold_usd is not None:
        sold_mc = (sold_usd / sold_amount) * supply
    if first_buy_price is None and first_buy_amount_usd is not None:
        first_buy_token_amount = _float(_first(raw, 'first_buy_token_amount', 'wallet_first_buy_token_amount'))
        if first_buy_token_amount and first_buy_token_amount > 0:
            first_buy_price = first_buy_amount_usd / first_buy_token_amount
    if first_buy_amount_usd is None and first_buy_price is not None and bought_amount:
        # This fallback is explicitly total cost only when a first-order value is
        # unavailable; activity events can replace it during group classification.
        first_buy_amount_usd = bought_usd

    liquidity = _float(token.get('liquidity'))
    tags = raw.get('wallet_token_tags')
    if isinstance(tags, str):
        tags = [tags]
    elif not isinstance(tags, list):
        tags = []

    row = {
        'wallet_address': wallet_address,
        'wallet_name': wallet_name,
        'token_address': token_address,
        'token_symbol': symbol,
        'token_name': name,
        'token_logo': token.get('logo') or '',
        'last_active_timestamp': last_active_ts or None,
        'last_active_display': _last_active_display(last_active_ts),
        'start_holding_timestamp': start_ts or None,
        'end_holding_timestamp': end_ts or None,
        'token_creation_timestamp': creation_ts or None,
        # Canonical names consumed by wallet group classification.  Keep the
        # existing fields above for backwards compatibility with the dashboard.
        'token_launch_time': creation_ts or None,
        'first_buy_timestamp': first_buy_timestamp or start_ts or None,
        'wallet_first_buy_time': first_buy_timestamp or start_ts or None,
        'wallet_first_buy_price': first_buy_price,
        'market_cap_at_first_buy_usd': bought_mc,
        'first_buy_amount_usd': first_buy_amount_usd,
        'total_position_usd': bought_usd,
        'token_age_at_entry_hours': token_age_at_entry_hours,
        'unrealized_profit_usd': unrealized_usd,
        'unrealized_profit_pct': unrealized_pct,
        'realized_profit_usd': realized_usd,
        'realized_profit_pct': realized_pct,
        'total_profit_usd': total_usd,
        'total_profit_pct': total_pct,
        'balance': balance,
        'balance_usd': balance_usd,
        'position_pct': None,
        'holding_duration_seconds': holding_seconds,
        'holding_duration_display': _duration_display(holding_seconds),
        'bought_usd': bought_usd,
        'bought_mc': bought_mc,
        'sold_usd': sold_usd,
        'sold_mc': sold_mc,
        'current_mc': current_mc,
        'liquidity_usd': liquidity,
        'buy_tx_count': buy_tx,
        'sell_tx_count': sell_tx,
        'transfer_in_count': transfer_in,
        'transfer_out_count': transfer_out,
        'txs_display': f'{buy_tx}/{sell_tx}',
        'is_closed': balance <= 0 and (sold_usd or 0) > 0,
        'is_honeypot': bool(token.get('is_honeypot')),
        'is_alert': bool(token.get('is_show_alert')),
        'launchpad_platform': token.get('launchpad_platform') or token.get('launchpad') or '',
        'wallet_token_tags': tags,
        'data_source': 'gmgn_holdings',
        'raw_fields_present': sorted(raw.keys()),
    }
    return apply_gmgn_display_flags(row)


def normalize_holdings_table(
    raw_rows: List[dict],
    wallet_address: str,
    wallet_name: str = '',
) -> List[Dict[str, Any]]:
    unique_rows: Dict[str, dict] = {}
    anonymous_rows: List[dict] = []
    for raw in raw_rows:
        token = raw.get('token') or {}
        address = _first(token, 'token_address', 'address')
        if address:
            # GMGN pagination can repeat the boundary token. Pages are sorted
            # descending, so the first occurrence matches the visible table.
            unique_rows.setdefault(str(address), raw)
        else:
            anonymous_rows.append(raw)
    rows = [
        normalize_holdings_row(r, wallet_address, wallet_name)
        for r in [*unique_rows.values(), *anonymous_rows]
    ]
    total_open_usd = sum((r.get('balance_usd') or 0.0) for r in rows)
    for row in rows:
        if total_open_usd > 0 and (row.get('balance_usd') or 0) > 0:
            row['position_pct'] = round((row['balance_usd'] / total_open_usd) * 100.0, 2)
        elif row.get('balance_usd', 0) <= 0:
            row['position_pct'] = 0.0
        apply_gmgn_display_flags(row)
    rows.sort(key=lambda r: (r.get('total_profit_usd') or 0.0), reverse=True)
    return rows


def enrich_cached_rows(rows: List[dict]) -> List[dict]:
    """Recompute GMGN display flags on already-exported JSON rows."""
    total_open_usd = sum(float(r.get('balance_usd') or 0) for r in rows)
    out = []
    for row in rows:
        row = dict(row)
        if total_open_usd > 0 and float(row.get('balance_usd') or 0) > 0:
            row['position_pct'] = round((float(row['balance_usd']) / total_open_usd) * 100.0, 2)
        apply_gmgn_display_flags(row)
        out.append(row)
    return out


def normalize_wallet_summary(profits_row: dict, wallet_address: str, wallet_name: str = '') -> Dict[str, Any]:
    realized = _float(_first(profits_row, 'realized_profit', 'total_realized_profit'))
    unrealized = _float(profits_row.get('unrealized_profit'))
    total = _float(profits_row.get('total_profit'))
    total_cost = _float(profits_row.get('total_cost'))
    return {
        'wallet_address': wallet_address,
        'wallet_name': wallet_name,
        'realized_profit_usd': realized,
        'unrealized_profit_usd': unrealized,
        'total_profit_usd': total,
        'total_cost_usd': total_cost,
        'buy_count': _int(profits_row.get('buy')),
        'sell_count': _int(profits_row.get('sell')),
        'data_source': 'gmgn_profits',
    }


def normalize_period_profit(
    profits_row: dict,
    period: str,
    wallet_address: str,
) -> Dict[str, Any]:
    """Normalize GMGN profits for 1d/7d/30d/all without inventing unsupported periods."""
    realized = _float(profits_row.get('realized_profit')) or 0.0
    realized_cost = _float(profits_row.get('realized_profit_cost')) or 0.0
    unrealized = _float(profits_row.get('unrealized_profit')) or 0.0
    total = _float(profits_row.get('total_profit'))
    total_cost = _float(profits_row.get('total_cost'))
    roi = (realized / realized_cost * 100.0) if realized_cost > 0 else None
    days = {'1d': 1, '7d': 7, '30d': 30}.get(period)
    return {
        'period': period,
        'wallet_address': wallet_address,
        'realized_profit_usd': realized,
        'realized_cost_usd': realized_cost,
        'realized_roi_pct': round(roi, 2) if roi is not None else None,
        'unrealized_profit_usd': unrealized,
        'total_profit_usd': total,
        'total_cost_usd': total_cost,
        'buy_count': _int(profits_row.get('buy')),
        'sell_count': _int(profits_row.get('sell')),
        'avg_daily_realized_profit_usd': round(realized / days, 2) if days else None,
        'data_source': 'gmgn_profits',
    }
