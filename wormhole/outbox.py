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
        db.execute('CREATE TABLE IF NOT EXISTS intents(hash TEXT PRIMARY KEY, sender TEXT, raw TEXT, state TEXT, ts REAL, fee REAL)')
        db.commit()
        yield db
    finally:
        db.close()

def record(h, sender, raw, fee):
    with journal() as db, db:
        db.execute('INSERT INTO intents VALUES(?,?,?,?,?,?)', (h, sender.lower(), raw, 'preparing', time.time(), fee))

def state(h, value):
    with journal() as db, db:
        db.execute('UPDATE intents SET state=? WHERE hash=?', (value, h))

def pending():
    with journal() as db:
        return [dict(r) for r in db.execute("SELECT * FROM intents WHERE state != 'settled' ORDER BY ts")]

def fees_today():
    with journal() as db:
        return db.execute('SELECT COALESCE(SUM(fee),0) FROM intents WHERE ts>?', (time.time()-86400,)).fetchone()[0]
