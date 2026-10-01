from app.portfolio_pnl.canonical_data import sync_canonical_wallet
from app.portfolio_pnl.store import PnlStore, decode_payload


def test_canonical_wallet_upsert_keeps_typed_facts_and_current_reports(tmp_path):
    store = PnlStore(tmp_path / 'canonical.sqlite3')
    address = 'wallet-1'
    store.save_dashboard_snapshot(address, {
        'wallet_address': address,
        'wallet_name': 'Test',
        'fetched_at': 100,
        'collection': {'complete': True},
        'rows': [{'token_address': 'token-1', 'token_symbol': 'CAT', 'bought_usd': 10}],
    })
    store.save_activity_snapshot(address, {
        'status': 'complete', 'fetched_at': 101, 'start_at': 1, 'end_at': 101,
        'days': 1, 'events': [{
            'event_type': 'buy', 'timestamp': 100, 'token_address': 'token-1',
            'token_symbol': 'CAT', 'amount_usd': 10, 'token_amount': 5,
            'price_usd': 2,
        }],
    })
    report = {
        'address': address,
        'pipeline': {
            'feature_version': 'features-test',
            'formula_version': 'labels-test',
            'input_hash': 'hash-test',
        },
        'group_labels': {'token_observations': [{
            'token_address': 'token-1', 'symbol': 'CAT', 'name': 'Cat',
            'token_launch_time': 90, 'wallet_first_buy_time': 100,
            'market_cap_at_first_buy_usd': 100_000,
            'total_position_usd': 10, 'volume_usd': 10,
        }], 'labels': ['G1_TEST']},
    }
    result = sync_canonical_wallet(store, address, report=report, projected_label={'labels': {}})
    assert result['status'] == 'READY'
    with store.connect() as db:
        assert db.execute('select count(*) from canonical_wallet_state').fetchone()[0] == 1
        row = db.execute('select token_address,market_cap_at_first_buy_usd from canonical_token_facts').fetchone()
        assert tuple(row) == ('token-1', 100_000)
        assert db.execute('select count(*) from wallet_events').fetchone()[0] == 1
        assert db.execute('select length(payload) from wallet_events').fetchone()[0] == 0
        payload = db.execute('select payload from canonical_labels_current').fetchone()[0]
        assert decode_payload(payload)['labels'] == {}
        inputs = db.execute(
            'select wallet_name, data_status, token_sample, early_lowcap_rate_pct, '
            'portfolio_max_drawdown_pct, group_labels_json from canonical_label_inputs_current'
        ).fetchone()
        assert inputs['wallet_name'] == 'Test'
        assert inputs['data_status'] == 'READY'
        assert inputs['token_sample'] is None  # no fabricated sample when G1 did not run
        assert inputs['early_lowcap_rate_pct'] is None
        assert inputs['portfolio_max_drawdown_pct'] is None
        assert 'G1_TEST' in inputs['group_labels_json']


def test_partial_sync_retains_last_label_input_values(tmp_path):
    store = PnlStore(tmp_path / 'canonical_partial.sqlite3')
    address = 'wallet-2'
    store.save_dashboard_snapshot(address, {
        'wallet_address': address, 'wallet_name': 'Keep me', 'fetched_at': 100,
        'collection': {'complete': True}, 'rows': [],
    })
    store.save_activity_snapshot(address, {
        'status': 'complete', 'fetched_at': 101, 'start_at': 1, 'end_at': 101,
        'days': 1, 'events': [],
    })
    report = {
        'address': address,
        'pipeline': {'feature_version': 'f', 'formula_version': 'l', 'input_hash': 'h'},
        'group_labels': {'g1': {'token_sample': 15, 'early_lowcap_rate_pct': 60}, 'labels': ['G1']},
    }
    sync_canonical_wallet(store, address, report=report, projected_label={})
    sync_canonical_wallet(store, address, error='activity timeout')
    with store.connect() as db:
        row = db.execute(
            'select data_status, token_sample, early_lowcap_rate_pct, formula_version '
            'from canonical_label_inputs_current where wallet=?', (address,)
        ).fetchone()
    assert row['data_status'] == 'PARTIAL'
    assert row['token_sample'] == 15
    assert row['early_lowcap_rate_pct'] == 60
    assert row['formula_version'] == 'l'


