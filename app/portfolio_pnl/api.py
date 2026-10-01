"""API helpers backed by the canonical SQLite PnL store."""
import json
import time
import zlib
from typing import Any, Dict, List, Optional

from app.portfolio_pnl.label_inputs import ALL_FIELDS
from app.portfolio_pnl.store import PnlStore, decode_payload


def load_master_index() -> Dict[str, Any]:
    snapshots = PnlStore().list_dashboard_snapshots()
    wallets = []
    for payload in snapshots:
        rows = payload.get('rows') or []
        wallets.append({
            'address': payload.get('wallet_address') or '',
            'name': payload.get('wallet_name') or '',
            'emoji': payload.get('wallet_emoji') or '',
            'has_pnl_data': bool(rows),
            'token_rows': len(rows),
            'summary': payload.get('summary') or {},
        })
    return {
        'schema': 'gmgn_recent_pnl_index_v1',
        'generated_at': int(time.time()),
        'total_wallets': len(wallets),
        'with_data': sum(1 for wallet in wallets if wallet['has_pnl_data']),
        'wallets': wallets,
    }


def load_wallet_pnl(wallet_address: str) -> Optional[Dict[str, Any]]:
    return PnlStore().load_dashboard_snapshot(wallet_address.strip())


def wallet_stats(wallet_address: str):
    from app.portfolio_pnl.analytics import get_wallet_stats
    return get_wallet_stats(wallet_address)


def cohort_stats(include_wallets: bool = False):
    from app.portfolio_pnl.analytics import compute_cohort_stats
    return compute_cohort_stats(include_wallets=include_wallets)


def _decode_object(value: Any) -> dict:
    """Decode a canonical JSON column without allowing one bad row to break the index."""
    if value is None:
        return {}
    try:
        decoded = value if isinstance(value, dict) else decode_payload(value)
    except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError, zlib.error):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _refresh_component(row: dict, component: str) -> dict:
    """Build the compact status object for one independently refreshed component."""
    status = row.get(f'{component}_status')
    result = {
        'status': status or 'missing',
        'last_attempt_at': row.get(f'{component}_last_attempt_at'),
        'last_success_at': row.get(f'{component}_last_success_at'),
        'error': row.get(f'{component}_error'),
    }
    # Watermarks are useful to the UI when it explains why activity is still
    # being backfilled. They are kept beside the component rather than exposed
    # as a second parallel array so a wallet can be rendered in one pass.
    if row.get(f'{component}_wm_status') is not None:
        result['watermark'] = {
            'status': row.get(f'{component}_wm_status'),
            'provider_cursor': row.get(f'{component}_provider_cursor'),
            'oldest_timestamp': row.get(f'{component}_oldest_timestamp'),
            'newest_timestamp': row.get(f'{component}_newest_timestamp'),
            'coverage_days': row.get(f'{component}_coverage_days'),
            'history_exhausted': bool(row.get(f'{component}_history_exhausted')),
            'payload_hash': row.get(f'{component}_payload_hash'),
            'updated_at': row.get(f'{component}_wm_updated_at'),
        }
    return result


