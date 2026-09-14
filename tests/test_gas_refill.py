import pytest
from eth_abi import decode
from fakes import transfer_log, topic_addr, word, decode_tx, tx_hash
from wormhole import config as C, gas_refill as G, treasury as T, outbox, tx


def configure(db, rpc, monkeypatch):
    T.ensure_tables(db)
    monkeypatch.setenv('WH_GAS_REFILL', '1')
    monkeypatch.setenv('WH_GAS_REFILL_MAX_USD', '2')
    monkeypatch.setenv('WH_GAS_REFILL_DAILY_USD', '5')
    monkeypatch.setattr(G, 'eth_usd', lambda **kw: 2500)
    monkeypatch.setattr(T, 'usdg_balance', lambda *args: 20)
    monkeypatch.setattr(G, 'quote', lambda r,n: n * 400000000)
    monkeypatch.setattr(G, 'call_fn', lambda r,c,s,*args: C.WETH if s=='WETH9()' else 10**9)
    rpc.balance = int(0.00029 * 1e18)
    rpc.gas_price = 1000000
    original = rpc.call
    monkeypatch.setattr(rpc,'call',lambda m,p,**kw: hex(C.CHAIN_ID) if m=='eth_chainId' else original(m,p,**kw))
    def receipts(h):
        raw = next((x for x in rpc.raw if tx_hash(x)==h), None)
        if raw is None:return None
        t = decode_tx(raw)
        deadline, parts = decode(['uint256','bytes[]'], t['data'][4:])
        fields = decode([T.SWAP_V3_T], parts[0][4:])[0]
        token_in, token_out, fee, recipient, amount, minimum, limit = fields
        assert (token_in,token_out,fee,recipient)==(C.USDG,C.WETH,100,C.SWAP_ROUTER_V3)
        assert decode(['uint256','address'],parts[1][4:])==(minimum,C.WALLET)
        return {'status':'0x1','blockNumber':'0x10','logs':[
            transfer_log(C.USDG,C.WALLET,C.SWAP_ROUTER_V3,amount),
            {'address':C.WETH,'topics':[G.WITHDRAWAL,topic_addr(C.SWAP_ROUTER_V3)],'data':word(minimum)}]}
    rpc.receipt_for = receipts


def test_atomic_refill_and_restart_cannot_repeat(db,rpc,acct,live,monkeypatch):
    configure(db,rpc,monkeypatch)
    assert G.cycle(rpc,db,acct)
    row=db.one('SELECT * FROM gas_refills')
    assert row['state']=='settled' and row['amount']<=2000000
    assert db.one("SELECT * FROM ledger WHERE kind='gas'")['amount']==row['amount']/1e6
    assert G.cycle(rpc,db,acct) and len(rpc.raw)==1
    G.settle(db,dict(row,state='pending'),rpc.receipt(row['tx']))
    assert len(db.q("SELECT * FROM ledger WHERE kind='gas'"))==1


def test_unproven_receipt_reserves_cash_and_blocks_restart(db,rpc,acct,live,monkeypatch):
    configure(db,rpc,monkeypatch)
    receipt = rpc.receipt_for
    rpc.receipt_for=lambda h:dict(receipt(h),logs=[])
    assert not G.cycle(rpc,db,acct)
    row=db.one('SELECT * FROM gas_refills')
    assert T.owed_total(db)==row['amount']/1e6
    assert not G.cycle(rpc,db,acct) and len(rpc.raw)==1
    rpc.receipt_for=receipt
    assert G.reconcile(rpc,db) and T.owed_total(db)==0


def test_allocations_and_operations_floor_never_spent(db,rpc,monkeypatch):
    configure(db,rpc,monkeypatch)
    db.x("INSERT INTO ledger(kind,amount,owner_share,burn_share,gold_share) VALUES('claim',24,.5,.2,.1)")
    with pytest.raises(ValueError,match='unreserved'):G.plan(rpc,db)


