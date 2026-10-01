"""Canonical serving layer for the continuously refreshed wallet cohort.

The existing snapshot/raw tables remain the audit source.  This module projects
their latest complete facts into typed tables so dashboard reads and label
recalculation do not need to scan or duplicate large JSON arrays.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

from app.portfolio_pnl.store import PnlStore, encode_payload
from app.portfolio_pnl.label_inputs import ALL_FIELDS, build_label_input_record
from app.wallet_identity import load_message_wallets


_IDENTITY_CACHE = None


def _identity(wallet: str, dashboard: dict, report: Optional[dict]) -> tuple[str, str]:
    global _IDENTITY_CACHE
    if _IDENTITY_CACHE is None:
        _IDENTITY_CACHE = {
            item['address']: item for item in load_message_wallets()
            if item.get('address')
        }
    item = _IDENTITY_CACHE.get(wallet) or {}
    return (
        item.get('name') or dashboard.get('wallet_name') or (report or {}).get('name') or wallet,
        item.get('emoji') or dashboard.get('wallet_emoji') or (report or {}).get('emoji') or '',
    )


def _num(value: Any):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float('inf') else None


def _int(value: Any):
    try:
        return int(float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _bool_int(value: Any):
    if value is None:
        return None
    return 1 if bool(value) else 0


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _token_facts(report: Optional[dict], dashboard: dict) -> list[dict]:
    """Use group-engine observations when available, with holdings fallback."""
    observations = ((report or {}).get('group_labels') or {}).get('token_observations') or []
    if observations:
        return observations
    rows = dashboard.get('rows') or []
    return [
        {
            'token_address': row.get('token_address'),
            'symbol': row.get('token_symbol'),
            'name': row.get('token_name'),
            'token_launch_time': row.get('token_launch_time') or row.get('token_creation_timestamp'),
            'wallet_first_buy_time': row.get('wallet_first_buy_time') or row.get('first_buy_timestamp') or row.get('start_holding_timestamp'),
            'wallet_first_buy_price': row.get('wallet_first_buy_price'),
            'market_cap_at_first_buy_usd': row.get('market_cap_at_first_buy_usd') or row.get('bought_mc'),
            'first_buy_amount_usd': row.get('first_buy_amount_usd'),
            'total_position_usd': row.get('total_position_usd') or row.get('bought_usd'),
            'realized_pnl_usd': row.get('realized_profit_usd'),
        }
        for row in rows if row.get('token_address')
    ]


def _token_fact_hash(values: dict) -> str:
    """Hash only the typed serving facts for an observation.

    ``updated_at`` and the raw provider fetch timestamp are deliberately not
    included: a refresh that returns the same token facts should not rewrite
    thousands of SQLite rows merely because the poll time changed.
    """
    return PnlStore.payload_hash([
        values.get('token_symbol'), values.get('token_name'),
        values.get('token_launch_timestamp'), values.get('first_buy_timestamp'),
        values.get('first_buy_price'), values.get('market_cap_at_first_buy_usd'),
        values.get('first_buy_amount_usd'), values.get('buy_usd'),
        values.get('total_position_usd'), values.get('realized_pnl_usd'),
        values.get('entry_age_minutes'), values.get('position_action_span_hours'),
        values.get('first_sell_pct'), values.get('sell_100pct'),
        values.get('averaging_down'), values.get('volume_usd'),
        values.get('buy_order_usd_json'),
    ])


def _report_versions(report: Optional[dict]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Read version metadata from both live reports and persisted reports.

    ``analysis_version`` is persisted before the runtime ``pipeline`` block is
    attached, so cached reports commonly have only the former.  Supporting
    both keeps the realtime fast path effective for fresh and cached runs.
    """
    report = report or {}
    pipeline = report.get('pipeline') or {}
    analysis_version = report.get('analysis_version') or {}
    return (
        pipeline.get('feature_version') or analysis_version.get('feature_version'),
        pipeline.get('formula_version') or analysis_version.get('formula_version'),
        pipeline.get('input_hash') or analysis_version.get('input_hash'),
    )


