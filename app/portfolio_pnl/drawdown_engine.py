"""Flow-neutral NAV reconstruction from reconciled balance and price observations.

Each observation is a full, simultaneous portfolio valuation. A flow observation
must have explicit before/after balances, valued with the same contemporaneous
prices. Fees remain in the portfolio loss, never in the external cash flow.
No forward filling, zero filling, or inferred starting capital is permitted.
"""
import math


class ReconstructionError(ValueError):
    pass


def number(value):
    try:
        value = float(value)
    except (ValueError, TypeError):
        raise ReconstructionError('missing_or_invalid_number')
    if not math.isfinite(value):
        raise ReconstructionError('nonfinite_number')
    return value


def valuation(balances, prices):
    total = 0.0
    for asset, quantity in balances.items():
        quantity = number(quantity)
        if quantity < 0:
            raise ReconstructionError('negative_balance:' + asset)
        if quantity == 0:
            continue
        if asset not in prices:
            raise ReconstructionError('missing_price:' + asset)
        price = number(prices[asset])
        if price < 0:
            raise ReconstructionError('negative_price:' + asset)
        total += quantity * price
    return total


def reconstruct_nav(observations, *, ledger_reconciled=False):
    """Return sampled MDD, including losses before transfers and transaction fees.

    Input order must be chronological; sequence disambiguates same-block events.
    Required row fields: timestamp, sequence, balances, prices. Flow rows also
    contain before_balances and signed external_quantities (token deposits > 0).
    A full withdrawal ends a segment. New capital starts a separate segment;
    the output MDD is the largest segment MDD, not a drawdown across empty periods.
    """
    if not ledger_reconciled:
        raise ReconstructionError('external_flow_ledger_not_reconciled')
    if len(observations) < 2:
        raise ReconstructionError('insufficient_valuations')
    curve = []
    units = peak = 0.0
    segment = 0
    previous = None
    max_dd = 0.0
    def record(row, equity, phase):
        nonlocal peak, max_dd
        nav = equity / units
        peak = max(peak, nav)
        dd = 100 * (peak - nav) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        curve.append(dict(timestamp=row['timestamp'], sequence=row['sequence'],
                          phase=phase, segment=segment, equity_usd=equity,
                          units=units, nav=nav, drawdown_pct=dd))
    for row in observations:
        key = (number(row['timestamp']), number(row['sequence']))
        if previous is not None and key <= previous:
            raise ReconstructionError('unordered_or_duplicate_observation')
        previous = key
        prices = row['prices']
        after = valuation(row['balances'], prices)
        external = row.get('external_quantities') or {}
        flow = 0.0
        for asset, qty in external.items():
            qty = number(qty)
            if qty:
                if asset not in prices:
                    raise ReconstructionError('missing_flow_price:' + asset)
                price = number(prices[asset])
                if price < 0:
                    raise ReconstructionError('negative_price:' + asset)
                flow += qty * price
        if external:
            if 'before_balances' not in row:
                raise ReconstructionError('missing_pre_flow_valuation')
            before = valuation(row['before_balances'], prices)
            if units > 0:
                record(row, before, 'before_flow')
                nav_before = before / units
                if nav_before <= 0:
                    raise ReconstructionError('capital_added_after_total_loss_requires_new_segment')
                units += flow / nav_before
            else:
                if before != 0 or flow <= 0:
                    raise ReconstructionError('invalid_segment_start')
                segment += 1
                peak = 1.0
                units = flow
            if units < -1e-8:
                raise ReconstructionError('withdrawal_exceeds_equity')
            if abs(units) < 1e-8:
                if after > 1e-8:
                    raise ReconstructionError('nonzero_balance_after_full_withdrawal')
                units = 0.0
                continue
        elif units == 0:
            if not curve and after > 0:
                segment += 1
                units = after
                peak = 1.0
            elif after > 0:
                raise ReconstructionError('unexplained_capital_after_empty_portfolio')
            else:
                continue
        record(row, after, 'after')
    if len(curve) < 2:
        raise ReconstructionError('insufficient_valuations')
    return {'portfolio_max_drawdown_pct': round(max_dd, 6),
            'portfolio_drawdown_status': 'RECONSTRUCTED_SAMPLED_NAV',
            'valuation_count': len(curve), 'segment_count': segment,
            'curve': curve, 'drawdown_used_for_ranking': False}
