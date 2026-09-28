"""No signing/network: exercise the production order journal with deterministic receipts."""
import json
import time
from types import SimpleNamespace

import pytest
from wormhole import config as C, lab, live_trading as L, trader, trade_risk
from test_trader import FakeRpc, receipt, pad, tok, HASH, RUNWAY, candidate
from wormhole.pons import TRANSFER

ARM = 'costout_1.5x@0m'          # these tests walk a take-profit, a trail and a stop: an arm that has all three
POOL_MID = L.pool_mid


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
    monkeypatch.setenv('WH_TRADING_BUDGET_USD', '100')
    from wormhole.paper import Paper
    Paper(db)                                          # live follows paper: the book has just bought this token under a passed rule
    db.x("INSERT INTO paper(token,symbol,opened_ts,entry_usd,size_usd,qty,status,strategy) VALUES(?,?,?,?,?,?,'open','rule-a')",
         (token, 'T1', int(time.time()), .01, 10.0, 970.0))
    monkeypatch.setattr(L.strategy_validation, 'summary', lambda db: {'passed': True, 'arm': ARM, 'passed_rules': ['rule-a']})
    monkeypatch.setattr(L.execution, 'pool', lambda *args, **kw: pool)
    monkeypatch.setattr(L.execution, 'entry', lambda *args, **kw: dict(quote))
    monkeypatch.setattr(L, 'pool_mid', lambda rpc, pk, token: .01)
    monkeypatch.setattr(L, 'approve_exact', lambda *args, **kw: int(time.time()) + 600)
    monkeypatch.setattr(L, 'call_fn', lambda *args: 10**30)     # chain.call_fn: one output comes back as the value itself
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
    monkeypatch.setattr(trader.poolstate, 'position_mids', lambda rpc, rows: ({s.token: .02}, {s.token}))   # its own pool prices it
    def quote(rpc, pk, token, amount, **kw):
        return {**s.quote, 'amount_raw': amount, 'minimum_raw': 12_000_000, 'direction': pk['c0'] == token}
    monkeypatch.setattr(L.execution, 'exit_quote', quote)
    s.rpc.receipts.clear()                             # the fake reuses one hash: the sell's receipt is not in yet
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
    monkeypatch.setattr(trader.poolstate, 'position_mids', lambda rpc, rows: ({s.token: .003}, {s.token}))   # its own pool prices it
    monkeypatch.setattr(L.execution, 'exit_quote', lambda rpc, pk, token, amount, **kw: {**s.quote, 'amount_raw': amount, 'minimum_raw': 2_000_000})
    s.rpc.receipts.clear()
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
    def sender(rpc, acct, to, data, **kw):
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
    monkeypatch.setattr(L, 'send_tx', lambda *a, **k: (HASH, {'status': '0x1'}))
    with pytest.raises(RuntimeError, match='readback'):
        L.approve_exact(object(), SimpleNamespace(address=C.WALLET), tok(5), 100)


# ---- live follows paper, the lifetime budget, and orders that never reached the key ---------------

def test_only_a_fresh_paper_entry_of_a_passed_rule_is_bought(setup, monkeypatch):
    s = setup
    s.db.x("UPDATE paper SET opened_ts=?", (int(time.time()) - L.ENTRY_MAX_AGE_S - 5,))
    buy(s)
    assert not s.calls                                   # the paper entry is no longer the same trade
    s.db.x("UPDATE paper SET opened_ts=?, strategy='rule-b'", (int(time.time()),))
    buy(s)
    assert not s.calls                                   # that rule's cohort has not passed
    s.db.x("UPDATE paper SET strategy='rule-a', status='closed'")
    buy(s)
    assert not s.calls                                   # the book already sold it
    s.db.x("UPDATE paper SET status='open'")
    buy(s)
    assert len(s.calls) == 1
    snap = json.loads(s.db.one('SELECT execution FROM trades')['execution'])
    assert snap['rule'] == 'rule-a' and snap['calldata'].startswith('0x') and len(snap['calldata']) == 66