def load_cohort_label_index(store: Optional[PnlStore] = None) -> Dict[str, Any]:
    """Load the complete cohort label table in one SQLite read.

    This is intentionally a serving/index query rather than a call to
    ``load_canonical_wallet`` for each address.  The dashboard polls this
    endpoint frequently, so one row per wallet contains the typed label inputs,
    parsed group/projected labels and the compact component refresh state while
    token facts remain on the per-wallet canonical endpoint.
    """
    store = store or PnlStore()
    component_names = ('holdings', 'profits', 'calendar', 'stats', 'activity', 'intraday')
    # Keep the projection explicit.  ``SELECT *`` would make joins vulnerable
    # to duplicate column names and would silently change the public response
    # when a migration adds an internal column.
    input_columns = ','.join(
        f'li.{field} AS feature_{field}' for field in ALL_FIELDS
    )
    component_aliases = {
        'holdings': 'h', 'profits': 'p', 'calendar': 'cal',
        'stats': 'st', 'activity': 'a', 'intraday': 'intr',
    }
    component_columns = ','.join(
        f'{alias}.status AS {component}_status,'
        f'{alias}.last_attempt_at AS {component}_last_attempt_at,'
        f'{alias}.last_success_at AS {component}_last_success_at,'
        f'{alias}.error AS {component}_error'
        for component, alias in component_aliases.items()
    )
    watermark_columns = ','.join(
        f'{alias}.status AS {component}_wm_status,'
        f'{alias}.provider_cursor AS {component}_provider_cursor,'
        f'{alias}.oldest_timestamp AS {component}_oldest_timestamp,'
        f'{alias}.newest_timestamp AS {component}_newest_timestamp,'
        f'{alias}.coverage_days AS {component}_coverage_days,'
        f'{alias}.history_exhausted AS {component}_history_exhausted,'
        f'{alias}.payload_hash AS {component}_payload_hash,'
        f'{alias}.updated_at AS {component}_wm_updated_at'
        for component, alias in {
            'holdings': 'wh', 'profits': 'wp', 'calendar': 'wcal',
            'stats': 'wst', 'activity': 'wa', 'intraday': 'wi',
        }.items()
    )
    query = f'''
        SELECT
            s.wallet AS wallet,
            COALESCE(NULLIF(li.wallet_name, ''), NULLIF(s.wallet_name, ''), s.wallet) AS wallet_name,
            COALESCE(NULLIF(li.wallet_emoji, ''), s.wallet_emoji, '') AS wallet_emoji,
            s.data_status AS data_status,
            s.updated_at AS updated_at,
            s.dashboard_fetched_at AS dashboard_fetched_at,
            s.activity_fetched_at AS activity_fetched_at,
            s.intraday_fetched_at AS intraday_fetched_at,
            s.holdings_complete AS holdings_complete,
            s.activity_complete AS activity_complete,
            s.token_count AS token_count,
            s.event_count AS event_count,
            s.feature_version AS state_feature_version,
            s.formula_version AS state_formula_version,
            s.input_hash AS state_input_hash,
            s.last_error AS state_last_error,
            rs.status AS refresh_status,
            rs.last_attempt_at AS refresh_last_attempt_at,
            rs.last_success_at AS refresh_last_success_at,
            rs.consecutive_failures AS refresh_consecutive_failures,
            rs.next_retry_at AS refresh_next_retry_at,
            rs.error AS refresh_error,
            li.updated_at AS feature_updated_at,
            li.data_status AS feature_data_status,
            li.feature_version AS feature_version,
            li.formula_version AS formula_version,
            li.input_hash AS feature_input_hash,
            li.source AS feature_source,
            li.group_labels_json AS group_labels_json,
            li.legacy_labels_json AS legacy_labels_json,
            li.value_sources_json AS value_sources_json,
            lc.payload AS projected_label_payload,
            {input_columns},
            {component_columns},
            {watermark_columns}
        FROM canonical_wallet_state s
        LEFT JOIN canonical_label_inputs_current li ON li.wallet = s.wallet
        LEFT JOIN canonical_labels_current lc ON lc.wallet = s.wallet
        LEFT JOIN wallet_refresh_state rs ON rs.wallet = s.wallet
        LEFT JOIN wallet_refresh_components h ON h.wallet = s.wallet AND h.component = 'holdings'
        LEFT JOIN wallet_refresh_components p ON p.wallet = s.wallet AND p.component = 'profits'
        LEFT JOIN wallet_refresh_components cal ON cal.wallet = s.wallet AND cal.component = 'calendar'
        LEFT JOIN wallet_refresh_components st ON st.wallet = s.wallet AND st.component = 'stats'
        LEFT JOIN wallet_refresh_components a ON a.wallet = s.wallet AND a.component = 'activity'
        LEFT JOIN wallet_refresh_components intr ON intr.wallet = s.wallet AND intr.component = 'intraday'
        LEFT JOIN wallet_component_watermarks wh ON wh.wallet = s.wallet AND wh.component = 'holdings'
        LEFT JOIN wallet_component_watermarks wp ON wp.wallet = s.wallet AND wp.component = 'profits'
        LEFT JOIN wallet_component_watermarks wcal ON wcal.wallet = s.wallet AND wcal.component = 'calendar'
        LEFT JOIN wallet_component_watermarks wst ON wst.wallet = s.wallet AND wst.component = 'stats'
        LEFT JOIN wallet_component_watermarks wa ON wa.wallet = s.wallet AND wa.component = 'activity'
        LEFT JOIN wallet_component_watermarks wi ON wi.wallet = s.wallet AND wi.component = 'intraday'
        ORDER BY s.wallet
    '''

    with store.connect() as db:
        rows = [dict(row) for row in db.execute(query).fetchall()]

    wallets = []
    for row in rows:
        features = {
            field: row.get(f'feature_{field}')
            for field in ALL_FIELDS
        }
        groups = _decode_object(row.get('group_labels_json'))
        qualified_labels = list(groups.get('labels') or []) if isinstance(groups, dict) else []
        # Keep a stable group object even for MISSING/PARTIAL wallets.  This
        # lets the frontend render the same hover card without null checks.
        group_labels = {
            'g1': groups.get('g1') or {},
            'g2': groups.get('g2') or {},
            'g3': groups.get('g3') or {},
            'g4': groups.get('g4') or {},
            'labels': qualified_labels,
            'group_count': len(qualified_labels),
            'qualified_labels': qualified_labels,
            'qualified_group_count': len(qualified_labels),
        }
        label_payload = _decode_object(row.get('projected_label_payload'))
        if label_payload:
            # Existing canonical rows may predate the multi-group projection.
            # Normalize the serving projection without waiting for a provider
            # refresh or a full cohort re-analysis.
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
            group_label_prefixes = ('G1_', 'G2_', 'G3_', 'G4_')
            behavior = [
                value for value in behavior
                if not str(value).upper().startswith(group_label_prefixes)
            ]
            for group_label in qualified_labels:
                if group_label not in behavior:
                    behavior.append(group_label)
            projected_labels['behavior'] = behavior
            label_payload['labels'] = projected_labels
        components = {
            component: _refresh_component(row, component)
            for component in component_names
        }
        wallets.append({
            'wallet': row.get('wallet') or '',
            'wallet_name': row.get('wallet_name') or row.get('wallet') or '',
            'wallet_emoji': row.get('wallet_emoji') or '',
            'data_status': row.get('data_status') or 'MISSING',
            'updated_at': row.get('updated_at'),
            'dashboard_fetched_at': row.get('dashboard_fetched_at'),
            'activity_fetched_at': row.get('activity_fetched_at'),
            'intraday_fetched_at': row.get('intraday_fetched_at'),
            'holdings_complete': bool(row.get('holdings_complete')),
            'activity_complete': bool(row.get('activity_complete')),
            'token_count': int(row.get('token_count') or 0),
            'event_count': int(row.get('event_count') or 0),
            'feature_version': row.get('feature_version') or row.get('state_feature_version'),
            'formula_version': row.get('formula_version') or row.get('state_formula_version'),
            'input_hash': row.get('feature_input_hash') or row.get('state_input_hash'),
            'feature_source': row.get('feature_source') or 'derived',
            'features': features,
            'group_labels': group_labels,
            'legacy_labels': _decode_object(row.get('legacy_labels_json')),
            'value_sources': _decode_object(row.get('value_sources_json')),
            'label': label_payload,
            'refresh': {
                'status': row.get('refresh_status') or 'not_started',
                'last_attempt_at': row.get('refresh_last_attempt_at'),
                'last_success_at': row.get('refresh_last_success_at'),
                'consecutive_failures': int(row.get('refresh_consecutive_failures') or 0),
                'next_retry_at': row.get('refresh_next_retry_at'),
                'error': row.get('refresh_error') or row.get('state_last_error'),
                'components': components,
            },
        })
    return {
        'schema': 'canonical_cohort_label_index_v1',
        'generated_at': int(time.time()),
        'total_wallets': len(wallets),
        'ready_wallets': sum(item['data_status'] == 'READY' for item in wallets),
        'wallets': wallets,
    }


