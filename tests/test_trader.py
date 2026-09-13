"""The trader against a fake RPC: caps inside a cycle, PENDING before broadcast, reconcile by receipt,
the live-sell gate, skips that leave the window, fresh ETH prices. Nothing is signed or sent."""
import json
import time

import pytest
from eth_abi import decode

from wormhole import config as C
from wormhole import lab
from wormhole import trader
from wormhole.chain import RpcError
from wormhole.pons import TRANSFER

NOW = int(time.time())
RUNWAY = {"can_invest": True, "surplus_usd": 1000.0}


@pytest.fixture(autouse=True)
def trading_on(monkeypatch):
    """The policy switch is off by default; these tests exercise the trader as if the creator had turned it on."""
    monkeypatch.setattr(C, "TRADING", True)
OUT = 10 ** 21                                  # the Quoter's answer: 1000 tokens for the buy
OTHER_ASSET = "0x" + "ab" * 20
HASH = "0x" + "cd" * 32


def tok(i):
    return "0x" + f"{i:040x}"


def pad(addr):
    return "0x" + "00" * 12 + addr[2:].lower()


class FakeRpc:
    def __init__(self, out=OUT, receipts=None, logs_error=None):
        self.out, self.receipts, self.logs_error, self.calls = out, receipts or {}, logs_error, []

    def eth_call(self, to, data, block="latest"):
        self.calls.append(("eth_call", to))
        return "0x" + (self.out.to_bytes(32, "big") + (100000).to_bytes(32, "big")).hex()

    def call(self, method, params, retries=4):
        self.calls.append((method, params))
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        return "0x"

    def get_logs(self, *a, **k):
        if self.logs_error:
            raise self.logs_error
        return iter([])


class FakeAcct:
    address = C.WALLET


class Sender:
    """A stand-in for tx.send_tx: records calls, returns a hash, or raises before any broadcast."""

    def __init__(self, error=None):
        self.error, self.calls = error, []

    def __call__(self, rpc, acct, to, data="0x", value=0, gas=None, wait=True, say=None):
        self.calls.append({"to": to, "value": value, "wait": wait, "data": data})
        if self.error:
            raise self.error
        return HASH, None


def receipt(token, wallet, amounts, status="0x1"):
    logs = [{"address": token, "topics": [TRANSFER.topic, pad(C.HOOK), pad(wallet)], "data": "0x" + f"{a:064x}"} for a in amounts]
    return {"status": status, "blockNumber": "0x10", "logs": logs}


@pytest.fixture
def tdb(db, monkeypatch):
    trader.ensure_tables(db)
    lab.ensure_tables(db)
    monkeypatch.setattr(trader, "eth_usd", lambda strict=False: 2500.0)
    monkeypatch.setattr(trader, "token_prices", lambda addrs: {})
    return db


def candidate(db, i, quote=C.ZERO, scored_at=None, partial=0, pool=True):
    t = tok(i)
    db.x("INSERT INTO launches(token,symbol,graduated,grad_block,creator_tax_bps,curve_fee_bps) VALUES(?,?,1,?,100,100)", (t, f"T{i}", 5000 + i))
    db.x("INSERT INTO scores(token,score,verdict,scored_at,partial) VALUES(?,?,?,?,?)", (t, 80, "looks healthy", scored_at or NOW - i, partial))
    if pool:
        c0, c1 = (quote, t) if quote < t else (t, quote)
        db.x("INSERT INTO pools(token,c0,c1,fee,tick_spacing,hooks,quote) VALUES(?,?,?,?,?,?,?)", (t, c0, c1, 3000, 60, C.HOOK, quote))
    return t


