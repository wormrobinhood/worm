"""Live pool prices read straight from the chain, for positions that cannot wait for a price API.

One batched extsload of the Uniswap v4 PoolManager per pool: slot0 holds sqrtPriceX96. This is the pool's
mid price at the latest block, before the hook's fee and the creator tax; an exit is still priced by a
quote. A pool against ETH or a tokenized stock is converted to USD through that asset's own price
(prices.asset_usd). Read-only: nothing here signs or sends."""
import math
import time

from eth_abi import encode
from eth_utils import keccak

from . import config as C
from .prices import asset_usd

POOLS_SLOT = 6                    # PoolManager: mapping(PoolId => Pool.State) _pools
EXTSLOAD = "0x1e2eaeaf"           # extsload(bytes32)
Q96 = 2 ** 96
QUOTE_DECIMALS = {C.USDG: 6, C.ZERO: 18}
DECIMALS = "0x313ce567"           # decimals()
_decimals = dict(QUOTE_DECIMALS)  # read once per asset, never guessed


def quote_decimals(rpc, asset):
    """The quote asset's decimals: known for USDG and ETH, read from the token once for anything else. None
    when the node does not answer or the answer is not plausible, so no price is made from a guessed scale."""
    a = (asset or "").lower()
    if a not in _decimals:
        try:
            d = int(rpc.eth_call(a, DECIMALS), 16)
        except Exception:
            return None
        if not 0 < d <= 36:
            return None
        _decimals[a] = d
    return _decimals[a]


def pool_id(pk):
    """PoolId = keccak256(abi.encode(PoolKey))."""
    return "0x" + keccak(encode(["address", "address", "uint24", "int24", "address"],
                                [pk["c0"], pk["c1"], int(pk["fee"]), int(pk["tick_spacing"]), pk["hooks"]])).hex()


def state_slot(pid):
    return "0x" + keccak(encode(["bytes32", "uint256"], [bytes.fromhex(pid[2:]), POOLS_SLOT])).hex()


def sqrt_price(raw):
    """sqrtPriceX96 from a slot0 word: the low 160 bits. None for an empty or malformed answer."""
    try:
        value = int(raw, 16) & ((1 << 160) - 1)
    except (TypeError, ValueError):
        return None
    return value or None


def token_price(pk, token, sqrt_x96, quote_usd, decimals=None):
    """USD price of one whole token (18 decimals) from the pool's sqrt price. quote_usd is the USD value of
    one whole quote asset (1.0 for USDG, the ETH price for ETH, a stock's price for a stock); `decimals` the
    quote asset's, when it is neither. None when anything is unusable."""
    decimals = QUOTE_DECIMALS.get(pk.get("quote")) if decimals is None else decimals
    if decimals is None or not sqrt_x96 or not quote_usd or not math.isfinite(quote_usd) or quote_usd <= 0:
        return None
    ratio = (sqrt_x96 / Q96) ** 2                     # raw currency1 per raw currency0
    if ratio <= 0 or not math.isfinite(ratio):
        return None
    in_quote = ratio * 10 ** (18 - decimals) if pk["c0"] == token else 10 ** (18 - decimals) / ratio
    price = in_quote * quote_usd
    return price if math.isfinite(price) and price > 0 else None


def mids(rpc, pools, eth_usd, *, budget_s=None, cached=False):
    """{token: usd mid price or None} for pools = {token: pool key}. One JSON-RPC batch. An ETH-quoted pool
    needs eth_usd; a pool against any other asset needs that asset's fresh price (cached=True: only one already
    fetched) and its decimals. Without them that token's price is None, never a guess."""
    tokens = list(pools)
    if not tokens:
        return {}
    others = sorted({pools[t].get("quote") for t in tokens} - {C.USDG, C.ZERO, None})
    other_usd = {a: asset_usd(a, cached=cached) for a in others}
    other_dec = {a: quote_decimals(rpc, a) for a in others}
    calls = [("eth_call", [{"to": C.POOL_MANAGER, "data": EXTSLOAD + state_slot(pool_id(pools[t]))[2:]}, "latest"])
             for t in tokens]
    out = {}
    answers = (rpc.batch_with_deadline(calls, time.monotonic() + budget_s)
               if budget_s is not None and hasattr(rpc, 'batch_with_deadline') else rpc.batch(calls))
    for token, raw in zip(tokens, answers):
        pk = pools[token]
        quote = pk.get("quote")
        quote_usd = 1.0 if quote == C.USDG else eth_usd if quote == C.ZERO else other_usd.get(quote)
        decimals = other_dec.get(quote) if quote in other_dec else None
        if quote in other_dec and decimals is None:
            out[token] = None
            continue
        out[token] = token_price(pk, token, sqrt_price(raw), quote_usd, decimals)
    return out


def position_mids(rpc, rows):
    """(mids, own) for position rows ({token, pool_key as JSON}): `own` is every token whose row carries a
    verified pool key, `mids` the chain's price for those the node answered. A position with its own pool is
    only ever priced from that pool: a price API that lags a thin pool by one trade reads as a fall from the
    peak and sells a position that never fell."""
    import json
    from . import trade_checks
    from .prices import eth_usd_last
    pools = {}
    for row in rows:
        try:
            pk = json.loads(row.get("pool_key") or "null")
        except (TypeError, ValueError):
            continue
        if isinstance(pk, dict) and "fee" in pk and trade_checks.verified(pk, row["token"]):
            pools[row["token"]] = pk
    if not pools or rpc is None:
        return {}, set(pools)
    try:
        found = mids(rpc, pools, eth_usd_last())
    except Exception:
        found = {}
    return {t: m for t, m in found.items() if m}, set(pools)
