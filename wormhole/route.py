"""Best-of routing between USDG and any Pons token, through the aggregators that serve Robinhood Chain.

Most graduations pair with ETH or a tokenized stock, and the wallet holds USDG. KyberSwap and LI.FI route
USDG to ETH or the stock and on to the token (and back) in one transaction; Relay quotes the same. best_quote
asks them in parallel, adds the token's own Pons pool when it trades against USDG, and returns the most tokens
out among the routes the worm could really execute and check. A quote names a dummy address, never the worm's
wallet; only build() (live, dormant) names the wallet. Nothing here signs or sends.

Executable: the Pons pool (the Universal Router path trader.py builds), KyberSwap and LI.FI, whose calldata is
decoded here so the recipient, tokens, amount and on-chain minimum are checked before anything is signed.
Relay's calldata is a multicall whose minimum lives inside third-party calls: its quote is recorded and
compared, never executed, and the paper book never fills at a price live could not check."""
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait as _wait

import requests
from eth_abi import decode, encode

from . import config as C
from .chain import selector

log = logging.getLogger("wormhole.route")

DUMMY = "0x000000000000000000000000000000000000dead"   # the address every read-only quote names
TIMEOUT_S = 4.0            # one provider request
WAIT_S = 5.0               # best_quote waits this long for every provider; a late answer is dropped
MIN_GAP_S = 0.25           # per provider: at most four requests a second, whatever the callers
BREAK_AFTER = 3            # consecutive failures that stand a provider down
BREAK_BASE_S = 30          # its first pause; each further failure doubles it ...
BREAK_MAX_S = 900          # ... up to this. A rate limit stands it down at once
QUOTE_TTL = 30             # a quote is acted on within this, like every other execution quote
MID_BAND = 0.15            # a route whose price is further than this from the pool's own mid is not believed
MAX_FEE = 0.01             # an aggregator's own fee on one leg, as a share of the order, at most
BUILD_SLIPPAGE_BPS = 250   # the minimum a built transaction encodes, below its own fresh quote: tighter than the
                           # book's 3%, so the built minimum sits above ours unless the route moved against us

KYBER = "https://aggregator-api.kyberswap.com/robinhood/api/v1"
LIFI = "https://li.quest/v1"
RELAY = "https://api.relay.link"
HEADERS = {"User-Agent": "wormhole/0.1", "Accept": "application/json", "x-client-id": "wormhole"}

# Every address a transaction may be sent to, or an allowance granted to, per provider (seen on 2026-09-28 in
# live quotes). A quote naming anything else is not executable; a built transaction naming anything else is refused.
KYBER_ROUTER = "0x6131b5fae19ea4f9d964eac0408e4408b66337b5"     # MetaAggregationRouterV2: router and spender
LIFI_DIAMOND = "0xb477751b76cf82d00a686a1232f5fcd772414af3"     # LI.FI on chain 4663: router and spender
RELAY_PROXY = "0xccc88a9d1b4ed6b0eaba998850414b24f1c315be"      # Relay's approval proxy (quote-only here)
ALLOWED = {"pons": {"to": (C.UNIVERSAL_ROUTER,), "spender": (C.PERMIT2,)},
           "kyber": {"to": (KYBER_ROUTER,), "spender": (KYBER_ROUTER,)},
           "lifi": {"to": (LIFI_DIAMOND,), "spender": (LIFI_DIAMOND,)},
           "relay": {"to": (RELAY_PROXY,), "spender": (RELAY_PROXY,)}}
PROVIDERS = ("kyber", "lifi", "relay")      # asked by best_quote
EXECUTABLE = ("pons", "kyber", "lifi")      # may be chosen, on paper and live alike
TRADE = ("kyber", "lifi")                   # asked on the exit and valuation paths: only what could be chosen

KYBER_SWAP = selector("swap((address,address,bytes,(address,address,address[],uint256[],address[],uint256[],"
                      "address,uint256,uint256,uint256,bytes),bytes))")
