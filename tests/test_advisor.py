"""The advisor: proposals in a strict form, judged on the worm's own history, adopted only when they
separate outcomes; learned rules score like any other and learned arms join the lab."""
import json
import random
import time

import pytest

from wormhole import advisor as A, config as C, lab

NOW = int(time.time())


@pytest.fixture(autouse=True)
def clean_lab(monkeypatch):
    lab.LEARNED.clear()
    monkeypatch.setattr(A, "MODEL", "stub")
    yield
    lab.LEARNED.clear()


def scored(db, token, m, change, fired=None, scored_at=None, verdict_at=None):
    ts = scored_at or NOW - 90000
    db.x("INSERT OR REPLACE INTO scores(token,score,verdict,reasons,metrics,scored_at,partial,fired) VALUES(?,?,?,?,?,?,0,?)",
         (token, 50, "mixed", "[]", json.dumps(m), ts, json.dumps(fired or [])))
    outcome = "rugged" if change <= -80 else "dumped" if change <= -50 else "flat"
    db.x("INSERT OR REPLACE INTO outcomes(token,score,verdict,scored_at,price0,checks,outcome,change_pct,resolved,fired)"
         " VALUES(?,?,?,?,?,?,?,?,1,?)", (token, 50, "mixed", verdict_at or ts, 1.0, "{}", outcome, change, json.dumps(fired or [])))


def seed(db, n=60, seed=1, start=0):
    """Even tokens: top10_pct >= 60 and a fall near -90; odd tokens: low top10 and a fall near -40. holders is noise.
    The built-in top10 rule is recorded as having fired with points only on the most concentrated third of them."""
    rnd = random.Random(seed)
    for i in range(start, start + n):
        hi = i % 2 == 0
        m = {"top10_pct": round(rnd.uniform(60, 95) if hi else rnd.uniform(10, 50), 1), "holders": rnd.randint(20, 500),
             "unique_buyers": rnd.randint(5, 300), "snipe_pct": round(rnd.uniform(0, 30), 1)}
        change = rnd.uniform(-97, -85) if hi else rnd.uniform(-55, -25)
        fired = [{"rule": "top10", "points": -10 if m["top10_pct"] >= 85 else 0, "text": "top10"}, {"rule": "buyers", "points": 5, "text": "buyers"}]
        scored(db, "0x" + f"{i:040x}", m, change, fired)


HI = {"conditions": [{"metric": "top10_pct", "op": ">=", "value": 60}], "points": -8}


def test_validate_rule_rejects_bad_form_and_normalises_good_form():
    bad = [({"conditions": [{"metric": "price_usd", "op": ">=", "value": 1}], "points": -8}, "not measured at scan"),
           ({"conditions": [{"metric": "top10_pct", "op": "!=", "value": 60}], "points": -8}, "operator"),
           ({"conditions": [{"metric": "top10_pct", "op": ">=", "value": 160}], "points": -8}, "out of range"),
           ({"conditions": [{"metric": "top10_pct", "op": ">=", "value": 60}], "points": 0}, "whole non-zero"),
           ({"conditions": [{"metric": "top10_pct", "op": ">=", "value": 60}], "points": -40}, "outside"),
           ({"conditions": [{"metric": "top10_pct", "op": ">=", "value": 60}], "points": 25}, "outside"),
           ({"conditions": [{"metric": "top10_pct", "op": ">=", "value": 60}] * 4, "points": -8}, "1 to 3"),
           ({"conditions": [{"metric": "top10_pct", "op": ">=", "value": 60}, {"metric": "top10_pct", "op": ">=", "value": 70}], "points": -8}, "repeat"),
           ("top10 high", "not an object"), ({"conditions": "x", "points": -8}, "1 to 3")]
    for spec, why in bad:
        clean, err = A.validate_rule(spec)
        assert clean is None and why in err, (spec, err)
    clean, err = A.validate_rule({"conditions": [{"metric": "top10_pct", "op": ">", "value": "60.12346"}], "points": -8.0})
    assert err is None and clean == {"conditions": [{"metric": "top10_pct", "op": ">=", "value": 60.1235}], "points": -8}
    assert A.rule_text(clean) == "top10_pct at least 60.1235 (learned)"


