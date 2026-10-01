"""Reconstruct capital & portfolio outcome metrics (Section 5) from GMGN events and balances."""
from datetime import datetime, timezone
import math
import statistics
from typing import Any, Dict, List, Optional


def _num(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        f = float(val)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _estimate_sol_price(events: List[dict]) -> Optional[float]:
    """Derive recent SOL/USD price from activity quote transactions."""
    sol_prices: List[float] = []
    for e in reversed(events):
        amt_usd = _num(e.get('amount_usd') or e.get('cost_usd'))
        quote_amt = _num(e.get('quote_amount'))
        if amt_usd > 1.0 and quote_amt > 0.001:
            price = amt_usd / quote_amt
            if 10.0 <= price <= 1000.0:
                sol_prices.append(price)
                if len(sol_prices) >= 20:
                    break
    if sol_prices:
        return float(statistics.median(sol_prices))
    return None


def calculate_external_flows(events: List[dict]) -> dict:
    """Aggregate deposit (transferIn) and withdrawal (transferOut) flows."""
    total_deposits = 0.0
    total_withdrawals = 0.0
    deposit_count = 0
    withdrawal_count = 0
    flow_by_date: Dict[str, float] = {}

    for e in events:
        etype = str(e.get('event_type') or '').lower()
        cost = _num(e.get('amount_usd') or e.get('cost_usd'))
        ts = int(_num(e.get('timestamp')))
        date_str = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat() if ts else ''

        if etype in ('transferin', 'deposit'):
            total_deposits += cost
            deposit_count += 1
            if date_str:
                flow_by_date[date_str] = flow_by_date.get(date_str, 0.0) + cost
        elif etype in ('transferout', 'withdraw', 'withdrawal'):
            total_withdrawals += cost
            withdrawal_count += 1
            if date_str:
                flow_by_date[date_str] = flow_by_date.get(date_str, 0.0) - cost

    return {
        'total_deposits_usd': round(total_deposits, 2),
        'total_withdrawals_usd': round(total_withdrawals, 2),
        'deposit_count': deposit_count,
        'withdrawal_count': withdrawal_count,
        'net_external_flow_usd': round(total_deposits - total_withdrawals, 2),
        'flow_by_date': flow_by_date,
    }


def reconstruct_equity_outcome(
    events: List[dict],
    holdings_rows: List[dict],
    daily_calendar: List[dict],
    stats_payload: Optional[dict] = None,
    total_profit_usd: Optional[float] = None,
    total_cost_usd: Optional[float] = None,
    activity_history_exhausted: bool = False,
    funding_override: Optional[dict] = None,
    historical_sol_price_usd: Optional[float] = None,
    onchain_verified: bool = False,
) -> dict:
    """Compute complete Section 5 Capital & Portfolio metrics."""
    stats = stats_payload or {}
    common = stats.get('common') or {}

    sol_price = _estimate_sol_price(events)

    # 1. Ending Equity
    open_tokens_val = sum(_num(r.get('balance_usd')) for r in holdings_rows if _num(r.get('balance_usd')) > 0.01)
    native_balance_available = 'native_balance' in stats and stats.get('native_balance') is not None
    native_sol = _num(stats.get('native_balance'))
    native_val = native_sol * sol_price if native_balance_available and sol_price is not None else None
    ending_equity = (
        round(open_tokens_val + native_val, 2)
        if native_val is not None else None
    )

    # 2. External Flows
    flows = calculate_external_flows(events)
    net_flow = flows['net_external_flow_usd']
    flow_by_date = flows['flow_by_date']

    # 3. Starting Equity
    funding = funding_override or {}
    fund_amount = _num(common.get('fund_amount') or funding.get('fund_amount'))
    fund_amount_usd = _num(funding.get('fund_amount_usd'))
    fund_ts = int(_num(common.get('fund_from_ts') or funding.get('block_time')))
    funding_basis = (
        'gmgn_stats'
        if common.get('fund_from_ts') or common.get('fund_amount')
        else str(funding.get('availability') or 'unavailable')
    )

    # Initial deposits during the first 7 days
    early_deposits = 0.0
    if fund_ts > 0:
        cutoff_ts = fund_ts + 7 * 86400
        for e in events:
            if str(e.get('event_type') or '').lower() in ('transferin', 'deposit'):
                if fund_ts <= int(_num(e.get('timestamp'))) <= cutoff_ts:
                    early_deposits += _num(e.get('amount_usd') or e.get('cost_usd'))

    funding_sol_price = (
        _num(historical_sol_price_usd)
        if _num(historical_sol_price_usd) > 0
        else sol_price
    )
    funded_value = (
        fund_amount_usd
        if fund_amount_usd > 0
        else fund_amount * funding_sol_price
        if fund_amount > 0 and funding_sol_price is not None
        else 0.0
    )
    # Funding can also appear in the activity transfer-in list. Do not add
    # both sources and count the same deposit twice.
    starting_equity_basis = funded_value if funded_value > 0 else early_deposits
    starting_equity = round(starting_equity_basis, 2) if starting_equity_basis > 0 else None

    # 4. Daily Equity Curve & Peak / Average Equity
    equity_curve: List[float] = [starting_equity] if starting_equity is not None else []
    current_equity = starting_equity

    sorted_daily = sorted(daily_calendar, key=lambda d: str(d.get('date') or ''))
    if current_equity is not None:
        for d in sorted_daily:
            d_pnl = _num(d.get('realized_pnl_usd'))
            d_flow = flow_by_date.get(str(d.get('date')), 0.0)
            current_equity = max(0.0, current_equity + d_pnl + d_flow)
            equity_curve.append(round(current_equity, 2))

    peak_equity = round(max(equity_curve), 2) if equity_curve else None
    avg_equity = round(statistics.mean(equity_curve), 2) if equity_curve else None

    # 5. True Portfolio Growth & Return %
    # Growth = Ending - Starting - Net Flow
    # Ensures deposits are not confused with trading gains
    earliest_event = min((int(_num(e.get('timestamp'))) for e in events if _num(e.get('timestamp')) > 0), default=0)
    chronological_coverage = bool(
        fund_ts > 0 and earliest_event > 0 and earliest_event <= fund_ts + 86400
    )
    # An exhausted provider cursor is stronger coverage evidence than the first
    # event timestamp: a wallet may be funded and remain idle for several days.
    history_covers_funding = bool(
        fund_ts > 0 and (chronological_coverage or activity_history_exhausted)
    )
    history_coverage_basis = (
        'provider_history_exhausted'
        if fund_ts > 0 and activity_history_exhausted
        else 'first_event_near_funding'
        if chronological_coverage
        else 'incomplete_activity_history'
    )
    capital_complete = bool(
        starting_equity is not None
        and ending_equity is not None
        and history_covers_funding
    )
    growth_usd = (
        round(ending_equity - starting_equity - net_flow, 2)
        if capital_complete else None
    )
    capital_base = (
        starting_equity + max(0.0, flows['total_deposits_usd'])
        if starting_equity is not None else None
    )
    portfolio_return_pct = (
        round(growth_usd / capital_base * 100.0, 2)
        if growth_usd is not None and capital_base and capital_base > 0 else None
    )

    # Capital Tier Proxy
    portfolio_size = max(
        value for value in (ending_equity, peak_equity, open_tokens_val)
        if value is not None
    )
    if portfolio_size >= 2_000_000:
        tier = 'C5'
    elif portfolio_size >= 1_000_000:
        tier = 'C4'
    elif portfolio_size >= 300_000:
        tier = 'C3'
    elif portfolio_size >= 100_000:
        tier = 'C2'
    elif portfolio_size >= 20_000:
        tier = 'C1'
    else:
        tier = 'C0'

    has_onchain_funding = bool(
        funding and (
            funding.get('availability') in ('solana_rpc_oldest_transaction', 'solscan_funded_by')
            or funding.get('verified') is True
        )
    )
    # Deep on-chain transaction evidence requires verified balances and fee records on timeline
    has_onchain_transactions = bool(
        onchain_verified
        or sum(1 for e in events if e.get('balance_verified')) >= 10
    )
    # Full mark-to-market equity curve currently not available across all tokens/timestamps
    has_mark_to_market_curve = False

    funding_verification_status = 'VERIFIED' if has_onchain_funding else 'UNVERIFIED'

    if capital_complete and has_onchain_funding and has_onchain_transactions and has_mark_to_market_curve:
        # Tier 1: Full Verified Equity (Currently: 0 wallets)
        status = 'FULL_VERIFIED_EQUITY'
        verification_status = 'VERIFIED'
        growth_out = growth_usd
        return_out = portfolio_return_pct
        peak_out = peak_equity
        avg_out = avg_equity
        covers_funding_out = True
        limitations = []
    elif capital_complete and has_onchain_transactions:
        # Tier 2: Transaction-Corroborated Equity (Currently: 1 wallet Hkk4yo... with 186 txs & 143 balance checks)
        status = 'TRANSACTION_CORROBORATED_EQUITY_HISTORY'
        verification_status = 'TRANSACTION_CORROBORATED'
        growth_out = growth_usd
        return_out = portfolio_return_pct
        peak_out = peak_equity
        avg_out = avg_equity
        covers_funding_out = True
        limitations = ['missing_mark_to_market_equity_curve']
    elif has_onchain_funding:
        # Tier 3: Funding Verified but Equity Reconstructed (Currently: 8 wallets)
        status = 'RECONSTRUCTED_EQUITY_HISTORY' if capital_complete else 'PARTIAL_CAPITAL_PROXY'
        verification_status = 'FUNDING_VERIFIED'
        growth_out = None
        return_out = None
        peak_out = None
        avg_out = None
        covers_funding_out = False
        limitations = [
            'missing_reconciled_external_flow_ledger',
            'missing_period_aligned_balance_snapshots',
            'missing_mark_to_market_equity_curve',
            'unverified_intraday_transactions',
        ]
    else:
        # Tier 4: Reconstructed / Unverified (Currently: 183 wallets)
        status = 'RECONSTRUCTED_EQUITY_HISTORY' if capital_complete else 'PARTIAL_CAPITAL_PROXY'
        verification_status = 'UNVERIFIED'
        growth_out = None
        return_out = None
        peak_out = None
        avg_out = None
        covers_funding_out = False
        limitations = [
            'missing_reconciled_external_flow_ledger',
            'missing_period_aligned_balance_snapshots',
            'missing_mark_to_market_equity_curve',
        ]

    for key, missing in (
        ('starting_equity', starting_equity is None),
        ('ending_native_balance_or_sol_price', ending_equity is None),
        ('activity_since_funding', not history_covers_funding),
    ):
        if missing and key not in limitations:
            limitations.append(key)

    return {
        'starting_equity_usd': starting_equity,
        'ending_equity_usd': ending_equity,
        'peak_equity_usd': peak_out,
        'average_equity_usd': avg_out,
        'estimated_peak_equity_usd': peak_equity,
        'estimated_average_equity_usd': avg_equity,
        'portfolio_growth_usd': growth_out,
        'portfolio_return_pct': return_out,
        'external_capital_flow_usd': net_flow,
        'total_deposits_usd': flows['total_deposits_usd'],
        'total_withdrawals_usd': flows['total_withdrawals_usd'],
        'capital_tier_proxy': tier,
        'status': status,
        'history_covers_funding': covers_funding_out,
        'provider_history_exhausted': activity_history_exhausted,
        'verification_status': verification_status,
        'funding_verification_status': funding_verification_status,
        'valuation_basis': (
            'ONCHAIN_VERIFIED_CAPITAL_HISTORY'
            if has_onchain_transactions
            else 'FUNDING_VERIFIED_RECONSTRUCTED'
            if has_onchain_funding
            else 'GMGN_ACTIVITY_AND_HOLDINGS_PROXY'
        ),
        'history_coverage_basis': history_coverage_basis,
        'funding_basis': funding_basis,
        'funding_price_basis': (
            'historical_sol_usd_daily'
            if fund_amount > 0 and _num(historical_sol_price_usd) > 0
            else 'observed_activity_sol_price'
            if fund_amount > 0 and sol_price is not None
            else 'stablecoin_usd'
            if fund_amount_usd > 0
            else 'observed_early_deposits_usd'
            if early_deposits > 0
            else 'unavailable'
        ),
        'funding_sol_price_usd': (
            round(funding_sol_price, 4)
            if fund_amount > 0 and funding_sol_price else None
        ),
        'native_balance_available': native_balance_available,
        'sol_price_usd': round(sol_price, 4) if sol_price is not None else None,
        'limitations': limitations,
    }

