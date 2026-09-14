"""Offline settlement boundaries and restart/reorg fault injection."""
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from fakes import block_hash, transfer_log
from wormhole import config as C, finality as F, outbox, tx, treasury as T
from wormhole import fee_sweep, gas_refill, launch, ops_health
from wormhole.chain import RpcError, call_data

H = '0x' + 'ab' * 32


def mined(rpc, status='0x1'):
    rpc.latest = 40
    rpc.finalized = 10
    rpc.receipts[H] = {'status': status, 'blockNumber': '0x10', 'logs': []}


@pytest.mark.parametrize('status', ['0x1', '0x0'])
def test_financial_success_and_revert_wait_for_finality(rpc, status):
    mined(rpc, status)
    assert F.receipt(rpc, H) is None
    rpc.finalized = 16
    assert F.receipt(rpc, H)['status'] == status
    assert not outbox.provisional_anchors()


def test_mined_payment_never_rebroadcast_or_replaced(rpc, acct, live):
    rpc.finalized = 10
    h, _ = tx.send_tx(rpc, acct, C.FACTORY, wait=False)
    assert not tx.recover(rpc)
    with pytest.raises(RuntimeError, match='unresolved'):
        tx.send_tx(rpc, acct, C.FACTORY)
    assert len([c for c in rpc.calls if c[0]=='eth_sendRawTransaction']) == 1
    rpc.finalized = 16
    assert tx.recover(rpc) and not outbox.pending()


@pytest.mark.parametrize('field,value', [
    ('transactionHash', '0x'+'cd'*32), ('status', None), ('status', '0x2'),
    ('blockHash', '0x123'), ('blockNumber', 'broken'),
])
def test_malformed_evidence_never_settles(rpc, field, value):
    mined(rpc)
    rpc.receipts[H][field] = value
    with pytest.raises((RpcError, ValueError)):
        F.receipt(rpc, H)


def test_noncanonical_unaccepted_receipt_waits_without_latch(rpc):
    mined(rpc)
    rpc.receipts[H]['blockHash'] = block_hash(999)
    assert F.receipt(rpc, H) is None
    assert outbox.finality_value('incident') is None


def test_receipt_changes_between_reads_stays_pending(rpc, monkeypatch):
    mined(rpc); rpc.finalized = 16
    original = rpc.call
    reads = 0
    def changing(method, params, **kw):
        nonlocal reads
        rc = original(method, params, **kw)
        if method == 'eth_getTransactionReceipt':
            reads += 1
            if reads == 2:
                rc['logs'] = [{'unexpected': True}]
        return rc
    monkeypatch.setattr(rpc, 'call', changing)
    assert F.receipt(rpc, H) is None


def test_no_finalized_rpc_support_blocks_signing(rpc, acct, live, monkeypatch):
    original = rpc.call
    def unsupported(method, params, **kw):
        if method == 'eth_getBlockByNumber' and params[0]=='finalized':
            raise RpcError('tag not supported')
        return original(method, params, **kw)
    monkeypatch.setattr(rpc, 'call', unsupported)
    with pytest.raises(RpcError):
        tx.send_tx(rpc, acct, C.FACTORY)
    assert not rpc.raw and not outbox.pending()


def test_approval_depth_and_finalization(rpc):
    mined(rpc); rpc.latest = 34
    assert F.receipt(rpc, H, approval=True) is None
    rpc.latest = 35
    assert F.receipt(rpc, H, approval=True)
    assert len(outbox.provisional_anchors()) == 1
    rpc.finalized = 16
    F.audit(rpc)
    assert not outbox.provisional_anchors()


@pytest.mark.parametrize('fault', ['block', 'missing', 'status'])
def test_accepted_approval_change_latches_across_restart(rpc, fault):
    mined(rpc)
    assert F.receipt(rpc, H, approval=True)
    if fault=='block':
        rpc.blocks[16] = {'number': '0x10', 'hash': block_hash(999)}
    elif fault=='missing':
        rpc.receipts[H] = None
    else:
        rpc.receipts[H]['status'] = '0x0'
    with pytest.raises(RpcError, match='paused'):
        F.audit(rpc)
    (C.DATA_DIR/'payments.paused').unlink()
    # Fresh connection/process memory cannot bypass the durable incident.
    from fakes import FakeRpc
    with pytest.raises(RpcError, match='operator review'):
        F.audit(FakeRpc())


