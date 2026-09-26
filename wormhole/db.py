"""SQLite storage. One connection, one lock, plain SQL."""
import collections
from contextlib import contextmanager
import sqlite3
import threading
import time

from . import config as C

# Housekeeping deletes in short transactions: the lock is let go between batches so the treasury and the scorer
# never wait long, and the WAL only ever holds one batch (a 150k-row DELETE in one go would grow it by ~100 MB).
PRUNE_BATCH = 500
PRUNE_MAX = 50_000          # rows per table per hourly run; a first run over a big table finishes over a few hours
PRUNE_PAUSE_S = 0.05
LAUNCH_KEEP_DAYS = 14       # ungraduated launches older than this are forgotten; their count per creator is kept

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
CREATE TABLE IF NOT EXISTS launch_history(deployer TEXT PRIMARY KEY, launches INTEGER NOT NULL DEFAULT 0) WITHOUT ROWID;
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


def delete_batched(db, table, where, args=(), batch=PRUNE_BATCH, limit=PRUNE_MAX, pause=PRUNE_PAUSE_S):
    """DELETE FROM table WHERE `where`, `batch` rows per transaction and at most `limit` per call, pausing
    between batches so other threads get the lock. Returns the number of rows removed."""
    done = 0
    while done < limit:
        want = min(batch, limit - done)
        n = db.xc(f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {where} LIMIT ?)", (*args, want))
        done += n
        if n < want:
            break
        time.sleep(pause)
    return done


def prune_launches(db, days=LAUNCH_KEEP_DAYS, batch=PRUNE_BATCH, limit=PRUNE_MAX, pause=PRUNE_PAUSE_S):
    """Forget ungraduated launches older than `days`: about 7,400 arrive a day and nothing looks at an old one
    except to count it for its creator. So the count per deployer moves to launch_history in the same
    transaction as the delete, and creator_trust, the flagged-creator count and the launch total read the
    same numbers before and after. Graduated launches, the worm's own token, rows without a timestamp or a
    deployer and the creators of tokens still waiting to be scored (a backlog after an outage) are kept. The windows that count
    launches by time (the 7-day serial list, the scorer's BACKFILL_HOURS) always stay whole. Returns the
    number of rows removed."""
    days = max(days, 8, C.BACKFILL_HOURS / 24 + 4)
    cutoff = int(time.time()) - int(days * 86400)
    done = 0
    while done < limit:
        want = min(batch, limit - done)
        with db.transaction():
            rows = db.q("SELECT token, deployer, ts FROM launches WHERE ts<? AND graduated=0 AND token!=?"
                        " AND deployer IS NOT NULL AND deployer NOT IN (SELECT l.deployer FROM scan_jobs j JOIN launches l ON l.token=j.token"
                        " WHERE j.state IN ('pending','leased') AND l.deployer IS NOT NULL) ORDER BY ts LIMIT ?",
                        (cutoff, C.TOKEN or "", want))
            if not rows:
                break
            db.many("INSERT INTO launch_history(deployer,launches) VALUES(?,?) ON CONFLICT(deployer)"
                    " DO UPDATE SET launches=launches+excluded.launches",
                    list(collections.Counter(r["deployer"] for r in rows).items()))
            for i in range(0, len(rows), 500):
                part = [r["token"] for r in rows[i:i + 500]]
                db.x(f"DELETE FROM launches WHERE token IN ({','.join('?' * len(part))})", part)
            # the span of forgotten launch times, so a forgotten launch that graduates later is not counted twice
            since, before = db.meta_get("launch_history_since"), db.meta_get("launch_history_before")
            lo, hi = min(r["ts"] for r in rows), max(r["ts"] for r in rows) + 1
            db.meta_set("launch_history_since", lo if since is None else min(int(since), lo))
            db.meta_set("launch_history_before", hi if before is None else max(int(before), hi))
            db.meta_set("launches_forgotten", int(db.meta_get("launches_forgotten") or 0) + len(rows))
        done += len(rows)
        if len(rows) < want:
            break
        time.sleep(pause)
    return done


def forgotten_launches(db, deployer):
    """Launches of this deployer that prune_launches removed: part of its record, no longer rows."""
    r = db.one("SELECT launches FROM launch_history WHERE deployer=?", (deployer,))
    return int(r["launches"]) if r else 0


def launches_total(db):
    """Every launch ever indexed, the forgotten ones included."""
    return db.one("SELECT COUNT(*) n FROM launches")["n"] + int(db.meta_get("launches_forgotten") or 0)


def launch_remembered(db, deployer, ts):
    """A launch was indexed again (a forgotten one graduated late and the indexer read it back from the
    factory): take it off the forgotten count, so its creator's record does not count it twice. Only a launch
    inside the forgotten span qualifies. True when a count was taken back."""
    since, before = db.meta_get("launch_history_since"), db.meta_get("launch_history_before")
    if ts is None or deployer is None or since is None or not int(since) <= int(ts) < int(before):
        return False
    with db.transaction():
        if db.xc("UPDATE launch_history SET launches=launches-1 WHERE deployer=? AND launches>0", (deployer,)) != 1:
            return False
        db.meta_set("launches_forgotten", max(0, int(db.meta_get("launches_forgotten") or 0) - 1))
    return True
