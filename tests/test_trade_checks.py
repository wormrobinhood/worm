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
    if failure == 'eth': pk['quote'] = C.ZERO                               # and no aggregator answers: nothing to fill from
    if failure == 'wrong_asset': pk['c0'] = tok(99)
    if failure == 'illiquid': monkeypatch.setattr(trader, 'quote_buy', lambda *a: (0, 100000, True))
    if failure == 'impact': monkeypatch.setattr(E, 'token_prices', lambda *a: {token: {'price_usd': .02}})
    if failure == 'gas': monkeypatch.setattr(E, 'gas_cost', lambda *a: 2.)
    if failure == 'missing_price': monkeypatch.setattr(E, 'token_prices', lambda *a: {})
    with pytest.raises(ValueError): E.entry(object(), db, token, 10.)


def test_expired_quote_is_never_used(db, quotes, monkeypatch):
    clock = iter([100])
    monkeypatch.setattr(E.time, 'time', lambda: next(clock, 131))
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


def test_an_eth_pool_without_a_route_is_quoted_directly_for_paper_only_and_marked(db, quotes, monkeypatch):
    token, pk = quotes
    pk.update(quote=C.ZERO, c0=C.ZERO, c1=token)
    with pytest.raises(ValueError):
        E.entry(object(), db, token, 10.)                                  # live: no route, no fill
    monkeypatch.setattr(E, 'eth_usd', lambda strict=False: 2500.0)
    monkeypatch.setattr(trader, 'quote_buy', lambda rpc, pool, target, amount: ((1000 * 10**18 if target == token else 3_880_000_000_000_000), 100000, target == token))
    q = E.entry(object(), db, token, 10., reference=.01, fallback=True)
    assert q['amount_raw'] == 4 * 10**15 and q['pool']['quote'] == C.ZERO  # $10 of ETH at $2,500
    assert q['provider'] == 'pool-eth' and q['live_fill'] is False and q['route'] is None
    assert q['roundtrip_ratio'] == pytest.approx((3.88e15 * .99 * .97 / 1e18 * 2500 - .1) / 10)   # the missing USDG leg charged
    bid = E.exit_quote(object(), pk, token, 10**21, fallback=True)
    assert bid['provider'] == 'pool-eth' and not bid['live_fill']
    assert bid['paper_fill_usd'] == pytest.approx(3.88e15 / 1e18 * 2500 * .99 * (1 - E.FALLBACK_HAIRCUT))
    with pytest.raises(ValueError):
        E.exit_quote(object(), pk, token, 10**21)                         # live: never
    monkeypatch.setattr(E, 'eth_usd', lambda strict=False: None)
    with pytest.raises(ValueError, match='ETH price'):
        E.entry(object(), db, token, 10., reference=.01, fallback=True)


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
     "risk = trade_risk.check(self.db, 'paper', scope='live')", "risk = {'allowed': True}"),
    ('route', 'best_quote', "    usable = [c for c in found", "abs(c[\"out\"] / expect - 1) <= MID_BAND", "abs(c[\"out\"] / expect - 1) <= 2 * MID_BAND"),
    ('route', 'ALLOWED', None, '"kyber": {"to": (KYBER_ROUTER,)', '"kyber": {"to": (KYBER_ROUTER, RELAY_PROXY)'),
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
    assert spec['exit_tolerance_retry'] == E.EXIT_TOLERANCE_RETRY and spec['route']['executable'] == ['pons', 'kyber', 'lifi']
    assert spec['semantics'] == 'paper-evidence-4' and spec['model'] == 'routed-quote-v1'
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
                             (E, 'EXIT_TOLERANCE_RETRY', .2), (E, 'FALLBACK_HAIRCUT', .02),
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




# ---- routed fills: any pair ------------------------------------------------------------------------

STOCK = '0x' + 'c0' * 20            # a tokenized stock a token's pool trades against


@pytest.fixture
def routed(db, monkeypatch):
    """A token at $0.01 whose pool trades against `pair[0]`; every provider routes USDG to it and back at 2% a side
    times its own rate. Gas costs grow with the route's gas units."""
    from wormhole import route
    token, pair = tok(2), [C.ZERO]
    monkeypatch.setattr(trader, 'pool_key', lambda *a: {'quote': pair[0], 'c0': pair[0], 'c1': token, 'hooks': C.HOOK})
    monkeypatch.setattr(E, 'gas_cost', lambda rpc, units: units / 1e7)
    rates = {'kyber': 1.0, 'lifi': .998, 'relay': 1.01}          # relay offers most, but is quote-only
    fees = {'kyber': 0.0, 'lifi': .025, 'relay': .015}
    def provider(p):
        def ask(tin, tout, amount):
            if rates[p] is None:
                raise route.ProviderError('down')
            out = amount * 10**14 * .98 * rates[p] if tin == C.USDG else amount / 1e18 * .01 * 1e6 * .98 * rates[p]
            return route.candidate(p, tin, tout, amount, int(out), 400_000, fees[p], route.ALLOWED[p]['to'][0],
                                   route.ALLOWED[p]['spender'][0])
        return ask
    monkeypatch.setattr(route, 'ASK', {p: provider(p) for p in rates})
    return token, pair, rates, fees


