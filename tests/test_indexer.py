"""The indexer against a fake chain: timestamps, one row per token, idempotent re-scans, a follow loop
that survives errors and skips a poison range, and the factory's own record for tokens launched before
our window."""
import pytest
from eth_abi import encode

from wormhole import config as C
from wormhole import indexer as ix
from wormhole.chain import RpcError, selector
from wormhole.indexer import Indexer
from wormhole.pons import POOL_GRADUATED, TOKEN_LAUNCHED

T1 = "0x" + "a1" * 20
CURVE, DEPLOYER = "0x" + "c1" * 20, "0x" + "d1" * 20
T0 = 1_700_000_000


def pad(addr):
    return "0x" + addr[2:].rjust(64, "0")


def ret(types, values):
    return "0x" + encode(list(types), list(values)).hex()


def txh(block):
    return "0x" + f"{block:064x}"


def launched(token, block, cfg=1, curve=CURVE, deployer=DEPLOYER, pair=C.USDG):
    return {"address": C.FACTORY, "blockNumber": hex(block), "transactionHash": txh(block), "logIndex": "0x0",
            "topics": [TOKEN_LAUNCHED.topic, pad(token), pad(curve), pad(deployer)],
            "data": ret(["address", "uint256", "uint256"], [pair, cfg, 10 ** 22])}


def graduated(token, block):
    return {"address": C.FACTORY, "blockNumber": hex(block), "transactionHash": txh(block), "logIndex": "0x1",
            "topics": [POOL_GRADUATED.topic, pad(token)], "data": ret(["uint256"] * 3, [7, 10 ** 26, 10 ** 9])}


def metadata(token, curve=CURVE, name="Worm", symbol="WORM", tax=250):
    return {(token, selector("name()")): ret(["string"], [name]), (token, selector("symbol()")): ret(["string"], [symbol]),
            (token, selector("socials()")): ret(["string"] * 5, ["x.com/w", "", "", "worm.example", ""]),
            (curve, selector("creatorTaxBps()")): ret(["uint256"], [tax]),
            (curve, selector("feeBps()")): ret(["uint256"], [100]),
            (curve, selector("buybackEnabled()")): ret(["bool"], [False])}


def struct(curve=CURVE, deployer=DEPLOYER, pair=C.USDG, thr=10 ** 22, tax=250, buyback=True, exists=True):
    """getLaunchedToken(address) return data: the LaunchedToken struct of ILaunchpadV2.sol, one word per field."""
    return ret(["address", "address", "address", "address", "address", "uint256", "uint24", "int24", "uint16", "bool",
                "uint8", "uint256", "uint256", "uint256", "bool"],
               [T1, curve, deployer, deployer, pair, thr, 3000, 60, tax, buyback, 1, 0, 0, 0, exists])


class Chain:
    """A fake node with 0.1 s blocks whose logs, calls and batches are scripted."""

    def __init__(self, head=3_000_000, logs=(), answers=None):
        self.head, self.logs, self.answers = head, list(logs), dict(answers or {})
        self.log_calls, self.ts_calls = [], []
        self.fail_logs = None            # exception every get_logs raises
        self.fail_selector = None        # batch raises when a call uses this selector

    def block_number(self):
        return self.head

    def block_ts(self, n):
        return T0 + n // 10

    def block_timestamps(self, nums):
        nums = sorted(set(nums))
        self.ts_calls.append(nums)
        return {n: self.block_ts(n) for n in nums}

    def get_logs(self, address, topics, a, b, chunk=150_000, cap=None):
        self.log_calls.append((topics[0], a, b))
        if self.fail_logs:
            raise self.fail_logs
        for lg in self.logs:
            n = int(lg["blockNumber"], 16)
            if (lg["address"] == address and lg["topics"][0] == topics[0] and a <= n <= b
                    and (len(topics) < 2 or lg["topics"][1] == topics[1])):
                yield lg

    def eth_call(self, to, data, block="latest"):
        return self.answers.get((to, data[:10]))

    def batch(self, calls):
        if self.fail_selector and any(p[0]["data"][:10] == self.fail_selector for _, p in calls):
            raise RpcError("batch failed")
        return [self.answers.get((p[0]["to"], p[0]["data"][:10])) for _, p in calls]


