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


def test_a_paper_fill_is_the_quote_less_one_percent_not_the_revert_bound(db, quotes):
    token, pk = quotes
    q = E.entry(object(), db, token, 10.)
    assert q['paper_fill_raw'] == 990 * 10**18 and q['minimum_raw'] == 970 * 10**18     # the live order still reverts below 97%
    bid = E.exit_quote(object(), pk, token, 10**21)
    assert bid['paper_fill_usd'] == pytest.approx(9.7 * .99) and bid['minimum_usd'] == pytest.approx(9.7 * .97)


def test_a_sell_may_give_up_more_only_within_bounds(db, quotes):
    token, pk = quotes
    wide = E.exit_quote(object(), pk, token, 10**21, tolerance=E.EXIT_TOLERANCE_RETRY)
    assert wide['minimum_raw'] == 9_700_000 * 9000 // 10000
    for bad in (0, -0.1, 0.5):
        with pytest.raises(ValueError, match='tolerance'):
            E.exit_quote(object(), pk, token, 10**21, tolerance=bad)


def test_eth_pools_are_for_the_paper_book_only(db, quotes, monkeypatch):
    token, pk = quotes
    pk.update(quote=C.ZERO, c0=C.ZERO, c1=token)
    with pytest.raises(ValueError):
        E.entry(object(), db, token, 10.)                                  # the live default: USDG only
    monkeypatch.setattr(E, 'eth_usd', lambda strict=False: 2500.0)
    monkeypatch.setattr(trader, 'quote_buy', lambda rpc, pool, target, amount: ((1000 * 10**18 if target == token else 3_880_000_000_000_000), 100000, target == token))
    q = E.entry(object(), db, token, 10., quotes=E.PAPER_QUOTES, reference=.01)
    assert q['amount_raw'] == 4 * 10**15 and q['pool']['quote'] == C.ZERO  # $10 of ETH at $2,500
    assert q['roundtrip_ratio'] == pytest.approx((3.88e15 * .97 / 1e18 * 2500 - .1) / 10)
    monkeypatch.setattr(E, 'eth_usd', lambda strict=False: None)
    with pytest.raises(ValueError, match='ETH price'):
        E.entry(object(), db, token, 10., quotes=E.PAPER_QUOTES, reference=.01)


def test_cached_paper_exit_uses_no_price_refresh_and_charges_gas(db, quotes, monkeypatch):
    token, pk = quotes
    monkeypatch.setattr(E, 'eth_usd', lambda *a, **k: pytest.fail('price refresh on fast path'))
    monkeypatch.setattr(E, 'eth_usd_cached', lambda: 2000)
    class Gas:
        def call(self, method, params):
            assert method == 'eth_gasPrice'
            return hex(10**9)
    result = E.exit_quote(Gas(), pk, token, 10**21, cached_prices=True)
    assert result['paper_fill_usd'] == pytest.approx(9.7*.99)
    assert result['gas_usd'] == pytest.approx((130_000+240_000)*1.25e9/1e18*2000)
    monkeypatch.setattr(E, 'eth_usd_cached', lambda: None)
    with pytest.raises(ValueError, match='cached'):
        E.exit_quote(Gas(), pk, token, 10**21, cached_prices=True)
