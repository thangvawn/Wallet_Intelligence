import json

from app.portfolio_pnl.api import load_cohort_label_index
from app.portfolio_pnl.canonical_data import sync_canonical_wallet
from app.portfolio_pnl.store import PnlStore, encode_payload


def _seed_wallet(store, address='wallet-api'):
    store.save_dashboard_snapshot(address, {
        'wallet_address': address,
        'wallet_name': 'API wallet',
        'wallet_emoji': '🐋',
        'fetched_at': 100,
        'collection': {'complete': True},
        'rows': [{'token_address': 'token-1'}],
    })
    store.save_activity_snapshot(address, {
        'status': 'complete', 'fetched_at': 101, 'start_at': 1, 'end_at': 101,
        'days': 30, 'events': [],
    })
    sync_canonical_wallet(
        store,
        address,
        report={
            'address': address,
            'name': 'API wallet',
            'emoji': '🐋',
            'pipeline': {
                'feature_version': 'features-test',
                'formula_version': 'labels-test',
                'input_hash': 'hash-test',
            },
            'group_labels': {
                'g1': {'qualified': True, 'label': 'G1_EARLY_MICROCAP_SPECIALIST'},
                'g2': {'qualified': False},
                'labels': ['G1_EARLY_MICROCAP_SPECIALIST'],
                'group_count': 1,
            },
        },
        projected_label={
            'address': address,
            'name': 'API wallet',
            'emoji': '🐋',
            'labels': {'rank': 'A', 'behavior': ['G1_EARLY_MICROCAP_SPECIALIST']},
            'priority': {'level': 'NORMAL'},
            'metrics': {'token_sample': 1},
        },
    )
    for component in ('holdings', 'profits', 'calendar', 'stats', 'activity', 'intraday'):
        store.mark_refresh_component(address, component, 'complete', timestamp=200)


def test_cohort_label_index_returns_typed_features_groups_label_and_refresh(tmp_path):
    store = PnlStore(tmp_path / 'cohort.sqlite3')
    _seed_wallet(store)

    result = load_cohort_label_index(store)

    assert result['schema'] == 'canonical_cohort_label_index_v1'
    assert result['total_wallets'] == 1
    assert result['ready_wallets'] == 1
    wallet = result['wallets'][0]
    assert (wallet['wallet'], wallet['wallet_name'], wallet['wallet_emoji']) == (
        'wallet-api', 'API wallet', '🐋',
    )
    assert 'money_earned_primary_usd' in wallet['features']
    assert wallet['group_labels']['g1']['qualified'] is True
    assert wallet['group_labels']['labels'] == ['G1_EARLY_MICROCAP_SPECIALIST']
    assert wallet['label']['labels']['rank'] == 'A'
    assert set(wallet['refresh']['components']) == {
        'holdings', 'profits', 'calendar', 'stats', 'activity', 'intraday',
    }
    assert wallet['refresh']['components']['activity']['status'] == 'complete'


def test_cohort_label_index_handles_missing_or_invalid_json(tmp_path):
    store = PnlStore(tmp_path / 'cohort_invalid.sqlite3')
    _seed_wallet(store, 'wallet-invalid')
    with store.connect() as db:
        db.execute(
            'UPDATE canonical_label_inputs_current '
            'SET group_labels_json=?, legacy_labels_json=?, value_sources_json=? '
            'WHERE wallet=?', ('{bad', '[]', json.dumps({'x': 'y'}), 'wallet-invalid'),
        )
        db.execute(
            'UPDATE canonical_labels_current SET payload=? WHERE wallet=?',
            (encode_payload({'labels': {'rank': 'B'}}), 'wallet-invalid'),
        )

    wallet = load_cohort_label_index(store)['wallets'][0]
    assert wallet['group_labels']['g1'] == {}
    assert wallet['legacy_labels'] == {}
    assert wallet['value_sources'] == {'x': 'y'}
    assert wallet['label']['labels']['rank'] == 'B'


def test_cohort_label_index_exposes_all_qualifying_groups_and_primary_hint(tmp_path):
    store = PnlStore(tmp_path / 'cohort_legacy_multi_group.sqlite3')
    _seed_wallet(store, 'wallet-multi')
    groups = {
        'g1': {'qualified': True, 'label': 'G1_EARLY_MICROCAP_SPECIALIST'},
        'g2': {'qualified': True, 'label': 'G2_WHALE_CONVICTION'},
        'labels': ['G1_EARLY_MICROCAP_SPECIALIST', 'G2_WHALE_CONVICTION'],
        'group_count': 2,
    }
    with store.connect() as db:
        db.execute(
            'UPDATE canonical_label_inputs_current SET group_labels_json=? WHERE wallet=?',
            (json.dumps(groups), 'wallet-multi'),
        )
        db.execute(
            'UPDATE canonical_labels_current SET payload=? WHERE wallet=?',
            (encode_payload({
                'metrics': {
                    'group_labels': groups['labels'],
                    'group_features': {'g1': groups['g1'], 'g2': groups['g2']},
                },
                'labels': {'rank': 'A', 'behavior': groups['labels']},
            }), 'wallet-multi'),
        )

    wallet = load_cohort_label_index(store)['wallets'][0]
    assert wallet['group_labels']['labels'] == [
        'G1_EARLY_MICROCAP_SPECIALIST', 'G2_WHALE_CONVICTION',
    ]
    assert wallet['group_labels']['group_count'] == 2
    assert wallet['group_labels']['qualified_group_count'] == 2
    assert wallet['label']['metrics']['group_labels'] == [
        'G1_EARLY_MICROCAP_SPECIALIST', 'G2_WHALE_CONVICTION',
    ]
    assert wallet['label']['labels']['behavior'] == [
        'G1_EARLY_MICROCAP_SPECIALIST', 'G2_WHALE_CONVICTION',
    ]
