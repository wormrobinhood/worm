"""Paper cohorts: what counts as a member, that a cohort is judged once on realised paper results, that a
failure raises the next bar, that a pass is renewed or expires, and that editing the strategy voids it."""
import json

import pytest

from wormhole import lab, strategy_validation as V, watch
from test_trader import tok

RULE = {"name": "rule-a", "looks": [30], "conditions": [{"feature": "ret_p0", "op": ">=", "value": -0.2}]}
OTHER = {"name": "rule-b", "looks": [60], "conditions": [{"feature": "ret_p0", "op": ">=", "value": 0.0}]}


@pytest.fixture(autouse=True)
def one_rule(monkeypatch):
    monkeypatch.setattr(watch, "STRATEGIES", [RULE])


def exit_spec():
    return json.dumps(lab.parse_arm(lab.DEFAULT)[0])


def position(db, i, ret=None, rule="rule-a", creator=None, spec=None, opened=None):
    """A paper row the rule opened; closed with `ret` per dollar when given."""
    from wormhole.paper import Paper
    Paper(db)
    token = tok(i)
    db.x("INSERT OR IGNORE INTO launches(token,deployer,symbol) VALUES(?,?,?)", (token, creator or tok(i + 5000), "T"))
    db.x("INSERT INTO paper(token,symbol,opened_ts,entry_usd,size_usd,qty,status,pnl_usd,policy,policy_spec,strategy,cost,gas_usd)"
         " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (token, "T", opened or 1000 + i, 1.0, 10.0, 10.0, "open" if ret is None else "closed",
          None if ret is None else ret * 10.0, lab.DEFAULT, spec or exit_spec(), rule, .02, .01))
    return token


def cohort(db, rets, start=0, **kw):
    for i, r in enumerate(rets):
        position(db, start + i, r, **kw)
    V.tick(db)


def test_a_cohort_starts_per_rule_and_ignores_the_old_sampled_design(db):
    V.ensure_tables(db)
    db.x("INSERT INTO strategy_trials(created,cutoff,arm,spec,baseline,status) VALUES(1,0,'costout_1.5x@0m','{}','{}','collecting')")
    V.tick(db)
    rows = db.q("SELECT rule,status,k FROM strategy_trials ORDER BY id")
    assert rows == [{"rule": None, "status": "superseded", "k": None}, {"rule": "rule-a", "status": "collecting", "k": 1}]
    s = V.summary(db)
    assert not s["passed"] and s["status"] == "collecting" and s["required"] == V.COHORT_N and s["arm"] == lab.DEFAULT


def test_members_are_this_rules_positions_one_per_creator_with_the_frozen_exit(db):
    V.tick(db)
    position(db, 1)
    position(db, 2, creator=tok(5001))                       # the same creator again: never a second member
    position(db, 3, rule="rule-b")                           # another rule's position
    position(db, 4, spec=json.dumps({"tp": [], "stop": -0.9}))   # opened under a different exit policy
    V.tick(db)
    assert [m["token"] for m in db.q("SELECT token FROM strategy_members")] == [tok(1)]


def test_positions_opened_before_the_trial_never_count(db):
    position(db, 1, .5)
    V.tick(db)
    assert db.one("SELECT COUNT(*) n FROM strategy_members")["n"] == 0


def test_no_verdict_before_every_member_has_closed(db):
    V.tick(db)
    cohort(db, [.3] * (V.COHORT_N - 1))
    open_one = position(db, 900)                             # the fiftieth member is still open
    cohort(db, [.9] * 5, start=1000)                         # later winners cannot take its place
    assert V.summary(db)["status"] == "collecting" and V.summary(db)["enrolled"] == V.COHORT_N
    db.x("UPDATE paper SET status='closed', pnl_usd=-3.0 WHERE token=?", (open_one,))
    V.tick(db)
    s = V.summary(db)
    assert s["passed"] and s["passed_rules"] == ["rule-a"] and s["rules"][0]["last"]["n"] == V.COHORT_N
    assert s["mean_ret"] == pytest.approx((.3 * 49 - .3) / 50) and 0 < s["lcb"] < s["mean_ret"]
    V.tick(db)
    s = V.summary(db)
    assert s["n"] == 5 and s["enrolled"] == 5                # the surplus rolls into the next cohort, in opening order


def test_a_lucky_mean_with_a_wide_spread_does_not_pass(db):
    V.tick(db)
    cohort(db, [9.0] + [-.1] * (V.COHORT_N - 1))             # one ten-bagger carries forty-nine small losses
    s = V.summary(db)
    assert s["rules"][0]["last"]["status"] == "failed" and not s["passed"] and s["mean_ret"] > 0 > s["lcb"]
    assert s["status"] == "collecting"                       # and the next cohort has begun


