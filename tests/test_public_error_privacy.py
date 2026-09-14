"""Upstream failures may include credentials; public status must use fixed messages."""
import pytest
from wormhole import config as C, treasury as T, tx, wallet as W

MARKER='private-upstream-marker'

def fail(*args, **kwargs):
    raise RuntimeError(MARKER)


@pytest.mark.parametrize('action',['claim','forward','burn_pool','burn_quote','burn_approval','burn_send','gold_quote','gold_approval','gold_send','reconcile'])
def test_treasury_errors_keep_private_text_out_of_events(db,rpc,acct,live,monkeypatch,action):
    from wormhole import trader
    T.ensure_tables(db)
    monkeypatch.setattr(C,'TOKEN','0x'+'44'*20)
    monkeypatch.setattr(T,'owed_to_owner',lambda db:10)
    monkeypatch.setattr(T,'owed_to_burn',lambda db:10)
    monkeypatch.setattr(T,'owed_to_gold',lambda db:10)
    monkeypatch.setattr(T,'usdg_balance',lambda *a:100)
    monkeypatch.setattr(trader,'pool_key',lambda *a:{'quote':C.USDG})
    monkeypatch.setattr(trader,'quote_buy',lambda *a:(10**18,100000,True))
    monkeypatch.setattr(T,'quote_gold',lambda *a:10**18)
    monkeypatch.setattr(T,'approve_for_router',lambda *a:int(T.time.time())+T.PERMIT_TTL_S)
    monkeypatch.setattr(T,'approve_for_gold',lambda *a:None)
    monkeypatch.setattr(T,'burn_calldata',lambda *a:'0x')
    monkeypatch.setattr(tx,'send_tx',fail)
    heard=[];monkeypatch.setattr(T,'WATCH',heard.append)
    if action=='claim':T.claim(rpc,db,acct,10)
    elif action=='forward':T.forward(rpc,db,acct)
    elif action.startswith('burn'):
        if action=='burn_pool':monkeypatch.setattr(trader,'pool_key',fail)
        if action=='burn_quote':monkeypatch.setattr(trader,'quote_buy',fail)
        if action=='burn_approval':monkeypatch.setattr(T,'approve_for_router',fail)
        T.burn(rpc,db,acct)
    elif action.startswith('gold'):
        if action=='gold_quote':monkeypatch.setattr(T,'quote_gold',fail)
        if action=='gold_approval':monkeypatch.setattr(T,'approve_for_gold',fail)
        T.gold(rpc,db,acct)
    else:
        monkeypatch.setattr(T,'reconcile',fail)
        T.cycle(rpc,db,acct)
    events=db.events(40)
    assert events and MARKER not in str(events) and MARKER not in str(heard)


def test_wallet_balance_errors_do_not_return_upstream_text(monkeypatch):
    monkeypatch.setattr(W,'Rpc',fail)
    st=W.balances(C.WALLET)
    assert set(st)=={'rh_error','base_error'} and MARKER not in str(st)


def test_accounting_summary_does_not_return_private_error(db,rpc,monkeypatch):
    monkeypatch.setattr(T,'owed_to_owner',fail)
    st=T.summary(rpc,db)
    assert st['owed_to_owner'] is None and MARKER not in str(st)