KYBER_T = "(address,address,bytes,(address,address,address[],uint256[],address[],uint256[],address,uint256,uint256,uint256,bytes),bytes)"
LIFI_SWAP_T = "(address,address,address,address,uint256,bytes,bool)"
LIFI_MULTI = selector(f"swapTokensMultipleV3ERC20ToERC20(bytes32,string,string,address,uint256,{LIFI_SWAP_T}[])")
LIFI_SINGLE = selector(f"swapTokensSingleV3ERC20ToERC20(bytes32,string,string,address,uint256,{LIFI_SWAP_T})")
APPROVE = selector("approve(address,uint256)")
BALANCE_OF = "0x70a08231"


class NoRoute(ValueError):
    """No executable route within the limits. `found` and `failed` say what was seen."""
    def __init__(self, msg, found=(), failed=None):
        super().__init__(msg)
        self.found, self.failed = list(found), dict(failed or {})


class ProviderError(Exception):
    pass


class RateLimited(ProviderError):
    pass


class UnsafeRoute(ValueError):
    pass


# ---- providers: pacing and circuit breakers --------------------------------------------------------

class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.last = 0.0
        self.fails = 0
        self.open_until = 0.0


_state = {p: _State() for p in PROVIDERS}
_session = requests.Session()
_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="route")


def reset():
    """Forget every provider's pacing and failures (tests; a fresh process starts this way)."""
    for p in PROVIDERS:
        _state[p] = _State()


def status():
    """{provider: {fails, backing_off_s}}: what the breakers hold now."""
    now = time.time()
    return {p: {"fails": s.fails, "backing_off_s": max(0, round(s.open_until - now))} for p, s in _state.items()}


def _available(p, now=None):
    return (now or time.time()) >= _state[p].open_until


def _pace(p):
    """Reserve the provider's next slot, MIN_GAP_S after the last, and wait for it outside the lock."""
    s = _state[p]
    with s.lock:
        now = time.time()
        slot = max(now, s.last + MIN_GAP_S)
        s.last = slot
    if slot > now:
        time.sleep(slot - now)


def _record(p, ok, limited=False):
    """A success closes the breaker. BREAK_AFTER failures in a row (or one rate limit) open it for
    BREAK_BASE_S, doubling with every further failure up to BREAK_MAX_S; the first request after that is the probe."""
    s = _state[p]
    with s.lock:
        if ok:
            s.fails, s.open_until = 0, 0.0
            return
        s.fails += 1
        if limited or s.fails >= BREAK_AFTER:
            k = max(1, s.fails - BREAK_AFTER + 1)
            s.open_until = time.time() + min(BREAK_MAX_S, BREAK_BASE_S * 2 ** (k - 1))


def _request(method, url, **kw):
    r = _session.request(method, url, timeout=TIMEOUT_S, headers=HEADERS, **kw)
    if r.status_code == 429:
        raise RateLimited("rate limited")
    if r.status_code >= 400:
        raise ProviderError(f"http {r.status_code}")
    return r.json()


def _int(v):
    if isinstance(v, int):
        return v
    s = str(v or "0")
    return int(s, 16) if s.startswith("0x") else int(s)


def _addr(v):
    return (v or "").lower()


# ---- quotes ---------------------------------------------------------------------------------------

