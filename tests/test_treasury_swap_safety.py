"""Bounded approvals, stale quotes and recovery, using disposable offline chain state."""
import time
from types import SimpleNamespace

import pytest
from eth_abi import decode

from wormhole import config as C, treasury as T, trader, tx, outbox
from fakes import decode_tx, tx_hash, uint_result, word
from test_treasury import (TOKEN, ALLOWANCE, P2_ALLOWANCE, QUOTE_SEL, QUOTE_V3_SEL,
                           ledger, rows, pool, burn_chain, burn_receipts,
                           gold_chain, gold_receipts, approval_reads)


def prepare(kind, db, rpc, monkeypatch):
    if kind == 'burn':
        monkeypatch.setattr(C, 'TOKEN', TOKEN)
        pool(db)
        ledger(db, 'claim', 30)
        burn_chain(rpc)
        burn_receipts(rpc)
        return T.burn, C.UNIVERSAL_ROUTER
    ledger(db, 'claim', 60)
    gold_chain(rpc)
    gold_receipts(rpc)
    return T.gold, C.SWAP_ROUTER_V3


@pytest.mark.parametrize('kind', ['burn', 'gold'])
def test_legacy_unlimited_approval_is_reduced_before_swap(kind, db, rpc, acct, live, monkeypatch):
    action, router = prepare(kind, db, rpc, monkeypatch)
    approval_reads(rpc, 2**256-1, 2**160-1, 2**48-1)
    assert action(rpc, db, acct)
    sent = [decode_tx(raw) for raw in rpc.raw]
    assert sent[-1]['to'] == router
    assert decode(['address', 'uint256'], sent[0]['data'][4:])[1] == 6_000_000
    if kind == 'burn':
        _, _, amount, expiry = decode(['address', 'address', 'uint160', 'uint48'], sent[1]['data'][4:])
        assert amount == 6_000_000 and expiry <= int(time.time()) + T.PERMIT_TTL_S


@pytest.mark.parametrize('expiry_delta', [-1, 10, 100_000])
def test_permit_expiry_is_renewed_or_shortened(db, rpc, acct, live, monkeypatch, expiry_delta):
    prepare('burn', db, rpc, monkeypatch)
    approval_reads(rpc, 6_000_000, 6_000_000, int(time.time()) + expiry_delta)
    exp = T.approve_for_router(rpc, db, acct, 6_000_000)
    assert len(rpc.raw) == 1 and decode_tx(rpc.raw[0])['to'] == C.PERMIT2
    assert int(time.time()) + T.SWAP_TTL_S <= exp <= int(time.time()) + T.PERMIT_TTL_S


@pytest.mark.parametrize('kind', ['burn', 'gold'])
def test_success_receipt_without_effective_approval_cannot_send_swap(kind, db, rpc, acct, live, monkeypatch):
    action, router = prepare(kind, db, rpc, monkeypatch)
    rpc.eth_calls[ALLOWANCE] = word(0)  # an ERC-20 may return false without reverting
    assert not action(rpc, db, acct)
    assert all(decode_tx(raw)['to'] != router for raw in rpc.raw)
    assert not rows(db, kind + '_pending') and not rows(db, kind)
    assert (T.owed_to_burn if kind == 'burn' else T.owed_to_gold)(db) == 6


def test_permit_readback_must_confirm_exact_amount(db, rpc, acct, live, monkeypatch):
    prepare('burn', db, rpc, monkeypatch)
    rpc.eth_calls[P2_ALLOWANCE] = uint_result(0, 0, 0)
    assert not T.burn(rpc, db, acct)
    assert all(decode_tx(raw)['to'] != C.UNIVERSAL_ROUTER for raw in rpc.raw)


@pytest.mark.parametrize('kind', ['burn', 'gold'])
def test_swap_uses_quote_refreshed_after_approval(kind, db, rpc, acct, live, monkeypatch):
    action, router = prepare(kind, db, rpc, monkeypatch)
    observations = []
    def quote(params):
        observations.append(len(rpc.raw))
        amount = 10**18 if len(observations) == 1 else 5*10**17
        return uint_result(amount, 100_000) if kind == 'burn' else uint_result(amount, 0, 0, 100_000)
    rpc.eth_calls[QUOTE_SEL if kind == 'burn' else QUOTE_V3_SEL] = quote
    assert action(rpc, db, acct)
    assert observations[0] == 0 and observations[1] > 0
    data = decode_tx(rpc.raw[-1])['data']
    if kind == 'burn':
        _, inputs, deadline = decode(['bytes', 'bytes[]', 'uint256'], data[4:])
        _, params = decode(['bytes', 'bytes[]'], inputs[0])
        minimum = decode([trader.SWAP_T], params[0])[0][3]
        slippage = T.BURN_SLIPPAGE
    else:
        deadline, calls = decode(['uint256', 'bytes[]'], data[4:])
        minimum = decode([T.SWAP_V3_T], calls[0][4:])[0][5]
        slippage = T.GOLD_SLIPPAGE
    assert minimum == int(5*10**17 * (1-slippage))
    assert int(time.time()) < deadline <= int(time.time()) + T.SWAP_TTL_S