class Ticks:
    """Stands in for the stop event: the loop runs n ticks and never sleeps."""

    def __init__(self, n):
        self.n, self.i = n, 0

    def is_set(self):
        self.i += 1
        return self.i > self.n

    def wait(self, t=None):
        return False


# -- time -------------------------------------------------------------------

def test_est_ts_from_a_given_anchor(db):
    idx = Indexer(Chain(), db)
    idx.anchor = (1_000_000, T0)
    assert idx.block_time == ix.BLOCK_TIME
    assert idx.est_ts(999_000) == int(T0 - 1000 * ix.BLOCK_TIME)


def test_est_ts_without_an_anchor_refreshes_and_measures(db):
    idx = Indexer(Chain(head=2_000_000), db)
    assert idx.est_ts(1_999_000) == T0 + 199_900          # 0.1 s blocks measured from the chain, so exact here
    assert idx.anchor == (2_000_000, T0 + 200_000) and abs(idx.block_time - 0.1) < 1e-12


def test_backfill_timestamps_interpolate_between_samples(db):
    node = Chain()
    idx = Indexer(node, db)
    blocks = list(range(100_000, 250_000, 3_000))           # 50 blocks: more than 25, so sampled
    ts = idx._timestamps(100_000, 249_999, blocks, live=False)
    assert set(ts) == set(blocks) and all(abs(ts[n] - node.block_ts(n)) <= 1 for n in blocks)   # within a second
    assert len(node.ts_calls) == 1 and len(node.ts_calls[0]) == 16   # 15 samples every 10k blocks plus the end
    live = idx._timestamps(100_000, 249_999, blocks[:3], live=True)
    assert live == {n: node.block_ts(n) for n in blocks[:3]} and node.ts_calls[-1] == sorted(blocks[:3])


def test_timestamps_none_when_a_sample_is_missing(db):
    node = Chain()
    node.block_timestamps = lambda nums: {n: (None if n == 110_000 else node.block_ts(n)) for n in nums}
    idx = Indexer(node, db)
    blocks = list(range(100_000, 250_000, 3_000))
    ts = idx._timestamps(100_000, 249_999, blocks, live=False)
    assert ts[103_000] is None and ts[112_000] is None and ts[121_000] == node.block_ts(121_000)


# -- ingest -----------------------------------------------------------------

def test_ingest_launch_and_graduation_in_one_slice(db):
    node = Chain(logs=[launched(T1, 100), graduated(T1, 200)], answers=metadata(T1))
    grads, launches = [], []
    idx = Indexer(node, db, on_graduation=grads.append, on_launch=launches.append)
    idx._ingest(0, 999, live=True)
    rows = db.q("SELECT * FROM launches")
    assert len(rows) == 1
    r = rows[0]
    assert r["graduated"] == 1 and r["grad_block"] == 200 and r["grad_ts"] == node.block_ts(200)
    assert r["block"] == 100 and r["ts"] == node.block_ts(100) and r["tx"] == txh(100) and r["grad_tx"] == txh(200)
    assert r["name"] == "Worm" and r["symbol"] == "WORM" and r["twitter"] == "x.com/w" and r["website"] == "worm.example"
    assert r["creator_tax_bps"] == 250 and r["curve_fee_bps"] == 100 and r["buyback"] == 0 and r["meta"] == 1
    assert r["config_id"] == "1" and r["pair_symbol"] == "USDG"
    assert db.meta_get("last_block") == "999" and idx.last_ok > 0
    assert grads == [T1] and launches == [T1]
    assert [e["kind"] for e in db.events()] == ["graduation"]


def test_ingest_twice_is_idempotent(db):
    node = Chain(logs=[launched(T1, 100), graduated(T1, 200)], answers=metadata(T1))
    grads, launches = [], []
    idx = Indexer(node, db, on_graduation=grads.append, on_launch=launches.append)
    idx._ingest(0, 999, live=True)
    idx._ingest(0, 999, live=True)           # the follow loop re-reads the last RESCAN blocks every tick
    assert db.one("SELECT COUNT(*) n FROM launches")["n"] == 1
    assert grads == [T1] and launches == [T1] and len(db.events()) == 1


