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
    def exit_quote(rpc, pool, token, amount, quotes=None, cached_prices=False):
        return {'minimum_usd': amount / 1e18 * prices[token] * (1 - pool.get('cost', 0.04)), 'gas_usd': 0}
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


def test_paper_close_keeps_reason(db, feed, monkeypatch):
    monkeypatch.setattr(lab, "pick_arm", lambda db: "costout_1.5x@0m")      # an arm with a take-profit and a trail
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


def test_paper_stop_reason_on_a_single_tick(db, feed, monkeypatch):
    monkeypatch.setattr(lab, "pick_arm", lambda db: "costout_1.5x@0m")
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
    assert row["hedged"] is False and row["change_pct"] == pytest.approx((1.2 / 1.04 - 1) * 100)
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


# ---- the default exit: the profit lock ------------------------------------------------------------

def enter(db, feed, price=1.0):
    pb = book(db)
    feed[TOKEN] = price
    assert pb.enter(TOKEN, "AAA", price, "rule-a", "second look")
    return pb


def test_the_default_exit_is_the_profit_lock(db, feed):
    pb = enter(db, feed)
    row = db.one("SELECT policy, strategy, policy_spec FROM paper")
    assert row["policy"] == lab.DEFAULT == "lock_20@0m" and row["strategy"] == "rule-a"
    assert lab.parse_arm(lab.DEFAULT)[0]["stop"] == -0.30 and lab.parse_arm(lab.DEFAULT)[0]["arm_at"] == 1.2
    feed[TOKEN] = 0.75
    pb.mark(prices={TOKEN: 0.75}, value=False)                       # above 70% of the acquisition cost: inside the stop
    assert db.one("SELECT status FROM paper")["status"] == "open"
    feed[TOKEN] = 0.69                                               # the pool's bid at that moment fills the exit
    pb.mark(prices={TOKEN: 0.69}, value=False)
    row = db.one("SELECT status, reason, pnl_usd FROM paper")
    assert row["status"] == "closed" and row["reason"] == "stop at -30%" and row["pnl_usd"] < -3.0


def test_the_lock_sells_nothing_on_the_way_up_and_everything_on_the_trail(db, feed):
    pb = enter(db, feed)
    for price in (1.1, 1.25, 1.6):                                   # +25% arms it; nothing is sold into strength
        feed[TOKEN] = price
        pb.mark(prices={TOKEN: price}, value=False)
    row = db.one("SELECT status, trail_on, qty, qty_left, peak_usd FROM paper")
    assert row["status"] == "open" and row["trail_on"] == 1 and row["qty_left"] == row["qty"] and row["peak_usd"] == 1.6
    feed[TOKEN] = 1.37                                               # 1.6 * 0.85 = 1.36: holds
    pb.mark(prices={TOKEN: 1.37}, value=False)
    assert db.one("SELECT status FROM paper")["status"] == "open"
    feed[TOKEN] = 1.35
    pb.mark(prices={TOKEN: 1.35}, value=False)
    row = db.one("SELECT status, reason, pnl_usd FROM paper")
    assert row["status"] == "closed" and row["reason"] == "trailing stop 15% below the peak" and row["pnl_usd"] > 2.0


def test_a_big_pump_gets_a_wider_trail(db, feed):
    pb = enter(db, feed)
    for price in (2.1, 4.2, 3.2):                                    # peak >4x acquisition cost trails 25%: 3.2 holds
        feed[TOKEN] = price
        pb.mark(prices={TOKEN: price}, value=False)
    assert db.one("SELECT status FROM paper")["status"] == "open"
    feed[TOKEN] = 2.95
    pb.mark(prices={TOKEN: 2.95}, value=False)
    row = db.one("SELECT status, reason FROM paper")
    assert row["status"] == "closed" and row["reason"] == "trailing stop 25% below the peak"


def test_dead_money_is_closed_after_twelve_hours(db, feed, monkeypatch):
    pb = enter(db, feed)
    opened = db.one("SELECT opened_ts FROM paper")["opened_ts"]
    monkeypatch.setattr(P.time, "time", lambda: opened + 12 * 3600 + 1)
    feed[TOKEN] = 1.05
    pb.mark(prices={TOKEN: 1.05}, value=False)
    assert db.one("SELECT status, reason FROM paper") == {"status": "closed", "reason": "time limit"}


