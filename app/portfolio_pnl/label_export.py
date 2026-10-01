"""Deterministic, atomic CSV/JSON exports for wallet labels."""
import csv
import io
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Iterable, List


CSV_FIELDS = [
    'Address', 'Emoji', 'Name', 'Rank', 'Rank_Status', 'Rank_Rule', 'Performance_Score',
    'Overall_Rank_Score', 'Rank_Confidence', 'Available_Rank_Weight',
    'Confidence', 'Confidence_Score', 'Absolute_PnL_USD', 'PnL_Pct',
    'Starting_Equity_USD', 'Ending_Equity_USD', 'Total_Deposits_USD', 'Total_Withdrawals_USD',
    'Portfolio_Growth_USD',
    'Portfolio_Return_Pct', 'Historical_PF', 'PF_30D', 'PnL_30D_USD',
    'Token_Win_Rate_Pct', 'Profitable_Day_Rate_Pct', 'Cluster_WR_Pct',
    'Conviction', 'Clan_ID', 'Clan_Size', 'Clan_Signal', 'Priority',
    'Performance_Label', 'Profile_Label', 'Setup_Label', 'Behavior_Labels',
    'Group_Labels', 'G1_Early_Lowcap_Rate_Pct', 'G2_Whale', 'G2_Conviction',
    'G4_Max_Realized_PnL_USD',
]

RANK_ORDER = {'S': 0, 'A': 1, 'B': 2, 'C': 3, 'D': 4}
PRIORITY_ORDER = {'CHECK_FIRST': 0, 'HIGH': 1, 'CHECK': 2, 'MEDIUM': 3, 'NORMAL': 4}

_EXPORT_LOCK = threading.Lock()


def csv_row(item: dict) -> dict:
    labels = item.get('labels') or {}
    metrics = item.get('metrics') or {}
    priority = item.get('priority') or {}
    groups = metrics.get('group_features') or {}
    g1 = groups.get('g1') or {}
    g2 = groups.get('g2') or {}
    g4 = groups.get('g4') or {}
    return {
        'Address': item.get('address') or '',
        'Emoji': item.get('emoji') or '',
        'Name': item.get('name') or '',
        'Rank': labels.get('rank') or '',
        'Rank_Status': labels.get('rank_status') or '',
        'Rank_Rule': labels.get('rank_rule') or '',
        'Performance_Score': metrics.get('performance_score'),
        'Overall_Rank_Score': metrics.get('overall_rank_score'),
        'Rank_Confidence': metrics.get('rank_confidence'),
        'Available_Rank_Weight': metrics.get('available_rank_weight'),
        'Confidence': metrics.get('confidence') or '',
        'Confidence_Score': metrics.get('confidence_score'),
        'Absolute_PnL_USD': metrics.get('absolute_pnl_usd'),
        'PnL_Pct': metrics.get('roi_on_trading_cost_pct'),
        'Starting_Equity_USD': metrics.get('starting_equity_usd'),
        'Ending_Equity_USD': metrics.get('ending_equity_usd'),
        'Total_Deposits_USD': metrics.get('total_deposits_usd'),
        'Total_Withdrawals_USD': metrics.get('total_withdrawals_usd'),
        'Portfolio_Growth_USD': metrics.get('portfolio_growth_usd'),
        'Portfolio_Return_Pct': metrics.get('portfolio_return_pct'),
        'Historical_PF': metrics.get('historical_profit_factor'),
        'PF_30D': metrics.get('profit_factor_30d'),
        'PnL_30D_USD': metrics.get('pnl_30d_usd'),
        'Token_Win_Rate_Pct': metrics.get('token_win_rate_pct'),
        'Profitable_Day_Rate_Pct': metrics.get('profitable_day_rate_pct'),
        'Cluster_WR_Pct': metrics.get('cluster_win_rate_pct'),
        'Conviction': metrics.get('conviction') or '',
        'Clan_ID': priority.get('clan_id') or '',
        'Clan_Size': priority.get('clan_size'),
        'Clan_Signal': priority.get('level') or 'NORMAL',
        'Priority': priority.get('action') or 'NORMAL',
        'Performance_Label': labels.get('performance') or '',
        'Profile_Label': labels.get('profile') or '',
        'Setup_Label': labels.get('setup') or '',
        'Behavior_Labels': '|'.join(labels.get('behavior') or []),
        'Group_Labels': '|'.join(metrics.get('group_labels') or []),
        'G1_Early_Lowcap_Rate_Pct': g1.get('early_lowcap_rate_pct'),
        'G2_Whale': g2.get('whale_size'),
        'G2_Conviction': g2.get('conviction'),
        'G4_Max_Realized_PnL_USD': g4.get('max_realized_pnl_single_token_usd'),
    }


