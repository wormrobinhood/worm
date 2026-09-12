"""Pons V2 and Uniswap v4 event definitions, plus token metadata reads."""
from . import config as C
from .chain import Event, batch_calls

TOKEN_LAUNCHED = Event("TokenLaunched", [
    ("token", "address", True), ("curve", "address", True), ("deployer", "address", True),
    ("pairToken", "address", False), ("launchConfigId", "uint256", False), ("graduationThreshold", "uint256", False)])
POOL_GRADUATED = Event("PoolGraduated", [
    ("token", "address", True), ("positionId", "uint256", False),
    ("tokenAmount", "uint256", False), ("pairTokenAmount", "uint256", False)])
LAUNCH_SWEPT = Event("LaunchSwept", [
    ("token", "address", True), ("quoteOut", "uint256", False), ("tokenOut", "uint256", False)])
CURVE_BUY = Event("CurveBuy", [
    ("buyer", "address", True), ("recipient", "address", True), ("quoteIn", "uint256", False),
    ("tokensOut", "uint256", False), ("fee", "uint256", False), ("tax", "uint256", False)])
CURVE_SELL = Event("CurveSell", [
    ("seller", "address", True), ("recipient", "address", True), ("tokensIn", "uint256", False),
    ("quoteOut", "uint256", False), ("fee", "uint256", False), ("tax", "uint256", False)])
POOL_REGISTERED = Event("PoolRegistered", [
    ("poolId", "bytes32", True), ("memecoin", "address", False),
    ("quoteToken", "address", False), ("creator", "address", False)])
TRANSFER = Event("Transfer", [("from", "address", True), ("to", "address", True), ("value", "uint256", False)])
# Uniswap v4 PoolManager swap. The topic is overridden at runtime from config if the chain's
# PoolManager emits a different signature (see scorer.SWAP_TOPIC).
SWAP = Event("Swap", [
    ("id", "bytes32", True), ("sender", "address", True), ("amount0", "int128", False),
    ("amount1", "int128", False), ("sqrtPriceX96", "uint160", False), ("liquidity", "uint128", False),
    ("tick", "int24", False), ("fee", "uint24", False)])   # tick before fee on this chain's PoolManager

_SYMBOL_CACHE = {C.ZERO: "ETH", C.USDG: "USDG", C.WETH: "WETH"}


def pair_symbols(rpc, addrs):
    """Symbol for each pair token address, cached."""
    need = [a for a in set(addrs) if a not in _SYMBOL_CACHE]
    if need:
        res = batch_calls(rpc, [(a, "symbol()", ("string",), (), ()) for a in need])
        for a, s in zip(need, res):
            _SYMBOL_CACHE[a] = s if isinstance(s, str) and s else a[:8]
    return {a: _SYMBOL_CACHE.get(a, a[:8]) for a in addrs}


def token_metadata(rpc, tokens, with_curve=None):
    """name, symbol, logo, description, socials for each token; creator tax and fee bps from its curve."""
    tokens = list(tokens)
    items = []
    for t in tokens:
        items += [(t, "name()", ("string",), (), ()), (t, "symbol()", ("string",), (), ()),
                  (t, "logo()", ("string",), (), ()), (t, "description()", ("string",), (), ()),
                  (t, "socials()", ("string", "string", "string", "string", "string"), (), ())]
    res = batch_calls(rpc, items)
    out = {}
    for i, t in enumerate(tokens):
        name, symbol, logo, desc, soc = res[i * 5:(i + 1) * 5]
        soc = soc or ("", "", "", "", "")
        out[t] = {"name": name or "", "symbol": symbol or "", "logo": logo or "", "description": desc or "",
                  "twitter": soc[0] or "", "telegram": soc[1] or "", "discord": soc[2] or "",
                  "website": soc[3] or "", "farcaster": soc[4] or ""}
    if with_curve:
        curves = [with_curve[t] for t in tokens]
        citems = []
        for c in curves:
            citems += [(c, "creatorTaxBps()", ("uint256",), (), ()), (c, "feeBps()", ("uint256",), (), ()),
                       (c, "buybackEnabled()", ("bool",), (), ())]
        cres = batch_calls(rpc, citems)
        for i, t in enumerate(tokens):
            tax, fee, bb = cres[i * 3:(i + 1) * 3]
            out[t].update({"creator_tax_bps": int(tax or 0), "curve_fee_bps": int(fee or 0),
                           "buyback": bool(bb)})
    return out


def curve_reserves(rpc, curve):
    from .chain import call_fn
    r = call_fn(rpc, curve, "getReserves()", ("uint256", "uint256"))
    return r