def _rehydrate_from_raw_pages(wallet_address: str, wallet_name: str = '') -> Optional[List[dict]]:
    """Rebuild Recent PnL rows from stored GMGN holdings pages (logo, honeypot, MC)."""
    from app.portfolio_pnl.normalize import normalize_holdings_table
    store = PnlStore()
    pages = store.latest_complete_pages(
        wallet_address,
        source='gmgn',
        endpoint='portfolio/holdings',
    )
    raw_rows: List[dict] = []
    for page in pages:
        payload = page.get('payload') or {}
        body = payload.get('data', payload) if isinstance(payload, dict) else {}
        lst = []
        if isinstance(body, list):
            lst = body
        elif isinstance(body, dict):
            lst = body.get('list') or body.get('holdings') or []
        if isinstance(lst, list):
            raw_rows.extend(lst)
    if not raw_rows:
        return None
    return normalize_holdings_table(raw_rows, wallet_address, wallet_name)


def lookup_wallet(wallet_address: str, wallet_name: str = '') -> Dict[str, Any]:
    """Load one wallet from local snapshots and return its table + analysis."""
    from app.portfolio_pnl.analytics import build_wallet_report

    address = wallet_address.strip()
    payload = load_wallet_pnl(address)
    rows = (payload or {}).get('rows') or []
    collection_complete = bool((payload or {}).get('collection', {}).get('complete'))
    needs_percentage_migration = bool(payload) and payload.get('percentage_unit') != 'percent'
    if payload and (not rows or not collection_complete or needs_percentage_migration):
        rebuilt = _rehydrate_from_raw_pages(address, payload.get('wallet_name') or wallet_name)
        if rebuilt:
            payload = {**payload, 'rows': rebuilt, 'percentage_unit': 'percent'}
    if not payload:
        raise FileNotFoundError('Wallet chưa có snapshot. Dùng chức năng phân tích ví mới để thu thập.')
    return build_wallet_report(payload)
