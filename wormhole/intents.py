"""What the chain says about an unsettled transaction intent, for tx.recover and scripts/resolve-intent.py.

Only evidence resolves an intent; nothing here guesses. The verdicts:
  settled    a receipt under the confirmation policy exists (success or revert: the caller's settle decides)
  mined      the node has a receipt or holds the transaction, but it is not settled yet: wait
  unseen     not mined, its nonce still open: recovery may resend the same signed bytes
  abandoned  still 'preparing' (signed, never handed to a node: tx.py writes 'ready' before any broadcast),
             and the node does not hold it
  dropped    not mined and it never can be: its nonce was used by another of OUR journaled transactions,
             whose receipt is settled (a cancel, or the original a cancel tried to replace)
  review     the evidence contradicts the journal, or the bookkeeping is not the treasury's own: the intent
             stays unsettled, blocks new sends, and carries the reason for the operator
Automatic 'abandoned'/'dropped' is limited to intents whose bookkeeping the treasury releases itself (ledger,
fee sweeps, gas refills) or that have none (an approval); a launch or a live trade is left to the operator.
The sender lock must be held by the caller: under it no other sender is between 'preparing' and broadcast."""
from . import finality, outbox

AUTO = ('ledger', 'fee_sweeps', 'gas_refills', None)


def _has(db, table):
    return bool(db.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)))


def owner(db, h):
    """Which bookkeeping waits on this hash: 'ledger', 'fee_sweeps', 'gas_refills', 'trades', 'launch', or None."""
    for table in ('ledger', 'fee_sweeps', 'gas_refills', 'trades'):
        if _has(db, table) and db.one(f"SELECT 1 FROM {table} WHERE tx=?", (h,)):
            return table
    if h and (db.meta_get('launch_pending') == h or h in (db.meta_get('launch_allocation') or '')):
        return 'launch'
    return None


def _known(rpc, h):
    return bool(rpc.call('eth_getTransactionByHash', [h]) or rpc.call('eth_getTransactionReceipt', [h]))


def consumer(rpc, item, nonce):
    """Another of our journaled transactions from the same sender at the same nonce with a settled receipt:
    proof that `item` can never be mined. None when there is no such transaction."""
    for other in outbox.everything():
        if other['hash'] == item['hash'] or other['sender'] != item['sender']:
            continue
        try:
            if outbox.fields(other['raw'])[0] != nonce:
                continue
        except Exception:
            continue
        if finality.receipt(rpc, other['hash'], approval=other['receipt_mode'] == 'approval'):
            return other['hash']
    return None


def never_executes(rpc, item):
    """The chain evidence alone, without the two judgments assess() leaves to the operator (a nonce used by a
    transaction outside this journal; bookkeeping outside the treasury): (end, why) when this intent can never
    execute, None when it still might. Used only by the operator's --accept."""
    h = item['hash']
    if _known(rpc, h):
        return None
    if item['state'] == 'preparing':
        return 'abandoned', 'operator accepted: interrupted before broadcast; the node does not hold it'
    nonce = outbox.fields(item['raw'])[0]
    if int(rpc.call('eth_getTransactionCount', [item['sender'], 'latest']), 16) > nonce and not _known(rpc, h):
        return 'dropped', 'operator accepted: never mined; its nonce was used by another transaction'
    return None


def assess(rpc, item, db=None):
    """(verdict, reason) for one unsettled intent. Reads only; the caller applies the verdict."""
    h = item['hash']
    if item['state'] == 'preparing':
        if _known(rpc, h):
            return 'review', 'signed but never handed to broadcast, yet the node holds it: verify its bookkeeping'
        who = owner(db, h) if db is not None else 'unknown'
        if who not in AUTO:
            return 'review', f'interrupted before broadcast; its {who} bookkeeping needs the operator'
        return 'abandoned', 'interrupted before broadcast; the node does not hold it'
    if finality.receipt(rpc, h, approval=item['receipt_mode'] == 'approval'):
        return 'settled', ''
    if rpc.call('eth_getTransactionReceipt', [h]):
        return 'mined', ''
    nonce = outbox.fields(item['raw'])[0]
    used = int(rpc.call('eth_getTransactionCount', [item['sender'], 'latest']), 16)
    if used <= nonce:
        return 'unseen', ''
    # The nonce is used. Ask again for our own hash after reading the count: a node that has just mined it
    # must not make our own transaction look replaced.
    if _known(rpc, h):
        return 'mined', ''
    other = consumer(rpc, item, nonce)
    if not other:
        return 'review', ('its nonce was used by a transaction that is not in this journal: another signer or '
                          'the key in use elsewhere; it can never be mined')
    who = owner(db, h) if db is not None else 'unknown'
    if who not in AUTO:
        return 'review', f'never mined: its nonce was used by {other}; its {who} bookkeeping needs the operator'
    return 'dropped', f'never mined: its nonce was used by {other}'
