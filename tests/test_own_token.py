"""The worm never grades, trades or rescans its own token."""
import time

import pytest

from fakerpc import FakeRpc, launch, run
from wormhole import config as C, paper as P, trader as TR

MINE = "0x" + "cd" * 20


@pytest.fixture
def own(monkeypatch):
    monkeypatch.setattr(C, "TOKEN", MINE)


def test_the_scorer_skips_its_own_token_with_one_note(db, own):
    rpc = FakeRpc(latest=200)
    launch(db, MINE, "0x" + "ee" * 20, C.WALLET, 100, 1000, 150, 2000)
    calls = []
    assert run(rpc, db, MINE, progress=calls.append) is None
    assert run(rpc, db, MINE, progress=calls.append) is None
    assert calls == []                                                   # no dig, so nothing for the screen or the page
    assert db.one("SELECT COUNT(*) n FROM scores WHERE token=?", (MINE,))["n"] == 0
    notes = db.q("SELECT text FROM events WHERE kind='launch' AND token=?", (MINE,))
    assert len(notes) == 1 and notes[0]["text"].startswith("graduated: its own token. The worm does not grade itself")
    other = "0x" + "ab" * 20                                             # everything else is scored as before
    launch(db, other, "0x" + "ef" * 20, "0x" + "11" * 20, 100, 1000, 150, 2000)
    assert run(rpc, db, other, progress=calls.append) is not None and calls


def test_the_paper_book_never_holds_its_own_token(db, own, monkeypatch):
    monkeypatch.setattr(P, "token_prices", lambda addrs: {a: {"price_usd": 1.0} for a in addrs})
    db.x("INSERT INTO launches(token,symbol,creator_tax_bps,curve_fee_bps) VALUES(?,?,?,?)", (MINE, "MINE", 200, 100))
    P.Paper(db).consider(MINE, {"score": 95, "verdict": "looks healthy", "metrics": {}})
    assert db.one("SELECT COUNT(*) n FROM paper WHERE token=?", (MINE,))["n"] == 0


def test_the_trader_never_lists_its_own_token(db, own):
    TR.ensure_tables(db)
    now = int(time.time())
    for t, sym in ((MINE, "MINE"), ("0x" + "ab" * 20, "OTHER")):
        db.x("INSERT INTO launches(token,symbol,graduated) VALUES(?,?,1)", (t, sym))
        db.x("INSERT INTO scores(token,score,verdict,scored_at,partial,metrics) VALUES(?,?,?,?,0,'{}')", (t, 95, "looks healthy", now))
    assert [r["symbol"] for r in TR.verdict_candidates(db)] == ["OTHER"]
    from wormhole.paper import Paper
    Paper(db)                                          # live follows paper: even a paper row for its own token is never a candidate
    for t, sym in ((MINE, "MINE"), ("0x" + "ab" * 20, "OTHER")):
        db.x("INSERT INTO paper(token,symbol,opened_ts,entry_usd,size_usd,qty,status,strategy) VALUES(?,?,?,1,10,10,'open','rule-a')", (t, sym, now))
    assert [r["symbol"] for r in TR.candidates(db, ["rule-a"])] == ["OTHER"]