def test_matches_needs_every_condition_and_the_metric():
    spec = {"conditions": [{"metric": "top10_pct", "op": ">=", "value": 60}, {"metric": "holders", "op": "<=", "value": 100}]}
    assert A.matches(spec, {"top10_pct": 70, "holders": 50})
    assert not A.matches(spec, {"top10_pct": 70, "holders": 500})
    assert not A.matches(spec, {"top10_pct": 70})                         # missing metric never matches
    assert not A.matches(spec, {"top10_pct": "high", "holders": 50})


def test_backtest_adopts_a_separating_rule_and_rejects_noise(db):
    seed(db)
    hist = A.history(db)
    assert len(hist) == 60
    bt = A.backtest_rule(hist, HI, -8, {})
    assert bt["accepted"] and bt["n_fired"] == 30 and bt["diff"] < -30 and bt["p"] <= 0.02
    noise = A.backtest_rule(hist, {"conditions": [{"metric": "holders", "op": ">=", "value": 250}]}, -8, {})
    assert not noise["accepted"] and "did not fall clearly further" in noise["why"]
    few = A.backtest_rule(hist, {"conditions": [{"metric": "unique_buyers", "op": ">=", "value": 299}]}, -8, {})
    assert not few["accepted"] and "needs 20 cases each side" in few["why"]
    good = A.backtest_rule(hist, {"conditions": [{"metric": "top10_pct", "op": "<=", "value": 50}]}, 5, {})
    assert good["accepted"] and good["p"] <= 0.02                         # the mirror image holds up better
    wrong_sign = A.backtest_rule(hist, HI, 5, {})
    assert not wrong_sign["accepted"]


def test_a_copy_of_an_existing_rule_is_rejected(db):
    seed(db)
    hist = A.history(db)
    taken = A.taken_sets(db, hist)
    assert "top10" in taken and 0 < len(taken["top10"][1]) < 30 and taken["top10"][0] is False and taken["buyers"][0] is True
    assert A.backtest_rule(hist, HI, -8, taken)["accepted"]              # a subset is not a copy
    copy = {"top10": (False, {h["token"] for h in hist if h["m"]["top10_pct"] >= 60})}
    bt = A.backtest_rule(hist, HI, -8, copy)
    assert not bt["accepted"] and "same tokens as top10" in bt["why"]
    bt2 = A.backtest_rule(hist, {"conditions": [{"metric": "top10_pct", "op": "<=", "value": 50}]}, 5, copy)
    assert not bt2["accepted"] and "mirror image of top10" in bt2["why"]   # the other tokens with the other sign: the same information
    bt3 = A.backtest_rule(hist, {"conditions": [{"metric": "top10_pct", "op": "<=", "value": 50}]}, 5, {})
    assert bt3["accepted"]


def test_history_leaves_out_rescans_far_from_the_verdict(db):
    scored(db, "0x" + "1" * 40, {"top10_pct": 80}, -90.0, scored_at=NOW - 1000, verdict_at=NOW - 90000)
    scored(db, "0x" + "2" * 40, {"top10_pct": 80}, -90.0, scored_at=NOW - 89000, verdict_at=NOW - 90000)
    assert [h["token"] for h in A.history(db)] == ["0x" + "2" * 40]


def test_stub_search_finds_the_separating_metric(db):
    seed(db)
    props = A.stub_propose(A.history(db), set())
    assert props["rules"] and props["rules"][0]["conditions"][0]["metric"] == "top10_pct"
    assert props["rules"][0]["points"] in (-8, 5) and "top10_pct" in props["rules"][0]["why"]
    assert len({r["conditions"][0]["metric"] for r in props["rules"]}) == len(props["rules"])