def _typed_token_row(wallet: str, row: dict, updated_at: int, source_snapshot_at: Any) -> tuple:
    """Convert one observation into the compact typed serving row."""
    values = {
        'token_symbol': row.get('symbol') or row.get('token_symbol'),
        'token_name': row.get('name') or row.get('token_name'),
        'token_launch_timestamp': _int(row.get('token_launch_time') or row.get('launch_ts')),
        'first_buy_timestamp': _int(row.get('wallet_first_buy_time') or row.get('first_buy_ts')),
        'first_buy_price': _num(row.get('wallet_first_buy_price') or row.get('first_buy_price')),
        'market_cap_at_first_buy_usd': _num(row.get('market_cap_at_first_buy_usd') or row.get('market_cap')),
        'first_buy_amount_usd': _num(row.get('first_buy_amount_usd')),
        'buy_usd': _num(row.get('buy_usd') or row.get('first_buy_amount_usd')),
        'total_position_usd': _num(row.get('total_position_usd') or row.get('position_usd')),
        'realized_pnl_usd': _num(row.get('realized_pnl_usd')),
        'entry_age_minutes': _num(row.get('entry_age_minutes')),
        'position_action_span_hours': _num(row.get('position_action_span_hours')),
        'first_sell_pct': _num(row.get('first_sell_pct')),
        'sell_100pct': _bool_int(row.get('sell_100pct')),
        'averaging_down': _bool_int(row.get('averaging_down')),
        'volume_usd': _num(row.get('volume_usd')),
        'buy_order_usd_json': _json(row.get('buy_order_usd') or []),
    }
    return (
        wallet, str(row.get('token_address')), updated_at,
        values['token_symbol'], values['token_name'],
        values['token_launch_timestamp'], values['first_buy_timestamp'],
        values['first_buy_price'], values['market_cap_at_first_buy_usd'],
        values['first_buy_amount_usd'], values['buy_usd'],
        values['total_position_usd'], values['realized_pnl_usd'],
        values['entry_age_minutes'], values['position_action_span_hours'],
        values['first_sell_pct'], values['sell_100pct'], values['averaging_down'],
        values['volume_usd'], values['buy_order_usd_json'], source_snapshot_at, '',
        _token_fact_hash(values),
    )