def test_unchanged_report_skips_realtime_serving_writes(tmp_path):
    store = PnlStore(tmp_path / 'canonical_fast_path.sqlite3')
    address = 'wallet-fast'
    store.save_dashboard_snapshot(address, {
        'wallet_address': address,
        'fetched_at': 100,
        'collection': {'complete': True},
        'rows': [{'token_address': 'token-1'}],
    })
    store.save_activity_snapshot(address, {
        'status': 'complete', 'fetched_at': 101, 'start_at': 1, 'end_at': 101,
        'days': 1, 'events': [],
    })
    report = {
        'address': address,
        'analysis_version': {
            'feature_version': 'features-fast',
            'formula_version': 'labels-fast',
            'input_hash': 'same-input',
        },
        'group_labels': {'token_observations': [{
            'token_address': 'token-1', 'symbol': 'CAT',
            'market_cap_at_first_buy_usd': 123,
        }]},
    }
    first = sync_canonical_wallet(store, address, report=report, projected_label={})
    second = sync_canonical_wallet(store, address, report=report, projected_label={})
    assert first.get('skipped_unchanged') is not True
    assert second['skipped_unchanged'] is True
    with store.connect() as db:
        assert db.execute(
            'SELECT COUNT(*) FROM canonical_token_facts WHERE wallet=?', (address,)
        ).fetchone()[0] == 1
        assert db.execute(
            'SELECT fact_hash FROM canonical_token_facts WHERE wallet=?', (address,)
        ).fetchone()[0]


def test_changed_token_fact_updates_only_that_fact(tmp_path):
    store = PnlStore(tmp_path / 'canonical_fact_hash.sqlite3')
    address = 'wallet-fact'
    store.save_dashboard_snapshot(address, {
        'wallet_address': address, 'fetched_at': 100,
        'collection': {'complete': True}, 'rows': [],
    })
    store.save_activity_snapshot(address, {
        'status': 'complete', 'fetched_at': 101, 'start_at': 1, 'end_at': 101,
        'days': 1, 'events': [],
    })
    report = {
        'address': address,
        'pipeline': {'feature_version': 'f', 'formula_version': 'l', 'input_hash': 'h1'},
        'group_labels': {'token_observations': [{
            'token_address': 'token-1', 'symbol': 'OLD',
        }]},
    }
    sync_canonical_wallet(store, address, report=report, projected_label={})
    report['pipeline']['input_hash'] = 'h2'
    report['group_labels']['token_observations'][0]['symbol'] = 'NEW'
    sync_canonical_wallet(store, address, report=report, projected_label={})
    with store.connect() as db:
        row = db.execute(
            'SELECT token_symbol,fact_hash FROM canonical_token_facts WHERE wallet=?',
            (address,),
        ).fetchone()
    assert row['token_symbol'] == 'NEW'
    assert row['fact_hash']


def test_removed_token_is_deleted_from_current_serving_facts(tmp_path):
    store = PnlStore(tmp_path / 'canonical_removed.sqlite3')
    address = 'wallet-removed'
    store.save_dashboard_snapshot(address, {
        'wallet_address': address, 'fetched_at': 100,
        'collection': {'complete': True}, 'rows': [],
    })
    store.save_activity_snapshot(address, {
        'status': 'complete', 'fetched_at': 101, 'start_at': 1, 'end_at': 101,
        'days': 1, 'events': [],
    })
    report = {
        'address': address,
        'pipeline': {'feature_version': 'f', 'formula_version': 'l', 'input_hash': 'h1'},
        'group_labels': {'token_observations': [
            {'token_address': 'token-1', 'symbol': 'ONE'},
            {'token_address': 'token-2', 'symbol': 'TWO'},
        ]},
    }
    sync_canonical_wallet(store, address, report=report, projected_label={})
    report['pipeline']['input_hash'] = 'h2'
    report['group_labels']['token_observations'].pop()
    sync_canonical_wallet(store, address, report=report, projected_label={})
    with store.connect() as db:
        addresses = [row[0] for row in db.execute(
            'SELECT token_address FROM canonical_token_facts WHERE wallet=? ORDER BY token_address',
            (address,),
        ).fetchall()]
        token_count = db.execute(
            'SELECT token_count FROM canonical_wallet_state WHERE wallet=?', (address,),
        ).fetchone()[0]
    assert addresses == ['token-1']
    assert token_count == 1
