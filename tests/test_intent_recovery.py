"""Stuck payments: an intent left 'preparing' by a crash, a resend the node refuses, a nonce used by another
transaction. Resolved only from chain evidence, parked and surfaced otherwise; the operator's script and cancel.
Offline: the fake node and throwaway keys only."""
import importlib.util
import pathlib
import time
from types import SimpleNamespace

import pytest

from fakes import decode_tx, tx_hash
from wormhole import config as C, ops_health, outbox, treasury as T, tx
from wormhole.chain import RpcError

NOTE = "20% of income: buy $WORM on its pool and send it to the burn address"


def book(db, h, kind='burn_pending', amount=6.0):
    T.ensure_tables(db)
    db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)", (int(time.time()), kind, 'USDG', amount, h, NOTE))


def crashed_while_preparing(rpc, acct, db):
    """The pending row is written, then the process dies before the journal says 'ready'."""
    seen = []

    def pending(h):
        book(db, h)
        seen.append(h)
        raise RuntimeError('killed by a redeploy')
    with pytest.raises(RuntimeError, match='redeploy'):
        tx.send_tx(rpc, acct, C.UNIVERSAL_ROUTER, data='0x01', on_broadcast=pending)
    assert outbox.pending()[0]['state'] == 'preparing' and rpc.raw == []
    return seen[0]


def never_delivered(rpc, acct, db, with_row=True):
    """Signed, journaled 'ready', but no node ever took it (three unanswered broadcasts)."""
    rpc.script = ['network'] * 3
    with pytest.raises(RpcError):
        tx.send_tx(rpc, acct, C.UNIVERSAL_ROUTER, data='0x02', on_broadcast=(lambda h: book(db, h)) if with_row else None)
    item = outbox.pending()[0]
    assert item['state'] == 'ready' and rpc.raw == []
    return item['hash']


def codes(rpc, db):
    return {a['code'] for a in ops_health.check(rpc, db, SimpleNamespace(), now=time.time())['alerts']}


# ---- (a) interrupted preparation -------------------------------------------------------------------------------------

def test_interrupted_preparation_is_abandoned_and_its_payment_owed_again(db, rpc, acct, live):
    h = crashed_while_preparing(rpc, acct, db)
    assert tx.recover(rpc, db)                                     # no longer raises forever
    assert outbox.get(h)['state'] == 'abandoned' and outbox.pending() == []
    assert rpc.raw == []                                           # the prepared bytes were never sent
    assert T.reconcile(rpc, db, ('burn_pending',), C.WALLET) == 0
    row = db.one("SELECT * FROM ledger WHERE tx=?", (h,))
    assert row['kind'] == 'burn_failed' and 'never broadcast' in row['note']
    assert 'burn never broadcast' in db.one("SELECT text FROM events WHERE kind='error' ORDER BY id DESC")['text']
    tx.send_tx(rpc, acct, C.FACTORY)                               # new sends are no longer blocked
    assert len(rpc.raw) == 1


def test_the_identical_payment_can_be_signed_again_after_its_intent_was_abandoned(db, rpc, acct, live):
    """Signing is deterministic: rebuilt with the same nonce and gas, the retry has the abandoned intent's hash.
    It takes that intent up again instead of failing on the journal key every cycle."""
    h = crashed_while_preparing(rpc, acct, db)
    assert tx.recover(rpc, db) and outbox.get(h)['state'] == 'abandoned'
    again, _ = tx.send_tx(rpc, acct, C.UNIVERSAL_ROUTER, data='0x01')
    assert again == h and len(rpc.raw) == 1 and tx_hash(rpc.raw[0]) == h
    assert outbox.get(h)['state'] != 'abandoned'


def test_without_the_database_an_interrupted_preparation_still_stops_everything(rpc, acct, live, db):
    crashed_while_preparing(rpc, acct, db)
    with pytest.raises(RuntimeError, match='preparation interrupted'):
        tx.recover(rpc)                                            # the launch paths pass no database


def test_a_prepared_transaction_the_node_holds_is_parked_not_guessed(db, rpc, acct, live):
    h = crashed_while_preparing(rpc, acct, db)
    rpc.known.add(h)                                               # contradicts the journal: never handed to a node
    assert not tx.recover(rpc, db) and not tx.recover(rpc, db)     # parked, and no exception on every cycle
    assert outbox.get(h)['state'] == 'preparing' and 'node holds it' in outbox.get(h)['note']
    assert db.one("SELECT kind FROM ledger WHERE tx=?", (h,))['kind'] == 'burn_pending'
    assert 'transaction_parked' in codes(rpc, db)
    stalled = ops_health.check(rpc, db, SimpleNamespace(), now=time.time())['stalled']
    assert stalled[0]['hash'] == h and stalled[0]['source'] == 'journal'
    with pytest.raises(RuntimeError, match='unresolved'):
        tx.send_tx(rpc, acct, C.FACTORY)


def test_a_preparation_owned_by_a_trade_is_left_to_the_operator(db, rpc, acct, live):
    from wormhole import trader
    h = crashed_while_preparing(rpc, acct, db)
    db.x("DELETE FROM ledger")
    trader.ensure_tables(db)
    db.x("INSERT INTO trades(ts,token,side,tx,mode,note) VALUES(?,?,?,?,?,?)", (0, '0x' + '11' * 20, 'buy', h, 'live', 'PENDING'))
    assert not tx.recover(rpc, db)
    assert 'trades bookkeeping' in outbox.get(h)['note'] and outbox.get(h)['state'] == 'preparing'


# ---- (b) a resend the node refuses; nonces used elsewhere; the operator's cancel -------------------------------------

