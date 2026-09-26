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


def recover(rpc, db=None):
    """Settle, resend or resolve every unsettled intent from chain evidence (wormhole/intents.py), under the
    sender lock. Returns True when nothing is left unsettled.

    A 'ready' intent the node has never seen is resent as the same signed bytes. An intent the evidence
    proves will never execute (abandoned before broadcast, or dropped because our own settled transaction
    used its nonce) is closed, and its caller's bookkeeping is released by the caller's own reconcile. One
    the evidence cannot settle is parked: it keeps blocking new sends and carries a note that private health
    shows. A resend the node refuses is parked the same way instead of raising on every cycle; the way out is
    the operator's cancel (scripts/resolve-intent.py --cancel). Without the bookkeeping database (db=None:
    the launch paths) an interrupted preparation still stops everything, as before."""
    from . import intents
    with sender_lock():
        finality.audit(rpc)
        unresolved = False
        for item in outbox.pending():
            h = item['hash']
            if item['state'] == 'preparing' and db is None:
                raise RuntimeError('transaction preparation interrupted; inspect private outbox before continuing')
            verdict, why = intents.assess(rpc, item, db)
            if verdict == 'settled':
                outbox.state(h, 'settled')
                continue
            if verdict in ('abandoned', 'dropped'):
                if outbox.resolve(h, verdict, why):
                    log.warning('transaction %s %s: %s', h, verdict, why)
                continue
            unresolved = True
            if verdict == 'review':
                if item['note'] != why:
                    log.error('transaction %s parked for operator review: %s', h, why)
                outbox.note(h, why)
                continue
            if verdict == 'mined' or not C.LIVE or (C.DATA_DIR / 'payments.paused').exists():
                if item['note'] and verdict == 'mined':
                    outbox.note(h, '')
                continue
            if item['sender'] != C.WALLET.lower():
                raise RuntimeError('outbox signer differs from configured wallet; operator review required')
            try:
                broadcast(rpc, item['raw'], h, log.info)
                if item['note']:
                    outbox.note(h, '')
            except RpcError as e:
                # The node refused these exact bytes (nonce taken, underpriced, out of gas money). Resending
                # them every cycle cannot help: keep the intent, say why, and wait for evidence or the operator.
                why = 'resend refused by the node: ' + _refusal(str(e))
                if item['note'] != why:
                    log.error('transaction %s parked: %s', h, why)
                outbox.note(h, why)
        return not unresolved


def _refusal(msg):
    """A short, secret-free name for a node's refusal: provider URLs and keys never reach the journal note."""
    m = msg.lower()
    for key, text in (('nonce already used', 'nonce already used by another transaction'),
                      ('nonce too low', 'nonce too low'), ('underpriced', 'gas price too low to replace or enter the pool'),
                      ('fee cap', 'gas price below the base fee'), ('base fee', 'gas price below the base fee'),
                      ('insufficient funds', 'insufficient ETH for gas'), ('unconfirmed after', 'no answer from the node')):
        if key in m:
            return text
    return 'rejected (see private logs)'


def cancel(rpc, acct, h, say=None):
    """Operator-only (scripts/resolve-intent.py --cancel): replace an unsettled intent that cannot go through
    (refused resend, stuck under the base fee) with a zero-value transfer to our own wallet at the same nonce,
    priced above both the original and the current gas price. Whichever of the two is mined, recovery then
    proves the other never can be (its nonce used by our own known transaction) and closes it; the caller's
    bookkeeping follows from the receipt. Refuses whenever the original may still be settled normally."""
    say = say or log.info
    if not C.LIVE:
        raise RuntimeError("WH_LIVE=0: refusing to sign. Set WH_LIVE=1 to arm real transactions.")
    if (C.DATA_DIR / 'payments.paused').exists():
        raise RuntimeError('payments paused by operator')
    frm = acct.address
    with sender_lock():
        finality.audit(rpc)
        item = outbox.get(h)
        if not item or item['state'] in outbox.DONE:
            raise RuntimeError('no unsettled intent with that hash')
        if item['sender'] != frm.lower() or frm.lower() != C.WALLET.lower():
            raise RuntimeError('intent signer differs from this key or the configured wallet')
        if rpc.call('eth_getTransactionReceipt', [h]):
            raise RuntimeError('already mined: recovery settles it from its receipt')
        nonce, old_price = outbox.fields(item['raw'])
        if int(rpc.call('eth_getTransactionCount', [frm, 'latest']), 16) > nonce:
            raise RuntimeError('its nonce is already used: recovery resolves it from the evidence')
        if any(o['hash'] != h and o['state'] not in outbox.DONE and outbox.fields(o['raw'])[0] == nonce
               and o['sender'] == frm.lower() for o in outbox.pending()):
            raise RuntimeError('a replacement for this nonce is already in flight')
        # A replacement must outbid the original by at least 10% to enter a node's pool.
        gas_price = max(int(int(rpc.call("eth_gasPrice", []), 16) * 1.25), math.ceil(old_price * 1.25) + 1)
        est = int(rpc.call("eth_estimateGas", [{"from": frm, "to": frm, "value": "0x0", "data": "0x"}]), 16)
        gas = int(est * 1.3)
        fee = gas * gas_price / 1e18
        max_fee = float(os.environ.get('WH_MAX_TX_FEE_ETH', '0.002'))
        daily_fee = float(os.environ.get('WH_MAX_DAILY_FEE_ETH', '0.01'))
        if not (math.isfinite(fee) and 0 < max_fee <= daily_fee and fee <= max_fee and outbox.fees_today() + fee <= daily_fee):
            raise RuntimeError('transaction exceeds configured ETH fee/value limits')
        if int(rpc.call("eth_getBalance", [frm, "latest"]), 16) < gas * gas_price:
            raise RuntimeError('insufficient ETH for the cancel')
        signed = acct.sign_transaction({"to": to_checksum_address(frm), "value": 0, "data": "0x", "nonce": nonce,
                                        "gasPrice": gas_price, "gas": gas, "chainId": C.CHAIN_ID})
        raw, h2 = _hex(signed.raw_transaction), _hex(signed.hash)
        mode = 'included' if finality.policy() == 'included' else 'finalized'
        outbox.record(h2, frm, raw, fee, mode)
        outbox.note(h2, f'cancels {h}')
        outbox.state(h2, 'ready')
        say(f"cancel {h2} for {h}: nonce {nonce} @ {gas_price / 1e9:.3f} gwei")
        return broadcast(rpc, raw, h2, say)


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
