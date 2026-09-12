"""How big IRL Worm is. The character grows with the treasury it actually holds.

WH_DEMO_TREASURY adds pretend money so the stages can be looked at before funding. It counts for the
character only: "usd" is what the worm displays (demo included), "usd_real" is real balances alone and
is the only number any money decision (runway, compute, giving, trading) may use."""
import logging
import os
import time

from . import config as C
from .chain import Rpc, call_data, decode_result
from .prices import eth_usd

log = logging.getLogger("wormhole.growth")

STAGES = [(0, "hatchling"), (20, "tiny"), (100, "small"), (500, "growing"), (2000, "long"),
          (10000, "mighty"), (50000, "legend")]
_cache = (0, None)


def demo_treasury():
    """The pretend treasury from WH_DEMO_TREASURY, or 0.0 when unset, empty, zero or not a number."""
    raw = os.environ.get("WH_DEMO_TREASURY", "").strip()
    try:
        return max(0.0, float(raw)) if raw else 0.0
    except ValueError:
        log.warning("WH_DEMO_TREASURY=%r is not a number; ignored", raw)
        return 0.0


def treasury(rpc):
    """USD value of the wallets the agent owns. Cached one minute. $0 when no wallet is configured."""
    global _cache
    if time.time() - _cache[0] < 60 and _cache[1]:
        return _cache[1]
    parts = {}
    demo = demo_treasury()
    if demo:
        parts["demo"] = demo
    if C.WALLET:
        try:
            wei = int(rpc.call("eth_getBalance", [C.WALLET, "latest"]), 16)
            parts["eth_rh"] = round(wei / 1e18 * eth_usd(), 2)
            raw = rpc.eth_call(C.USDG, call_data("balanceOf(address)", ("address",), (C.WALLET,)))
            parts["usdg_rh"] = round((decode_result(raw, ("uint256",)) or 0) / 1e6, 2)
        except Exception as e:
            log.info("treasury read failed: %s", e)
    if C.BASE_WALLET:
        try:
            base = Rpc(C.BASE_RPC, timeout=20)
            raw = base.eth_call(C.BASE_USDC, call_data("balanceOf(address)", ("address",), (C.BASE_WALLET,)))
            parts["usdc_base"] = round((decode_result(raw, ("uint256",)) or 0) / 1e6, 2)
        except Exception as e:
            log.info("base treasury read failed: %s", e)
    usd = round(sum(parts.values()), 2)
    usd_real = round(sum(v for k, v in parts.items() if k != "demo"), 2)
    stage = 0
    for i, (thr, _) in enumerate(STAGES):
        if usd >= thr:
            stage = i
    nxt = STAGES[stage + 1][0] if stage + 1 < len(STAGES) else None
    out = {"usd": usd, "usd_real": usd_real, "parts": parts, "stage": stage, "stage_name": STAGES[stage][1],
           "stages": len(STAGES), "demo": demo > 0,
           "next_usd": nxt, "wallet": C.WALLET or None, "base_wallet": C.BASE_WALLET or None}
    _cache = (time.time(), out)
    return out
