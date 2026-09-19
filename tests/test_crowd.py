"""Wallet records: a pick counts once its outcome is known, once per token; the read is a share of buy volume;
tokens remembered by recipient are re-read from the chain. No network."""
import json
import time

from wormhole import config as C
from wormhole import crowd

from fakerpc import FakeRpc, addr, launch

NOW = int(time.time())
LB, GB = 1_000_000, 1_070_000
ROUTER = addr(0x9000)


def token(db, i, outcome, wallets, by="holder", resolved=1):
    """A scored graduation with its remembered buyers and an outcome."""
    t, curve = addr(0x100 + i), addr(0x200 + i)
    launch(db, t, curve, addr(0x300 + i), LB + i, NOW - 90000, GB + i, NOW - 86400)
    metrics = {"buyers_by": by} if by else {}
    db.x("INSERT INTO scores(token,score,verdict,reasons,metrics,scored_at,partial,fired) VALUES(?,?,?,?,?,?,0,'[]')",
         (t, 20, "avoid", "[]", json.dumps(metrics), NOW - 86000 + i))
    db.x("INSERT INTO outcomes(token, verdict, outcome, resolved, change_pct, scored_at) VALUES(?,?,?,?,?,?)",
         (t, "avoid", outcome, resolved, -90.0, NOW - 86000 + i))
    db.many("INSERT OR REPLACE INTO curve_buyers(token,wallet,tokens_out,ts) VALUES(?,?,?,?)", [(t, w, 1e21, NOW - 86400) for w in wallets])
    return t, curve


def record(db, wallet):
    r = db.one("SELECT picks, good, grew FROM wallet_records WHERE wallet=?", (wallet,))
    return (r["picks"], r["good"], r["grew"]) if r else None


def test_a_pick_counts_once_its_outcome_is_known_and_only_once(db):
    a, b = addr(1), addr(2)
    token(db, 1, "rugged", [a, b])
    token(db, 2, "grew", [a])
    token(db, 3, "flat", [b])
    token(db, 4, "pending", [a, b], resolved=0)                 # not known yet: nobody is judged by it
    assert crowd.tick(None, db) == 3
    assert record(db, a) == (2, 1, 1) and record(db, b) == (2, 1, 0)
    assert crowd.tick(None, db) == 0 and record(db, a) == (2, 1, 1)      # a second pass changes nothing
    assert crowd.history(db) == 3


def test_an_outcome_nobody_could_read_teaches_nothing(db):
    token(db, 1, "unknown", [addr(1)])
    assert crowd.tick(None, db) == 0 and record(db, addr(1)) is None
    assert crowd.history(db) == 0 and db.one("SELECT source FROM wallet_folded")["source"] == "skipped"


def test_the_creator_is_not_part_of_its_own_crowd(db):
    t, _ = token(db, 1, "rugged", [addr(1), addr(0x301)])      # addr(0x301) is this token's deployer
    crowd.tick(None, db)
    assert record(db, addr(1)) == (1, 0, 0) and record(db, addr(0x301)) is None


def test_the_read_is_a_share_of_buy_volume_from_wallets_with_enough_picks(db):
    loser, mixed, rookie = addr(1), addr(2), addr(3)
    for i, outcome in enumerate(["rugged", "dumped", "rugged"]):
        token(db, i, outcome, [loser, mixed] if i else [loser, mixed, rookie])
    token(db, 7, "grew", [mixed])
    crowd.tick(None, db)
    seen = crowd.read(db, {loser: 600, mixed: 300, rookie: 50, addr(9): 50})
    assert seen["losing_pct"] == 60.0 and seen["losing_buyers"] == 1          # three picks, none survived
    assert seen["known_buyers_pct"] == 90.0 and seen["crowd_history"] == 4    # the rookie has one pick: not judged
    assert crowd.read(db, {})["losing_pct"] is None


def test_tokens_remembered_by_recipient_are_re_read_from_the_chain(db):
    users = [addr(0xA000 + i) for i in range(3)]
    t, curve = token(db, 1, "rugged", [ROUTER], by=None)       # scored before buys were followed: the router was "the buyer"
    rpc = FakeRpc(GB + 5000)
    for i, u in enumerate(users):
        tx = "0x" + format(0xF000 + i, "064x")
        rpc.transfer(t, LB + 10 + i, curve, ROUTER, 10**21, tx=tx)
        rpc.curve_buy(curve, LB + 10 + i, ROUTER, ROUTER, 10**21, tx=tx)
        rpc.transfer(t, LB + 10 + i, ROUTER, u, 10**21, tx=tx)
    assert crowd.tick(rpc, db) == 1
    assert all(record(db, u) == (1, 0, 0) for u in users) and record(db, ROUTER) is None
    assert db.one("SELECT source, buyers FROM wallet_folded WHERE token=?", (t,)) == {"source": "chain", "buyers": 3}


def test_the_chain_is_re_read_a_few_tokens_a_cycle_and_a_dead_read_is_given_up(db):
    for i in range(6):
        token(db, i, "rugged", [ROUTER], by=None)
    rpc = FakeRpc(GB + 5000)
    rpc.fail_addr.update(addr(0x200 + i) for i in range(6))     # every curve read fails
    crowd._failed.clear()
    for _ in range(3):
        assert crowd.tick(rpc, db, backfill=2) == 0
    assert db.one("SELECT COUNT(*) n FROM wallet_folded WHERE source='skipped'")["n"] == 2      # the newest two, after three tries
    assert len([c for c in rpc.calls]) == 6                     # two tokens a cycle, never more


def test_the_chain_re_read_also_teaches_who_passes_tokens_on(db):
    from wormhole import linked
    t, curve = token(db, 1, "rugged", [ROUTER], by=None)
    rpc = FakeRpc(GB + 5000)
    user, friend = addr(0xA000), addr(0xA001)
    tx = "0x" + format(0xF000, "064x")
    rpc.transfer(t, LB + 10, curve, ROUTER, 10**21, tx=tx)
    rpc.curve_buy(curve, LB + 10, ROUTER, ROUTER, 10**21, tx=tx)
    rpc.transfer(t, LB + 10, ROUTER, user, 10**21, tx=tx)
    rpc.transfer(t, LB + 40, user, friend, 10**20)
    assert crowd.tick(rpc, db) == 1
    assert {x["wallet"] for x in db.q("SELECT wallet FROM token_senders WHERE token=?", (t,))} == {ROUTER, user}
    assert linked.history(db) == 1