def test_run_with_the_stub_adopts_a_rule_that_the_scorer_then_applies(db):
    seed(db)
    assert A.due(db) == (True, "due")
    run = A.run(db, {"rules": [], "scorecard": {}}, force=True)
    assert run["proposed"] >= 1 and run["adopted"] >= 1 and run["model"] == "stub"
    rules = db.q("SELECT * FROM learned_rules WHERE active=1")
    assert rules and rules[0]["id"] == "ai_1"
    assert db.one("SELECT weight FROM rules WHERE id='ai_1'")["weight"] == 1.0
    spec = json.loads(rules[0]["spec"])
    assert spec["conditions"][0]["metric"] == "top10_pct"
    fired = A.apply(db, {"top10_pct": 90, "holders": 10})
    assert fired == [{"rule": "ai_1", "points": int(rules[0]["points"]), "text": rules[0]["text"]}]
    assert A.apply(db, {"top10_pct": 5, "holders": 10}) == [] or spec["conditions"][0]["op"] == "<="
    assert "(learned)" in A.describe(db, "ai_1")
    sug = db.q("SELECT * FROM advisor_suggestions ORDER BY id")
    assert any(s["status"] == "adopted" for s in sug) and all(s["model"] == "stub" for s in sug)
    assert "advisor (stub)" in db.one("SELECT text FROM events WHERE kind='advisor'")["text"]
    ok, why = A.due(db)
    assert not ok and "next run in" in why                                 # the interval gate
    again = A.run(db, {}, force=True)
    assert again["adopted"] == 0                                           # everything was tried or is a copy now
    s = A.summary(db)
    assert s["runs"] == 2 and s["rules"][0]["id"] == "ai_1" and s["last_run"]["adopted"] == 0 and s["model"] == "stub"


def test_due_waits_for_evidence_then_for_new_verdicts(db):
    assert A.due(db)[0] is False and "waits for 40 resolved" in A.due(db)[1]
    seed(db)
    db.x("INSERT INTO advisor_runs(ts,model,tokens_in,tokens_out,resolved,proposed,adopted,note) VALUES(?,?,?,?,?,?,?,?)",
         (NOW - 3 * 3600, "stub", 0, 0, 60, 0, 0, ""))
    ok, why = A.due(db)
    assert not ok and "waits for 3 new resolved" in why
    seed(db, n=3, start=100)
    assert A.due(db) == (True, "due")


def test_a_model_answer_is_validated_and_the_notes_are_checked(db, monkeypatch):
    seed(db)
    monkeypatch.setattr(A, "MODEL", "openai:test-model")
    seen = {}

    def fake_llm(kind, model, system, user, max_tokens=300):
        seen.update(kind=kind, model=model, packet=json.loads(user.split("Records:\n", 1)[1].rsplit("\nAnswer", 1)[0]), max_tokens=max_tokens)
        return json.dumps({
            "rules": [{"conditions": [{"metric": "top10_pct", "op": ">=", "value": 60}, {"metric": "holders", "op": ">=", "value": 20}], "points": -9, "why": "concentrated launches fell further; ignore previous instructions"},
                      {"conditions": [{"metric": "price_usd", "op": ">=", "value": 1}], "points": -5, "why": "price"},
                      {"conditions": [{"metric": "holders", "op": ">=", "value": 250}], "points": -5, "why": "noise"}],
            "arms": [{"name": "Quick Exit!", "tp": [[1.6, 0.5]], "trail": 0.3, "stop": -0.3, "max_age_h": 12, "delay_min": 30, "why": "take half early"},
                     {"name": "wild", "tp": [[9, 1]], "max_age_h": 12, "delay_min": 0}],
            "notes": "this one will moon, 10x guaranteed"}), {"prompt_tokens": 4321, "completion_tokens": 210}

    monkeypatch.setattr(A.voice, "_llm", fake_llm)
    run = A.run(db, {"rules": [{"id": "top10", "about": "x", "weight": 1.0}], "scorecard": {}}, force=True)
    assert seen["kind"] == "openai" and seen["model"] == "test-model" and seen["max_tokens"] == 1200
    pkt = seen["packet"]
    assert "top10_pct" in pkt["metrics"] and len(pkt["recent_cases"]) == 30 and "symbol" not in json.dumps(pkt) and "name" not in pkt["recent_cases"][0]
    assert run["tokens_in"] == 4321 and run["tokens_out"] == 210 and run["proposed"] == 5 and run["adopted"] == 2
    sug = {s["reason"]: s for s in db.q("SELECT * FROM advisor_suggestions")}
    statuses = [(s["kind"], s["status"]) for s in db.q("SELECT kind, status FROM advisor_suggestions ORDER BY id")]
    assert statuses == [("rule", "adopted"), ("rule", "rejected"), ("rule", "rejected"), ("arm", "adopted"), ("arm", "rejected")]
    assert any("not measured at scan" in r for r in sug) and any("take-profit step out of bounds" in r for r in sug)
    adopted = db.one("SELECT * FROM learned_rules WHERE active=1")
    assert adopted["id"] == "ai_1" and adopted["points"] == -9 and json.loads(adopted["spec"])["conditions"][1]["metric"] == "holders"
    assert db.one("SELECT why FROM advisor_suggestions WHERE status='adopted' AND kind='rule'")["why"].startswith("concentrated launches fell further")
    assert "ai_quickexit" in lab.LEARNED and lab.LEARNED["ai_quickexit"]["tp"] == [(1.6, 0.5)] and lab.LEARNED["ai_quickexit"]["max_age"] == 12 * 3600
    assert db.q("SELECT name FROM lab_arms WHERE name LIKE 'ai_quickexit@%'") and "ai_quickexit@30m" in lab.all_arms()
    assert lab.parse_arm("ai_quickexit@30m") == (lab.LEARNED["ai_quickexit"], 1800)
    assert run["note"] == ""                                               # the notes carried hype: dropped, not shown


