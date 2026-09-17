"""No signing/network: exercise the production order journal with deterministic receipts."""
import json
import time
from types import SimpleNamespace

import pytest
from wormhole import config as C, lab, live_trading as L, trader, trade_risk
from test_trader import FakeRpc, receipt, pad, tok, HASH, RUNWAY, candidate
from wormhole.pons import TRANSFER


@pytest.fixture
def setup(db, monkeypatch):
    trader.ensure_tables(db)
    token = candidate(db, 1, quote=C.USDG)
    pool = db.one('SELECT * FROM pools WHERE token=?', (token,))
    quote = {'pool': pool, 'amount_raw': 10_000_000, 'minimum_raw': 970 * 10**18,
             'out_raw': 1000 * 10**18, 'direction': pool['c0'] == C.USDG,
             'gas_usd': .1, 'expires_at': time.time() + 30, 'price': .01}
    monkeypatch.setattr(C, 'TRADING', True)
    monkeypatch.setattr(trader, 'LIVE_SELL_READY', True)
    monkeypatch.setattr(L.strategy_validation, 'summary', lambda db: {'passed': True, 'arm': lab.DEFAULT})
    monkeypatch.setattr(L.execution, 'entry', lambda *args: dict(quote))
    monkeypatch.setattr(L, 'approve_exact', lambda *args: int(time.time()) + 600)
    monkeypatch.setattr(L, 'call_fn', lambda *args: [10**30])
    monkeypatch.setattr(trader, 'token_prices', lambda tokens: {})
    calls = []
    def sender(*args, **kwargs):
        row = db.one("SELECT * FROM trades WHERE note='PENDING'")
        assert row and row['tx'] is None  # durable intent before signing
        kwargs['on_broadcast'](HASH)
        assert db.one('SELECT tx FROM trades WHERE id=?', (row['id'],))['tx'] == HASH
        calls.append(kwargs)
        return HASH, None
    monkeypatch.setattr(L, 'send_tx', sender)
    return SimpleNamespace(db=db, token=token, pool=pool, quote=quote, calls=calls,
                           rpc=FakeRpc(), acct=SimpleNamespace(address=C.WALLET))


def buy(s):
    trader.decide(s.rpc, s.db, RUNWAY, True, s.acct, ready={'ready': True})


def transfer(currency, src, dst, amount):
    return {'address': currency, 'topics': [TRANSFER.topic, pad(src), pad(dst)], 'data': hex(amount)}


def settle_buy(s):
    s.rpc.receipts[HASH] = receipt(s.token, C.WALLET, [970 * 10**18])
    s.rpc.receipts[HASH]['logs'].append(transfer(C.USDG, C.WALLET, C.HOOK, 10_000_000))
    trader.reconcile(s.rpc, s.db)


def test_pending_is_durable_blocks_repeat_and_recovers_exact_receipt(setup):
    s = setup
    buy(s)
    assert trader.open_count(s.db) == 1 and trader.spent_today(s.db) == 10
    buy(s)
    assert len(s.calls) == 1
    trader.reconcile(s.rpc, s.db)
    assert s.db.one('SELECT note FROM trades')['note'] == 'PENDING'
    settle_buy(s)
    p = s.db.one('SELECT * FROM positions')
    assert p['qty'] == 970 and p['qty_raw'] == str(970 * 10**18)
    assert p['realized_usd'] == -.1 and p['quote'] == 'USDG'
    trader.reconcile(s.rpc, s.db)
    assert s.db.one('SELECT COUNT(*) n FROM positions')['n'] == 1


def test_ambiguous_broadcast_stays_pending_with_same_hash(setup, monkeypatch):
    s = setup
    def fail(*args, **kw):
        kw['on_broadcast'](HASH)
        raise TimeoutError('node accepted bytes but reply was lost')
    monkeypatch.setattr(L, 'send_tx', fail)
    buy(s)
    row = s.db.one('SELECT * FROM trades')
    assert row['note'] == 'PENDING' and row['tx'] == HASH
    buy(s)
    assert s.db.one('SELECT COUNT(*) n FROM trades')['n'] == 1
    settle_buy(s)
    assert s.db.one('SELECT note FROM trades')['note'] == 'SUCCESS'


