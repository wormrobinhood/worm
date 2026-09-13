"""Signing and sending on Robinhood Chain. Nothing is signed unless WH_LIVE=1.

One sender at a time: a process lock plus a file lock on DATA_DIR/.txlock are held from the nonce
read to the broadcast, so run.py's marker thread and `python -m wormhole.launch --live` never race
for the same nonce. A broadcast is never retried blindly: the hash is computed locally from the
signed bytes, a node that answers "already known" or "nonce too low" has the transaction already,
and a broadcast that got no answer is checked by hash before the same bytes are sent again."""
import fcntl
import logging
import re
import threading
import time
from contextlib import contextmanager

from eth_utils import to_checksum_address

from . import config as C
from .chain import RpcError

log = logging.getLogger("wormhole.tx")
_lock = threading.Lock()
KNOWN = ("already known", "nonce too low", "already exists")   # the node has the tx: broadcast succeeded
BROADCAST_TRIES = 3
BROADCAST_RETRY_S = 1.5
RECEIPT_WAIT_S = 120
RECEIPT_POLL_S = 1.0


def _hex(b):
    h = b.hex()
    return h if h.startswith("0x") else "0x" + h


@contextmanager
def sender_lock():
    """Serialises every sender in this process and across processes that share DATA_DIR."""
    with _lock:
        C.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(C.DATA_DIR / ".txlock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def _node_answered(msg):
    """True when the error is the node's verdict on the transaction. chain.Rpc prefixes what the node
    said with "<method>: "; a network failure carries the exception text instead. A rate limit or a
    malformed body gets the prefix too but says nothing about the transaction."""
    m = msg.lower()
    if re.search(r"\b429\b", m) or "too many requests" in m or "rate limit" in m or "malformed body" in m:
        return False
    return "eth_sendrawtransaction: " in m


def broadcast(rpc, raw, h_local, say):
    """Send the signed bytes and return the hash. A node that already has the transaction says so; a
    node that gave no answer is asked by hash before the same bytes go out again (the node de-duplicates
    by hash, so a resend can never make a second transaction). Only a real rejection raises."""
    last = None
    for attempt in range(BROADCAST_TRIES):
        try:
            h = rpc.call("eth_sendRawTransaction", [raw], retries=1)
        except RpcError as e:
            msg = str(e)
            if any(s in msg.lower() for s in KNOWN):
                if "nonce too low" in msg.lower() and not rpc.call("eth_getTransactionByHash", [h_local]):
                    raise RpcError(f"nonce already used by another transaction, not this one: {msg[:120]}")
                return h_local                      # the node holds these exact bytes already
            if _node_answered(msg):
                raise
            last = e
            say(f"broadcast attempt {attempt + 1} got no answer ({msg[:80]}); checking by hash")
            if rpc.call("eth_getTransactionByHash", [h_local]):
                return h_local
            time.sleep(BROADCAST_RETRY_S)
            continue
        if str(h).lower() != h_local.lower():
            raise RpcError(f"node returned hash {h} for a transaction hashed locally as {h_local}")
        return h
    raise RpcError(f"broadcast of {h_local} unconfirmed after {BROADCAST_TRIES} attempts: {last}")


def wait_receipt(rpc, h, say):
    for _ in range(int(RECEIPT_WAIT_S / RECEIPT_POLL_S) if RECEIPT_POLL_S else RECEIPT_WAIT_S):
        time.sleep(RECEIPT_POLL_S)
        rc = rpc.call("eth_getTransactionReceipt", [h])
        if rc:
            ok = rc.get("status") == "0x1"
            say(f"mined in block {int(rc['blockNumber'], 16)}: {'SUCCESS' if ok else 'REVERTED'}")
            return rc
    raise RpcError(f"no receipt for {h} after {RECEIPT_WAIT_S}s")


def send_tx(rpc, acct, to, data="0x", value=0, gas=None, gas_floor=None, wait=True, say=None, on_broadcast=None):
    """Sign, broadcast, wait for the receipt. Returns (hash, receipt), or (hash, None) with wait=False.
    gas: a fixed limit, or None to estimate and add 30%; gas_floor is a minimum either way.
    on_broadcast(hash) runs as soon as the node has the transaction, before the wait: callers use it
    to write a pending ledger row so a crash or a lost receipt can be reconciled later."""
    say = say or log.info
    if not C.LIVE:
        raise RuntimeError("WH_LIVE=0: refusing to sign. Set WH_LIVE=1 to arm real transactions.")
    to = to_checksum_address(to)        # config keeps addresses lowercase for comparisons; the signer wants EIP-55
    frm = acct.address
    with sender_lock():
        nonce = int(rpc.call("eth_getTransactionCount", [frm, "pending"]), 16)
        gas_price = int(int(rpc.call("eth_gasPrice", []), 16) * 1.25)
        if gas is None:
            est = int(rpc.call("eth_estimateGas", [{"from": frm, "to": to, "value": hex(value), "data": data}]), 16)
            gas = int(est * 1.3)
        gas = max(int(gas), int(gas_floor or 0))
        bal = int(rpc.call("eth_getBalance", [frm, "latest"]), 16)
        need = value + gas * gas_price
        if bal < need:
            raise RuntimeError(f"insufficient ETH: have {bal / 1e18:.6f}, need about {need / 1e18:.6f}")
        tx = {"to": to, "value": value, "data": data, "nonce": nonce, "gasPrice": gas_price, "gas": gas,
              "chainId": C.CHAIN_ID}
        signed = acct.sign_transaction(tx)
        raw = _hex(signed.raw_transaction)
        h_local = _hex(signed.hash)
        say(f"sending tx to {to[:12]}… nonce {nonce}, gas {gas:,} @ {gas_price / 1e9:.3f} gwei, value {value / 1e18:.6f} ETH")
        h = broadcast(rpc, raw, h_local, say)
        say(f"broadcast {h}")
        if on_broadcast:
            try:
                on_broadcast(h)
            except Exception as e:              # the transaction is out: never let a bookkeeping failure hide it
                log.error("broadcast callback failed for %s: %s", h, str(e)[:200])
                say(f"the ledger write for {h} failed ({str(e)[:80]}); the receipt will rebuild it")
    if not wait:
        return h, None
    return h, wait_receipt(rpc, h, say)
