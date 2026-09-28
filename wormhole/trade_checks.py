"""Shared, read-only execution checks for the USDG trading pilot and its paper book.

Every fill, paper or live, spends or receives USDG through the best checked route (route.best_quote): an
aggregator (USDG to ETH or a stock and on to the token, in one transaction) or the token's own USDG pool. So a
token paired with anything can be traded, and its paper result is what live would have had. Quotes are
observations, not guaranteed fills. No function in this module signs or sends.
"""
import ast
import copy
import hashlib
import json
import math
import os
import sys
import time
from functools import lru_cache
from pathlib import Path

from . import config as C, route
from .prices import eth_usd, eth_usd_cached, token_prices, usable_price

MIN_SCORE = max(70, int(os.environ.get('WH_BUY_MIN_SCORE', '70')))
SLIPPAGE = 0.03
QUOTE_TTL = 30
SWAP_TTL = 180
PERMIT_TTL = 600
MAX_ROUNDTRIP_LOSS = 0.15
MAX_PRICE_IMPACT = 0.08
MAX_GAS_FRACTION = 0.10
PAPER_FILL = 0.01              # a paper fill is the quote less this, a side: what a fill a second after the quote
                               # plausibly loses. The 3% tolerance is a live order's revert bound, not an expected fill.
EXIT_TOLERANCE = 0.03          # a sell's minimum is the quote less this
EXIT_TOLERANCE_RETRY = 0.10    # after a sell reverted: give up more to get out
FALLBACK_HAIRCUT = 0.01        # a paper fill straight from an ETH pool while every aggregator is down also gives up this:
                               # the ETH-to-USDG leg a route would have paid, so a fallback never flatters a result
EXIT_WAIT_S = 3.0              # an exit or a valuation waits this long for the aggregators, inside the paper book's 5 s read
                               # budget: the pool's own quotes and the gas price are read before it, so a slow provider
                               # costs its own answer, never the whole quote
VALUE_PROVIDERS = ('kyber',)   # a valuation asks KyberSwap and the pool only: it must never spend LI.FI's request budget
DIRECT_EXIT_FLOOR = 0.80       # an entry needs a direct exit (its own pool, then the other asset's v3 pool to USDG) paying at
                               # least this share of the best route's: no position the worm could only sell through an aggregator
MODEL = 'routed-quote-v1'


# What a paper position's evidence was produced under. A cohort compares this, not whole source files: the old
# whole-file hash voided every cohort in progress on any edit (a comment, an unrelated fix in chain.py), and each void
# cost an attempt, so the gate could never finish. Constants are listed by value; the code that makes fills, features
# and exits is covered by a digest of its syntax (EVIDENCE_CODE), blind to comments and docstrings, computed at run
# time. SEMANTICS is bumped by hand only for a change of meaning outside that code (a new data source, say).
SEMANTICS = 'paper-evidence-4'     # 4: fills are route quotes for any pair (2026-09-28)
GAS_UNITS_MARGIN = 1.3          # the swap's estimated gas units, padded
APPROVAL_GAS_UNITS = 240_000    # two bounded approvals, charged on every swap even when an allowance could be reused
GAS_PRICE_MARGIN = 1.25         # the node's gas price, padded


@lru_cache(maxsize=None)
def _tree(module):
    return ast.parse((Path(__file__).parent / f'{module}.py').read_text())


def _named(tree, name):
    """The top-level function, `Class.method` or top-level assignment called `name`, or None."""
    scope, _, attr = name.rpartition('.')
    body = tree.body
    if scope:
        cls = next((n for n in body if isinstance(n, ast.ClassDef) and n.name == scope), None)
        body = cls.body if cls else []
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == attr:
            return node
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == attr for t in node.targets):
            return node
    return None


def source_digest(tree, names):
    """sha256 (128 bits) over the syntax of `names` in a parsed module, docstrings removed: blind to comments,
    blank lines and wording, changed by any edit to what the code does (a renamed variable included)."""
    h = hashlib.sha256(('python %d.%d' % sys.version_info[:2]).encode())   # ast.dump differs between Python versions
    for name in names:
        node = _named(tree, name)
        if node is None:
            raise ValueError(f'{name} not found: the evidence code list is out of date')
        node = copy.deepcopy(node)
        for n in ast.walk(node):
            body = getattr(n, 'body', None)
            if (isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str)):
                n.body = body[1:] or [ast.Pass()]
        h.update(name.encode() + b'\0' + ast.dump(node).encode())
    return h.hexdigest()[:32]