def test_ingest_failure_leaves_last_block_alone(db):
    node = Chain(logs=[launched(T1, 100), graduated(T1, 200)], answers=metadata(T1))
    node.fail_selector = selector("name()")
    idx = Indexer(node, db)
    with pytest.raises(RpcError):
        idx._ingest(0, 999, live=True)
    assert db.meta_get("last_block") is None


def test_failed_metadata_is_not_final(db):
    node = Chain(logs=[launched(T1, 100), graduated(T1, 200)])      # every metadata read fails
    idx = Indexer(node, db)
    idx._ingest(0, 999, live=True)
    r = db.one("SELECT name, creator_tax_bps, meta FROM launches WHERE token=?", (T1,))
    assert r["name"] is None and r["creator_tax_bps"] is None and r["meta"] == 0
    node.answers.update(metadata(T1))
    assert idx.refetch_metadata() == 1
    r = db.one("SELECT name, creator_tax_bps, meta FROM launches WHERE token=?", (T1,))
    assert r["name"] == "Worm" and r["creator_tax_bps"] == 250 and r["meta"] == 1


def test_metadata_is_truncated(db):
    node = Chain(logs=[launched(T1, 100), graduated(T1, 200)], answers=metadata(T1, name="N" * 500, symbol="S" * 100))
    Indexer(node, db)._ingest(0, 999, live=False)
    r = db.one("SELECT name, symbol FROM launches WHERE token=?", (T1,))
    assert r["name"] == "N" * 80 and r["symbol"] == "S" * 24


def test_config_id_beyond_64_bits_is_stored(db):
    node = Chain(logs=[launched(T1, 100, cfg=2 ** 64)])
    Indexer(node, db)._ingest(0, 999, live=False)
    assert db.one("SELECT config_id FROM launches")["config_id"] == str(2 ** 64)


# -- backfill ---------------------------------------------------------------

def test_backfill_slices_and_advances_last_block(db, monkeypatch):
    monkeypatch.setattr(C, "BACKFILL_HOURS", 10.0)        # 360k blocks at 0.1 s: three slices
    node = Chain(head=3_000_000)
    idx = Indexer(node, db)
    seen, orig = [], idx._ingest

    def spy(a, b, live):
        orig(a, b, live)
        seen.append(db.meta_get("last_block"))
    idx._ingest = spy
    idx.backfill()
    latest = 3_000_000 - ix.CONFIRM
    windows = [(a, b) for t, a, b in node.log_calls if t == TOKEN_LAUNCHED.topic]
    assert windows[0][0] == latest - int(10.0 * 3600 / idx.block_time) and windows[-1][1] == latest
    assert len(windows) == 3 and all(b - a + 1 <= C.LOG_CHUNK for a, b in windows)
    assert all(windows[i][1] + 1 == windows[i + 1][0] for i in range(len(windows) - 1))
    assert seen == [str(b) for _, b in windows]            # last_block advanced after every slice
    assert db.meta_get("last_block") == str(latest) and idx.ready.is_set()


def test_backfill_nothing_to_do_when_ahead(db):
    node = Chain(head=3_000_000)
    db.meta_set("last_block", 3_000_010)
    idx = Indexer(node, db)
    idx.backfill()
    assert node.log_calls == [] and idx.ready.is_set() and db.meta_get("last_block") == "3000010"


# -- follow -----------------------------------------------------------------

def test_follow_survives_errors(db):
    node = Chain()

    def down():
        raise RpcError("down")
    node.block_number = down
    idx = Indexer(node, db)
    idx.stop = Ticks(3)
    idx.follow()                                            # three failing ticks, no exception
    assert idx.fails == 3 and db.meta_get("last_block") is None and db.events() == []


