import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from wormhole import config as C, launch_schedule as S, launch as L, server, treasury as T
from test_server import FakeRpc, FakeBrain, FakePaper, small_snapshot

@pytest.fixture(autouse=True)
def no_token(monkeypatch):
    monkeypatch.setattr(C,'TOKEN','')


def test_requires_timezone_and_future(db):
    for value in ('2030-01-01T12:00:00','invalid','2000-01-01T00:00:00Z'):
        with pytest.raises(ValueError):
            S.configure(db,value)
    result=S.configure(db,'2030-01-01T12:00:00+03:00',now=100)
    assert result['at']==S.parse_time('2030-01-01T09:00:00Z')
    assert result['state']=='scheduled'
    assert S.configure(db,None)['state']=='cancelled'


def test_due_launch_runs_once_and_survives_restart(db, acct, live, monkeypatch):
    at=S.parse_time('2030-01-01T00:00:00Z')
    S.configure(db,'2030-01-01T00:00:00Z',now=at-10)
    hub=server.Hub()
    calls=[]
    def launch(*args):
        calls.append(1)
        db.meta_set('own_token','0x'+'ab'*20)
        return '0x'+'ab'*20
    monkeypatch.setattr(L,'go',launch)
    # No browser or manual request flag is involved.
    S.tick(FakeRpc(),db,acct,hub,now=at-1)
    assert calls==[]
    S.tick(FakeRpc(),db,acct,server.Hub(),now=at)
    S.tick(FakeRpc(),db,acct,server.Hub(),now=at+1)
    assert calls==[1] and S.status(db)['state']=='launched'


def test_due_but_unarmed_does_not_launch(db, acct, monkeypatch):
    S.store(db,{'state':'scheduled','at':1})
    monkeypatch.setattr(L,'go',lambda *args:pytest.fail('must not launch'))
    S.tick(FakeRpc(),db,acct,server.Hub(),now=2)
    assert S.status(db,2)['state']=='due'
    assert S.saved(db)['state']=='scheduled'


def test_failed_schedule_does_not_retry_automatically(db, acct, live, monkeypatch):
    S.store(db,{'state':'scheduled','at':1})
    calls=[]
    monkeypatch.setattr(L,'go',lambda *args:calls.append(1))
    for _ in range(3):S.tick(FakeRpc(),db,acct,server.Hub(),now=2)
    assert calls==[1] and S.saved(db)['state']=='failed'


def test_interrupted_start_requires_review(db, acct, live, monkeypatch):
    S.store(db,{'state':'scheduled','at':1})
    def crash(*args): raise RuntimeError('interrupted')
    monkeypatch.setattr(L,'go',crash)
    with pytest.raises(RuntimeError):S.tick(FakeRpc(),db,acct,server.Hub(),now=2)
    assert S.saved(db)['state']=='launching'
    assert S.status(db)['state']=='review'
    S.tick(FakeRpc(),db,acct,server.Hub(),now=3)
    with pytest.raises(ValueError):S.configure(db,'2030-01-01T00:00:00Z',now=3)


def test_schedule_controls_require_secret(db, monkeypatch):
    monkeypatch.setattr(server,'snapshot',small_snapshot)
    monkeypatch.setenv('WH_OPS_TOKEN','test-ops-secret')
    app=server.make_app(FakeRpc(),db,FakeBrain(),FakePaper(),server.Hub())
    client=TestClient(app,client=('127.0.0.1',1234))
    payload={'at':'2030-01-01T00:00:00Z'}
    assert client.post('/api/launch/schedule',json=payload).status_code==403
    r=client.post('/api/launch/schedule',json=payload,headers={'X-Ops-Token':'test-ops-secret'})
    assert r.status_code==200 and r.json()['state']=='scheduled'
    r=client.get('/api/launch/status')
    assert r.status_code==200 and r.json()['at'] and r.headers['cache-control']=='no-store'
    assert 'test-ops-secret' not in r.text
    r=client.post('/api/launch/schedule',json={'at':None},headers={'X-Ops-Token':'test-ops-secret'})
    assert r.json()['state']=='cancelled'


def test_paid_pending_blocks_scheduled_launch(db, acct, live, monkeypatch):
    T.ensure_tables(db)
    db.x("INSERT INTO ledger(kind,asset,amount) VALUES('compute_pending','USDC',5)")
    S.store(db,{'state':'scheduled','at':1})
    monkeypatch.setattr(L,'go',lambda *args:pytest.fail('must wait for reconciliation'))
    S.tick(FakeRpc(),db,acct,server.Hub(),now=2)
    assert S.saved(db)['state']=='scheduled'
