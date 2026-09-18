"""The paper book: one row per token, the token's own cost, the close reason kept, totals over the
whole book. The price feed is stubbed; no network."""
import sqlite3
import threading
import time

import pytest

from wormhole import config as C
from wormhole import lab
from wormhole import paper as P

TOKEN = "0x" + "aa" * 20
OTHER = "0x" + "bb" * 20
TRAIL = "trailing stop 40% below the peak"


@pytest.fixture
def feed(monkeypatch, db):
    prices = {TOKEN: 1.0, OTHER: 1.0}
    monkeypatch.setattr(P, "token_prices", lambda addrs: {a: {"price_usd": prices.get(a)} for a in addrs})
    def entry(rpc, database, token, dollars, quotes=None, reference=None):
        cost = lab.token_cost(database, token)
        price = reference or prices[token]
        qty = dollars / price / (1 + cost)
        return {'price': price, 'pool': {'cost': cost}, 'minimum_raw': int(qty * 1e18), 'gas_usd': 0,
                'liquidation_usd': qty * price * (1 - cost)}
    def exit_quote(rpc, pool, token, amount, quotes=None):
        return {'minimum_usd': amount / 1e18 * prices[token] * (1 - pool['cost']), 'gas_usd': 0}
    monkeypatch.setattr(P.execution, 'entry', entry)
    monkeypatch.setattr(P.execution, 'exit_quote', exit_quote)
    return prices


def book(db, tax=200, fee=100):
    db.x("INSERT INTO launches(token,symbol,creator_tax_bps,curve_fee_bps) VALUES(?,?,?,?)", (TOKEN, "AAA", tax, fee))
    db.x("INSERT INTO launches(token,symbol) VALUES(?,?)", (OTHER, "BBB"))
    return P.Paper(db)


def test_paper_buy_uses_the_token_cost(db, feed):
    pb = book(db)
    pb.consider(TOKEN, {"score": 80, "verdict": "looks healthy", "metrics": {}})
    row = db.one("SELECT * FROM paper WHERE token=?", (TOKEN,))
    assert row["status"] == "open" and abs(row["cost"] - 0.04) < 1e-12 and row["policy"] == lab.DEFAULT
    assert abs(row["qty"] - C.PAPER_SIZE_USD / 1.0 / 1.04) < 1e-9
    assert "cost 4.0%/side" in db.one("SELECT text FROM events ORDER BY id DESC LIMIT 1")["text"]
    pb.consider(OTHER, {"score": 80, "verdict": "looks healthy", "metrics": {}})
    assert db.one("SELECT cost FROM paper WHERE token=?", (OTHER,))["cost"] == lab.FEE


def test_paper_close_keeps_reason(db, feed):
    pb = book(db)
    pb.consider(TOKEN, {"score": 80, "verdict": "looks healthy", "metrics": {}})
    qty = db.one("SELECT qty FROM paper WHERE token=?", (TOKEN,))["qty"]
    feed[TOKEN] = 1.6                                     # take profit at 1.5x: two thirds out
    pb.mark()
    row = db.one("SELECT * FROM paper WHERE token=?", (TOKEN,))
    assert row["status"] == "open" and row["tp_done"] == "[1.5]" and row["reason"] is None
    assert abs(row["qty_left"] - qty * 0.333) < 1e-6 and abs(row["realized_usd"] - 0.667 * qty * 1.6 * 0.96) < 1e-6
    feed[TOKEN] = 0.9                                     # 1.6 * 0.6 = 0.96: the trailing stop closes it
    pb.mark()
    row = db.one("SELECT * FROM paper WHERE token=?", (TOKEN,))
    assert row["status"] == "closed" and row["reason"] == TRAIL and row["exit_usd"] == 0.9
    want = 0.667 * qty * 1.6 * 0.96 + 0.333 * qty * 0.9 * 0.96 - C.PAPER_SIZE_USD
    assert abs(row["pnl_usd"] - want) < 1e-6 and row["qty_left"] < 1e-9
    ev = db.one("SELECT text FROM events WHERE text LIKE 'paper close%'")["text"]
    assert TRAIL in ev and "None" not in ev


def test_paper_stop_reason_on_a_single_tick(db, feed):
    pb = book(db)
    pb.consider(TOKEN, {"score": 80, "verdict": "looks healthy", "metrics": {}})
    feed[TOKEN] = 0.5
    pb.mark()
    row = db.one("SELECT * FROM paper WHERE token=?", (TOKEN,))
    assert row["status"] == "closed" and row["reason"] == "stop at -35%"


