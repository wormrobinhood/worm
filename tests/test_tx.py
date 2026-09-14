"""send_tx: the LIVE guard, checksummed destinations, gas, the idempotent broadcast and the sender lock."""
import threading

import pytest
from eth_account import Account

from fakes import decode_tx, tx_hash
from wormhole import config as C, tx
from wormhole.chain import RpcError


def sends(rpc):
    return [p[0] for m, p in rpc.calls if m == "eth_sendRawTransaction"]


def test_live_guard_raises_before_any_call(rpc, acct):
    with pytest.raises(RuntimeError, match="WH_LIVE=0"):
        tx.send_tx(rpc, acct, C.FACTORY)
    assert rpc.calls == []


def test_lowercase_destination_signs_and_recovers(rpc, acct, live):
    h, rc = tx.send_tx(rpc, acct, C.FACTORY, data="0xabcd", value=5)
    raw = rpc.raw[0]
    t = decode_tx(raw)
    assert t["to"] == C.FACTORY and t["nonce"] == 7 and t["value"] == 5 and t["data"] == b"\xab\xcd"
    assert t["gasPrice"] == int(90_600_000 * 1.25)
    assert t["v"] in (9361, 9362)                                    # EIP-155 for chain 4663
    assert Account.recover_transaction(raw) == acct.address
    assert h == tx_hash(raw) and rc["status"] == "0x1"


def test_every_lowercase_address_in_config_signs(rpc, acct, live):
    dests = (C.FACTORY, C.FEE_ESCROW, C.USDG, C.UNIVERSAL_ROUTER)
    assert all(d == d.lower() for d in dests)
    for d in dests:
        tx.send_tx(rpc, acct, d)
    assert [decode_tx(r)["to"] for r in rpc.raw] == list(dests)


def test_gas_is_estimated_with_margin_and_floored(rpc, acct, live):
    tx.send_tx(rpc, acct, C.FACTORY)
    assert decode_tx(rpc.raw[-1])["gas"] == 130_000                  # 100k estimate + 30%
    tx.send_tx(rpc, acct, C.FACTORY, gas_floor=4_500_000)
    assert decode_tx(rpc.raw[-1])["gas"] == 4_500_000
    rpc.estimate = 5_000_000
    tx.send_tx(rpc, acct, C.FACTORY, gas=None, gas_floor=4_500_000)
    assert decode_tx(rpc.raw[-1])["gas"] == 6_500_000
    n = len(rpc.calls)
    tx.send_tx(rpc, acct, C.FACTORY, gas=200_000, gas_floor=150_000)
    assert decode_tx(rpc.raw[-1])["gas"] == 200_000
    assert "eth_estimateGas" not in [m for m, _ in rpc.calls[n:]]


def test_insufficient_eth_refuses_before_signing(rpc, acct, live):
    rpc.balance = 1000
    with pytest.raises(RuntimeError, match="insufficient ETH"):
        tx.send_tx(rpc, acct, C.FACTORY)
    assert rpc.raw == [] and sends(rpc) == []


def test_dropped_socket_then_already_known_is_a_success(rpc, acct, live):
    rpc.script = ["network", "already known"]
    seen = []
    h, rc = tx.send_tx(rpc, acct, C.FACTORY, on_broadcast=seen.append)
    s = sends(rpc)
    assert len(s) == 2 and s[0] == s[1]                              # the same bytes again, never a new tx
    assert ("eth_getTransactionByHash", [h]) in rpc.calls
    assert seen == [h] and h == tx_hash(s[0]) and rc["status"] == "0x1"


def test_nonce_too_low_is_only_a_success_when_the_node_holds_our_hash(rpc, acct, live):
    rpc.script = ["nonce too low"]                                    # the fake delivers the tx on this verdict
    h, rc = tx.send_tx(rpc, acct, C.FACTORY)
    assert rc["status"] == "0x1"
    rpc.script = ["nonce too low"]
    rpc._deliver = lambda raw: None                                   # another sender used the nonce: our bytes are not in
    with pytest.raises(Exception, match="nonce already used"):
        tx.send_tx(rpc, acct, C.FACTORY)


