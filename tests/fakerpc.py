"""Offline chain for tests: a fake RPC that serves synthetic logs through the real Rpc.get_logs chunk and
cap logic, plus helpers to build launches. Nothing here touches the network."""
from eth_abi import encode

from wormhole import config as C
from wormhole.chain import Rpc, RpcError
from wormhole.pons import CURVE_BUY, CURVE_SELL, TRANSFER, POOL_REGISTERED
from wormhole.scorer import SWAP_TOPIC, Scorer

POOL_ID = "0x" + "77" * 32


def pad_addr(a):
    return "0x" + a[2:].lower().rjust(64, "0")


def addr(i):
    return "0x" + format(i, "040x")


class FakeRpc(Rpc):
    """Serves logs from memory; emulates the public node's cap per query when node_cap is set."""

    def __init__(self, latest, node_cap=None):
        super().__init__("http://fake.invalid", min_interval=0)
        self.latest = latest
        self.logs = []           # dicts: address, topics, blockNumber (int), data
        self.fail_addr = set()   # addresses whose eth_getLogs raises
        self.node_cap = node_cap
        self.calls = []
        self.nonces = {}         # address -> transaction count; unknown wallets look well used
        self.default_nonce = 40
        self.fail_batch = False

    def batch(self, calls, chunk=25):
        """The real batch() posts JSON-RPC arrays; here every item goes through call(), None on failure."""
        if self.fail_batch:
            return [None] * len(calls)
        out = []
        for method, params in calls:
            try:
                out.append(self.call(method, params))
            except RpcError:
                out.append(None)
        return out

    def call(self, method, params, retries=4):
        if method == "eth_blockNumber":
            return hex(self.latest)
        if method == "eth_getTransactionCount":
            return hex(self.nonces.get(params[0].lower(), self.default_nonce))
        if method == "eth_getLogs":
            f = params[0]
            a, b = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            address = f["address"].lower()
            self.calls.append((address, a, b))
            if address in self.fail_addr:
                raise RpcError("eth_getLogs failed after 4 tries: connection reset")
            want = f["topics"]
            out = []
            for n, lg in enumerate(self.logs):
                if lg["address"].lower() != address or not (a <= lg["blockNumber"] <= b):
                    continue
                ok = True
                for i, t in enumerate(want):
                    if t is None:
                        continue
                    if isinstance(t, list):
                        if lg["topics"][i] not in t:
                            ok = False
                    elif lg["topics"][i] != t:
                        ok = False
                if ok:
                    out.append((n, lg))
            if self.node_cap and len(out) > self.node_cap:
                raise RpcError(f"eth_getLogs: query exceeds limit of {self.node_cap} logs")
            # every log is its own transaction unless the test names one; the log index is the order of insertion,
            # so a curve buy and the transfers of the same transaction keep their order across queries
            return [{"address": lg["address"], "topics": lg["topics"], "blockNumber": hex(lg["blockNumber"]),
                     "transactionHash": lg.get("tx") or "0x" + format(n + 1, "064x"), "logIndex": hex(n), "data": lg["data"]}
                    for n, lg in out]
        raise RpcError("unsupported " + method)

    def curve_buy(self, curve, block, buyer, recipient, tokens_out, quote_in=1, tx=None):
        self.logs.append({"address": curve, "blockNumber": block, "tx": tx,
                          "topics": [CURVE_BUY.topic, pad_addr(buyer), pad_addr(recipient)],
                          "data": "0x" + encode(["uint256"] * 4, [quote_in, tokens_out, 0, 0]).hex()})

    def curve_sell(self, curve, block, seller, recipient, tokens_in, quote_out=1):
        self.logs.append({"address": curve, "blockNumber": block,
                          "topics": [CURVE_SELL.topic, pad_addr(seller), pad_addr(recipient)],
                          "data": "0x" + encode(["uint256"] * 4, [tokens_in, quote_out, 0, 0]).hex()})

    def transfer(self, token, block, frm, to, value, tx=None):
        self.logs.append({"address": token, "blockNumber": block, "tx": tx,
                          "topics": [TRANSFER.topic, pad_addr(frm), pad_addr(to)],
                          "data": "0x" + encode(["uint256"], [value]).hex()})

    def pool(self, block, pool_id, token, quote, creator):
        self.logs.append({"address": C.HOOK, "blockNumber": block, "topics": [POOL_REGISTERED.topic, pool_id],
                          "data": "0x" + encode(["address"] * 3, [token, quote, creator]).hex()})

    def swap(self, block, pool_id, amount0, amount1, sender=None):
        self.logs.append({"address": C.POOL_MANAGER, "blockNumber": block,
                          "topics": [SWAP_TOPIC, pool_id, pad_addr(sender or addr(0xdead1))],
                          "data": "0x" + encode(["int128", "int128", "uint160", "uint128", "int24", "uint24"],
                                                [amount0, amount1, 1, 1, 0, 3000]).hex()})


def launch(db, token, curve, deployer, block, ts, grad_block, grad_ts, tax=0, socials=True, graduated=1):
    db.x("INSERT OR REPLACE INTO launches(token,curve,deployer,pair_token,pair_symbol,config_id,grad_threshold,block,ts,tx,"
         "name,symbol,twitter,telegram,website,creator_tax_bps,graduated,grad_block,grad_ts)"
         " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (token, curve, deployer, C.USDG, "USDG", 0, "1", block, ts, None, "Test", "TST",
          "https://x.com/t" if socials else "", "", "", tax, graduated, grad_block, grad_ts))


def run(rpc, db, token, weights=None, progress=None):
    return Scorer(rpc, db, weights=weights or (lambda: {}), progress=progress or (lambda ev: None)).score(token)
