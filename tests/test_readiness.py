from wormhole import readiness as R

RUNWAY_OK = {"treasury_usd": 200.0, "reserve_days": 90, "runway_days_no_income": 233.0, "surplus_usd": 122.0, "can_invest": True}


def _lab(n, mean):
    return {"arms": [{"arm": "costout_1.5x@0m", "n": n, "mean_ret": mean}], "cases_resolved": n}


def test_weights_sum_to_one():
    assert abs(sum(R.WEIGHTS.values()) - 1.0) < 1e-9


def test_empty_evidence_scores_zero_even_with_demo_money():
    r = R.compute({"scorecard": {}}, _lab(0, None), RUNWAY_OK, treasury_is_demo=True)
    assert r["score"] == 0 and r["ready"] is False
    assert [p["score"] for p in r["parts"]] == [0, 0, 0, 0]


def test_demo_treasury_never_counts():
    r = R.compute({"scorecard": {}}, _lab(30, 0.2), RUNWAY_OK, treasury_is_demo=True)
    parts = {p["id"]: p["score"] for p in r["parts"]}
    assert parts["runway"] == 0 and parts["surplus"] == 0
    assert r["ready"] is False


def test_real_money_and_full_evidence_is_ready():
    card = {"avoid": {"rugged": 12, "dumped": 3, "flat": 2}, "looks healthy": {"grew": 5, "flat": 3, "rugged": 1}}
    r = R.compute({"scorecard": card}, _lab(40, 0.15), RUNWAY_OK, treasury_is_demo=False)
    assert r["score"] >= R.READY_AT and r["ready"] is True


def test_losing_best_rule_blocks_ready_even_at_high_score():
    card = {"avoid": {"rugged": 20}, "looks healthy": {"grew": 20}}
    r = R.compute({"scorecard": card}, _lab(40, -0.05), RUNWAY_OK, treasury_is_demo=False)
    assert r["parts"][0]["positive"] is False and r["ready"] is False


def test_accuracy_counts_unknown_as_nothing():
    card = {"avoid": {"unknown": 50}, "looks healthy": {"unknown": 50}}
    r = R.compute({"scorecard": card}, _lab(0, None), RUNWAY_OK, treasury_is_demo=False)
    acc = next(p for p in r["parts"] if p["id"] == "accuracy")
    assert acc["score"] == 0 and acc["checked"] == 0


def test_score_is_bounded():
    card = {"avoid": {"rugged": 1000}, "looks healthy": {"grew": 1000}}
    r = R.compute({"scorecard": card}, _lab(1000, 9.9), {**RUNWAY_OK, "runway_days_no_income": 9e6, "surplus_usd": 9e6}, False)
    assert 0 <= r["score"] <= 100 and all(0 <= p["score"] <= 100 for p in r["parts"])