def sync_canonical_wallet(
    store: PnlStore,
    wallet: str,
    report: Optional[dict] = None,
    projected_label: Optional[dict] = None,
    error: Optional[str] = None,
    legacy_inputs: Optional[dict] = None,
) -> dict:
    """Upsert the current canonical facts for one wallet atomically.

    ``report`` is optional so a partial component refresh can still update the
    manifest and token facts. Feature/label payloads are replaced only when a
    fresh report is supplied; a failed refresh therefore cannot erase the last
    usable label.
    """
    dashboard = store.load_dashboard_snapshot(wallet) or {}
    activity = store.load_activity_snapshot(wallet) or {}
    intraday = store.load_intraday_snapshot(wallet) or {}
    now = int(time.time())
    watermarks = store.load_component_watermarks(wallet)

    def watermark_time(component: str, fallback: Any) -> Optional[int]:
        marker = watermarks.get(component) or {}
        if marker.get('status') == 'complete' and marker.get('updated_at'):
            return _int(marker.get('updated_at'))
        return _int(fallback)

    dashboard_fetched_at = max(
        [_int(dashboard.get('fetched_at')) or 0]
        + [watermark_time(component, 0) or 0 for component in (
            'holdings', 'profits', 'calendar', 'stats',
        )]
    ) or None
    activity_fetched_at = watermark_time('activity', activity.get('fetched_at'))
    intraday_fetched_at = watermark_time('intraday', intraday.get('fetched_at'))
    rows = _token_facts(report, dashboard)
    typed_rows = [row for row in rows if row.get('token_address')]
    token_count = len(typed_rows)
    events = activity.get('events') or []
    collection = dashboard.get('collection') or {}
    activity_complete = activity.get('status') == 'complete'
    feature_version, formula_version, input_hash = _report_versions(report)
    data_status = (
        'READY' if dashboard and collection.get('complete') and activity_complete
        else 'PARTIAL' if dashboard or activity else 'MISSING'
    )
    if error:
        data_status = 'ERROR' if not dashboard else 'PARTIAL'
    wallet_name, wallet_emoji = _identity(wallet, dashboard, report)

    # A refresh can fetch the same complete snapshots repeatedly. Analysis
    # computes a content hash, so avoid reopening/updating every token and
    # serving row when that hash is already current. This is the common path
    # for short realtime polls and substantially reduces WAL churn.
    if report is not None and not error and input_hash:
        with store.connect() as db:
            current = db.execute(
                'SELECT s.wallet_name,s.wallet_emoji,s.input_hash,s.feature_version,s.formula_version,s.data_status, '
                's.token_count, '
                '(SELECT COUNT(*) FROM canonical_token_facts t WHERE t.wallet=s.wallet) AS fact_count, '
                'f.wallet AS feature_wallet,l.wallet AS label_wallet,i.wallet AS input_wallet '
                'FROM canonical_wallet_state s '
                'LEFT JOIN canonical_features_current f ON f.wallet=s.wallet '
                'LEFT JOIN canonical_labels_current l ON l.wallet=s.wallet '
                'LEFT JOIN canonical_label_inputs_current i ON i.wallet=s.wallet '
                'WHERE s.wallet=?', (wallet,),
            ).fetchone()
        if (
            current
            and current['wallet_name'] == wallet_name
            and current['wallet_emoji'] == wallet_emoji
            and current['input_hash'] == input_hash
            and current['feature_version'] == feature_version
            and current['formula_version'] == formula_version
            and current['data_status'] == data_status
            and int(current['token_count'] or 0) == token_count
            and int(current['fact_count'] or 0) == token_count
            and current['feature_wallet']
            and current['label_wallet']
            and current['input_wallet']
        ):
            # Keep the lightweight manifest's freshness clock current even
            # when the large serving projection is unchanged. This preserves
            # realtime status semantics without rewriting token facts.
            with store.connect() as db:
                db.execute(
                    'UPDATE canonical_wallet_state SET wallet_name=?,wallet_emoji=?,updated_at=?, '
                    'dashboard_fetched_at=?,activity_fetched_at=?,intraday_fetched_at=?, '
                    'holdings_complete=?,activity_complete=?,event_count=?,data_status=?,last_error=NULL '
                    'WHERE wallet=?',
                    (
                        wallet_name, wallet_emoji, now,
                        dashboard_fetched_at, activity_fetched_at,
                        intraday_fetched_at, int(bool(collection.get('complete'))),
                        int(activity_complete), len(events), data_status, wallet,
                    ),
                )
            return {
                'wallet': wallet,
                'status': data_status,
                'token_count': token_count,
                'event_count': len(events),
                'updated_at': now,
                'feature_version': feature_version,
                'formula_version': formula_version,
                'skipped_unchanged': True,
            }

    with store.connect() as db:
        previous_state = db.execute(
            'SELECT feature_version,formula_version,input_hash FROM canonical_wallet_state '
            'WHERE wallet=?', (wallet,)
        ).fetchone()
        projection_unchanged = bool(
            report is not None and previous_state and feature_version and formula_version
            and input_hash
            and previous_state['feature_version'] == feature_version
            and previous_state['formula_version'] == formula_version
            and previous_state['input_hash'] == input_hash
        )
        previous_input = db.execute(
            'SELECT * FROM canonical_label_inputs_current WHERE wallet=?', (wallet,)
        ).fetchone()
        previous_input = dict(previous_input) if previous_input else None
        db.execute(
            'INSERT INTO canonical_wallet_state('
            'wallet,wallet_name,wallet_emoji,updated_at,dashboard_fetched_at,activity_fetched_at,'
            'intraday_fetched_at,holdings_complete,activity_complete,token_count,'
            'event_count,feature_version,formula_version,input_hash,data_status,last_error'
            ') VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(wallet) DO UPDATE SET '
            'wallet_name=excluded.wallet_name,wallet_emoji=excluded.wallet_emoji,updated_at=excluded.updated_at,'
            'dashboard_fetched_at=excluded.dashboard_fetched_at,'
            'activity_fetched_at=excluded.activity_fetched_at,'
            'intraday_fetched_at=excluded.intraday_fetched_at,'
            'holdings_complete=excluded.holdings_complete,'
            'activity_complete=excluded.activity_complete,token_count=excluded.token_count,'
            'event_count=excluded.event_count,feature_version=COALESCE(excluded.feature_version,canonical_wallet_state.feature_version),'
            'formula_version=COALESCE(excluded.formula_version,canonical_wallet_state.formula_version),'
            'input_hash=COALESCE(excluded.input_hash,canonical_wallet_state.input_hash),'
            'data_status=excluded.data_status,last_error=excluded.last_error',
            (
                wallet, wallet_name, wallet_emoji, now,
                dashboard_fetched_at, activity_fetched_at,
                intraday_fetched_at, int(bool(collection.get('complete'))),
                int(activity_complete), token_count, len(events), feature_version,
                formula_version, input_hash, data_status, error,
            ),
        )
        # A refresh can contact GMGN without changing any normalized input.
        # The report hash is the content identity for the serving projection;
        # skip re-upserting thousands of token rows when that identity is
        # unchanged. Raw pages and normalized event history remain untouched.
        if rows and not projection_unchanged:
            db.executemany(
                'INSERT INTO canonical_token_facts('
                'wallet,token_address,updated_at,token_symbol,token_name,'
                'token_launch_timestamp,first_buy_timestamp,first_buy_price,'
                'market_cap_at_first_buy_usd,first_buy_amount_usd,buy_usd,total_position_usd,'
                'realized_pnl_usd,entry_age_minutes,position_action_span_hours,'
                'first_sell_pct,sell_100pct,averaging_down,volume_usd,'
                'buy_order_usd_json,source_snapshot_at,raw_fields_json,fact_hash'
                ') VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(wallet,token_address) DO UPDATE SET '
                'updated_at=excluded.updated_at,token_symbol=excluded.token_symbol,'
                'token_name=excluded.token_name,token_launch_timestamp=excluded.token_launch_timestamp,'
                'first_buy_timestamp=excluded.first_buy_timestamp,first_buy_price=excluded.first_buy_price,'
                'market_cap_at_first_buy_usd=excluded.market_cap_at_first_buy_usd,'
                'first_buy_amount_usd=excluded.first_buy_amount_usd,buy_usd=excluded.buy_usd,total_position_usd=excluded.total_position_usd,'
                'realized_pnl_usd=excluded.realized_pnl_usd,entry_age_minutes=excluded.entry_age_minutes,'
                'position_action_span_hours=excluded.position_action_span_hours,first_sell_pct=excluded.first_sell_pct,'
                'sell_100pct=excluded.sell_100pct,averaging_down=excluded.averaging_down,'
                'volume_usd=excluded.volume_usd,buy_order_usd_json=excluded.buy_order_usd_json,'
                'source_snapshot_at=excluded.source_snapshot_at,raw_fields_json=excluded.raw_fields_json,'
                'fact_hash=excluded.fact_hash '
                'WHERE canonical_token_facts.fact_hash IS NOT excluded.fact_hash',
                [
                    _typed_token_row(wallet, row, now, _int(dashboard.get('fetched_at')))
                    for row in typed_rows
                ],
            )
        # Holdings can legitimately lose a token between polls. Remove facts
        # that are no longer in the current normalized observation set so the
        # serving table cannot accumulate stale tokens and inflate sample
        # metrics. The query is chunked to stay below SQLite's bind limit.
        current_addresses = {str(row.get('token_address')) for row in typed_rows}
        existing_addresses = {
            str(item['token_address']) for item in db.execute(
                'SELECT token_address FROM canonical_token_facts WHERE wallet=?',
                (wallet,),
            ).fetchall()
        }
        stale_addresses = existing_addresses - current_addresses
        if stale_addresses:
            stale_list = list(stale_addresses)
            for offset in range(0, len(stale_list), 500):
                chunk = stale_list[offset:offset + 500]
                placeholders = ','.join('?' for _ in chunk)
                db.execute(
                    f'DELETE FROM canonical_token_facts WHERE wallet=? '
                    f'AND token_address IN ({placeholders})',
                    [wallet, *chunk],
                )
        elif not current_addresses:
            # Make the empty snapshot semantics explicit.
            db.execute(
                'DELETE FROM canonical_token_facts WHERE wallet=?', (wallet,),
            )
        label_input = build_label_input_record(
            wallet, report=report, projected_label=projected_label,
            wallet_name=wallet_name, wallet_emoji=wallet_emoji,
            data_status=data_status, feature_version=feature_version,
            formula_version=formula_version, input_hash=input_hash,
            events=events, existing=previous_input,
            legacy_inputs=legacy_inputs,
        )
        columns = [
            'wallet', 'wallet_name', 'wallet_emoji', 'updated_at', 'data_status',
            'feature_version', 'formula_version', 'input_hash', 'source',
            *ALL_FIELDS, 'group_labels_json', 'legacy_labels_json',
            'value_sources_json',
        ]
        values = [
            label_input.get('wallet'), label_input.get('wallet_name'),
            label_input.get('wallet_emoji'), now, label_input.get('data_status'),
            label_input.get('feature_version'), label_input.get('formula_version'),
            label_input.get('input_hash'), label_input.get('source'),
            *[label_input.get(field) for field in ALL_FIELDS],
            label_input.get('group_labels_json') or '{}',
            label_input.get('legacy_labels_json') or '{}',
            label_input.get('value_sources_json') or '{}',
        ]
        assignments = ','.join(f'{column}=excluded.{column}' for column in columns[1:])
        db.execute(
            f'INSERT INTO canonical_label_inputs_current({",".join(columns)}) '
            f'VALUES({",".join("?" for _ in columns)}) '
            f'ON CONFLICT(wallet) DO UPDATE SET {assignments}', values,
        )
        if report is not None:
            feature_payload = {
                key: report.get(key)
                for key in (
                    'address', 'performance_profile', 'capital_outcome',
                    'position_cluster_profile', 'risk_profile', 'consistency_profile',
                    'regime_profile', 'activity_profile', 'payoff_profile',
                    'alpha_setup', 'entry_profile', 'conviction_behavior',
                    'timing_profile', 'behavior_profile', 'wallet_class',
                    'data_quality', 'group_labels',
                )
            }
            group_payload = dict(feature_payload.get('group_labels') or {})
            # Token-level observations are served from canonical_token_facts;
            # repeating them inside every current feature payload wastes space.
            group_payload.pop('token_observations', None)
            feature_payload['group_labels'] = group_payload
            if feature_version and input_hash and not projection_unchanged:
                db.execute(
                    'INSERT INTO canonical_features_current(wallet,feature_version,input_hash,computed_at,payload) '
                    'VALUES(?,?,?,?,?) ON CONFLICT(wallet) DO UPDATE SET '
                    'feature_version=excluded.feature_version,input_hash=excluded.input_hash,'
                    'computed_at=excluded.computed_at,payload=excluded.payload',
                    (wallet, feature_version, input_hash, now, encode_payload(feature_payload)),
                )
            if projected_label is None:
                projected_label = report
            if formula_version and input_hash:
                db.execute(
                    'INSERT INTO canonical_labels_current(wallet,formula_version,input_hash,computed_at,payload) '
                    'VALUES(?,?,?,?,?) ON CONFLICT(wallet) DO UPDATE SET '
                    'formula_version=excluded.formula_version,input_hash=excluded.input_hash,'
                    'computed_at=excluded.computed_at,payload=excluded.payload',
                    (wallet, formula_version, input_hash, now, encode_payload(projected_label)),
                )
    return {
        'wallet': wallet,
        'status': data_status,
        'token_count': token_count,
        'event_count': len(events),
        'updated_at': now,
        'feature_version': feature_version,
        'formula_version': formula_version,
    }


