"""Evidence-backed wallet labels derived from cached GMGN portfolio data."""
import json
import math
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.runtime import BASE
from app.portfolio_pnl.api import load_wallet_pnl
from app.portfolio_pnl.analytics import compute_wallet_stats, get_wallet_stats
from app.portfolio_pnl.intraday import build_intraday_report
from app.portfolio_pnl.cluster_engine import (
    build_position_clusters,
    compute_cluster_profile,
    analyze_conviction_and_scaling,
)
from app.portfolio_pnl.group_labels import classify_wallet_groups
from app.portfolio_pnl.equity_engine import (
    reconstruct_equity_outcome,
)
from app.portfolio_pnl.store import PnlStore


MC_BUCKETS = (
    ('MICRO_CAP', 'Micro cap < $100K', 0, 100_000),
    ('SMALL_CAP', 'Small cap $100K–$500K', 100_000, 500_000),
    ('MID_CAP', 'Mid cap $500K–$5M', 500_000, 5_000_000),
    ('LARGE_CAP', 'Large cap ≥ $5M', 5_000_000, math.inf),
)
WEEKDAYS = ['Thứ 2', 'Thứ 3', 'Thứ 4', 'Thứ 5', 'Thứ 6', 'Thứ 7', 'Chủ nhật']
INTRADAY_DIR = BASE / 'data' / 'portfolio_pnl' / 'intraday'


