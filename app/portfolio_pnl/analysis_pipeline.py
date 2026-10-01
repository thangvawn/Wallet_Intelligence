"""Database-only statistical analysis pipeline for one wallet."""
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict

from app.portfolio_pnl.store import PnlStore
from app.portfolio_pnl.wallet_labels import build_pnl_wallet_label

FEATURE_VERSION = 'wallet_features_v6_13_groups'
FORMULA_VERSION = 'wallet_labels_v6_23_weighted_rank_9_metrics_audit_no_inactive_veto'


def _content_digest(value: Any) -> str:
    """Hash complete normalized input content instead of freshness metadata only."""
    body = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    )
    return hashlib.sha256(body.encode()).hexdigest()


def _nearest_historical_price(snapshot: dict, timestamp: int):
    if timestamp <= 0:
        return None
    candidates = []
    for row in snapshot.get('prices') or []:
        try:
            point_time = int(float(row[0]) / 1000)
            price = float(row[1])
        except (IndexError, TypeError, ValueError):
            continue
        candidates.append((abs(point_time - timestamp), price))
    if not candidates:
        return None
    distance, price = min(candidates)
    return price if distance <= 2 * 86400 else None


def _input_hash(
    dashboard: dict, intraday: dict, activity: dict, stats: dict, funding: dict,
    historical_sol_price, onchain_evidence: dict = None, as_of_date: str = '',
) -> str:
    body = json.dumps(
        {
            'dashboard_sha256': _content_digest(dashboard),
            'intraday_sha256': _content_digest(intraday),
            'activity_sha256': _content_digest(activity),
            'stats_sha256': _content_digest(stats),
            'funding_sha256': _content_digest(funding),
            'historical_sol_price': historical_sol_price,
            'onchain_evidence_sha256': _content_digest(onchain_evidence or {}),
            'as_of_date': as_of_date,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )
    return hashlib.sha256(body.encode()).hexdigest()


def analyze_wallet_from_database(
    wallet_address: str, store: PnlStore = None,
    as_of: datetime = None,
) -> Dict[str, Any]:
    """Load dashboard, intraday, activity, and stats snapshots from SQLite and compute a fresh report."""
    address = wallet_address.strip()
    store = store or PnlStore()
    analysis_time = as_of or datetime.now(tz=timezone.utc)
    if analysis_time.tzinfo is None:
        analysis_time = analysis_time.replace(tzinfo=timezone.utc)
    else:
        analysis_time = analysis_time.astimezone(timezone.utc)
    as_of_date = analysis_time.date().isoformat()
    payload = store.load_dashboard_snapshot(address)
    if not payload:
        raise FileNotFoundError(
            'Wallet chưa có dashboard snapshot trong database; hãy chờ job cập nhật nền hoàn tất.'
        )
    intraday = store.load_intraday_snapshot(address) or {}
    activity = store.load_activity_snapshot(address) or {}
    stats = store.load_stats_snapshot(address) or {}
    funding_snapshot = store.load_external_snapshot(
        'solana_rpc', 'funded_by', address,
    ) or {}
    funding = funding_snapshot.get('data') or {}
    onchain_evidence = store.load_external_snapshot(
        'solana_rpc', 'onchain_evidence', address,
    ) or {}
    onchain_txs = onchain_evidence.get('transactions') or {}
    onchain_funding = onchain_evidence.get('initial_funding') or {}
    if not funding and onchain_funding:
        funding = onchain_funding

    price_snapshot = store.load_external_snapshot(
        'coingecko', 'market_chart', 'SOL',
    ) or store.load_external_snapshot(
        'binance_spot', 'market_chart', 'SOL',
    ) or {}
    common = stats.get('common') or {}
    funding_timestamp = int(float(
        common.get('fund_from_ts') or funding.get('block_time') or 0
    ))
    historical_sol_price = _nearest_historical_price(
        price_snapshot, funding_timestamp,
    )

    # Enrich activity events with on-chain fees, counterparty transfers and pre/post balances
    raw_events = activity.get('events') or []
    enriched_activity_events = []
    for e in raw_events:
        e_copy = dict(e)
        sig = str(e_copy.get('tx_hash') or '')
        if sig in onchain_txs:
            tx_info = onchain_txs[sig]
            e_copy['fee_sol'] = tx_info.get('total_fee_sol')
            e_copy['jito_tip_sol'] = tx_info.get('jito_tip_sol')
            e_copy['pre_balance_sol'] = tx_info.get('pre_balance_sol')
            e_copy['post_balance_sol'] = tx_info.get('post_balance_sol')
            e_copy['balance_verified'] = tx_info.get('balance_verified', False)
            if tx_info.get('transfer'):
                t = tx_info['transfer']
                e_copy['from_address'] = t.get('source')
                e_copy['to_address'] = t.get('destination')
                e_copy['corroborated'] = t.get('corroborated', False)
                e_copy['is_cex_deposit'] = t.get('is_cex_deposit', False)
                e_copy['is_cex_withdrawal'] = t.get('is_cex_withdrawal', False)
                e_copy['cex_name'] = t.get('cex_name')
            flags = list(e_copy.get('flags') or [])
            if 'total_fees_unverified' in flags:
                flags.remove('total_fees_unverified')
            e_copy['flags'] = flags
        enriched_activity_events.append(e_copy)

    input_hash = _input_hash(
        payload, intraday, activity, stats, funding, historical_sol_price,
        onchain_evidence, as_of_date=as_of_date,
    )
    cached = store.load_label_run(address, FORMULA_VERSION, input_hash)
    if cached:
        report = cached['payload']
        # Persist the pipeline metadata before projecting the report.  The
        # canonical serving layer uses these fields to decide whether its
        # current rows are reusable.  Older code added this metadata only
        # after the projection, which made the first projection look like an
        # unversioned report and forced callers to write canonical data a
        # second time.
        report['pipeline'] = {
            'input': 'sqlite.dashboard_snapshots',
            'intraday_input': 'sqlite.intraday_snapshots' if intraday else 'not_collected',
            'activity_input': 'sqlite.wallet_activity_snapshots' if activity else 'not_collected',
            'stats_input': 'sqlite.raw_pages.portfolio/stats' if stats else 'not_collected',
            'computed_at': cached['computed_at'],
            'analysis_persisted': True,
            'served_from_persisted_analysis': True,
            'json_export_used': False,
            'feature_version': FEATURE_VERSION,
            'formula_version': FORMULA_VERSION,
            'as_of_date_utc': as_of_date,
            'input_hash': input_hash,
            'feature_run_id': cached['feature_run_id'],
            'label_run_id': cached['id'],
        }
        from app.portfolio_pnl.canonical_data import sync_canonical_wallet
        from app.portfolio_pnl.label_projection import project_wallet_label
        sync_canonical_wallet(
            store, address, report=report,
            projected_label=project_wallet_label(report),
        )
        return report

    onchain_verified = bool(
        onchain_evidence.get('status') == 'complete'
        and (
            len(onchain_txs) >= 10
            or onchain_evidence.get('verified_balances_count', 0) >= 10
        )
    )

    report = build_pnl_wallet_label(
        address,
        payload.get('wallet_name') or '',
        payload_override=payload,
        intraday_events_override=intraday.get('events') or [],
        activity_events_override=enriched_activity_events,
        activity_history_exhausted=activity.get('history_exhausted') is True,
        stats_payload_override=stats,
        funding_override=funding,
        historical_sol_price_usd=historical_sol_price,
        onchain_verified=onchain_verified,
        as_of=analysis_time,
    )

    report['analysis_version'] = {
        'feature_version': FEATURE_VERSION,
        'formula_version': FORMULA_VERSION,
        'as_of_date_utc': as_of_date,
        'input_hash': input_hash,
    }
    feature_payload = {
        'wallet_address': address,
        'performance_profile': report.get('performance_profile'),
        'capital_outcome': report.get('capital_outcome'),
        'position_cluster_profile': report.get('position_cluster_profile'),
        'risk_profile': report.get('risk_profile'),
        'consistency_profile': report.get('consistency_profile'),
        'regime_profile': report.get('regime_profile'),
        'activity_profile': report.get('activity_profile'),
        'payoff_profile': report.get('payoff_profile'),
        'alpha_setup': report.get('alpha_setup'),
        'entry_profile': report.get('entry_profile'),
        'conviction_behavior': report.get('conviction_behavior'),
        'group_labels': report.get('group_labels'),
        'timing_profile': report.get('timing_profile'),
        'behavior_profile': report.get('behavior_profile'),
        'wallet_class': report.get('wallet_class'),
        'data_quality': report.get('data_quality'),
    }
    feature_run_id = store.save_feature_run(
        address,
        FEATURE_VERSION,
        input_hash,
        feature_payload,
    )
    label_run_id = store.save_label_run(
        address,
        FORMULA_VERSION,
        input_hash,
        feature_run_id,
        report,
    )
    # Set the version/hash metadata before writing the current serving rows;
    # sync_canonical_wallet uses it for idempotent projection and cache reuse.
    report['pipeline'] = {
        'input': 'sqlite.dashboard_snapshots',
        'intraday_input': (
            'sqlite.intraday_snapshots'
            if intraday else 'not_collected'
        ),
        'activity_input': (
            'sqlite.wallet_activity_snapshots'
            if activity else 'not_collected'
        ),
        'stats_input': (
            'sqlite.raw_pages.portfolio/stats'
            if stats else 'not_collected'
        ),
        'computed_at': int(analysis_time.timestamp()),
        'analysis_persisted': True,
        'served_from_persisted_analysis': False,
        'json_export_used': False,
        'feature_version': FEATURE_VERSION,
        'formula_version': FORMULA_VERSION,
        'as_of_date_utc': as_of_date,
        'input_hash': input_hash,
        'feature_run_id': feature_run_id,
        'label_run_id': label_run_id,
    }
    from app.portfolio_pnl.canonical_data import sync_canonical_wallet
    from app.portfolio_pnl.label_projection import project_wallet_label
    sync_canonical_wallet(
        store, address, report=report,
        projected_label=project_wallet_label(report),
    )
    return report