def load_canonical_wallet(store: PnlStore, wallet: str, include_tokens: bool = True) -> Optional[dict]:
    """Read one wallet from the typed canonical serving layer."""
    with store.connect() as db:
        state = db.execute(
            'SELECT * FROM canonical_wallet_state WHERE wallet=?', (wallet,)
        ).fetchone()
        if not state:
            return None
        result = {'state': dict(state)}
        feature = db.execute(
            'SELECT * FROM canonical_features_current WHERE wallet=?', (wallet,)
        ).fetchone()
        label = db.execute(
            'SELECT * FROM canonical_labels_current WHERE wallet=?', (wallet,)
        ).fetchone()
        if feature:
            result['features_current'] = {**dict(feature), 'payload': _decode_db_payload(feature['payload'])}
        if label:
            label_payload = _decode_db_payload(label['payload'])
            qualified_labels = list((label_payload.get('metrics') or {}).get('group_labels') or [])
            metrics = dict(label_payload.get('metrics') or {})
            metrics.update({
                'group_labels': qualified_labels,
                'group_count': len(qualified_labels),
                'qualified_group_labels': qualified_labels,
                'qualified_group_count': len(qualified_labels),
            })
            label_payload['metrics'] = metrics
            projected_labels = dict(label_payload.get('labels') or {})
            behavior = projected_labels.get('behavior') or []
            group_prefixes = ('G1_', 'G2_', 'G3_', 'G4_')
            behavior = [
                value for value in behavior
                if not str(value).upper().startswith(group_prefixes)
            ]
            for group_label in qualified_labels:
                if group_label not in behavior:
                    behavior.append(group_label)
            projected_labels['behavior'] = behavior
            label_payload['labels'] = projected_labels
            result['labels_current'] = {**dict(label), 'payload': label_payload}
        label_input = db.execute(
            'SELECT * FROM canonical_label_inputs_current WHERE wallet=?', (wallet,)
        ).fetchone()
        if label_input:
            result['label_inputs_current'] = dict(label_input)
        if include_tokens:
            result['token_facts'] = [_token_aliases(dict(row)) for row in db.execute(
                'SELECT * FROM canonical_token_facts WHERE wallet=? ORDER BY token_address',
                (wallet,),
            )]
    return result


