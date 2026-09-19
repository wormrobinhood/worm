"""The second look: what gets watched, how a look judges the path, that a pass buys once on paper at the
pool's own mid, and that open positions are re-priced from the chain between the slow marks."""
import json

import pytest

from wormhole import config as C, lab, paper as P, poolstate, watch as W

T0 = 1_800_000_000
TOKEN = "0x" + "a1" * 20
OTHER = "0x" + "b2" * 20


def pool(token, quote=C.USDG):
    c0, c1 = sorted([token, quote], key=lambda a: int(a, 16))
    return {"token": token, "c0": c0, "c1": c1, "fee": 0, "tick_spacing": 200, "hooks": C.HOOK, "quote": quote}


RULE = {"name": "test-look", "looks": [30, 60, 120],
        "conditions": [{"feature": "creator_tax_bps", "op": "<=", "value": 200},
                       {"feature": "creator_prev_launches", "op": "<=", "value": 4},
                       {"feature": "ret_p0", "op": ">=", "value": -0.20},
                       {"feature": "dd_peak", "op": ">=", "value": -0.35},
                       {"feature": "moves_15m", "op": ">=", "value": 5}]}


@pytest.fixture(autouse=True)
def one_rule(monkeypatch):
    """The mechanics are tested against one fixed rule; the rules in production have their own tests below."""
    monkeypatch.setattr(W, "STRATEGIES", [RULE])
    monkeypatch.setattr(W, "STRATEGY", RULE)


@pytest.fixture
def chain(monkeypatch, db):
    """Pool mids and fills come from this dict instead of the chain."""
    mids = {}
    monkeypatch.setattr(poolstate, "mids", lambda rpc, pools, eth: {t: mids.get(t) for t in pools})
    monkeypatch.setattr(W, "eth_usd_last", lambda: 2500.0)
    from wormhole import trader
    monkeypatch.setattr(trader, "pool_key", lambda rpc, database, token: pool(token))

    def entry(rpc, database, token, dollars, quotes=None, reference=None):
        qty = dollars / reference / 1.02
        return {"price": reference, "pool": pool(token), "minimum_raw": int(qty * 1e18), "gas_usd": 0.0,
                "liquidation_usd": qty * reference * 0.98}
    monkeypatch.setattr(P.execution, "entry", entry)
    monkeypatch.setattr(P.execution, "exit_quote",
                        lambda rpc, pk, token, amount, quotes=None: {"minimum_usd": amount / 1e18 * mids[token] * 0.98, "gas_usd": 0.0})
    return mids


def verdict(**metrics):
    return {"score": 20, "verdict": "avoid", "metrics": {"creator_tax_bps": 100, "creator_prev_launches": 0, **metrics}}


def walk(w, mids, token, prices, start=T0, step=60):
    """Feed one mid a minute; returns the time after the last step."""
    now = start
    for p in prices:
        mids[token] = p
        w.sampled = 0
        w.step(now)
        now += step
    return now


def test_partial_and_own_tokens_are_not_watched(db, monkeypatch):
    assert not W.add(db, TOKEN, {"score": 50, "verdict": "mixed", "metrics": {"partial": True}}, now=T0)
    monkeypatch.setattr(C, "TOKEN", OTHER)
    assert not W.add(db, OTHER, verdict(), now=T0)
    assert W.add(db, TOKEN, verdict(), "AAA", now=T0)
    assert not W.add(db, TOKEN, verdict(), "AAA", now=T0 + 600)          # a rescan never restarts the clock
    assert db.one("SELECT t0, status FROM watch WHERE token=?", (TOKEN,)) == {"t0": T0, "status": "new"}


def test_a_pool_the_book_cannot_trade_is_not_watched(db, chain, monkeypatch):
    from wormhole import trader
    monkeypatch.setattr(trader, "pool_key", lambda rpc, database, token: pool(token, quote="0x" + "cd" * 20))
    W.add(db, TOKEN, verdict(), "AAA", now=T0)
    W.Watcher(None, db, P.Paper(db)).step(T0 + 1)
    assert db.one("SELECT status FROM watch")["status"] == "unsupported"