def code_digest(parts):
    """source_digest of {module: names} read from the package's own files, never from the live objects, so a test
    double or a runtime patch cannot change it. Computed at run time: nobody pins it by hand."""
    return _code_digest(tuple(sorted((m, tuple(n)) for m, n in parts.items())))


@lru_cache(maxsize=None)
def _code_digest(parts):
    h = hashlib.sha256()
    for module, names in parts:
        h.update(module.encode() + source_digest(_tree(module), names).encode())
    return h.hexdigest()[:32]


# The code that picks, prices and closes a paper position and decides whether it could stand in for live. Its digest
# is part of every position's evidence spec, computed at run time, so no edit to it can reach a cohort unannounced.
# The entry rules' own definitions are frozen per rule (strategy_validation.frozen), not here: adding a rule must not
# void the others. A new Python minor version changes every digest and so voids every cohort once.
EVIDENCE_CODE = {
    'trade_checks': ('entry', '_entry_direct', 'exit_quote', '_direct', '_direct_exit', '_limits', 'quote_unit', 'gas_cost',
                     '_gas_cost_at_price', 'entry_basis', 'verified', 'live_fill', 'pool', 'eligible'),
    'route': ('candidate', 'parse_kyber', 'parse_lifi', 'parse_relay', '_kyber', '_lifi', '_relay', '_lifi_params', '_ask',
              '_record', '_available', '_admit', '_gap', '_pace', 'best_quote', 'ALLOWED', 'PROVIDERS', 'EXECUTABLE', 'OWN',
              'TRADE'),
    'paper': ('pair', 'pair_symbol', 'live_comparable', 'pause_windows', 'Paper._consider', 'Paper.enter', 'Paper._open',
              'Paper._mark', 'Paper._mark_position', 'Paper._quote', 'Paper._value'),
    'trade_risk': ('loss_limit', 'check'),
    'lab': ('parse_arm', 'side_cost', 'token_cost', 'trail_pct', 'exit_step'),
    'watch': ('features', 'passes', 'flow', '_flows', 'holders_kept', 'sample_bucket', '_needs_flow', '_needs_holders',
              'all_looks', '_resolve_pools', '_sample', 'filtered_member', 'Watcher._pools', 'Watcher.mark_positions'),
    'poolstate': ('pool_id', 'state_slot', 'sqrt_price', 'quote_decimals', 'token_price', 'mids', 'position_mids'),
    'trader': ('pool_key', 'quote_buy', 'quote_v3', 'best_v3'),
    'prices': ('usable_price', 'observed_at', 'token_prices', 'eth_usd', 'eth_usd_cached', 'eth_usd_last', 'asset_usd'),
    'paper_research': ('FILTER_VERSION', 'FEATURES', 'LIMITS', 'entry_features', 'risk_filter'),
}


def evidence_spec():
    from . import watch, trade_risk
    return {'model': MODEL, 'semantics': SEMANTICS, 'code': code_digest(EVIDENCE_CODE),
            'entry_basis': 'acquisition_cost_per_token',
            'paper_size_usd': C.PAPER_SIZE_USD, 'paper_max_open': C.PAPER_MAX_OPEN,
            'paper_fill': PAPER_FILL, 'slippage': SLIPPAGE, 'exit_tolerance': EXIT_TOLERANCE,
            'exit_tolerance_retry': EXIT_TOLERANCE_RETRY,
            'max_roundtrip_loss': MAX_ROUNDTRIP_LOSS, 'max_price_impact': MAX_PRICE_IMPACT,
            'max_gas_fraction': MAX_GAS_FRACTION, 'quote_ttl': QUOTE_TTL, 'fallback_haircut': FALLBACK_HAIRCUT,
            'direct_exit_floor': DIRECT_EXIT_FLOOR, 'exit_wait_s': EXIT_WAIT_S, 'value_providers': list(VALUE_PROVIDERS),
            'route': route.evidence(),
            'gas': {'units_margin': GAS_UNITS_MARGIN, 'approval_units': APPROVAL_GAS_UNITS, 'price_margin': GAS_PRICE_MARGIN},
            'loss_limit_usd': trade_risk.loss_limit(), 'watch': watch.evidence_constants()}