def candidate(provider, token_in, token_out, amount_in, out, gas, fee_usd=0.0, to=None, spender=None, **extra):
    """One route, normalised. Executable only for a provider allowed to execute whose router and spender are on
    its allowlist: a provider that moves its contracts is noticed, never followed."""
    out, amount_in = int(out), int(amount_in)
    if out <= 0 or amount_in <= 0 or not math.isfinite(float(fee_usd)) or fee_usd < 0:
        raise ProviderError("no usable amount out")
    to, spender = _addr(to), _addr(spender)
    allowed = ALLOWED.get(provider) or {"to": (), "spender": ()}
    now = time.time()
    return {"provider": provider, "token_in": _addr(token_in), "token_out": _addr(token_out), "amount_in": amount_in,
            "out": out, "gas": int(gas or 0), "fee_usd": float(fee_usd), "to": to, "spender": spender,
            "executable": provider in EXECUTABLE and to in allowed["to"] and spender in allowed["spender"],
            "quoted_at": now, "expires_at": now + QUOTE_TTL, **extra}


def parse_kyber(j, token_in, token_out, amount_in):
    if not isinstance(j, dict) or j.get("code") != 0:
        raise ProviderError("no route")
    d = j["data"]
    rs = d["routeSummary"]
    if _addr(rs["tokenIn"]) != _addr(token_in) or _addr(rs["tokenOut"]) != _addr(token_out) or int(rs["amountIn"]) != int(amount_in):
        raise ProviderError("route answers another question")
    fee = rs.get("extraFee") or {}
    if str(fee.get("feeAmount") or "0") not in ("", "0"):
        raise ProviderError("unexpected integrator fee")          # none is asked for; one appearing is not ours
    router = d.get("routerAddress") or rs.get("routerAddress")
    return candidate("kyber", token_in, token_out, amount_in, int(rs["amountOut"]), int(rs.get("gas") or 0), 0.0,
                     router, router, route_summary=rs)


def parse_lifi(j, token_in, token_out, amount_in):
    if not isinstance(j, dict) or "estimate" not in j:
        raise ProviderError("no route")
    e, a, tx = j["estimate"], j.get("action") or {}, j.get("transactionRequest") or {}
    if (_addr((a.get("fromToken") or {}).get("address")) != _addr(token_in)
            or _addr((a.get("toToken") or {}).get("address")) != _addr(token_out) or int(e["fromAmount"]) != int(amount_in)):
        raise ProviderError("route answers another question")
    fees = e.get("feeCosts") or []
    if any(f.get("included") is False for f in fees):
        raise ProviderError("a fee charged on top of the order")   # would be paid in ETH value: never
    fee_usd = sum(float(f.get("amountUSD") or 0) for f in fees)
    gas = sum(_int(g.get("estimate") or 0) for g in e.get("gasCosts") or [])
    return candidate("lifi", token_in, token_out, amount_in, int(e["toAmount"]), gas, fee_usd, tx.get("to"),
                     e.get("approvalAddress"), tx={"to": _addr(tx.get("to")), "data": tx.get("data"), "value": _int(tx.get("value"))})


def parse_relay(j, token_in, token_out, amount_in):
    if not isinstance(j, dict) or "details" not in j:
        raise ProviderError("no route")
    d = j["details"]
    if (_addr(d["currencyIn"]["currency"]["address"]) != _addr(token_in) or int(d["currencyIn"]["amount"]) != int(amount_in)
            or _addr(d["currencyOut"]["currency"]["address"]) != _addr(token_out)):
        raise ProviderError("route answers another question")
    steps = {s.get("id"): s for s in j.get("steps") or []}
    swap = [(i.get("data") or {}) for i in (steps.get("swap") or {}).get("items") or []]
    approve = [(i.get("data") or {}) for i in (steps.get("approve") or {}).get("items") or []]
    if not swap:
        raise ProviderError("no swap step")
    spender = None
    if approve and (approve[0].get("data") or "").startswith(APPROVE):
        spender = "0x" + approve[0]["data"][10 + 24:10 + 64]
    fees = j.get("fees") or {}
    fee_usd = sum(float((fees.get(k) or {}).get("amountUsd") or 0) for k in ("relayer", "app"))
    return candidate("relay", token_in, token_out, amount_in, int(d["currencyOut"]["amount"]),
                     sum(_int(s.get("gas") or 0) for s in swap), fee_usd, swap[-1].get("to"), spender or swap[-1].get("to"))


