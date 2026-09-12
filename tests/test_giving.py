"""Giving: weights validated, real money only, per-cause gating, pending gifts settled."""
import logging
import time

import pytest
from eth_abi import decode

from fakes import auto_receipts, decode_tx, transfer_log, tx_hash
from wormhole import config as C, giving as G, treasury as T

A = "0x" + "aa" * 20
B = "0x" + "bb" * 20
RUNWAY = {"surplus_usd": 72.9, "can_invest": True}


@pytest.fixture
def two_causes(monkeypatch):
    monkeypatch.setenv("WH_CAUSES", f"Cause A|{A}|2,Cause B|{B}|1")
    monkeypatch.setattr(G, "SHARE", 0.10)
    monkeypatch.setenv("WH_DEMO_TREASURY", "0")


def transfers(rpc):
    out = []
    for raw in rpc.raw:
        t = decode_tx(raw)
        assert t["to"] == C.USDG
        to, n = decode(["address", "uint256"], t["data"][4:])
        out.append((to.lower(), n))
    return out


def ledger(db):
    return db.q("SELECT kind, amount, note FROM ledger ORDER BY id")


def test_causes_parse_and_reject_bad_weights(monkeypatch, caplog):
    monkeypatch.setenv("WH_CAUSES", f"A|{A}|2, B|{B}|-1,C|{A}|nan,D|{B}|0,E|{A}|inf,F|0x123|1,G|{B},H|{B}|x,|{A}|1,I|{A}|")
    with caplog.at_level(logging.WARNING, logger="wormhole.giving"):
        cs = G.causes()
    assert [(c["name"], c["address"], c["weight"]) for c in cs] == [("A", A, 2.0), ("G", B, 1.0), (A[:10], A, 1.0), ("I", A, 1.0)]
    assert caplog.text.count("skipped") == 5


def test_demo_treasury_never_moves_real_money(db, rpc, acct, live, two_causes, monkeypatch):
    monkeypatch.setenv("WH_DEMO_TREASURY", "150")
    G.cycle(rpc, db, acct, RUNWAY, True)
    assert rpc.calls == [] and [r["kind"] for r in ledger(db)] == ["give_demo", "give_demo"]
    monkeypatch.setenv("WH_DEMO_TREASURY", "0")
    G.cycle(rpc, db, acct, {**RUNWAY, "treasury_is_demo": True}, True)
    assert rpc.calls == []


def test_live_gifts_are_split_by_weight(db, rpc, acct, live, two_causes):
    auto_receipts(rpc, C.WALLET)
    G.cycle(rpc, db, acct, RUNWAY, True)
    assert transfers(rpc) == [(A, 4_860_000), (B, 2_430_000)]
    assert ledger(db) == [{"kind": "give", "amount": 4.86, "note": "to Cause A"}, {"kind": "give", "amount": 2.43, "note": "to Cause B"}]
    assert db.q("SELECT tx FROM ledger ORDER BY id") == [{"tx": tx_hash(rpc.raw[0])}, {"tx": tx_hash(rpc.raw[1])}]
    assert G.summary(db)["given_total"] == 7.29
    texts = [e["text"] for e in db.q("SELECT text FROM events WHERE kind='giving' ORDER BY id")]
    assert texts == ["gave 4.86 USDG to Cause A", "gave 2.43 USDG to Cause B"]
    G.cycle(rpc, db, acct, RUNWAY, True)                              # within 30 days: nothing more
    assert len(rpc.raw) == 2


def test_a_failed_cause_is_retried_next_cycle_alone(db, rpc, acct, live, two_causes):
    auto_receipts(rpc, C.WALLET, status_for=lambda to, n: "0x0" if to == B else "0x1")
    G.cycle(rpc, db, acct, RUNWAY, True)
    assert [r["kind"] for r in ledger(db)] == ["give", "give_failed"]
    auto_receipts(rpc, C.WALLET)
    G.cycle(rpc, db, acct, RUNWAY, True)
    assert transfers(rpc)[2:] == [(B, 2_430_000)] and len(rpc.raw) == 3
    assert [r["kind"] for r in ledger(db)] == ["give", "give_failed", "give"]


def test_a_transfer_without_its_log_is_not_a_gift(db, rpc, acct, live, two_causes):
    G.cycle(rpc, db, acct, RUNWAY, True)                              # status 0x1, empty logs
    assert [r["kind"] for r in ledger(db)] == ["give_failed", "give_failed"]
    assert G.summary(db)["given_total"] == 0


def test_demo_rows_do_not_gate_live_and_live_rows_do_not_gate_demo(db, rpc, acct, live, two_causes):
    T.ensure_tables(db)
    db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)", (int(time.time()), "give_demo", "USDG", 1.0, None, "to Cause A"))
    auto_receipts(rpc, C.WALLET)
    G.cycle(rpc, db, acct, RUNWAY, True)
    assert transfers(rpc) == [(A, 4_860_000), (B, 2_430_000)]
    G.cycle(rpc, db, None, RUNWAY, False)
    assert [r["kind"] for r in ledger(db)] == ["give_demo", "give", "give", "give_demo"]   # only B in demo: A's demo row is fresh


def test_a_pending_gift_is_settled_before_new_ones(db, rpc, acct, live, two_causes):
    T.ensure_tables(db)
    h = "0x" + "ee" * 32
    db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)", (int(time.time()) - 60, "give_pending", "USDG", 4.86, h, "to Cause A"))
    rpc.receipts[h] = {"status": "0x1", "blockNumber": "0x10", "logs": [transfer_log(C.USDG, C.WALLET, A, 4_860_000)]}
    auto_receipts(rpc, C.WALLET)
    G.cycle(rpc, db, acct, RUNWAY, True)
    assert ledger(db)[0] == {"kind": "give", "amount": 4.86, "note": "to Cause A"}
    assert transfers(rpc) == [(B, 2_430_000)]                         # A just got its gift; only B is sent
    rpc.receipts[h] = None
    db.x("UPDATE ledger SET kind='give_pending' WHERE tx=?", (h,))
    G.cycle(rpc, db, acct, RUNWAY, True)
    assert len(rpc.raw) == 1                                          # still in flight: nothing new


def test_demo_mode_writes_would_give(db, rpc, two_causes):
    G.cycle(rpc, db, None, RUNWAY, False)
    assert ledger(db) == [{"kind": "give_demo", "amount": 4.86, "note": "to Cause A"}, {"kind": "give_demo", "amount": 2.43, "note": "to Cause B"}]
    texts = [e["text"] for e in db.q("SELECT text FROM events WHERE kind='giving' ORDER BY id")]
    assert texts[0] == "demo: would give 4.86 USDG to Cause A (10% of the surplus)"
    assert rpc.calls == []


def test_small_or_negative_surplus_gives_nothing(db, rpc, acct, live, two_causes):
    G.cycle(rpc, db, acct, {"surplus_usd": 5.0}, True)                # 10% is under the $1 minimum
    G.cycle(rpc, db, acct, {"surplus_usd": -3.0}, True)
    G.cycle(rpc, db, acct, {}, True)
    assert rpc.calls == [] and ledger(db) == []


def test_no_causes_means_nothing(db, rpc, acct, live, monkeypatch):
    monkeypatch.setenv("WH_CAUSES", "")
    G.cycle(rpc, db, acct, RUNWAY, True)
    assert rpc.calls == [] and G.summary(db)["causes"] == []
