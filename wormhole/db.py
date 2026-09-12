"""SQLite storage. One connection, one lock, plain SQL."""
import sqlite3
import threading
import time

from . import config as C

SCHEMA = """
CREATE TABLE IF NOT EXISTS launches(
  token TEXT PRIMARY KEY, curve TEXT, deployer TEXT, pair_token TEXT, pair_symbol TEXT,
  config_id INTEGER, grad_threshold TEXT, block INTEGER, ts INTEGER, tx TEXT,
  name TEXT, symbol TEXT, logo TEXT, description TEXT, twitter TEXT, telegram TEXT, website TEXT,
  creator_tax_bps INTEGER, curve_fee_bps INTEGER, meta INTEGER DEFAULT 0,
  graduated INTEGER DEFAULT 0, grad_block INTEGER, grad_ts INTEGER, grad_tx TEXT);
CREATE INDEX IF NOT EXISTS launches_deployer ON launches(deployer);
CREATE INDEX IF NOT EXISTS launches_block ON launches(block);
CREATE INDEX IF NOT EXISTS launches_grad ON launches(graduated, grad_block);
CREATE TABLE IF NOT EXISTS scores(
  token TEXT PRIMARY KEY, score INTEGER, verdict TEXT, reasons TEXT, metrics TEXT,
  scored_at INTEGER, partial INTEGER DEFAULT 0, fired TEXT);
CREATE TABLE IF NOT EXISTS paper(
  id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT, symbol TEXT, opened_ts INTEGER, entry_usd REAL,
  size_usd REAL, qty REAL, status TEXT, closed_ts INTEGER, exit_usd REAL, pnl_usd REAL, last_usd REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS outcomes(
  token TEXT PRIMARY KEY, score INTEGER, verdict TEXT, scored_at INTEGER, price0 REAL,
  checks TEXT, outcome TEXT DEFAULT 'pending', change_pct REAL, resolved INTEGER DEFAULT 0, fired TEXT);
CREATE TABLE IF NOT EXISTS rules(
  id TEXT PRIMARY KEY, weight REAL DEFAULT 1.0, hits INTEGER DEFAULT 0, misses INTEGER DEFAULT 0, updated INTEGER);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT, text TEXT, token TEXT);
CREATE TABLE IF NOT EXISTS samples(ts INTEGER PRIMARY KEY, usd REAL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


class DB:
    def __init__(self, path=C.DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.c = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.c.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.c.executescript(SCHEMA)
            self.c.commit()

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.c.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql, args=()):
        with self.lock:
            self.c.execute(sql, args)
            self.c.commit()

    def many(self, sql, rows):
        with self.lock:
            self.c.executemany(sql, rows)
            self.c.commit()

    def meta_get(self, key, default=None):
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else default

    def meta_set(self, key, value):
        self.x("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, str(value)))

    def add_event(self, kind, text, token=None):
        self.x("INSERT INTO events(ts,kind,text,token) VALUES(?,?,?,?)", (int(time.time()), kind, text[:300], token))

    def events(self, n=40):
        return self.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (n,))
