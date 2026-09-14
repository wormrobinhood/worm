import pytest
from eth_abi import encode
from wormhole import fee_sweep as F, config as C, treasury as T, claim_policy as P, outbox
from fakes import decode_tx

TOKEN='0x'+'66'*20
CURVE='0x'+'77'*20


def swept(amount=10000000):
    return {'address':CURVE,'topics':[F.SWEPT], 'data':'0x'+encode(['uint256']*3,[1000000,0,amount]).hex()}


def configure(db,rpc,monkeypatch):
    T.ensure_tables(db)
    monkeypatch.setattr(C,'TOKEN',TOKEN)
    monkeypatch.setattr(F,'candidate',lambda _: {'curve':CURVE,'amount':10})
    monkeypatch.setattr(P,'funded_runway',lambda *args:10)
    monkeypatch.setattr(P,'eth_usd',lambda **kw:2500)
    rpc.logs=[swept()]
    rpc.eth_calls[F.call_data("sweepFees(uint256)", ("uint256",), (0,))[:10]]="0x"


def test_sweep_persists_then_confirms_without_income(db,rpc,acct,live,monkeypatch):
    configure(db,rpc,monkeypatch)
    assert F.cycle(rpc,db,acct,0)
    row=db.one('SELECT * FROM fee_sweeps')
    assert row['state']=='settled' and row['credited_usdg']==10
    assert not db.one('SELECT 1 FROM ledger')
    t=decode_tx(rpc.raw[0]);assert t['to']==CURVE and t['value']==0
    assert t['data'].hex()==F.call_data('sweepFees(uint256)',('uint256',),(0,))[2:]
    assert F.cycle(rpc,db,acct,0) and len(rpc.raw)==1


def test_missing_receipt_event_blocks_restart_and_other_payments(db,rpc,acct,live,monkeypatch):
    configure(db,rpc,monkeypatch);rpc.logs=[]
    assert not F.cycle(rpc,db,acct,0)
    assert db.one('SELECT state FROM fee_sweeps')['state']=='pending'
    assert not F.cycle(rpc,db,acct,0) and len(rpc.raw)==1
    rpc.logs=[swept()]
    assert F.reconcile(rpc,db)
    assert db.one('SELECT state FROM fee_sweeps')['state']=='settled'


def test_wrong_event_contract_cannot_settle(db,rpc,acct,live,monkeypatch):
    configure(db,rpc,monkeypatch);rpc.logs=[dict(swept(),address=TOKEN)]
    assert not F.cycle(rpc,db,acct,0)


def test_revert_is_recorded_and_cooled_down(db,rpc,acct,live,monkeypatch):
    configure(db,rpc,monkeypatch);rpc.status='0x0'
    assert F.cycle(rpc,db,acct,0)
    assert db.one('SELECT state FROM fee_sweeps')['state']=='reverted'
    assert F.cycle(rpc,db,acct,0) and len(rpc.raw)==1


def test_unavailable_price_no_sweep(db,rpc,acct,live,monkeypatch):
    configure(db,rpc,monkeypatch);monkeypatch.setattr(P,'eth_usd',lambda **kw:None)
    assert F.cycle(rpc,db,acct,0) and not rpc.raw


def test_reserve_and_cost_blocks(db,rpc,acct,live,monkeypatch):
    configure(db,rpc,monkeypatch);rpc.balance=1
    assert F.cycle(rpc,db,acct,0) and not rpc.raw
    rpc.balance=10**18;rpc.gas_price=10**11
    assert F.cycle(rpc,db,acct,0) and not rpc.raw


def test_funded_sweep_waits_daily(db,rpc,acct,live,monkeypatch):
    configure(db,rpc,monkeypatch);monkeypatch.setattr(P,'funded_runway',lambda *args:100)
    assert F.cycle(rpc,db,acct,0) and not rpc.raw


def candidate_chain(rpc,monkeypatch):
    monkeypatch.setattr(C,'TOKEN',TOKEN)
    words=[TOKEN,CURVE,C.WALLET,C.WALLET,C.USDG]
    record='0x'+''.join(x[2:].zfill(64) for x in words)+('0'*64)*5
    monkeypatch.setattr(rpc,'eth_call',lambda *args:record)
    vals={'graduated()':False,'buybackEnabled()':False,'factory()':C.FACTORY,'token()':TOKEN,
          'pairToken()':C.USDG,'deployer()':C.WALLET,'feeEscrow()':C.FEE_ESCROW,
          'buybackQuoteBalance()':0,'quoteFeeBalance()':1000000,'creatorTaxBalance()':2000000,'protocolFeeShareBps()':3000}
    monkeypatch.setattr(F,'call_fn',lambda r,c,s,*args:vals[s])
    return vals


def test_candidate_exact_fee_allocation(rpc,monkeypatch):
    candidate_chain(rpc,monkeypatch)
    assert F.candidate(rpc)=={'curve':CURVE,'amount':2.7}


@pytest.mark.parametrize('key,value',[('deployer()',TOKEN),('feeEscrow()',TOKEN),('buybackEnabled()',True),('buybackQuoteBalance()',1),('protocolFeeShareBps()',10001)])
def test_candidate_rejects_wrong_identity_or_buyback(rpc,monkeypatch,key,value):
    vals=candidate_chain(rpc,monkeypatch);vals[key]=value
    with pytest.raises(ValueError):F.candidate(rpc)


def test_graduated_curve_skips(rpc,monkeypatch):
    vals=candidate_chain(rpc,monkeypatch);vals['graduated()']=True
    assert F.candidate(rpc) is None


def test_pending_callback_failure_cannot_broadcast(db,rpc,acct,live,monkeypatch):
    configure(db,rpc,monkeypatch)
    original=db.x
    def fail(sql,*args):
        if sql.startswith('INSERT INTO fee_sweeps'):raise RuntimeError('disk failure')
        return original(sql,*args)
    monkeypatch.setattr(db,'x',fail)
    F.cycle(rpc,db,acct,0)
    assert not rpc.raw and outbox.pending()[0]['state']=='preparing'


@pytest.mark.parametrize('extra',[{'fee_limit_eth':1e-12},{'min_remaining_eth':1.0}])
def test_sender_rechecks_sweep_budget(rpc,acct,live,extra):
    from wormhole.tx import send_tx
    with pytest.raises(RuntimeError,match='operation gas budget or reserve'):
        send_tx(rpc,acct,CURVE,F.call_data('sweepFees(uint256)',('uint256',),(0,)),**extra)
    assert not rpc.raw


def test_treasury_stops_on_unresolved_sweep(db,rpc,acct,live,monkeypatch):
    configure(db,rpc,monkeypatch)
    F.ensure(db)
    db.x("INSERT INTO fee_sweeps(ts,token,curve,expected_usdg,tx,state) VALUES(0,?,?,10,?,'pending')",(TOKEN,CURVE,'0x'+'99'*32))
    monkeypatch.setattr(T,'claimable_usdg',lambda *args:100)
    called=[]
    monkeypatch.setattr(T,'claim',lambda *args:called.append('claim'))
    monkeypatch.setattr(T,'forward',lambda *args:called.append('forward'))
    T.cycle(rpc,db,acct)
    assert called==[] and not rpc.raw
