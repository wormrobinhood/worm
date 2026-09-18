"""Shared, read-only execution checks for the USDG trading pilot and its paper book.

Quotes are observations, not guaranteed fills. No function in this module signs or sends.
"""
import json
import math
import os
import time

from . import config as C
from .prices import eth_usd, token_prices, usable_price

MIN_SCORE = max(70, int(os.environ.get('WH_BUY_MIN_SCORE', '70')))
SLIPPAGE = 0.03
QUOTE_TTL = 30
SWAP_TTL = 180
PERMIT_TTL = 600
MAX_ROUNDTRIP_LOSS = 0.15
MAX_PRICE_IMPACT = 0.08
MAX_GAS_FRACTION = 0.10
EXIT_TOLERANCE = 0.03          # a sell's minimum is the quote less this
EXIT_TOLERANCE_RETRY = 0.10    # after a sell reverted: give up more to get out
MODEL = 'quoted-usdg-v1'


def eligible(token, result):
    try:
        metrics = result.get('metrics') or {}
        if isinstance(metrics, str):
            metrics = json.loads(metrics)
        if not isinstance(metrics, dict):
            return False
    except (ValueError, TypeError):
        return False
    return bool(token and (not C.TOKEN or token.lower() != C.TOKEN.lower())
                and result.get('verdict') == 'looks healthy'
                and result.get('score', 0) >= MIN_SCORE
                and not result.get('partial') and not metrics.get('partial'))


LIVE_QUOTES = (C.USDG,)            # what the live pilot may spend
PAPER_QUOTES = (C.USDG, C.ZERO)    # the paper book also reads ETH pools: most graduations pair with ETH


def verified(pk, token, quotes=LIVE_QUOTES):
    """The token's own Pons pool against an asset the caller accepts."""
    return bool(pk and pk.get('quote') in quotes and pk.get('hooks') == C.HOOK
                and {pk.get('c0'), pk.get('c1')} == {token, pk['quote']})


def quote_unit(pk):
    """(base units per whole quote asset, USD per whole quote asset). ETH needs a fresh price; a stale or
    missing one raises, so nothing is ever sized or valued from a guess."""
    if pk['quote'] == C.USDG:
        return 10 ** 6, 1.0
    if pk['quote'] == C.ZERO:
        price = eth_usd(strict=True)
        if price is None or not math.isfinite(price) or price <= 0:
            raise ValueError('fresh ETH price unavailable')
        return 10 ** 18, float(price)
    raise ValueError('unsupported quote asset')


def pool(rpc, db, token, quotes=LIVE_QUOTES):
    from .trader import pool_key, ensure_tables
    ensure_tables(db)
    pk = pool_key(rpc, db, token)
    if not verified(pk, token, quotes):
        raise ValueError('pilot requires a verified USDG pool' if quotes == LIVE_QUOTES else 'no verified pool in a supported asset')
    return pk


def gas_cost(rpc, units):
    price = eth_usd(strict=True)
    gp = int(rpc.call('eth_gasPrice', []), 16)
    if price is None or not math.isfinite(price) or price <= 0 or gp <= 0 or units <= 0:
        raise ValueError('fresh gas pricing unavailable')
    # Include two bounded approvals as well as the swap, even if an allowance can be reused.
    return (math.ceil(units * 1.3) + 240_000) * math.ceil(gp * 1.25) / 1e18 * price


def entry(rpc, db, token, dollars, *, quotes=LIVE_QUOTES, reference=None):
    """A buy quote with its round trip. `reference` is the mid the caller just read from the pool; without
    one the price API's reading is used. Raw amounts are in the pool's quote asset."""
    from .trader import quote_buy
    if rpc is None or not math.isfinite(dollars) or dollars <= 0:
        raise ValueError('execution quotes unavailable')
    started = time.time()
    pk = pool(rpc, db, token, quotes)
    unit, usd = quote_unit(pk)
    price = reference if reference is not None else usable_price(token_prices([token]).get(token))
    if price is None or not math.isfinite(price) or price <= 0:
        raise ValueError('fresh reference price unavailable')
    amount = int(dollars / usd * unit)
    out, gas, direction = quote_buy(rpc, pk, token, amount)
    if out <= 0:
        raise ValueError('buy quote unavailable')
    minimum = out * 9700 // 10000
    impact = 1 - (out / 1e18 * price / dollars)
    back, sell_gas, _ = quote_buy(rpc, pk, pk['quote'], minimum)
    fee = gas_cost(rpc, gas)
    roundtrip = (back * (1 - SLIPPAGE) / unit * usd - fee - gas_cost(rpc, sell_gas)) / dollars
    if (not math.isfinite(impact) or abs(impact) > MAX_PRICE_IMPACT
            or roundtrip < 1 - MAX_ROUNDTRIP_LOSS or fee > dollars * MAX_GAS_FRACTION):
        raise ValueError('price impact, round-trip loss or gas exceeds the pilot limit')
    if time.time() >= started + QUOTE_TTL:
        raise ValueError('execution quote expired')
    return {'pool': pk, 'amount_raw': amount, 'out_raw': out, 'minimum_raw': minimum,
            'gas_usd': fee, 'quoted_at': started, 'expires_at': started + QUOTE_TTL,
            'direction': direction, 'price': price, 'roundtrip_ratio': roundtrip,
            'liquidation_usd': back * 9700 // 10000 / unit * usd - gas_cost(rpc, sell_gas), 'model': MODEL}


def exit_quote(rpc, pk, token, amount, *, quotes=LIVE_QUOTES, tolerance=None):
    from .trader import quote_buy
    tolerance = EXIT_TOLERANCE if tolerance is None else tolerance
    if not 0 < tolerance <= EXIT_TOLERANCE_RETRY:
        raise ValueError('sell tolerance out of bounds')
    if rpc is None or not verified(pk, token, quotes) or amount <= 0:
        raise ValueError('sell quote unavailable')
    started = time.time()
    unit, usd = quote_unit(pk)
    out, gas, direction = quote_buy(rpc, pk, pk['quote'], amount)
    minimum = out * (10000 - int(round(tolerance * 10000))) // 10000
    if minimum <= 0:
        raise ValueError('no executable sell quote')
    fee = gas_cost(rpc, gas)
    if time.time() >= started + QUOTE_TTL:
        raise ValueError('sell quote expired')
    return {'amount_raw': amount, 'out_raw': out, 'minimum_raw': minimum, 'minimum_usd': minimum / unit * usd,
            'gas_usd': fee, 'quoted_at': started, 'expires_at': started + QUOTE_TTL,
            'direction': direction, 'pool': pk}
