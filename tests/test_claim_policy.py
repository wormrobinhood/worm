import pytest
from wormhole import claim_policy as P, treasury as T


def setup(db):
    T.ensure_tables(db)


def test_growth_cap_and_timeout(db):
    setup(db)
    assert P.batching(db, 1, now=100000)['due'] is False
    assert P.batching(db, 1, now=186400)['due'] is True
    assert P.batching(db, 1, now=186400)['threshold_usdg']==5
    db.x("INSERT INTO ledger(ts,kind,asset,amount) VALUES(100000,'claim','USDG',10000)")
    assert P.batching(db, 10, now=186400)['threshold_usdg']==100


def test_empty_balance_resets_wait(db):
    setup(db)
    P.batching(db,1,now=100000)
    P.batching(db,0,now=186400)
    assert not P.batching(db,1,now=200000)['due']


def fake_chain(monkeypatch, db, rpc):
    setup(db)
    monkeypatch.setattr(P, 'eth_usd', lambda **kw: 2500)
    monkeypatch.setattr(P, 'funded_runway', lambda *args: None)
    def call(method, args):
        return {'eth_estimateGas':hex(100000),'eth_gasPrice':hex(100000000),'eth_getBalance':hex(10**16)}[method]
    monkeypatch.setattr(rpc,'call',call)


def test_fresh_cost_permits_claim(db,rpc,monkeypatch):
    fake_chain(monkeypatch,db,rpc)
    assert P.evaluate(rpc,db,5,now=100000)['allowed']


def test_timeout_never_bypasses_cost(db,rpc,monkeypatch):
    fake_chain(monkeypatch,db,rpc)
    P.batching(db,1,now=100000)
    d=P.evaluate(rpc,db,1,now=186400)
    assert not d['allowed'] and d['reason']=='claim gas exceeds cost limit'


def test_missing_price_blocks(db,rpc,monkeypatch):
    fake_chain(monkeypatch,db,rpc)
    monkeypatch.setattr(P,'eth_usd',lambda **kw:None)
    assert not P.evaluate(rpc,db,50)['allowed']


def test_reserve_blocks(db,rpc,monkeypatch):
    fake_chain(monkeypatch,db,rpc)
    old=rpc.call
    monkeypatch.setattr(rpc,'call',lambda m,a:hex(10**13) if m=='eth_getBalance' else old(m,a))
    assert P.evaluate(rpc,db,50)['reason']=='waiting for ETH gas reserve'


@pytest.mark.parametrize('bad',['nan','-1','99999'])
def test_invalid_config_fails_closed(db,rpc,monkeypatch,bad):
    fake_chain(monkeypatch,db,rpc)
    monkeypatch.setenv('WH_CLAIM_MAX_GAS_FRACTION',bad)
    assert not P.evaluate(rpc,db,50)['allowed']


def test_direct_claim_cannot_bypass_reserve(db,rpc,acct,live,monkeypatch):
    from wormhole import prices, tx, config as C
    from fakes import word
    from wormhole.chain import selector
    setup(db)
    monkeypatch.setattr(prices,'eth_usd',lambda **kw:2500)
    rpc.eth_calls[selector('balanceOfToken(address,address)')] = word(100000000)
    monkeypatch.setenv('WH_CLAIM_ETH_RESERVE','1')
    with pytest.raises(RuntimeError,match='claim gas cost or ETH reserve'):
        tx.send_tx(rpc,acct,C.FEE_ESCROW,P.call_data('claimToken(address)',('address',),(C.USDG,)))
    assert not rpc.raw


