"""The paper book: one row per token, the token's own cost, the close reason kept, totals over the
whole book. The price feed is stubbed; no network."""
import json
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
    def entry(rpc, database, token, dollars, reference=None, **k):
        cost = lab.token_cost(database, token)
        price = reference or prices[token]
        qty = dollars / price / (1 + cost)
        return {'price': price, 'pool': {'cost': cost}, 'minimum_raw': int(qty * 1e18), 'gas_usd': 0,
                'liquidation_usd': qty * price * (1 - cost)}
    def exit_quote(rpc, pool, token, amount, **k):
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
    # the quote is stale but the pool's mid was just read: the breaker counts the position at that mid less the worst
    # round trip, instead of pausing every entry because one provider was slow
    assert s['risk']['allowed'] and s['risk']['loss_usd'] == pytest.approx(10 - 10 / 1.04 * (1 - P.trade_risk.STALE_HAIRCUT), abs=1e-3)
    db.x('UPDATE paper SET monitor_ts=1')                     # and without a fresh mid either: unknown, entries wait
    assert not pb.summary()['risk']['allowed']


def test_a_valuation_asks_kyberswap_and_the_pool_only(db, feed, monkeypatch):
    pb = enter(db, feed)
    seen = []
    exit_quote = P.execution.exit_quote
    monkeypatch.setattr(P.execution, 'exit_quote', lambda *a, **k: seen.append(k.get('providers')) or exit_quote(*a, **k))
    pb.mark()
    assert seen == [P.execution.VALUE_PROVIDERS] == [('kyber',)]


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
    def quote(p, amount, reference=None, providers=None):
        if threading.current_thread().name == 'slow-value':
            waiting.set()
            assert release.wait(3)
            return 999.0, 0.0
        return original(p, amount, reference, providers)
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
    def quote(p, amount, reference=None, providers=None):
        if p['token'] == TOKEN:
            waiting.set(); assert release.wait(3)
        return original(p, amount, reference, providers)
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


# ---- the loss breaker on second-look entries --------------------------------------------------------

def pool_of(token, quote):
    return {'token': token, 'c0': quote, 'c1': token, 'fee': 0, 'tick_spacing': 200, 'hooks': C.HOOK, 'quote': quote}


def lost(db, token, quote, pnl, strategy='rule-a', closed=None, opened=None, live_fill=None):
    """A closed quoted position in a `quote` pool that lost `pnl`. live_fill None: a row from before routing."""
    import json
    closed = closed or int(time.time()) - 60
    db.x("INSERT INTO paper(token,symbol,opened_ts,entry_usd,size_usd,qty,status,closed_ts,pnl_usd,realized_usd,execution_model,pool_key,strategy,live_fill)"
         " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (token, 'L', opened or closed - 600, 1.0, 10.0, 10.0, 'closed', closed, pnl, 10.0 + pnl, 'quoted-pool-v2',
          json.dumps(pool_of(token, quote)), strategy, live_fill))


def test_a_paused_breaker_turns_second_look_entries_away_and_records_them(db, feed):
    pb = book(db)
    db.meta_set('loss_pause_until_paper_live', int(time.time()) + 3600)
    assert not pb.enter(TOKEN, 'AAA', 1.0, 'rule-a', 'second look', features={'snipe_pct': 1})
    assert pb.last_skip == 'loss breaker'
    assert db.one('SELECT COUNT(*) n FROM paper')['n'] == 0
    skip = db.one('SELECT token,strategy,reason,reference FROM paper_skips')
    assert skip == {'token': TOKEN, 'strategy': 'rule-a', 'reason': 'loss breaker', 'reference': 1.0}
    db.meta_set('loss_pause_until_paper_live', 0)
    db.meta_set('loss_pause_until_paper_usdg', int(time.time()) + 3600)     # a pause the USDG-only breaker still holds
    assert not pb.enter(TOKEN, 'AAA', 1.0, 'rule-a', 'second look')
    db.meta_set('loss_pause_until_paper_usdg', 0)
    assert pb.enter(TOKEN, 'AAA', 1.0, 'rule-a', 'second look') and pb.last_skip is None