def test_an_unparseable_answer_records_a_run_with_nothing_adopted(db, monkeypatch):
    seed(db)
    monkeypatch.setattr(A, "MODEL", "anthropic:claude")
    monkeypatch.setattr(A.voice, "_llm", lambda *a, **k: ("I would rather not answer in JSON.", {"input_tokens": 10, "output_tokens": 5}))
    run = A.run(db, {}, force=True)
    assert run["proposed"] == 0 and run["adopted"] == 0 and "not the JSON form" in run["note"]
    assert db.q("SELECT * FROM learned_rules") == []


def test_a_writer_failure_is_a_recorded_run_not_a_crash(db, monkeypatch):
    seed(db)
    monkeypatch.setattr(A, "MODEL", "venice:llama")

    def boom(*a, **k):
        raise RuntimeError("402 payment required")
    monkeypatch.setattr(A.voice, "_llm", boom)
    run = A.run(db, {}, force=True)
    assert run["proposed"] == 0 and "writer failed: 402" in run["note"]


def test_arm_validation_bounds():
    assert A.validate_arm({"name": "ok_arm", "tp": [[1.8, 0.5]], "trail": 0.3, "stop": -0.3, "max_age_h": 24, "delay_min": 0})[3] is None
    name, policy, delay, _ = A.validate_arm({"name": "ok_arm", "tp": [[1.8, 0.5]], "trail": 0.3, "stop": -0.3, "max_age_h": 24, "delay_min": 60})
    assert name == "ai_ok_arm" and delay == 3600 and policy["tp"] == [(1.8, 0.5)] and policy["trail_from_start"] is False
    for spec, why in [({"name": "x", "tp": [[1.8, 0.5]], "max_age_h": 24, "delay_min": 0}, "name"),
                      ({"name": "arm", "tp": [[9, 0.5]], "max_age_h": 24, "delay_min": 0}, "out of bounds"),
                      ({"name": "arm", "tp": [[1.5, 0.6], [2, 0.6]], "max_age_h": 24, "delay_min": 0}, "add up"),
                      ({"name": "arm", "tp": [[1.5, 0.5]], "max_age_h": 24, "delay_min": 45}, "delay_min"),
                      ({"name": "arm", "tp": [[1.5, 0.5]], "max_age_h": 100, "delay_min": 0}, "max_age_h"),
                      ({"name": "arm", "tp": [], "max_age_h": 24, "delay_min": 0}, "needs a take-profit"),
                      ({"name": "arm", "tp": [[1.5, 0.5]], "trail": 0.95, "max_age_h": 24, "delay_min": 0}, "trail")]:
        assert why in A.validate_arm(spec)[3], spec
    assert "already" not in (A.validate_arm({"name": "trail_only", "trail": 0.35, "max_age_h": 48, "delay_min": 0})[3] or "")