def test_finalized_checkpoint_change_pauses(rpc):
    F.audit(rpc)
    rpc.blocks[100] = {'number':hex(100),'hash':block_hash(999)}
    with pytest.raises(RpcError, match='paused'):
        F.audit(rpc)


def test_regressed_node_cannot_lower_checkpoint(rpc):
    F.audit(rpc)
    rpc.finalized = 99
    with pytest.raises(RpcError, match='regressed'):
        F.audit(rpc)
    assert json.loads(outbox.finality_value('checkpoint'))['number']==100


def test_only_approval_call_gets_short_confirmation_policy(rpc, acct, live):
    rpc.finalized = 10; rpc.latest = 35
    data = call_data('approve(address,uint256)', ('address','uint256'), (C.SWAP_ROUTER_V3, 100))
    h, rc = tx.send_tx(rpc, acct, C.USDG, data)
    assert rc and len(outbox.provisional_anchors())==1
    with outbox.journal() as journal:
        assert journal.execute('SELECT receipt_mode FROM intents WHERE hash=?',(h,)).fetchone()[0]=='approval'
    h2, _ = tx.send_tx(rpc, acct, C.FACTORY, data, wait=False)
    assert outbox.pending()[0]['hash']==h2
    assert outbox.pending()[0]['receipt_mode']=='finalized'


def test_legacy_outbox_defaults_to_stronger_policy():
    C.DATA_DIR.mkdir(parents=True)
    with sqlite3.connect(C.DATA_DIR/'transactions.sqlite3') as db:
        db.execute('CREATE TABLE intents(hash TEXT PRIMARY KEY,sender TEXT,raw TEXT,state TEXT,ts REAL,fee REAL)')
        db.execute('INSERT INTO intents VALUES(?,?,?,?,?,?)',(H,C.WALLET,'private-test-bytes','ready',time.time(),0))
    assert outbox.pending()[0]['receipt_mode']=='finalized'


def test_reconcile_forward_reserves_until_finalized_and_only_once(db, rpc):
    T.ensure_tables(db); mined(rpc)
    rpc.receipts[H]['logs'] = [transfer_log(C.USDG,C.WALLET,C.OWNER_WALLET,1000000)]
    db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,to_addr) VALUES(0,'forward_pending','USDG',1,?,?)",(H,C.OWNER_WALLET))
    reconcile = lambda: T.reconcile(rpc,db,('forward_pending',),C.WALLET,lambda row:row['to_addr'])
    assert reconcile()==1 and T.owed_total(db)==1
    rpc.finalized=16
    assert reconcile()==0 and T.owed_total(db)==0
    assert reconcile()==0 and len(db.q("SELECT * FROM ledger WHERE kind='forward'"))==1


@pytest.mark.parametrize('kind',['sweep','refill','launch'])
def test_reconcile_paths_retain_mined_reverts_until_finalized(db,rpc,kind):
    mined(rpc,'0x0')
    if kind=='sweep':
        fee_sweep.ensure(db)
        db.x("INSERT INTO fee_sweeps(ts,tx,state) VALUES(0,?,'pending')",(H,))
        check=lambda:fee_sweep.reconcile(rpc,db)
        state=lambda:db.one('SELECT state FROM fee_sweeps')['state']
    elif kind=='refill':
        gas_refill.ensure(db)
        db.x("INSERT INTO gas_refills(ts,tx,state) VALUES(0,?,'pending')",(H,))
        check=lambda:gas_refill.reconcile(rpc,db)
        state=lambda:db.one('SELECT state FROM gas_refills')['state']
    else:
        db.meta_set('launch_pending',H)
        check=lambda:launch.go(rpc,db,None)
        state=lambda:'pending' if db.meta_get('launch_pending') else 'reverted'
    check(); assert state()=='pending'
    rpc.finalized=16
    check(); assert state()=='reverted'


def test_normal_wait_healthy_but_stale_or_preparing_alerts(db,rpc):
    now=time.time()
    outbox.record(H,C.WALLET,'test-bytes',0); outbox.state(H,'ready')
    assert ops_health.check(rpc,db,SimpleNamespace(),now=now+1200)['ok']
    codes=lambda at:{a['code'] for a in ops_health.check(rpc,db,SimpleNamespace(),now=at)['alerts']}
    assert 'transaction_unresolved' in codes(now+3601)
    outbox.state(H,'preparing')
    assert 'transaction_unresolved' in codes(now)