def test_losses_live_could_not_have_had_do_not_pause_it_but_routed_losses_in_any_pair_do(db, feed):
    pb = book(db)
    lost(db, '0x' + 'e1' * 20, C.ZERO, -12.0)                 # an ETH-pool fill from before routing: live never had it
    lost(db, '0x' + 'e3' * 20, C.ZERO, -12.0, live_fill=0)    # an ETH pool quoted directly while the aggregators were down
    assert pb.enter(OTHER, 'BBB', 1.0, 'rule-a', 'eth candidate', pool=pool_of(OTHER, C.ZERO))   # a route reaches it
    lost(db, '0x' + 'e2' * 20, C.ZERO, -11.0, live_fill=1)    # a routed ETH-pool loss: live would have had it
    third = '0x' + 'cc' * 20
    db.x("INSERT INTO launches(token,symbol) VALUES(?,?)", (third, 'CCC'))
    feed[third] = 1.0
    assert not pb.enter(third, 'CCC', 1.0, 'rule-a', 'usdg candidate', pool=pool_of(third, C.USDG))
    stock = '0x' + 'cd' * 20
    db.x("INSERT INTO launches(token,symbol,pair_symbol) VALUES(?,?,?)", (stock, 'DDD', 'META'))
    assert not pb.enter(stock, 'DDD', 1.0, 'rule-a', 'stock candidate', pool=pool_of(stock, '0x' + 'c0' * 20))
    assert [r['pair'] for r in db.q('SELECT pair FROM paper_skips ORDER BY id')] == ['USDG', 'META']


def test_an_underwater_position_flagged_as_opened_in_a_pause_still_counts_against_the_usdg_breaker(db, feed):
    import json
    pb = book(db)
    token = '0x' + 'e7' * 20
    db.x("INSERT INTO paper(token,symbol,opened_ts,entry_usd,size_usd,qty,status,execution_model,pool_key,strategy,"
         "liquidation_usd,marked_ts,realized_usd,opened_in_pause) VALUES(?,?,?,?,?,?,'open','quoted-pool-v2',?,'rule-a',?,?,0,1)",
         (token, 'U', int(time.time()) - 600, 1.0, 10.0, 10.0, json.dumps(pool_of(token, C.USDG)), 1.0, int(time.time())))
    risk = P.trade_risk.check(db, 'paper', latch=False, scope='live')
    assert risk['loss_usd'] == 9.0 and risk['allowed']                     # out of the evidence, not out of the losses
    lost(db, '0x' + 'e8' * 20, C.USDG, -2.0)
    assert not pb.enter(TOKEN, 'AAA', 1.0, 'rule-a', 'usdg candidate', pool=pool_of(TOKEN, C.USDG))


def test_positions_opened_during_a_past_pause_are_flagged_not_deleted(db):
    import json
    now = int(time.time())
    P.Paper(db)
    db.meta_set('paper_pause_flags', '')                                     # a book from before the flag existed
    lost(db, '0x' + 'e1' * 20, C.USDG, -6.0, closed=now - 20 * 3600)
    lost(db, '0x' + 'e2' * 20, C.USDG, -5.0, closed=now - 19 * 3600)   # the day's losses reach $10 here
    for i, opened in enumerate((now - 19.5 * 3600, now - 10 * 3600)):
        token = '0x' + f'{i + 1:02x}' * 20
        db.x("INSERT INTO paper(token,symbol,opened_ts,entry_usd,size_usd,qty,status,execution_model,pool_key,strategy) VALUES(?,?,?,?,?,?,?,?,?,?)",
             (token, 'X', int(opened), 1.0, 10.0, 10.0, 'open', 'quoted-pool-v2', json.dumps(pool_of(token, C.USDG)), 'rule-a'))
    P.Paper(db)
    flags = {r['token'][:4]: r['opened_in_pause'] for r in db.q("SELECT token,opened_in_pause FROM paper WHERE symbol='X'")}
    assert flags == {'0x01': 0, '0x02': 1}
    assert db.one("SELECT COUNT(*) n FROM paper")['n'] == 4                  # nothing deleted
    row = db.one("SELECT * FROM paper WHERE token=?", ('0x' + '02' * 20,))
    assert not P.live_comparable(row) and P.pair(row) == 'USDG'
    db.x("UPDATE paper SET opened_in_pause=0 WHERE token=?", (row['token'],))
    P.Paper(db)                                                              # the migration runs once
    assert P.live_comparable(db.one("SELECT * FROM paper WHERE token=?", (row['token'],)))


