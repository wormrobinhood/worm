import ast
import hashlib
import inspect
import json
import pathlib
import textwrap
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

def behaviour_digest(sources):
    """sha256 over the syntax trees of `sources` with docstrings removed: blind to comments, blank lines and
    docstrings, changed by any edit to what the code does (a renamed variable included, deliberately)."""
    h = hashlib.sha256()
    for src in sources:
        tree = ast.parse(textwrap.dedent(src))
        for node in ast.walk(tree):
            body = getattr(node, 'body', None)
            if (isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
        h.update(ast.dump(tree).encode())
    return h.hexdigest()[:32]           # 128 bits: plenty to notice an edit, and not mistaken for a key


def semantic_code():
    """The code that turns quotes, mids and swap logs into paper fills, features and exits."""
    from wormhole import lab, paper, poolstate, watch
    fns = (E.entry, E.exit_quote, E.quote_unit, E._gas_cost_at_price, E.entry_basis, E.verified,
           lab.exit_step, lab.trail_pct, paper.Paper._open, paper.Paper._mark_position, paper.Paper._quote,
           watch.features, watch.passes, watch.flow, watch.holders_kept, watch.sample_bucket, watch._sample,
           poolstate.token_price, poolstate.sqrt_price, poolstate.mids)
    return [inspect.getsource(f) for f in fns]


# Re-pin ONLY after deciding the change leaves every fill, feature and exit exactly as it was. Otherwise bump
# trade_checks.SEMANTICS (which voids every cohort in progress) and then re-pin. ast.dump output is stable within
# one Python minor version: 3.11, as in the Dockerfile and CI; moving Python means re-pinning once.
PINNED = {'semantics': 'paper-evidence-3', 'digest': 'a34f31a1afa727bfd98265e0d1bf53ea'}


def test_the_code_that_makes_evidence_is_pinned_to_its_semantics_version():
    assert E.SEMANTICS == PINNED['semantics'], 'SEMANTICS was bumped: re-pin the digest below to the new code'
    assert behaviour_digest(semantic_code()) == PINNED['digest'], (
        'evidence code changed: bump trade_checks.SEMANTICS (voids cohorts) unless behaviour is truly unchanged, then re-pin')


def test_the_digest_ignores_comments_and_docstrings_but_not_parameters():
    base = 'def f(x):\n    """Doc."""\n    return x * 0.97\n'
    assert behaviour_digest([base]) == behaviour_digest(['def f(x):\n    """Other words."""\n    # why\n\n    return x * 0.97  # note\n'])
    assert behaviour_digest([base]) != behaviour_digest(['def f(x):\n    """Doc."""\n    return x * 0.96\n'])


def test_the_evidence_spec_reads_no_source_files_and_moves_with_every_parameter(monkeypatch):
    from wormhole import lab, strategy_validation as V, watch
    rule = watch.STRATEGIES[0]
    spec = E.evidence_spec()
    assert 'implementation' not in spec and spec['semantics'] == E.SEMANTICS
    frozen = V.frozen(rule)
    # a comment-only edit changes a file's bytes, never a value: reading any source file would fail here
    monkeypatch.setattr(pathlib.Path, 'read_bytes', lambda self: pytest.fail('evidence must not hash source files'))
    assert V.frozen(rule) == frozen and json.loads(frozen)['execution'] == spec
    for obj, name, value in ((E, 'PAPER_FILL', .02), (E, 'APPROVAL_GAS_UNITS', 250_000), (E, 'SEMANTICS', 'x'),
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