def sort_labels(labels: Iterable[dict]) -> List[dict]:
    def queue_order(item):
        rank = (item.get('labels') or {}).get('rank')
        level = (item.get('priority') or {}).get('level')
        if level == 'PRIORITY_ALERT' and rank in ('S', 'A'):
            return 0
        if level == 'PRIORITY_ALERT' and rank == 'B':
            return 1
        if rank == 'S':
            return 2
        if rank == 'D':
            return 7
        if level == 'CLAN_ALERT':
            return 3
        return {'A': 4, 'B': 5, 'C': 6}.get(rank, 8)

    return sorted(
        labels,
        key=lambda item: (
            queue_order(item),
            RANK_ORDER.get((item.get('labels') or {}).get('rank'), 9),
            item.get('name') or '',
            item.get('address') or '',
        ),
    )


def _atomic_write(path: Path, content: str, encoding: str = 'utf-8') -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding=encoding, newline='') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


SUMMARY_CSV_FIELDS = [
    'Wallet', 'Address', 'Rank', 'Rank Score', 'Rank Confidence', 'Perf Score', 'Confidence', 'Start Equity', 'End Equity',
    'Growth', 'Abs PnL', 'Cluster WR', 'Conviction', 'Clan ID', 'Clan Size',
    'Clan Signal', 'Priority',
]


def format_usd(val, is_pnl: bool = False) -> str:
    if val is None or val == '':
        return '—'
    try:
        v = float(val)
    except (ValueError, TypeError):
        return '—'
    sign = '+' if (is_pnl and v > 0) else '-' if v < 0 else ''
    v_abs = abs(v)
    if v_abs >= 1_000_000:
        return f"{sign}${v_abs/1_000_000:.1f}M"
    elif v_abs >= 1_000:
        return f"{sign}${v_abs/1_000:.0f}K"
    else:
        return f"{sign}${v_abs:.0f}"


def format_pct(val, with_sign: bool = False) -> str:
    if val is None or val == '':
        return '—'
    try:
        v = float(val)
        sign = '+' if (with_sign and v > 0) else '-' if (with_sign and v < 0) else ''
        return f"{sign}{abs(v):.0f}%"
    except (ValueError, TypeError):
        return '—'


def build_clan_map(labels: Iterable[dict]) -> dict:
    clan_map = {}
    for w in labels:
        cid = (w.get('priority') or {}).get('clan_id')
        if cid and cid not in clan_map:
            clan_map[cid] = f"C{len(clan_map)+1:02d}"
    return clan_map


