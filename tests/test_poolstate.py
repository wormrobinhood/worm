"""Live pool prices from PoolManager storage: the id, the slot, the price maths and the batched read."""
from eth_abi import encode
from eth_utils import keccak

from wormhole import config as C, poolstate

TOKEN_LOW = "0x43b182a7d5dbc0e96e8ee5ea3c6a0a08b4569274"      # sorts before USDG: currency0
TOKEN_HIGH = "0x" + "f1" * 20                                   # sorts after USDG and ETH: currency1
WORM_POOL = {"c0": TOKEN_LOW, "c1": C.USDG, "fee": 0, "tick_spacing": 200, "hooks": C.HOOK, "quote": C.USDG}


def word(sqrt_x96, tick=0):
    return "0x" + (((tick & 0xFFFFFF) << 160) | sqrt_x96).to_bytes(32, "big").hex()


def sqrt_for(ratio):
    return int(ratio ** 0.5 * poolstate.Q96)


class BatchRpc:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def batch(self, calls):
        self.calls.append(calls)
        return list(self.answers)


def test_pool_id_matches_the_chain():
    # the $WORM pool: id read from its PoolRegistered and Initialize logs on Robinhood Chain
    assert poolstate.pool_id(WORM_POOL) == "0x8c3e2408f38d22325e294e68303aae74bd18a130eaa1897d7839211472343389"


def test_state_slot_is_the_pools_mapping_entry():
    pid = poolstate.pool_id(WORM_POOL)
    assert poolstate.state_slot(pid) == "0x" + keccak(encode(["bytes32", "uint256"], [bytes.fromhex(pid[2:]), 6])).hex()


def test_sqrt_price_ignores_the_tick_and_fee_bits():
    assert poolstate.sqrt_price(word(123456789, tick=-402453)) == 123456789
    assert poolstate.sqrt_price("0x" + "00" * 32) is None        # an uninitialised pool is no price
    assert poolstate.sqrt_price(None) is None and poolstate.sqrt_price("0xzz") is None


def test_token_as_currency0_against_usdg():
    # 3.33e-6 USDG per token: raw USDG (6 decimals) per raw token (18 decimals) is 3.33e-18
    sp = sqrt_for(3.33e-18)
    assert abs(poolstate.token_price(WORM_POOL, TOKEN_LOW, sp, 1.0) / 3.33e-6 - 1) < 1e-6


def test_token_as_currency1_against_eth():
    pk = {"c0": C.ZERO, "c1": TOKEN_HIGH, "fee": 0, "tick_spacing": 200, "hooks": C.HOOK, "quote": C.ZERO}
    sp = sqrt_for(50_000_000)                                      # 50M tokens per ETH
    assert abs(poolstate.token_price(pk, TOKEN_HIGH, sp, 2500.0) / (2500.0 / 50_000_000) - 1) < 1e-6


def test_token_as_currency1_against_usdg():
    pk = {"c0": C.USDG, "c1": TOKEN_HIGH, "fee": 0, "tick_spacing": 200, "hooks": C.HOOK, "quote": C.USDG}
    sp = sqrt_for(1e12 / 2e-5)                                     # raw token per raw USDG at $0.00002
    assert abs(poolstate.token_price(pk, TOKEN_HIGH, sp, 1.0) / 2e-5 - 1) < 1e-6


def test_unknown_quote_or_missing_eth_price_is_no_price():
    pk = {**WORM_POOL, "quote": "0x" + "ab" * 20}
    assert poolstate.token_price(pk, TOKEN_LOW, sqrt_for(1e-18), 1.0) is None
    eth = {"c0": C.ZERO, "c1": TOKEN_HIGH, "fee": 0, "tick_spacing": 200, "hooks": C.HOOK, "quote": C.ZERO}
    assert poolstate.token_price(eth, TOKEN_HIGH, sqrt_for(5e7), None) is None
    assert poolstate.token_price(WORM_POOL, TOKEN_LOW, None, 1.0) is None


def test_mids_reads_every_pool_in_one_batch():
    eth = {"c0": C.ZERO, "c1": TOKEN_HIGH, "fee": 0, "tick_spacing": 200, "hooks": C.HOOK, "quote": C.ZERO}
    rpc = BatchRpc([word(sqrt_for(3.33e-18)), word(sqrt_for(5e7)), None])
    dead = "0x" + "e1" * 20
    out = poolstate.mids(rpc, {TOKEN_LOW: WORM_POOL, TOKEN_HIGH: eth, dead: {**WORM_POOL, "c0": dead}}, 2000.0)
    assert len(rpc.calls) == 1 and len(rpc.calls[0]) == 3
    method, params = rpc.calls[0][0]
    assert method == "eth_call" and params[0]["to"] == C.POOL_MANAGER and params[0]["data"].startswith(poolstate.EXTSLOAD)
    assert abs(out[TOKEN_LOW] / 3.33e-6 - 1) < 1e-6 and abs(out[TOKEN_HIGH] / (2000.0 / 5e7) - 1) < 1e-6
    assert out[dead] is None                                       # a call the node did not answer


def test_mids_without_pools_makes_no_call():
    rpc = BatchRpc([])
    assert poolstate.mids(rpc, {}, 2000.0) == {} and rpc.calls == []