def entry_basis(dollars, quantity):
    """The shared paper/live exit reference. Gas is accounted for separately in cashflows."""
    if not math.isfinite(dollars) or not math.isfinite(quantity) or dollars <= 0 or quantity <= 0:
        raise ValueError('invalid acquisition cost or quantity')
    return dollars / quantity


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


def verified(pk, token):
    """The token's own Pons pool: the hook's, holding the token against one other asset (USDG, ETH or a stock).
    Its mid prices exits and valuations; fills are routed, so the other asset may be anything."""
    return bool(pk and pk.get('quote') and pk.get('hooks') == C.HOOK
                and {pk.get('c0'), pk.get('c1')} == {token, pk['quote']})


def live_fill(row):
    """Could live have made this paper position's entry fill? Yes for a route an executable provider quoted from
    USDG, or the token's own USDG pool; no for an ETH pool filled directly while every aggregator was down (live
    holds no ETH to spend). A row from before routing: a USDG pool only, as then."""
    value = row.get('live_fill')
    if value is not None:
        return bool(value)
    try:
        quote = (json.loads(row.get('pool_key') or 'null') or {}).get('quote')
    except (ValueError, TypeError, AttributeError):
        return False
    return quote == C.USDG


def quote_unit(pk):
    """(base units per whole quote asset, USD per whole quote asset) for a pool quoted directly: USDG, or ETH
    with a fresh price; a stale or missing one raises, so nothing is ever sized or valued from a guess."""
    if pk['quote'] == C.USDG:
        return 10 ** 6, 1.0
    if pk['quote'] == C.ZERO:
        price = eth_usd(strict=True)
        if price is None or not math.isfinite(price) or price <= 0:
            raise ValueError('fresh ETH price unavailable')
        return 10 ** 18, float(price)
    raise ValueError('unsupported quote asset')


def pool(rpc, db, token):
    from .trader import pool_key, ensure_tables
    ensure_tables(db)
    pk = pool_key(rpc, db, token)
    if not verified(pk, token):
        raise ValueError('no verified Pons pool')
    return pk


def gas_cost(rpc, units):
    price = eth_usd(strict=True)
    return _gas_cost_at_price(rpc, units, price)


def _gas_cost_at_price(rpc, units, price, gp=None):
    gp = int(rpc.call('eth_gasPrice', []), 16) if gp is None else gp
    if price is None or not math.isfinite(price) or price <= 0 or gp <= 0 or units <= 0:
        raise ValueError('fresh gas pricing unavailable')
    # Include two bounded approvals as well as the swap, even if an allowance can be reused.
    return (math.ceil(units * GAS_UNITS_MARGIN) + APPROVAL_GAS_UNITS) * math.ceil(gp * GAS_PRICE_MARGIN) / 1e18 * price


def _direct(rpc, pk, token_in, token_out, amount):
    """The token's own pool as a fourth route, when it trades against USDG: the Universal Router path the pilot
    has always had. None for any other pool, or when the quoter does not answer."""
    from .trader import quote_buy
    if pk.get('quote') != C.USDG:
        return None
    try:
        out, gas, direction = quote_buy(rpc, pk, token_out, amount)
        return route.candidate('pons', token_in, token_out, amount, out, gas, 0.0, C.UNIVERSAL_ROUTER, C.PERMIT2,
                               direction=direction)
    except Exception:
        return None


def _direct_exit(rpc, pk, token, amount):
    """The direct exit for a pool against ETH or a stock: the token sold on its own pool for that asset, then the
    asset sold for USDG on its best Uniswap v3 pool, in one Universal Router call (trader.exit_calldata). Needs no
    aggregator: the path every position can always be sold by. None for a USDG pool, or when a leg does not quote."""
    from .trader import V3_GAS, best_v3, quote_buy
    quote = pk.get('quote')
    if quote in (None, C.USDG):
        return None
    try:
        middle, gas, direction = quote_buy(rpc, pk, quote, amount)
        if middle <= 0:
            return None
        fee, out = best_v3(rpc, C.WETH if quote == C.ZERO else quote, C.USDG, middle)
        return route.candidate('pons-v3', token, C.USDG, amount, out, gas + V3_GAS, 0.0, C.UNIVERSAL_ROUTER, C.PERMIT2,
                               direction=direction, v3_fee=fee)
    except Exception:
        return None


