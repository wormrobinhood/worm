"""A small JSON-RPC client and ABI helpers. No web3 dependency, just requests + eth_abi."""
import logging
import threading
import time

import requests
from eth_abi import decode, encode
from eth_utils import keccak

log = logging.getLogger("wormhole.chain")


class RpcError(Exception):
    pass


def _is_rate_limit(msg):
    m = msg.lower()
    return "429" in m or "too many requests" in m or "rate limit" in m


def _is_range_error(msg):
    m = msg.lower()
    return "exceeds limit" in m or "timed out" in m or "too many" in m or "block range" in m


class Rpc:
    def __init__(self, url, timeout=90, min_interval=0.08):
        self.url = url
        self.timeout = timeout
        self.s = requests.Session()
        self._id = 0
        self._lock = threading.Lock()
        self._gate = threading.Lock()
        self._last = 0.0
        self.min_interval = min_interval   # seconds between requests, shared by every thread

    def _wait_turn(self):
        with self._gate:
            now = time.monotonic()
            gap = self.min_interval - (now - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()

    def _next(self):
        with self._lock:
            self._id += 1
            return self._id

    def call(self, method, params, retries=4):
        payload = {"jsonrpc": "2.0", "id": self._next(), "method": method, "params": params}
        last = None
        for i in range(retries):
            self._wait_turn()
            try:
                r = self.s.post(self.url, json=payload, timeout=self.timeout)
                if r.status_code == 429:
                    last = RpcError("429 Too Many Requests")
                    time.sleep(1.5 * (2 ** i))
                    continue
                j = r.json()
                if "error" in j:
                    err = RpcError(f"{method}: {j['error']}")
                    if _is_range_error(str(j["error"])):
                        raise err
                    last = err
                    if _is_rate_limit(str(j["error"])):
                        time.sleep(1.5 * (2 ** i))
                        continue
                else:
                    return j["result"]
            except RpcError:
                raise
            except (requests.RequestException, ValueError) as e:
                last = e
            time.sleep(0.7 * (i + 1))
        raise RpcError(f"{method} failed after {retries} tries: {last}")

    def batch(self, calls, chunk=25):
        """calls: list of (method, params). Returns results in order, None where a call failed.
        A rejected batch is split in half and retried, down to single calls."""
        out = []
        for i in range(0, len(calls), chunk):
            out.extend(self._batch_part(calls[i:i + chunk]))
        return out

    def _batch_part(self, part):
        payload = [{"jsonrpc": "2.0", "id": k + 1, "method": m, "params": p} for k, (m, p) in enumerate(part)]
        js = None
        for attempt in range(5):
            self._wait_turn()
            try:
                r = self.s.post(self.url, json=payload, timeout=self.timeout)
                if r.status_code == 429:
                    time.sleep(1.5 * (2 ** attempt))
                    continue
                js = r.json()
                if isinstance(js, dict) and _is_rate_limit(str(js.get("error", ""))):
                    js = None
                    time.sleep(1.5 * (2 ** attempt))
                    continue
                break
            except (requests.RequestException, ValueError):
                time.sleep(0.8 * (attempt + 1))
        ok = isinstance(js, list) and len(js) == len(part) and all(isinstance(x, dict) and "result" in x for x in js)
        if ok:
            res = {x["id"]: x["result"] for x in js}
            return [res.get(k + 1) for k in range(len(part))]
        if len(part) == 1:
            log.info("batch call failed: %s", str(js)[:160])
            return [None]
        mid = len(part) // 2
        return self._batch_part(part[:mid]) + self._batch_part(part[mid:])

    def block_number(self):
        return int(self.call("eth_blockNumber", []), 16)

    def get_block(self, n):
        return self.call("eth_getBlockByNumber", [hex(n) if isinstance(n, int) else n, False])

    def block_ts(self, n):
        b = self.get_block(n)
        return int(b["timestamp"], 16) if b else None

    def block_timestamps(self, numbers):
        """Exact timestamps for a set of blocks, batched."""
        nums = sorted(set(numbers))
        res = self.batch([("eth_getBlockByNumber", [hex(n), False]) for n in nums])
        return {n: (int(b["timestamp"], 16) if b else None) for n, b in zip(nums, res)}

    def eth_call(self, to, data, block="latest"):
        return self.call("eth_call", [{"to": to, "data": data}, block])

    def get_logs(self, address, topics, from_block, to_block, chunk=150_000, cap=None):
        """Yield logs over a range, shrinking the chunk when the node refuses a window."""
        start = from_block
        seen = 0
        while start <= to_block:
            end = min(start + chunk - 1, to_block)
            try:
                logs = self.call("eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(end),
                                                  "address": address, "topics": topics}])
            except RpcError as e:
                if _is_range_error(str(e)) and end > start:
                    chunk = max(500, (end - start + 1) // 4)
                    continue
                raise
            for l in logs:
                yield l
            seen += len(logs)
            if cap is not None and seen >= cap:
                return
            start = end + 1
            if chunk < 150_000:
                chunk = min(150_000, chunk * 2)


# ---- ABI helpers -----------------------------------------------------------

def topic(sig):
    return "0x" + keccak(text=sig).hex()


def selector(sig):
    return "0x" + keccak(text=sig).hex()[:8]


def addr_from_topic(t):
    return "0x" + t[-40:].lower()


def _signed(v, bits):
    return v - (1 << bits) if v >= 1 << (bits - 1) else v


class Event:
    """Minimal event decoder. inputs: list of (name, type, indexed)."""

    def __init__(self, name, inputs):
        self.name = name
        self.inputs = inputs
        self.sig = f"{name}({','.join(t for _, t, _ in inputs)})"
        self.topic = topic(self.sig)
        self.idx = [(n, t) for n, t, i in inputs if i]
        self.data = [(n, t) for n, t, i in inputs if not i]

    def decode(self, lg):
        out = {"_block": int(lg["blockNumber"], 16), "_tx": lg["transactionHash"],
               "_index": int(lg["logIndex"], 16), "_address": lg["address"].lower()}
        for k, (n, t) in enumerate(self.idx):
            raw = lg["topics"][k + 1]
            if t == "address":
                out[n] = addr_from_topic(raw)
            elif t.startswith("uint"):
                out[n] = int(raw, 16)
            elif t.startswith("int"):
                out[n] = _signed(int(raw, 16), int(t[3:] or 256))
            else:
                out[n] = raw.lower()
        if self.data:
            vals = decode([t for _, t in self.data], bytes.fromhex(lg["data"][2:]))
            for (n, t), v in zip(self.data, vals):
                out[n] = v.lower() if t == "address" else v
        return out


def call_data(sig, arg_types=(), args=()):
    return selector(sig) + (encode(list(arg_types), list(args)).hex() if arg_types else "")


def decode_result(raw, out_types):
    if not raw or raw == "0x":
        return None
    try:
        vals = decode(list(out_types), bytes.fromhex(raw[2:]))
    except Exception:
        return None
    return vals if len(out_types) > 1 else vals[0]


def call_fn(rpc, to, sig, out_types, arg_types=(), args=(), block="latest"):
    return decode_result(rpc.eth_call(to, call_data(sig, arg_types, args), block), out_types)


def batch_calls(rpc, items, block="latest"):
    """items: list of (to, sig, out_types, arg_types, args). Returns decoded results (None on failure)."""
    calls = [("eth_call", [{"to": to, "data": call_data(sig, at, a)}, block]) for to, sig, _, at, a in items]
    raws = rpc.batch(calls)
    return [decode_result(raw, it[2]) for raw, it in zip(raws, items)]
