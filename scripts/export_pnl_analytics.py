#!/usr/bin/env python3
"""Export cohort + per-wallet PnL analytics to JSON."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.portfolio_pnl.analytics import compute_cohort_stats


def main():
    parser = argparse.ArgumentParser(description='Export GMGN portfolio PnL analytics')
    parser.add_argument(
        '-o', '--output',
        default=str(ROOT / 'data' / 'portfolio_pnl' / 'analytics_report.json'),
        help='Output JSON path',
    )
    args = parser.parse_args()

    report = compute_cohort_stats(include_wallets=True)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f'Wrote {out} ({report["wallets_with_payload"]} wallets)')


if __name__ == '__main__':
    main()