def load_canonical_summary(store: PnlStore, wallet: str) -> Optional[dict]:
    """Read only the small manifest after a refresh.

    Refresh workers use this instead of loading the feature payload and every
    token fact merely to report a successful update.  The full endpoint keeps
    using :func:`load_canonical_wallet` when callers explicitly request the
    detailed data.
    """
    with store.connect() as db:
        row = db.execute(
            'SELECT wallet,data_status,updated_at,token_count,event_count,'
            'feature_version,formula_version,input_hash,last_error '
            'FROM canonical_wallet_state WHERE wallet=?', (wallet,),
        ).fetchone()
    return dict(row) if row else None


def list_canonical_wallet_state(store: PnlStore) -> list[dict]:
    with store.connect() as db:
        return [dict(row) for row in db.execute(
            'SELECT * FROM canonical_wallet_state ORDER BY wallet'
        )]


def _decode_db_payload(value):
    from app.portfolio_pnl.store import decode_payload
    return decode_payload(value)


def _token_aliases(item: dict) -> dict:
    """Expose rule-file names alongside compact typed storage names."""
    return {
        **item,
        'token_launch_time': item.get('token_launch_timestamp'),
        'wallet_first_buy_time': item.get('first_buy_timestamp'),
        'wallet_first_buy_price': item.get('first_buy_price'),
        'symbol': item.get('token_symbol'),
    }
