import json
from types import SimpleNamespace
from wormhole import ops_health as H,config as C,treasury as T,gas_refill as G


def test_low_eth_and_unresolved_payment_are_actionable(db,rpc,live,monkeypatch):
    monkeypatch.delenv('WH_SCREEN_CDP_URL',raising=False)
    T.ensure_tables(db);rpc.balance=1
    db.x("INSERT INTO ledger(ts,kind,amount) VALUES(0,'compute_pending',1)")
    st=H.check(rpc,db,SimpleNamespace(),now=1000)
    assert {a['code'] for a in st['alerts']}=={'low_eth','payment_unresolved'}
    H.record(db,st)
    assert 'compute_pending' not in db.meta_get('ops_health_status')


def test_demo_wallet_zero_does_not_raise_funding_alarm(db,rpc,monkeypatch):
    monkeypatch.delenv('WH_SCREEN_CDP_URL',raising=False)
    rpc.balance=0
    assert H.check(rpc,db,SimpleNamespace(),now=1000)['ok']


def test_stale_indexer_and_browser_detected(db,rpc,monkeypatch):
    monkeypatch.setenv('WH_SCREEN_CDP_URL','http://browser:9223')
    h=SimpleNamespace(started_at=1,indexer=SimpleNamespace(last_ok=1),frame_latest={'ts':1})
    st=H.check(rpc,db,h,now=1000)
    assert {a['code'] for a in st['alerts']}=={'indexer_stale','screen_stale'}


def test_check_failure_does_not_expose_exception(db,rpc,live,monkeypatch):
    monkeypatch.setattr(rpc,'call',lambda *a:(_ for _ in ()).throw(RuntimeError('sensitive upstream response')))
    st=H.check(rpc,db,SimpleNamespace(),now=1000)
    assert not st['ok'] and 'sensitive' not in json.dumps(st)
