"""The initial buy is not trading; receipts, reserves and restart recovery gate each leg."""
import json
from types import SimpleNamespace

import pytest
from eth_abi import encode, decode

from fakes import word, transfer_log, topic_addr, decode_tx, tx_hash
from test_launch import launched_log, TOKEN, CURVE
from wormhole import config as C, launch as L, launch_allocation as A, launch_schedule as S, treasury as T
from wormhole.chain import selector, RpcError

SUPPLY=10**27


def abi(types, values):
    return '0x'+encode(types,values).hex()


@pytest.fixture
def initial(rpc,db,acct,live,monkeypatch):
    monkeypatch.setattr(C,'TOKEN','')
    monkeypatch.setattr(C,'TRADING',False)
    monkeypatch.setenv('WH_LAUNCH_BUY','1')
    monkeypatch.setenv('WH_LAUNCH_BUY_MAX_USDG','70')
    monkeypatch.setenv('WH_LAUNCH_KEEP_USDG','76.50')
    monkeypatch.setenv('WH_LAUNCH_KEEP_ETH','0.0001')
    monkeypatch.setattr(rpc,'block_number',lambda:100,raising=False)
    responses={
        'factory()':abi(['address'],[C.FACTORY]),
        'launchForwarder()':abi(['address'],[C.PONS_ROUTER]),
        'getLaunchConfig(uint256)':abi(['uint256','uint256','uint256','uint256','uint24','int24','bool'],
                                     [SUPPLY,100,168*10**16,420*10**16,0,200,True]),
        'pairTokenEconomics(address)':abi(['uint256','uint256','uint8'],[3236000000,8090000000,6]),
        'previewLaunchEconomics(uint256,address)':'0x'+'a1'*32,
        'launchFee()':word(500000000000000),
        'balanceOf(address)':word(200_000_000),
        'allowance(address,address)':word(0),
        'transfer(address,uint256)':word(1),
    }
    rpc.eth_calls.update({selector(k):v for k,v in responses.items()})
    q=A.quote(rpc,200)
    buy_selector=selector(f'launchAndBuy({L.TOKEN_PARAMS_T},uint256,address,uint256,uint256,address,address[])')
    rpc.eth_calls[buy_selector]=abi(['address','address','uint256'],[TOKEN,CURVE,q['expected_units']])
    applied=set()
    def receipt(h):
        if h not in rpc.known:return None
        t=decode_tx(next(raw for raw in rpc.raw if tx_hash(raw)==h))
        sig='0x'+t['data'][:4].hex()
        logs=[]
        if sig==selector('approve(address,uint256)'):
            if h not in applied:
                rpc.eth_calls[selector('allowance(address,address)')]=word(q['quote_units'])
        elif sig==buy_selector:
            logs=launch_logs(q,acct.address)
            if h not in applied:
                rpc.eth_calls[selector('balanceOf(address)')]=word(q['expected_units'])
        elif sig==selector('transfer(address,uint256)'):
            recipient,amount=decode(['address','uint256'],t['data'][4:])
            logs=[transfer_log(TOKEN,acct.address,recipient,amount)]
        applied.add(h)
        return {'status':'0x1','logs':logs,'blockNumber':'0x10'}
    rpc.receipt_for=receipt
    return q


def launch_logs(q,wallet):
    return [launched_log(TOKEN,CURVE,wallet),
            {'address':C.PONS_ROUTER,'topics':[A.ROUTER_EVENT.topic,topic_addr(TOKEN),topic_addr(CURVE),topic_addr(wallet)],
             'data':abi(['address','uint256','uint256'],[wallet,q['quote_units'],q['expected_units']]),
             'blockNumber':'0x10','transactionHash':'0x'+'00'*32,'logIndex':'0x1'},
            transfer_log(TOKEN,C.ZERO,CURVE,q['supply']),
            transfer_log(TOKEN,CURVE,wallet,q['expected_units'])]


def test_integer_quote_covers_two_percent_with_tiny_rounding():
    amount,target,out=A.quote_amount(SUPPLY,3236000000,100,200)
    assert 68_000_000<amount<69_000_000
    assert target==SUPPLY//50 and out>=target
    assert out-target<10**18  # less than one token; no float error in 27-digit supply
    for supply in (10**6,10**27,10**36):
        for fee in (0,100,1000):
            for tax in (0,200,1000):
                gross,target,out=A.quote_amount(supply,3236000000,fee,tax)
                assert out>=target
                net=gross-gross*fee//10000-gross*tax//10000
                assert out==net*supply//(3236000000+net)


@pytest.mark.parametrize('args',[(0,100,100,200),(101,100,100,200),(100,0,100,200),(100,100,9000,1000),(100,100,-1,200)])
def test_bad_economics_rejected(args):
    with pytest.raises(ValueError):A.quote_amount(*args)


