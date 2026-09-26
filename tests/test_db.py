"""Storage: one connection shared by threads, WAL, the indexes the hot queries need, the migration, pruning."""
import sqlite3
import threading
import time

from wormhole.db import (DB, delete_batched, forgotten_launches, launch_remembered, launches_total,
                         prune_launches)


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


def test_prune_launches_keeps_what_matters(db, monkeypatch):
    from wormhole import config as C
    monkeypatch.setattr(C, "TOKEN", "0x9")
    now = int(time.time())
    old, new = now - 40 * 86400, now - 86400
    rows = [("0x1", "0xd1", old, 0, 0),       # old and never graduated: forgotten
            ("0x2", "0xd1", new, 0, 0),       # recent: kept
            ("0x3", "0xd2", old, 0, 0),       # old, although its deployer graduated 0x4: forgotten, counted
            ("0x4", "0xd2", old, 1, 1),       # graduated: kept
            ("0x5", "0xd3", old, 1, 0),       # has metadata: forgotten all the same
            ("0x6", "0xd4", None, 0, 0),      # no timestamp: kept
            ("0x7", "0xd5", old, 0, 0),       # its creator has a token waiting to be scored: kept
            ("0x8", "0xd5", new, 0, 1),
            ("0x9", "0xd6", old, 0, 0),       # the worm's own token: kept
            ("0xa", None, old, 0, 0)]         # no deployer: kept (no creator to count it for)
    db.many("INSERT INTO launches(token,deployer,ts,meta,graduated) VALUES(?,?,?,?,?)", rows)
    db.x("INSERT INTO scan_jobs(token,available_at) VALUES('0x8',0)")
    before = launches_total(db)
    assert prune_launches(db, pause=0) == 3
    assert [r["token"] for r in db.q("SELECT token FROM launches ORDER BY token")] == ["0x2", "0x4", "0x6", "0x7", "0x8", "0x9", "0xa"]
    assert {r["deployer"]: r["launches"] for r in db.q("SELECT * FROM launch_history")} == {"0xd1": 1, "0xd2": 1, "0xd3": 1}
    assert launches_total(db) == before == 10 and forgotten_launches(db, "0xd2") == 1 and forgotten_launches(db, "0xd4") == 0
    assert prune_launches(db, pause=0) == 0
    db.x("UPDATE scan_jobs SET state='done'")
    assert prune_launches(db, pause=0) == 1 and forgotten_launches(db, "0xd5") == 1     # once it is scored


def test_prune_launches_is_batched_bounded_and_never_cuts_into_the_windows(db):
    now = int(time.time())
    db.many("INSERT INTO launches(token,deployer,ts) VALUES(?,?,?)",
            [(f"0x{i:03x}", f"0xd{i % 3}", now - 30 * 86400 - i) for i in range(12)]
            + [(f"0x{i:03x}", "0xd0", now - 7 * 86400 - 3600) for i in range(100, 105)])     # inside the 7-day list + margin
    batches = []
    real = db.transaction
    def counted():
        batches.append(1)
        return real()
    db.transaction = counted
    assert prune_launches(db, days=1, batch=4, limit=10, pause=0) == 10 and len(batches) == 3      # 4 + 4 + 2
    assert prune_launches(db, days=1, batch=4, limit=10, pause=0) == 2
    assert db.one("SELECT COUNT(*) n FROM launches")["n"] == 5                   # days=1 is raised to the floor
    assert sum(r["launches"] for r in db.q("SELECT launches FROM launch_history")) == 12
    assert db.meta_get("launches_forgotten") == "12"


def test_delete_batched_removes_in_bounded_batches(db):
    db.many("INSERT INTO events(ts,kind,text) VALUES(?,?,?)", [(i, "k", str(i)) for i in range(25)])
    assert delete_batched(db, "events", "ts<?", (20,), batch=6, limit=12, pause=0) == 12
    assert delete_batched(db, "events", "ts<?", (20,), batch=6, limit=12, pause=0) == 8
    assert [r["ts"] for r in db.q("SELECT ts FROM events ORDER BY ts")] == [20, 21, 22, 23, 24]


def test_a_forgotten_launch_that_graduates_late_is_counted_once(db):
    from wormhole.indexer import Indexer
    from wormhole.learn import creator_trust
    from test_indexer import Chain, launched, struct, GET, T1, DEPLOYER
    node = Chain(logs=[launched(T1, 500)], answers={GET: struct()})
    db.many("INSERT INTO launches(token,deployer,ts,graduated) VALUES(?,?,?,0)",
            [(T1, DEPLOYER, node.block_ts(500)), ("0xb", DEPLOYER, node.block_ts(400))])
    total = launches_total(db)
    assert prune_launches(db, pause=0) == 2 and forgotten_launches(db, DEPLOYER) == 2
    assert Indexer(node, db)._ensure_launch(T1, 900) is True             # its graduation: read back from the factory
    assert forgotten_launches(db, DEPLOYER) == 1 and launches_total(db) == total
    assert creator_trust(db, DEPLOYER)[1]["launches"] == 2
    assert not launch_remembered(db, DEPLOYER, int(time.time()))           # a launch newer than any forgotten one


def test_old_code_reads_are_asked_again(db):
    from wormhole import linked
    now = int(time.time())
    linked.ensure_tables(db)
    db.many("INSERT INTO code_cache(address,is_contract,ts) VALUES(?,?,?)",
            [("0xa", 1, now - 40 * 86400), ("0xb", 0, now - 86400)])
    db.many("INSERT INTO token_senders(wallet,token,ts) VALUES(?,?,?)", [("0xa", "0xt", now - 30 * 86400), ("0xb", "0xt", now)])
    assert linked.prune(db, pause=0) == (1, 1)
    assert [r["address"] for r in db.q("SELECT address FROM code_cache")] == ["0xb"]