def position(db, token, mode, entry, qty=1000.0, policy=lab.DEFAULT, sym="POS"):
    db.x("INSERT INTO positions(token,symbol,opened_ts,entry_usd,size_usd,qty,qty_left,peak_usd,status,mode,quote,policy,tp_done)"
         " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (token, sym, NOW - 600, entry, 10.0, qty, qty, entry, "open", mode, "ETH", policy, "[]"))


def events(db, like):
    return db.q("SELECT * FROM events WHERE text LIKE ? ORDER BY id", (like,))


# ---- encoding and sizing ------------------------------------------------------------------------

def test_swap_calldata_roundtrip():
    token = tok(7)
    pk = {"c0": C.ZERO, "c1": token, "fee": 3000, "tick_spacing": 60, "hooks": C.HOOK, "quote": C.ZERO}
    data = trader.swap_calldata(pk, True, 4 * 10 ** 15, 970, C.ZERO, token)
    assert data.startswith("0x3593564c")
    commands, inputs, deadline = decode(["bytes", "bytes[]", "uint256"], bytes.fromhex(data[10:]))
    assert commands == b"\x10" and len(inputs) == 1 and time.time() + 500 < deadline <= time.time() + 600
    actions, params = decode(["bytes", "bytes[]"], inputs[0])
    assert actions == b"\x06\x0c\x0f"
    key, zfo, amount_in, min_out, price_limit, hook_data = decode([trader.SWAP_T], params[0])[0]
    assert price_limit == 0                                   # no price limit: min_out is the guard
    assert key == (C.ZERO, token, 3000, 60, C.HOOK) and zfo is True and amount_in == 4 * 10 ** 15 and min_out == 970
    assert decode(["address", "uint256"], params[1]) == (C.ZERO, 4 * 10 ** 15)
    assert decode(["address", "uint256"], params[2]) == (token, 970)


def test_quote_units(monkeypatch):
    monkeypatch.setattr(trader, "eth_usd", lambda strict=False: 2500.0)
    assert trader.quote_units({"quote": C.USDG}, 10.0) == (10_000_000, "USDG")
    assert trader.quote_units({"quote": C.ZERO}, 10.0) == (int(10 / 2500 * 1e18), "ETH")
    assert trader.quote_units({"quote": OTHER_ASSET}, 10.0) == (None, None)


def test_eth_sizing_needs_fresh_price(tdb, monkeypatch):
    monkeypatch.setattr(trader, "eth_usd", lambda strict=False: None)          # stale or never fetched
    assert trader.quote_units({"quote": C.ZERO}, 10.0) == (None, None)
    monkeypatch.setattr(trader, "eth_usd", lambda: 2500.0)                     # a feed without the strict flag
    assert trader.quote_units({"quote": C.ZERO}, 10.0) == (None, None)
    monkeypatch.setattr(trader, "eth_usd", lambda strict=False: None)
    candidate(tdb, 1)
    rpc = FakeRpc()
    for _ in range(2):
        trader.decide(rpc, tdb, RUNWAY, False)
    assert tdb.one("SELECT COUNT(*) n FROM trades")["n"] == 0 and rpc.calls == []
    assert len(events(tdb, "skip $T1: no fresh ETH price")) == 1              # said once an hour, not once a cycle
    assert tdb.one("SELECT status FROM trade_intents WHERE token=?", (tok(1),))["status"] is None   # not a permanent skip
    assert [r["token"] for r in trader.candidates(tdb)] == [tok(1)]


# ---- demo buys and the caps -----------------------------------------------------------------------

def test_decide_demo_buys_and_records(tdb):
    t = candidate(tdb, 1)
    rpc = FakeRpc()
    trader.decide(rpc, tdb, RUNWAY, False)
    tr = tdb.one("SELECT * FROM trades")
    assert tr["mode"] == "demo" and tr["side"] == "buy" and tr["usd"] == 10.0 and tr["qty"] == 1000.0 and tr["price_usd"] == 0.01
    p = tdb.one("SELECT * FROM positions WHERE token=?", (t,))
    assert p["status"] == "open" and p["mode"] == "demo" and p["qty"] == 1000.0 and p["policy"] == lab.DEFAULT and p["quote"] == "ETH"
    assert "demo: would buy $10.00 of $T1" in events(tdb, "demo: would buy%")[0]["text"]
    assert trader.candidates(tdb) == []


def test_decide_respects_gates(tdb):
    candidate(tdb, 1)
    rpc = FakeRpc()
    trader.decide(rpc, tdb, {"can_invest": False, "surplus_usd": 1000.0}, False)
    trader.decide(rpc, tdb, RUNWAY, False, ready={"ready": False, "score": 40})
    trader.decide(rpc, tdb, {"can_invest": True, "surplus_usd": 5.0}, False)      # 10% of the surplus is under $1
    assert tdb.one("SELECT COUNT(*) n FROM trades")["n"] == 0 and rpc.calls == []
    trader.decide(rpc, tdb, RUNWAY, False, ready={"ready": True, "score": 90})
    assert tdb.one("SELECT COUNT(*) n FROM trades")["n"] == 1


def test_decide_caps_open_positions_within_a_cycle(tdb):
    for k in range(4):
        position(tdb, tok(100 + k), "demo", 0.01)
    for i in (1, 2, 3):
        candidate(tdb, i)
    trader.decide(FakeRpc(), tdb, RUNWAY, False)
    assert tdb.one("SELECT COUNT(*) n FROM positions WHERE status='open'")["n"] == trader.MAX_OPEN == 5
    assert tdb.one("SELECT COUNT(*) n FROM trades")["n"] == 1


def test_decide_caps_daily_spend_within_a_cycle(tdb):
    for i in (1, 2, 3):
        candidate(tdb, i)
    tdb.x("INSERT INTO trades(ts,token,symbol,side,usd,mode,note) VALUES(?,?,?,?,?,?,?)", (NOW - 3600, tok(50), "OLD", "buy", 15.0, "demo", "quote"))
    trader.decide(FakeRpc(), tdb, RUNWAY, False)
    assert tdb.one("SELECT SUM(usd) s FROM trades WHERE side='buy'")["s"] == 25.0          # 15 + one $10 buy; a second would pass $30
    assert tdb.one("SELECT COUNT(*) n FROM positions")["n"] == 1
    trader.decide(FakeRpc(), tdb, RUNWAY, False)
    assert tdb.one("SELECT SUM(usd) s FROM trades WHERE side='buy'")["s"] == 25.0
    tdb.x("INSERT INTO trades(ts,token,symbol,side,usd,mode,note) VALUES(?,?,?,?,?,?,?)", (NOW - 3600, tok(51), "OLD", "buy", 8.0, "live", "FAILED: insufficient ETH"))
    assert trader.spent_today(tdb) == 25.0                                                 # a buy that never went out cost nothing


# ---- live buys: pending, failed, reconciled, gated ----------------------------------------------

def test_decide_refuses_live_without_sells(tdb, monkeypatch):
    candidate(tdb, 1)
    sender = Sender()
    monkeypatch.setattr(trader, "send_tx", sender)
    assert trader.LIVE_SELL_READY is False
    rpc = FakeRpc()
    trader.decide(rpc, tdb, RUNWAY, True, FakeAcct())
    trader.decide(rpc, tdb, RUNWAY, True, FakeAcct())
    assert sender.calls == [] and rpc.calls == [] and tdb.one("SELECT COUNT(*) n FROM trades")["n"] == 0
    assert len(events(tdb, "live buys stay off until live sells exist")) == 1
    assert "live buys wait for live sells" in trader.summary(tdb)["policy"]


def test_decide_records_pending_before_broadcast(tdb, monkeypatch):
    monkeypatch.setattr(trader, "LIVE_SELL_READY", True)
    t = candidate(tdb, 1)
    sender = Sender()
    monkeypatch.setattr(trader, "send_tx", sender)
    rpc = FakeRpc()
    trader.decide(rpc, tdb, RUNWAY, True, FakeAcct())
    assert len(sender.calls) == 1 and sender.calls[0]["wait"] is False and sender.calls[0]["to"] == C.UNIVERSAL_ROUTER
    assert sender.calls[0]["value"] == int(10 / 2500 * 1e18)
    tr = tdb.one("SELECT * FROM trades")
    assert tr["note"] == "PENDING" and tr["tx"] == HASH and tr["mode"] == "live" and tr["usd"] == 10.0
    assert tdb.one("SELECT COUNT(*) n FROM positions")["n"] == 0
    assert trader.spent_today(tdb) == 10.0 and trader.open_count(tdb) == 1
    trader.decide(rpc, tdb, RUNWAY, True, FakeAcct())                       # next cycle: the token is off the table
    assert len(sender.calls) == 1 and tdb.one("SELECT COUNT(*) n FROM trades")["n"] == 1
    assert trader.candidates(tdb) == []
    # the receipt arrives: 970 tokens really came in, not the 1000 quoted
    rpc.receipts[HASH] = receipt(t, C.WALLET, [900 * 10 ** 18, 70 * 10 ** 18])
    trader.mark(rpc, tdb, True, FakeAcct())
    tr = tdb.one("SELECT * FROM trades")
    assert tr["note"] == "SUCCESS" and tr["qty"] == 970.0 and abs(tr["price_usd"] - 10 / 970) < 1e-12
    p = tdb.one("SELECT * FROM positions WHERE token=?", (t,))
    assert p["status"] == "open" and p["mode"] == "live" and p["qty"] == 970.0 and p["qty_left"] == 970.0
    assert p["policy"] == lab.DEFAULT and p["quote"] == "ETH" and json.loads(p["pool_key"])["quote"] == C.ZERO
    assert len(events(tdb, "bought $10.00 of $T1 (970 tokens received)")) == 1
    trader.mark(rpc, tdb, True, FakeAcct())                                 # nothing left to reconcile
    assert tdb.one("SELECT COUNT(*) n FROM trades")["n"] == 1


def test_pending_without_receipt_holds_its_slot(tdb, monkeypatch):
    monkeypatch.setattr(trader, "LIVE_SELL_READY", True)
    candidate(tdb, 1)
    monkeypatch.setattr(trader, "send_tx", Sender())
    rpc = FakeRpc()
    trader.decide(rpc, tdb, RUNWAY, True, FakeAcct())
    trader.mark(rpc, tdb, True, FakeAcct())                                 # no receipt yet
    assert tdb.one("SELECT note FROM trades")["note"] == "PENDING" and trader.open_count(tdb) == 1
    assert ("eth_getTransactionReceipt", [HASH]) in rpc.calls
    rpc.calls.clear()
    trader.reconcile(FakeRpc(), tdb)                                        # a fresh rpc, still no receipt: still pending
    assert tdb.one("SELECT note FROM trades")["note"] == "PENDING"


def test_reconcile_falls_back_to_the_quote_without_transfer_logs(tdb, monkeypatch):
    monkeypatch.setattr(trader, "LIVE_SELL_READY", True)
    t = candidate(tdb, 1)
    monkeypatch.setattr(trader, "send_tx", Sender())
    rpc = FakeRpc()
    trader.decide(rpc, tdb, RUNWAY, True, FakeAcct())
    rpc.receipts[HASH] = {"status": "0x1", "logs": [{"address": tok(9), "topics": [TRANSFER.topic, pad(C.HOOK), pad(C.WALLET)], "data": "0x" + f"{5 * 10 ** 18:064x}"}]}
    trader.reconcile(rpc, tdb)
    p = tdb.one("SELECT * FROM positions WHERE token=?", (t,))
    assert p["qty"] == 1000.0 and p["entry_usd"] == 0.01


def test_reconcile_marks_reverted_and_blocks(tdb, monkeypatch):
    monkeypatch.setattr(trader, "LIVE_SELL_READY", True)
    t = candidate(tdb, 1)
    sender = Sender()
    monkeypatch.setattr(trader, "send_tx", sender)
    rpc = FakeRpc(receipts={HASH: {"status": "0x0", "logs": []}})
    trader.decide(rpc, tdb, RUNWAY, True, FakeAcct())
    trader.mark(rpc, tdb, True, FakeAcct())
    assert tdb.one("SELECT note FROM trades")["note"] == "REVERTED"
    assert tdb.one("SELECT COUNT(*) n FROM positions")["n"] == 0
    assert trader.spent_today(tdb) == 10.0                                  # gas and value were at risk: it counts
    assert tdb.one("SELECT blocked_until FROM trade_intents WHERE token=?", (t,))["blocked_until"] >= NOW + 6 * 3600 - 5
    trader.decide(rpc, tdb, RUNWAY, True, FakeAcct())
    assert len(sender.calls) == 1


def test_decide_never_broadcast_marks_failed_and_blocks(tdb, monkeypatch):
    monkeypatch.setattr(trader, "LIVE_SELL_READY", True)
    t = candidate(tdb, 1)
    sender = Sender(error=RuntimeError("insufficient ETH: have 0.001000, need about 0.004500"))
    monkeypatch.setattr(trader, "send_tx", sender)
    rpc = FakeRpc()
    trader.decide(rpc, tdb, RUNWAY, True, FakeAcct())
    tr = tdb.one("SELECT * FROM trades")
    assert tr["note"].startswith("FAILED: insufficient ETH") and tr["tx"] is None
    assert tdb.one("SELECT COUNT(*) n FROM positions")["n"] == 0
    assert trader.spent_today(tdb) == 0 and trader.open_count(tdb) == 0
    blocked = tdb.one("SELECT blocked_until FROM trade_intents WHERE token=?", (t,))["blocked_until"]
    assert NOW + 6 * 3600 - 5 <= blocked <= NOW + 6 * 3600 + 60
    assert len(events(tdb, "buy $T1 failed before broadcast%")) == 1
    for _ in range(3):
        trader.decide(rpc, tdb, RUNWAY, True, FakeAcct())
    assert len(sender.calls) == 1 and tdb.one("SELECT COUNT(*) n FROM trades")["n"] == 1
    tdb.x("UPDATE trade_intents SET blocked_until=? WHERE token=?", (NOW - 1, t))
    assert [r["token"] for r in trader.candidates(tdb)] == [t]           # the block expires


def test_live_usdg_buys_are_skipped_for_good(tdb, monkeypatch):
    monkeypatch.setattr(trader, "LIVE_SELL_READY", True)
    t = candidate(tdb, 1, quote=C.USDG)
    sender = Sender()
    monkeypatch.setattr(trader, "send_tx", sender)
    trader.decide(FakeRpc(), tdb, RUNWAY, True, FakeAcct())
    trader.decide(FakeRpc(), tdb, RUNWAY, True, FakeAcct())
    assert sender.calls == [] and tdb.one("SELECT COUNT(*) n FROM trades")["n"] == 0
    assert tdb.one("SELECT status FROM trade_intents WHERE token=?", (t,))["status"] == "unsupported"
    assert len(events(tdb, "skip $T1: live USDG-quoted buys need the Permit2 setup%")) == 1


# ---- exits --------------------------------------------------------------------------------------

def test_mark_persists_peak_when_sell_deferred(tdb, monkeypatch):
    t = tok(1)
    position(tdb, t, "live", 1e-5)
    prices = {t: 2e-5}
    monkeypatch.setattr(trader, "token_prices", lambda addrs: {a: {"price_usd": prices[a]} for a in addrs})
    rpc = FakeRpc()
    trader.mark(rpc, tdb, True, FakeAcct())                                 # 2x: the arm would take profit
    p = tdb.one("SELECT * FROM positions WHERE token=?", (t,))
    assert p["status"] == "open" and p["peak_usd"] == 2e-5 and p["qty_left"] == 1000.0 and p["tp_done"] == "[]" and p["trail_on"] == 0
    assert tdb.one("SELECT COUNT(*) n FROM trades")["n"] == 0
    assert len(events(tdb, "sell $POS (take profit at 1.5x): live selling needs%")) == 1
    prices[t] = 0.5e-5                                                      # the stop would fire: still deferred, peak kept
    trader.mark(rpc, tdb, True, FakeAcct())
    p = tdb.one("SELECT * FROM positions WHERE token=?", (t,))
    assert p["status"] == "open" and p["peak_usd"] == 2e-5 and p["qty_left"] == 1000.0
    trader.mark(rpc, tdb, False, None)                                      # a live bag is never sold on paper
    assert tdb.one("SELECT COUNT(*) n FROM trades")["n"] == 0 and rpc.calls == []


def test_mark_demo_sells_keep_reason(tdb, monkeypatch):
    t = tok(1)
    position(tdb, t, "demo", 1.0)
    prices = {t: 1.6}
    monkeypatch.setattr(trader, "token_prices", lambda addrs: {a: {"price_usd": prices[a]} for a in addrs})
    trader.mark(FakeRpc(), tdb, False)
    p = tdb.one("SELECT * FROM positions WHERE token=?", (t,))
    assert p["status"] == "open" and p["tp_done"] == "[1.5]" and abs(p["qty_left"] - 333.0) < 1e-6 and p["reason"] is None
    assert abs(p["realized_usd"] - 667 * 1.6 * (1 - lab.FEE)) < 1e-6 and p["recovered_usd"] == p["realized_usd"]
    prices[t] = 0.9
    trader.mark(FakeRpc(), tdb, False)
    p = tdb.one("SELECT * FROM positions WHERE token=?", (t,))
    assert p["status"] == "closed" and p["reason"] == "trailing stop 40% below the peak" and p["qty_left"] < 1e-9
    sells = tdb.q("SELECT * FROM trades WHERE side='sell' ORDER BY id")
    assert [s["note"] for s in sells] == ["demo: would sell (take profit at 1.5x)", "demo: would sell (trailing stop 40% below the peak)"]


def test_mark_updates_the_peak_without_a_sale(tdb, monkeypatch):
    t = tok(1)
    position(tdb, t, "demo", 1.0)
    monkeypatch.setattr(trader, "token_prices", lambda addrs: {a: {"price_usd": 1.2} for a in addrs})
    trader.mark(FakeRpc(), tdb, False)
    assert tdb.one("SELECT peak_usd, status FROM positions")["peak_usd"] == 1.2


# ---- skips --------------------------------------------------------------------------------------

def test_quote_out_zero_is_skipped(tdb):
    t = candidate(tdb, 1)
    rpc = FakeRpc(out=0)
    trader.decide(rpc, tdb, RUNWAY, False)
    trader.decide(rpc, tdb, RUNWAY, False)
    assert tdb.one("SELECT COUNT(*) n FROM trades")["n"] == 0 and tdb.one("SELECT COUNT(*) n FROM positions")["n"] == 0
    assert tdb.one("SELECT status FROM trade_intents WHERE token=?", (t,))["status"] == "no_price"
    assert len(events(tdb, "skip $T1: no liquidity quoted")) == 1
    assert trader.candidates(tdb) == []


def test_pool_lookup_failure_is_a_skip(tdb):
    t = candidate(tdb, 1, pool=False)
    rpc = FakeRpc(logs_error=RpcError("eth_getLogs failed after 4 tries: 429"))
    trader.decide(rpc, tdb, RUNWAY, False)                                  # does not raise
    assert tdb.one("SELECT status FROM trade_intents WHERE token=?", (t,))["status"] == "no_pool"
    assert len(events(tdb, "skip $T1: pool lookup failed%")) == 1
    t2 = candidate(tdb, 2, pool=False)
    tdb.x("UPDATE launches SET grad_block=NULL WHERE token=?", (t2,))
    trader.decide(FakeRpc(), tdb, RUNWAY, False)
    assert tdb.one("SELECT status FROM trade_intents WHERE token=?", (t2,))["status"] == "no_pool"
    assert len(events(tdb, "skip $T2: pool not found")) == 1
    assert trader.candidates(tdb) == []


def test_skipped_candidates_leave_the_window(tdb):
    eth = candidate(tdb, 1, scored_at=NOW - 100)                            # the oldest of four
    for i in (2, 3, 4):
        candidate(tdb, i, quote=OTHER_ASSET, scored_at=NOW - i)
    rpc = FakeRpc()
    trader.decide(rpc, tdb, RUNWAY, False)
    assert tdb.one("SELECT COUNT(*) n FROM positions")["n"] == 0            # the window held only the three unsupported ones
    assert len(events(tdb, "skip $T%: paired with an asset the worm does not hold yet")) == 3
    trader.decide(rpc, tdb, RUNWAY, False)
    assert tdb.one("SELECT token FROM positions")["token"] == eth
    assert len(events(tdb, "skip $T%: paired with an asset the worm does not hold yet")) == 3


def test_partial_scores_and_held_tokens_are_not_candidates(tdb):
    candidate(tdb, 1, partial=1)
    held = candidate(tdb, 2)
    position(tdb, held, "demo", 0.01)
    ok = candidate(tdb, 3)
    assert [r["token"] for r in trader.candidates(tdb)] == [ok]


def test_delayed_arm_enters_later(tdb, monkeypatch):
    monkeypatch.setattr(lab, "current_policy", lambda db: ("costout_1.5x@30m", "learned"))
    t = candidate(tdb, 1, scored_at=NOW - 60)
    trader.decide(FakeRpc(), tdb, RUNWAY, False)
    assert tdb.one("SELECT COUNT(*) n FROM positions")["n"] == 0
    assert tdb.one("SELECT arm FROM trade_intents WHERE token=?", (t,))["arm"] == "costout_1.5x@30m"
    tdb.x("UPDATE trade_intents SET scored_at=? WHERE token=?", (NOW - 1900, t))
    trader.decide(FakeRpc(), tdb, RUNWAY, False)
    assert tdb.one("SELECT policy FROM positions WHERE token=?", (t,))["policy"] == "costout_1.5x@30m"


def test_received_qty_sums_transfer_logs():
    t = tok(1)
    rc = receipt(t, C.WALLET, [3 * 10 ** 18, 2 * 10 ** 18])
    rc["logs"].append({"address": t, "topics": [TRANSFER.topic, pad(C.WALLET), pad(C.HOOK)], "data": "0x" + f"{10 ** 18:064x}"})   # outgoing
    rc["logs"].append({"address": tok(2), "topics": [TRANSFER.topic, pad(C.HOOK), pad(C.WALLET)], "data": "0x" + f"{10 ** 18:064x}"})  # another token
    assert trader.received_qty(rc, t, C.WALLET) == 5.0
    assert trader.received_qty({"logs": []}, t, C.WALLET) is None


def test_trading_off_by_policy_makes_no_decisions(tdb, monkeypatch):
    monkeypatch.setattr(C, "TRADING", False)
    monkeypatch.setattr(trader, "candidates", lambda *a, **k: (_ for _ in ()).throw(AssertionError("candidates consulted while trading is off")))
    trader.decide(object(), tdb, RUNWAY, False, ready={"ready": True, "score": 90})
    assert tdb.q("SELECT * FROM positions") == [] and tdb.q("SELECT * FROM trades") == []
    s = trader.summary(tdb)
    assert s["enabled"] is False and s["policy"].startswith("off by policy")
    assert "off by policy" in tdb.one("SELECT text FROM events ORDER BY id DESC LIMIT 1")["text"]