def test_a_second_look_entry_is_once_per_token_and_never_its_own(db, feed, monkeypatch):
    pb = enter(db, feed)
    assert not pb.enter(TOKEN, "AAA", 1.0, "rule-a", "again")
    monkeypatch.setattr(C, "TOKEN", OTHER)
    feed[OTHER] = 1.0
    assert not pb.enter(OTHER, "BBB", 1.0, "rule-a", "own token")
    assert db.one("SELECT COUNT(*) n FROM paper")["n"] == 1


def test_the_fast_mark_does_not_ask_for_a_valuation_quote(db, feed, monkeypatch):
    pb = enter(db, feed)
    asked = []
    original = P.execution.exit_quote
    monkeypatch.setattr(P.execution, "exit_quote", lambda *a, **k: asked.append(a) or original(*a, **k))
    pb.mark(prices={TOKEN: 1.05}, value=False)
    assert asked == [] and db.one("SELECT last_usd, liquidation_usd FROM paper")["last_usd"] == 1.05
    pb.mark()                                                        # the slow mark values the position from a bid
    assert len(asked) == 1


# ---- one price source per position ----------------------------------------------------------------

def real_pool(token):
    c0, c1 = sorted([token, C.USDG], key=lambda a: int(a, 16))
    return {"token": token, "c0": c0, "c1": c1, "fee": 0, "tick_spacing": 200, "hooks": C.HOOK, "quote": C.USDG}


def test_a_position_with_its_own_pool_is_never_marked_by_the_price_api(db, feed, monkeypatch):
    """Production, 2026-09-19: a thin pool spiked on the chain (peak 1.38x, trail armed), the five-minute mark
    then read the price API's older 0.99x and sold a position that was still 26% up."""
    import json
    pb = enter(db, feed)
    db.x("UPDATE paper SET pool_key=?", (json.dumps(real_pool(TOKEN)),))
    chain = {TOKEN: 1.38}
    monkeypatch.setattr(P.poolstate, "position_mids", lambda rpc, rows: ({t: chain[t] for t in chain if chain[t]}, {TOKEN}))
    feed[TOKEN] = 1.38
    pb.mark(prices={TOKEN: 1.38}, value=False)                       # the fast mark sees the spike and arms the trail
    monkeypatch.setattr(P, "token_prices", lambda addrs: {a: {"price_usd": 0.99} for a in addrs})   # the API lags by one trade
    chain[TOKEN] = 1.30
    feed[TOKEN] = 1.30
    pb.mark()                                                        # the slow mark reads the pool, not the API
    row = db.one("SELECT status, last_usd, peak_usd FROM paper")
    assert row["status"] == "open" and row["last_usd"] == 1.30 and row["peak_usd"] == 1.38
    chain[TOKEN] = None                                              # the node does not answer: the position waits, the API is still not asked
    pb.mark()
    assert db.one("SELECT status, last_usd FROM paper") == {"status": "open", "last_usd": 1.30}
    chain[TOKEN] = 1.10
    feed[TOKEN] = 1.10
    pb.mark()                                                        # a real fall of 20% from the peak does sell
    assert db.one("SELECT status, reason FROM paper") == {"status": "closed", "reason": "trailing stop 15% below the peak"}


def test_rows_from_before_pools_were_stored_are_still_marked_by_the_price_api(db, feed, monkeypatch):
    monkeypatch.setattr(lab, "pick_arm", lambda db: "costout_1.5x@0m")
    pb = book(db)
    pb.consider(TOKEN, {"score": 80, "verdict": "looks healthy", "metrics": {}})
    feed[TOKEN] = 0.5
    pb.mark()
    assert db.one("SELECT status FROM paper")["status"] == "closed"


def test_legacy_quoted_positions_never_fall_back_to_fabricated_fills(db, feed, monkeypatch):
    pb = enter(db, feed)
    db.x("UPDATE paper SET execution_model='quoted-usdg-v1',marked_ts=1")
    s = pb.summary()
    assert s['unpriced_count'] == 1 and s['unrealized_usd'] is None
    def no_quote(*args, **kwargs):
        raise RuntimeError('quote unavailable')
    monkeypatch.setattr(P.execution, 'exit_quote', no_quote)
    pb.mark(prices={TOKEN: .1}, value=False)
    assert db.one('SELECT status FROM paper')['status'] == 'open'