@pytest.mark.parametrize('case',['price','quote','bootstrap','fees','daily','disabled','wrong_chain'])
def test_unsafe_or_disabled_refill_sends_nothing(db,rpc,acct,live,monkeypatch,case):
    configure(db,rpc,monkeypatch)
    if case=='price':monkeypatch.setattr(G,'eth_usd',lambda **kw:None)
    if case=='quote':monkeypatch.setattr(G,'quote',lambda *args:1)
    if case=='bootstrap':rpc.balance=10
    if case=='fees':rpc.gas_price=10**12
    if case=='disabled':monkeypatch.setenv('WH_GAS_REFILL','0')
    if case=='wrong_chain':monkeypatch.setattr(C,'CHAIN_ID',8453)  # RPC fixture uses this too; pin wrong response below
    if case=='wrong_chain':
        original=rpc.call
        monkeypatch.setattr(rpc,'call',lambda m,p,**kw:'0x123' if m=='eth_chainId' else original(m,p,**kw))
    if case=='daily':
        G.ensure(db)
        import time
        db.x("INSERT INTO gas_refills(ts,amount,state) VALUES(?,5000000,'settled')",(time.time()-25000,))
    G.cycle(rpc,db,acct)
    assert not rpc.raw


def test_callback_failure_blocks_broadcast(db,rpc,acct,live,monkeypatch):
    configure(db,rpc,monkeypatch)
    original=db.x
    def fail(sql,*args):
        if sql.startswith('INSERT INTO gas_refills'):raise OSError('disk full')
        return original(sql,*args)
    monkeypatch.setattr(db,'x',fail)
    assert not G.cycle(rpc,db,acct)
    assert not rpc.raw and outbox.pending()[0]['state']=='preparing'


def test_confirmed_revert_has_cooldown_without_gas_income(db,rpc,acct,live,monkeypatch):
    configure(db,rpc,monkeypatch)
    rpc.receipt_for=lambda h:{'status':'0x0','blockNumber':'0x10','logs':[]}
    assert G.cycle(rpc,db,acct)
    assert db.one('SELECT state FROM gas_refills')['state']=='reverted'
    assert not db.one("SELECT 1 FROM ledger WHERE kind='gas'")
    assert G.cycle(rpc,db,acct) and len(rpc.raw)==1


def test_other_transfers_cannot_drain_bootstrap(rpc,acct,live,monkeypatch):
    monkeypatch.setenv('WH_GAS_REFILL','1')
    rpc.balance=10**14
    with pytest.raises(RuntimeError,match='bootstrap'):
        tx.send_tx(rpc,acct,C.OWNER_WALLET,value=60000000000000)
    assert not rpc.raw


def test_exact_approval_then_atomic_swap(db,rpc,acct,live,monkeypatch):
    configure(db,rpc,monkeypatch)
    monkeypatch.setattr(G,'call_fn',lambda r,c,s,*args:C.WETH if s=='WETH9()' else 0)
    original=rpc.receipt_for
    def receipts(h):
        raw=next((x for x in rpc.raw if tx_hash(x)==h),None)
        if raw and decode_tx(raw)['to']==C.USDG:
            spender,amount=decode(['address','uint256'],decode_tx(raw)['data'][4:])
            assert spender==C.SWAP_ROUTER_V3 and 250000<=amount<=2000000
            return {'status':'0x1','blockNumber':'0x10','logs':[], 'gasUsed':'0x186a0','effectiveGasPrice':'0xf4240'}
        return original(h)
    rpc.receipt_for=receipts
    assert G.cycle(rpc,db,acct) and len(rpc.raw)==2
    assert db.one('SELECT state FROM gas_refills')['state']=='settled'


def test_missing_allowance_receipt_cannot_start_swap(db,rpc,acct,live,monkeypatch):
    configure(db,rpc,monkeypatch)
    monkeypatch.setattr(G,'call_fn',lambda r,c,s,*args:C.WETH if s=='WETH9()' else 0)
    rpc.receipt_for=lambda h:None
    monkeypatch.setattr(tx,'RECEIPT_WAIT_S',1)
    assert not G.cycle(rpc,db,acct)
    assert len(rpc.raw)==1 and outbox.pending()
    assert not db.one('SELECT 1 FROM gas_refills')


def test_chain_weth_burn_event_reconciles_without_new_transaction(db,rpc,acct,live,monkeypatch):
    configure(db,rpc,monkeypatch)
    original=rpc.receipt_for
    def receipt(h):
        rc=original(h)
        if rc:
            n=int(rc['logs'][-1]['data'],16)
            rc['logs'][-1]=transfer_log(C.WETH,C.SWAP_ROUTER_V3,C.ZERO,n)
        return rc
    rpc.receipt_for=receipt
    assert G.cycle(rpc,db,acct)
    row=db.one('SELECT * FROM gas_refills')
    assert row['state']=='settled' and int(row['received'])==int(row['minimum'])
    assert G.reconcile(rpc,db) and len(rpc.raw)==1
