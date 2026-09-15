"""Included receipts can advance payments while preserving identity and reorg checks."""
import pytest

from fakes import block_hash, transfer_log
from wormhole import config as C, finality as F, outbox, tx, treasury as T
from wormhole.chain import RpcError

H = '0x' + 'ab' * 32


def mined(rpc, status='0x1'):
    rpc.latest = 16
    rpc.finalized = 10
    rpc.receipts[H] = {'status': status, 'blockNumber': '0x10', 'logs': []}


@pytest.mark.parametrize('approval', [False, True])
@pytest.mark.parametrize('status', ['0x1', '0x0'])
def test_default_accepts_included_receipt_without_finality(rpc, monkeypatch, approval, status):
    monkeypatch.delenv('WH_TX_CONFIRMATION', raising=False)
    mined(rpc, status)
    assert F.policy() == 'included'
    assert F.receipt(rpc, H, approval=approval)['status'] == status
    assert len(outbox.provisional_anchors()) == 1
    rpc.finalized = 16
    F.audit(rpc)
    assert not outbox.provisional_anchors()


def test_inclusion_recovery_never_repeats_completed_send(rpc, acct, live):
    rpc.finalized = 10
    h, _ = tx.send_tx(rpc, acct, C.FACTORY, wait=False)
    assert outbox.pending()[0]['receipt_mode'] == 'included'
    assert tx.recover(rpc) and not outbox.pending()
    assert tx.recover(rpc)
    assert len([c for c in rpc.calls if c[0] == 'eth_sendRawTransaction']) == 1
    assert outbox.provisional_anchors()[0]['hash'] == h


@pytest.mark.parametrize('fault', ['block', 'missing', 'status'])
def test_changed_accepted_payment_blocks_new_send(rpc, acct, live, fault):
    mined(rpc)
    assert F.receipt(rpc, H)
    if fault == 'block':
        rpc.blocks[16] = {'number': '0x10', 'hash': block_hash(999)}
    elif fault == 'missing':
        rpc.receipts[H] = None
    else:
        rpc.receipts[H]['status'] = '0x0'
    with pytest.raises(RpcError, match='paused'):
        tx.send_tx(rpc, acct, C.FACTORY)
    assert outbox.finality_value('incident') == 'accepted_receipt_changed'
    assert not rpc.raw


@pytest.mark.parametrize('fault', ['missing', 'wrong_hash', 'noncanonical', 'future', 'changed_logs'])
def test_included_mode_still_requires_consistent_evidence(rpc, monkeypatch, fault):
    mined(rpc)
    if fault == 'missing':
        rpc.receipts[H] = None
    elif fault == 'wrong_hash':
        rpc.receipts[H]['transactionHash'] = '0x' + 'cd' * 32
    elif fault == 'noncanonical':
        rpc.receipts[H]['blockHash'] = block_hash(999)
    elif fault == 'future':
        rpc.latest = 15
    else:
        original = rpc.call
        reads = 0
        def changing(method, params, **kw):
            nonlocal reads
            result = original(method, params, **kw)
            if method == 'eth_getTransactionReceipt':
                reads += 1
                if reads == 2:
                    result['logs'] = [{'changed': True}]
            return result
        monkeypatch.setattr(rpc, 'call', changing)
    if fault == 'wrong_hash':
        with pytest.raises(RpcError):
            F.receipt(rpc, H)
    else:
        assert F.receipt(rpc, H) is None
    assert not outbox.provisional_anchors()


@pytest.mark.parametrize('valid_transfer', [True, False])
def test_included_forward_requires_exact_recipient_and_amount_once(db, rpc, valid_transfer):
    T.ensure_tables(db)
    mined(rpc)
    recipient = C.OWNER_WALLET if valid_transfer else C.WALLET
    rpc.receipts[H]['logs'] = [transfer_log(C.USDG, C.WALLET, recipient, 1000000)]
    db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,to_addr) VALUES(0,'forward_pending','USDG',1,?,?)", (H, C.OWNER_WALLET))
    reconcile = lambda: T.reconcile(rpc, db, ('forward_pending',), C.WALLET, lambda row: row['to_addr'])
    assert reconcile() == (0 if valid_transfer else 1)
    assert reconcile() == (0 if valid_transfer else 1)
    assert len(db.q("SELECT * FROM ledger WHERE kind='forward'")) == int(valid_transfer)


def test_reverted_inclusion_is_failure_not_payment(db, rpc):
    T.ensure_tables(db)
    mined(rpc, '0x0')
    db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,to_addr) VALUES(0,'forward_pending','USDG',1,?,?)", (H, C.OWNER_WALLET))
    assert T.reconcile(rpc, db, ('forward_pending',), C.WALLET) == 0
    assert db.one('SELECT kind FROM ledger')['kind'] == 'forward_failed'


def test_invalid_policy_cannot_sign(rpc, acct, live, monkeypatch):
    monkeypatch.setenv('WH_TX_CONFIRMATION', 'sucess')
    with pytest.raises(RpcError, match='WH_TX_CONFIRMATION'):
        tx.send_tx(rpc, acct, C.FACTORY)
    assert not rpc.raw and not outbox.pending()