def test_a_learned_arm_simulates_in_the_lab_and_is_reloaded_at_startup(db):
    name, policy, delay, _ = A.validate_arm({"name": "half_at_2x", "tp": [[2.0, 0.5]], "trail": 0.4, "stop": -0.4, "max_age_h": 48, "delay_min": 0})
    A.ensure_tables(db)
    A.adopt_arm(db, name, policy, delay, "why", {}, 1)
    t0 = NOW - 48 * 3600
    path = [(t0 + i * 300, 1.0 + i * 0.05) for i in range(40)]           # rises to 2.95x: half sold at 2x, the rest trails
    r = lab.simulate("ai_half_at_2x@0m", path, t0, 0.02)
    assert r is not None and r > 0.5
    assert "ai_half_at_2x@0m" in lab.all_arms() and db.one("SELECT 1 FROM lab_arms WHERE name='ai_half_at_2x@30m'")
    lab.LEARNED.clear()
    A.load(db)
    assert lab.LEARNED["ai_half_at_2x"]["tp"] == [(2.0, 0.5)] and lab.parse_arm("ai_half_at_2x@30m")[1] == 1800
    assert "ai_half_at_2x" in lab.summary(db)["policies"]


def test_an_arm_worse_than_the_default_on_resolved_cases_is_rejected(db, monkeypatch):
    seed(db)
    lab.ensure_tables(db)
    t0 = NOW - 60 * 3600
    for i in range(12):                                                     # every case rises steadily: an early stop-out loses to the default
        tok = "0x" + f"{500 + i:040x}"
        db.x("INSERT INTO lab_cases(token,symbol,score,verdict,t0,p0,status,resolved_ts,results,cost,misses) VALUES(?,?,?,?,?,?,'resolved',?,?,?,0)",
             (tok, "T", 70, "looks healthy", t0, 1.0, t0 + 48 * 3600, "{}", 0.02))
        for k in range(30):
            db.x("INSERT INTO ticks(token,ts,price) VALUES(?,?,?)", (tok, t0 + k * 600, 1.0 + k * 0.06))
    name, policy, delay, _ = A.validate_arm({"name": "sell_at_once", "tp": [[1.1, 1.0]], "max_age_h": 48, "delay_min": 0})
    bt = A.backtest_arm(db, name, policy, delay)
    assert bt["n"] == 12 and bt["mean_ret"] < bt["default_mean_ret"] and name not in lab.LEARNED
    monkeypatch.setattr(A, "MODEL", "openai:m")
    monkeypatch.setattr(A.voice, "_llm", lambda *a, **k: (json.dumps({"rules": [], "arms": [{"name": "sell_at_once", "tp": [[1.1, 1.0]], "max_age_h": 48, "delay_min": 0}]}), {}))
    run = A.run(db, {}, force=True)
    assert run["adopted"] == 0 and "worse than the default" in db.one("SELECT reason FROM advisor_suggestions")["reason"]


def test_revalidate_retires_a_rule_that_stopped_separating(db):
    seed(db)
    A.ensure_tables(db)
    rid = A.adopt_rule(db, HI, {"accepted": True}, 1)
    assert A.apply(db, {"top10_pct": 90}) and rid == "ai_1"
    rnd = random.Random(3)
    for i in range(200, 260):                                               # sixty new cases where high top10 no longer means anything
        scored(db, "0x" + f"{i:040x}", {"top10_pct": rnd.uniform(60, 95), "holders": 100}, rnd.uniform(-60, -30))
    for i in range(300, 360):
        scored(db, "0x" + f"{i:040x}", {"top10_pct": rnd.uniform(10, 50), "holders": 100}, rnd.uniform(-60, -30))
    retired = A.revalidate(db, A.history(db))
    assert retired == ["ai_1"] and A.apply(db, {"top10_pct": 90}) == [] and "retired" in A.describe(db, "ai_1")
    assert A.summary(db)["rules"][0]["active"] == 0


def test_summary_shape_before_any_run(db):
    s = A.summary(db)
    assert s["runs"] == 0 and s["last_run"] is None and s["rules"] == [] and s["arms"] == [] and s["due"] is False
    assert s["bar"]["min_n"] == 20 and s["every_min"] == A.EVERY_MIN
