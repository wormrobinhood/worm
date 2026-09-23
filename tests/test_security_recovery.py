"""Fault-injection tests: all data and keys are disposable; network is prohibited."""
import json
import socket
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from fakes import transfer_log
from wormhole import config as C, outbox, tx, treasury as T, launch as L, compute as CP, server
from wormhole.chain import RpcError

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def stop(*args, **kwargs):
        raise AssertionError('network forbidden')
    monkeypatch.setattr(socket.socket, 'connect', stop)
    monkeypatch.setattr(socket, 'getaddrinfo', stop)


def test_persistence_precedes_broadcast(rpc, acct, live):
    seen = []
    def persist(h):
        assert not rpc.raw
        assert outbox.pending()[0]['hash'] == h
        seen.append(h)
    h, _ = tx.send_tx(rpc, acct, C.FACTORY, on_broadcast=persist)
    assert seen == [h] and not outbox.pending()


def test_lost_ack_and_lookup_recovers_same_payment(rpc, acct, live):
    original = rpc.call
    def flaky(method, args, **kwargs):
        if method == 'eth_getTransactionByHash':
            raise RpcError('lookup disconnected')
        return original(method, args, **kwargs)
    rpc.script = ['delivered-network']
    seen = []
    with patch.object(rpc, 'call', side_effect=flaky), pytest.raises(RpcError):
        tx.send_tx(rpc, acct, C.FACTORY, on_broadcast=seen.append)
    assert len(seen) == len(rpc.raw) == 1
    assert outbox.pending()[0]['state'] == 'ready'
    with pytest.raises(RuntimeError, match='unresolved'):
        tx.send_tx(rpc, acct, C.FACTORY)
    assert tx.recover(rpc)
    assert len(rpc.raw) == 1 and not outbox.pending()


def test_unknown_submission_replays_identical_bytes(rpc, acct, live):
    rpc.script = ['network'] * 3
    with pytest.raises(RpcError):
        tx.send_tx(rpc, acct, C.FACTORY)
    raw = outbox.pending()[0]['raw']
    assert not tx.recover(rpc)  # submit exact saved transaction, then wait for next pass
    assert rpc.raw == [raw]
    assert tx.recover(rpc)


def test_interrupted_callback_never_broadcasts(rpc, acct, live):
    def fail(h):
        raise RuntimeError('disk full')
    with pytest.raises(RuntimeError, match='disk full'):
        tx.send_tx(rpc, acct, C.FACTORY, on_broadcast=fail)
    with pytest.raises(RuntimeError, match='preparation interrupted'):
        tx.recover(rpc)
    assert rpc.raw == []


def test_fee_cap_and_pause_prevent_signing(rpc, acct, live, monkeypatch):
    monkeypatch.setenv('WH_MAX_TX_FEE_ETH', '0.0000001')
    with pytest.raises(RuntimeError, match='limits'):
        tx.send_tx(rpc, acct, C.FACTORY)
    C.DATA_DIR.mkdir(exist_ok=True)
    (C.DATA_DIR / 'payments.paused').touch()
    with pytest.raises(RuntimeError, match='paused'):
        tx.send_tx(rpc, acct, C.FACTORY)
    assert not rpc.raw


def test_accounting_failure_returns_no_spendable_funds(db, monkeypatch):
    monkeypatch.setattr(T, 'owed_total', lambda _: (_ for _ in ()).throw(RuntimeError('read failed')))
    assert T.free_usd(db, 100) == 0


def test_claim_split_is_frozen(db, monkeypatch):
    T.ensure_tables(db)
    db.x("INSERT INTO ledger(kind,amount,owner_share,burn_share,gold_share) VALUES('claim',100,.5,.2,.1)")
    monkeypatch.setattr(C, 'OWNER_SHARE', .6)
    assert T.owed_to_owner(db) == 50
    assert T.owed_to_burn(db) == 20
    assert T.owed_to_gold(db) == 10


def test_legacy_split_requires_explicit_policy(db, monkeypatch):
    T.ensure_tables(db)
    db.x("INSERT INTO ledger(kind,amount) VALUES('claim',100)")
    for name in ('OWNER','BURN','GOLD'):
        monkeypatch.delenv('WH_LEGACY_'+name+'_SHARE', raising=False)
    assert T.free_usd(db, 100) == 0
    for name, value in [('OWNER','.6'),('BURN','.2'),('GOLD','0')]:
        monkeypatch.setenv('WH_LEGACY_'+name+'_SHARE',value)
    assert T.owed_to_owner(db) == 60
    monkeypatch.setenv('WH_LEGACY_OWNER_SHARE','.5')
    assert T.owed_to_owner(db) == 60


def test_launch_settlement_is_atomic(db, monkeypatch):
    token = '0x'+'ab'*20
    db.meta_set('launch_pending','hash')
    monkeypatch.setattr(L,'launched_token',lambda _:token)
    db.x("CREATE TRIGGER reject_own BEFORE INSERT ON meta WHEN NEW.key='own_token' BEGIN SELECT RAISE(ABORT,'disk fault'); END")
    with pytest.raises(Exception, match='disk fault'):
        L._settle(db,'hash',{'status':'0x1'},'WORM')
    assert db.meta_get('launch_pending') == 'hash' and not db.meta_get('own_token')


def test_missing_launch_evidence_remains_pending(db):
    db.meta_set('launch_pending','hash')
    assert L._settle(db,'hash',{'status':'0x1','logs':[]},'WORM') is None
    assert db.meta_get('launch_pending') == 'hash'