def _kyber(token_in, token_out, amount_in):
    j = _request("GET", f"{KYBER}/routes", params={"tokenIn": token_in, "tokenOut": token_out, "amountIn": str(amount_in)})
    return parse_kyber(j, token_in, token_out, amount_in)


def _lifi_params(token_in, token_out, amount_in, sender, slippage=None):
    params = {"fromChain": C.CHAIN_ID, "toChain": C.CHAIN_ID, "fromToken": token_in, "toToken": token_out,
              "fromAmount": str(amount_in), "fromAddress": sender, "toAddress": sender}
    if slippage is not None:
        params["slippage"] = slippage
    return params


def _lifi(token_in, token_out, amount_in):
    return parse_lifi(_request("GET", f"{LIFI}/quote", params=_lifi_params(token_in, token_out, amount_in, DUMMY)),
                      token_in, token_out, amount_in)


def _relay(token_in, token_out, amount_in):
    j = _request("POST", f"{RELAY}/quote", json={"user": DUMMY, "recipient": DUMMY, "originChainId": C.CHAIN_ID,
                                                  "destinationChainId": C.CHAIN_ID, "originCurrency": token_in,
                                                  "destinationCurrency": token_out, "amount": str(amount_in),
                                                  "tradeType": "EXACT_INPUT"})
    return parse_relay(j, token_in, token_out, amount_in)


ASK = {"kyber": _kyber, "lifi": _lifi, "relay": _relay}


def _ask(p, token_in, token_out, amount_in):
    _pace(p)
    try:
        c = ASK[p](token_in, token_out, amount_in)
    except RateLimited:
        _record(p, False, limited=True)
        raise
    except Exception:
        _record(p, False)
        raise
    _record(p, True)
    return c


def _why(e):
    return "rate limited" if isinstance(e, RateLimited) else type(e).__name__ if not isinstance(e, ProviderError) else str(e)


def best_quote(token_in, token_out, amount_in, *, extra=(), expect=None, max_fee_usd=None, providers=PROVIDERS, wait_s=WAIT_S):
    """The executable route giving the most `token_out` for `amount_in` of `token_in` (base units), asking
    `providers` in parallel (a provider standing down after failures is skipped) and adding `extra` candidates
    (the token's own Pons pool). `expect` is the amount out the pool's own mid implies: a route further than
    MID_BAND from it is not believed. `max_fee_usd` caps an aggregator's own fee. The result is the chosen
    candidate plus `compared` (every route seen) and `failed` ({provider: why}). Raises NoRoute when nothing
    executable is left."""
    token_in, token_out, amount_in = _addr(token_in), _addr(token_out), int(amount_in)
    if amount_in <= 0:
        raise ValueError("nothing to route")
    found, failed, jobs = [c for c in extra if c], {}, {}
    for p in providers:
        if not _available(p):
            failed[p] = "backing off"
            continue
        jobs[_pool.submit(_ask, p, token_in, token_out, amount_in)] = p
    done, late = _wait(jobs, timeout=wait_s) if jobs else (set(), set())
    for f in done:
        try:
            found.append(f.result())
        except Exception as e:
            failed[jobs[f]] = _why(e)
    for f in late:
        failed[jobs[f]] = "timed out"
    for c in found:
        c["band_ok"] = expect is None or (expect > 0 and abs(c["out"] / expect - 1) <= MID_BAND)
        c["fee_ok"] = max_fee_usd is None or c["fee_usd"] <= max_fee_usd
    usable = [c for c in found if c["executable"] and c["band_ok"] and c["fee_ok"]]
    compared = [{"provider": c["provider"], "out": str(c["out"]), "executable": c["executable"], "band_ok": c["band_ok"],
                 "fee_ok": c["fee_ok"]} for c in found]
    if not usable:
        raise NoRoute("no executable route within limits", found, failed)
    # Most out wins; on a tie the pool itself (no aggregator between the wallet and the pool).
    best = max(usable, key=lambda c: (c["out"], c["provider"] == "pons"))
    return {**best, "compared": compared, "failed": failed}