def test_submission_without_hash_is_held_for_review(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(L, 'send_tx', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('storage error')))
    buy(s)
    assert s.db.one('SELECT note FROM trades')['note'] == 'REVIEW'
    assert trader.open_count(s.db) == 1 and trader.spent_today(s.db) == 10
    buy(s)
    assert s.db.one('SELECT COUNT(*) n FROM trades')['n'] == 1


@pytest.mark.parametrize('wrong', ['missing', 'wrong_spend', 'too_few_tokens'])
def test_never_falls_back_to_quote_when_receipt_evidence_invalid(setup, wrong):
    s = setup
    buy(s)
    n = 969 if wrong == 'too_few_tokens' else 970
    logs = [transfer(s.token, C.HOOK, C.WALLET, n * 10**18)]
    if wrong != 'missing':
        logs.append(transfer(C.USDG, C.WALLET, C.HOOK, 9_000_000 if wrong == 'wrong_spend' else 10_000_000))
    s.rpc.receipts[HASH] = {'status': '0x1', 'logs': logs}
    trader.reconcile(s.rpc, s.db)
    assert s.db.one('SELECT note FROM trades')['note'] == 'REVIEW'
    assert not s.db.q('SELECT * FROM positions')


def test_reverted_buy_counts_gas_and_does_not_open(setup):
    s = setup
    buy(s)
    s.rpc.receipts[HASH] = {'status': '0x0', 'logs': []}
    trader.reconcile(s.rpc, s.db)
    assert s.db.one('SELECT note FROM trades')['note'] == 'REVERTED'
    assert not s.db.q('SELECT * FROM positions')
    assert trade_risk.check(s.db)['loss_usd'] == .1


def test_live_requires_explicit_readiness_and_prospective_pass(setup, monkeypatch):
    s = setup
    trader.decide(s.rpc, s.db, RUNWAY, True, s.acct)
    assert not s.calls
    monkeypatch.setattr(L.strategy_validation, 'summary', lambda db: {'passed': False})
    buy(s)
    assert not s.calls


def test_exit_works_with_entry_policy_off_and_loss_pause(setup, monkeypatch):
    s = setup
    buy(s)
    settle_buy(s)
    monkeypatch.setattr(C, 'TRADING', False)
    s.db.meta_set('loss_pause_until_live', int(time.time()) + 86400)
    monkeypatch.setattr(trader, 'token_prices', lambda tokens: {s.token: {'price_usd': .02}})
    def quote(rpc, pk, token, amount):
        return {**s.quote, 'amount_raw': amount, 'minimum_raw': 12_000_000, 'direction': pk['c0'] == token}
    monkeypatch.setattr(L.execution, 'exit_quote', quote)
    trader.mark(s.rpc, s.db, True, s.acct)
    order = s.db.one("SELECT * FROM trades WHERE side='sell'")
    p = s.db.one('SELECT * FROM positions')
    assert order['note'] == 'PENDING' and p['tp_done'] == '[]' and p['qty'] == p['qty_left']
    snap = json.loads(order['execution'])
    amount = int(snap['amount_raw'])
    s.rpc.receipts[HASH] = {'status': '0x1', 'logs': [transfer(s.token, C.WALLET, C.HOOK, amount),
                                                   transfer(C.USDG, C.HOOK, C.WALLET, 13_000_000)]}
    trader.reconcile(s.rpc, s.db)
    p = s.db.one('SELECT * FROM positions')
    assert p['tp_done'] == '[1.5]' and p['trail_on'] == 1
    assert int(p['qty_left_raw']) == 970 * 10**18 - amount
    assert p['realized_usd'] == pytest.approx(12.8)
    trader.reconcile(s.rpc, s.db)
    assert s.db.one('SELECT realized_usd FROM positions')['realized_usd'] == pytest.approx(12.8)


def test_reverted_exit_keeps_original_flags_and_holding(setup, monkeypatch):
    s = setup
    buy(s); settle_buy(s)
    p = s.db.one('SELECT * FROM positions')
    q = {**s.quote, 'amount_raw': 100 * 10**18, 'minimum_raw': 1_000_000}
    L.submit(s.rpc, s.db, s.acct, s.token, 'T1', 'sell', 0, q,
             {'expected_remaining': p['qty_left_raw'], 'state': {'tp_done': [1.5], 'trail_on': True, 'peak': .02}, 'reason': 'profit'}, int(time.time()) + 600)
    s.rpc.receipts[HASH] = {'status': '0x0', 'logs': []}
    trader.reconcile(s.rpc, s.db)
    after = s.db.one('SELECT * FROM positions')
    assert after['tp_done'] == '[]' and after['qty_left_raw'] == p['qty_left_raw']
    assert after['realized_usd'] == pytest.approx(-.2)


