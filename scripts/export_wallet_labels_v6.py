#!/usr/bin/env python3
"""Export schema v6 rank, metrics and clan priority from cached SQLite data."""
import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.wallet_identity import load_message_wallets
from app.portfolio_pnl.analysis_pipeline import analyze_wallet_from_database
from app.portfolio_pnl.auto_label import DEFAULT_LABEL_JSON
from app.portfolio_pnl.label_export import write_label_exports
from app.portfolio_pnl.label_projection import project_wallet_label
from app.portfolio_pnl.single_wallet_pipeline import _data_availability
from app.portfolio_pnl.store import PnlStore


def main() -> int:
    parser = argparse.ArgumentParser(description='Export deterministic wallet rank schema v6')
    parser.add_argument('-o', '--output', default=str(DEFAULT_LABEL_JSON))
    args = parser.parse_args()
    labels = []
    errors = []
    store = PnlStore()
    for identity in load_message_wallets():
        try:
            report = analyze_wallet_from_database(identity['address'], store=store)
            label = project_wallet_label(
                report, name=identity['name'], emoji=identity['emoji'],
            )
            label['data_availability'] = _data_availability(
                store, identity['address'], report, refreshed=False,
                as_of=datetime.now(tz=timezone.utc),
                required_activity_days=1095,
            )
            if label['data_availability']['status'] != 'READY':
                label['labels']['rank_status'] = 'PROVISIONAL'
            labels.append(label)
        except Exception as exc:
            errors.append({'address': identity['address'], 'error': str(exc)})
    result = write_label_exports(labels, Path(args.output))
    result['rank_counts'] = dict(Counter(item['labels']['rank'] for item in labels))
    result['errors'] = errors
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