def test_slow_valuation_cannot_block_exit_or_resurrect_closed_value(db, feed, monkeypatch):
    pb = enter(db, feed)
    waiting, release = threading.Event(), threading.Event()
    original = pb._quote
    def quote(p, amount):
        if threading.current_thread().name == 'slow-value':
            waiting.set()
            assert release.wait(3)
            return 999.0, 0.0
        return original(p, amount)
    monkeypatch.setattr(pb, '_quote', quote)
    slow = threading.Thread(name='slow-value', target=pb.mark)
    slow.start()
    try:
        assert waiting.wait(2)
        feed[TOKEN] = .4
        fast = threading.Thread(target=lambda: pb.mark(prices={TOKEN: .4}, value=False))
        fast.start(); fast.join(1)
        assert not fast.is_alive(), 'valuation blocked the stop'
        row = db.one('SELECT status,liquidation_usd,pnl_usd FROM paper')
        assert row['status'] == 'closed' and row['liquidation_usd'] == 0 and row['pnl_usd'] < -5
    finally:
        release.set(); slow.join(3)
    assert db.one('SELECT liquidation_usd FROM paper')['liquidation_usd'] == 0


def test_slow_entry_does_not_block_existing_position_exit(db, feed, monkeypatch):
    pb = enter(db, feed)
    waiting, release = threading.Event(), threading.Event()
    original = P.execution.entry
    def entry(*args, **kwargs):
        waiting.set(); assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(P.execution, 'entry', entry)
    slow = threading.Thread(target=lambda: pb.enter(OTHER, 'BBB', 1.0, 'rule-a', 'entry'))
    slow.start()
    try:
        assert waiting.wait(2)
        feed[TOKEN] = .4
        fast = threading.Thread(target=lambda: pb.mark(prices={TOKEN: .4}, value=False))
        fast.start(); fast.join(1)
        assert not fast.is_alive()
        assert db.one('SELECT status FROM paper WHERE token=?', (TOKEN,))['status'] == 'closed'
    finally:
        release.set(); slow.join(3)


def test_parallel_marks_never_double_sell_and_other_positions_progress(db, feed, monkeypatch):
    pb = enter(db, feed)
    pb.enter(OTHER, 'BBB', 1.0, 'rule-a', 'entry')
    waiting, release = threading.Event(), threading.Event()
    original = pb._quote
    def quote(p, amount):
        if p['token'] == TOKEN:
            waiting.set(); assert release.wait(3)
        return original(p, amount)
    monkeypatch.setattr(pb, '_quote', quote)
    feed[TOKEN] = feed[OTHER] = .4
    first = threading.Thread(target=lambda: pb.mark(prices={TOKEN: .4}, value=False))
    first.start()
    try:
        assert waiting.wait(2)
        pb.mark(prices={TOKEN: .4, OTHER: .4}, value=False)
        assert db.one('SELECT status FROM paper WHERE token=?', (OTHER,))['status'] == 'closed'
    finally:
        release.set(); first.join(3)
    assert db.one("SELECT COUNT(*) n FROM events WHERE text LIKE 'paper sell:%'")['n'] == 2


def test_failed_exit_is_measured_and_not_filled_at_stop_price(db, feed, monkeypatch):
    pb = enter(db, feed)
    def unavailable(*args):
        raise RuntimeError('unavailable')
    with monkeypatch.context() as m:
        m.setattr(pb, '_quote', unavailable)
        pb.mark(prices={TOKEN: .4}, value=False)
    p = db.one('SELECT * FROM paper')
    assert p['status'] == 'open' and p['exit_quote_failures'] == 1
    assert p['monitor_ts'] and p['exit_trigger_ts'] and p['exit_quote_failed_ts']
    feed[TOKEN] = .35
    pb.mark(prices={TOKEN: .35}, value=False)
    p = db.one('SELECT * FROM paper')
    assert p['status'] == 'closed' and p['pnl_usd'] < -6
    assert p['closed_ts'] >= p['exit_trigger_ts']


def test_stale_mid_response_cannot_overwrite_newer_mark(db, feed):
    pb = enter(db, feed)
    pid = db.one('SELECT id FROM paper')['id']
    pb._mark_position(pid, 1.6, 200)
    pb._mark_position(pid, .4, 100)
    p = db.one('SELECT * FROM paper')
    assert p['status'] == 'open' and p['last_usd'] == 1.6 and p['peak_usd'] == 1.6


def test_entry_shadow_is_frozen_without_vetoing_control_book(db, feed):
    import json
    pb = book(db)
    assert pb.enter(TOKEN, 'AAA', 1, 'rule-a', 'research', features={'snipe_pct': 100})
    p = db.one('SELECT status,entry_shadow FROM paper')
    decision = json.loads(p['entry_shadow'])
    assert p['status'] == 'open' and decision['decision'] == 'skip'
    assert decision['version'] == P.paper_research.FILTER_VERSION