def test_a_refused_resend_is_parked_instead_of_raising_every_cycle(db, rpc, acct, live):
    h = never_delivered(rpc, acct, db)
    rpc.script = ['replacement transaction underpriced'] * 2
    assert not tx.recover(rpc, db)
    assert not tx.recover(rpc, db)
    assert outbox.get(h)['note'] == 'resend refused by the node: gas price too low to replace or enter the pool'
    assert 'transaction_parked' in codes(rpc, db)
    assert tx.recover(rpc, db) is False and rpc.raw                # the node takes it later: delivered, waiting
    assert outbox.get(h)['note'] == ''                             # the note clears with the evidence
    assert tx.recover(rpc, db) and db.one("SELECT kind FROM ledger WHERE tx=?", (h,))['kind'] == 'burn_pending'


def test_the_operator_cancel_replaces_it_and_recovery_closes_the_original(db, rpc, acct, live):
    h = never_delivered(rpc, acct, db)
    old = outbox.get(h)
    h2 = tx.cancel(rpc, acct, h)
    c = decode_tx(rpc.raw[-1])
    assert tx_hash(rpc.raw[-1]) == h2 and c['to'] == C.WALLET and c['value'] == 0 and c['data'] == b''
    assert c['nonce'] == outbox.fields(old['raw'])[0] and c['gasPrice'] > outbox.fields(old['raw'])[1] * 1.25
    assert outbox.get(h2)['note'] == f'cancels {h}'
    assert tx.recover(rpc, db)                                      # the cancel settled; the original can never be mined
    assert outbox.get(h2)['state'] == 'settled' and outbox.get(h)['state'] == 'dropped'
    assert h2 in outbox.get(h)['note']
    T.reconcile(rpc, db, ('burn_pending',), C.WALLET)
    row = db.one("SELECT * FROM ledger WHERE tx=?", (h,))
    assert row['kind'] == 'burn_failed' and 'never mined' in row['note']


def test_if_the_original_lands_instead_the_cancel_is_closed(db, rpc, acct, live):
    h = never_delivered(rpc, acct, db)
    rpc.script = ['network'] * 3                                   # the cancel gets no answer
    with pytest.raises(RpcError):
        tx.cancel(rpc, acct, h)
    h2 = outbox.pending()[-1]['hash']
    rpc.script = ['ok', 'nonce too low']                            # the original goes in first; the cancel is late
    assert not tx.recover(rpc, db)
    assert tx.recover(rpc, db)
    assert outbox.get(h)['state'] == 'settled' and outbox.get(h2)['state'] == 'dropped'


def test_cancel_refuses_whenever_normal_settlement_is_still_possible(db, rpc, acct, live):
    h, _ = tx.send_tx(rpc, acct, C.FACTORY, wait=False)
    with pytest.raises(RuntimeError, match='already mined'):
        tx.cancel(rpc, acct, h)
    with pytest.raises(RuntimeError, match='no unsettled intent'):
        tx.cancel(rpc, acct, '0x' + '00' * 32)


def test_a_nonce_used_outside_the_journal_is_parked_and_only_the_operator_accepts_it(db, rpc, acct, live):
    h = never_delivered(rpc, acct, db)
    rpc.nonce += 1                                                 # another signer used our nonce
    assert not tx.recover(rpc, db)
    assert 'not in this journal' in outbox.get(h)['note'] and outbox.get(h)['state'] == 'ready'
    assert db.one("SELECT kind FROM ledger WHERE tx=?", (h,))['kind'] == 'burn_pending'
    script = load_script()
    lines = []
    assert script.main(['--accept', h], rpc=rpc, db=db, out=lines.append) == 0   # dry run first
    assert outbox.get(h)['state'] == 'ready' and any('dry run' in x for x in lines)
    assert script.main(['--accept', h, '--apply'], rpc=rpc, db=db, out=lines.append) == 0
    assert outbox.get(h)['state'] == 'dropped'
    T.reconcile(rpc, db, ('burn_pending',), C.WALLET)
    assert db.one("SELECT kind FROM ledger WHERE tx=?", (h,))['kind'] == 'burn_failed'


# ---- the operator script ---------------------------------------------------------------------------------------------

def load_script():
    path = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'resolve-intent.py'
    spec = importlib.util.spec_from_file_location('resolve_intent', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_dry_run_prints_the_evidence_and_changes_nothing(db, rpc, acct, live):
    h = crashed_while_preparing(rpc, acct, db)
    lines = []
    assert load_script().main([], rpc=rpc, db=db, out=lines.append) == 0
    text = '\n'.join(lines)
    assert h in text and 'evidence: abandoned' in text and 'dry run' in text and 'bookkeeping: ledger' in text
    assert outbox.get(h)['state'] == 'preparing' and rpc.raw == []
    assert outbox.get(h)['raw'] not in text                         # signed bytes are never printed
    assert load_script().main(['--apply'], rpc=rpc, db=db, out=lines.append) == 0
    assert outbox.get(h)['state'] == 'abandoned'


def test_script_cancel_is_a_dry_run_unless_applied(db, rpc, acct, live):
    h = never_delivered(rpc, acct, db)
    lines = []
    assert load_script().main(['--cancel', h], rpc=rpc, db=db, acct=acct, out=lines.append) == 0
    assert rpc.raw == [] and len(outbox.pending()) == 1
    assert load_script().main(['--cancel', h, '--apply'], rpc=rpc, db=db, acct=acct, out=lines.append) == 0
    assert len(rpc.raw) == 1 and decode_tx(rpc.raw[0])['to'] == C.WALLET
