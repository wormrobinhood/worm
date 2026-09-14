"""Private durable transaction journal. Never exposed by the public state endpoint."""
import os
import sqlite3
import time
from contextlib import contextmanager
from . import config as C

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
        db.execute('CREATE TABLE IF NOT EXISTS receipt_anchors(hash TEXT PRIMARY KEY, block_number INTEGER, block_hash TEXT, status TEXT, finalized INTEGER)')
        db.execute('CREATE TABLE IF NOT EXISTS finality_meta(key TEXT PRIMARY KEY, value TEXT)')
        db.commit()
        yield db
    finally:
        db.close()

def record(h, sender, raw, fee, receipt_mode='finalized'):
    with journal() as db, db:
        db.execute('INSERT INTO intents(hash,sender,raw,state,ts,fee,receipt_mode) VALUES(?,?,?,?,?,?,?)',
                   (h, sender.lower(), raw, 'preparing', time.time(), fee, receipt_mode))

def state(h, value):
    with journal() as db, db:
        db.execute('UPDATE intents SET state=? WHERE hash=?', (value, h))

def pending():
    with journal() as db:
        return [dict(r) for r in db.execute("SELECT * FROM intents WHERE state != 'settled' ORDER BY ts")]

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