def _limits(impact, roundtrip, fee, dollars):
    if (not math.isfinite(impact) or abs(impact) > MAX_PRICE_IMPACT
            or roundtrip < 1 - MAX_ROUNDTRIP_LOSS or fee > dollars * MAX_GAS_FRACTION):
        raise ValueError('price impact, round-trip loss or gas exceeds the pilot limit')


def entry(rpc, db, token, dollars, *, reference=None, fallback=False, providers=route.PROVIDERS, direct=True, lane='paper'):
    """A buy of `dollars` USDG of `token` by the best checked route, with its round trip back to USDG by the
    best route for the tokens it would at least get: every hop's fee, the creator tax, the impact, the
    aggregator's own fee and gas on both legs. `reference` is the mid the caller just read from the token's
    pool; without one the price API's reading is used. A route further than route.MID_BAND from that mid is not
    believed. Raw amounts are USDG (6 decimals) and the token (18).

    fallback (paper only): when no route answers and the pool trades against ETH, the pool is quoted directly as
    before routing; that fill is marked live_fill=False, since live holds no ETH to make it. `providers` and
    `direct` narrow the candidates (live, re-quoting through the provider it has just approved). Every entry also
    needs a direct exit (the pool itself, or it and the other asset's v3 pool) paying at least DIRECT_EXIT_FLOOR of
    the best route's sell: a position is never bought that only an aggregator could sell."""
    if rpc is None or not math.isfinite(dollars) or dollars <= 0:
        raise ValueError('execution quotes unavailable')
    started = time.time()
    pk = pool(rpc, db, token)
    price = reference if reference is not None else usable_price(token_prices([token]).get(token))
    if price is None or not math.isfinite(price) or price <= 0:
        raise ValueError('fresh reference price unavailable')
    amount = int(round(dollars * 10 ** 6))
    try:
        buy = route.best_quote(C.USDG, token, amount, extra=[_direct(rpc, pk, C.USDG, token, amount) if direct else None],
                               expect=dollars / price * 1e18, max_fee_usd=dollars * route.MAX_FEE, providers=providers, lane=lane)
    except route.NoRoute:
        if not fallback or pk['quote'] != C.ZERO:
            raise
        return _entry_direct(rpc, pk, token, dollars, price, started)
    out = buy['out']
    minimum = out * 9700 // 10000
    own_exit = _direct(rpc, pk, token, C.USDG, minimum) or _direct_exit(rpc, pk, token, minimum)
    sell = route.best_quote(token, C.USDG, minimum, extra=[own_exit], expect=minimum / 1e18 * price * 1e6,
                            max_fee_usd=dollars * route.MAX_FEE, lane=lane)
    if not own_exit or own_exit['out'] < DIRECT_EXIT_FLOOR * sell['out']:
        raise ValueError('no direct exit path within the floor')
    fee, sell_fee = gas_cost(rpc, buy['gas']), gas_cost(rpc, sell['gas'])
    impact = 1 - (out / 1e18 * price / dollars)
    roundtrip = (sell['out'] * (1 - SLIPPAGE) / 1e6 - fee - sell_fee) / dollars
    _limits(impact, roundtrip, fee, dollars)
    if time.time() >= started + QUOTE_TTL:
        raise ValueError('execution quote expired')
    return {'pool': pk, 'amount_raw': amount, 'out_raw': out, 'minimum_raw': minimum,
            'paper_fill_raw': int(out * (1 - PAPER_FILL)),
            'gas_usd': fee, 'quoted_at': started, 'expires_at': started + QUOTE_TTL,
            'direction': buy.get('direction'), 'price': price, 'roundtrip_ratio': roundtrip,
            'liquidation_usd': sell['out'] * 9700 // 10000 / 1e6 - sell_fee, 'model': MODEL,
            'route': buy, 'provider': buy['provider'], 'live_fill': True, 'direct_exit': own_exit['provider']}