def test_the_summary_puts_what_live_could_trade_next_to_the_whole_book(db, feed):
    pb = book(db)
    now = int(time.time()) - 3 * 86400                        # old enough not to trip the breaker
    lost(db, '0x' + 'e1' * 20, C.ZERO, 8.0, closed=now, strategy='rule-a')      # an ETH winner
    lost(db, '0x' + 'e2' * 20, C.USDG, -2.0, closed=now, strategy='rule-a')     # a USDG loser
    lost(db, '0x' + 'e3' * 20, C.USDG, 3.0, closed=now, strategy='rule-b')      # a USDG winner ...
    lost(db, '0x' + 'e4' * 20, C.USDG, 5.0, closed=now, strategy='rule-b')      # ... and one opened in a pause
    db.x("UPDATE paper SET opened_in_pause=1 WHERE token=?", ('0x' + 'e4' * 20,))
    lost(db, '0x' + 'e5' * 20, C.USDG, 4.0, closed=now, strategy=P.VERDICT_ENTRY)   # bought at the verdict: live never follows
    s = pb.summary()
    assert s['realized_usd'] == 18.0 and s['all_pools']['closed_count'] == 5 and s['all_pools']['win_rate'] == 80
    lc = s['live_comparable']
    assert (lc['closed_count'], lc['realized_usd'], lc['wins'], lc['win_rate']) == (2, 1.0, 1, 50)
    assert {r['rule']: (r['closed_count'], r['realized_usd']) for r in lc['by_rule']} == {'rule-a': (1, -2.0), 'rule-b': (1, 3.0)}
    assert {r['rule']: r['closed_count'] for r in s['all_pools']['by_rule']} == {'rule-a': 2, 'rule-b': 2, P.VERDICT_ENTRY: 1}
    pairs = {p['token'][:4]: (p['pair'], p['live_comparable']) for p in s['closed']}
    assert pairs['0xe1'] == ('ETH', False) and pairs['0xe2'] == ('USDG', True) and pairs['0xe4'] == ('USDG', False)
    assert s['risk_live_comparable']['allowed'] and s['skipped'] == []
    db.x("UPDATE paper SET strategy='quiet-v1', entry_shadow=? WHERE token IN (?,?)",
         ('{"decision": "keep", "version": "entry-risk-shadow-v1"}', '0x' + 'e1' * 20, '0x' + 'e2' * 20))
    kept = next(f for f in pb.summary()['filtered'] if f['rule'] == 'shadow-keep-v1')
    assert kept['all_pools']['closed_count'] == 2 and kept['live_comparable']['closed_count'] == 1   # the ETH one is not


def test_the_rebuilt_pause_lasts_a_day_after_losses_fall_back():
    from wormhole.db import DB
    import tempfile, pathlib
    db = DB(pathlib.Path(tempfile.mkdtemp()) / 't.db')
    P.Paper(db)
    lost(db, '0x' + 'e1' * 20, C.ZERO, -11.0, closed=1000)
    assert P.pause_windows(db, 10.0) == [(1000, 1000 + 2 * 86400)]         # over the limit for a day, paused a day more


# ---- routed fills: any pair --------------------------------------------------------------------------

META = '0x' + 'c0' * 20


