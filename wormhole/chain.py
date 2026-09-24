"""A small JSON-RPC client and ABI helpers. No web3 dependency, just requests + eth_abi."""
import logging
import re
import threading
import time

import requests
from eth_abi import decode, encode
from eth_utils import keccak

log = logging.getLogger("wormhole.chain")

MIN_CHUNK = 500      # get_logs never shrinks a window below this; at the floor the error surfaces to the caller


class RpcError(Exception):
    pass


def _is_rate_limit(msg):
    m = msg.lower()
    return bool(re.search(r"\b429\b", m)) or "too many requests" in m or "rate limit" in m


def _is_range_error(msg):
    """The node refused the window (too many logs, too many blocks, took too long): worth a smaller one."""
    m = msg.lower()
    return not _is_rate_limit(m) and ("exceeds limit" in m or "timed out" in m or "too many results" in m
                                      or "block range" in m or "more than" in m)


def _is_deterministic(err):
    """An execution revert or a malformed request: asking again returns the same answer."""
    code = err.get("code") if isinstance(err, dict) else None
    return code in (3, -32600, -32601, -32602) or "execution reverted" in str(err).lower()


class Rpc:
    def __init__(self, url, timeout=90, min_interval=0.11):   # the public node allows about 10 requests a second
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
        """One JSON-RPC call. Rate limits back off (1.5, 3, 6 s...), transport errors retry, a revert or a
        refused window raises at once so the caller can decide, and a body without result or error is
        retried like a transport error."""
        payload = {"jsonrpc": "2.0", "id": self._next(), "method": method, "params": params}
        last = None
        for i in range(retries):
            self._wait_turn()
            try:
                r = self.s.post(self.url, json=payload, timeout=self.timeout)
                if r.status_code == 429:
                    last = RpcError(f"{method}: 429 Too Many Requests")
                    time.sleep(1.5 * (2 ** i))
                    continue
                j = r.json()
            except (requests.RequestException, ValueError) as e:
                last = e
                time.sleep(0.7 * (i + 1))
                continue
            if not isinstance(j, dict):
                last = RpcError(f"{method}: malformed body {str(j)[:80]}")
            elif "error" in j:
                msg = str(j["error"])
                if _is_rate_limit(msg):                   # first: "Too Many Requests" is not a range error
                    last = RpcError(f"{method}: {msg}")
                    time.sleep(1.5 * (2 ** i))
                    continue
                err = RpcError(f"{method}: {msg}")
                if _is_range_error(msg) or _is_deterministic(j["error"]):
                    raise err
                last = err
            elif "result" in j:
                return j["result"]
            else:
                last = RpcError(f"{method}: malformed body {str(j)[:80]}")
            time.sleep(0.7 * (i + 1))
        raise RpcError(f"{method} failed after {retries} tries: {last}")

    def read_only(self, budget_s=5):
        """A no-retry network budget for disposable paper quotes, never transaction recovery."""
        return ReadBudget(self, time.monotonic() + budget_s)

    def call_with_deadline(self, method, params, deadline):
        if method not in ('eth_call', 'eth_gasPrice'):
            raise ValueError('bounded quote reads only allow eth_call and eth_gasPrice')
        self._wait_turn()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RpcError('quote read budget exhausted')
        payload = {'jsonrpc': '2.0', 'id': self._next(), 'method': method, 'params': params}
        try:
            response = self.s.post(self.url, json=payload, timeout=min(5, remaining))
            if response.status_code != 200:
                raise RpcError('quote read unavailable')
            body = response.json()
            if (time.monotonic() > deadline or not isinstance(body, dict)
                    or body.get('error') or 'result' not in body):
                raise RpcError('quote read failed or exceeded budget')
            return body['result']
        except (requests.RequestException, ValueError) as exc:
            raise RpcError('quote read unavailable') from exc

    def batch(self, calls, chunk=25):
        """calls: list of (method, params). Returns results in order, None where a call failed.
        Only a batch the node rejects as a whole is split in half and retried, down to single calls."""
        out = []
        for i in range(0, len(calls), chunk):
            out.extend(self._batch_part(calls[i:i + chunk]))
        return out

    def batch_with_deadline(self, calls, deadline, chunk=25):
        """Optional metadata reads: no retry amplification, missing answers remain None.

        The shared monotonic deadline covers successive batches. Request timeouts use the
        remaining allowance; this is a network time budget, not a hard process deadline.
        This method is deliberately unavailable for signing, sending or receipt recovery.
        """
        if any(method != 'eth_call' for method, _ in calls):
            raise ValueError('bounded metadata batches only allow eth_call')
        out = [None] * len(calls)
        for start in range(0, len(calls), chunk):
            if time.monotonic() >= deadline:
                break
            self._wait_turn()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            part = calls[start:start + chunk]
            payload = [{'jsonrpc': '2.0', 'id': i + 1, 'method': method, 'params': params}
                       for i, (method, params) in enumerate(part)]
            try:
                response = self.s.post(self.url, json=payload, timeout=min(5, remaining))
                if response.status_code != 200:
                    break
                body = response.json()
                if not isinstance(body, list):
                    break
                answers = {item.get('id'): item for item in body if isinstance(item, dict)}
                for i in range(len(part)):
                    out[start + i] = answers.get(i + 1, {}).get('result')
            except (requests.RequestException, ValueError):
                break
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
            except (requests.RequestException, ValueError):
                time.sleep(0.8 * (attempt + 1))
                continue
            if isinstance(js, dict) and _is_rate_limit(str(js.get("error", ""))):
                js = None
                time.sleep(1.5 * (2 ** attempt))
                continue
            break
        if isinstance(js, list) and len(js) == len(part):
            # a per-item error (a revert, a missing block) is an answer: None for that item only
            res = {x.get("id"): x for x in js if isinstance(x, dict)}
            return [res.get(k + 1, {}).get("result") for k in range(len(part))]
        if js is None:                         # transport or rate limit exhausted: splitting would only multiply posts
            log.info("batch of %d calls got no answer", len(part))
            return [None] * len(part)
        if len(part) == 1:
            log.info("batch call failed: %s", str(js)[:160])
            return [None]
        mid = len(part) // 2                   # only a batch rejected as a whole is split
        return self._batch_part(part[:mid]) + self._batch_part(part[mid:])

    def block_number(self):
        return int(self.call("eth_blockNumber", []), 16)

    def get_block(self, n):
        return self.call("eth_getBlockByNumber", [hex(n) if isinstance(n, int) else n, False])

    def block_ts(self, n):
        b = self.get_block(n)
        return int(b["timestamp"], 16) if b else None

    def block_timestamps(self, numbers):
        """Exact timestamps for a set of blocks, batched. None for a block the node did not answer."""
        nums = sorted(set(numbers))
        res = self.batch([("eth_getBlockByNumber", [hex(n), False]) for n in nums])
        return {n: (int(b["timestamp"], 16) if b else None) for n, b in zip(nums, res)}

    def eth_call(self, to, data, block="latest"):
        return self.call("eth_call", [{"to": to, "data": data}, block])

    def get_logs(self, address, topics, from_block, to_block, chunk=150_000, cap=None):
        """Yield logs over a range. A window the node refuses is quartered, never below MIN_CHUNK (there the
        error surfaces, so the caller's own retry applies); after a success the window grows back."""
        full = chunk
        start = from_block
        seen = 0
        while start <= to_block:
            end = min(start + chunk - 1, to_block)
            try:
                logs = self.call("eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(end),
                                                  "address": address, "topics": topics}])
            except RpcError as e:
                if _is_range_error(str(e)) and end > start and chunk > MIN_CHUNK:
                    chunk = max(MIN_CHUNK, (end - start + 1) // 4)
                    continue
                raise
            for l in logs:
                yield l
            seen += len(logs)
            if cap is not None and seen >= cap:
                return
            start = end + 1
            if chunk < full:
                chunk = min(full, chunk * 2)


class ReadBudget:
    """Small read-only RPC interface sharing the parent's rate gate and one deadline.

    Requests timeouts are network budgets, not hard process deadlines.
    """
    def __init__(self, rpc, deadline):
        self._rpc, self.deadline = rpc, deadline

    def call(self, method, params):
        return self._rpc.call_with_deadline(method, params, self.deadline)

    def eth_call(self, to, data, block='latest'):
        return self.call('eth_call', [{'to': to, 'data': data}, block])


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
                out[n] = _signed(int(raw, 16), 256)      # a topic is sign-extended to 32 bytes whatever the width
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


def batch_calls(rpc, items, block="latest", deadline=None):
    """items: list of (to, sig, out_types, arg_types, args). Returns decoded results (None on failure)."""
    calls = [("eth_call", [{"to": to, "data": call_data(sig, at, a)}, block]) for to, sig, _, at, a in items]
    bounded = getattr(rpc, 'batch_with_deadline', None)
    raws = bounded(calls, deadline) if deadline is not None and bounded else rpc.batch(calls)
    return [decode_result(raw, it[2]) for raw, it in zip(raws, items)]
