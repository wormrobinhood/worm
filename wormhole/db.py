"""SQLite storage. One connection, one lock, plain SQL."""
from contextlib import contextmanager
import sqlite3
import threading
import time

from . import config as C

SCHEMA = """
CREATE TABLE IF NOT EXISTS launches(
  token TEXT PRIMARY KEY, curve TEXT, deployer TEXT, pair_token TEXT, pair_symbol TEXT,
  config_id TEXT, grad_threshold TEXT, block INTEGER, ts INTEGER, tx TEXT,
  name TEXT, symbol TEXT, logo TEXT, description TEXT, twitter TEXT, telegram TEXT, website TEXT,
  creator_tax_bps INTEGER, curve_fee_bps INTEGER, buyback INTEGER, meta INTEGER DEFAULT 0,
  graduated INTEGER DEFAULT 0, grad_block INTEGER, grad_ts INTEGER, grad_tx TEXT);
DROP INDEX IF EXISTS launches_deployer;
CREATE INDEX IF NOT EXISTS launches_deployer_block ON launches(deployer, block);
CREATE INDEX IF NOT EXISTS launches_block ON launches(block);
CREATE INDEX IF NOT EXISTS launches_ts ON launches(ts);
CREATE INDEX IF NOT EXISTS launches_grad ON launches(graduated, grad_block);
CREATE TABLE IF NOT EXISTS scores(
  token TEXT PRIMARY KEY, score INTEGER, verdict TEXT, reasons TEXT, metrics TEXT,
  scored_at INTEGER, partial INTEGER DEFAULT 0, fired TEXT);
CREATE TABLE IF NOT EXISTS assessments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL, score INTEGER, verdict TEXT,
  reasons TEXT, metrics TEXT, scored_at INTEGER, partial INTEGER, fired TEXT, engine_version TEXT);
CREATE INDEX IF NOT EXISTS assessments_token_time ON assessments(token, scored_at);
CREATE TABLE IF NOT EXISTS scan_jobs(
  token TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER DEFAULT 0,
  available_at INTEGER NOT NULL, lease_until INTEGER, generation INTEGER DEFAULT 1);
CREATE INDEX IF NOT EXISTS scan_jobs_state_time ON scan_jobs(state, available_at);
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
CREATE TABLE IF NOT EXISTS curve_buyers(token TEXT, wallet TEXT, tokens_out REAL, ts INTEGER, PRIMARY KEY(token, wallet));
CREATE INDEX IF NOT EXISTS curve_buyers_wallet ON curve_buyers(wallet, ts);
CREATE INDEX IF NOT EXISTS curve_buyers_ts ON curve_buyers(ts);
CREATE TABLE IF NOT EXISTS wallet_records(wallet TEXT PRIMARY KEY, picks INTEGER, good INTEGER, grew INTEGER, updated INTEGER);
CREATE TABLE IF NOT EXISTS wallet_folded(token TEXT PRIMARY KEY, ts INTEGER, buyers INTEGER, source TEXT);
CREATE INDEX IF NOT EXISTS events_text ON events(text);
"""

# Columns added after the first release. CREATE TABLE IF NOT EXISTS leaves an existing table alone.
ADDED_COLUMNS = {"launches": [("buyback", "INTEGER")],
                 "outcomes": [("assessment_id", "INTEGER"), ("baseline_ts", "INTEGER")]}


class DB:
    def __init__(self, path=C.DB_PATH):
        self.path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.c = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.c.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._depth = 0
        with self.lock:
            self.c.execute("PRAGMA journal_mode=WAL")       # readers never wait for the writer
            self.c.execute("PRAGMA synchronous=FULL")
            self.c.executescript(SCHEMA)
            self._migrate()
            self.c.commit()

    def _migrate(self):
        for table, cols in ADDED_COLUMNS.items():
            have = {r["name"] for r in self.c.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, typ in cols:
                if name not in have:
                    self.c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.c.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    @contextmanager
    def transaction(self):
        """A nested, rollback-safe unit of work on this connection."""
        with self.lock:
            name = f"unit_{self._depth}"
            self.c.execute(f"SAVEPOINT {name}")
            self._depth += 1
            try:
                yield
                self.c.execute(f"RELEASE SAVEPOINT {name}")
            except BaseException:
                # SQLITE_FULL can roll back the entire transaction itself. Do not mask that
                # original error with "no such savepoint", or leave a failed commit pending.
                try:
                    if self.c.in_transaction:
                        self.c.execute(f"ROLLBACK TO SAVEPOINT {name}")
                        self.c.execute(f"RELEASE SAVEPOINT {name}")
                except sqlite3.Error:
                    self.c.rollback()
                raise
            finally:
                self._depth -= 1

    def x(self, sql, args=()):
        with self.lock:
            self.c.execute(sql, args)
            if not self._depth:
                self.c.commit()

    def insert(self, sql, args=()):
        """Return the ID of this insert while holding the connection lock."""
        with self.transaction():
            return self.c.execute(sql, args).lastrowid

    def xc(self, sql, args=()):
        """Like x(), returning the number of rows the statement changed."""
        with self.lock:
            n = self.c.execute(sql, args).rowcount
            if not self._depth:
                self.c.commit()
            return n

    def many(self, sql, rows):
        with self.lock:
            self.c.executemany(sql, rows)
            if not self._depth:
                self.c.commit()

    def atomic(self, statements):
        """Commit a related group of writes together, rolling back on any failure."""
        with self.transaction():
            for sql, args in statements:
                self.c.execute(sql, args)

    def meta_get(self, key, default=None):
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else default

    def meta_set(self, key, value):
        self.x("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, str(value)))

    def add_event(self, kind, text, token=None):
        self.x("INSERT INTO events(ts,kind,text,token) VALUES(?,?,?,?)", (int(time.time()), kind, text[:300], token))

    def events(self, n=40):
        return self.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (n,))


def prune_launches(db, days=30):
    """Forget launches older than `days` that never graduated, never got metadata, and whose deployer
    never graduated anything. Creator history keeps everything else. Returns the number of rows removed."""
    cutoff = int(time.time()) - int(days * 86400)
    return db.xc("DELETE FROM launches WHERE graduated=0 AND meta=0 AND ts<? AND token!=?"
                 " AND deployer NOT IN (SELECT deployer FROM launches WHERE graduated=1)",
                 (cutoff, C.TOKEN or ""))