def test_nothing_is_bought_before_the_first_look(db, chain):
    W.add(db, TOKEN, verdict(), "AAA", now=T0)
    w = W.Watcher(None, db, P.Paper(db))
    walk(w, chain, TOKEN, [1.0] * 29)
    assert db.one("SELECT COUNT(*) n FROM paper")["n"] == 0
    assert db.one("SELECT status, p0 FROM watch") == {"status": "watching", "p0": 1.0}


def test_a_token_that_held_up_is_bought_once_at_the_pool_mid(db, chain):
    W.add(db, TOKEN, verdict(), "AAA", now=T0)
    w = W.Watcher(None, db, P.Paper(db))
    prices = [1.0 + 0.002 * (i % 7) for i in range(31)]               # alive: the mid keeps moving, no collapse
    now = walk(w, chain, TOKEN, prices)
    row = db.one("SELECT * FROM paper")
    assert row["status"] == "open" and row["strategy"] == W.STRATEGY["name"] and row["entry_usd"] == prices[-1]
    assert row["policy"] == lab.DEFAULT
    assert db.one("SELECT status FROM watch")["status"] == "entered"
    assert db.one("SELECT t0, p0 FROM lab_cases WHERE token=?", (TOKEN,)) == {"t0": now - 60, "p0": prices[-1]}
    walk(w, chain, TOKEN, [1.0] * 40, start=now)                      # later looks never buy it again
    assert db.one("SELECT COUNT(*) n FROM paper")["n"] == 1


@pytest.mark.parametrize("prices,metrics,why", [
    ([1.0] * 5 + [0.5 + 0.001 * (i % 5) for i in range(26)], {}, "ret_p0"),             # collapsed after the verdict
    ([1.0 + 0.002 * (i % 7) for i in range(31)], {"creator_tax_bps": 500}, "creator_tax_bps"),
    ([1.0 + 0.002 * (i % 7) for i in range(31)], {"creator_prev_launches": 40}, "creator_prev_launches"),
    ([1.0] * 31, {}, "moves_15m"),                                                         # nobody trades it any more
])
def test_a_failed_look_buys_nothing_and_says_why(db, chain, prices, metrics, why):
    W.add(db, TOKEN, verdict(**metrics), "AAA", now=T0)
    walk(W.Watcher(None, db, P.Paper(db)), chain, TOKEN, prices)
    assert db.one("SELECT COUNT(*) n FROM paper")["n"] == 0
    row = db.one("SELECT status, note, looks_done FROM watch")
    assert row["status"] == "watching" and why in row["note"] and json.loads(row["looks_done"]) == [30]


def test_a_later_look_can_still_buy(db, chain):
    W.add(db, TOKEN, verdict(), "AAA", now=T0)
    w = W.Watcher(None, db, P.Paper(db))
    now = walk(w, chain, TOKEN, [1.0] * 31)                           # flat and silent at 30 min: passed over
    walk(w, chain, TOKEN, [1.0 + 0.003 * (i % 5) for i in range(30)], start=now)
    assert db.one("SELECT strategy FROM paper")["strategy"] == W.STRATEGY["name"]
    assert "look 60" in db.one("SELECT note FROM watch")["note"]


def test_a_missing_price_waits_for_the_next_minute(db, chain):
    W.add(db, TOKEN, verdict(), "AAA", now=T0)
    w = W.Watcher(None, db, P.Paper(db))
    now = walk(w, chain, TOKEN, [1.0 + 0.002 * (i % 7) for i in range(30)])
    chain[TOKEN] = None                                               # the node did not answer at the look
    w.sampled = 0
    w.step(now)
    assert json.loads(db.one("SELECT looks_done FROM watch")["looks_done"]) == []
    walk(w, chain, TOKEN, [1.004], start=now + 60)
    assert db.one("SELECT COUNT(*) n FROM paper")["n"] == 1


def test_the_list_lets_go_after_the_last_look(db, chain):
    W.add(db, TOKEN, verdict(), "AAA", now=T0)
    w = W.Watcher(None, db, P.Paper(db))
    walk(w, chain, TOKEN, [1.0] * 125)
    assert db.one("SELECT status FROM watch")["status"] == "done"
    assert db.one("SELECT COUNT(*) n FROM paper")["n"] == 0


