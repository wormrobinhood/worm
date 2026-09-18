from wormhole import readiness as R

RUNWAY_OK = {"treasury_usd": 200.0, "reserve_days": 90, "runway_days_no_income": 233.0, "surplus_usd": 122.0, "can_invest": True}
PASSED = {"passed": True, "passed_rules": ["rule-a"], "rule": "rule-a", "n": 12, "enrolled": 14, "required": 50, "mean_ret": .21, "lcb": .06}


def _lab(validation=None):
    return {"arms": [], "cases_resolved": 0, "validation": validation or {"passed": False, "status": "collecting", "n": 0, "enrolled": 0, "required": 50}}


def part(r, pid):
    return next(p for p in r["parts"] if p["id"] == pid)


def test_weights_sum_to_one():
    assert abs(sum(R.WEIGHTS.values()) - 1.0) < 1e-9


def test_a_passed_paper_cohort_with_real_money_is_ready():
    r = R.compute({"scorecard": {}}, _lab(PASSED), RUNWAY_OK)
    p = part(r, "lab")
    assert p["positive"] is True and p["score"] == 100 and p["lcb"] == .06 and "rule-a" in p["detail"]
    assert r["score"] >= R.READY_AT and r["ready"] is True          # the verdict grade moves the number, it is not the gate


def test_without_a_passed_cohort_nothing_else_can_unlock_trading():
    card = {"avoid": {"rugged": 100}, "looks healthy": {"grew": 100}}
    almost = {"passed": False, "status": "collecting", "rule": "rule-a", "n": 49, "enrolled": 50, "required": 50}
    r = R.compute({"scorecard": card}, _lab(almost), RUNWAY_OK)
    assert r["ready"] is False and part(r, "lab")["score"] == 49 and "49 of 50 positions closed" in r["next"]


def test_a_failed_cohort_shows_its_numbers_and_stays_unproven():
    failed = {"passed": False, "status": "collecting", "rule": "rule-a", "n": 3, "enrolled": 4, "required": 50, "mean_ret": .08, "lcb": -.12}
    p = part(R.compute({"scorecard": {}}, _lab(failed), RUNWAY_OK), "lab")
    assert p["positive"] is False and p["score"] == 3 and "lower bound of -12%" in p["detail"] and "not proven" in p["detail"]


def test_a_pass_without_surplus_is_not_ready():
    r = R.compute({"scorecard": {}}, _lab(PASSED), {**RUNWAY_OK, "surplus_usd": 0.0, "can_invest": False})
    assert r["ready"] is False


def test_accuracy_counts_unknown_as_nothing():
    card = {"avoid": {"unknown": 50}, "looks healthy": {"unknown": 50}}
    acc = part(R.compute({"scorecard": card}, _lab(), RUNWAY_OK), "accuracy")
    assert acc["score"] == 0 and acc["checked"] == 0


def test_avoid_everything_earns_no_skill():
    """20 avoid verdicts and nothing else: whatever the rug rate, precision equals the base rate."""
    for rugged in (10, 14, 18):
        card = {"avoid": {"rugged": rugged, "flat": 20 - rugged}}
        acc = part(R.compute({"scorecard": card}, _lab(), RUNWAY_OK), "accuracy")
        assert acc["score"] == 40 and acc["skill_pct"] == 0 and acc["checked"] == 20 and acc["score"] <= 50
        assert acc["base_rate_pct"] == rugged * 5 and "capped at 50" in acc["detail"]


def test_skill_over_the_base_rate_needs_healthy_checks():
    card = {"avoid": {"rugged": 10}, "looks healthy": {"flat": 5}}                   # perfect separation, 5 healthy
    acc = part(R.compute({"scorecard": card}, _lab(), RUNWAY_OK), "accuracy")
    assert acc["skill_pct"] == 100 and acc["score"] == 50 and acc["healthy_checked"] == 5
    card = {"avoid": {"rugged": 10}, "looks healthy": {"flat": 10}, "mixed": {"rugged": 2, "flat": 3}}
    acc = part(R.compute({"scorecard": card}, _lab(), RUNWAY_OK), "accuracy")
    assert acc["score"] == 100 and acc["checked"] == 25 and acc["base_rate_pct"] == 48
    card = {"avoid": {"rugged": 5, "flat": 5}, "looks healthy": {"flat": 5, "rugged": 5}}   # no skill at all
    acc = part(R.compute({"scorecard": card}, _lab(), RUNWAY_OK), "accuracy")
    assert acc["skill_pct"] == 0 and acc["score"] == 40


def test_score_is_bounded():
    card = {"avoid": {"rugged": 1000}, "looks healthy": {"grew": 1000}}
    r = R.compute({"scorecard": card}, _lab({**PASSED, "lcb": 9.9}), {**RUNWAY_OK, "runway_days_no_income": 9e6, "surplus_usd": 9e6})
    assert 0 <= r["score"] <= 100 and all(0 <= p["score"] <= 100 for p in r["parts"])
    assert set(r) == {"score", "ready_at", "ready", "parts", "weights", "next", "gate"}
    assert [p["id"] for p in r["parts"]] == ["lab", "accuracy", "runway", "surplus"]