def test_paper_consider_is_idempotent_under_threads(db, monkeypatch, feed):
    def slow_prices(addrs):
        time.sleep(0.2)
        return {a: {"price_usd": 1.0} for a in addrs}
    monkeypatch.setattr(P, "token_prices", slow_prices)
    pb = book(db)
    res = {"score": 80, "verdict": "looks healthy", "metrics": {}}
    ts = [threading.Thread(target=pb.consider, args=(TOKEN, res)) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert db.one("SELECT COUNT(*) n FROM paper WHERE token=?", (TOKEN,))["n"] == 1
    assert db.one("SELECT COUNT(*) n FROM events WHERE text LIKE 'paper buy%'")["n"] == 1


def test_paper_dedupes_old_rows_and_enforces_one_per_token(db, feed):
    for k in range(3):
        db.x("INSERT INTO paper(token,symbol,opened_ts,entry_usd,size_usd,qty,status) VALUES(?,?,?,?,?,?,?)",
             (TOKEN, "AAA", 1000 + k, 1.0, 10.0, 10.0, "open"))
    first = db.one("SELECT MIN(id) id FROM paper")["id"]
    book(db)
    rows = db.q("SELECT id FROM paper WHERE token=?", (TOKEN,))
    assert [r["id"] for r in rows] == [first]
    with pytest.raises(sqlite3.IntegrityError):
        db.x("INSERT INTO paper(token,symbol,opened_ts,entry_usd,size_usd,qty,status) VALUES(?,?,?,?,?,?,?)",
             (TOKEN, "AAA", 2000, 1.0, 10.0, 10.0, "open"))


def test_paper_summary_all_closed(db, feed):
    pb = book(db)
    now = int(time.time())
    for k in range(31):
        db.x("INSERT INTO paper(token,symbol,opened_ts,entry_usd,size_usd,qty,status,closed_ts,exit_usd,pnl_usd) VALUES(?,?,?,?,?,?,?,?,?,?)",
             ("0x" + f"{k:040x}", f"T{k}", now - 100 - k, 1.0, 10.0, 10.0, "closed", now - k, 1.1, 1.0 if k < 20 else -1.0))
    s = pb.summary()
    assert len(s["closed"]) == 30 and s["closed_count"] == 31
    assert abs(s["realized_usd"] - (20 - 11)) < 1e-9 and s["win_rate"] == round(100 * 20 / 31)
    assert s["unrealized_usd"] == 0 and "in use: " + lab.DEFAULT in s["rules"]


def test_paper_summary_open_value_uses_the_cost(db, feed):
    pb = book(db)
    pb.consider(TOKEN, {"score": 80, "verdict": "looks healthy", "metrics": {}})
    feed[TOKEN] = 1.2
    pb.mark()
    s = pb.summary()
    row = s["open"][0]
    assert row["hedged"] is False and abs(row["change_pct"] - 20) < 1e-9
    assert abs(row["pnl_usd"] - (row["qty"] * 1.2 * 0.96 - 10.0)) < 1e-6


def test_partial_scores_are_not_traded(db, feed):
    pb = book(db)
    now = int(time.time())
    db.x("INSERT INTO scores(token,score,verdict,scored_at,partial,metrics) VALUES(?,?,?,?,?,?)", (TOKEN, 80, "looks healthy", now, 1, "{}"))
    db.x("INSERT INTO scores(token,score,verdict,scored_at,partial,metrics) VALUES(?,?,?,?,?,?)", (OTHER, 80, "looks healthy", now, 0, "{}"))
    pb.retry_pending()
    assert db.one("SELECT 1 FROM paper WHERE token=?", (TOKEN,)) is None
    assert db.one("SELECT 1 FROM paper WHERE token=?", (OTHER,))
    pb.consider(TOKEN, {"score": 80, "verdict": "looks healthy", "metrics": {"partial": True}})
    assert db.one("SELECT 1 FROM paper WHERE token=?", (TOKEN,)) is None


def test_delayed_arm_waits(db, feed, monkeypatch):
    pb = book(db)
    monkeypatch.setattr(lab, "pick_arm", lambda db: "costout_1.5x@30m")
    pb.consider(TOKEN, {"score": 80, "verdict": "looks healthy", "metrics": {}})
    assert db.one("SELECT 1 FROM paper WHERE token=?", (TOKEN,)) is None
    assert db.one("SELECT arm FROM intents WHERE token=?", (TOKEN,))["arm"] == "costout_1.5x@30m"
    db.x("UPDATE intents SET scored_at=scored_at-1800 WHERE token=?", (TOKEN,))
    pb.consider(TOKEN, {"score": 80, "verdict": "looks healthy", "metrics": {}})
    assert db.one("SELECT policy FROM paper WHERE token=?", (TOKEN,))["policy"] == "costout_1.5x@30m"


def test_failed_partial_quote_does_not_mark_take_profit_done(db, feed, monkeypatch):
    pb = book(db)
    pb.consider(TOKEN, {'score': 80, 'verdict': 'looks healthy', 'metrics': {}})
    original = P.execution.exit_quote
    qty = db.one('SELECT qty FROM paper')['qty']
    def fail_partial(rpc, pk, token, amount, **kw):
        if amount < int(qty * 1e18) - 10000:
            raise ValueError('quote outage')
        return original(rpc, pk, token, amount)
    monkeypatch.setattr(P.execution, 'exit_quote', fail_partial)
    feed[TOKEN] = 1.6
    pb.mark()
    row = db.one('SELECT * FROM paper')
    assert row['tp_done'] == '[]' and row['qty_left'] == qty and row['realized_usd'] == 0


def test_missing_mark_is_unknown_not_fake_profit(db, feed, monkeypatch):
    pb = book(db)
    pb.consider(TOKEN, {'score': 80, 'verdict': 'looks healthy', 'metrics': {}})
    db.x('UPDATE paper SET marked_ts=1')
    monkeypatch.setattr(P.execution, 'exit_quote', lambda *a, **k: (_ for _ in ()).throw(ValueError('quote outage')))
    pb.mark()
    s = pb.summary()
    assert s['open'][0]['pnl_usd'] is None and s['unpriced_count'] == 1
    assert not s['risk']['allowed']