def test_open_positions_are_marked_from_the_chain_between_slow_marks(db, chain, monkeypatch):
    W.add(db, TOKEN, verdict(), "AAA", now=T0)
    book = P.Paper(db)
    w = W.Watcher(None, db, book)
    now = walk(w, chain, TOKEN, [1.0 + 0.002 * (i % 7) for i in range(31)])
    monkeypatch.setattr(P.time, "time", lambda: now)
    entry = db.one("SELECT entry_usd FROM paper")["entry_usd"]
    stop = lab.parse_arm(lab.DEFAULT)[0]["stop"]
    chain[TOKEN] = entry * (1 + stop) * 0.99                          # through the stop between two samples
    w.sampled = now                                                   # the watch list is not due: only the fast book runs
    w.step(now + 15)
    row = db.one("SELECT status, reason, pnl_usd FROM paper")
    assert row["status"] == "closed" and row["reason"].startswith("stop at") and row["pnl_usd"] < 0


def test_summary_counts_without_naming_tokens(db, chain):
    W.add(db, TOKEN, verdict(), "AAA")
    s = W.summary(db)
    assert s["watching"] == 1 and s["strategies"][0]["name"] == W.STRATEGY["name"] and TOKEN not in json.dumps(s)


# ---- swap flow: the pool's own logs ---------------------------------------------------------------

def swap_log(amount0, amount1):
    word = lambda v: (v % (1 << 256)).to_bytes(32, "big").hex()
    return {"data": "0x" + word(amount0) + word(amount1) + "00" * 32 * 4, "blockNumber": "0x10"}


class LogRpc:
    def __init__(self, logs, head=1_000_000):
        self.logs, self.head, self.asked = logs, head, []

    def block_number(self):
        return self.head

    def get_logs(self, address, topics, first, last, chunk, cap=None):
        self.asked.append((address, topics, first, last))
        return iter(self.logs)


def test_flow_counts_buys_and_usd_volume_for_a_usdg_pool():
    pk = pool(TOKEN)                                                  # TOKEN sorts after USDG: the token is currency1
    assert pk["c1"] == TOKEN
    rpc = LogRpc([swap_log(-5_000_000, 10 ** 21), swap_log(2_000_000, -4 * 10 ** 20), swap_log(-1_000_000, 10 ** 20)])
    got = W.flow(rpc, pk, TOKEN, 100, 200)
    assert W.flow(rpc, {**pk, "quote": "0x" + "cd" * 20}, TOKEN, 100, 200) is None
    assert got == {"swaps": 3, "buys": 2, "usd": pytest.approx(8.0)}
    address, topics, first, last = rpc.asked[0]
    assert address == C.POOL_MANAGER and topics == [W.SWAP_TOPIC, poolstate.pool_id(pk)] and (first, last) == (100, 200)


def test_flow_values_an_eth_pool_at_the_fresh_eth_price(monkeypatch):
    pk = pool(TOKEN, quote=C.ZERO)                                    # ETH is always currency0
    rpc = LogRpc([swap_log(-10 ** 16, 5 * 10 ** 22), swap_log(3 * 10 ** 16, -10 ** 23)])
    assert W.flow(rpc, pk, TOKEN, 1, 2, 2000.0) == {"swaps": 2, "buys": 1, "usd": pytest.approx(80.0)}
    assert W.flow(rpc, pk, TOKEN, 1, 2, None) == {"swaps": 2, "buys": 1, "usd": None}   # the counts stand; dollars are never guessed


def test_flow_features_reach_the_rule(db, chain, monkeypatch):
    monkeypatch.setattr(W, "STRATEGIES", [{"name": "flow-test", "looks": [30], "conditions": [
        {"feature": "vol_ratio", "op": ">=", "value": 0.5}, {"feature": "buy_share_15m", "op": ">=", "value": 0.5}]}])
    db.x("INSERT INTO launches(token,symbol,grad_block) VALUES(?,?,?)", (TOKEN, "AAA", 900_000))
    W.add(db, TOKEN, verdict(), "AAA", now=T0)
    rpc = LogRpc([swap_log(-5_000_000, 10 ** 21), swap_log(-5_000_000, 10 ** 21), swap_log(2_000_000, -4 * 10 ** 20)])
    walk(W.Watcher(rpc, db, P.Paper(db)), chain, TOKEN, [1.0 + 0.002 * (i % 7) for i in range(31)])
    assert db.one("SELECT strategy FROM paper")["strategy"] == "flow-test"
    first = json.loads(db.one("SELECT flow0 FROM watch")["flow0"])
    assert first["swaps"] == 3 and len(rpc.asked) == 2                # the first window is read once and kept
    assert rpc.asked[1][2] == 900_000