def test_a_failing_pending_callback_prevents_submission(rpc, acct, live):
    def boom(h):
        raise RuntimeError("disk full")
    with pytest.raises(RuntimeError, match='disk full'):
        tx.send_tx(rpc, acct, C.FACTORY, on_broadcast=boom)
    assert len(sends(rpc)) == 0              # persistence failure must prevent submission


def test_delivered_but_unanswered_is_found_by_hash(rpc, acct, live):
    rpc.script = ["delivered-network"]
    h, rc = tx.send_tx(rpc, acct, C.FACTORY)
    assert len(sends(rpc)) == 1 and rc["status"] == "0x1" and h == tx_hash(rpc.raw[0])


def test_rate_limit_is_not_a_verdict(rpc, acct, live):
    rpc.script = ["429", "ok"]
    seen = []
    h, rc = tx.send_tx(rpc, acct, C.FACTORY, on_broadcast=seen.append)
    s = sends(rpc)
    assert len(s) == 2 and s[0] == s[1] and seen == [h] and rc["status"] == "0x1"


def test_node_rejection_keeps_the_prepared_record(rpc, acct, live):
    rpc.script = ["insufficient funds for gas * price + value"]
    seen = []
    with pytest.raises(RpcError, match="insufficient funds"):
        tx.send_tx(rpc, acct, C.FACTORY, on_broadcast=seen.append)
    assert len(seen) == 1 and rpc.raw == [] and len(sends(rpc)) == 1


def test_no_answer_keeps_pending_record_for_recovery(rpc, acct, live):
    rpc.script = ["network"] * 3
    seen = []
    with pytest.raises(RpcError, match="unconfirmed"):
        tx.send_tx(rpc, acct, C.FACTORY, on_broadcast=seen.append)
    assert len(seen) == 1 and len(sends(rpc)) == 3


def test_hash_mismatch_is_an_error(rpc, acct, live):
    rpc.script = ["wrong hash"]
    with pytest.raises(RpcError, match="hashed locally"):
        tx.send_tx(rpc, acct, C.FACTORY)


def test_on_broadcast_runs_before_the_receipt_wait(rpc, acct, live):
    order = []
    orig = rpc.call

    def spy(method, params, retries=4):
        if method == "eth_getTransactionReceipt":
            order.append("receipt")
        return orig(method, params, retries)

    rpc.call = spy
    tx.send_tx(rpc, acct, C.FACTORY, on_broadcast=lambda h: order.append("pending"))
    assert order[0] == "pending" and "receipt" in order


def test_no_receipt_raises_after_the_wait(rpc, acct, live, monkeypatch):
    monkeypatch.setattr(tx, "RECEIPT_WAIT_S", 3)
    rpc.receipt_for = lambda h: None
    with pytest.raises(RpcError, match="no settled receipt"):
        tx.send_tx(rpc, acct, C.FACTORY)


def test_wait_false_returns_right_after_broadcast(rpc, acct, live):
    h, rc = tx.send_tx(rpc, acct, C.FACTORY, wait=False)
    assert rc is None and h == tx_hash(rpc.raw[0])
    assert "eth_getTransactionReceipt" not in [m for m, _ in rpc.calls]


def test_concurrent_senders_get_distinct_nonces(rpc, acct, live):
    rpc.count_delay = 0.05
    errors = []

    def go():
        try:
            tx.send_tx(rpc, acct, C.FACTORY)
        except Exception as e:                   # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all('unresolved transaction' in str(e) for e in errors)
    nonces = [decode_tx(r)['nonce'] for r in rpc.raw]
    assert len(nonces) >= 1 and len(nonces) == len(set(nonces))
    assert (C.DATA_DIR / ".txlock").exists()


def test_pause_during_rpc_preflight_prevents_signing(rpc, acct, live):
    from types import SimpleNamespace
    from wormhole import outbox
    original=rpc.call
    signed=[]
    def call(method, params, **kwargs):
        result=original(method, params, **kwargs)
        if method=='eth_getBalance':
            (C.DATA_DIR/'payments.paused').touch()
        return result
    rpc.call=call
    guarded=SimpleNamespace(address=acct.address, sign_transaction=lambda value:signed.append(value))
    with pytest.raises(RuntimeError, match='paused before signing'):
        tx.send_tx(rpc, guarded, C.FACTORY)
    assert not signed and not rpc.raw and not outbox.pending()
