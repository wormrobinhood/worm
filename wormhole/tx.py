"""Signing and sending on Robinhood Chain. Nothing is signed unless WH_LIVE=1."""
import logging
import time

from . import config as C
from .chain import RpcError

log = logging.getLogger("wormhole.tx")


def send_tx(rpc, acct, to, data="0x", value=0, gas=None, wait=True, say=None):
    say = say or log.info
    if not C.LIVE:
        raise RuntimeError("WH_LIVE=0: refusing to sign. Set WH_LIVE=1 to arm real transactions.")
    frm = acct.address
    nonce = int(rpc.call("eth_getTransactionCount", [frm, "pending"]), 16)
    gas_price = int(int(rpc.call("eth_gasPrice", []), 16) * 1.25)
    tx = {"to": to, "value": value, "data": data, "nonce": nonce, "gasPrice": gas_price, "chainId": C.CHAIN_ID}
    if gas is None:
        est = int(rpc.call("eth_estimateGas", [{"from": frm, "to": to, "value": hex(value), "data": data}]), 16)
        gas = int(est * 1.3)
    tx["gas"] = gas
    bal = int(rpc.call("eth_getBalance", [frm, "latest"]), 16)
    need = value + gas * gas_price
    if bal < need:
        raise RuntimeError(f"insufficient ETH: have {bal / 1e18:.6f}, need about {need / 1e18:.6f}")
    signed = acct.sign_transaction(tx)
    raw = signed.raw_transaction.hex()
    if not raw.startswith("0x"):
        raw = "0x" + raw
    say(f"sending tx to {to[:12]}… gas {gas:,} @ {gas_price / 1e9:.3f} gwei, value {value / 1e18:.6f} ETH")
    h = rpc.call("eth_sendRawTransaction", [raw])
    say(f"broadcast {h}")
    if not wait:
        return h, None
    for _ in range(120):
        time.sleep(1)
        rc = rpc.call("eth_getTransactionReceipt", [h])
        if rc:
            ok = rc.get("status") == "0x1"
            say(f"mined in block {int(rc['blockNumber'], 16)}: {'SUCCESS' if ok else 'REVERTED'}")
            return h, rc
    raise RpcError(f"no receipt for {h} after 120s")