def test_a_member_without_a_result_fails_the_cohort_instead_of_being_replaced(db):
    V.tick(db)
    cohort(db, [.4] * (V.COHORT_N - 1))
    broken = position(db, 900, .4)
    db.x("UPDATE paper SET pnl_usd=NULL WHERE token=?", (broken,))
    V.tick(db)
    assert V.summary(db)["rules"][0]["last"]["status"] == "failed"


def test_a_failure_raises_the_next_bar_and_a_pass_does_not(db):
    V.tick(db)
    cohort(db, [-.2] * V.COHORT_N)
    trials = db.q("SELECT status,k FROM strategy_trials WHERE rule='rule-a' ORDER BY id")
    assert trials == [{"status": "failed", "k": 1}, {"status": "collecting", "k": 2}]
    cohort(db, [.3 + .001 * i for i in range(V.COHORT_N)], start=2000)
    first_pass = json.loads(db.one("SELECT result FROM strategy_trials WHERE status='passed'")["result"])
    assert first_pass["attempt"] == 2 and first_pass["z"] > 2.0
    assert db.q("SELECT status,k FROM strategy_trials WHERE rule='rule-a' ORDER BY id")[-1] == {"status": "collecting", "k": 2}   # a renewal keeps its bar
    cohort(db, [-.2] * V.COHORT_N, start=4000)                       # the renewal fails: the retry is a new attempt
    assert db.q("SELECT status,k FROM strategy_trials WHERE rule='rule-a' ORDER BY id")[-1] == {"status": "collecting", "k": 3}


def test_a_pass_expires_unless_a_new_cohort_renews_it(db, monkeypatch):
    V.tick(db)
    cohort(db, [.3 + .001 * i for i in range(V.COHORT_N)])
    done = db.one("SELECT completed FROM strategy_trials WHERE status='passed'")["completed"]
    nxt = V.summary(db)["rules"][0]["collecting"]
    assert V.summary(db)["passed"] and (nxt["n"], nxt["enrolled"], nxt["attempt"]) == (0, 0, 1)
    monkeypatch.setattr(V.time, "time", lambda: done + V.VALID_FOR + 10)
    assert not V.summary(db)["passed"]
    cohort(db, [.3 + .001 * i for i in range(V.COHORT_N)], start=3000)
    assert V.summary(db)["passed"]


def test_editing_the_rule_or_the_exit_policy_voids_a_pass(db, monkeypatch):
    V.tick(db)
    cohort(db, [.3 + .001 * i for i in range(V.COHORT_N)])
    assert V.summary(db)["passed"]
    monkeypatch.setattr(watch, "STRATEGIES", [{**RULE, "looks": [45]}])
    assert not V.summary(db)["passed"]
    V.tick(db)
    assert [r["status"] for r in db.q("SELECT status FROM strategy_trials WHERE rule='rule-a' ORDER BY id")] == ["voided", "voided", "collecting"]
    monkeypatch.setattr(watch, "STRATEGIES", [RULE])
    monkeypatch.setitem(lab.POLICIES, lab.DEFAULT.split("@")[0], {**lab.parse_arm(lab.DEFAULT)[0], "stop": -.99})
    assert not V.summary(db)["passed"]


def test_rules_are_judged_separately_and_share_the_error_budget(db, monkeypatch):
    monkeypatch.setattr(watch, "STRATEGIES", [RULE, OTHER])
    V.tick(db)
    assert [(r["rule"], r["k"]) for r in db.q("SELECT rule,k FROM strategy_trials ORDER BY id")] == [("rule-a", 1), ("rule-b", 2)]
    cohort(db, [.3 + .001 * i for i in range(V.COHORT_N)], rule="rule-b")
    s = V.summary(db)
    assert s["passed"] and s["passed_rules"] == ["rule-b"]
    assert {v["rule"]: v["passed"] for v in s["rules"]} == {"rule-a": False, "rule-b": True}
    assert s["rules"][1]["collecting"]["attempt"] == 2               # rule-b's renewal is judged where it passed, whatever rule-a does


def test_gas_cost_applies_to_every_cash_leg():
    path = [(0, 1), (300, 1.6), (600, 2), (900, .9)]
    spec, delay = lab.parse_arm("costout_1.5x@0m")
    normal = lab.simulate("costout_1.5x@0m", path, 0)
    withgas = lab.simulate("costout_1.5x@0m", path, 0, policy=spec, delay=delay, gas_per_side=.01)
    assert normal - withgas == pytest.approx(.03)  # buy, take profit, trailing exit