def test_a_pool_outside_the_pilot_is_passed_over_quietly(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(L.execution, 'pool', lambda *a, **k: (_ for _ in ()).throw(ValueError('pilot requires a verified USDG pool')))
    buy(s); buy(s)
    assert not s.calls and s.db.one('SELECT status FROM trade_intents')['status'] == 'unsupported'
    assert not s.db.q("SELECT 1 FROM events WHERE kind IN ('trade','error')")


def test_no_budget_no_buys_and_losses_use_the_budget_up(setup, monkeypatch):
    s = setup
    monkeypatch.delenv('WH_TRADING_BUDGET_USD')
    buy(s)
    assert not s.calls and L.budget(s.db)['room'] == 0   # the default budget is zero: the pilot needs an explicit number
    monkeypatch.setenv('WH_TRADING_BUDGET_USD', '25')
    s.db.x("INSERT INTO positions(token,symbol,size_usd,realized_usd,status,mode) VALUES(?,?,?,?,'closed','live')", (tok(50), 'L1', 10.0, 2.0))
    s.db.x("INSERT INTO positions(token,symbol,size_usd,realized_usd,status,mode) VALUES(?,?,?,?,'closed','live')", (tok(51), 'W1', 10.0, 13.0))
    s.db.x("INSERT INTO positions(token,symbol,size_usd,realized_usd,status,mode) VALUES(?,?,?,?,'open','live')", (tok(52), 'O1', 10.0, -.1))
    b = L.budget(s.db)
    assert b == {'budget': 25.0, 'at_risk': 10.0, 'lost': 5.0, 'room': 10.0} and L.realized_pnl(s.db) == pytest.approx(-5.0)
    s.db.x("UPDATE positions SET realized_usd=40.0 WHERE token=?", (tok(51),))
    assert L.budget(s.db)['lost'] == 0 and L.budget(s.db)['room'] == 15.0      # a profit never makes room beyond the budget
    monkeypatch.setenv('WH_TRADING_BUDGET_USD', '14')
    monkeypatch.setattr(L, 'liquidation_marks', lambda rpc, db: {tok(52): 9.5})   # the open position has a fresh bid
    buy(s)
    assert s.calls and s.db.one("SELECT usd FROM trades WHERE side='buy'")['usd'] == 4.0   # sized to what is left
    monkeypatch.setenv('WH_TRADING_BUDGET_USD', 'lots')
    assert L.budget_usd() == 0.0


def test_a_refusal_before_signing_releases_the_order(setup, monkeypatch):
    s = setup
    def refused(*a, **k):
        e = RuntimeError('unresolved transaction: reconcile before sending anything new')
        e.not_signed = True
        raise e
    monkeypatch.setattr(L, 'send_tx', refused)
    buy(s)
    row = s.db.one('SELECT note, tx FROM trades')
    assert row == {'note': 'FAILED: not sent', 'tx': None}
    assert trader.open_count(s.db) == 0 and trader.spent_today(s.db) == 0     # no slot, no daily budget, nothing to review
    assert "nothing was signed" in s.db.one("SELECT text FROM events WHERE kind='trade' ORDER BY id DESC LIMIT 1")['text']


def journal(raw_to, data, h):
    """A signed legacy transaction in the private journal, as send_tx leaves it before broadcasting."""
    import rlp
    from wormhole import outbox
    raw = '0x' + rlp.encode([b'\x01', b'\x02', b'\x03', bytes.fromhex(raw_to[2:]), b'', data, b'\x25', b'\x01', b'\x01']).hex()
    outbox.record(h, C.WALLET, raw, 0.0, 'included')
    outbox.state(h, 'settled')


def test_an_order_without_a_hash_is_matched_to_the_journal_or_released(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(L, 'send_tx', lambda *a, **k: (_ for _ in ()).throw(OSError('disk full while storing the hash')))
    buy(s)
    assert s.db.one('SELECT note FROM trades')['note'] == 'REVIEW'
    L.resolve_unsent(s.db)
    assert s.db.one('SELECT note FROM trades')['note'] == 'REVIEW'            # too fresh to judge
    s.db.x('UPDATE trades SET ts=ts-?', (L.UNSENT_AFTER_S + 5,))
    L.resolve_unsent(s.db)
    assert s.db.one('SELECT note FROM trades')['note'] == 'FAILED: not sent'  # the journal never saw it: nothing was signed

    s.db.x("UPDATE trades SET note='REVIEW'")
    snap = json.loads(s.db.one('SELECT execution FROM trades')['execution'])
    calldata = b'the very bytes of this swap'
    snap['calldata'] = '0x' + __import__('eth_utils').keccak(calldata).hex()
    s.db.x('UPDATE trades SET execution=?', (json.dumps(snap),))
    journal(C.FACTORY, calldata, '0x' + 'a1' * 32)                           # same bytes to another contract: not this order
    L.resolve_unsent(s.db)
    assert s.db.one('SELECT note FROM trades')['note'] == 'FAILED: not sent'
    s.db.x("UPDATE trades SET note='REVIEW'")
    journal(C.UNIVERSAL_ROUTER, calldata, '0x' + 'b2' * 32)
    L.resolve_unsent(s.db)
    assert s.db.one('SELECT note, tx FROM trades') == {'note': 'PENDING', 'tx': '0x' + 'b2' * 32}


def test_nothing_is_decided_while_the_journal_is_mid_write(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(L, 'send_tx', lambda *a, **k: (_ for _ in ()).throw(OSError('lost')))
    buy(s)
    s.db.x('UPDATE trades SET ts=ts-?', (L.UNSENT_AFTER_S + 5,))
    monkeypatch.setattr(L.outbox, 'pending', lambda: [{'state': 'preparing', 'hash': '0x00'}])
    L.resolve_unsent(s.db)
    assert s.db.one('SELECT note FROM trades')['note'] == 'REVIEW'


def test_a_settled_buy_stands_its_exit_approval_at_once(setup, monkeypatch):
    s = setup
    asked = []
    monkeypatch.setattr(L, 'approve_exact', lambda rpc, acct, currency, need, ttl=None: asked.append((currency, need, ttl)) or int(time.time()) + 600)
    buy(s)
    s.rpc.receipts[HASH] = receipt(s.token, C.WALLET, [970 * 10**18])
    s.rpc.receipts[HASH]['logs'].append(transfer(C.USDG, C.WALLET, C.HOOK, 10_000_000))
    trader.reconcile(s.rpc, s.db, s.acct)
    assert asked[-1] == (s.token, 970 * 10**18, L.EXIT_APPROVAL_S)           # exactly what was bought, for as long as it may be held
    assert asked[0] == (C.USDG, 10_000_000, None)
    trader.reconcile(s.rpc, s.db)                                            # without the signer nothing is approved
    assert len(asked) == 2


def test_a_reverted_sell_is_retried_soon_with_more_room(setup, monkeypatch):
    s = setup
    buy(s); settle_buy(s)
    s.rpc.receipts.clear()
    monkeypatch.setattr(trader.poolstate, 'position_mids', lambda rpc, rows: ({s.token: .003}, {s.token}))   # its own pool prices it
    seen = []
    def quote(rpc, pk, token, amount, tolerance=None, **kw):
        seen.append(tolerance)
        return {**s.quote, 'amount_raw': amount, 'minimum_raw': 2_000_000}
    monkeypatch.setattr(L.execution, 'exit_quote', quote)
    trader.mark(s.rpc, s.db, True, s.acct)
    s.rpc.receipts[HASH] = {'status': '0x0', 'logs': []}
    trader.reconcile(s.rpc, s.db)
    assert s.db.one("SELECT note FROM trades WHERE side='sell'")['note'] == 'REVERTED' and seen == [None, None]
    s.rpc.receipts.clear()
    trader.mark(s.rpc, s.db, True, s.acct)
    assert s.db.one("SELECT COUNT(*) n FROM trades WHERE side='sell'")['n'] == 1    # not within the same few seconds
    s.db.x("UPDATE trades SET ts=ts-? WHERE side='sell'", (L.RETRY_UNSENT_S + 1,))
    trader.mark(s.rpc, s.db, True, s.acct)
    assert s.db.one("SELECT COUNT(*) n FROM trades WHERE side='sell'")['n'] == 2 and seen[-1] == L.execution.EXIT_TOLERANCE_RETRY


def test_the_watcher_buys_on_the_markers_last_gate_reading_only_while_it_is_fresh(setup, monkeypatch):
    s = setup
    trader.decide_now(s.rpc, s.db, True, s.acct)
    assert not s.calls                                   # no gate reading yet
    trader.remember_gate(RUNWAY, {'ready': True})
    monkeypatch.setitem(trader._gate, 'ts', time.time() - trader.GATE_MAX_AGE_S - 1)
    trader.decide_now(s.rpc, s.db, True, s.acct)
    assert not s.calls
    trader.remember_gate(RUNWAY, {'ready': False})
    trader.decide_now(s.rpc, s.db, True, s.acct)
    assert not s.calls
    trader.remember_gate(RUNWAY, {'ready': True})
    trader.decide_now(s.rpc, s.db, True, s.acct)
    assert len(s.calls) == 1


def test_allocated_burn_profit_cannot_refill_spent_principal(setup, monkeypatch):
    from wormhole import treasury as T
    s = setup
    monkeypatch.setenv('WH_TRADING_BUDGET_USD', '10')
    s.db.x("INSERT INTO positions(token,size_usd,realized_usd,status,mode) VALUES('win',10,30,'closed','live')")
    assert T.sweep_trading_profit(s.db) == 20
    s.db.x("INSERT INTO positions(token,size_usd,realized_usd,status,mode) VALUES('loss',10,0,'closed','live')")
    assert L.budget(s.db)['room'] == 0
    assert L.budget(s.db)['lost'] == 10
    # Settling the burn does not debit trading principal twice.
    s.db.x("INSERT INTO ledger(kind,amount) VALUES('burn',20)")
    assert L.budget(s.db)['lost'] == 10
    assert T.sweep_trading_profit(s.db) == 0
    # The guard survives reconstructing the database object after a restart.
    from wormhole.db import DB
    reopened = DB(s.db.path)
    assert L.budget(reopened)['room'] == 0
    reopened.c.close()


def test_live_measures_impact_against_the_pools_own_mid_like_paper(setup, monkeypatch):
    s = setup
    seen = []
    monkeypatch.setattr(L.execution, 'entry', lambda *a, **kw: seen.append(kw.get('reference')) or dict(s.quote))
    mids = iter([.0101, .0102])
    monkeypatch.setattr(L, 'pool_mid', lambda rpc, pk, token: next(mids))
    buy(s)
    assert seen == [.0101, .0102]                     # a fresh pool read for the quote and for its refresh
    assert s.db.one("SELECT COUNT(*) n FROM trades")['n'] == 1


def test_no_pool_mid_no_live_order(setup, monkeypatch):
    s = setup
    def unavailable(rpc, pk, token):
        raise ValueError('pool price unavailable')
    monkeypatch.setattr(L, 'pool_mid', unavailable)
    buy(s)
    assert s.db.one("SELECT COUNT(*) n FROM trades")['n'] == 0 and not s.calls


def test_pool_mid_reads_the_pool_and_refuses_a_missing_answer(monkeypatch):
    pk = {'c0': C.USDG, 'c1': tok(1), 'fee': 0, 'tick_spacing': 200, 'hooks': C.HOOK, 'quote': C.USDG}
    monkeypatch.setattr(L, 'token_prices', lambda tokens: {})               # no API price: no veto
    monkeypatch.setattr(L.poolstate, 'mids', lambda rpc, pools, eth: {t: .02 for t in pools})
    assert L.pool_mid(None, pk, tok(1)) == .02
    monkeypatch.setattr(L.poolstate, 'mids', lambda rpc, pools, eth: {t: None for t in pools})
    with pytest.raises(ValueError):
        L.pool_mid(None, pk, tok(1))


def test_a_pool_mid_far_from_the_price_api_is_no_live_reference(monkeypatch):
    pk = {'c0': C.USDG, 'c1': tok(1), 'fee': 0, 'tick_spacing': 200, 'hooks': C.HOOK, 'quote': C.USDG}
    monkeypatch.setattr(L.poolstate, 'mids', lambda rpc, pools, eth: {t: .02 for t in pools})
    fresh = lambda price: (lambda tokens: {tok(1): {'price_usd': price, 'observed_at': time.time()}})
    monkeypatch.setattr(L, 'token_prices', fresh(.018))                      # 11% apart: within the band
    assert L.pool_mid(None, pk, tok(1)) == .02
    monkeypatch.setattr(L, 'token_prices', fresh(.016))                      # 25% apart: a pushed pool, or a stale API
    with pytest.raises(ValueError, match='disagree'):
        L.pool_mid(None, pk, tok(1))
    def down(tokens):
        raise OSError('price API down')
    monkeypatch.setattr(L, 'token_prices', down)                             # the API failing is no veto either
    assert L.pool_mid(None, pk, tok(1)) == .02


def test_a_disagreeing_pool_skips_the_live_entry(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(L, 'pool_mid', POOL_MID)               # the real check, which the fixture stubs out
    monkeypatch.setattr(L.poolstate, 'mids', lambda rpc, pools, eth: {t: .02 for t in pools})
    monkeypatch.setattr(L, 'token_prices', lambda tokens: {t: {'price_usd': .01, 'observed_at': time.time()} for t in tokens})
    buy(s)
    assert s.db.one("SELECT COUNT(*) n FROM trades")['n'] == 0 and not s.calls


def test_paper_and_receipt_settlement_share_the_same_exit_basis(setup, monkeypatch):
    from wormhole.paper import Paper
    s = setup
    s.db.x('DELETE FROM paper')
    q = {**s.quote, 'paper_fill_raw': 970 * 10**18, 'liquidation_usd': 9.0}
    monkeypatch.setattr(L.execution, 'entry', lambda *a, **kw: dict(q))
    pb = Paper(s.db)
    assert pb.enter(s.token, 'T1', .01, 'rule-a', 'parity test')
    buy(s)
    settle_buy(s)
    paper = s.db.one('SELECT * FROM paper')
    live = s.db.one('SELECT * FROM positions')
    assert paper['entry_usd'] == live['entry_usd'] == pytest.approx(10 / 970)
    assert paper['last_usd'] == .01  # pool mid is separate from acquisition cost
    policy, _ = lab.parse_arm(lab.DEFAULT)
    decisions = []
    for row in (paper, live):
        state = {'entry': row['entry_usd'], 'entry_ts': 0, 'qty_left': 1,
                 'tp_done': [], 'peak': row['peak_usd'], 'trail_on': False}
        decisions.append([lab.exit_step(policy, state, px, ts) for ts, px in [(1, .0121), (2, .0102), (3, .013), (4, .0109)]])
    assert decisions[0] == decisions[1]
    assert decisions[0][1][0] == 0  # the old mid-price reference would have sold here
    assert decisions[0][-1][0] == 1


# ---- routed orders (any pair, through an allowlisted aggregator) ----------------------------------------

from eth_abi import decode as abi_decode, encode as abi_encode
from wormhole import route as R

KYBER = R.KYBER_ROUTER


def kyber_calldata(src, dst, receiver, amount, min_out, flags=R.KYBER_FLAGS):
    desc = (src, dst, [R.KYBER_EXECUTOR], [amount], [], [], receiver, amount, min_out, flags, b'')
    return R.KYBER_SWAP + abi_encode([R.KYBER_T], [(R.KYBER_EXECUTOR, C.ZERO, b'', desc, b'')]).hex()


@pytest.fixture
def routed(setup, monkeypatch):
    """The setup's buy, routed through KyberSwap: build, simulation and approvals recorded, never on the network."""
    s = setup
    s.quote.update(provider='kyber', route=R.candidate('kyber', C.USDG, s.token, 10_000_000, 1000 * 10**18, 500_000, 0,
                                                        KYBER, KYBER, route_summary={}))
    s.tx = {}                      # overrides for the built transaction
    s.sim = {'got': 990 * 10**18, 'spent': 10_000_000}
    s.approvals, s.sims, s.sent, s.built = [], [], [], []
    def build(q, wallet, deadline):
        s.built.append(wallet)
        t = {'receiver': wallet, 'min_out': q['out'] * 975 // 1000, 'amount': q['amount_in'], 'to': KYBER, 'value': 0, **s.tx}
        return {'to': t['to'], 'value': t['value'], 'spender': KYBER, 'out': q['out'],
                'data': kyber_calldata(q['token_in'], q['token_out'], t['receiver'], t['amount'], t['min_out'])}
    def simulate(rpc, wallet, tx, token_in, token_out, amount_in, approve_to=None):
        s.sims.append(approve_to)
        return (s.sim['got'], s.sim['spent']) if token_in == C.USDG else (s.sim['usdg'], amount_in)
    monkeypatch.setattr(R, 'build', build)
    monkeypatch.setattr(R, 'simulate', simulate)
    monkeypatch.setattr(L, 'approve_route', lambda rpc, acct, currency, spender, need: s.approvals.append((currency, spender, need)))
    s.pins, s.cleared = [], []
    monkeypatch.setattr(R, 'verify_contracts', lambda rpc, provider: s.pins.append(provider))
    monkeypatch.setattr(L, 'clear_route_allowance', lambda rpc, db, acct, currency, spender, token, swapped: s.cleared.append((currency, spender, swapped)))
    def sender(rpc, acct, to, data, **kwargs):
        s.sent.append({'to': to, 'data': data, 'value': kwargs.get('value', 0)})
        kwargs['on_broadcast'](HASH)
        return HASH, None
    monkeypatch.setattr(L, 'send_tx', sender)
    return s


def test_a_routed_buy_is_built_for_the_wallet_checked_simulated_and_sent_to_the_allowlisted_router(routed):
    s = routed
    buy(s)
    assert s.built == [C.WALLET]                                        # built for the wallet only once decided and approved
    assert s.sims == [None]                                             # simulated as it will run, the approval on chain
    assert s.approvals == [(C.USDG, KYBER, 10_000_000)]
    assert len(s.sent) == 1 and s.sent[0]['to'] == KYBER and s.sent[0]['value'] == 0
    assert s.pins and s.pins[0] == 'kyber' and s.cleared == [(C.USDG, KYBER, True)]   # pinned before, nothing left after
    snap = json.loads(s.db.one("SELECT execution FROM trades")['execution'])
    assert snap['provider'] == 'kyber' and snap['router'] == KYBER
    settle_buy(s)
    p = s.db.one('SELECT * FROM positions')
    assert p['qty_raw'] == str(970 * 10**18) and p['quote'] == 'USDG'


@pytest.mark.parametrize('unsafe', ['router', 'value', 'receiver', 'minimum', 'amount', 'short', 'overspend', 'relay', 'stale'])
def test_an_unsafe_route_is_refused_and_leaves_no_allowance(routed, unsafe):
    s = routed
    if unsafe == 'router': s.tx['to'] = '0x' + '66' * 20
    if unsafe == 'value': s.tx['value'] = 1
    if unsafe == 'receiver': s.tx['receiver'] = '0x' + '88' * 20
    if unsafe == 'minimum': s.tx['min_out'] = 1                          # the router's own floor under our slippage cap
    if unsafe == 'amount': s.tx['amount'] = 10_000_001
    if unsafe == 'short': s.sim['got'] = 969 * 10**18                    # the simulation delivers less than our minimum
    if unsafe == 'overspend': s.sim['spent'] = 10_000_001
    if unsafe == 'relay': s.quote.update(provider='relay', route=R.candidate('relay', C.USDG, s.token, 10_000_000, 10**21, 1, 0, R.RELAY_PROXY, R.RELAY_PROXY))
    if unsafe == 'stale': s.quote['route']['expires_at'] = time.time() - 1
    buy(s)
    assert not s.sent and not s.db.q('SELECT * FROM trades')
    assert s.db.one('SELECT blocked_until FROM trade_intents WHERE token=?', (s.token,))['blocked_until'] > time.time()
    if unsafe in ('relay', 'stale'):
        assert not s.approvals and not s.built                            # refused before any approval or build
    else:
        assert s.approvals and s.cleared == [(C.USDG, KYBER, False)]      # refused at the final build: the allowance reset


def test_a_different_winner_after_the_approval_is_not_bought(routed, monkeypatch):
    s = routed
    quotes = iter([dict(s.quote), {**s.quote, 'provider': 'lifi', 'route': R.candidate('lifi', C.USDG, s.token, 10_000_000,
                                                                                         10**21, 1, .025, R.LIFI_DIAMOND, R.LIFI_DIAMOND)}])
    monkeypatch.setattr(L.execution, 'entry', lambda *a, **kw: next(quotes))
    buy(s)
    assert s.approvals and not s.sent and not s.db.q('SELECT * FROM trades')


def test_the_requote_after_approval_asks_only_the_approved_provider(routed, monkeypatch):
    s = routed
    seen = []
    monkeypatch.setattr(L.execution, 'entry', lambda *a, **kw: seen.append(kw) or dict(s.quote))
    buy(s)
    assert 'providers' not in seen[0] and seen[1]['providers'] == ('kyber',) and seen[1]['direct'] is False


def test_a_routed_exit_sells_through_the_allowlisted_router_with_an_exact_approval(routed, monkeypatch):
    s = routed
    buy(s); settle_buy(s)
    monkeypatch.setattr(trader.poolstate, 'position_mids', lambda rpc, rows: ({s.token: .003}, {s.token}))
    s.sim['usdg'] = 2_050_000
    seen = []
    def quote(rpc, pk, token, amount, **kw):
        seen.append(kw)
        return {**s.quote, 'amount_raw': amount, 'minimum_raw': 2_000_000, 'provider': 'kyber',
                'route': R.candidate('kyber', token, C.USDG, amount, 2_060_000, 500_000, 0, KYBER, KYBER, route_summary={})}
    monkeypatch.setattr(L.execution, 'exit_quote', quote)
    s.rpc.receipts.clear()
    s.sent.clear(); s.approvals.clear()
    trader.mark(s.rpc, s.db, True, s.acct)
    assert s.approvals == [(s.token, KYBER, 970 * 10**18)] and s.sent and s.sent[0]['to'] == KYBER
    assert seen[0]['reference'] == .003 and seen[1]['providers'] == ('kyber',)       # the mid that triggered it bands the route
    (params,) = abi_decode([R.KYBER_T], bytes.fromhex(s.sent[0]['data'][10:]))
    assert params[3][0].lower() == s.token and params[3][1].lower() == C.USDG and params[3][6].lower() == C.WALLET


def test_routed_approvals_are_exact_zeroed_first_and_never_unlimited(monkeypatch):
    acct = SimpleNamespace(address=C.WALLET)
    state = {'allowance': 5}
    sent = []
    def call_fn(rpc, to, sig, out, types, args):
        assert sig == 'allowance(address,address)' and args == [C.WALLET, KYBER]
        return state['allowance']
    def send(rpc, acct, to, data, **kw):
        spender, amount = abi_decode(['address', 'uint256'], bytes.fromhex(data[10:]))
        sent.append((to, spender.lower(), amount))
        state['allowance'] = amount
        return HASH, {'status': '0x1'}
    monkeypatch.setattr(L, 'call_fn', call_fn)
    monkeypatch.setattr(L, 'send_tx', send)
    L.approve_route(None, acct, C.USDG, KYBER, 10_000_000)
    assert sent == [(C.USDG, KYBER, 0), (C.USDG, KYBER, 10_000_000)]            # an old allowance is zeroed first
    sent.clear()
    L.approve_route(None, acct, C.USDG, KYBER, 10_000_000)
    assert sent == []                                                            # exactly right already: nothing sent
    for bad in ((C.USDG, KYBER, 2**256 - 1), (C.USDG, KYBER, 0), (C.USDG, R.RELAY_PROXY, 1), (C.USDG, C.PERMIT2, 1),
                (C.USDG, '0x' + '66' * 20, 1)):
        with pytest.raises(ValueError):
            L.approve_route(None, acct, *bad)
    monkeypatch.setattr(L, 'send_tx', lambda *a, **k: (HASH, {'status': '0x1'}))          # a token that ignores approve
    with pytest.raises(RuntimeError, match='readback'):
        L.approve_route(None, acct, C.USDG, KYBER, 7)


def test_only_allowlisted_routers_are_journaled_or_sent_to():
    assert L.routers() == {C.UNIVERSAL_ROUTER, R.KYBER_ROUTER, R.LIFI_DIAMOND}
    with pytest.raises(ValueError):
        L.submit(None, None, None, tok(1), 'T', 'buy', 1, {'expires_at': time.time() + 30, 'pool': {}}, {}, int(time.time()) + 60,
                 tx={'to': R.RELAY_PROXY, 'data': '0x', 'value': 0})


def test_a_standing_exit_approval_serves_the_direct_exit_of_any_pool(db, monkeypatch):
    trader.ensure_tables(db)
    db.x("INSERT INTO positions(token,symbol,status,mode,pool_key) VALUES(?,?,?,?,?)", (tok(1), 'T', 'open', 'live', json.dumps({'quote': C.ZERO})))
    seen = []
    monkeypatch.setattr(L, 'approve_exact', lambda rpc, acct, token, need, ttl=None: seen.append((token, need, ttl)))
    assert L.prepare_exit(None, db, SimpleNamespace(address=C.WALLET), tok(1), 10**18) is True
    assert seen == [(tok(1), 10**18, L.EXIT_APPROVAL_S)]                  # Permit2 to the Universal Router, exact


def test_a_balance_read_decodes_the_single_value_chain_call_fn_returns():
    class Answer:
        def eth_call(self, to, data, block='latest'):
            return '0x' + (123).to_bytes(32, 'big').hex()
    class Silent:
        def eth_call(self, to, data, block='latest'):
            return '0x'
    assert L.balance_of(Answer(), C.USDG) == 123 and L.erc20_allowance(Answer(), C.USDG, KYBER) == 123
    with pytest.raises(ValueError):
        L.balance_of(Silent(), C.USDG)



# ---- availability: every aggregator down, or the simulation service -----------------------------------

def open_eth_position(s, monkeypatch, mid=.003):
    buy(s); settle_buy(s)
    pk = {**json.loads(s.db.one('SELECT pool_key FROM positions')['pool_key']), 'quote': C.ZERO}
    s.db.x('UPDATE positions SET pool_key=?', (json.dumps(pk),))
    monkeypatch.setattr(trader.poolstate, 'position_mids', lambda rpc, rows: ({s.token: mid}, {s.token}))
    s.rpc.receipts.clear()
    return pk


def own_quote(s, amount, **kw):
    pk = json.loads(s.db.one('SELECT pool_key FROM positions')['pool_key'])
    return {**s.quote, 'pool': pk, 'amount_raw': amount, 'minimum_raw': 2_000_000, 'provider': 'pons-v3',
            'route': R.candidate('pons-v3', s.token, C.USDG, amount, 2_060_000, 300_000, 0, C.UNIVERSAL_ROUTER, C.PERMIT2, v3_fee=500)}


def test_with_every_aggregator_and_the_simulator_down_a_live_position_still_sells_through_its_own_pools(routed, monkeypatch):
    s = routed
    open_eth_position(s, monkeypatch)
    seen = []
    def quote(rpc, pk, token, amount, **kw):
        seen.append(kw.get('providers'))
        if kw.get('providers') != ():
            raise R.NoRoute('no executable route within limits')           # every aggregator down
        return own_quote(s, amount)
    monkeypatch.setattr(L.execution, 'exit_quote', quote)
    monkeypatch.setattr(R, 'simulate', lambda *a, **k: pytest.fail('the direct exit needs no simulation service'))
    permits = []
    monkeypatch.setattr(L, 'approve_exact', lambda rpc, acct, token, need, ttl=None: permits.append((token, need)) or int(time.time()) + 600)
    s.sent.clear(); s.approvals.clear()
    trader.mark(s.rpc, s.db, True, s.acct)
    assert permits == [(s.token, 970 * 10**18)] and not s.approvals          # Permit2 to the Universal Router only
    assert len(s.sent) == 1 and s.sent[0]['to'] == C.UNIVERSAL_ROUTER and s.sent[0]['value'] == 0
    from eth_abi import decode as d
    cmds, inputs, _ = d(['bytes', 'bytes[]', 'uint256'], bytes.fromhex(s.sent[0]['data'][10:]))
    assert cmds == trader.V4_SWAP + trader.WRAP_ETH + trader.V3_SWAP_EXACT_IN
    assert json.loads(s.db.one("SELECT execution FROM trades WHERE side='sell'")['execution'])['provider'] == 'pons-v3'


def test_a_routed_exit_the_simulator_refuses_falls_back_to_its_own_pools(routed, monkeypatch):
    s = routed
    open_eth_position(s, monkeypatch)
    def quote(rpc, pk, token, amount, **kw):
        if kw.get('providers') == ():
            return own_quote(s, amount)
        return {**s.quote, 'amount_raw': amount, 'minimum_raw': 2_000_000, 'provider': 'kyber',
                'route': R.candidate('kyber', token, C.USDG, amount, 2_060_000, 500_000, 0, KYBER, KYBER, route_summary={})}
    monkeypatch.setattr(L.execution, 'exit_quote', quote)
    monkeypatch.setattr(R, 'simulate', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('eth_simulateV1: method not found')))
    monkeypatch.setattr(L, 'approve_exact', lambda *a, **k: int(time.time()) + 600)
    s.sent.clear()
    trader.mark(s.rpc, s.db, True, s.acct)
    assert len(s.sent) == 1 and s.sent[0]['to'] == C.UNIVERSAL_ROUTER
    assert s.db.one("SELECT COUNT(*) n FROM events WHERE text LIKE 'routed exit for%selling through its own pool'")['n'] == 1


def test_an_exit_that_fails_on_every_path_is_written_as_an_error(routed, monkeypatch):
    s = routed
    open_eth_position(s, monkeypatch)
    monkeypatch.setattr(L.execution, 'exit_quote', lambda *a, **k: (_ for _ in ()).throw(R.NoRoute('nothing')))
    s.sent.clear()
    trader.mark(s.rpc, s.db, True, s.acct)
    assert not s.sent
    assert s.db.one("SELECT kind FROM events WHERE text LIKE 'exit for%failed on every path%'")['kind'] == 'error'



def test_an_unsafe_route_after_the_approval_leaves_no_allowance(routed, monkeypatch):
    s = routed
    original = R.simulate
    def short_after_approval(rpc, wallet, tx, token_in, token_out, amount_in, approve_to=None):
        got, spent = original(rpc, wallet, tx, token_in, token_out, amount_in, approve_to)
        return (1 if approve_to is None else got), spent       # the final simulation delivers almost nothing
    monkeypatch.setattr(R, 'simulate', short_after_approval)
    buy(s)
    assert s.approvals and not s.sent and s.cleared == [(C.USDG, KYBER, False)]


def test_a_changed_contract_stops_the_order_before_any_approval(routed, monkeypatch):
    s = routed
    monkeypatch.setattr(R, 'verify_contracts', lambda rpc, provider: (_ for _ in ()).throw(R.UnsafeRoute("a pinned contract's code changed")))
    buy(s)
    assert not s.approvals and not s.sent and not s.cleared


def test_an_allowance_left_standing_is_reset_and_one_left_after_a_swap_is_an_error(db, monkeypatch):
    state = {'allowance': 7}
    sent = []
    monkeypatch.setattr(L, 'erc20_allowance', lambda rpc, currency, spender: state['allowance'])
    def send(rpc, acct, to, data, **kw):
        spender, amount = abi_decode(['address', 'uint256'], bytes.fromhex(data[10:]))
        sent.append((to, spender.lower(), amount))
        state['allowance'] = amount
        return HASH, {'status': '0x1'}
    monkeypatch.setattr(L, 'send_tx', send)
    acct = SimpleNamespace(address=C.WALLET)
    L.clear_route_allowance(None, db, acct, C.USDG, KYBER, tok(1), swapped=True)
    assert sent == [(C.USDG, KYBER, 0)] and db.one("SELECT kind FROM events WHERE text LIKE 'a router left%'")['kind'] == 'error'
    sent.clear()
    L.clear_route_allowance(None, db, acct, C.USDG, KYBER, tok(1), swapped=False)
    assert sent == []                                         # nothing stands: nothing sent
    state['allowance'] = 5
    monkeypatch.setattr(L, 'send_tx', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('unresolved transaction')))
    L.clear_route_allowance(None, db, acct, C.USDG, KYBER, tok(1), swapped=False)
    assert db.one("SELECT COUNT(*) n FROM events WHERE text LIKE 'a routed allowance could not be reset yet%'")['n'] == 1


def test_a_settled_routed_order_is_checked_for_a_standing_allowance(routed, monkeypatch):
    s = routed
    buy(s)
    s.cleared.clear()
    s.rpc.receipts[HASH] = receipt(s.token, C.WALLET, [970 * 10**18])
    s.rpc.receipts[HASH]['logs'].append(transfer(C.USDG, C.WALLET, C.HOOK, 10_000_000))
    trader.reconcile(s.rpc, s.db, s.acct)
    assert s.cleared == [(C.USDG, KYBER, True)]



def test_the_price_api_check_holds_for_a_pool_against_eth(monkeypatch):
    pk = {'c0': C.ZERO, 'c1': tok(1), 'fee': 0, 'tick_spacing': 200, 'hooks': C.HOOK, 'quote': C.ZERO}
    seen = []
    monkeypatch.setattr(L, 'eth_usd', lambda strict=False: seen.append(strict) or 2500.0)
    monkeypatch.setattr(L.poolstate, 'mids', lambda rpc, pools, eth: {t: .02 if eth == 2500.0 else None for t in pools})
    monkeypatch.setattr(L, 'token_prices', lambda tokens: {tok(1): {'price_usd': .016}})     # 25% apart
    with pytest.raises(ValueError, match='disagree'):
        L.pool_mid(None, pk, tok(1))
    assert seen == [True]                                                    # converted through a fresh ETH price only
    monkeypatch.setattr(L, 'token_prices', lambda tokens: {tok(1): {'price_usd': .019}})
    assert L.pool_mid(None, pk, tok(1)) == .02
    monkeypatch.setattr(L, 'eth_usd', lambda strict=False: None)             # no fresh ETH price: no mid, no order
    with pytest.raises(ValueError, match='unavailable'):
        L.pool_mid(None, pk, tok(1))



def test_live_marks_ask_kyberswap_and_the_pool_only(db, monkeypatch):
    trader.ensure_tables(db)
    db.x("INSERT INTO positions(token,symbol,status,mode,pool_key,qty_left_raw) VALUES(?,?,?,?,?,?)",
         (tok(1), 'T', 'open', 'live', json.dumps({'quote': C.ZERO}), str(10**21)))
    seen = []
    monkeypatch.setattr(L.execution, 'exit_quote', lambda rpc, pk, token, amount, **kw: seen.append(kw) or
                        {'minimum_raw': 9_000_000, 'gas_usd': .05})
    assert L.liquidation_marks(None, db) == {tok(1): pytest.approx(8.95)}
    assert seen == [{'lane': 'live', 'providers': ('kyber',)}]