# ---- the hooks the trader hangs on the watcher ----------------------------------------------------

def test_the_entry_hook_runs_after_a_paper_entry_and_cannot_break_the_watcher(db, chain):
    W.add(db, TOKEN, verdict(), "AAA", now=T0)
    calls = []
    def hook():
        calls.append(db.one("SELECT COUNT(*) n FROM paper WHERE status='open'")["n"])
        raise RuntimeError("the trader is having a bad day")
    w = W.Watcher(None, db, P.Paper(db), on_entry=hook)
    walk(w, chain, TOKEN, [1.0 + 0.002 * (i % 7) for i in range(31)])
    assert calls == [1]                                               # once, and the paper row was already there
    assert db.one("SELECT status FROM watch")["status"] == "entered"


def test_open_trader_positions_get_fresh_pool_prices_every_step(db, chain):
    from wormhole import trader
    trader.ensure_tables(db)
    db.x("INSERT INTO positions(token,symbol,status,mode,pool_key) VALUES(?,?,'open','live',?)", (TOKEN, "AAA", json.dumps(pool(TOKEN))))
    db.x("INSERT INTO positions(token,symbol,status,mode,pool_key) VALUES(?,?,'closed','live',?)", (OTHER, "BBB", json.dumps(pool(OTHER))))
    seen = []
    w = W.Watcher(None, db, P.Paper(db), on_prices=seen.append)
    chain[TOKEN], chain[OTHER] = 0.5, 0.7
    w.sampled = T0
    w.step(T0 + 15)
    assert seen == [{TOKEN: 0.5}]
    chain[TOKEN] = None                                               # no answer from the node: no price, no call
    w.step(T0 + 30)
    assert len(seen) == 1


def test_the_first_rule_that_passes_names_the_position(db, chain, monkeypatch):
    strict = {"name": "strict", "looks": [30], "conditions": [{"feature": "ret_p0", "op": ">=", "value": 5.0}]}
    easy = {"name": "easy", "looks": [30], "conditions": [{"feature": "ret_p0", "op": ">=", "value": -0.5}]}
    later = {"name": "later", "looks": [60], "conditions": [{"feature": "ret_p0", "op": ">=", "value": -0.5}]}
    monkeypatch.setattr(W, "STRATEGIES", [strict, easy, later])
    assert W.all_looks() == [30, 60] and W.watch_for_s() == 60 * 60 + 600
    W.add(db, TOKEN, verdict(), "AAA", now=T0)
    walk(W.Watcher(None, db, P.Paper(db)), chain, TOKEN, [1.0 + 0.002 * (i % 7) for i in range(31)])
    assert db.one("SELECT strategy FROM paper")["strategy"] == "easy"
    assert "easy entered" in db.one("SELECT note FROM watch")["note"]


def test_a_collapsed_pool_leaves_the_list(db, chain):
    W.add(db, TOKEN, verdict(), "AAA", now=T0)
    w = W.Watcher(None, db, P.Paper(db))
    now = walk(w, chain, TOKEN, [1.0] * 3 + [0.02] * 26)               # -98% but only 29 minutes old: still watched
    assert db.one("SELECT status FROM watch")["status"] == "watching"
    walk(w, chain, TOKEN, [0.02] * 3, start=now)
    row = db.one("SELECT status, note FROM watch")
    assert row["status"] == "done" and "collapsed" in row["note"]


# ---- the rules in production ----------------------------------------------------------------------

ALIVE = {"creator_prev_launches": 0, "creator_tax_bps": 100, "swaps_15m": 60, "ret_60m": 0.04, "ret_15m": 0.02,
         "fdv_usd": 40_000, "ret_p0": -0.3, "vol_15m_usd": 3000.0}