def summary_csv_row(item: dict, clan_map: dict = None) -> dict:
    labels = item.get('labels') or {}
    metrics = item.get('metrics') or {}
    priority = item.get('priority') or {}
    rank = labels.get('rank') or '—'
    addr = item.get('address') or ''
    name = item.get('name') or ''
    short_addr = f"{addr[:4]}...{addr[-4:]}" if len(addr) >= 8 else addr
    wallet = f"{name} ({short_addr})" if name else short_addr

    score = f"{metrics.get('performance_score'):.0f}" if metrics.get('performance_score') is not None else '—'
    rank_score = f"{metrics.get('overall_rank_score'):.1f}" if metrics.get('overall_rank_score') is not None else '—'
    rank_confidence = format_pct((metrics.get('rank_confidence') or 0) * 100)
    conf = metrics.get('confidence') or '—'
    start_eq = format_usd(metrics.get('starting_equity_usd'))
    end_eq = format_usd(metrics.get('ending_equity_usd'))

    growth_val = metrics.get('portfolio_return_pct')
    if growth_val is None:
        growth_val = metrics.get('roi_on_trading_cost_pct')
    growth = format_pct(growth_val, with_sign=True)

    abs_pnl = format_usd(metrics.get('absolute_pnl_usd'), is_pnl=True)
    c_wr = format_pct(metrics.get('cluster_win_rate_pct'))

    conviction_v2 = labels.get('conviction_v2') or {}
    conv_raw = (conviction_v2.get('label') or metrics.get('conviction') or '—').lower()
    conv = conv_raw.capitalize() if conv_raw != '—' else '—'

    cid_raw = priority.get('clan_id')
    clan_id = (clan_map.get(cid_raw, cid_raw) if clan_map else cid_raw) or '—'
    clan_sz = str(priority.get('clan_size')) if priority.get('clan_size') else '—'

    level_map = {
        'PRIORITY_ALERT': 'Priority Alert',
        'CLAN_WATCH': 'Clan Watch',
        'CLAN_ALERT': 'Clan Alert',
        'NORMAL': 'Normal',
    }
    clan_sig = level_map.get(priority.get('level'), (priority.get('level') or 'Normal').replace('_', ' ').title())

    action_map = {
        'CHECK_FIRST': 'CHECK FIRST',
        'CHECK': 'CHECK',
        'NORMAL': 'Low' if rank in ('C', 'D') else 'Normal',
    }
    prio = action_map.get(priority.get('action'), priority.get('action') or 'Normal')

    return {
        'Wallet': wallet,
        'Address': addr,
        'Rank': rank,
        'Rank Score': rank_score,
        'Rank Confidence': rank_confidence,
        'Perf Score': score,
        'Confidence': conf,
        'Start Equity': start_eq,
        'End Equity': end_eq,
        'Growth': growth,
        'Abs PnL': abs_pnl,
        'Cluster WR': c_wr,
        'Conviction': conv,
        'Clan ID': clan_id,
        'Clan Size': clan_sz,
        'Clan Signal': clan_sig,
        'Priority': prio,
    }


def write_label_exports(
    labels: Iterable[dict], json_path: Path, *, apply_clans: bool = True,
) -> dict:
    """Write detailed exports plus matching 14-field executive CSV/JSON."""
    json_path = Path(json_path)
    csv_path = json_path.with_suffix('.csv')
    summary_path = json_path.parent / (json_path.stem.replace('_labels', '_summary') + '.csv')
    if summary_path == csv_path:
        summary_path = json_path.parent / f"{json_path.stem}_summary.csv"
    summary_json_path = summary_path.with_suffix('.json')
    from app.portfolio_pnl.clan_engine import apply_clan_signals

    prepared = apply_clan_signals(labels) if apply_clans else list(labels)
    ordered = sort_labels(prepared)
    clan_map = build_clan_map(ordered)
    json_text = json.dumps(ordered, indent=2, ensure_ascii=False) + '\n'

    # 1. Full Raw CSV
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
    writer.writeheader()
    writer.writerows(csv_row(item) for item in ordered)

    # 2. Executive 14-Column Summary CSV
    summary_rows = [summary_csv_row(item, clan_map) for item in ordered]
    sum_stream = io.StringIO(newline='')
    sum_writer = csv.DictWriter(sum_stream, fieldnames=SUMMARY_CSV_FIELDS)
    sum_writer.writeheader()
    sum_writer.writerows(summary_rows)
    summary_json_text = json.dumps(summary_rows, indent=2, ensure_ascii=False) + '\n'

    _atomic_write(json_path, json_text)
    _atomic_write(csv_path, stream.getvalue(), encoding='utf-8-sig')
    _atomic_write(summary_path, sum_stream.getvalue(), encoding='utf-8-sig')
    _atomic_write(summary_json_path, summary_json_text)
    return {
        'json': str(json_path),
        'csv': str(csv_path),
        'summary_csv': str(summary_path),
        'summary_json': str(summary_json_path),
        'wallets': len(ordered),
    }


def upsert_label_exports(label: dict, json_path: Path) -> dict:
    """Replace one wallet by address and atomically refresh both export files."""
    address = (label.get('address') or '').strip()
    if not address:
        raise ValueError('Label không có địa chỉ ví.')
    json_path = Path(json_path)
    with _EXPORT_LOCK:
        existing = []
        if json_path.exists():
            existing = json.loads(json_path.read_text(encoding='utf-8'))
            if not isinstance(existing, list):
                raise ValueError('File JSON label hiện tại phải chứa một array.')
        labels = [item for item in existing if item.get('address') != address]
        labels.append(label)
        return write_label_exports(labels, json_path)
