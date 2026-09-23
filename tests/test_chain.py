"""The RPC client: error classes, retries, batches, log windows, ABI decoding. All offline."""
import pytest
import requests
from eth_abi import encode
from eth_abi.exceptions import InsufficientDataBytes

from wormhole import chain
from wormhole import config as C
from wormhole.chain import Event, Rpc, RpcError, _is_range_error, _is_rate_limit, _signed, decode_result
from wormhole.pons import POOL_GRADUATED, SWAP, TOKEN_LAUNCHED

TOKEN = "0x" + "aa" * 20
CURVE = "0x" + "bb" * 20
DEPLOYER = "0x" + "cc" * 20
TX = "0x" + "ab" * 32


def pad(addr):
    return "0x" + addr[2:].rjust(64, "0")


def make_log(event, indexed, data_types, data_values, block=58_400_000, index=3, address=C.FACTORY):
    """A log in the shape the node returns: hex block and index, topics, ABI-encoded data."""
    return {"address": address, "blockNumber": hex(block), "transactionHash": TX, "logIndex": hex(index),
            "topics": [event.topic] + list(indexed),
            "data": "0x" + (encode(list(data_types), list(data_values)).hex() if data_types else "")}


class Resp:
    def __init__(self, body, status=200):
        self.status_code, self.body = status, body

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class Session:
    """A scripted requests.Session: every post takes the next reply and keeps its payload."""

    def __init__(self, replies):
        self.replies, self.posts = list(replies), []

    def post(self, url, json=None, timeout=None):
        self.posts.append(json)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def ok(result):
    return Resp({"jsonrpc": "2.0", "id": 1, "result": result})