@pytest.fixture
def rules(monkeypatch):
    monkeypatch.undo()                                   # the module's own rule set, not the test rule
    return {r["name"]: r for r in W.STRATEGIES}


def test_rule_names_are_unique_and_every_feature_is_one_the_watcher_measures(rules):
    assert len(rules) == len(W.STRATEGIES) and all(name.endswith(("-v1", "-v2", "-v3")) for name in rules)
    measured = {"ret_p0", "dd_peak", "rebound", "moves_15m", "ret_60m", "ret_15m", "ret_5m", "age_min", "fdv_usd",
                "sample_bucket", "swaps_15m", "vol_15m_usd", "buy_share_15m", "vol_ratio", "creator_tax_bps", "creator_prev_launches",
                "creator_rugged", "top10_pct", "holders", "snipe_pct", "fleet_pct", "launch_to_grad_s", "score",
                "losing_pct", "crowd_history"}
    for rule in W.STRATEGIES:
        # the first hour loses: no rule looks that early. One exception, by measurement: what the crowd's record says
        # is worth 7-12 points in the first hour and nothing after the second, so that rule has to look early or not at all
        early_ok = rule["name"].startswith("clean-crowd") and min(rule["looks"]) >= 30
        assert rule["looks"] == sorted(rule["looks"]) and (min(rule["looks"]) >= 60 or early_ok)
        assert {c["feature"] for c in rule["conditions"]} <= measured and all(c["op"] in W.OPS for c in rule["conditions"])


def test_quiet_wants_a_cheap_token_that_still_trades_but_is_no_longer_churned(rules):
    rule = rules["quiet-v1"]
    calm = {**ALIVE, "swaps_15m": 6, "sample_bucket": 10}
    assert rule["looks"] == [120] and W.passes(rule, calm) == (True, "")
    for change, why in (({"swaps_15m": 0}, "swaps_15m"), ({"swaps_15m": 60}, "swaps_15m"), ({"creator_tax_bps": 200}, "creator_tax_bps"),
                        ({"creator_prev_launches": 40}, "creator_prev_launches"), ({"sample_bucket": 80}, "sample_bucket"),
                        ({"swaps_15m": None}, "swaps_15m")):
        ok, said = W.passes(rule, {**calm, **change})
        assert not ok and why in said


def test_runner_wants_a_cheap_token_worth_twice_its_graduation_value_at_four_hours(rules):
    rule = rules["runner-v1"]
    grown = {**ALIVE, "fdv_usd": 250_000, "swaps_15m": 300}
    assert rule["looks"] == [240] and W.passes(rule, grown) == (True, "")
    assert not W.passes(rule, {**grown, "fdv_usd": 60_000})[0] and not W.passes(rule, {**grown, "creator_tax_bps": 300})[0]
    assert not W.passes(rule, {**grown, "swaps_15m": 0})[0]


def test_no_rule_buys_a_pool_the_bots_are_still_churning_early(rules):
    busy = {**ALIVE, "swaps_15m": 400, "fdv_usd": 60_000, "sample_bucket": 5}
    assert not any(W.passes(rule, busy)[0] for rule in W.STRATEGIES)


def test_the_sample_bucket_is_fixed_per_token_and_spreads_evenly():
    assert W.sample_bucket(TOKEN) == W.sample_bucket(TOKEN.upper().replace("0X", "0x")) and 0 <= W.sample_bucket(TOKEN) <= 99
    buckets = [W.sample_bucket("0x" + f"{i:040x}") for i in range(3000)]
    assert 0.28 < sum(b <= 33 for b in buckets) / len(buckets) < 0.40


def test_what_the_look_measured_is_kept_on_the_paper_row(db, chain):
    W.add(db, TOKEN, verdict(), "AAA", now=T0)
    walk(W.Watcher(None, db, P.Paper(db)), chain, TOKEN, [1.0 + 0.002 * (i % 7) for i in range(31)])
    kept = json.loads(db.one("SELECT features FROM paper")["features"])
    assert kept["creator_tax_bps"] == 100 and "ret_p0" in kept and "fdv_usd" in kept and kept["moves_15m"] >= 5


