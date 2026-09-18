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
    assert got == {"swaps": 3, "buys": 2, "usd": pytest.approx(8.0)}
    address, topics, first, last = rpc.asked[0]
    assert address == C.POOL_MANAGER and topics == [W.SWAP_TOPIC, poolstate.pool_id(pk)] and (first, last) == (100, 200)


def test_flow_values_an_eth_pool_at_the_fresh_eth_price(monkeypatch):
    monkeypatch.setattr(W.execution, "eth_usd", lambda strict=False: 2000.0)
    pk = pool(TOKEN, quote=C.ZERO)                                    # ETH is always currency0
    rpc = LogRpc([swap_log(-10 ** 16, 5 * 10 ** 22), swap_log(3 * 10 ** 16, -10 ** 23)])
    assert W.flow(rpc, pk, TOKEN, 1, 2) == {"swaps": 2, "buys": 1, "usd": pytest.approx(80.0)}
    monkeypatch.setattr(W.execution, "eth_usd", lambda strict=False: None)
    assert W.flow(rpc, pk, TOKEN, 1, 2) is None                       # no fresh ETH price: not measured, never guessed


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
