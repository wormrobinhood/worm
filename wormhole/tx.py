"""Signing and sending on Robinhood Chain. Nothing is signed unless WH_LIVE=1.

One sender at a time: a process lock plus a file lock on DATA_DIR/.txlock are held from the nonce
read to the broadcast, so run.py's marker thread and `python -m wormhole.launch --live` never race
for the same nonce. A broadcast is never retried blindly: the hash is computed locally from the
signed bytes, a node that answers "already known" or "nonce too low" has the transaction already,
and a broadcast that got no answer is checked by hash before the same bytes are sent again."""
import fcntl
import logging
import math
import os
import re
import threading
import time
from contextlib import contextmanager

from eth_utils import to_checksum_address

from . import config as C
from .chain import RpcError
from . import outbox, finality

log = logging.getLogger("wormhole.tx")
_lock = threading.Lock()
KNOWN = ("already known", "nonce too low", "already exists")   # the node has the tx: broadcast succeeded
BROADCAST_TRIES = 3
BROADCAST_RETRY_S = 1.5
RECEIPT_WAIT_S = 120
RECEIPT_POLL_S = 5.0


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
    by hash, so a resend can never make a second transaction). An unresolved transport failure also raises; callers must retain the pending intent."""
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


class ReceiptPending(RpcError):
    """Submitted, but not yet settled under the receipt policy. Never authorize a new retry."""


def wait_receipt(rpc, h, say, *, approval=False, poll_s=None):
    poll_s = RECEIPT_POLL_S if poll_s is None else min(poll_s, RECEIPT_POLL_S)
    for _ in range(int(RECEIPT_WAIT_S / poll_s) if poll_s else RECEIPT_WAIT_S):
        time.sleep(poll_s)
        rc = finality.receipt(rpc, h, approval=approval)
        if rc:
            ok = rc.get("status") == "0x1"
            label = 'confirmed' if finality.policy() == 'included' else ('confirmed approval' if approval else 'finalized')
            say(f"{label} in block {int(rc['blockNumber'], 16)}: {'SUCCESS' if ok else 'REVERTED'}")
            return rc
    raise ReceiptPending(f"no settled receipt for {h} after {RECEIPT_WAIT_S}s; retained pending")


def recover(rpc):
    """Resume only durably prepared submissions. Unknown preparation blocks for operator review."""
    with sender_lock():
        finality.audit(rpc)
        unresolved = False
        for item in outbox.pending():
            if item['state'] == 'preparing':
                raise RuntimeError('transaction preparation interrupted; inspect private outbox before continuing')
            rc = finality.receipt(rpc, item['hash'], approval=item['receipt_mode']=='approval')
            if rc:
                outbox.state(item['hash'], 'settled')
            else:
                unresolved = True
                mined = rpc.call('eth_getTransactionReceipt', [item['hash']])
                if C.LIVE and not mined and not (C.DATA_DIR / 'payments.paused').exists():
                    if item['sender'] != C.WALLET.lower():
                        raise RuntimeError('outbox signer differs from configured wallet; operator review required')
                    broadcast(rpc, item['raw'], item['hash'], log.info)
        return not unresolved


def send_tx(rpc, acct, to, data="0x", value=0, gas=None, gas_floor=None, wait=True, say=None, on_broadcast=None, min_remaining_eth=0.0, fee_limit_eth=None, valid_until=None, poll_s=None):
    """send_tx proper, plus one promise to callers: an exception raised before the key was used carries
    not_signed=True. No transaction can exist then, so an order may be released instead of held for review.
    Anything raised from signing onwards carries no such mark and must be treated as possibly sent."""
    progress = {"signed": False}
    try:
        return _send_tx(rpc, acct, to, data, value, gas, gas_floor, wait, say, on_broadcast, min_remaining_eth,
                        fee_limit_eth, valid_until, poll_s, progress)
    except BaseException as e:
        if not progress["signed"]:
            try:
                e.not_signed = True
            except Exception:
                pass
        raise


def _send_tx(rpc, acct, to, data, value, gas, gas_floor, wait, say, on_broadcast, min_remaining_eth, fee_limit_eth, valid_until, poll_s, progress):
    """Sign, broadcast, wait for the receipt. Returns (hash, receipt), or (hash, None) with wait=False.
    gas: a fixed limit, or None to estimate and add 30%; gas_floor is a minimum either way.
    valid_until: optional local quote-validity bound, checked before RPC work and immediately before signing.
    Callers must also encode an on-chain deadline; recovery preserves the original signed bytes.
    on_broadcast is a legacy name: it now MUST persist the pending record BEFORE submission.
    A failed callback stops submission and leaves the private journal for operator review."""
    say = say or log.info
    def check_freshness():
        if valid_until is not None and (not math.isfinite(valid_until) or time.time() >= valid_until):
            raise RuntimeError('transaction quote expired before signing')
    check_freshness()
    if (C.DATA_DIR / 'payments.paused').exists():
        raise RuntimeError('payments paused by operator')
    if not C.LIVE:
        raise RuntimeError("WH_LIVE=0: refusing to sign. Set WH_LIVE=1 to arm real transactions.")
    if outbox.pending():
        raise RuntimeError('unresolved transaction: reconcile before sending anything new')
    to = to_checksum_address(to)        # config keeps addresses lowercase for comparisons; the signer wants EIP-55
    frm = acct.address
    with sender_lock():
        if outbox.pending():
            raise RuntimeError('unresolved transaction: reconcile before sending anything new')
        finality.audit(rpc)
        nonce = int(rpc.call("eth_getTransactionCount", [frm, "pending"]), 16)
        gas_price = int(int(rpc.call("eth_gasPrice", []), 16) * 1.25)
        if gas is None:
            est = int(rpc.call("eth_estimateGas", [{"from": frm, "to": to, "value": hex(value), "data": data}]), 16)
            gas = int(est * 1.3)
        gas = max(int(gas), int(gas_floor or 0))
        bal = int(rpc.call("eth_getBalance", [frm, "latest"]), 16)
        fee = gas * gas_price / 1e18
        max_fee = float(os.environ.get('WH_MAX_TX_FEE_ETH', '0.002'))
        daily_fee = float(os.environ.get('WH_MAX_DAILY_FEE_ETH', '0.01'))
        max_value = float(os.environ.get('WH_MAX_TX_VALUE_ETH', '0.01'))
        if not (all(math.isfinite(v) for v in (fee, max_fee, daily_fee, max_value))
                and 0 < max_fee <= daily_fee and fee <= max_fee and outbox.fees_today() + fee <= daily_fee
                and 0 <= value / 1e18 <= max_value):
            raise RuntimeError('transaction exceeds configured ETH fee/value limits')
        need = value + gas * gas_price
        if os.environ.get('WH_GAS_REFILL', '0') == '1':
            from .claim_policy import setting
            bootstrap = setting('WH_GAS_BOOTSTRAP_ETH', '0.00005', 0.00001, 0.01)
            if bal < need + math.ceil(bootstrap * 1e18):
                raise RuntimeError('transaction would consume protected gas bootstrap reserve')
        if (not math.isfinite(min_remaining_eth) or min_remaining_eth < 0
                or (min_remaining_eth > 0 and bal < need + math.ceil(min_remaining_eth * 1e18))
                or (fee_limit_eth is not None and (not math.isfinite(fee_limit_eth)
                    or fee_limit_eth <= 0 or fee > fee_limit_eth))):
            raise RuntimeError('operation gas budget or reserve check failed')
        # Claim-specific guard also covers direct/manual claim calls and gas movement after
        # the batch decision. Other transaction types retain their existing spending caps.
        from .chain import call_data
        claim_data = call_data("claimToken(address)", ("address",), (C.USDG,))
        if to.lower() == C.FEE_ESCROW.lower() and data.lower() == claim_data.lower():
            from .claim_policy import setting
            from .prices import eth_usd
            from .treasury import claimable_usdg
            reserve = setting('WH_CLAIM_ETH_RESERVE', '0.0001', 0, 1)
            price = eth_usd(strict=True)
            amount = claimable_usdg(rpc, frm)
            fraction = setting('WH_CLAIM_MAX_GAS_FRACTION', '0.02', 0.0001, 0.1)
            if (price is None or not math.isfinite(float(price)) or price <= 0
                    or not math.isfinite(amount) or amount <= 0
                    or fee * float(price) > amount * fraction
                    or bal < need + math.ceil(reserve * 1e18)):
                raise RuntimeError('claim gas cost or ETH reserve check failed')
        if bal < need:
            raise RuntimeError(f"insufficient ETH: have {bal / 1e18:.6f}, need about {need / 1e18:.6f}")
        tx = {"to": to, "value": value, "data": data, "nonce": nonce, "gasPrice": gas_price, "gas": gas,
              "chainId": C.CHAIN_ID}
        # The operator may pause while this sender waits for the lock or slow RPC preflight.
        if not C.LIVE or (C.DATA_DIR / 'payments.paused').exists():
            raise RuntimeError('payments paused before signing')
        check_freshness()
        progress["signed"] = True           # from here on a transaction may exist: never report "not signed"
        signed = acct.sign_transaction(tx)
        raw = _hex(signed.raw_transaction)
        h_local = _hex(signed.hash)
        say(f"sending tx to {to[:12]}… nonce {nonce}, gas {gas:,} @ {gas_price / 1e9:.3f} gwei, value {value / 1e18:.6f} ETH")
        from .chain import selector
        approval = ((to.lower() == C.USDG and data[:10] == selector('approve(address,uint256)'))
                    or (to.lower() == C.PERMIT2 and data[:10] == selector('approve(address,address,uint160,uint48)')))
        mode = 'included' if finality.policy() == 'included' else ('approval' if approval else 'finalized')
        outbox.record(h_local, frm, raw, fee, mode)
        if on_broadcast:
            on_broadcast(h_local)  # exceptions MUST prevent submission
        outbox.state(h_local, 'ready')
        h = broadcast(rpc, raw, h_local, say)
        say(f"broadcast {h}")
    if not wait:
        return h, None
    rc = wait_receipt(rpc, h, say, approval=approval, poll_s=poll_s)
    if rc.get('status') not in ('0x0', '0x1'):
        raise RuntimeError('invalid receipt status; transaction retained for reconciliation')
    outbox.state(h, 'settled')
    return h, rc