def _number(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _median_outflow_holding_pct(events: Optional[List[dict]]) -> Optional[float]:
    """Return the median sell/transfer size as a percentage of held tokens.

    The balance is reconstructed per token from the normalized activity feed.
    Missing or invalid token quantities are skipped instead of being treated as
    zero; an outflow with no verified positive balance is conservatively treated
    as a full outflow.
    """
    balances: Dict[str, float] = {}
    outflows: List[float] = []
    ordered = sorted(
        events or [],
        key=lambda event: (
            _number(event.get('timestamp')) or 0,
            str(event.get('tx_hash') or ''),
        ),
    )
    for event in ordered:
        token = str(event.get('token_address') or '').strip()
        if not token:
            continue
        try:
            amount = float(
                event.get('token_amount')
                or event.get('amount_token')
                or event.get('amount')
                or 0
            )
        except (TypeError, ValueError):
            continue
        if not math.isfinite(amount) or amount <= 0:
            continue
        kind = str(event.get('event_type') or event.get('type') or '').strip().lower()
        if kind in {'buy', 'transferin'}:
            balances[token] = balances.get(token, 0.0) + amount
        elif kind in {'sell', 'transferout'}:
            balance = balances.get(token, 0.0)
            outflows.append(_clip(amount / balance * 100.0, 0.0, 100.0) if balance > 0 else 100.0)
            balances[token] = max(0.0, balance - amount)
    return round(statistics.median(outflows), 2) if outflows else None


def _percentile(values: Iterable[float], percentile: float) -> Optional[float]:
    ordered = sorted(float(value) for value in values if _number(value) is not None)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _range(values: Iterable[float], unit: str = 'usd') -> dict:
    values = [float(value) for value in values if _number(value) is not None]
    p25 = _percentile(values, 0.25)
    p75 = _percentile(values, 0.75)
    return {
        'p25': round(p25, 2) if p25 is not None else None,
        'p75': round(p75, 2) if p75 is not None else None,
        'sample_size': len(values),
        'unit': unit,
    }


def _profit_factor(profit: float, loss: float) -> Optional[float]:
    if loss > 0:
        return round(profit / loss, 3)
    return None


def _profit_factor_status(profit: float, loss: float) -> str:
    if loss > 0:
        return 'FINITE'
    if profit > 0:
        return 'NO_LOSS'
    return 'NO_OUTCOME'


def _extract_gmgn_winrate(stats_payload: Optional[dict]) -> Optional[float]:
    """Extract authoritative portfolio-wide win rate from GMGN portfolio/stats endpoint."""
    if not stats_payload or not isinstance(stats_payload, dict):
        return None
    data = stats_payload.get('data') if isinstance(stats_payload.get('data'), dict) else stats_payload
    if not isinstance(data, dict):
        return None
    pnl_stat = data.get('pnl_stat')
    if isinstance(pnl_stat, dict):
        wr = pnl_stat.get('winrate')
        if wr is not None:
            try:
                wr_val = float(wr)
                if 0.0 <= wr_val <= 1.0:
                    return round(wr_val * 100.0, 2)
                elif 1.0 < wr_val <= 100.0:
                    return round(wr_val, 2)
            except (TypeError, ValueError):
                pass
    return None


def _extract_gmgn_token_num(stats_payload: Optional[dict]) -> Optional[int]:
    """Extract total tokens traded from GMGN portfolio/stats endpoint."""
    if not stats_payload or not isinstance(stats_payload, dict):
        return None
    data = stats_payload.get('data') if isinstance(stats_payload.get('data'), dict) else stats_payload
    if not isinstance(data, dict):
        return None
    pnl_stat = data.get('pnl_stat')
    if isinstance(pnl_stat, dict):
        tnum = pnl_stat.get('token_num')
        if tnum is not None:
            try:
                return int(tnum)
            except (TypeError, ValueError):
                pass
    return None


def _max_drawdown(daily: List[dict]) -> float:
    cumulative = peak = drawdown = 0.0
    for row in sorted(daily, key=lambda item: item.get('date') or ''):
        cumulative += float(row.get('realized_pnl_usd') or 0)
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    return round(drawdown, 2)


def _correlation(xs: List[float], ys: List[float]) -> Optional[float]:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mean_x = statistics.mean(xs)
    mean_y = statistics.mean(ys)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    variance_x = sum((x - mean_x) ** 2 for x in xs)
    variance_y = sum((y - mean_y) ** 2 for y in ys)
    if not variance_x or not variance_y:
        return None
    return covariance / math.sqrt(variance_x * variance_y)


def _ranks(values: List[float]) -> List[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(indexed):
        end = position
        while end + 1 < len(indexed) and indexed[end + 1][1] == indexed[position][1]:
            end += 1
        average_rank = (position + end + 2) / 2
        for index in range(position, end + 1):
            ranks[indexed[index][0]] = average_rank
        position = end + 1
    return ranks


def _streaks(rows: List[dict]) -> dict:
    longest_win = longest_loss = current_win = current_loss = 0
    for row in sorted(rows, key=lambda item: item.get('date') or ''):
        pnl = float(row.get('realized_pnl_usd') or 0)
        if pnl > 0:
            current_win += 1
            current_loss = 0
        elif pnl < 0:
            current_loss += 1
            current_win = 0
        else:
            current_win = current_loss = 0
        longest_win = max(longest_win, current_win)
        longest_loss = max(longest_loss, current_loss)
    return {'longest_win_streak_days': longest_win, 'longest_loss_streak_days': longest_loss}


def _drawdown_stats(rows: List[dict]) -> dict:
    cumulative = peak = max_drawdown = 0.0
    underwater_start = None
    max_underwater_days = 0
    for row in sorted(rows, key=lambda item: item.get('date') or ''):
        try:
            day = datetime.strptime(row['date'], '%Y-%m-%d').date()
        except (KeyError, TypeError, ValueError):
            continue
        cumulative += float(row.get('realized_pnl_usd') or 0)
        if cumulative >= peak:
            if underwater_start is not None:
                max_underwater_days = max(max_underwater_days, (day - underwater_start).days)
            peak = cumulative
            underwater_start = None
        else:
            if underwater_start is None:
                underwater_start = day
            max_drawdown = max(max_drawdown, peak - cumulative)
    if underwater_start is not None and rows:
        try:
            final_day = datetime.strptime(
                max(row.get('date') for row in rows if row.get('date')),
                '%Y-%m-%d',
            ).date()
            max_underwater_days = max(max_underwater_days, (final_day - underwater_start).days)
        except (ValueError, TypeError):
            pass
    return {
        'max_drawdown_usd': round(max_drawdown, 2),
        'max_underwater_days': max_underwater_days,
        'peak_cumulative_pnl_usd': round(peak, 2),
    }


def _window_summary(rows: List[dict]) -> dict:
    return {
        'net_pnl_usd': round(sum(float(row.get('realized_pnl_usd') or 0) for row in rows), 2),
        'buy_tx_count': sum(int(row.get('buy_tx_count') or 0) for row in rows),
        'sell_tx_count': sum(int(row.get('sell_tx_count') or 0) for row in rows),
        'active_days': sum(float(row.get('realized_pnl_usd') or 0) != 0 for row in rows),
    }


def _hours_from_seconds(seconds: Optional[float]) -> Optional[float]:
    value = _number(seconds)
    if value is None or value < 0:
        return None
    return value / 3600.0


def _holding_discipline(rows: List[dict]) -> dict:
    closed = [row for row in rows if row.get('is_closed')]
    winners = [row for row in closed if float(row.get('realized_profit_usd') or 0) > 0]
    losers = [row for row in closed if float(row.get('realized_profit_usd') or 0) < 0]
    winner_hours = [
        hours for hours in (_hours_from_seconds(row.get('holding_duration_seconds')) for row in winners)
        if hours is not None
    ]
    loser_hours = [
        hours for hours in (_hours_from_seconds(row.get('holding_duration_seconds')) for row in losers)
        if hours is not None
    ]
    early_cuts = sum(1 for hours in loser_hours if hours <= 6)
    bag_holds = sum(1 for hours in loser_hours if hours >= 72)
    sample = len(closed)
    status = 'AVAILABLE' if sample >= 20 else 'INSUFFICIENT_DATA'
    if status == 'AVAILABLE' and loser_hours and winner_hours:
        if statistics.median(loser_hours) <= statistics.median(winner_hours) * 0.5:
            style = 'CUTS_LOSSES_FASTER'
        elif bag_holds / max(1, len(loser_hours)) >= 0.4:
            style = 'BAG_HOLDING_LOSERS'
        else:
            style = 'MIXED_HOLDING'
    else:
        style = 'INSUFFICIENT_DATA'
    return {
        'status': status,
        'style': style,
        'closed_positions': sample,
        'hodl_count': sum(1 for row in rows if row.get('is_hodl')),
        'close_rate_pct': round(100 * sample / len(rows), 1) if rows else None,
        'median_winner_hold_hours': round(statistics.median(winner_hours), 2) if winner_hours else None,
        'median_loser_hold_hours': round(statistics.median(loser_hours), 2) if loser_hours else None,
        'early_cut_loss_rate_pct': round(100 * early_cuts / len(loser_hours), 1) if loser_hours else None,
        'bag_hold_loss_rate_pct': round(100 * bag_holds / len(loser_hours), 1) if loser_hours else None,
    }


def _exit_efficiency(rows: List[dict]) -> dict:
    closed = [
        row for row in rows
        if row.get('is_closed')
        and _number(row.get('bought_mc'))
        and _number(row.get('sold_mc'))
        and float(row.get('bought_mc') or 0) > 0
    ]
    expansions = [float(row['sold_mc']) / float(row['bought_mc']) for row in closed]
    realized = sum(float(row.get('realized_profit_usd') or 0) for row in rows)
    total = sum(float(row.get('total_profit_usd') or 0) for row in rows)
    status = 'AVAILABLE' if len(closed) >= 20 else 'INSUFFICIENT_DATA'
    return {
        'status': status,
        'closed_with_mc': len(closed),
        'median_exit_mc_multiple': round(statistics.median(expansions), 3) if expansions else None,
        'realized_share_of_total_pct': round(100 * realized / total, 1) if total else None,
        'style': (
            'TAKES_EXPANSION'
            if status == 'AVAILABLE' and expansions and statistics.median(expansions) >= 1.5
            else 'EXITS_NEAR_ENTRY_MC'
            if status == 'AVAILABLE' and expansions and statistics.median(expansions) < 1.1
            else 'MIXED_EXIT'
            if status == 'AVAILABLE'
            else 'INSUFFICIENT_DATA'
        ),
    }


def _scale_style(rows: List[dict]) -> dict:
    buys = [int(row.get('buy_tx_count') or 0) for row in rows]
    sells = [int(row.get('sell_tx_count') or 0) for row in rows]
    multi_buy = sum(1 for count in buys if count >= 3)
    status = 'AVAILABLE' if len(rows) >= 20 else 'INSUFFICIENT_DATA'
    median_buys = statistics.median(buys) if buys else None
    median_sells = statistics.median(sells) if sells else None
    if status != 'AVAILABLE':
        style = 'INSUFFICIENT_DATA'
    elif median_buys and median_buys >= 3:
        style = 'SCALES_IN'
    elif median_sells and median_sells >= 3:
        style = 'SCALES_OUT'
    else:
        style = 'SINGLE_FILL_BIAS'
    return {
        'status': status,
        'style': style,
        'median_buy_tx': round(median_buys, 1) if median_buys is not None else None,
        'median_sell_tx': round(median_sells, 1) if median_sells is not None else None,
        'multi_buy_token_rate_pct': round(100 * multi_buy / len(rows), 1) if rows else None,
    }


def _concentration_profile(rows: List[dict]) -> dict:
    open_rows = [row for row in rows if float(row.get('balance_usd') or 0) > 0.01]
    open_total = sum(float(row.get('balance_usd') or 0) for row in open_rows)
    shares = [
        float(row.get('balance_usd') or 0) / open_total
        for row in open_rows
        if open_total > 0
    ]
    hhi = sum(share ** 2 for share in shares) * 10_000 if shares else None
    profits = sorted(
        (float(row.get('total_profit_usd') or 0) for row in rows),
        key=abs,
        reverse=True,
    )
    total_abs = sum(abs(value) for value in profits)
    top5_share = (
        100 * sum(abs(value) for value in profits[:5]) / total_abs
        if total_abs else None
    )
    status = 'AVAILABLE' if len(rows) >= 5 else 'INSUFFICIENT_DATA'
    if status != 'AVAILABLE':
        style = 'INSUFFICIENT_DATA'
    elif (hhi or 0) >= 2500 or (top5_share or 0) >= 80:
        style = 'CONCENTRATED'
    else:
        style = 'DIVERSIFIED'
    return {
        'status': status,
        'style': style,
        'open_positions': len(open_rows),
        'hhi': round(hhi, 1) if hhi is not None else None,
        'top1_open_pct': round(100 * max(shares), 1) if shares else None,
        'top5_abs_pnl_share_pct': round(top5_share, 1) if top5_share is not None else None,
    }


def _transfer_dependence(rows: List[dict]) -> dict:
    transferred = [
        row for row in rows
        if row.get('didnt_buy')
        or int(row.get('transfer_in_count') or 0) > 0
        or int(row.get('transfer_out_count') or 0) > 0
    ]
    share = 100 * len(transferred) / len(rows) if rows else None
    status = 'AVAILABLE' if len(rows) >= 20 else 'INSUFFICIENT_DATA'
    return {
        'status': status,
        'style': (
            'TRANSFER_DEPENDENT'
            if status == 'AVAILABLE' and (share or 0) >= 30
            else 'BUY_SELL_PRIMARY'
            if status == 'AVAILABLE'
            else 'INSUFFICIENT_DATA'
        ),
        'transfer_related_token_rate_pct': round(share, 1) if share is not None else None,
        'didnt_buy_count': sum(1 for row in rows if row.get('didnt_buy')),
    }


def _token_age_bands(rows: List[dict]) -> dict:
    bands = (
        ('UNDER_1H', 0, 1),
        ('1H_6H', 1, 6),
        ('6H_24H', 6, 24),
        ('OVER_24H', 24, math.inf),
    )
    breakdown = []
    for code, low, high in bands:
        members = [
            row for row in rows
            if (age := _number(row.get('token_age_at_entry_hours'))) is not None
            and low <= age < high
        ]
        pnl = sum(float(row.get('total_profit_usd') or 0) for row in members)
        breakdown.append({
            'code': code,
            'token_count': len(members),
            'net_pnl_usd': round(pnl, 2),
        })
    eligible = [band for band in breakdown if band['token_count'] >= 15]
    best = max(eligible, key=lambda item: item['net_pnl_usd']) if eligible else None
    return {
        'status': 'AVAILABLE' if best else 'INSUFFICIENT_DATA',
        'best_band': best['code'] if best else None,
        'breakdown': breakdown,
    }


def _session_habit(intraday: dict) -> dict:
    if not intraday.get('insight_eligible'):
        return {
            'status': intraday.get('status') or 'NOT_COLLECTED',
            'style': 'INSUFFICIENT_DATA',
            'dominant_session': None,
        }
    sessions = list(intraday.get('sessions') or [])
    if not sessions:
        return {'status': 'PARTIAL_HEURISTIC', 'style': 'INSUFFICIENT_DATA', 'dominant_session': None}
    dominant = max(sessions, key=lambda item: abs(float(item.get('net_pnl_usd') or 0)))
    return {
        'status': 'AVAILABLE_HEURISTIC',
        'style': 'HAS_SESSION_BIAS' if abs(float(dominant.get('net_pnl_usd') or 0)) > 0 else 'FLAT',
        'dominant_session': dominant.get('label'),
        'dominant_net_pnl_usd': dominant.get('net_pnl_usd'),
        'attribution_quality': intraday.get('attribution_quality'),
    }


def _classify_wallet(
    tier: str,
    confidence: str,
    holdings_sufficient: bool,
    setup: dict,
    conviction: dict,
    holding: dict,
    exit_profile: dict,
    scale: dict,
    concentration: dict,
    transfer: dict,
    session: dict,
    age_bands: dict,
) -> dict:
    historical = (
        'HIGH_HISTORICAL_VALUE'
        if tier in ('ELITE_PNL', 'STRONG_PNL') and confidence in ('HIGH', 'MEDIUM')
        else 'LOSS_MAKING_HISTORY'
        if tier == 'LOSS_MAKING'
        else 'MIXED_HISTORY'
        if tier == 'MIXED_PNL'
        else 'WEAK_OR_THIN_HISTORY'
    )
    behavior = []
    if setup.get('status') == 'AVAILABLE':
        behavior.append(f"SPECIALIST_{setup.get('optimal_setup_archetype')}")
    if session.get('style') == 'HAS_SESSION_BIAS':
        behavior.append('SESSION_HABIT')
    if holding.get('style') not in (None, 'INSUFFICIENT_DATA'):
        behavior.append(holding['style'])
    if exit_profile.get('style') not in (None, 'INSUFFICIENT_DATA'):
        behavior.append(exit_profile['style'])
    if scale.get('style') not in (None, 'INSUFFICIENT_DATA'):
        behavior.append(scale['style'])
    if concentration.get('style') not in (None, 'INSUFFICIENT_DATA'):
        behavior.append(concentration['style'])
    if transfer.get('style') == 'TRANSFER_DEPENDENT':
        behavior.append('TRANSFER_DEPENDENT')
    if conviction.get('conviction_verdict') in ('POSITIVE_SIZE_EDGE', 'OVERCONFIDENCE_RISK'):
        behavior.append(conviction['conviction_verdict'])
    if age_bands.get('best_band'):
        behavior.append(f"ENTRY_AGE_{age_bands['best_band']}")
    if not behavior:
        behavior.append('HABITS_NOT_LABELLED')
    data_quality = (
        'HIGH_CONFIDENCE'
        if confidence == 'HIGH' and holdings_sufficient
        else 'MEDIUM_CONFIDENCE'
        if confidence in ('HIGH', 'MEDIUM')
        else 'LOW_CONFIDENCE'
    )
    if historical == 'HIGH_HISTORICAL_VALUE' and any(tag.startswith('SPECIALIST_') for tag in behavior):
        primary = 'VALUABLE_SPECIALIST'
    elif historical == 'HIGH_HISTORICAL_VALUE':
        primary = 'VALUABLE_GENERALIST'
    elif 'TRANSFER_DEPENDENT' in behavior:
        primary = 'TRANSFER_OR_INTERNAL_FLOW'
    elif historical == 'LOSS_MAKING_HISTORY':
        primary = 'NEGATIVE_EDGE'
    else:
        primary = 'RESEARCH_CANDIDATE'
    return {
        'primary_wallet_value': primary,
        'historical_quality': historical,
        'trading_behavior': behavior,
        'predictive_value': 'NOT_MEASURED',
        'data_quality': data_quality,
        'notes': 'Primary value is historical/behavioral only; not a copy-trade certification.',
    }


def _market_cap_profile(rows: List[dict]) -> dict:
    profiles = []
    for code, label, low, high in MC_BUCKETS:
        members = [
            row for row in rows
            if (mc := _number(row.get('bought_mc'))) is not None and low <= mc < high
        ]
        wins = [row for row in members if float(row.get('total_profit_usd') or 0) > 0]
        losses = [row for row in members if float(row.get('total_profit_usd') or 0) < 0]
        gross_profit = sum(float(row.get('total_profit_usd') or 0) for row in wins)
        gross_loss = abs(sum(float(row.get('total_profit_usd') or 0) for row in losses))
        rois = [
            float(row['total_profit_pct'])
            for row in members
            if _number(row.get('total_profit_pct')) is not None
        ]
        median_roi = statistics.median(rois) if rois else None
        win_rate = 100 * len(wins) / len(members) if members else None
        pf = _profit_factor(gross_profit, gross_loss)
        quality_score = None
        if members:
            quality_score = (
                0.4 * win_rate
                + 0.3 * _clip((pf if pf is not None else 3.0) / 2 * 100, 0, 100)
                + 0.3 * _clip(50 + (median_roi or 0) / 2, 0, 100)
            )
        profiles.append({
            'code': code,
            'label': label,
            'token_count': len(members),
            'win_rate_pct': round(win_rate, 1) if win_rate is not None else None,
            'profit_usd': round(gross_profit, 2),
            'loss_usd': round(gross_loss, 2),
            'net_pnl_usd': round(gross_profit - gross_loss, 2),
            'profit_factor': pf,
            'median_roi_pct': round(median_roi, 2) if median_roi is not None else None,
            'quality_score': round(quality_score, 1) if quality_score is not None else None,
            '_members': members,
        })

    minimum_sample = 15
    eligible = [profile for profile in profiles if profile['token_count'] >= minimum_sample]
    best = max(eligible, key=lambda item: item['quality_score']) if eligible else None
    worst = min(eligible, key=lambda item: item['quality_score']) if eligible else None
    public_profiles = [{key: value for key, value in row.items() if key != '_members'} for row in profiles]
    return {'profiles': public_profiles, 'best': best, 'worst': worst, 'minimum_sample': minimum_sample}


def _conviction_profile(rows: List[dict], activity_events: Optional[List[dict]] = None) -> dict:
    observations = []
    for row in rows:
        size = _number(row.get('bought_usd'))
        roi = _number(row.get('total_profit_pct'))
        if size is not None and size > 0 and roi is not None:
            observations.append((math.log1p(size), _clip(roi, -100, 500), size, roi))
    median_buy = statistics.median(item[2] for item in observations) if observations else None

    # Calculate historical allocation proxy from on-chain pre-balance if available
    allocations = []
    if activity_events:
        for e in activity_events:
            etype = str(e.get('event_type') or e.get('type') or '').lower()
            if etype != 'buy':
                continue
            amt_usd = _number(e.get('amount_usd') or e.get('cost_usd') or e.get('buy_cost_usd'))
            pre_sol = _number(e.get('pre_balance_sol'))
            price_sol = _number(e.get('sol_price_usd')) or _number(e.get('price_usd'))
            quote_amt = _number(e.get('quote_amount'))

            if pre_sol is not None and pre_sol > 0:
                if quote_amt is not None and quote_amt > 0:
                    pct = min(100.0, (quote_amt / pre_sol) * 100.0)
                    allocations.append(pct)
                elif amt_usd is not None and amt_usd > 0 and price_sol is not None and price_sol > 0:
                    sol_spent = amt_usd / price_sol
                    pct = min(100.0, (sol_spent / pre_sol) * 100.0)
                    allocations.append(pct)

    has_onchain_alloc = any(e.get('pre_balance_sol') for e in (activity_events or []))
    alloc_status = 'VERIFIED' if (has_onchain_alloc and allocations and len(allocations) >= 3) else 'NOT_AVAILABLE'
    historical_alloc_pct = round(statistics.median(allocations), 2) if allocations else None

    if len(observations) < 30:
        return {
            'conviction_verdict': 'INSUFFICIENT_DATA',
            'sample_size': len(observations),
            'median_buy_usd': round(median_buy, 2) if median_buy is not None else None,
            'regression_beta_slope': None,
            'size_roi_correlation': None,
            'correlation_method': 'spearman_rank',
            'correlation_95ci': None,
            'large_vs_small_median_roi_gap_pct': None,
            'conviction_analysis': 'Cần tối thiểu 30 token có buy size và ROI để kiểm định.',
            'allocation_status': alloc_status,
            'historical_allocation_pct': historical_alloc_pct,
            'allocation_sample_size': len(allocations),
        }

    xs = [item[0] for item in observations]
    ys = [item[1] for item in observations]
    mean_x = statistics.mean(xs)
    mean_y = statistics.mean(ys)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    variance_x = sum((x - mean_x) ** 2 for x in xs)
    variance_y = sum((y - mean_y) ** 2 for y in ys)
    slope = covariance / variance_x if variance_x else 0.0
    correlation = _correlation(_ranks(xs), _ranks(ys)) or 0.0
    clipped_correlation = _clip(correlation, -0.999, 0.999)
    fisher_z = math.atanh(clipped_correlation)
    fisher_se = 1 / math.sqrt(len(observations) - 3)
    correlation_ci = (
        math.tanh(fisher_z - 1.96 * fisher_se),
        math.tanh(fisher_z + 1.96 * fisher_se),
    )
    ordered = sorted(observations, key=lambda item: item[2])
    quartile = max(1, len(ordered) // 4)
    small_roi = statistics.median(item[3] for item in ordered[:quartile])
    large_roi = statistics.median(item[3] for item in ordered[-quartile:])
    gap = large_roi - small_roi
    if correlation >= 0.30 and correlation_ci[0] > 0 and gap > 0:
        verdict = 'POSITIVE_SIZE_EDGE'
        analysis = 'Nhóm lệnh size lớn có ROI tốt hơn; đây là tương quan lịch sử, không chứng minh quan hệ nhân quả.'
    elif correlation <= -0.30 and correlation_ci[1] < 0 and gap < 0:
        verdict = 'OVERCONFIDENCE_RISK'
        analysis = 'Nhóm lệnh size lớn có ROI kém hơn, thể hiện rủi ro over-sizing trong mẫu quan sát.'
    else:
        verdict = 'SIZE_INVARIANT'
        analysis = 'Chưa thấy mối liên hệ đủ mạnh giữa buy size và ROI.'
    return {
        'conviction_verdict': verdict,
        'sample_size': len(observations),
        'median_buy_usd': round(median_buy, 2),
        'regression_beta_slope': round(slope, 2),
        'size_roi_correlation': round(correlation, 3),
        'correlation_method': 'spearman_rank',
        'correlation_95ci': [round(correlation_ci[0], 3), round(correlation_ci[1], 3)],
        'large_vs_small_median_roi_gap_pct': round(gap, 2),
        'conviction_analysis': analysis,
        'allocation_status': alloc_status,
        'historical_allocation_pct': historical_alloc_pct,
        'allocation_sample_size': len(allocations),
    }



def _daily_profile(daily: List[dict]) -> dict:
    official = sorted([
        row for row in daily
        if row.get('source') == 'gmgn_web_daily_profits'
    ], key=lambda item: item.get('date') or '')
    profits = sum(max(0.0, float(row.get('profit_usd') or 0)) for row in official)
    losses = sum(max(0.0, float(row.get('loss_usd') or 0)) for row in official)
    positive_days = sum(float(row.get('realized_pnl_usd') or 0) > 0 for row in official)
    negative_days = sum(float(row.get('realized_pnl_usd') or 0) < 0 for row in official)
    active_days = positive_days + negative_days
    weekday = [{'label': label, 'profit_usd': 0.0, 'loss_usd': 0.0} for label in WEEKDAYS]
    for row in official:
        try:
            index = datetime.strptime(row['date'], '%Y-%m-%d').weekday()
        except (KeyError, TypeError, ValueError):
            continue
        weekday[index]['profit_usd'] += max(0.0, float(row.get('profit_usd') or 0))
        weekday[index]['loss_usd'] += max(0.0, float(row.get('loss_usd') or 0))
    for row in weekday:
        row['profit_usd'] = round(row['profit_usd'], 2)
        row['loss_usd'] = round(row['loss_usd'], 2)
        row['net_pnl_usd'] = round(row['profit_usd'] - row['loss_usd'], 2)
    active_values = [
        float(row.get('realized_pnl_usd') or 0)
        for row in official
        if float(row.get('realized_pnl_usd') or 0) != 0
    ]
    value_at_risk_95 = _percentile(active_values, 0.05)
    cvar_values = [
        value for value in active_values
        if value_at_risk_95 is not None and value <= value_at_risk_95
    ]
    volumes = [
        float(row.get('buy_volume_usd') or 0) + float(row.get('sell_volume_usd') or 0)
        for row in official
    ]
    daily_values = [float(row.get('realized_pnl_usd') or 0) for row in official]
    volume_pnl_correlation = _correlation(volumes, daily_values) if len(official) >= 20 else None
    weekend_values = []
    weekday_values = []
    dated_rows = []
    for row in official:
        try:
            day = datetime.strptime(row['date'], '%Y-%m-%d').date()
        except (KeyError, TypeError, ValueError):
            continue
        dated_rows.append((day, row))
        target = weekend_values if day.weekday() >= 5 else weekday_values
        target.append(float(row.get('realized_pnl_usd') or 0))
    recent_rows = []
    prior_rows = []
    if dated_rows:
        end_day = max(day for day, _row in dated_rows)
        recent_start = end_day - timedelta(days=29)
        prior_start = end_day - timedelta(days=59)
        recent_rows = [row for day, row in dated_rows if recent_start <= day <= end_day]
        prior_rows = [row for day, row in dated_rows if prior_start <= day < recent_start]
    recent_summary = _window_summary(recent_rows)
    prior_summary = _window_summary(prior_rows)
    activity_recent = recent_summary['buy_tx_count'] + recent_summary['sell_tx_count']
    activity_prior = prior_summary['buy_tx_count'] + prior_summary['sell_tx_count']
    drawdown = _drawdown_stats(official)
    return {
        'source': 'gmgn_web_daily_profits' if official else None,
        'timezone': 'UTC',
        'calendar_rows': len(official),
        'active_days': active_days,
        'positive_days': positive_days,
        'negative_days': negative_days,
        'profitable_day_rate_pct': round(100 * positive_days / active_days, 1) if active_days else None,
        'profit_usd': round(profits, 2),
        'loss_usd': round(losses, 2),
        'net_pnl_usd': round(profits - losses, 2),
        'profit_factor': _profit_factor(profits, losses),
        'profit_factor_status': _profit_factor_status(profits, losses),
        'max_drawdown_usd': drawdown['max_drawdown_usd'],
        'best_weekday': max(weekday, key=lambda item: item['net_pnl_usd']) if official else None,
        'worst_weekday': min(weekday, key=lambda item: item['net_pnl_usd']) if official else None,
        'weekday_breakdown': weekday,
        'start_date': min((row.get('date') for row in official), default=None),
        'end_date': max((row.get('date') for row in official), default=None),
        'risk': {
            'daily_volatility_usd': round(statistics.stdev(active_values), 2) if len(active_values) >= 2 else None,
            'median_active_day_pnl_usd': round(statistics.median(active_values), 2) if active_values else None,
            'value_at_risk_95_usd': round(value_at_risk_95, 2) if value_at_risk_95 is not None else None,
            'cvar_95_usd': round(statistics.mean(cvar_values), 2) if cvar_values else None,
            'max_drawdown_usd': drawdown['max_drawdown_usd'],
            'max_underwater_days': drawdown['max_underwater_days'],
            'peak_cumulative_pnl_usd': drawdown['peak_cumulative_pnl_usd'],
            'worst_day_usd': round(min(active_values), 2) if active_values else None,
        },
        'consistency': {
            **_streaks(official),
            'weekend_median_pnl_usd': round(statistics.median(weekend_values), 2) if weekend_values else None,
            'weekday_median_pnl_usd': round(statistics.median(weekday_values), 2) if weekday_values else None,
            'volume_pnl_correlation': round(volume_pnl_correlation, 3) if volume_pnl_correlation is not None else None,
        },
        'regime': {
            'recent_30d': recent_summary,
            'prior_30d': prior_summary,
            'pnl_change_usd': round(
                recent_summary['net_pnl_usd'] - prior_summary['net_pnl_usd'],
                2,
            ),
            'activity_change_pct': (
                round(100 * (activity_recent - activity_prior) / activity_prior, 1)
                if activity_prior else None
            ),
        },
    }


def _activity_profile(daily: List[dict], as_of: Optional[datetime] = None) -> dict:
    """Measure current activity against today, not the last date in the dataset."""
    today = (as_of or datetime.now(tz=timezone.utc)).date()
    active = []
    for row in daily:
        if row.get('source') != 'gmgn_web_daily_profits':
            continue
        try:
            day = datetime.strptime(row['date'], '%Y-%m-%d').date()
        except (KeyError, TypeError, ValueError):
            continue
        pnl = float(row.get('realized_pnl_usd') or 0)
        profit = max(0.0, float(row.get('profit_usd') or 0))
        loss = max(0.0, float(row.get('loss_usd') or 0))
        tx_count = int(row.get('buy_tx_count') or 0) + int(row.get('sell_tx_count') or 0)
        if pnl or profit or loss or tx_count:
            active.append((day, pnl, profit, loss, tx_count))

    last_day = max((row[0] for row in active), default=None)
    days_since = max(0, (today - last_day).days) if last_day else None
    start_7d = today - timedelta(days=6)
    start_30d = today - timedelta(days=29)
    recent_7d = [row for row in active if start_7d <= row[0] <= today]
    recent_30d = [row for row in active if start_30d <= row[0] <= today]
    profit_30d = sum(row[2] for row in recent_30d)
    loss_30d = sum(row[3] for row in recent_30d)
    pnl_30d = sum(row[1] for row in recent_30d)

    if days_since is None or days_since > 60:
        status = 'INACTIVE_60D'
        recency_score = 0.0
    elif days_since > 30:
        status = 'DORMANT_30D'
        recency_score = 25.0
    elif days_since > 14:
        status = 'ACTIVE_30D'
        recency_score = 60.0
    elif days_since > 7:
        status = 'ACTIVE_14D'
        recency_score = 85.0
    else:
        status = 'ACTIVE_7D'
        recency_score = 100.0

    return {
        'status': status,
        'last_active_date': last_day.isoformat() if last_day else None,
        'days_since_last_active': days_since,
        'active_days_7d': len(recent_7d),
        'active_days_30d': len(recent_30d),
        'pnl_30d_usd': round(pnl_30d, 2),
        'profit_factor_30d': _profit_factor(profit_30d, loss_30d),
        'profit_factor_30d_status': _profit_factor_status(profit_30d, loss_30d),
        'profitable_day_rate_30d_pct': (
            round(100 * sum(row[1] > 0 for row in recent_30d) / len(recent_30d), 1)
            if recent_30d else None
        ),
        'recency_score': recency_score,
    }


def _asymmetric_payoff_profile(rows: List[dict]) -> dict:
    """Separate repeatable asymmetric payoff from a single lucky winner."""
    outcomes = [
        float(row.get('total_profit_usd') or 0)
        for row in rows
        if float(row.get('total_profit_usd') or 0) != 0
    ]
    wins = [value for value in outcomes if value > 0]
    losses = [abs(value) for value in outcomes if value < 0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    largest_win = max(wins, default=0.0)
    total_pnl = gross_profit - gross_loss
    median_loss = statistics.median(losses) if losses else 0.0
    big_win_threshold = 5 * median_loss if median_loss > 0 else math.inf
    big_winner_count = sum(value >= big_win_threshold for value in wins)

    ordered = [
        value for _timestamp, value in sorted(
            (int(row.get('last_active_timestamp')), float(row.get('total_profit_usd') or 0))
            for row in rows
            if row.get('last_active_timestamp') and float(row.get('total_profit_usd') or 0) != 0
        )
    ]
    longest = current = 0
    for value in ordered:
        current = current + 1 if value < 0 else 0
        longest = max(longest, current)
    previous_longest = 0
    previous_current = 0
    for value in ordered[:-current] if current else ordered:
        previous_current = previous_current + 1 if value < 0 else 0
        previous_longest = max(previous_longest, previous_current)
    abnormal_streak = current >= max(5, math.ceil(max(1, previous_longest) * 1.5))
    top1_share = 100 * largest_win / gross_profit if gross_profit else None
    pnl_without_largest = total_pnl - largest_win if wins else total_pnl
    one_hit = bool(
        top1_share is not None
        and top1_share > 70
        and pnl_without_largest <= 0
    )
    average_win = statistics.mean(wins) if wins else 0.0
    average_loss = statistics.mean(losses) if losses else 0.0
    payoff_ratio = average_win / average_loss if average_loss else None
    return {
        'style': (
            'ONE_HIT_DEPENDENT' if one_hit
            else 'REPEATABLE_ASYMMETRIC' if big_winner_count >= 3 and payoff_ratio and payoff_ratio >= 3
            else 'ASYMMETRIC_HIGH_PAYOFF' if payoff_ratio and payoff_ratio >= 3
            else 'BALANCED_PAYOFF'
        ),
        'top1_gross_profit_share_pct': round(top1_share, 1) if top1_share is not None else None,
        'pnl_without_largest_win_usd': round(pnl_without_largest, 2),
        'big_winner_count': big_winner_count,
        'payoff_ratio': round(payoff_ratio, 2) if payoff_ratio is not None else None,
        'expectancy_usd': round(total_pnl / len(outcomes), 2) if outcomes else None,
        'longest_loss_streak': longest,
        'current_loss_streak': current,
        'previous_longest_loss_streak': previous_longest,
        'current_loss_streak_abnormal': abnormal_streak,
        'outcome_basis': 'TOKEN_AGGREGATE_PROXY',
    }


def _cluster_payoff_profile(clusters: List[dict]) -> Optional[dict]:
    """Calculate payoff dependence and streaks from auditable closed cycles."""
    eligible = [cluster for cluster in clusters if cluster.get('outcome_eligible')]
    if len(eligible) < 5:
        return None
    rows = [
        {
            'total_profit_usd': cluster.get('realized_pnl_usd'),
            'last_active_timestamp': cluster.get('end_timestamp'),
        }
        for cluster in eligible
    ]
    profile = _asymmetric_payoff_profile(rows)
    profile['outcome_basis'] = 'CLOSED_POSITION_CLUSTERS'
    profile['outcome_sample_size'] = len(eligible)
    return profile


def _format_intraday_profile(report: dict) -> dict:
    summary = report.get('summary') or {}
    coverage = float(summary.get('coverage_pct') or 0)
    matched_days = int(summary.get('matched_days') or 0)
    eligible = coverage >= 80 and matched_days >= 30
    return {
        'status': 'AVAILABLE_HEURISTIC' if eligible else 'PARTIAL_HEURISTIC',
        'insight_eligible': eligible,
        'timezone': report['timezone'],
        'coverage_pct': coverage,
        'matched_days': matched_days,
        'exact_attribution_pct': summary.get('exact_attribution_pct'),
        'attribution_quality': summary.get('attribution_quality'),
        'observed_sell_profit_usd': summary.get('observed_sell_profit_usd'),
        'observed_sell_loss_usd': summary.get('observed_sell_loss_usd'),
        'allocated_profit_usd': summary.get('allocated_profit_usd'),
        'allocated_loss_usd': summary.get('allocated_loss_usd'),
        'sessions': report['sessions'] if eligible else [],
        'loss_timing': report['loss_timing'] if eligible else {},
    }


def _intraday_profile(
    address: str,
    daily: List[dict],
    events_override: Optional[List[dict]] = None,
) -> dict:
    if events_override is not None:
        if not events_override:
            return {
                'status': 'NOT_COLLECTED',
                'timezone': 'UTC+7',
                'reason': 'Database chưa có activity timestamp cho wallet này.',
            }
        report = build_intraday_report(address, events_override, daily, 7)
        return _format_intraday_profile(report)
    path = INTRADAY_DIR / f'{address}.json'
    if not path.exists():
        return {
            'status': 'NOT_COLLECTED',
            'timezone': 'UTC+7',
            'reason': 'Chưa có activity timestamp cache; không suy đoán giờ/phiên từ dữ liệu ngày.',
        }
    try:
        cached = json.loads(path.read_text(encoding='utf-8'))
        report = build_intraday_report(address, cached.get('events') or [], daily, 7)
    except (OSError, ValueError, json.JSONDecodeError):
        return {'status': 'INVALID_CACHE', 'timezone': 'UTC+7'}
    return _format_intraday_profile(report)


def build_pnl_wallet_label(
    address: str,
    name: str = '',
    emoji: str = '',
    cohort_cache: Optional[Dict[str, Any]] = None,
    payload_override: Optional[Dict[str, Any]] = None,
    intraday_events_override: Optional[List[dict]] = None,
    activity_events_override: Optional[List[dict]] = None,
    activity_history_exhausted: bool = False,
    stats_payload_override: Optional[dict] = None,
    funding_override: Optional[dict] = None,
    historical_sol_price_usd: Optional[float] = None,
    onchain_verified: bool = False,
    as_of: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    payload = payload_override if payload_override is not None else load_wallet_pnl(address)

    # Activity events for Section 9 & 10 reconstruction
    activity_events = activity_events_override
    if activity_events is None and intraday_events_override:
        activity_events = intraday_events_override

    # Stats payload for Section 5 capital & native balance reconstruction
    stats_payload = stats_payload_override
    if not payload:
        return {
            'schema': 'gmgn_wallet_statistical_label_v6',
            'address': address,
            'name': name,
            'emoji': emoji,
            'meta': {
                'data_mode': 'no_dashboard_data',
                'generated_at': int(datetime.now(tz=timezone.utc).timestamp()),
                'confidence': 'NONE',
                'formula_version': 'wallet_labels_v6_23_weighted_rank_9_metrics_audit_no_inactive_veto',
            },
            'information_value': {
                'tier': 'NO_DASHBOARD_DATA',
                'grade': 'N/A',
                'composite_alpha_score': None,
                'score_type': 'historical_pnl_quality_not_predictive_alpha',
                'forensic_cleanliness': None,
                'actionable_assessment': 'Chưa có GMGN dashboard PnL để phân tích.',
            },
            'alpha_setup': {
                'status': 'INSUFFICIENT_DATA',
                'optimal_setup_archetype': 'INSUFFICIENT_DATA',
                'avoid_setup_archetype': 'INSUFFICIENT_DATA',
            },
            'entry_profile': {'status': 'INSUFFICIENT_DATA'},
            'conviction_behavior': {
                'conviction_verdict': 'INSUFFICIENT_DATA',
                'conviction_analysis': 'Chưa có GMGN dashboard holdings.',
            },
            'timing_profile': {
                'daily': {'status': 'INSUFFICIENT_DATA'},
                'intraday': {'status': 'NOT_COLLECTED'},
            },
            'new_token_predictive_power': {
                'predictive_power_score': None,
                'predictive_tier': 'NOT_MEASURED',
                'lead_time_advantage': None,
                'historical_win_rate_1h': None,
                'copy_trade_recommendation': 'NO_DASHBOARD_DATA',
                'max_recommended_allocation_usd': None,
            },
            'data_quality': {
                'verified_sources': [],
                'section_status': {'dashboard_pnl': 'MISSING'},
                'notes': 'Không fallback sang transfer/evidence cũ.',
            },
            'wallet_class': {
                'primary_wallet_value': 'NO_DASHBOARD_DATA',
                'historical_quality': 'NO_DASHBOARD_DATA',
                'trading_behavior': [],
                'predictive_value': 'NOT_MEASURED',
                'data_quality': 'NONE',
            },
            'group_labels': classify_wallet_groups(),
        }
    rows = [
        row for row in (payload.get('rows') or [])
        if float(row.get('bought_usd') or 0) > 0
    ]
    summary = payload.get('summary') or {}
    daily = payload.get('daily_calendar') or []
    holdings_sufficient = len(rows) >= 20
    pnl_stats = (
        compute_wallet_stats(payload)
        if payload_override is not None
        else get_wallet_stats(address, cohort_cache=cohort_cache)
    ) or {}

    wins = [row for row in rows if float(row.get('total_profit_usd') or 0) > 0]
    losses = [row for row in rows if float(row.get('total_profit_usd') or 0) < 0]
    gross_profit = sum(float(row.get('total_profit_usd') or 0) for row in wins)
    gross_loss = abs(sum(float(row.get('total_profit_usd') or 0) for row in losses))
    total_cost = float(summary.get('total_cost_usd') or 0)
    total_profit = float(summary.get('total_profit_usd') or 0)
    roi = 100 * total_profit / total_cost if total_cost > 0 else None

    # portfolio/stats is collected for 30D only. Preserve it as recent context,
    # while the complete paginated holdings sample supplies historical token WR.
    official_win_rate_30d = _extract_gmgn_winrate(stats_payload)
    sample_win_rate = 100 * len(wins) / len(rows) if rows else None
    win_rate = official_win_rate_30d if official_win_rate_30d is not None else sample_win_rate
    win_rate_basis = 'gmgn_portfolio_stats_30d' if official_win_rate_30d is not None else 'gmgn_holdings_observed_sample'
    recent_tokens_traded_30d = _extract_gmgn_token_num(stats_payload)
    total_tokens_traded = len(rows) if rows else recent_tokens_traded_30d
    token_pf = _profit_factor(gross_profit, gross_loss)
    roi_values = [
        float(row['total_profit_pct'])
        for row in rows if _number(row.get('total_profit_pct')) is not None
    ]
    median_roi = statistics.median(roi_values) if roi_values else None
    daily_profile = _daily_profile(daily)
    activity_profile = _activity_profile(daily, as_of=as_of)
    max_drawdown = float(daily_profile.get('max_drawdown_usd') or 0)
    active_days = int(daily_profile.get('active_days') or 0)
    peak_cumulative_pnl = float(
        (daily_profile.get('risk') or {}).get('peak_cumulative_pnl_usd') or 0
    )
    drawdown_to_peak_pct = (
        round(100 * max_drawdown / peak_cumulative_pnl, 1)
        if peak_cumulative_pnl > 0 else None
    )

    score_pf = daily_profile.get('profit_factor')
    components = {
        'profitability': _clip(50 + roi / 2, 0, 100) if roi is not None else None,
        'profit_factor': (
            100.0
            if active_days and not float(daily_profile.get('loss_usd') or 0)
            else _clip(score_pf / 2 * 100, 0, 100) if score_pf is not None else None
        ),
        'token_win_rate': win_rate,
        'daily_consistency': daily_profile.get('profitable_day_rate_pct'),
        'sample_size': _clip(
            max(
                active_days,
                len(rows),
                min(total_tokens_traded or 0, 30),
            ) / 30 * 100,
            0,
            100,
        ),
    }
    component_weights = {
        # The former 0.15 PnL-drawdown component is excluded. Available
        # components are normalized below; missing portfolio MDD is not zero risk.
        'profitability': 0.25,
        'profit_factor': 0.20,
        'token_win_rate': 0.15,
        'daily_consistency': 0.15,
        'sample_size': 0.10,
    }
    available_weight = sum(
        component_weights[key]
        for key, value in components.items()
        if value is not None
    )
    raw_score = (
        sum(
            component_weights[key] * value
            for key, value in components.items()
            if value is not None
        ) / available_weight
        if available_weight else 50.0
    )
    reliability = (
        0.7 * min(1.0, active_days / 30)
        + 0.3 * min(1.0, len(rows) / 30)
    )
    score = round(50 + (raw_score - 50) * reliability, 1)
    historical_confidence = (
        'HIGH' if active_days >= 30
        else 'MEDIUM' if active_days >= 10
        else 'LOW'
    )
    history_score = min(1.0, active_days / 60) * 100
    recent_activity_score = min(1.0, activity_profile['active_days_30d'] / 10) * 100
    token_sample_score = min(1.0, max(len(rows), min(total_tokens_traded or 0, 30)) / 30) * 100
    confidence_score = round(
        0.35 * history_score
        + 0.35 * activity_profile['recency_score']
        + 0.20 * recent_activity_score
        + 0.10 * token_sample_score,
        1,
    )
    if confidence_score >= 75:
        confidence = 'HIGH'
    elif confidence_score >= 50:
        confidence = 'MEDIUM'
    else:
        confidence = 'LOW'

    if active_days < 5:
        tier, grade = 'INSUFFICIENT_SAMPLE', 'N/A'
    elif total_profit < 0 or score < 40:
        tier, grade = 'LOSS_MAKING', 'D'
    elif score >= 75 and historical_confidence == 'HIGH':
        tier, grade = 'ELITE_PNL', 'A'
    elif score >= 65 and historical_confidence in ('HIGH', 'MEDIUM'):
        tier, grade = 'STRONG_PNL', 'B+'
    elif score >= 50:
        tier, grade = 'MIXED_PNL', 'B-/C+'
    else:
        tier, grade = 'WEAK_PNL', 'C-'

    mc = _market_cap_profile(rows)
    best = mc['best']
    worst = mc['worst']
    profile_rows = (best or {}).get('_members') or []
    profitable_profile_rows = [
        row for row in profile_rows if float(row.get('total_profit_usd') or 0) > 0
    ]
    envelope_rows = profitable_profile_rows if len(profitable_profile_rows) >= 2 else profile_rows
    mc_range = _range((row.get('bought_mc') for row in envelope_rows))
    liquidity_range = _range((row.get('liquidity_usd') for row in envelope_rows))
    age_range = _range(
        (
            row.get('token_age_at_entry_hours')
            for row in envelope_rows
            if 0 <= float(row.get('token_age_at_entry_hours') or -1) <= 8760
        ),
        unit='hours',
    )
    # Reconstruct Position Clusters & Conviction Behavior (Sections 9 & 10)
    position_clusters = build_position_clusters(activity_events) if activity_events else []
    base_conviction = _conviction_profile(rows, activity_events=activity_events)
    if position_clusters:
        cluster_profile = compute_cluster_profile(position_clusters)
        scale_res, cluster_conviction = analyze_conviction_and_scaling(position_clusters)
        scale = scale_res
        if cluster_conviction.get('conviction_verdict') != 'INSUFFICIENT_DATA':
            conviction = {**base_conviction, **cluster_conviction}
            if 'allocation_status' not in cluster_conviction:
                conviction['allocation_status'] = base_conviction.get('allocation_status')
                conviction['historical_allocation_pct'] = base_conviction.get('historical_allocation_pct')
                conviction['allocation_sample_size'] = base_conviction.get('allocation_sample_size')
        else:
            conviction = base_conviction
        destructive_averaging_down = scale_res.get('destructive_averaging_down', False)
    else:
        closed_clusters = [row for row in rows if row.get('is_closed')]
        winning_clusters = [
            row for row in closed_clusters
            if float(row.get('total_profit_usd') or 0) > 0
        ]
        cluster_win_rate = (
            100 * len(winning_clusters) / len(closed_clusters)
            if closed_clusters else None
        )
        cluster_profile = {
            'status': 'TOKEN_AGGREGATE_PROXY',
            'cluster_definition': 'one aggregated GMGN holdings row per token',
            'closed_clusters': len(closed_clusters),
            'winning_clusters': len(winning_clusters),
            'cluster_win_rate_pct': round(cluster_win_rate, 1) if cluster_win_rate is not None else None,
            'limitation': 'Cannot separate multiple entry/exit cycles or theses in the same token.',
        }
        conviction = base_conviction
        scale = _scale_style(rows)
        destructive_averaging_down = None


    payoff_profile = _cluster_payoff_profile(position_clusters) or _asymmetric_payoff_profile(rows)
    payoff_profile['median_outflow_holding_pct'] = _median_outflow_holding_pct(activity_events)

    intraday = _intraday_profile(address, daily, intraday_events_override)
    all_period = (payload.get('periods') or {}).get('all') or {}
    holding = _holding_discipline(rows)
    exit_profile = _exit_efficiency(rows)
    concentration = _concentration_profile(rows)
    transfer = _transfer_dependence(rows)
    age_bands = _token_age_bands(rows)
    session_habit = _session_habit(intraday)
    wallet_class = _classify_wallet(
        tier,
        historical_confidence,
        holdings_sufficient,
        {
            'status': (
                'AVAILABLE'
                if best
                else 'INSUFFICIENT_BUCKET_SAMPLE'
            ),
            'optimal_setup_archetype': best['code'] if best else 'INSUFFICIENT_DATA',
        },
        conviction,
        holding,
        exit_profile,
        scale,
        concentration,
        transfer,
        session_habit,
        age_bands,
    )

    current_open_value = sum(float(row.get('balance_usd') or 0) for row in rows)
    capital_tier = (
        'C5' if current_open_value > 2_000_000
        else 'C4' if current_open_value >= 1_000_000
        else 'C3' if current_open_value >= 300_000
        else 'C2' if current_open_value >= 100_000
        else 'C1' if current_open_value >= 20_000
        else 'C0'
    )

    # Reconstruct Capital & Portfolio Outcome (Section 5)
    capital_reconstructed = reconstruct_equity_outcome(
        activity_events or [],
        rows,
        daily,
        stats_payload=stats_payload,
        total_profit_usd=total_profit,
        total_cost_usd=total_cost,
        activity_history_exhausted=activity_history_exhausted,
        funding_override=funding_override,
        historical_sol_price_usd=historical_sol_price_usd,
        onchain_verified=onchain_verified,
    ) if (activity_events or stats_payload) else {}

    # Requested production groups are computed from normalized token rows plus
    # activity evidence.  Use the strongest observed capital proxy only for the
    # G2 fallback whale rule; missing capital remains unknown and cannot pass it.
    capital_candidates = [
        _number(capital_reconstructed.get('peak_equity_usd')),
        _number(capital_reconstructed.get('starting_equity_usd')),
        _number(capital_reconstructed.get('total_deposits_usd')),
    ]
    capital_base_usd = max((value for value in capital_candidates if value is not None and value > 0), default=None)
    group_labels = classify_wallet_groups(
        holdings=rows,
        activity_events=activity_events or [],
        capital_base_usd=capital_base_usd,
    )

    if capital_reconstructed and capital_reconstructed.get('capital_tier_proxy'):
        capital_tier = capital_reconstructed['capital_tier_proxy']
    capital_verified = capital_reconstructed.get('status') in ('VERIFIED_EQUITY_HISTORY', 'FULL_VERIFIED_EQUITY')
    capital_corroborated = capital_reconstructed.get('status') in (
        'VERIFIED_EQUITY_HISTORY', 'FULL_VERIFIED_EQUITY', 'TRANSACTION_CORROBORATED_EQUITY_HISTORY',
    )
    peak_equity = _number(capital_reconstructed.get('peak_equity_usd'))
    ending_equity = _number(capital_reconstructed.get('ending_equity_usd'))
    equity_drawdown_pct = (
        100 * (peak_equity - ending_equity) / peak_equity
        if capital_corroborated and peak_equity and ending_equity is not None else None
    )
    severe_capital_destruction = (
        equity_drawdown_pct > 80
        if equity_drawdown_pct is not None else None
    )
    risk_profile = {
        **(daily_profile.get('risk') or {}),
        'max_realized_pnl_drawdown_usd': max_drawdown if daily_profile.get('source') else None,
        'realized_pnl_drawdown_to_peak_pnl_pct': drawdown_to_peak_pct,
        'portfolio_max_drawdown_pct': None,
        'portfolio_drawdown_status': 'NOT_RECONSTRUCTED',
        'drawdown_used_for_ranking': False,
        # Legacy aliases retained for existing API consumers; diagnostic only.
        'max_pnl_drawdown_to_peak_pct': drawdown_to_peak_pct,
        'max_drawdown_to_peak_pct': drawdown_to_peak_pct,
        'drawdown_basis': 'CUMULATIVE_REALIZED_PNL_NOT_PORTFOLIO_EQUITY',
        'equity_peak_to_current_drawdown_pct': (
            round(equity_drawdown_pct, 1) if equity_drawdown_pct is not None else None
        ),
        'severe_capital_destruction': severe_capital_destruction,
    }

    if tier in ('ELITE_PNL', 'STRONG_PNL') and confidence != 'LOW':
        recommendation = 'MONITOR_HIGH_PRIORITY_NOT_COPY_VALIDATED'
    elif tier == 'LOSS_MAKING':
        recommendation = 'HISTORICAL_LOSS_AVOID_COPYING'
    else:
        recommendation = 'RESEARCH_ONLY_NOT_COPY_VALIDATED'

    return {
        'schema': 'gmgn_wallet_statistical_label_v6',
        'address': address,
        'name': name or payload.get('wallet_name') or '',
        'emoji': emoji,
        'meta': {
            'data_mode': 'verified_gmgn_portfolio',
            'generated_at': int(datetime.now(tz=timezone.utc).timestamp()),
            'source_fetched_at': payload.get('fetched_at'),
            'token_sample_size': len(rows),
            'total_tokens_traded': total_tokens_traded,
            'daily_sample_size': daily_profile['active_days'],
            'confidence': confidence,
            'confidence_score': confidence_score,
            'formula_version': 'wallet_labels_v6_23_weighted_rank_9_metrics_audit_no_inactive_veto',
        },
        'wallet_class': wallet_class,
        'information_value': {
            'tier': tier,
            'grade': grade,
            'composite_alpha_score': score,
            'score_type': 'historical_pnl_quality_not_predictive_alpha',
            'score_components': {
                key: round(value, 1) if value is not None else None
                for key, value in components.items()
            },
            'available_component_weight': round(available_weight, 2),
            'score_component_sources': {
                'profitability': 'gmgn_profits',
                'profit_factor': 'gmgn_web_daily_profits',
                'token_win_rate': win_rate_basis,
                'daily_consistency': 'gmgn_web_daily_profits',
                'sample_size': 'official_daily_active_days',
            },
            'score_reliability': round(reliability, 3),
            'forensic_cleanliness': None,
            'actionable_assessment': (
                f'PnL {total_profit:,.0f} USD, ROI trên cost '
                f'{roi:.1f}% và confidence {confidence}.'
                if roi is not None else f'PnL {total_profit:,.0f} USD; thiếu cost để tính ROI.'
            ),
        },
        'capital_outcome': capital_reconstructed,
        'performance_profile': {
            'capital_outcome': {
                'starting_equity_usd': capital_reconstructed.get('starting_equity_usd'),
                'ending_equity_usd': capital_reconstructed.get('ending_equity_usd'),
                'peak_equity_usd': capital_reconstructed.get('peak_equity_usd'),
                'average_equity_usd': capital_reconstructed.get('average_equity_usd'),
                'portfolio_growth_usd': capital_reconstructed.get('portfolio_growth_usd'),
                'portfolio_return_pct': capital_reconstructed.get('portfolio_return_pct'),
                'external_capital_flow_usd': capital_reconstructed.get('external_capital_flow_usd'),
                'total_deposits_usd': capital_reconstructed.get('total_deposits_usd'),
                'total_withdrawals_usd': capital_reconstructed.get('total_withdrawals_usd'),
                'absolute_pnl_usd': round(total_profit, 2),
                'roi_on_trading_cost_pct': round(roi, 2) if roi is not None else None,
                'current_open_positions_value_usd': round(current_open_value, 2),
                'capital_tier_proxy': capital_tier,
                'capital_tier_basis': 'reconstructed portfolio equity from activity & holdings' if capital_reconstructed else 'current GMGN open-token value; not full wallet equity',
                'status': capital_reconstructed.get('status', 'PARTIAL_NO_EQUITY_HISTORY'),
            },
            'all_time': {
                'realized_profit_usd': round(float(summary.get('realized_profit_usd') or 0), 2),
                'unrealized_profit_usd': round(float(summary.get('unrealized_profit_usd') or 0), 2),
                'total_profit_usd': round(total_profit, 2),
                'total_cost_usd': round(total_cost, 2),
                'roi_on_cost_pct': round(roi, 2) if roi is not None else None,
                'realized_roi_pct': _number(all_period.get('realized_roi_pct')),
                'realized_cost_usd': _number(all_period.get('realized_cost_usd')),
                'token_win_rate_pct': round(win_rate, 1) if win_rate is not None else None,
                'win_rate_basis': win_rate_basis,
                'holdings_sample_win_rate_pct': round(sample_win_rate, 1) if sample_win_rate is not None else None,
                'historical_token_win_rate_pct': round(sample_win_rate, 1) if sample_win_rate is not None else None,
                'historical_token_win_rate_basis': 'gmgn_paginated_holdings_all_available_history' if rows else 'missing',
                'recent_30d_token_win_rate_pct': round(official_win_rate_30d, 1) if official_win_rate_30d is not None else None,
                'recent_30d_total_tokens_traded': recent_tokens_traded_30d,
                'total_tokens_traded': total_tokens_traded,
                'profit_factor': token_pf,
                'profit_factor_status': _profit_factor_status(gross_profit, gross_loss),
                'median_token_roi_pct': round(median_roi, 2) if median_roi is not None else None,
                'winning_tokens': len(wins),
                'losing_tokens': len(losses),
                'token_metrics_status': 'AVAILABLE' if rows else 'MISSING',
                'cohort_rank': pnl_stats.get('cohort_rank'),
                'cohort_percentile': pnl_stats.get('cohort_percentile'),
            },
            'daily_calendar': daily_profile,
        },
        'position_cluster_profile': cluster_profile,
        'risk_profile': risk_profile,
        'consistency_profile': daily_profile.get('consistency') or {},
        'regime_profile': daily_profile.get('regime') or {},
        'activity_profile': activity_profile,
        'payoff_profile': payoff_profile,
        'alpha_setup': {
            'optimal_setup_archetype': best['code'] if best else 'INSUFFICIENT_DATA',
            'optimal_setup_name': best['label'] if best else None,
            'setup_specialization_score': best['quality_score'] if best else None,
            'avoid_setup_archetype': worst['code'] if worst else 'INSUFFICIENT_DATA',
            'avoid_setup_name': worst['label'] if worst else None,
            'minimum_bucket_sample': mc['minimum_sample'],
            'status': (
                'AVAILABLE'
                if best
                else 'INSUFFICIENT_BUCKET_SAMPLE'
                if holdings_sufficient
                else 'INSUFFICIENT_BUCKET_SAMPLE'
            ),
            'market_cap_breakdown': mc['profiles'],
        },
        'entry_profile': {
            'optimal_market_cap_range': mc_range,
            'optimal_token_age_range': age_range,
            'optimal_liquidity_range': liquidity_range,
            'max_holder_top10_pct': None,
            'basis': 'IQR của token có lãi trong market-cap bucket tốt nhất',
            'validation_status': 'IN_SAMPLE_DESCRIPTIVE_NOT_FORWARD_VALIDATED',
            'status': 'AVAILABLE' if best else 'INSUFFICIENT_DATA',
        },
        'conviction_behavior': conviction,
        'group_labels': group_labels,
        'behavior_profile': {
            'holding': holding,
            'exit': exit_profile,
            'scale': scale,
            'concentration': concentration,
            'transfer': transfer,
            'token_age': age_bands,
            'session': session_habit,
            'destructive_averaging_down': destructive_averaging_down,
        },
        'timing_profile': {
            'daily': {
                'timezone': 'UTC',
                'best_weekday': daily_profile['best_weekday'],
                'worst_weekday': daily_profile['worst_weekday'],
                'weekday_breakdown': daily_profile['weekday_breakdown'],
            },
            'intraday': intraday,
        },
        'new_token_predictive_power': {
            'predictive_power_score': None,
            'predictive_tier': 'NOT_MEASURED',
            'lead_time_advantage': None,
            'historical_win_rate_1h': None,
            'profit_factor': token_pf,
            'copy_trade_recommendation': recommendation,
            'max_recommended_allocation_usd': None,
            'signal_evaluation_thesis': (
                'Dữ liệu hiện tại đo hiệu suất PnL, không có forward return 1h/social benchmark '
                'để kết luận sức mạnh dự báo token mới.'
            ),
        },
        'data_quality': {
            'verified_sources': [
                source for source in ('gmgn_holdings', 'gmgn_profits', daily_profile['source'])
                if source
            ],
            'section_status': {
                'all_time_performance': 'AVAILABLE',
                'daily_performance': 'AVAILABLE' if daily_profile['source'] else 'MISSING',
                'token_setup': (
                    'AVAILABLE'
                    if best
                    else 'INSUFFICIENT_BUCKET_SAMPLE'
                    if holdings_sufficient
                    else 'INSUFFICIENT_BUCKET_SAMPLE'
                ),
                'conviction': 'AVAILABLE' if (conviction.get('conviction_verdict') != 'INSUFFICIENT_DATA' or len(rows) >= 30) else 'INSUFFICIENT_DATA',
                'holding_exit_scale': holding.get('status'),
                'intraday': intraday['status'],
                'intraday_exactness': intraday.get('attribution_quality'),
                'predictive_power': 'NOT_MEASURED',
            },
            'unsupported_claims': [
                claim for claim in [
                    'forensic_cleanliness',
                    'holder_top10_at_entry',
                    'social_lead_time',
                    'forward_win_rate_1h',
                    'recommended_position_size',
                    'historical_position_to_portfolio_allocation' if conviction.get('allocation_status') != 'VERIFIED' else None,
                    'starting_ending_peak_average_equity' if not capital_corroborated else None,
                    'portfolio_growth_and_return' if not capital_corroborated else None,
                    'external_deposit_withdrawal_flow' if not capital_corroborated else None,
                    'full_mark_to_market_equity_curve' if not capital_verified else None,
                    'buy_sequence_and_buy_intervals' if not position_clusters else None,
                    'destructive_averaging_down' if not position_clusters else None,
                    'true_multi_cycle_position_clusters' if not position_clusters else None,
                ] if claim is not None
            ],

            'notes': 'Trường không có bằng chứng được để null/NOT_MEASURED; không dùng giá trị mặc định.',
        },
    }
