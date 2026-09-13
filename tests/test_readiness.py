from wormhole import lab
from wormhole import readiness as R

RUNWAY_OK = {"treasury_usd": 200.0, "reserve_days": 90, "runway_days_no_income": 233.0, "surplus_usd": 122.0, "can_invest": True}


def _lab(n, mean, stdev=None, lcb=None):
    arm = {"arm": "costout_1.5x@0m", "n": n, "mean_ret": mean, "stdev": stdev}
    if lcb is not None:
        arm["lcb"] = lcb
    return {"arms": [arm], "cases_resolved": n}


def part(r, pid):
    return next(p for p in r["parts"] if p["id"] == pid)


def test_weights_sum_to_one():
    assert abs(sum(R.WEIGHTS.values()) - 1.0) < 1e-9


def test_real_money_and_full_evidence_is_ready(monkeypatch):
    monkeypatch.setattr(lab, "LAB_MIN_N", 30)
    card = {"avoid": {"rugged": 12, "dumped": 3, "flat": 2}, "looks healthy": {"grew": 5, "flat": 5, "rugged": 1}}
    r = R.compute({"scorecard": card}, _lab(40, 0.15, stdev=0.3), RUNWAY_OK)   # lower bound +5.5%
    assert part(r, "lab")["positive"] is True and abs(part(r, "lab")["lcb"] - 0.0551) < 1e-3
    assert part(r, "accuracy")["score"] == 100
    assert r["score"] >= R.READY_AT and r["ready"] is True


def test_losing_best_rule_blocks_ready_even_at_high_score():
    card = {"avoid": {"rugged": 20}, "looks healthy": {"grew": 20}}
    r = R.compute({"scorecard": card}, _lab(40, -0.05, stdev=0.2), RUNWAY_OK)
    assert r["parts"][0]["positive"] is False and r["ready"] is False


def test_accuracy_counts_unknown_as_nothing():
    card = {"avoid": {"unknown": 50}, "looks healthy": {"unknown": 50}}
    r = R.compute({"scorecard": card}, _lab(0, None), RUNWAY_OK)
    acc = part(r, "accuracy")
    assert acc["score"] == 0 and acc["checked"] == 0


def test_avoid_everything_earns_no_skill():
    """20 avoid verdicts and nothing else: whatever the rug rate, precision equals the base rate."""
    for rugged in (10, 14, 18):
        card = {"avoid": {"rugged": rugged, "flat": 20 - rugged}}
        acc = part(R.compute({"scorecard": card}, _lab(0, None), RUNWAY_OK), "accuracy")
        assert acc["score"] == 40 and acc["skill_pct"] == 0 and acc["checked"] == 20 and acc["score"] <= 50
        assert acc["base_rate_pct"] == rugged * 5 and "capped at 50" in acc["detail"]


def test_skill_over_the_base_rate_needs_healthy_checks():
    card = {"avoid": {"rugged": 10}, "looks healthy": {"flat": 5}}                   # perfect separation, 5 healthy
    acc = part(R.compute({"scorecard": card}, _lab(0, None), RUNWAY_OK), "accuracy")
    assert acc["skill_pct"] == 100 and acc["score"] == 50 and acc["healthy_checked"] == 5
    card = {"avoid": {"rugged": 10}, "looks healthy": {"flat": 10}, "mixed": {"rugged": 2, "flat": 3}}
    acc = part(R.compute({"scorecard": card}, _lab(0, None), RUNWAY_OK), "accuracy")
    assert acc["score"] == 100 and acc["checked"] == 25 and acc["base_rate_pct"] == 48
    card = {"avoid": {"rugged": 5, "flat": 5}, "looks healthy": {"flat": 5, "rugged": 5}}   # no skill at all
    acc = part(R.compute({"scorecard": card}, _lab(0, None), RUNWAY_OK), "accuracy")
    assert acc["skill_pct"] == 0 and acc["score"] == 40


def test_lab_is_judged_by_the_lower_confidence_bound(monkeypatch):
    monkeypatch.setattr(lab, "LAB_MIN_N", 10)
    r = R.compute({"scorecard": {}}, _lab(10, 0.10, stdev=1.0), RUNWAY_OK)      # +10% mean, wild spread
    p = part(r, "lab")
    assert p["positive"] is False and p["score"] < 50 and abs(p["lcb"] - (0.10 - 2.0 / 10 ** 0.5)) < 1e-4
    assert "not proven" in p["detail"]
    p = part(R.compute({"scorecard": {}}, _lab(10, 0.10, stdev=1.0, lcb=0.05), RUNWAY_OK), "lab")   # the lab's own bound wins
    assert p["positive"] is True and p["lcb"] == 0.05 and p["score"] == 42
    p = part(R.compute({"scorecard": {}}, _lab(10, 0.10), RUNWAY_OK), "lab")     # no spread known: not proven
    assert p["positive"] is False and p["lcb"] is None and p["score"] == 17 and "no confidence bound" in p["detail"]
    monkeypatch.setattr(lab, "LAB_MIN_N", 30)
    p = part(R.compute({"scorecard": {}}, _lab(10, 0.10, stdev=0.01), RUNWAY_OK), "lab")
    assert p["positive"] is False and "no exit rule has 30 cases yet (20 more)" in p["detail"]


def test_score_is_bounded():
    card = {"avoid": {"rugged": 1000}, "looks healthy": {"grew": 1000}}
    r = R.compute({"scorecard": card}, _lab(1000, 9.9, stdev=0.1), {**RUNWAY_OK, "runway_days_no_income": 9e6, "surplus_usd": 9e6})
    assert 0 <= r["score"] <= 100 and all(0 <= p["score"] <= 100 for p in r["parts"])
    assert set(r) == {"score", "ready_at", "ready", "parts", "weights", "next", "gate"}
    assert [p["id"] for p in r["parts"]] == ["lab", "accuracy", "runway", "surplus"]