def routed_feed(feed, monkeypatch, provider='kyber', live=True, exit_provider='lifi', exit_live=True):
    """The feed's fills, as if a route had made them in a META-paired pool."""
    entry, exit_quote = P.execution.entry, P.execution.exit_quote
    def routed_entry(rpc, database, token, dollars, reference=None, **k):
        q = entry(rpc, database, token, dollars, reference=reference)
        return {**q, 'pool': pool_of(token, META), 'provider': provider, 'live_fill': live,
                'route': {'provider': provider, 'amount_in': 10_000_000, 'out': 10**21, 'to': '0x' + '61' * 20,
                          'compared': [{'provider': 'relay', 'out': '1', 'executable': False}]} if live else None}
    def routed_exit(rpc, pk, token, amount, **k):
        return {**exit_quote(rpc, pk, token, amount), 'provider': exit_provider, 'live_fill': exit_live}
    monkeypatch.setattr(P.execution, 'entry', routed_entry)
    monkeypatch.setattr(P.execution, 'exit_quote', routed_exit)


def test_a_routed_position_keeps_its_pair_and_route_and_counts_as_could_be_real(db, feed, monkeypatch):
    routed_feed(feed, monkeypatch)
    pb = book(db)
    db.x("UPDATE launches SET pair_symbol='META' WHERE token=?", (TOKEN,))
    assert pb.enter(TOKEN, 'AAA', 1.0, 'rule-a', 'second look', pool=pool_of(TOKEN, META))
    row = db.one('SELECT * FROM paper')
    assert (row['pair'], row['route_provider'], row['live_fill']) == ('META', 'kyber', 1)
    assert json.loads(row['route'])['compared'][0]['provider'] == 'relay'
    assert 'META pool, filled via kyber' in db.one("SELECT text FROM events WHERE text LIKE 'paper buy%'")['text']
    s = pb.summary()
    shown = s['open'][0]
    assert shown['pair'] == 'META' and shown['live_comparable'] and shown['route_provider'] == 'kyber' and 'route' not in shown
    assert s['live_comparable']['open_count'] == 1 and s['live_comparable']['by_pair'][0]['pair'] == 'META'
    feed[TOKEN] = .5                                                    # the stop: sold by the best route, recorded
    pb.mark(prices={TOKEN: .5}, value=False)
    row = db.one('SELECT * FROM paper')
    assert row['status'] == 'closed' and row['exit_provider'] == 'lifi' and row['fallback_fills'] == 0
    assert 'via lifi' in db.one("SELECT text FROM events WHERE text LIKE 'paper sell%'")['text']


def test_a_direct_eth_fill_while_the_aggregators_were_down_is_marked_and_is_learning_data(db, feed, monkeypatch):
    routed_feed(feed, monkeypatch, provider='pool-eth', live=False, exit_provider='pool-eth', exit_live=False)
    pb = book(db)
    assert pb.enter(TOKEN, 'AAA', 1.0, 'rule-a', 'second look', pool=pool_of(TOKEN, C.ZERO))
    row = db.one('SELECT * FROM paper')
    assert (row['route_provider'], row['live_fill']) == ('pool-eth', 0) and not P.live_comparable(row)
    feed[TOKEN] = .5
    pb.mark(prices={TOKEN: .5}, value=False)
    assert db.one('SELECT fallback_fills FROM paper')['fallback_fills'] == 1
    s = pb.summary()
    assert s['live_comparable']['closed_count'] == 0 and s['all_pools']['closed_count'] == 1


def test_an_exit_is_asked_for_near_the_mid_that_triggered_it(db, feed, monkeypatch):
    pb = enter(db, feed)
    seen = []
    exit_quote = P.execution.exit_quote
    monkeypatch.setattr(P.execution, 'exit_quote', lambda *a, **k: seen.append(k) or exit_quote(*a, **k))
    pb.mark(prices={TOKEN: .5}, value=False)
    assert seen and seen[0]['reference'] == .5 and seen[0]['fallback'] and seen[0]['cached_prices']