def _entry_direct(rpc, pk, token, dollars, price, started):
    """Paper only, no route answering: the token's own ETH pool, quoted both ways as before routing."""
    from .trader import quote_buy
    unit, usd = quote_unit(pk)
    amount = int(dollars / usd * unit)
    out, gas, direction = quote_buy(rpc, pk, token, amount)
    if out <= 0:
        raise ValueError('buy quote unavailable')
    minimum = out * 9700 // 10000
    impact = 1 - (out / 1e18 * price / dollars)
    back, sell_gas, _ = quote_buy(rpc, pk, pk['quote'], minimum)
    fee = gas_cost(rpc, gas)
    back_usd = back * (1 - FALLBACK_HAIRCUT) / unit * usd
    roundtrip = (back_usd * (1 - SLIPPAGE) - fee - gas_cost(rpc, sell_gas)) / dollars
    _limits(impact, roundtrip, fee, dollars)
    if time.time() >= started + QUOTE_TTL:
        raise ValueError('execution quote expired')
    return {'pool': pk, 'amount_raw': amount, 'out_raw': out, 'minimum_raw': minimum,
            'paper_fill_raw': int(out * (1 - PAPER_FILL)),
            'gas_usd': fee, 'quoted_at': started, 'expires_at': started + QUOTE_TTL,
            'direction': direction, 'price': price, 'roundtrip_ratio': roundtrip,
            'liquidation_usd': back_usd * .97 - gas_cost(rpc, sell_gas), 'model': MODEL,
            'route': None, 'provider': 'pool-eth', 'live_fill': False}


def exit_quote(rpc, pk, token, amount, *, tolerance=None, cached_prices=False, fallback=False, reference=None,
               providers=route.TRADE, direct=True, lane='paper'):
    """A sell of `amount` tokens into USDG by the best checked route, the direct exit included: the token's own
    USDG pool, or its own pool and the other asset's v3 pool to USDG. `reference`, the pool's USD mid when the
    caller has it, refuses only a route paying suspiciously more than it (never the direct exit). fallback (paper
    rows live could not have made only): with no route and an ETH pool, the pool is quoted directly, less
    FALLBACK_HAIRCUT, and marked live_fill=False. cached_prices: no price request on the exit path."""
    from .trader import quote_buy
    tolerance = EXIT_TOLERANCE if tolerance is None else tolerance
    if not 0 < tolerance <= EXIT_TOLERANCE_RETRY:
        raise ValueError('sell tolerance out of bounds')
    if rpc is None or not verified(pk, token) or amount <= 0:
        raise ValueError('sell quote unavailable')
    started = time.time()
    cached = eth_usd_cached() if cached_prices else None
    if cached_prices and cached is None:
        raise ValueError('fresh cached gas pricing unavailable')
    # On the paper book's budgeted read: the gas price and the pool's own quotes first, the aggregators last and
    # for at most EXIT_WAIT_S, so one slow provider cannot starve the reads the quote needs.
    gas_price = int(rpc.call('eth_gasPrice', []), 16) if cached_prices else None
    expect = amount / 1e18 * reference * 1e6 if reference else None
    try:
        own = (_direct(rpc, pk, token, C.USDG, amount) or _direct_exit(rpc, pk, token, amount)) if direct else None
        q = route.best_quote(token, C.USDG, amount, extra=[own], expect=expect, providers=providers, side='exit', lane=lane,
                             wait_s=EXIT_WAIT_S)
        (unit, usd), out, gas, direction, haircut = (10 ** 6, 1.0), q['out'], q['gas'], q.get('direction'), 0.0
    except route.NoRoute:
        if not fallback or pk['quote'] != C.ZERO:
            raise
        q, haircut = None, FALLBACK_HAIRCUT
        unit, usd = (10 ** 18, cached) if cached_prices else quote_unit(pk)
        out, gas, direction = quote_buy(rpc, pk, pk['quote'], amount)
    minimum = out * (10000 - int(round(tolerance * 10000))) // 10000
    if minimum <= 0:
        raise ValueError('no executable sell quote')
    fee = _gas_cost_at_price(rpc, gas, cached, gas_price) if cached_prices else gas_cost(rpc, gas)
    if time.time() >= started + QUOTE_TTL:
        raise ValueError('sell quote expired')
    return {'amount_raw': amount, 'out_raw': out, 'minimum_raw': minimum,
            'minimum_usd': minimum / unit * usd * (1 - haircut),
            'paper_fill_usd': out * (1 - PAPER_FILL) / unit * usd * (1 - haircut),
            'gas_usd': fee, 'quoted_at': started, 'expires_at': started + QUOTE_TTL,
            'direction': direction, 'pool': pk, 'route': q, 'provider': q['provider'] if q else 'pool-eth',
            'live_fill': q is not None}