@pytest.mark.parametrize('kind', ['burn', 'gold'])
@pytest.mark.parametrize('failure', ['zero', 'rpc_error', 'slow'])
def test_refresh_failure_leaves_share_owed_without_swap(kind, failure, db, rpc, acct, live, monkeypatch):
    action, router = prepare(kind, db, rpc, monkeypatch)
    clock = [1_800_000_000.0]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    calls = []
    def quote(params):
        calls.append(1)
        amount = 10**18
        if len(calls) == 2:
            if failure == 'rpc_error':
                raise RuntimeError('private-provider-detail')
            if failure == 'zero': amount = 0
            if failure == 'slow': clock[0] += T.QUOTE_MAX_AGE_S + 1
        return uint_result(amount, 100_000) if kind == 'burn' else uint_result(amount, 0, 0, 100_000)
    rpc.eth_calls[QUOTE_SEL if kind == 'burn' else QUOTE_V3_SEL] = quote
    assert not action(rpc, db, acct)
    assert all(decode_tx(raw)['to'] != router for raw in rpc.raw)
    assert not rows(db, kind + '_pending') and not outbox.pending()
    assert (T.owed_to_burn if kind == 'burn' else T.owed_to_gold)(db) == 6
    assert 'private-provider-detail' not in str(db.events(50))


@pytest.mark.parametrize('kind', ['burn', 'gold'])
def test_ambiguous_swap_retains_original_bytes_and_deadline_on_recovery(kind, db, rpc, acct, live, monkeypatch):
    action, router = prepare(kind, db, rpc, monkeypatch)
    base = rpc.receipt_for
    def receipt(h):
        raw = next((r for r in rpc.raw if tx_hash(r) == h), None)
        if raw and decode_tx(raw)['to'] == router: return None
        return base(h)
    rpc.receipt_for = receipt
    monkeypatch.setattr(tx, 'RECEIPT_WAIT_S', 1)
    assert not action(rpc, db, acct)
    pending = outbox.pending()
    assert len(pending) == 1 and rows(db, kind + '_pending')
    original = pending[0]['raw']
    count = len(rpc.raw)
    assert not tx.recover(rpc)
    assert len(rpc.raw) == count and outbox.pending()[0]['raw'] == original
    # A later confirmed revert (including an on-chain deadline rejection) releases the obligation.
    h = pending[0]['hash']
    rpc.receipts[h] = {'transactionHash':h, 'status':'0x0', 'blockNumber':'0x11', 'logs':[]}
    assert tx.recover(rpc)
    T.reconcile(rpc, db, (kind + '_pending',), C.WALLET)
    assert not outbox.pending() and not rows(db, kind + '_pending')
    assert rows(db, kind + '_failed')
    assert (T.owed_to_burn if kind == 'burn' else T.owed_to_gold)(db) == 6
    rpc.receipt_for = base
    assert action(rpc, db, acct)
    assert len(rows(db, kind)) == 1  # retry spends the obligation exactly once


@pytest.mark.parametrize('valid_until', [0, float('nan'), float('inf')])
def test_invalid_quote_validity_never_signs(db, rpc, acct, live, valid_until):
    signed = []
    guarded = SimpleNamespace(address=acct.address, sign_transaction=signed.append)
    with pytest.raises(RuntimeError, match='quote expired'):
        tx.send_tx(rpc, guarded, C.SWAP_ROUTER_V3, valid_until=valid_until)
    assert not signed and not rpc.raw and not outbox.pending()


def test_quote_expiring_during_rpc_preflight_never_signs(db, rpc, acct, live, monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    call = rpc.call
    def delayed(method, params, **kw):
        result = call(method, params, **kw)
        if method == 'eth_getBalance': clock[0] += 31
        return result
    rpc.call = delayed
    signed = []
    guarded = SimpleNamespace(address=acct.address, sign_transaction=signed.append)
    with pytest.raises(RuntimeError, match='quote expired'):
        tx.send_tx(rpc, guarded, C.SWAP_ROUTER_V3, valid_until=clock[0]+30)
    assert not signed and not rpc.raw and not outbox.pending()
