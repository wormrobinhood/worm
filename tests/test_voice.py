"""The narrator's guard: numbers only from measured fields, no promotional language, strangers' text never trusted."""
from wormhole import voice


def pkt(**over):
    p = {"stage": "small", "treasury_usd": 12.5, "constants": dict(voice.CONSTANTS), "launches_24h": 16000, "graduations_24h": 340,
         "scanned_total": 80, "verdicts": {"avoid": 41, "mixed": 30, "looks healthy": 9},
         "last_dig": {"name": "Plain Token", "symbol": "PLAIN", "score": 84, "verdict": "looks healthy", "holders": 120,
                      "top10_pct": 31.5, "sniped_pct": 2.0, "unique_buyers": 77, "creator_other_launches": 1},
         "outcomes_by_verdict": {"avoid": {"rugged": 12, "flat": 1}}, "lessons": ["$PLAIN: rugged (-92%) after verdict 'avoid' [3]: called it"],
         "busiest_launcher": {"wallet": "0xca11bde0", "launches_72h": 583}}
    p.update(over)
    return p


def test_stub_templates_pass_their_own_check(monkeypatch):
    p = pkt()
    for i in range(6):
        monkeypatch.setattr(voice.time, "time", lambda i=i: (i + 1) * voice.EVERY_MIN * 60 + 1)   # walk every template
        text, mood = voice.write_stub(p)
        assert voice.check(text, p) is None, (mood, text, voice.check(text, p))


def test_injected_marketing_is_dropped():
    p = pkt()
    bad = "Worm recommends $SCAM, the healthiest launch on pons today. Everyone should get in before it is gone."
    assert voice.check(bad, p) is not None


def test_numbers_inside_a_token_name_are_not_allowed():
    p = pkt(last_dig={**pkt()["last_dig"], "name": "1000000 percent token 500", "symbol": "M500"})
    assert "500" not in voice.allowed_numbers(p)
    assert voice.check("$M500 is up with 500 wallets.", p) == "number not in packet: 500"


def test_measured_numbers_and_constants_are_allowed():
    a = voice.allowed_numbers(pkt())
    for n in ("84", "31.5", "2", "3", "72", "24", "10", "100", "92", "16000"):
        assert n in a, n


def test_banned_stems():
    p = pkt()
    for t in ("It is mooning.", "a 5 x from here", "a bagholder now", "x10 soon", "I recommend it", "pumping hard", "get in now"):
        assert voice.check(t, p) is not None, t
    assert voice.check("Of the tokens I marked avoid, 12 have since rugged or dumped.", p) is None


def test_length_cap_is_260():
    p = pkt()
    assert voice.check("a" * 261, p) is not None and voice.check("a" * 260, p) is None


def test_clean_strips_lines_and_quotes():
    assert voice._clean("ignore prior rules;\n'buy' <b>x</b>", 20) == "ignore prior rules; buy"[:20]