def err(code, message):
    return Resp({"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}})


def make_rpc(*replies):
    r = Rpc("http://fake", min_interval=0)      # no pacing gap: the only sleeps are the retry policy's
    r.s = Session(replies)
    return r


def test_optional_metadata_deadline_preserves_partial_answers_and_stops(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(chain.time, 'monotonic', lambda: clock[0])
    rpc = make_rpc()
    observed = []
    def post(url, json, timeout):
        observed.append(timeout)
        clock[0] = 16.0
        return Resp([{'id': 2, 'result': '0x22'}, {'id': 1, 'result': '0x11'}])
    rpc.s.post = post
    calls = [('eth_call', [])] * 3
    assert rpc.batch_with_deadline(calls, 15, chunk=2) == ['0x11', '0x22', None]
    assert observed == [5]


@pytest.mark.parametrize('reply', [Resp({}, 429), requests.Timeout(), Resp({'error': 'unavailable'})])
def test_optional_metadata_failure_does_not_retry_or_invent_values(reply):
    rpc = make_rpc(reply)
    assert rpc.batch_with_deadline([('eth_call', [])] * 30, chain.time.monotonic() + 15) == [None] * 30
    assert len(rpc.s.posts) == 1


def test_metadata_deadline_never_accepts_transaction_methods():
    rpc = make_rpc()
    with pytest.raises(ValueError, match='only allow eth_call'):
        rpc.batch_with_deadline([('eth_sendRawTransaction', [])], chain.time.monotonic() + 15)
    assert rpc.s.posts == []


@pytest.fixture
def sleeps(monkeypatch):
    out = []
    monkeypatch.setattr(chain.time, "sleep", out.append)
    return out


# -- classification ---------------------------------------------------------

def test_rate_limits_are_not_range_errors():
    for m in ("Too Many Requests", "429 Too Many Requests", "rate limit exceeded",
              "eth_getLogs: {'code': -32005, 'message': 'rate limit'}"):
        assert _is_rate_limit(m) and not _is_range_error(m)
    for m in ("query exceeds limit of 10000", "logs matched by query exceeds limit of 10000", "block range too large",
              "request timed out", "query returned more than 10000 results", "too many results"):
        assert _is_range_error(m) and not _is_rate_limit(m)
    assert not _is_rate_limit("block 4290001 not found")       # a number that happens to contain 429


def test_signed_indexed_int_uses_the_whole_topic():
    assert _signed(int("ff" * 32, 16), 256) == -1
    ev = Event("Tick", [("tick", "int24", True)])
    assert ev.decode(make_log(ev, ["0x" + "ff" * 32], (), ()))["tick"] == -1
    assert ev.decode(make_log(ev, ["0x" + encode(["int24"], [-139313]).hex()], (), ()))["tick"] == -139313


# -- event decoding ---------------------------------------------------------

def test_token_launched_decodes_and_the_signature_is_pinned():
    assert TOKEN_LAUNCHED.topic == "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"
    lg = make_log(TOKEN_LAUNCHED, [pad(TOKEN), pad(CURVE), pad(DEPLOYER)],
                  ("address", "uint256", "uint256"), (C.USDG, 2, 10 ** 22))
    d = TOKEN_LAUNCHED.decode(lg)
    assert d["token"] == TOKEN and d["curve"] == CURVE and d["deployer"] == DEPLOYER
    assert d["pairToken"] == C.USDG and d["launchConfigId"] == 2 and d["graduationThreshold"] == 10 ** 22
    assert d["_block"] == 58_400_000 and d["_tx"] == TX and d["_index"] == 3 and d["_address"] == C.FACTORY


def test_pool_graduated_decodes():
    assert POOL_GRADUATED.topic == "0x0a44ef75df69c534f43cd6c1aa3ef8983065fe5fe79ef9e79f6494e6f258c259"
    lg = make_log(POOL_GRADUATED, [pad(TOKEN)], ("uint256", "uint256", "uint256"), (77, 5 * 10 ** 26, 3 * 10 ** 9))
    d = POOL_GRADUATED.decode(lg)
    assert d["token"] == TOKEN and d["positionId"] == 77
    assert d["tokenAmount"] == 5 * 10 ** 26 and d["pairTokenAmount"] == 3 * 10 ** 9


def test_swap_decodes_a_negative_tick():
    assert SWAP.topic == "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
    pool = "0x" + "11" * 32
    lg = make_log(SWAP, [pool, pad(C.UNIVERSAL_ROUTER)], ("int128", "int128", "uint160", "uint128", "int24", "uint24"),
                  (-1_000_000, 3 * 10 ** 18, 2 ** 96, 10 ** 20, -139313, 0), address=C.POOL_MANAGER)
    d = SWAP.decode(lg)
    assert d["id"] == pool and d["sender"] == C.UNIVERSAL_ROUTER
    assert d["amount0"] == -1_000_000 and d["amount1"] == 3 * 10 ** 18 and d["tick"] == -139313 and d["fee"] == 0


def test_missing_data_raises():
    lg = make_log(TOKEN_LAUNCHED, [pad(TOKEN), pad(CURVE), pad(DEPLOYER)], (), ())     # data "0x"
    with pytest.raises(InsufficientDataBytes):
        TOKEN_LAUNCHED.decode(lg)


def test_decode_result_edge_cases():
    assert decode_result("0x", ("string",)) is None
    assert decode_result(None, ("uint256",)) is None
    assert decode_result("0x" + encode(["string"], ["ABC"]).hex(), ("string",)) == "ABC"
    assert decode_result("0x" + encode(["uint256", "uint256"], [1, 2]).hex(), ("uint256", "uint256")) == (1, 2)
    bad = (32).to_bytes(32, "big") + (3).to_bytes(32, "big") + b"\xff\xfe\xfd".ljust(32, b"\x00")   # not UTF-8
    assert decode_result("0x" + bad.hex(), ("string",)) is None
    assert decode_result("0x" + (1).to_bytes(32, "big").hex(), ("string",)) is None               # bytes32 for a string


# -- call -------------------------------------------------------------------

def test_call_backs_off_on_http_429(sleeps):
    r = make_rpc(Resp({}, 429), Resp({}, 429), Resp({}, 429), ok("0x10"))
    assert r.call("eth_blockNumber", []) == "0x10"
    assert len(r.s.posts) == 4 and sleeps == [1.5, 3.0, 6.0]


def test_call_backs_off_on_json_rate_limit_then_gives_up(sleeps):
    r = make_rpc(*[err(-32005, "Too Many Requests")] * 4)
    with pytest.raises(RpcError, match="failed after 4 tries"):
        r.call("eth_getLogs", [{}])
    assert len(r.s.posts) == 4 and sleeps == [1.5, 3.0, 6.0, 12.0]


def test_call_malformed_body_is_an_rpc_error(sleeps):
    r = make_rpc(*[Resp({"jsonrpc": "2.0", "id": 1})] * 4)
    with pytest.raises(RpcError, match="malformed body"):
        r.call("eth_chainId", [])
    assert len(r.s.posts) == 4


def test_call_revert_raises_at_once(sleeps):
    r = make_rpc(err(3, "execution reverted"))
    with pytest.raises(RpcError, match="execution reverted"):
        r.call("eth_call", [{}, "latest"])
    assert len(r.s.posts) == 1 and sleeps == []


def test_call_range_error_raises_at_once(sleeps):
    r = make_rpc(err(-32000, "logs matched by query exceeds limit of 10000"))
    with pytest.raises(RpcError, match="exceeds limit"):
        r.call("eth_getLogs", [{}])
    assert len(r.s.posts) == 1 and sleeps == []


def test_call_retries_transport_errors(sleeps):
    r = make_rpc(requests.ConnectionError("boom"), ok("0x1"))
    assert r.call("eth_chainId", []) == "0x1"
    assert len(r.s.posts) == 2 and sleeps == [0.7]


# -- get_logs ---------------------------------------------------------------

def logs_rpc(behaviour):
    """An Rpc whose eth_getLogs is scripted: behaviour(start, end, call_no) returns logs or raises."""
    r = Rpc("http://fake", min_interval=0)
    sizes = []

    def call(method, params, retries=4):
        q = params[0]
        s, e = int(q["fromBlock"], 16), int(q["toBlock"], 16)
        sizes.append(e - s + 1)
        return behaviour(s, e, len(sizes))
    r.call = call
    return r, sizes


def test_get_logs_shrinks_then_grows_back():
    def behaviour(s, e, n):
        if n == 1:
            raise RpcError("eth_getLogs: query exceeds limit of 10000")
        return []
    r, sizes = logs_rpc(behaviour)
    assert list(r.get_logs(C.FACTORY, [TOKEN_LAUNCHED.topic], 0, 599_999)) == []
    assert sizes[:4] == [150_000, 37_500, 75_000, 150_000]


def test_get_logs_failure_is_bounded():
    def behaviour(s, e, n):
        raise RpcError("eth_getLogs: block range too large")
    r, sizes = logs_rpc(behaviour)
    with pytest.raises(RpcError, match="block range"):
        list(r.get_logs(C.FACTORY, [TOKEN_LAUNCHED.topic], 0, 599_999))
    assert sizes == [150_000, 37_500, 9_375, 2_343, 585, 500]


def test_get_logs_rate_limit_is_not_shrunk():
    def behaviour(s, e, n):
        raise RpcError("eth_getLogs: Too Many Requests")
    r, sizes = logs_rpc(behaviour)
    with pytest.raises(RpcError, match="Too Many"):
        list(r.get_logs(C.FACTORY, [TOKEN_LAUNCHED.topic], 0, 599_999))
    assert sizes == [150_000]


def test_get_logs_cap_stops_after_the_window_that_reaches_it():
    def behaviour(s, e, n):
        return [{"n": n, "i": i} for i in range(25)]
    r, sizes = logs_rpc(behaviour)
    got = list(r.get_logs(C.FACTORY, [TOKEN_LAUNCHED.topic], 0, 599_999, cap=10))
    assert len(got) == 25 and sizes == [150_000]


# -- batch ------------------------------------------------------------------

CALLS = [("eth_call", [{"to": TOKEN, "data": "0x"}, "latest"])] * 25


def test_batch_per_item_error_is_one_none():
    items = [{"jsonrpc": "2.0", "id": k + 1, "result": hex(k)} for k in range(25)]
    items[7] = {"jsonrpc": "2.0", "id": 8, "error": {"code": 3, "message": "execution reverted"}}
    r = make_rpc(Resp(items[::-1]))               # out of order too: answers are matched by id
    out = r.batch(CALLS)
    assert len(r.s.posts) == 1 and out[7] is None and out[0] == "0x0" and out[24] == "0x18"


def test_batch_sustained_429_does_not_split(sleeps):
    r = make_rpc(*[Resp({}, 429)] * 5)
    out = r.batch(CALLS)
    assert out == [None] * 25 and len(r.s.posts) == 5 and all(len(p) == 25 for p in r.s.posts)
    assert sleeps == [1.5, 3.0, 6.0, 12.0, 24.0]


def test_batch_rejected_as_a_whole_is_split():
    half = lambda: Resp([{"jsonrpc": "2.0", "id": k + 1, "result": "0x1"} for k in range(2)])
    r = make_rpc(Resp({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "batch too large"}}),
                 half(), half())
    assert r.batch(CALLS[:4]) == ["0x1"] * 4 and len(r.s.posts) == 3


def test_block_timestamps_none_for_a_missing_block():
    r = Rpc("http://fake", min_interval=0)
    seen = []

    def batch(calls, chunk=25):
        seen.append([int(p[0], 16) for _, p in calls])
        return [{"timestamp": hex(1_700_000_000)}, None]
    r.batch = batch
    assert r.block_timestamps([5, 3, 5]) == {3: 1_700_000_000, 5: None}
    assert seen == [[3, 5]]