def test_funded_claims_daily_or_sooner_for_a_large_balance(db):
    setup(db)
    assert not P.batching(db,1000,now=100000,runway_days=90)['due']
    held=P.batching(db,1000,now=107199,runway_days=90)
    assert not held['due'] and held['reason']=='large balance: spacing claims 2 hours apart' and held['next_claim_after']==107200
    early=P.batching(db,1000,now=107200,runway_days=90)
    assert early['due'] and early['reason']=='large balance claim due'
    small=P.batching(db,99.99,now=107200,runway_days=90)
    assert not small['due'] and small['reason']=='daily interval: 90 days funded' and small['next_claim_after']==186400
    assert P.batching(db,99.99,now=186400,runway_days=90)['reason']=='daily claim due'
    assert P.batching(db,1000,now=186400,runway_days=90)['reason']=='daily claim due'
    db.x("INSERT INTO ledger(ts,kind,asset,amount) VALUES(186400,'claim','USDG',1000)")
    assert not P.batching(db,1000,now=186401,runway_days=90)['due']
    assert P.batching(db,1000,now=186400+7200,runway_days=90)['due']
    assert P.batching(db,1000,now=186401,runway_days=89)['due']


def test_large_balance_settings(db,rpc,monkeypatch):
    setup(db)
    monkeypatch.setenv('WH_CLAIM_LARGE_USD','500')
    monkeypatch.setenv('WH_CLAIM_LARGE_EVERY_HOURS','6')
    P.batching(db,400,now=100000,runway_days=90)
    assert not P.batching(db,400,now=100000+6*3600,runway_days=90)['due']
    assert not P.batching(db,500,now=100000+6*3600-1,runway_days=90)['due']
    assert P.batching(db,500,now=100000+6*3600,runway_days=90)['due']
    # a large-balance bar below the ordinary minimum is a mistake: no claim at all until it is fixed
    fake_chain(monkeypatch,db,rpc)
    monkeypatch.setattr(P,'funded_runway',lambda *args:100)
    monkeypatch.setenv('WH_CLAIM_LARGE_USD','1')
    assert P.evaluate(rpc,db,1000,now=300000)=={'allowed':False,'reason':'claim policy or gas data unavailable'}


def test_daily_needs_minimum_and_does_not_count_future_income(db):
    setup(db)
    P.batching(db,1,now=100000,runway_days=100)
    assert not P.batching(db,1,now=200000,runway_days=100)['due']
    assert P.batching(db,5,now=200000,runway_days=100)['due']


def test_funded_runway_excludes_obligations(db,rpc,monkeypatch):
    from wormhole import budget
    setup(db)
    monkeypatch.setattr(budget,'COMPUTE_USD_DAY',1)
    monkeypatch.setattr(budget,'GAS_USD_DAY',0)
    monkeypatch.setattr(budget,'BRIDGE_USD_MONTH',0)
    monkeypatch.setattr(T,'usdg_balance',lambda *args:100)
    monkeypatch.setattr(T,'owed_total',lambda *args:20)
    assert P.funded_runway(rpc,db)==80
    monkeypatch.setattr(budget,'COMPUTE_USD_DAY',float('nan'))
    assert P.funded_runway(rpc,db) is None


def test_curve_wait_clock_survives_empty_escrow(db):
    setup(db)
    P.batching(db,2,now=100000,balance_key='sweep_balance_since')
    P.batching(db,0,now=100010)
    assert P.batching(db,2,now=186400,balance_key='sweep_balance_since')['due']


def test_reserve_uses_only_matching_recent_successful_claim(db, monkeypatch):
    import time
    from wormhole import claim_policy as P, config as C, treasury as T
    T.ensure_tables(db)
    db.x("INSERT INTO ledger(ts,kind,tx,amount) VALUES(?,'claim','0xabc',1)", (time.time(),))
    transaction={'from':C.WALLET,'to':C.FEE_ESCROW,'input':P.call_data('claimToken(address)',('address',),(C.USDG,))}
    receipt={'status':'0x1','gasUsed':hex(56177)}
    class Rpc:
        def call(self,method,params):return transaction if method=='eth_getTransactionByHash' else receipt
    assert P.claim_gas_reserve(Rpc(),db)==150000
    transaction['from']=C.ZERO
    assert P.claim_gas_reserve(Rpc(),db)==260000
    transaction['from']=C.WALLET;receipt['status']='0x0'
    assert P.claim_gas_reserve(Rpc(),db)==260000
    receipt.update(status='0x1',gasUsed=hex(200000))
    assert P.claim_gas_reserve(Rpc(),db)==520000
