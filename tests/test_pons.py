"""Pons reads: symbols capped and never stuck on a failure; metadata that admits what it does not know."""
from eth_abi import encode

from wormhole import pons
from wormhole.chain import selector
from wormhole.pons import pair_symbols, token_metadata

A = "0x" + "aa" * 20
B = "0x" + "bb" * 20
CURVE = "0x" + "cc" * 20


def ret(types, values):
    return "0x" + encode(list(types), list(values)).hex()


class Answers:
    """rpc.batch stand-in: answers eth_call by (target, selector); anything unknown is None, a failed call."""

    def __init__(self, answers):
        self.answers, self.calls = answers, 0

    def batch(self, calls):
        self.calls += 1
        return [self.answers.get((p[0]["to"], p[0]["data"][:10])) for _, p in calls]


def test_pair_symbols_truncates_and_caches(monkeypatch):
    monkeypatch.setattr(pons, "_SYMBOL_CACHE", {})
    node = Answers({(A, selector("symbol()")): ret(["string"], ["X" * 5000])})
    assert pair_symbols(node, [A]) == {A: "X" * 24}
    assert pair_symbols(node, [A, A]) == {A: "X" * 24} and node.calls == 1      # cached


def test_pair_symbols_fallback_is_not_sticky(monkeypatch):
    monkeypatch.setattr(pons, "_SYMBOL_CACHE", {})
    assert pair_symbols(Answers({}), [B]) == {B: B[:8]}
    assert B not in pons._SYMBOL_CACHE
    assert pair_symbols(Answers({(B, selector("symbol()")): ret(["string"], ["OK"])}), [B]) == {B: "OK"}


def test_token_metadata_keeps_none_for_failed_reads():
    m = token_metadata(Answers({}), [A], with_curve={A: CURVE})[A]
    assert m["name"] is None and m["symbol"] is None and m["logo"] is None and m["twitter"] is None
    assert m["creator_tax_bps"] is None and m["curve_fee_bps"] is None and m["buyback"] is None


def test_token_metadata_partial_failure_keeps_what_it_got():
    node = Answers({(A, selector("name()")): ret(["string"], ["Worm"]), (A, selector("symbol()")): ret(["string"], ["WORM"]),
                    (CURVE, selector("creatorTaxBps()")): ret(["uint256"], [250]),
                    (CURVE, selector("feeBps()")): ret(["uint256"], [100]),
                    (CURVE, selector("buybackEnabled()")): ret(["bool"], [True])})
    m = token_metadata(node, [A], with_curve={A: CURVE})[A]
    assert m["name"] == "Worm" and m["symbol"] == "WORM"
    assert m["twitter"] is None and m["website"] is None           # socials() failed: unknown, not empty
    assert m["creator_tax_bps"] == 250 and m["curve_fee_bps"] == 100 and m["buyback"] is True


def test_token_metadata_empty_social_is_an_answer():
    node = Answers({(A, selector("socials()")): ret(["string"] * 5, ["", "t.me/x", "", "", ""])})
    m = token_metadata(node, [A])[A]
    assert m["twitter"] == "" and m["telegram"] == "t.me/x" and "creator_tax_bps" not in m