def test_full_flow_once_and_trading_stays_off(initial,rpc,db,acct):
    for _ in range(8):L.go(rpc,db,acct)
    p=A.saved(db)
    assert p['state']=='complete' and C.TOKEN==TOKEN and not C.TRADING
    assert len(rpc.raw)==3
    approval,buy,transfer=map(decode_tx,rpc.raw)
    assert approval['to']==C.USDG and buy['to']==C.PONS_ROUTER and transfer['to']==TOKEN
    args=decode(['address','uint256'],transfer['data'][4:])
    assert args==(C.OWNER_WALLET,SUPPLY//100)
    assert T.owed_total(db)==0
    assert not db.meta_get('launch_pending')
    assert S.status(db)['allocation']['state']=='complete'
    assert p['received_units']-p['creator_units']>=SUPPLY//100


@pytest.mark.parametrize('after', [1,2,3])
def test_restart_recovers_only_missing_legs(initial,rpc,db,acct,monkeypatch,after):
    for _ in range(after):L.go(rpc,db,acct)
    # Persistent DB/outbox are retained; token/config can be restored at process startup.
    monkeypatch.setattr(C,'TOKEN',db.meta_get('own_token') or '')
    monkeypatch.setenv('WH_LAUNCH_BUY','0')
    monkeypatch.setenv('WH_LAUNCH_BUY_MAX_USDG','1000')
    original_owner=C.OWNER_WALLET
    monkeypatch.setattr(C,'OWNER_WALLET','0x'+'33'*20)
    hub=SimpleNamespace(launch_wanted=False)
    for _ in range(8):S.tick(rpc,db,acct,hub)
    assert A.saved(db)['state']=='complete' and len(rpc.raw)==3
    assert A.saved(db)['creator']==original_owner
    assert A.saved(db)['max_units']==70_000_000


def test_missing_receipt_never_repeats_launch(initial,rpc,db,acct):
    L.go(rpc,db,acct); L.go(rpc,db,acct)
    p=A.saved(db); assert p['state']=='launch_pending'
    rpc.receipts[p['launch_tx']]=None
    for _ in range(5):L.go(rpc,db,acct)
    assert len(rpc.raw)==2 and not C.TOKEN


def test_missing_transfer_event_holds_for_review(initial,rpc,db,acct):
    for _ in range(3):L.go(rpc,db,acct)
    p=A.saved(db); assert p['state']=='transfer_pending'
    rpc.receipts[p['transfer_tx']]={'status':'0x1','logs':[]}
    for _ in range(5):L.go(rpc,db,acct)
    assert A.saved(db)['state']=='review' and len(rpc.raw)==3


@pytest.mark.parametrize('mutate', ['router','recipient','mint','quantity'])
def test_wrong_launch_evidence_never_pays_creator(initial,rpc,db,acct,mutate):
    L.go(rpc,db,acct); L.go(rpc,db,acct)
    p=A.saved(db); logs=launch_logs(initial,acct.address)
    if mutate=='router':logs[1]['address']=C.FACTORY
    if mutate=='recipient':logs[1]['topics'][3]=topic_addr(C.OWNER_WALLET)
    if mutate=='mint':logs[2]['data']=word(SUPPLY+1)
    if mutate=='quantity':logs[3]['data']=word(initial['target_units']-1)
    rpc.receipts[p['launch_tx']]={'status':'0x1','logs':logs}
    L.go(rpc,db,acct)
    assert A.saved(db)['state']=='review' and not C.TOKEN and len(rpc.raw)==2
    assert db.meta_get('launch_pending')==p['launch_tx']


def test_reverted_launch_never_relaunched(initial,rpc,db,acct):
    L.go(rpc,db,acct);L.go(rpc,db,acct)
    p=A.saved(db)
    rpc.receipts[p['launch_tx']]={'status':'0x0','logs':[]}
    for _ in range(4):L.go(rpc,db,acct)
    assert A.saved(db)['state']=='reverted' and not C.TOKEN and len(rpc.raw)==2


@pytest.mark.parametrize('setting,value',[
    ('WH_LAUNCH_BUY_MAX_USDG','1'),('WH_LAUNCH_BUY_MAX_USDG','NaN'),
    ('WH_LAUNCH_KEEP_USDG','0'),('WH_LAUNCH_KEEP_ETH','-1')])
def test_cap_and_reserve_settings_fail_before_any_signature(initial,rpc,db,acct,monkeypatch,setting,value):
    monkeypatch.setenv(setting,value)
    with pytest.raises(ValueError):L.go(rpc,db,acct)
    assert not rpc.raw and not A.saved(db)


def test_runway_and_owed_funds_cannot_fund_purchase(initial,rpc,db,acct):
    rpc.eth_calls[selector('balanceOf(address)')]=word(initial['quote_units']+76_500_000-1)
    with pytest.raises(ValueError,match='protected USDG'):L.go(rpc,db,acct)
    assert not rpc.raw


def test_disabled_live_never_signs(initial,rpc,db,acct,monkeypatch):
    monkeypatch.setattr(C,'LIVE',False)
    L.go(rpc,db,acct)
    assert not rpc.raw


def test_pause_after_launch_holds_creator_transfer(initial,rpc,db,acct):
    L.go(rpc,db,acct);L.go(rpc,db,acct)
    (C.DATA_DIR/'payments.paused').touch()
    L.go(rpc,db,acct)
    assert A.saved(db)['state']=='allocation_ready' and C.TOKEN==TOKEN and len(rpc.raw)==2


def test_economics_change_after_approval_stops(initial,rpc,db,acct):
    L.go(rpc,db,acct)
    rpc.eth_calls[selector('previewLaunchEconomics(uint256,address)')]='0x'+'b2'*32
    L.go(rpc,db,acct)
    assert A.saved(db)['state']=='review' and len(rpc.raw)==1


def test_simulation_must_match_actual_quantity(initial,rpc,db,acct):
    L.go(rpc,db,acct)
    sig=selector(f'launchAndBuy({L.TOKEN_PARAMS_T},uint256,address,uint256,uint256,address,address[])')
    rpc.eth_calls[sig]=abi(['address','address','uint256'],[TOKEN,CURVE,initial['target_units']-1])
    L.go(rpc,db,acct)
    assert A.saved(db)['state']=='review' and len(rpc.raw)==1


def test_no_new_schedule_over_saved_intent(initial,rpc,db,acct):
    L.go(rpc,db,acct)
    with pytest.raises(ValueError,match='already exists'):S.configure(db,'2099-01-01T00:00:00Z')


def test_direct_cli_cannot_bypass_allocation_service(initial):
    with pytest.raises(SystemExit,match='service'):L.main(['--live'])