@pytest.mark.parametrize('quote', [C.ZERO, STOCK])
def test_a_token_paired_with_anything_is_bought_by_the_best_route_at_its_full_round_trip(db, routed, quote):
    token, pair, rates, _ = routed
    pair[0] = quote
    q = E.entry(object(), db, token, 10., reference=.01)
    out = int(10**7 * 10**14 * .98)
    assert q['provider'] == 'kyber' and q['live_fill'] and q['out_raw'] == out and q['amount_raw'] == 10**7
    assert q['route']['to'] == q['route']['spender'] and {c['provider'] for c in q['route']['compared']} == {'kyber', 'lifi', 'relay'}
    back = int(out * 97 // 100 / 1e18 * .01 * 1e6 * .98)
    assert q['roundtrip_ratio'] == pytest.approx((back * .97 / 1e6 - .04 - .04) / 10)
    assert q['liquidation_usd'] == pytest.approx(back * 97 // 100 / 1e6 - .04)
    assert q['paper_fill_raw'] == int(out * .99) and q['minimum_raw'] == out * 97 // 100


def test_an_aggregator_fee_over_the_cap_or_a_price_far_from_the_mid_is_not_used(db, routed):
    token, pair, rates, fees = routed
    rates['kyber'] = None
    fees['lifi'] = .2                                    # 2% of a $10 order: over the 1% cap
    with pytest.raises(ValueError):
        E.entry(object(), db, token, 10., reference=.01)
    fees['lifi'] = .025
    assert E.entry(object(), db, token, 10., reference=.01)['provider'] == 'lifi'
    with pytest.raises(ValueError):                      # the pool's mid says 1,000x more tokens: no route is believed
        E.entry(object(), db, token, 10., reference=.00001)


def test_with_every_aggregator_down_a_usdg_pool_is_its_own_route_and_a_stock_pool_is_not_traded(db, routed, monkeypatch):
    token, pair, rates, _ = routed
    for p in rates:
        rates[p] = None
    pair[0] = C.USDG
    monkeypatch.setattr(trader, 'quote_buy', lambda rpc, pool, target, amount: (
        (int(amount * 10**14 * .98), 150_000, True) if target == token else (int(amount / 1e18 * .01 * 1e6 * .98), 150_000, False)))
    q = E.entry(object(), db, token, 10., reference=.01)
    assert q['provider'] == 'pons' and q['live_fill'] and q['direction'] is True
    pair[0] = STOCK
    with pytest.raises(ValueError):
        E.entry(object(), db, token, 10., reference=.01, fallback=True)


def test_a_routed_exit_is_booked_in_usdg_and_must_sit_near_the_mid(db, routed):
    token, pair, rates, _ = routed
    pk = trader.pool_key()
    bid = E.exit_quote(object(), pk, token, 1000 * 10**18)
    assert bid['provider'] == 'kyber' and bid['live_fill'] and bid['out_raw'] == int(10 * .98 * 1e6)
    assert bid['paper_fill_usd'] == pytest.approx(10 * .98 * .99) and bid['minimum_usd'] == pytest.approx(9.8 * .97, rel=1e-6)
    assert E.exit_quote(object(), pk, token, 1000 * 10**18, reference=.01)['provider'] == 'kyber'
    with pytest.raises(ValueError):
        E.exit_quote(object(), pk, token, 1000 * 10**18, reference=.05)     # every route 80% under the mid


def test_the_relay_quote_is_asked_on_entries_but_never_on_exits(db, routed, monkeypatch):
    from wormhole import route
    token, pair, rates, _ = routed
    asked = []
    original = dict(route.ASK)
    monkeypatch.setattr(route, 'ASK', {p: (lambda f, p: lambda *a: asked.append(p) or f(*a))(f, p) for p, f in original.items()})
    E.exit_quote(object(), trader.pool_key(), token, 10**21)
    assert sorted(asked) == ['kyber', 'lifi']


def test_live_fill_is_read_from_the_row_and_old_rows_count_only_in_usdg_pools():
    import json as j
    assert E.live_fill({'live_fill': 1, 'pool_key': j.dumps({'quote': C.ZERO})})
    assert not E.live_fill({'live_fill': 0, 'pool_key': j.dumps({'quote': C.USDG})})
    assert E.live_fill({'pool_key': j.dumps({'quote': C.USDG})}) and not E.live_fill({'pool_key': j.dumps({'quote': C.ZERO})})
    assert not E.live_fill({'pool_key': 'broken'})
