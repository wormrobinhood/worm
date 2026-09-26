#!/usr/bin/env python3
"""Show what chain evidence says about every unsettled payment transaction, and act on it only when asked.

Dry run by default: nothing is resolved, signed or sent (the finality checks keep their receipt anchors, as
the service does on every read). Run it on the host that owns the data volume, with the
same environment as the service (it reads WH_DATA_DIR, WH_RPC, WH_WALLET).

  resolve-intent.py                       list unsettled intents, their evidence and what recovery will do,
                                          and payments whose receipt lacks the evidence their bookkeeping needs
  resolve-intent.py --apply               close now what the evidence settles (abandoned / dropped), exactly as
                                          the service's next cycle would; bookkeeping follows on that cycle
  resolve-intent.py --accept HASH --apply close a parked intent that the evidence shows can never execute, after
                                          YOU have explained the judgment left to you (a nonce used by a
                                          transaction outside the journal; a launch or trade that owns it)
  resolve-intent.py --cancel HASH --apply sign and send a zero-value transfer to the wallet at HASH's nonce, priced
                                          to replace it (needs WH_LIVE=1, no pause marker); recovery then closes
                                          whichever of the two is not mined

Nothing here guesses: an intent whose outcome the chain cannot yet show stays as it is. Signed bytes are never
printed."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ACTIONS = {'settled': 'mark settled (the payment is reconciled from its receipt)',
           'mined': 'wait for its receipt to settle', 'unseen': 'resend the same signed bytes (WH_LIVE=1)',
           'abandoned': 'close as abandoned; its pending payment becomes failed and is owed again',
           'dropped': 'close as dropped; its pending payment becomes failed and is owed again',
           'review': 'nothing automatic: operator decision (--accept or --cancel)'}


def report(rpc, db, out=print):
    from wormhole import intents, outbox
    items = outbox.pending()
    if not items:
        out('No unsettled transactions in the journal.')
    for item in items:
        try:
            verdict, why = intents.assess(rpc, item, db)
        except Exception as e:
            verdict, why = 'review', f'evidence unavailable ({type(e).__name__}); try again'
        try:
            nonce, price = outbox.fields(item['raw'])
        except Exception:
            nonce, price = '?', 0
        out(f"{item['hash']}  {item['state']}  nonce {nonce}  {price / 1e9:.4f} gwei  "
            f"age {(time.time() - item['ts']) / 3600:.1f} h  bookkeeping: {intents.owner(db, item['hash']) or 'none'}")
        if item['note']:
            out(f"    parked: {item['note']}")
        out(f"    evidence: {verdict}{': ' + why if why else ''}")
        out(f"    recovery will: {ACTIONS[verdict]}")
    review = json.loads(db.meta_get('payment_review') or '{}')
    for h, v in review.items():
        out(f"{h}  ledger pending  {v.get('reason', '')}  (funds stay reserved; verify the receipt privately)")
    return items


def apply(rpc, db, out=print):
    from wormhole import intents, outbox, tx
    done = 0
    with tx.sender_lock():
        for item in outbox.pending():
            verdict, why = intents.assess(rpc, item, db)
            if verdict in ('abandoned', 'dropped') and outbox.resolve(item['hash'], verdict, why):
                out(f"{item['hash']}: {verdict} ({why})")
                done += 1
    out(f"{done} intent(s) closed. The service's next treasury cycle releases their pending payments.")
    return done


def accept(rpc, h, out=print):
    from wormhole import intents, outbox, tx
    with tx.sender_lock():
        item = outbox.get(h)
        if not item or item['state'] in outbox.DONE:
            out('No unsettled intent with that hash.')
            return False
        end = intents.never_executes(rpc, item)
        if not end:
            out('The chain does not show that it can never execute. Nothing changed.')
            return False
        outbox.resolve(h, *end)
        out(f"{h}: {end[0]} ({end[1]}). A launch or trade that references it needs its own review.")
        return True


def main(argv=None, rpc=None, db=None, acct=None, out=print):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--apply', action='store_true', help='write (and for --cancel, sign and send); default: dry run')
    g = p.add_mutually_exclusive_group()
    g.add_argument('--accept', metavar='HASH')
    g.add_argument('--cancel', metavar='HASH')
    args = p.parse_args(argv)
    from wormhole import config as C, outbox
    if rpc is None:
        from wormhole.chain import Rpc
        rpc = Rpc(C.RPC)
    if db is None:
        from wormhole.db import DB
        db = DB(C.DB_PATH)
    if args.accept or args.cancel:
        h = (args.accept or args.cancel).lower()
        item = outbox.get(h)
        if not item:
            out('No intent with that hash in the journal.')
            return 1
        if not args.apply:
            report(rpc, db, out)
            out(f"dry run: would {'close' if args.accept else 'cancel'} {h} if the evidence allows. Add --apply.")
            return 0
        if args.accept:
            return 0 if accept(rpc, h, out) else 1
        if acct is None:
            if not C.SECRET:
                out('No signing key configured.')
                return 1
            from eth_account import Account
            acct = Account.from_key(C.SECRET)
        from wormhole import tx
        out(f"cancel sent: {tx.cancel(rpc, acct, h, out)}. The service settles it; rerun this to watch.")
        return 0
    report(rpc, db, out)
    if args.apply:
        apply(rpc, db, out)
    else:
        out('dry run: nothing written. Add --apply to close what the evidence settles.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