def test_net_transfer_ignores_self_and_accounts_for_outgoing():
    t = tok(1)
    rc = {'logs': [transfer(t, C.HOOK, C.WALLET, 5), transfer(t, C.WALLET, C.HOOK, 2), transfer(t, C.WALLET, C.WALLET, 100)]}
    assert L.net_transfer(rc, t, C.WALLET) == 3


def test_full_stop_sells_only_tracked_tokens_and_closes_on_receipt(setup, monkeypatch):
    s = setup
    buy(s); settle_buy(s)
    monkeypatch.setattr(trader, 'token_prices', lambda tokens: {s.token: {'price_usd': .003}})
    monkeypatch.setattr(L.execution, 'exit_quote', lambda rpc, pk, token, amount: {**s.quote, 'amount_raw': amount, 'minimum_raw': 2_000_000})
    trader.mark(s.rpc, s.db, True, s.acct)
    order = s.db.one("SELECT * FROM trades WHERE side='sell'")
    assert int(json.loads(order['execution'])['amount_raw']) == 970 * 10**18  # ignores donated balance
    s.rpc.receipts[HASH] = {'status': '0x1', 'logs': [transfer(s.token, C.WALLET, C.HOOK, 970 * 10**18),
                                                   transfer(C.USDG, C.HOOK, C.WALLET, 2_100_000)]}
    trader.reconcile(s.rpc, s.db)
    p = s.db.one('SELECT * FROM positions')
    assert p['status'] == 'closed' and p['qty_left_raw'] == '0' and p['reason'] == 'stop at -35%'
    assert p['realized_usd'] == pytest.approx(1.9)


def test_receipt_bookkeeping_failure_rolls_back_holdings(setup, monkeypatch):
    s = setup
    buy(s)
    original = s.db.x
    def fail(sql, args=()):
        if "note='SUCCESS'" in sql: raise RuntimeError('disk full')
        return original(sql, args)
    monkeypatch.setattr(s.db, 'x', fail)
    settle_buy(s)
    assert not s.db.q('SELECT * FROM positions')
    assert s.db.one('SELECT note FROM trades')['note'] == 'REVIEW'


def test_exact_approval_reduces_unlimited_and_uses_short_expiry(db, monkeypatch):
    from eth_abi import decode
    now = int(time.time())
    state = [2**256-1, 2**160-1, now + 10000]
    currency, amount = tok(5), 123456
    monkeypatch.setattr(L, 'allowance', lambda *a: tuple(state))
    calls = []
    def sender(rpc, acct, to, data):
        calls.append(to)
        if to == currency:
            spender, value = decode(['address','uint256'], bytes.fromhex(data[10:]))
            assert spender == C.PERMIT2 and value == amount
            state[0] = value
        else:
            token, spender, value, expiry = decode(['address','address','uint160','uint48'], bytes.fromhex(data[10:]))
            assert token == currency and spender == C.UNIVERSAL_ROUTER and value == amount
            assert now + 590 <= expiry <= now + 610
            state[1:] = [value, expiry]
        return HASH, {'status': '0x1'}
    monkeypatch.setattr(L, 'send_tx', sender)
    L.approve_exact(object(), SimpleNamespace(address=C.WALLET), currency, amount)
    assert calls == [currency, C.PERMIT2]
    L.approve_exact(object(), SimpleNamespace(address=C.WALLET), currency, amount)
    assert len(calls) == 2  # exact recent allowances may be reused


def test_successful_receipt_is_not_enough_for_approval_readback(monkeypatch):
    monkeypatch.setattr(L, 'allowance', lambda *a: (0, 0, 0))
    monkeypatch.setattr(L, 'send_tx', lambda *a: (HASH, {'status': '0x1'}))
    with pytest.raises(RuntimeError, match='readback'):
        L.approve_exact(object(), SimpleNamespace(address=C.WALLET), tok(5), 100)
