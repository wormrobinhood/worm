"""Private durable transaction journal. Never exposed by the public state endpoint.

An intent is 'preparing' from signing until its caller's bookkeeping is written, 'ready' once it may be
broadcast, and 'settled' once a receipt under the confirmation policy exists. Two more end states are only
ever written from chain evidence (tx.recover, scripts/resolve-intent.py): 'abandoned', a prepared intent
that was never handed to a node and that the node does not hold, and 'dropped', an intent that can never be
mined because its nonce was used by another of our own journaled transactions, settled. A 'note' says why
an unsettled intent is waiting for the operator; it is cleared once the evidence resolves."""
import os
import sqlite3
import time
from contextlib import contextmanager

import rlp

from . import config as C

DONE = ('settled', 'abandoned', 'dropped')     # nothing more can happen to these on chain

@contextmanager
def journal():
    C.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = C.DATA_DIR / 'transactions.sqlite3'
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS intents(hash TEXT PRIMARY KEY, sender TEXT, raw TEXT, state TEXT, ts REAL, fee REAL)')
        if 'receipt_mode' not in {r[1] for r in db.execute('PRAGMA table_info(intents)')}:
            db.execute("ALTER TABLE intents ADD COLUMN receipt_mode TEXT NOT NULL DEFAULT 'finalized'")
        if 'note' not in {r[1] for r in db.execute('PRAGMA table_info(intents)')}:
            db.execute("ALTER TABLE intents ADD COLUMN note TEXT NOT NULL DEFAULT ''")
        db.execute('CREATE TABLE IF NOT EXISTS receipt_anchors(hash TEXT PRIMARY KEY, block_number INTEGER, block_hash TEXT, status TEXT, finalized INTEGER)')
        db.execute('CREATE TABLE IF NOT EXISTS finality_meta(key TEXT PRIMARY KEY, value TEXT)')
        db.commit()
        yield db
    finally:
        db.close()

def record(h, sender, raw, fee, receipt_mode='finalized'):
    with journal() as db, db:
        # Signing is deterministic: a payment rebuilt with the same nonce, gas and data after its intent was
        # abandoned (never broadcast, not held by the node) has the same hash and the same bytes. Take that intent
        # up again instead of failing on the key every cycle; any other clash still raises.
        old = db.execute('SELECT state, raw FROM intents WHERE hash=?', (h,)).fetchone()
        if old and old['state'] == 'abandoned' and old['raw'] == raw:
            db.execute("UPDATE intents SET state='preparing', ts=?, fee=?, receipt_mode=?, note='' WHERE hash=?",
                       (time.time(), fee, receipt_mode, h))
            return
        db.execute('INSERT INTO intents(hash,sender,raw,state,ts,fee,receipt_mode) VALUES(?,?,?,?,?,?,?)',
                   (h, sender.lower(), raw, 'preparing', time.time(), fee, receipt_mode))

def state(h, value):
    with journal() as db, db:
        db.execute('UPDATE intents SET state=? WHERE hash=?', (value, h))

def pending():
    with journal() as db:
        return [dict(r) for r in db.execute("SELECT * FROM intents WHERE state NOT IN (?,?,?) ORDER BY ts", DONE)]


def get(h):
    with journal() as db:
        row = db.execute('SELECT * FROM intents WHERE hash=?', (h,)).fetchone()
        return dict(row) if row else None


def everything():
    """Every intent, settled ones included: the nonce evidence needs the whole history."""
    with journal() as db:
        return [dict(r) for r in db.execute('SELECT * FROM intents ORDER BY ts')]


def note(h, text):
    """Why an unsettled intent waits for the operator ('' once it does not)."""
    with journal() as db, db:
        db.execute('UPDATE intents SET note=? WHERE hash=?', (text[:300], h))


def resolve(h, end, why):
    """Move an unsettled intent to 'abandoned' or 'dropped'. Only chain evidence may call this. Returns
    True when this call made the change (never twice, never over a settled intent)."""
    if end not in ('abandoned', 'dropped'):
        raise ValueError('invalid intent resolution')
    with journal() as db, db:
        return db.execute('UPDATE intents SET state=?, note=? WHERE hash=? AND state NOT IN (?,?,?)',
                          (end, why[:300], h) + DONE).rowcount == 1


def gone(h):
    """(state, why) when the journal proves this hash never executes; None otherwise. Bookkeeping that
    waits on a receipt for h may be released as failed on this evidence, and on nothing weaker."""
    if not h:
        return None
    item = get(h)
    if item and item['state'] in ('abandoned', 'dropped'):
        return item['state'], item['note'] or item['state']
    return None


def parked():
    """Unsettled intents waiting for the operator, with the reason, for private health."""
    return [{'hash': r['hash'], 'state': r['state'], 'reason': r['note'], 'since': int(r['ts'])}
            for r in pending() if r['note']]


def fields(raw):
    """(nonce, gas price) of a signed transaction: a legacy RLP list, or a typed (EIP-2718) envelope.
    For an EIP-1559 transaction the gas price is its max fee."""
    b = bytes.fromhex(raw[2:] if raw.startswith('0x') else raw)
    if b and b[0] < 0x7f:                          # typed: [chainId, nonce, (tip,) fee..., ...]
        f = rlp.decode(b[1:])
        price = f[3] if b[0] == 2 else f[2]
        return int.from_bytes(f[1], 'big'), int.from_bytes(price, 'big')
    f = rlp.decode(b)
    return int.from_bytes(f[0], 'big'), int.from_bytes(f[1], 'big')

def fees_today():
    with journal() as db:
        return db.execute('SELECT COALESCE(SUM(fee),0) FROM intents WHERE ts>?', (time.time()-86400,)).fetchone()[0]


def finality_value(key, value=None):
    with journal() as db, db:
        if value is not None:
            db.execute('INSERT OR REPLACE INTO finality_meta VALUES(?,?)', (key, value))
        row = db.execute('SELECT value FROM finality_meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else None


def anchor(rc, finalized):
    with journal() as db, db:
        db.execute('INSERT OR REPLACE INTO receipt_anchors VALUES(?,?,?,?,?)',
                   (rc['transactionHash'].lower(), int(rc['blockNumber'], 16), rc['blockHash'].lower(), rc['status'], int(finalized)))


def provisional_anchors():
    with journal() as db:
        return [dict(r) for r in db.execute('SELECT * FROM receipt_anchors WHERE finalized=0')]
