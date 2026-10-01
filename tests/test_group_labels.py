from app.portfolio_pnl.group_labels import classify_wallet_groups


def _token(address, ts, market_cap=100_000):
    return {
        'token_address': address,
        'token_symbol': 'CAT',
        'token_name': 'CAT narrative',
        'token_launch_time': ts - 60 * 60,
        'first_buy_timestamp': ts,
        'market_cap_at_first_buy_usd': market_cap,
    }


def _events(address, ts, sell_amount=500):
    return [
        {
            'event_type': 'buy', 'token_address': address, 'timestamp': ts,
            'amount_usd': 30_000, 'token_amount': 1_000, 'price_usd': 1.0,
        },
        {
            'event_type': 'buy', 'token_address': address, 'timestamp': ts + 1,
            'amount_usd': 30_000, 'token_amount': 1_000, 'price_usd': 0.9,
        },
        {
            'event_type': 'sell', 'token_address': address, 'timestamp': ts + 13 * 3600,
            'amount_usd': 300_000 if address == 'winner' else 10_000,
            'token_amount': sell_amount, 'price_usd': 2.0,
        },
    ]


def test_g1_inclusive_boundaries_and_sample_gate():
    base = 1_700_000_000
    holdings = [_token(f't{i}', base + i * 10, 30_000 if i == 0 else 150_000 if i == 1 else 100_000) for i in range(15)]
    result = classify_wallet_groups(holdings, [])
    assert result['g1']['lowcap_entry_count'] == 15
    assert result['g1']['early_lowcap_count'] == 15
    assert result['g1']['qualified'] is True

    result = classify_wallet_groups(holdings[:14], [])
    assert result['g1']['token_sample'] == 14
    assert result['g1']['qualified'] is False


def test_g2_and_g4_use_explicit_conviction_rules():
    base = 1_700_000_000
    holdings = [_token('winner' if i == 0 else f't{i}', base + i * 10) for i in range(15)]
    events = [_event for i, token in enumerate(holdings) for _event in _events(token['token_address'], base + i * 10)]
    result = classify_wallet_groups(holdings, events, capital_base_usd=200_000)
    assert result['g2']['whale'] is True
    assert result['g2']['conviction'] is True
    assert result['g2']['qualified'] is True
    assert result['g4']['max_realized_pnl_single_token_usd'] >= 100_000
    assert result['g4']['holder_score'] >= 2
    assert result['g4']['qualified'] is True
    assert set(result['qualified_labels']) == {
        'G1_EARLY_MICROCAP_SPECIALIST', 'G2_WHALE_CONVICTION',
        'G3_NARRATIVE_BASKET', 'G4_UNICON_HOLDER',
    }
    assert result['labels'] == [
        'G1_EARLY_MICROCAP_SPECIALIST', 'G2_WHALE_CONVICTION',
        'G3_NARRATIVE_BASKET', 'G4_UNICON_HOLDER',
    ]
    assert 'primary_group' not in result
    assert not any(key.startswith('is_g') for key in result)
    assert result['group_count'] == 4
    assert result['qualified_group_count'] == 4


def test_missing_data_fails_closed_without_division_by_zero():
    result = classify_wallet_groups([{'token_address': 'only'}], [], None)
    assert result['labels'] == []
    assert result['g1']['early_lowcap_rate_pct'] == 0.0
    assert result['g2']['whale'] is False
    assert result['g4']['holder_score'] == 0
