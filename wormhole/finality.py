"""Verified inclusion by default, with optional finalized settlement.

All evidence is from the configured RPC, not an independent consensus proof. A missing RPC
answer never downgrades this policy. A changed accepted anchor requires operator review.
"""
import fcntl
import functools
import threading
import json
import os
import re

from . import config as C, outbox
from .chain import RpcError

APPROVAL_CONFIRMATIONS = 20
_lock = threading.Lock()


def policy():
    value = os.environ.get('WH_TX_CONFIRMATION', 'included').strip().lower()
    if value not in ('included', 'finalized'):
        raise RpcError('invalid WH_TX_CONFIRMATION: expected included or finalized')
    return value


def _serialized(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        with _lock:
            C.DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(C.DATA_DIR / '.finalitylock', 'a') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    return fn(*args, **kwargs)
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
    return wrapped


HASH = re.compile(r'^0x[0-9a-fA-F]{64}$')


def _number(value):
    if not isinstance(value, str) or not value.startswith('0x'):
        raise RpcError('finality block number unavailable')
    try:
        number = int(value, 16)
    except ValueError as exc:
        raise RpcError('invalid finality block number') from exc
    if number < 0:
        raise RpcError('invalid finality block number')
    return number


def _block(rpc, tag):
    block = rpc.call('eth_getBlockByNumber', [tag, False])
    if not isinstance(block, dict) or not HASH.fullmatch(block.get('hash') or ''):
        raise RpcError('canonical block evidence unavailable')
    _number(block.get('number'))
    if tag.startswith('0x') and _number(block['number']) != _number(tag):
        raise RpcError('canonical block number mismatch')
    return block


def _pause(reason):
    # The journal latch survives deletion of payments.paused: recovery needs explicit private review.
    outbox.finality_value('incident', reason)
    C.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (C.DATA_DIR / 'payments.paused').touch(mode=0o600)
    raise RpcError('chain finality changed; payments paused for operator review')


def _audit(rpc):
    """Verify the finalized checkpoint and all receipts accepted before finalization."""
    policy()  # Invalid configuration must also block signing before any receipt exists.
    if outbox.finality_value('incident'):
        raise RpcError('chain finality incident requires operator review')
    head = _block(rpc, 'finalized')
    height = _number(head['number'])
    previous = outbox.finality_value('checkpoint')
    if previous:
        old = json.loads(previous)
        if height < old['number']:
            raise RpcError('finalized head regressed; waiting for consistent RPC evidence')
        canonical = _block(rpc, hex(old['number']))
        if canonical['hash'].lower() != old['hash']:
            _pause('finalized_checkpoint_changed')
    canonical_head = _block(rpc, head['number'])
    if canonical_head['hash'].lower() != head['hash'].lower():
        raise RpcError('finalized head disagrees with canonical block')
    for anchor in outbox.provisional_anchors():
        canonical = _block(rpc, hex(anchor['block_number']))
        rc = rpc.call('eth_getTransactionReceipt', [anchor['hash']])
        if (canonical['hash'].lower() != anchor['block_hash'] or not rc
                or str(rc.get('transactionHash', '')).lower() != anchor['hash']
                or str(rc.get('blockHash', '')).lower() != anchor['block_hash']
                or rc.get('status') != anchor['status']
                or _number(rc.get('blockNumber')) != anchor['block_number']):
            _pause('accepted_receipt_changed')
        if anchor['block_number'] <= height:
            outbox.anchor(rc, True)
    outbox.finality_value('checkpoint', json.dumps({'number':height, 'hash':head['hash'].lower()}))
    return height


@_serialized
def audit(rpc):
    return _audit(rpc)


@_serialized
def receipt(rpc, h, *, approval=False):
    """Return a canonical receipt only after its required settlement boundary, otherwise None."""
    finalized_height = _audit(rpc)
    rc = rpc.call('eth_getTransactionReceipt', [h])
    if not rc:
        return None
    if (rc.get('status') not in ('0x0', '0x1')
            or str(rc.get('transactionHash', '')).lower() != h.lower()
            or not HASH.fullmatch(rc.get('blockHash') or '')):
        raise RpcError('receipt identity or status unavailable; retained pending')
    number = _number(rc.get('blockNumber'))
    canonical = _block(rpc, hex(number))
    if canonical['hash'].lower() != rc['blockHash'].lower():
        return None   # a not-yet-accepted payment can reappear in a different canonical block
    is_finalized = number <= finalized_height
    if not is_finalized:
        included = policy() == 'included'
        if not included and not approval:
            return None
        latest = _block(rpc, 'latest')
        depth = 1 if included else APPROVAL_CONFIRMATIONS
        if _number(latest['number']) - number + 1 < depth:
            return None
    # The node can change between reads; do not return evidence from two different inclusions.
    again = rpc.call('eth_getTransactionReceipt', [h])
    if not again or any(again.get(k) != rc.get(k) for k in ('transactionHash','blockHash','blockNumber','status','logs')):
        return None
    outbox.anchor(rc, is_finalized)
    return rc
