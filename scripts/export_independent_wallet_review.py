#!/usr/bin/env python3
"""Create a conservative, independently reviewed wallet ordering.

This export does not mutate the engine labels. It reads the detailed label JSON,
applies stricter capital/risk rules, computes a review score, and emits a flat
CSV/JSON intended for human triage.
"""
import argparse
import csv
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.portfolio_pnl.label_export import build_clan_map, summary_csv_row


OUTPUT_FIELDS = [
    'Wallet', 'Address', 'Rank', 'Perf Score', 'Confidence', 'Start Equity',
    'End Equity', 'Growth', 'Abs PnL', 'Cluster WR', 'Conviction', 'Clan ID',
    'Clan Size', 'Clan Signal', 'Priority', 'Engine Rank', 'Review Score',
    'Review Notes',
]
RANK_ORDER = {'S': 0, 'A': 1, 'B': 2, 'C': 3, 'D': 4}


def number(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def effective_pf(metrics: dict, prefix: str):
    value = number(metrics.get(prefix))
    status = metrics.get(f'{prefix}_status')
    return float('inf') if value is None and status == 'NO_LOSS' else value


def review_score(item: dict) -> float:
    """Score 0-100 for ordering within a hard-gated review rank."""
    metrics = item.get('metrics') or {}
    score = number(metrics.get('performance_score')) or 0.0

    confidence = metrics.get('confidence')
    score += 5 if confidence == 'HIGH' else 0 if confidence == 'MEDIUM' else -10

    historical_pf = effective_pf(metrics, 'historical_profit_factor')
    if historical_pf is not None:
        score += 5 if historical_pf >= 2 else 3 if historical_pf >= 1.5 else -8 if historical_pf < 1 else 0

    token_wr = number(metrics.get('token_win_rate_pct'))
    if token_wr is not None:
        score += 4 if token_wr >= 45 else 1 if token_wr >= 30 else -5 if token_wr < 22 else 0

    profitable_days = number(metrics.get('profitable_day_rate_pct'))
    if profitable_days is not None:
        score += 4 if profitable_days >= 55 else 2 if profitable_days >= 50 else -4 if profitable_days < 40 else 0

    days_since = number(metrics.get('days_since_last_active'))
    if days_since is None:
        score -= 25
    elif days_since <= 7:
        score += 5
    elif days_since < 14:
        score += 3
    elif days_since > 60:
        score -= 25
    elif days_since > 30:
        score -= 8

    pnl_30d = number(metrics.get('pnl_30d_usd'))
    score += 3 if pnl_30d is not None and pnl_30d > 0 else -5 if pnl_30d is not None and pnl_30d < 0 else 0

    pf_30d = effective_pf(metrics, 'profit_factor_30d')
    if pf_30d is not None:
        score += 3 if pf_30d >= 1.5 else 2 if pf_30d > 1.2 else -7 if pf_30d < 0.7 else 0

    drawdown = number(metrics.get('max_drawdown_to_peak_pct'))
    if drawdown is not None:
        score += 3 if drawdown <= 40 else -10 if drawdown > 80 else 0

    cluster_wr = number(metrics.get('cluster_win_rate_pct'))
    if cluster_wr is not None:
        score += 2 if cluster_wr >= 50 else -2 if cluster_wr < 25 else 0

    score += 3 if metrics.get('conviction') == 'HIGH' else -5 if metrics.get('conviction') == 'RISKY' else 0
    score -= 15 if metrics.get('one_hit_dependent') else 0
    score -= 10 if metrics.get('abnormal_current_loss_streak') else 0
    return round(max(0.0, min(100.0, score)), 1)


def reviewed_rank(item: dict, score: float):
    metrics = item.get('metrics') or {}
    labels = item.get('labels') or {}
    engine_rule = labels.get('rank_rule')
    performance_score = number(metrics.get('performance_score'))
    days_since = number(metrics.get('days_since_last_active'))
    active_days = number(metrics.get('active_days')) or 0
    active_30d = number(metrics.get('active_days_30d')) or 0
    pnl = number(metrics.get('absolute_pnl_usd'))
    pnl_30d = number(metrics.get('pnl_30d_usd'))
    pf_30d = effective_pf(metrics, 'profit_factor_30d')
    historical_pf = effective_pf(metrics, 'historical_profit_factor')
    growth = number(metrics.get('portfolio_growth_usd'))
    portfolio_return = number(metrics.get('portfolio_return_pct'))

    if days_since is None:
        return 'C', 'Thiếu dữ liệu activity; chỉ xếp watchlist.'
    if days_since > 60:
        return 'D', 'Không hoạt động trên 60 ngày.'
    if active_days >= 5 and (
        (pnl is not None and pnl < -100_000)
        or (performance_score is not None and performance_score < 40)
    ):
        return 'D', 'Lỗ lịch sử hoặc Performance Score dưới 40.'
    if active_30d >= 5 and pnl_30d is not None and pnl_30d < 0 and pf_30d is not None and pf_30d < 0.7:
        return 'D', 'PnL 30D âm và Profit Factor 30D dưới 0.7.'
    if engine_rule == 'D5_DESTRUCTIVE_POSITION_BEHAVIOR':
        return 'D', 'Có destructive averaging-down đã quan sát.'
    if engine_rule == 'D4_SEVERE_CAPITAL_DESTRUCTION' and growth is not None and growth < 0:
        return 'D', 'Drawdown vốn trên 80% và growth sau external flow vẫn âm.'

    if metrics.get('one_hit_dependent'):
        return 'C', 'One-hit dependent; giới hạn tối đa Rank C.'
    if metrics.get('abnormal_current_loss_streak'):
        return 'C', 'Chuỗi thua hiện tại bất thường; giới hạn Rank C.'

    capital_gate = bool(
        growth is not None and portfolio_return is not None
        and (
            growth >= 1_000_000
            or (growth >= 300_000 and portfolio_return >= 25)
        )
    )
    token_wr = number(metrics.get('token_win_rate_pct')) or 0.0
    profitable_days = number(metrics.get('profitable_day_rate_pct')) or 0.0
    s_gates = ((item.get('rank_audit') or {}).get('s_gates') or {})
    fat_tail_door = (((s_gates.get('S2') or {}).get('evidence') or {}).get('token_wr_door2_pass') is True)
    repeatability_gate = (s_gates.get('S3') or {}).get('passed') is True
    conviction_gate = (s_gates.get('S4') or {}).get('passed') is True
    if (
        capital_gate
        and (performance_score or 0) >= 75
        and historical_pf is not None and historical_pf >= 1.5
        and (token_wr >= 45 or fat_tail_door)
        and profitable_days >= 50
        and repeatability_gate and conviction_gate
    ):
        return 'S', 'Đạt đủ bốn gate S với Portfolio Growth có bằng chứng.'

    drawdown = number(metrics.get('max_drawdown_to_peak_pct'))
    if (
        score >= 65
        and metrics.get('confidence') == 'HIGH'
        and days_since < 14
        and active_30d >= 7
        and pnl_30d is not None and pnl_30d > 0
        and pf_30d is not None and pf_30d > 1.2
        and historical_pf is not None and historical_pf >= 1.1
        and (drawdown is None or drawdown <= 70)
    ):
        return 'A', 'Hiệu suất mạnh, gần đây tích cực và rủi ro chấp nhận được.'

    recent_result_ok = (
        (pnl_30d is not None and pnl_30d >= 0)
        or (pf_30d is not None and pf_30d >= 0.9)
    )
    if (
        score >= 52
        and metrics.get('confidence') in ('HIGH', 'MEDIUM')
        and days_since <= 30
        and active_30d >= 4
        and recent_result_ok
        and historical_pf is not None and historical_pf >= 0.9
    ):
        return 'B', 'Đủ chất lượng và hoạt động gần đây cho nhóm B.'
    return 'C', 'Watchlist: chưa đủ gate A/B hoặc bằng chứng chưa mạnh.'


def review_priority(item: dict, rank: str, score: float) -> str:
    clan_level = (item.get('priority') or {}).get('level')
    if rank == 'S':
        return 'CHECK FIRST'
    if rank == 'A':
        return 'CHECK FIRST' if score >= 80 or clan_level == 'PRIORITY_ALERT' else 'CHECK'
    if rank == 'B':
        return 'CHECK' if score >= 75 and clan_level == 'PRIORITY_ALERT' else 'WATCH'
    return 'WATCH' if rank == 'C' else 'AVOID'


def build_rows(labels: list) -> list:
    clan_map = build_clan_map(labels)
    reviewed = []
    for item in labels:
        score = review_score(item)
        rank, note = reviewed_rank(item, score)
        engine_rank = (item.get('labels') or {}).get('rank') or '—'
        if rank != engine_rank:
            note = f'Đổi từ engine {engine_rank} sang {rank}. {note}'
        row = summary_csv_row(item, clan_map)
        row['Rank'] = rank
        row['Priority'] = review_priority(item, rank, score)
        row['Engine Rank'] = engine_rank
        row['Review Score'] = f'{score:.1f}'
        row['Review Notes'] = note
        reviewed.append((item, row, score))

    reviewed.sort(key=lambda value: (
        RANK_ORDER[value[1]['Rank']],
        -value[2],
        -(number((value[0].get('metrics') or {}).get('absolute_pnl_usd')) or 0),
        value[1]['Address'],
    ))
    return [row for _item, row, _score in reviewed]


def main() -> int:
    parser = argparse.ArgumentParser(description='Export independent reviewed wallet ordering')
    parser.add_argument('input_json', type=Path)
    parser.add_argument('-o', '--output', type=Path, required=True, help='Output JSON path')
    args = parser.parse_args()

    labels = json.loads(args.input_json.read_text(encoding='utf-8'))
    if not isinstance(labels, list):
        raise ValueError('Detailed label input must be a JSON array.')
    rows = build_rows(labels)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

    csv_path = args.output.with_suffix('.csv')
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=OUTPUT_FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    csv_path.write_text('\ufeff' + stream.getvalue(), encoding='utf-8')

    counts = {rank: sum(row['Rank'] == rank for row in rows) for rank in RANK_ORDER}
    changes = sum(row['Rank'] != row['Engine Rank'] for row in rows)
    print(json.dumps({
        'json': str(args.output), 'csv': str(csv_path), 'wallets': len(rows),
        'rank_counts': counts, 'rank_changes_vs_engine': changes,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