def summary(q):
    """What a paper row or an order keeps of a route: small, JSON-safe, no calldata."""
    if not q:
        return None
    return {k: (str(q[k]) if k in ("out", "amount_in") else q[k]) for k in
            ("provider", "token_in", "token_out", "amount_in", "out", "gas", "fee_usd", "to", "spender", "quoted_at",
             "compared", "failed") if k in q}


def evidence():
    """What decides a route: part of the paper evidence spec (trade_checks.evidence_spec)."""
    return {"providers": list(PROVIDERS), "executable": list(EXECUTABLE), "mid_band": MID_BAND, "max_fee": MAX_FEE,
            "timeout_s": TIMEOUT_S, "wait_s": WAIT_S, "quote_ttl": QUOTE_TTL,
            "allowed": {p: {k: list(v) for k, v in a.items()} for p, a in sorted(ALLOWED.items())}}


# ---- live (dormant): build, check, simulate ------------------------------------------------------

def build(q, wallet, deadline):
    """The chosen aggregator's transaction for `wallet` (sender and recipient), asked afresh. Live only.
    Returns {to, data, value, out, spender}; check() must pass before it is signed."""
    if q["provider"] == "kyber":
        j = _request("POST", f"{KYBER}/route/build", json={"routeSummary": q["route_summary"], "sender": wallet,
                                                             "recipient": wallet, "slippageTolerance": BUILD_SLIPPAGE_BPS,
                                                             "deadline": int(deadline)})
        if not isinstance(j, dict) or j.get("code") != 0:
            raise ProviderError("build failed")
        d = j["data"]
        return {"to": _addr(d.get("routerAddress")), "data": d["data"], "value": _int(d.get("transactionValue")),
                "out": int(d["amountOut"]), "spender": _addr(d.get("routerAddress"))}
    if q["provider"] == "lifi":
        j = _request("GET", f"{LIFI}/quote", params=_lifi_params(q["token_in"], q["token_out"], q["amount_in"], wallet,
                                                                   BUILD_SLIPPAGE_BPS / 10000))
        c = parse_lifi(j, q["token_in"], q["token_out"], q["amount_in"])
        return {**c["tx"], "out": c["out"], "spender": c["spender"]}
    raise UnsafeRoute("this provider's transactions are not executed")


def decode_kyber(data):
    if not data.startswith(KYBER_SWAP):
        raise UnsafeRoute("unexpected router function")
    (p,) = decode([KYBER_T], bytes.fromhex(data[10:]))
    d = p[3]
    return {"token_in": _addr(d[0]), "token_out": _addr(d[1]), "receiver": _addr(d[6]), "amount_in": int(d[7]),
            "min_out": int(d[8])}


def decode_lifi(data):
    if data.startswith(LIFI_MULTI):
        v = decode(["bytes32", "string", "string", "address", "uint256", f"{LIFI_SWAP_T}[]"], bytes.fromhex(data[10:]))
        swaps = list(v[5])
    elif data.startswith(LIFI_SINGLE):
        v = decode(["bytes32", "string", "string", "address", "uint256", LIFI_SWAP_T], bytes.fromhex(data[10:]))
        swaps = [v[5]]
    else:
        raise UnsafeRoute("unexpected router function")
    if not swaps:
        raise UnsafeRoute("no swap in the transaction")
    return {"token_in": _addr(swaps[0][2]), "token_out": _addr(swaps[-1][3]), "receiver": _addr(v[3]),
            "amount_in": int(swaps[0][4]), "min_out": int(v[4])}


DECODERS = {"kyber": decode_kyber, "lifi": decode_lifi}