def test_an_unknown_creator_tax_is_read_from_the_launch_and_otherwise_fails_the_rule(db, rules):
    db.x("INSERT INTO launches(token,symbol,creator_tax_bps) VALUES(?,?,?)", (TOKEN, "AAA", 400))
    W.add(db, TOKEN, {"score": 20, "verdict": "avoid", "metrics": {}}, now=T0)
    assert json.loads(db.one("SELECT metrics FROM watch WHERE token=?", (TOKEN,))["metrics"])["creator_tax_bps"] == 400
    W.add(db, OTHER, {"score": 20, "verdict": "avoid", "metrics": {}}, "BBB", now=T0)
    assert json.loads(db.one("SELECT metrics FROM watch WHERE token=?", (OTHER,))["metrics"])["creator_tax_bps"] is None
    ok, why = W.passes(rules["quiet-v1"], {**ALIVE, "swaps_15m": 5, "sample_bucket": 1, "creator_tax_bps": None})
    assert not ok and "creator_tax_bps" in why


def test_a_look_whose_flow_could_not_be_read_waits_instead_of_being_spent(db, chain, monkeypatch):
    monkeypatch.setattr(W, "STRATEGIES", [{"name": "flow-test", "looks": [30], "conditions": [{"feature": "swaps_15m", "op": ">=", "value": 2}]}])
    db.x("INSERT INTO launches(token,symbol,grad_block) VALUES(?,?,?)", (TOKEN, "AAA", 900_000))
    W.add(db, TOKEN, verdict(), "AAA", now=T0)

    class Down(LogRpc):
        def get_logs(self, *a, **k):
            raise RuntimeError("429 Too Many Requests")
    w = W.Watcher(Down([]), db, P.Paper(db))
    now = walk(w, chain, TOKEN, [1.0 + 0.002 * (i % 7) for i in range(33)])
    assert json.loads(db.one("SELECT looks_done FROM watch")["looks_done"]) == []      # three minutes of outage: still due
    w.rpc = LogRpc([swap_log(-5_000_000, 10 ** 21)] * 3)
    walk(w, chain, TOKEN, [1.004], start=now)
    assert db.one("SELECT strategy FROM paper")["strategy"] == "flow-test"
    db.x("DELETE FROM paper"); db.x("DELETE FROM watch"); db.x("DELETE FROM watch_ticks")
    W.add(db, TOKEN, verdict(), "AAA", now=T0)
    w = W.Watcher(Down([]), db, P.Paper(db))
    walk(w, chain, TOKEN, [1.0 + 0.002 * (i % 7) for i in range(42)])                 # past the retry window: the look is spent
    assert json.loads(db.one("SELECT looks_done FROM watch")["looks_done"]) == [30] and not db.q("SELECT 1 FROM paper")


def test_clean_crowd_wants_a_cheap_token_whose_buyers_have_no_losing_record(rules):
    rule = rules["clean-crowd-v1"]
    clean = {**ALIVE, "losing_pct": 2.0, "crowd_history": 400, "moves_15m": 3, "sample_bucket": 70}
    assert rule["looks"] == [30] and W.passes(rule, clean) == (True, "")
    for change, why in (({"losing_pct": 12.0}, "losing_pct"), ({"losing_pct": None}, "losing_pct"), ({"crowd_history": 40}, "crowd_history"),
                        ({"moves_15m": 0}, "moves_15m"), ({"creator_tax_bps": 300}, "creator_tax_bps"),
                        ({"sample_bucket": 20}, "sample_bucket")):          # the third of tokens the quiet rule takes is left to it
        ok, said = W.passes(rule, {**clean, **change})
        assert not ok and said.startswith(why)


def test_the_crowd_read_travels_from_the_verdict_to_the_look(db):
    W.add(db, TOKEN, {"score": 50, "verdict": "mixed", "metrics": {"creator_tax_bps": 0, "losing_pct": 3.5, "crowd_history": 220}}, symbol="T", now=1000)
    kept = json.loads(db.one("SELECT metrics FROM watch WHERE token=?", (TOKEN,))["metrics"])
    assert kept["losing_pct"] == 3.5 and kept["crowd_history"] == 220
