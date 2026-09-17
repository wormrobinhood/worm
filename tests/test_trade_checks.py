import time
import pytest
from wormhole import config as C, trade_checks as E, trader
from test_trader import tok


@pytest.fixture
def quotes(db, monkeypatch):
    token = tok(1)
    pk = {'quote': C.USDG, 'c0': token, 'c1': C.USDG, 'hooks': C.HOOK}
    monkeypatch.setattr(trader, 'pool_key', lambda *a: pk)
    monkeypatch.setattr(E, 'token_prices', lambda tokens: {token: {'price_usd': .01}})
    monkeypatch.setattr(E, 'gas_cost', lambda *a: .05)
    def quote(rpc, pool, target, amount):
        return (1000 * 10**18 if target == token else 9_700_000), 100000, target == token
    monkeypatch.setattr(trader, 'quote_buy', quote)
    return token, pk


def test_roundtrip_quote_and_gas_are_charged(db, quotes):
    token, _ = quotes
    q = E.entry(object(), db, token, 10.)
    assert q['minimum_raw'] == 970 * 10**18
    assert q['roundtrip_ratio'] == pytest.approx((9.409 - .1) / 10)
    assert q['liquidation_usd'] == pytest.approx(9.359)


@pytest.mark.parametrize('failure', ['hook', 'eth', 'wrong_asset', 'illiquid', 'impact', 'gas', 'missing_price'])
def test_unsafe_entry_is_deferred(db, quotes, monkeypatch, failure):
    token, pk = quotes
    if failure == 'hook': pk['hooks'] = tok(99)
    if failure == 'eth': pk['quote'] = C.ZERO
    if failure == 'wrong_asset': pk['c0'] = tok(99)
    if failure == 'illiquid': monkeypatch.setattr(trader, 'quote_buy', lambda *a: (0, 100000, True))
    if failure == 'impact': monkeypatch.setattr(E, 'token_prices', lambda *a: {token: {'price_usd': .02}})
    if failure == 'gas': monkeypatch.setattr(E, 'gas_cost', lambda *a: 2.)
    if failure == 'missing_price': monkeypatch.setattr(E, 'token_prices', lambda *a: {})
    with pytest.raises(ValueError): E.entry(object(), db, token, 10.)


def test_expired_quote_is_never_used(db, quotes, monkeypatch):
    clock = iter([100, 131])
    monkeypatch.setattr(E.time, 'time', lambda: next(clock))
    with pytest.raises(ValueError, match='expired'): E.entry(object(), db, quotes[0], 10.)


def test_no_rpc_no_paper_fill(db):
    with pytest.raises(ValueError): E.entry(None, db, tok(1), 10.)


def test_complete_healthy_scores_only():
    for result in ({'score': 69, 'verdict': 'looks healthy'}, {'score': 90, 'verdict': 'mixed'},
                   {'score': 90, 'verdict': 'looks healthy', 'metrics': '{"partial":true}'},
                   {'score': 90, 'verdict': 'looks healthy', 'metrics': 'broken'}):
        assert not E.eligible(tok(1), result)
    assert E.eligible(tok(1), {'score': 70, 'verdict': 'looks healthy', 'metrics': '{}'})
