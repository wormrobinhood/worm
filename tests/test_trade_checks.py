import ast
import json
import pathlib
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


# ---- the evidence fingerprint -------------------------------------------------------------------

def source(module):
    return (pathlib.Path(E.__file__).parent / f'{module}.py').read_text()


@pytest.mark.parametrize('module,name,comment_at,logic_from,logic_to', [
    ('paper', 'Paper.enter', "            arm = lab.pick_arm(self.db)\n",
     "risk = trade_risk.check(self.db, 'paper', scope='usdg' if usdg else None)", "risk = {'allowed': True}"),
    ('trade_risk', 'check', "    losses, unknown = 0.0, False\n", "losses < limit and now >= until", "losses <= limit and now >= until"),
    ('paper_research', 'LIMITS', None, "('fleet_pct', '<=', 20)", "('fleet_pct', '<=', 25)"),
    ('trade_checks', 'entry', "    minimum = out * 9700 // 10000\n", "minimum = out * 9700 // 10000", "minimum = out * 9600 // 10000"),
])
def test_a_comment_leaves_the_evidence_digest_alone_and_a_logic_edit_moves_it(module, name, comment_at, logic_from, logic_to):
    src = source(module)
    assert name in E.EVIDENCE_CODE[module]
    base = E.source_digest(ast.parse(src), E.EVIDENCE_CODE[module])
    if comment_at:
        assert comment_at in src
        commented = src.replace(comment_at, comment_at + comment_at[:len(comment_at) - len(comment_at.lstrip())] + '# only a comment\n', 1)
        assert E.source_digest(ast.parse(commented), E.EVIDENCE_CODE[module]) == base
    assert logic_from in src
    assert E.source_digest(ast.parse(src.replace(logic_from, logic_to, 1)), E.EVIDENCE_CODE[module]) != base


def test_a_docstring_edit_leaves_the_digest_alone():
    src = source('trade_checks')
    old = '"""The shared paper/live exit reference. Gas is accounted for separately in cashflows."""'
    assert old in src
    assert E.source_digest(ast.parse(src.replace(old, '"""Reworded."""')), ('entry_basis',)) == \
        E.source_digest(ast.parse(src), ('entry_basis',))


def test_the_evidence_spec_carries_the_code_digest_and_ignores_runtime_patches(monkeypatch):
    from wormhole import paper, trade_risk
    spec = E.evidence_spec()
    assert spec['code'] == E.code_digest(E.EVIDENCE_CODE) and len(spec['code']) == 32
    assert spec['exit_tolerance_retry'] == E.EXIT_TOLERANCE_RETRY and spec['paper_quotes'] == list(E.PAPER_QUOTES)
    monkeypatch.setattr(E, 'entry', lambda *a, **k: {})                 # a test double is not an edit
    monkeypatch.setattr(trade_risk, 'check', lambda *a, **k: {'allowed': True})
    monkeypatch.setattr(paper.Paper, 'enter', lambda *a, **k: True)
    assert E.evidence_spec() == spec
    with pytest.raises(ValueError, match='out of date'):
        E.code_digest({'paper': ('Paper.no_such_method',)})


def test_the_evidence_spec_moves_with_every_parameter(monkeypatch):
    from wormhole import lab, strategy_validation as V, watch
    rule = watch.STRATEGIES[0]
    frozen = V.frozen(rule)
    assert V.frozen(rule) == frozen and json.loads(frozen)['execution'] == E.evidence_spec()
    for obj, name, value in ((E, 'PAPER_FILL', .02), (E, 'APPROVAL_GAS_UNITS', 250_000), (E, 'SEMANTICS', 'x'),
                             (E, 'EXIT_TOLERANCE_RETRY', .2), (E, 'PAPER_QUOTES', (C.USDG,)),
                             (watch, 'FLOW_WINDOW_S', 600), (watch, 'FAST_EVERY_S', 30), (C, 'PAPER_SIZE_USD', 20.0)):
        with monkeypatch.context() as m:
            m.setattr(obj, name, value)
            assert V.frozen(rule) != frozen, name
    with monkeypatch.context() as m:
        m.setenv('WH_MAX_DAILY_LOSS_USD', '20')
        assert V.frozen(rule) != frozen
    with monkeypatch.context() as m:
        m.setitem(lab.POLICIES, lab.DEFAULT.split('@')[0], {**lab.parse_arm(lab.DEFAULT)[0], 'stop': -.25})
        assert V.frozen(rule) != frozen
    changed = {**rule, 'conditions': [dict(c) for c in rule['conditions']]}
    changed['conditions'][0]['value'] += 1
    assert V.frozen(changed) != frozen


