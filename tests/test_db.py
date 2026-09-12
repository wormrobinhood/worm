"""Storage: one connection shared by threads, WAL, the indexes the hot queries need, the migration, pruning."""
import sqlite3
import threading
import time

from wormhole.db import DB, prune_launches


def test_threads_share_the_connection(db):
    errors = []

    def work(tag):
        try:
            for i in range(1000):
                db.x("INSERT INTO events(ts,kind,text) VALUES(?,?,?)", (i, tag, "x"))
                db.q("SELECT COUNT(*) n FROM events WHERE kind=?", (tag,))
        except Exception as e:      # a ProgrammingError would land here
            errors.append(e)
    threads = [threading.Thread(target=work, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and db.one("SELECT COUNT(*) n FROM events")["n"] == 2000


def test_wal_and_rowcount(db):
    assert db.c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    db.x("INSERT INTO launches(token, graduated) VALUES('0xa', 0)")
    assert db.xc("UPDATE launches SET graduated=1 WHERE token='0xa' AND graduated=0") == 1
    assert db.xc("UPDATE launches SET graduated=1 WHERE token='0xa' AND graduated=0") == 0


def test_hot_queries_use_an_index(db):
    def plan(sql, args):
        return " ".join(r["detail"] for r in db.q("EXPLAIN QUERY PLAN " + sql, args))
    assert "USING INDEX launches_ts" in plan("SELECT token FROM launches WHERE ts>=?", (0,))
    assert "USING INDEX launches_deployer_block" in plan("SELECT token FROM launches WHERE deployer=? AND block>=?", ("0xd", 0))
    assert "INDEX launches_deployer_block" in plan("SELECT COUNT(*) FROM launches WHERE deployer=?", ("0xd",))   # covering


def test_old_table_gains_the_new_column_and_indexes(tmp_path):
    p = tmp_path / "old.db"
    c = sqlite3.connect(str(p))
    c.execute("CREATE TABLE launches(token TEXT PRIMARY KEY, curve TEXT, deployer TEXT, pair_token TEXT, pair_symbol TEXT,"
              " config_id INTEGER, grad_threshold TEXT, block INTEGER, ts INTEGER, tx TEXT, name TEXT, symbol TEXT, logo TEXT,"
              " description TEXT, twitter TEXT, telegram TEXT, website TEXT, creator_tax_bps INTEGER, curve_fee_bps INTEGER,"
              " meta INTEGER DEFAULT 0, graduated INTEGER DEFAULT 0, grad_block INTEGER, grad_ts INTEGER, grad_tx TEXT)")
    c.execute("CREATE INDEX launches_deployer ON launches(deployer)")
    c.commit()
    c.close()
    db = DB(p)
    assert "buyback" in {r["name"] for r in db.q("PRAGMA table_info(launches)")}
    names = {r["name"] for r in db.q("PRAGMA index_list(launches)")}
    assert {"launches_deployer_block", "launches_ts", "launches_block", "launches_grad"} <= names
    assert "launches_deployer" not in names
    db.x("INSERT INTO launches(token, config_id) VALUES(?, ?)", ("0xa", str(2 ** 64)))     # no OverflowError in the old shape
    assert DB(p).one("SELECT token FROM launches")["token"] == "0xa"                       # opening again is harmless


def test_prune_launches_keeps_what_matters(db):
    now = int(time.time())
    old, new = now - 40 * 86400, now - 86400
    rows = [("0x1", "0xd1", old, 0, 0),       # old, no metadata, deployer never graduated: pruned
            ("0x2", "0xd1", new, 0, 0),       # recent: kept
            ("0x3", "0xd2", old, 0, 0),       # old, but its deployer graduated 0x4: kept for creator history
            ("0x4", "0xd2", old, 1, 1),       # graduated: kept
            ("0x5", "0xd3", old, 1, 0),       # has metadata: kept
            ("0x6", "0xd4", None, 0, 0)]      # no timestamp: kept
    db.many("INSERT INTO launches(token,deployer,ts,meta,graduated) VALUES(?,?,?,?,?)", rows)
    assert prune_launches(db, days=30) == 1
    assert [r["token"] for r in db.q("SELECT token FROM launches ORDER BY token")] == ["0x2", "0x3", "0x4", "0x5", "0x6"]
    assert prune_launches(db, days=30) == 0