def check(q, tx, wallet, min_out):
    """Refuse anything but this provider's own router, an allowance to its own spender, no ETH sent, and calldata
    that pays `wallet` at least `min_out` of the token asked for while spending exactly the amount quoted."""
    p = q.get("provider")
    if p not in DECODERS or p not in EXECUTABLE:
        raise UnsafeRoute("this provider's transactions are not executed")
    allowed = ALLOWED[p]
    if _addr(tx.get("to")) not in allowed["to"]:
        raise UnsafeRoute("router not on the allowlist")
    if _addr(q.get("spender")) not in allowed["spender"] or _addr(tx.get("spender") or q.get("spender")) != _addr(q.get("spender")):
        raise UnsafeRoute("spender not on the allowlist")
    if _int(tx.get("value")) != 0:
        raise UnsafeRoute("a swap from USDG or into USDG never sends ETH")
    d = DECODERS[p](tx.get("data") or "")
    if d["receiver"] != _addr(wallet):
        raise UnsafeRoute("the swap pays someone else")
    if d["token_in"] != q["token_in"] or d["token_out"] != q["token_out"] or d["amount_in"] != int(q["amount_in"]):
        raise UnsafeRoute("the swap is not the one quoted")
    if min_out <= 0 or d["min_out"] < min_out:
        raise UnsafeRoute("the swap's own minimum is below ours")
    return d


def simulate(rpc, wallet, tx, token_in, token_out, amount_in, approve_to=None):
    """eth_simulateV1 of the exact transaction from `wallet` at the latest block, between two readings of both
    balances, after an exact approval to `approve_to` when that is not on chain yet. Returns (received, spent)
    in base units of token_out and token_in. Raises when any call reverts."""
    word = wallet[2:].lower().rjust(64, "0")
    def bal(t):
        return {"from": wallet, "to": t, "data": BALANCE_OF + word}
    calls = [bal(token_out), bal(token_in)]
    if approve_to:
        calls.append({"from": wallet, "to": token_in,
                      "data": APPROVE + encode(["address", "uint256"], [approve_to, int(amount_in)]).hex()})
    calls.append({"from": wallet, "to": tx["to"], "data": tx["data"], "value": "0x0"})
    calls += [bal(token_out), bal(token_in)]
    res = rpc.call("eth_simulateV1", [{"blockStateCalls": [{"calls": calls}]}, "latest"])
    got = (res or [{}])[0].get("calls") or []
    if len(got) != len(calls) or any(c.get("status") != "0x1" for c in got):
        raise UnsafeRoute("simulation reverted")
    def v(i):
        return int(got[i].get("returnData") or "0x", 16) if (got[i].get("returnData") or "0x") != "0x" else 0
    return v(-2) - v(0), v(1) - v(-1)


def verify_simulation(rpc, wallet, q, tx, min_out, approve_to=None):
    """The simulated swap must spend exactly the amount quoted and deliver at least `min_out` to `wallet`."""
    received, spent = simulate(rpc, wallet, tx, q["token_in"], q["token_out"], q["amount_in"], approve_to)
    if spent != int(q["amount_in"]):
        raise UnsafeRoute("the simulated swap spends another amount")
    if received < min_out:
        raise UnsafeRoute("the simulated swap delivers less than the minimum")
    return received


def compare_later(token_in, token_out, amount_in, direct_out, label):
    """Log (never act on) how the best aggregator route compares with a direct pool fill: the burn's check."""
    def run():
        try:
            q = best_quote(token_in, token_out, amount_in)
            log.info("%s: direct pool %d, best route %s %s (%+.2f%%)", label, direct_out, q["provider"], q["out"],
                     100 * (q["out"] / direct_out - 1) if direct_out else 0.0)
        except Exception as e:
            log.info("%s: route comparison unavailable (%s)", label, type(e).__name__)
    threading.Thread(target=run, daemon=True, name="route-compare").start()
