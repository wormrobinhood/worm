"""A stand-in for chain.Rpc and a few receipt builders. Nothing here touches the network."""
import time

import rlp
from eth_abi import decode, encode
from eth_account._utils.legacy_transactions import Transaction
from eth_utils import keccak

from wormhole import config as C
from wormhole.chain import RpcError, selector
from wormhole.treasury import CLAIMED_TOPIC, TRANSFER_TOPIC

CLAIM_SEL = bytes.fromhex(selector("claimToken(address)")[2:])
TRANSFER_SEL = bytes.fromhex(selector("transfer(address,uint256)")[2:])


def word(n):
    """A uint256 as an eth_call result."""
    return "0x" + n.to_bytes(32, "big").hex()


def topic_addr(a):
    return "0x" + "00" * 12 + a[2:].lower()


def tx_hash(raw):
    return "0x" + keccak(hexstr=raw).hex()


def decode_tx(raw):
    """Fields of a signed legacy transaction."""
    t = rlp.decode(bytes.fromhex(raw[2:]), Transaction)
    return {"nonce": t.nonce, "gasPrice": t.gasPrice, "gas": t.gas, "to": "0x" + t.to.hex(), "value": t.value,
            "data": t.data, "v": t.v}


def transfer_log(token, frm, to, value):
    return {"address": token, "topics": [TRANSFER_TOPIC, topic_addr(frm), topic_addr(to)], "data": word(value),
            "blockNumber": "0x10", "transactionHash": "0x" + "00" * 32, "logIndex": "0x0"}


def claimed_log(recipient, token, amount):
    return {"address": C.FEE_ESCROW, "topics": [CLAIMED_TOPIC, topic_addr(recipient), topic_addr(token)], "data": word(amount),
            "blockNumber": "0x10", "transactionHash": "0x" + "00" * 32, "logIndex": "0x0"}


class FakeRpc:
    """Records every call and answers from canned values.

    eth_sendRawTransaction follows `script`, one entry per attempt (default 'ok'):
      'ok'                 the node takes the tx and returns its hash
      'network'            no answer (the RpcError chain.Rpc raises after a dropped connection); tx not delivered
      'delivered-network'  the tx arrived but the answer was lost
      'wrong hash'         the node answers with some other hash
      '429'                the gateway rate-limited the request; tx not delivered
      any other text       a node rejection with that message ('already known', 'insufficient funds', ...)
    Receipts: `receipts[h]` (None = not mined yet) wins, then `receipt_for(h)`, then a default receipt
    with `status` and `logs` for every tx the node holds."""

    def __init__(self, nonce=7, gas_price=90_600_000, estimate=100_000, balance=10 ** 18):
        self.calls = []
        self.nonce, self.gas_price, self.estimate, self.balance = nonce, gas_price, estimate, balance
        self.script = []
        self.raw = []                # every raw tx the node took, in order
        self.known = set()           # hashes the node holds
        self.status = "0x1"
        self.logs = []
        self.receipts = {}
        self.receipt_for = None
        self.eth_calls = {}          # selector -> raw result, or callable(params) -> raw result
        self.count_delay = 0.0       # widens the race window in the nonce test

    def _deliver(self, raw):
        if raw not in self.raw:
            self.raw.append(raw)
            self.nonce += 1
        self.known.add(tx_hash(raw))

    def receipt(self, h):
        if h in self.receipts:
            return self.receipts[h]
        if self.receipt_for:
            return self.receipt_for(h)
        if h not in self.known:
            return None
        return {"transactionHash": h, "status": self.status, "blockNumber": "0x10", "logs": list(self.logs)}

    def call(self, method, params, retries=4):
        self.calls.append((method, params))
        if method == "eth_getTransactionCount":
            time.sleep(self.count_delay)
            return hex(self.nonce)
        if method == "eth_gasPrice":
            return hex(self.gas_price)
        if method == "eth_estimateGas":
            return hex(self.estimate)
        if method == "eth_getBalance":
            return hex(self.balance)
        if method == "eth_sendRawTransaction":
            raw = params[0]
            outcome = self.script.pop(0) if self.script else "ok"
            if outcome == "ok":
                self._deliver(raw)
                return tx_hash(raw)
            if outcome == "wrong hash":
                self._deliver(raw)
                return "0x" + "ab" * 32
            if outcome in ("network", "delivered-network"):
                if outcome == "delivered-network":
                    self._deliver(raw)
                raise RpcError(f"{method} failed after {retries} tries: ('Connection aborted.', "
                               "ConnectionResetError(54, 'Connection reset by peer'))")
            if outcome == "429":
                raise RpcError(f"{method} failed after {retries} tries: {method}: 429 Too Many Requests")
            if outcome in ("already known", "nonce too low", "already exists"):
                self._deliver(raw)
            raise RpcError(f"{method} failed after {retries} tries: {method}: {{'code': -32000, 'message': '{outcome}'}}")
        if method == "eth_getTransactionByHash":
            return {"hash": params[0]} if params[0] in self.known else None
        if method == "eth_getTransactionReceipt":
            return self.receipt(params[0])
        if method == "eth_call":
            data = params[0]["data"]
            v = self.eth_calls.get(data[:10])
            if v is None:
                raise RpcError(f"eth_call: nothing canned for {data[:10]}")
            return v(params) if callable(v) else v
        raise RpcError(f"fake rpc: {method} is not scripted")

    def eth_call(self, to, data, block="latest"):
        return self.call("eth_call", [{"to": to, "data": data}, block])


def auto_receipts(rpc, wallet, claimed_units=None, status="0x1", status_for=None):
    """Receipts that reflect the transaction: a claimToken call gets the escrow's ClaimedToken log
    (claimed_units, or the row's own amount is used by the caller), a transfer(to, n) call gets the
    Transfer log. status_for(to_address, n) may override the status per transfer."""
    def receipt(h):
        raw = next((r for r in rpc.raw if tx_hash(r) == h), None)
        if raw is None:
            return None
        t = decode_tx(raw)
        logs, st = [], status
        if t["to"] == C.FEE_ESCROW and t["data"][:4] == CLAIM_SEL and claimed_units is not None:
            logs = [claimed_log(wallet, C.USDG, claimed_units)]
        elif t["to"] == C.USDG and t["data"][:4] == TRANSFER_SEL:
            to, n = decode(["address", "uint256"], t["data"][4:])
            if status_for:
                st = status_for(to.lower(), n)
            logs = [transfer_log(C.USDG, wallet, to, n)]
        return {"transactionHash": h, "status": st, "blockNumber": "0x10", "logs": logs if st == "0x1" else []}
    rpc.receipt_for = receipt


def uint_result(*vals):
    return "0x" + encode(["uint256"] * len(vals), list(vals)).hex()