def test_follow_skips_a_poison_range(db):
    node = Chain(head=3_000_000)
    node.fail_logs = RpcError("eth_getLogs: block range too large")
    db.meta_set("last_block", 2_999_000)
    idx = Indexer(node, db)
    idx.stop = Ticks(ix.POISON_FAILS - 1)
    idx.follow()
    assert idx.fails == ix.POISON_FAILS - 1 and db.meta_get("last_block") == "2999000"     # still trying
    idx.stop = Ticks(1)
    idx.follow()
    head = 3_000_000 - ix.CONFIRM
    assert idx.fails == 0 and db.meta_get("last_block") == str(head)
    ev = db.events()
    assert len(ev) == 1 and ev[0]["kind"] == "error"
    assert ev[0]["text"].startswith(f"skipping blocks {2_999_001 - ix.RESCAN}..{head}")
    idx.stop = Ticks(2)
    idx.follow()                                            # head unchanged: nothing to read, no new failures
    assert idx.fails == 0 and len(db.events()) == 1


def test_follow_reads_with_rescan_and_lag(db):
    node = Chain(head=3_000_000, logs=[launched(T1, 2_999_500)], answers=metadata(T1))
    db.meta_set("last_block", 2_999_800)
    idx = Indexer(node, db)
    idx.stop = Ticks(1)
    idx.follow()
    assert node.log_calls[0][1:] == (2_999_801 - ix.RESCAN, 3_000_000 - ix.CONFIRM)
    assert db.one("SELECT block FROM launches WHERE token=?", (T1,))["block"] == 2_999_500   # inside the rescan window
    assert db.meta_get("last_block") == str(3_000_000 - ix.CONFIRM)


# -- launches before the window --------------------------------------------

GET = (C.FACTORY, selector("getLaunchedToken(address)"))


def test_ensure_launch_from_the_factory_record(db):
    node = Chain(logs=[launched(T1, 500)], answers={GET: struct()})
    idx = Indexer(node, db)
    assert idx._ensure_launch(T1, 900) is True
    r = db.one("SELECT * FROM launches WHERE token=?", (T1,))
    assert r["curve"] == CURVE and r["deployer"] == DEPLOYER and r["pair_token"] == C.USDG and r["pair_symbol"] == "USDG"
    assert r["grad_threshold"] == str(10 ** 22) and r["creator_tax_bps"] == 250 and r["buyback"] == 1
    assert r["block"] == 500 and r["tx"] == txh(500) and r["ts"] == node.block_ts(500) and r["config_id"] is None
    assert node.log_calls[0] == (TOKEN_LAUNCHED.topic, 0, 900)          # searched backwards from the graduation
    assert idx._ensure_launch(T1, 900) is True and len(node.log_calls) == 1   # known now: no more reads


def test_ensure_launch_without_a_log_leaves_block_null(db):
    node = Chain(answers={GET: struct()})
    assert Indexer(node, db)._ensure_launch(T1, 900) is True
    r = db.one("SELECT block, ts, tx FROM launches WHERE token=?", (T1,))
    assert r["block"] is None and r["ts"] is None and r["tx"] is None


def test_ensure_launch_unknown_token(db):
    node = Chain(answers={GET: struct(curve=C.ZERO, exists=False)})
    assert Indexer(node, db)._ensure_launch(T1, 900) is False
    assert db.one("SELECT COUNT(*) n FROM launches")["n"] == 0


def test_stub_row_is_upgraded_by_the_real_launch(db):
    node = Chain(logs=[graduated(T1, 900)], answers={GET: struct()})
    idx = Indexer(node, db)
    idx._ingest(800, 999, live=False)                       # graduation of a token launched before the window
    r = db.one("SELECT block, graduated FROM launches WHERE token=?", (T1,))
    assert r["block"] is None and r["graduated"] == 1
    node.logs.append(launched(T1, 500, cfg=4))
    idx._ingest(400, 799, live=False)                       # the launch turns up later: the stub fills in
    r = db.one("SELECT block, tx, ts, graduated, config_id FROM launches WHERE token=?", (T1,))
    assert r["block"] == 500 and r["tx"] == txh(500) and r["ts"] == node.block_ts(500)
    assert r["graduated"] == 1 and r["config_id"] == "4"
