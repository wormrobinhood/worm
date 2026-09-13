"""How big the worm is. The character grows with the treasury it actually holds: real balances only.

"usd" is what the worm displays (its gold reserve included), "usd_real" is the spendable part and is the only
number any money decision (runway, compute, trading) may use. The gold reserve (GLD bought with the gold
share) counts for the character's size but never for a money decision."""
import logging
import time

from . import config as C
from .chain import Rpc, call_data, decode_result
from .prices import eth_usd

log = logging.getLogger("wormhole.growth")

STAGES = [(0, "hatchling"), (20, "tiny"), (100, "small"), (500, "growing"), (2000, "long"),
          (10000, "mighty"), (50000, "legend")]
_cache = (0, None)


def treasury(rpc):
    """USD value of the wallets the agent owns. Cached one minute. $0 when no wallet is configured."""
    global _cache
    if time.time() - _cache[0] < 60 and _cache[1]:
        return _cache[1]
    parts = {}
    if C.WALLET:
        try:
            wei = int(rpc.call("eth_getBalance", [C.WALLET, "latest"]), 16)
            parts["eth_rh"] = round(wei / 1e18 * eth_usd(), 2)
            raw = rpc.eth_call(C.USDG, call_data("balanceOf(address)", ("address",), (C.WALLET,)))
            parts["usdg_rh"] = round((decode_result(raw, ("uint256",)) or 0) / 1e6, 2)
        except Exception as e:
            log.info("treasury read failed: %s", e)
        try:
            from . import treasury as T
            held = T.gold_held(rpc, C.WALLET)
            parts["gold_rh"] = round(held * T.gold_price_usd(rpc), 2) if held else 0.0
        except Exception as e:
            log.info("gold read failed: %s", e)
    if C.BASE_WALLET:
        try:
            base = Rpc(C.BASE_RPC, timeout=20)
            raw = base.eth_call(C.BASE_USDC, call_data("balanceOf(address)", ("address",), (C.BASE_WALLET,)))
            parts["usdc_base"] = round((decode_result(raw, ("uint256",)) or 0) / 1e6, 2)
        except Exception as e:
            log.info("base treasury read failed: %s", e)
    usd = round(sum(parts.values()), 2)
    usd_real = round(sum(v for k, v in parts.items() if k != "gold_rh"), 2)   # gold is a reserve, never runway
    stage = 0
    for i, (thr, _) in enumerate(STAGES):
        if usd >= thr:
            stage = i
    nxt = STAGES[stage + 1][0] if stage + 1 < len(STAGES) else None
    out = {"usd": usd, "usd_real": usd_real, "gold_usd": parts.get("gold_rh"), "parts": parts, "stage": stage, "stage_name": STAGES[stage][1],
           "stages": len(STAGES),
           "next_usd": nxt, "wallet": C.WALLET or None, "base_wallet": C.BASE_WALLET or None}
    _cache = (time.time(), out)
    return out