def test_missing_transfer_evidence_does_not_release_reservation(db, rpc):
    T.ensure_tables(db)
    db.x("INSERT INTO ledger(ts,kind,amount,tx,note) VALUES(?, 'forward_pending',5,'hash','test')", (int(time.time()),))
    rpc.receipts['hash'] = {'status':'0x1','logs':[]}
    assert T.reconcile(rpc,db,('forward_pending',), C.WALLET) == 1
    assert db.one('SELECT kind FROM ledger')['kind'] == 'forward_pending'


def test_old_venice_pending_blocks_new_topup(db, acct, live, monkeypatch):
    T.ensure_tables(db)
    db.x("INSERT INTO ledger(ts,kind,asset,amount) VALUES(?,'compute_pending','USDC',5)", (int(time.time())-90000,))
    monkeypatch.setattr(CP,'status',lambda *a: pytest.fail('must not start another payment'))
    result = CP.plan(db,acct,20,True,True)
    assert 'reconciliation' in result['error']


def test_venice_amount_recipient_and_durable_authorization(acct, live, monkeypatch):
    recipient = '0x'+'33'*20
    rail = {'scheme':'exact','network':'base','asset':C.BASE_USDC,'amount':'5000000','payTo':recipient,'maxTimeoutSeconds':999999}
    class Response:
        status_code=402
        ok=True
        def json(self): return {'accepts':[rail]}
    posts=[]
    saved=[]
    def post(url, **kw):
        if 'headers' in kw:
            assert saved and int(saved[0]['validBefore'])-int(saved[0]['validAfter']) <= 900
        posts.append(kw)
        return Response()
    monkeypatch.setattr(CP.requests,'post',post)
    monkeypatch.setenv('WH_VENICE_PAY_TO',recipient)
    result=CP.top_up(acct,5,True,before_payment=saved.append)
    assert result['amount_usd']==5 and saved[0]['value']=='5000000'
    rail['amount']='1000000000'
    with pytest.raises(RuntimeError,match='minimum exceeds'):
        CP.top_up(acct,5,True,before_payment=saved.append)
    assert len(saved)==1
    rail['amount']='5000000'; rail['payTo']='0x'+'44'*20
    with pytest.raises(RuntimeError,match='recipient'):
        CP.top_up(acct,5,True,before_payment=saved.append)


def test_loopback_launch_requires_secret(monkeypatch):
    from starlette.requests import Request
    monkeypatch.delenv('WH_OPS_TOKEN', raising=False)
    request=Request({'type':'http','client':('127.0.0.1',123),'headers':[(b'origin',b'https://untrusted.example')]})
    assert not server.ops_allowed(request)


def test_pending_payout_still_reserves_wallet_funds(db):
    T.ensure_tables(db)
    db.x("INSERT INTO ledger(kind,amount,owner_share,burn_share,gold_share) VALUES('claim',100,.5,.2,.1)")
    db.x("INSERT INTO ledger(kind,asset,amount) VALUES('forward_pending','USDG',50)")
    assert T.owed_to_owner(db) == 0
    assert T.free_usd(db,100) == 20


def test_runner_recovers_launch_without_new_request(db, rpc, acct, monkeypatch):
    from run import launch_cycle
    from test_launch import launched_log, TOKEN, CURVE
    monkeypatch.setattr(C, 'TOKEN', '')
    hub = server.Hub()
    db.meta_set('launch_pending','hash')
    rpc.receipts['hash'] = {'status':'0x1','logs':[launched_log(TOKEN,CURVE,acct.address)]}
    assert not hub.launch_wanted
    assert launch_cycle(rpc,db,acct,hub) == TOKEN.lower()
    assert db.meta_get('own_token') == TOKEN.lower()
    assert not db.meta_get('launch_pending') and not rpc.raw


def test_backup_preserves_database_and_outbox(tmp_path):
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path
    source = tmp_path/'source'
    source.mkdir()
    for name in ('wormhole.db','transactions.sqlite3'):
        with sqlite3.connect(source/name) as conn:
            conn.execute('CREATE TABLE evidence(value TEXT)')
            conn.execute("INSERT INTO evidence VALUES('retained')")
    dest = tmp_path/'backup'
    result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1]/'scripts/backup-state.py'),
                             str(source),str(dest),'--service-stopped'], capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
    assert (dest/'payments.paused').exists()
    for name in ('wormhole.db','transactions.sqlite3'):
        with sqlite3.connect(dest/name) as conn:
            assert conn.execute('SELECT value FROM evidence').fetchone()[0] == 'retained'
        assert (dest/name).stat().st_mode & 0o077 == 0


def test_transferred_backup_verifies_and_tampering_is_rejected(tmp_path):
    import shutil
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    source = tmp_path / 'state'
    source.mkdir()
    with sqlite3.connect(source / 'wormhole.db') as conn:
        conn.execute('CREATE TABLE evidence(value TEXT)')
        conn.execute("INSERT INTO evidence VALUES('recover me')")
    backup = tmp_path / 'backup'
    result = subprocess.run([sys.executable, str(scripts / 'backup-state.py'), str(source), str(backup), '--service-stopped'], capture_output=True)
    assert result.returncode == 0
    restored = tmp_path / 'transferred'
    shutil.copytree(backup, restored)
    command = [sys.executable, str(scripts / 'verify-backup.py'), str(restored)]
    assert subprocess.run(command, capture_output=True).returncode == 0
    with sqlite3.connect(restored / 'wormhole.db') as conn:
        assert conn.execute('SELECT value FROM evidence').fetchone()[0] == 'recover me'
        conn.execute("UPDATE evidence SET value='tampered'")
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert subprocess.run([command[0], '-O', *command[1:]], capture_output=True).returncode != 0
    unsafe = subprocess.run([sys.executable, str(scripts / 'backup-state.py'), str(source), str(source / 'backups'), '--service-stopped'], capture_output=True)
    assert unsafe.returncode != 0 and not (source / 'backups').exists()
