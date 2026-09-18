"""The exit rule and the simulator (pure functions against hand-computed numbers), the lab loop on a
temp db with a stubbed price feed, and the ranking's lower bound against zero-edge paths."""
import json
import math
import random

from wormhole import lab

F = lab.FEE
QS = 1.0 / (1 + F)       # entry fee buys fewer tokens
NOW = 1_800_000_000
TOKEN_A = "0x" + "aa" * 20
TOKEN_B = "0x" + "bb" * 20


def path(prices, step=300, t0=0):
    return [(t0 + i * step, p) for i, p in enumerate(prices)]


def net(cash_legs, entry=1.0, fee=F):
    """cash from a list of (fraction, price) legs relative to the entry, net of both fees, minus the
    1.0 risked: scale-free, like the simulator"""
    qs = 1.0 / (1 + fee)
    return sum(frac * qs * (price / entry) * (1 - fee) for frac, price in cash_legs) - 1.0


class Clock:
    """Stands in for lab.time so a test can walk the marker through hours in no time."""

    def __init__(self, t):
        self.t = t

    def time(self):
        return self.t


def arm_stats(db, name, n, mean, sd):
    """Write lab_arms sums that reproduce a mean and a population stdev."""
    db.x("UPDATE lab_arms SET n=?, sum_ret=?, sum_sq=?, wins=? WHERE name=?",
         (n, n * mean, n * (sd * sd + mean * mean), n if mean > 0 else 0, name))


def case(db, token, t0, ticks, cost=None, sym="AAA"):
    lab.ensure_tables(db)
    db.x("INSERT INTO lab_cases(token,symbol,score,verdict,t0,p0,status,cost,misses) VALUES(?,?,80,'looks healthy',?,?,'active',?,0)",
         (token, sym, t0, ticks[0][1], cost))
    db.many("INSERT INTO ticks(token,ts,price) VALUES(?,?,?)", [(token, ts, p) for ts, p in ticks])


# ---- the exit rule and the simulator ----------------------------------------------------------

def test_parse_arm():
    pol, delay = lab.parse_arm("costout_1.5x@30m")
    assert pol is lab.POLICIES["costout_1.5x"] and delay == 1800
    assert lab.parse_arm("hedge_2x@0m")[1] == 0


def test_costout_takes_two_thirds_then_trails():
    # 1.0 -> 1.6 (take profit at 1.5x, sell 2/3) -> 2.0 (new peak) -> 1.1 (40% under the peak: trailing stop)
    r = lab.simulate("costout_1.5x@0m", path([1.0, 1.6, 2.0, 1.1, 1.0]), 0)
    assert abs(r - net([(0.667, 1.6), (0.333, 1.1)])) < 1e-9


def test_simulate_is_scale_free():
    shape = [1.0, 1.6, 2.0, 1.1, 1.0]
    want = net([(0.667, 1.6), (0.333, 1.1)])
    for e in (1e-5, 1.0, 100.0):
        r = lab.simulate("costout_1.5x@0m", path([e * x for x in shape]), 0)
        assert abs(r - want) < 1e-9, e


def test_every_arm_is_scale_free():
    rng = random.Random(3)
    shape, p = [], 1.0
    for _ in range(60):
        shape.append(p)
        p *= math.exp(0.2 * rng.gauss(0, 1))
    for arm in lab.ARMS:
        rs = [lab.simulate(arm, path([e * x for x in shape], step=1200), 0) for e in (1e-5, 1.0, 100.0)]
        assert rs[0] is not None
        assert max(rs) - min(rs) < 1e-9, arm


def test_costout_on_real_price_levels():
    r = lab.simulate("costout_1.5x@0m", path([1e-5, 1.6e-5, 2e-5, 1.1e-5]), 0)
    want = 0.667 * QS * 1.6 * (1 - F) + 0.333 * QS * 1.1 * (1 - F) - 1
    assert abs(r - want) < 1e-9 and 0.3 < r < 0.5


def test_per_side_cost_is_a_parameter():
    p = path([1.0, 1.6, 2.0, 1.1, 1.0])
    r = lab.simulate("costout_1.5x@0m", p, 0, fee=0.04)
    assert abs(r - net([(0.667, 1.6), (0.333, 1.1)], fee=0.04)) < 1e-9
    assert r < lab.simulate("costout_1.5x@0m", p, 0)


def test_stop_before_take_profit_sells_everything():
    r = lab.simulate("costout_1.5x@0m", path([1.0, 0.9, 0.6, 0.2]), 0)
    assert abs(r - net([(1.0, 0.6)])) < 1e-9


def test_after_take_profit_the_stop_no_longer_applies():
    # once cost is out the rest rides on the trailing stop only: 1.6 -> 0.5 is below the old stop but
    # exits on the trail (peak 1.6 * 0.6 = 0.96) at the 0.5 tick, not earlier
    r = lab.simulate("costout_1.5x@0m", path([1.0, 1.6, 0.5]), 0)
    assert abs(r - net([(0.667, 1.6), (0.333, 0.5)])) < 1e-9


def test_trailing_from_start_tracks_the_peak():
    r = lab.simulate("trail_35@0m", path([1.0, 2.0, 1.31, 1.29, 3.0]), 0)
    # 2.0 * 0.65 = 1.30: the 1.31 tick holds, the 1.29 tick exits; the later 3.0 never counts
    assert abs(r - net([(1.0, 1.29)])) < 1e-9


def test_ladder_sells_three_quarters_on_a_spike_then_trails():
    r = lab.simulate("ladder@0m", path([1.0, 3.2, 1.5]), 0)
    # 1.5x, 2x and 3x all hit on the same tick: three quarters at 3.2; the last quarter exits on the
    # 50% trail (3.2 * 0.5 = 1.6) at the 1.5 tick
    assert abs(r - net([(0.75, 3.2), (0.25, 1.5)])) < 1e-9


def test_time_limit_sells_at_the_deadline():
    prices = [1.0] * 80 + [1.2] * 5     # 300 s ticks: index 72 is 6 h after entry
    r = lab.simulate("time_6h@0m", path(prices), 0)
    assert abs(r - net([(1.0, 1.0)])) < 1e-9


def test_entry_delay_enters_on_the_first_tick_at_or_after_the_delay():
    p = path([1.0, 1.1, 1.2, 1.3, 1.35, 1.4, 1.45, 1.5, 1.4, 1.3], step=600)   # tick 3 is t=1800
    r = lab.simulate("fixed_40_25@30m", p, 0)
    # entry at 1.3; +40% would need 1.82: never, so it rides to the last tick at 1.3
    assert abs(r - net([(1.0, 1.3)], entry=1.3)) < 1e-9


def test_delay_arm_needs_a_tick_near_the_delay():
    gap = [(0, 1.0), (300, 1.0), (7500, 1.2), (7800, 1.2), (8100, 1.2)]       # 2 h without a price
    assert lab.simulate("costout_1.5x@30m", gap, 0) is None
    assert lab.simulate("costout_1.5x@60m", gap, 0) is None
    assert lab.simulate("costout_1.5x@0m", gap, 0) is not None
    near = [(0, 1.0), (300, 1.0), (2400, 1.0), (2700, 1.0), (3000, 1.0)]        # 10 min late is fine
    assert lab.simulate("costout_1.5x@30m", near, 0) is not None


def test_zero_prices():
    assert lab.simulate("costout_1.5x@0m", [(0, 0.0), (300, 1.0), (600, 1.0)], 0) is None   # no entry at 0
    assert lab.simulate("costout_1.5x@0m", path([1.0, 1.1, 0.0, 0.0]), 0) == -1.0           # drained: everything lost
    assert lab.simulate("trail_35@0m", path([1.0, 1.1, 0.0]), 0) == -1.0


def test_costs_alone_make_a_flat_path_slightly_negative():
    r = lab.simulate("fixed_100_40@0m", path([1.0, 1.0, 1.0]), 0)
    assert abs(r - (QS * (1 - F) - 1.0)) < 1e-12 and r < 0


def test_too_short_a_path_is_no_case():
    assert lab.simulate("hedge_2x@0m", path([1.0]), 0) is None
    assert lab.simulate("hedge_2x@60m", path([1.0, 1.1], step=300), 0) is None


LOCK = {"tp": [], "trail": 0.15, "trail_from_start": False, "stop": -0.30, "max_age": lab.HORIZON_S,
        "arm_at": 1.2, "trail_tiers": [(2.0, 0.20), (4.0, 0.25)]}


def lock(prices, **changes):
    return lab.simulate("lock", path(prices), 0, policy={**LOCK, **changes}, delay=0)


def test_profit_lock_stops_the_loss_before_it_arms():
    # never reached 1.2x: the trailing stop stays off (1.15 -> 0.9 is 22% under the peak and holds); -30% sells
    assert abs(lock([1.0, 1.15, 0.9, 0.69, 2.0]) - net([(1.0, 0.69)])) < 1e-9


def test_profit_lock_arms_in_profit_and_sells_nothing_until_the_trail():
    # 1.25 arms it; 1.25 * 0.85 = 1.0625: the 1.07 tick holds, the 1.06 tick sells everything
    assert abs(lock([1.0, 1.25, 1.07, 1.06, 3.0]) - net([(1.0, 1.06)])) < 1e-9


def test_profit_lock_widens_the_trail_after_a_big_pump():
    # peak 4.0 trails 25% (3.0); a 20% trail would have sold the 3.1 tick
    assert abs(lock([1.0, 2.0, 4.0, 3.1, 2.9]) - net([(1.0, 2.9)])) < 1e-9
    # peak 2.0 trails 20% (1.6): 1.65 holds, 1.55 sells
    assert abs(lock([1.0, 2.0, 1.65, 1.55]) - net([(1.0, 1.55)])) < 1e-9


def test_profit_lock_floor_never_gives_back_the_entry():
    # armed at 1.2 with a 15% trail the stop would sit at 1.02; a 1.08 floor sells the 1.07 tick instead
    assert abs(lock([1.0, 1.2, 1.07, 0.5], floor=1.08) - net([(1.0, 1.07)])) < 1e-9
    # the floor does not apply before the position arms
    assert abs(lock([1.0, 1.1, 1.05, 0.69], floor=1.08) - net([(1.0, 0.69)])) < 1e-9
    # and the trail takes over once it is above the floor
    assert abs(lock([1.0, 2.0, 1.59], floor=1.08) - net([(1.0, 1.59)])) < 1e-9


def test_profit_lock_state_survives_a_restart():
    # the book stores peak and trail_on between cycles: a position that armed earlier sells on the trail
    st = {"entry": 1.0, "entry_ts": 0, "qty_left": 1.0, "tp_done": [], "peak": 1.3, "trail_on": True}
    frac, why = lab.exit_step(LOCK, st, 1.10, 600)
    assert frac == 1.0 and why == "trailing stop 15% below the peak"
    st = {"entry": 1.0, "entry_ts": 0, "qty_left": 1.0, "tp_done": [], "peak": 1.1, "trail_on": False}
    assert lab.exit_step(LOCK, st, 0.95, 600) == (0.0, None)


def test_old_policies_do_not_need_the_new_keys():
    for name, pol in lab.POLICIES.items():
        assert lab.trail_pct(pol, 5.0) == pol.get("trail") or pol.get("trail_tiers"), name


def test_exit_step_never_sells_more_than_is_left():
    pol = lab.POLICIES["ladder"]
    st = {"entry": 1.0, "entry_ts": 0, "qty_left": 0.2, "tp_done": [], "peak": 1.0, "trail_on": False}
    frac, why = lab.exit_step(pol, st, 1.6, 300)
    assert frac == 0.2 and why.startswith("take profit")


# ---- costs per token ----------------------------------------------------------------------------

def test_cost_uses_creator_tax(db):
    assert abs(lab.side_cost(200, 100) - (0.03 + lab.SLIPPAGE)) < 1e-12
    assert abs(lab.side_cost(0, 100) - 0.02) < 1e-12
    assert lab.side_cost(None, None) is None
    db.x("INSERT INTO launches(token,symbol,creator_tax_bps,curve_fee_bps) VALUES(?,?,?,?)", (TOKEN_A, "AAA", 200, 100))
    db.x("INSERT INTO launches(token,symbol) VALUES(?,?)", (TOKEN_B, "BBB"))
    assert abs(lab.token_cost(db, TOKEN_A) - 0.04) < 1e-12
    assert lab.token_cost(db, TOKEN_B) == lab.FEE
    assert lab.token_cost(db, "0x" + "cc" * 20) == lab.FEE


def test_enroll_stores_cost_and_starts_at_the_price_time(db):
    db.x("INSERT INTO launches(token,symbol,creator_tax_bps,curve_fee_bps) VALUES(?,?,?,?)", (TOKEN_A, "AAA", 200, 100))
    db.x("INSERT INTO launches(token,symbol) VALUES(?,?)", (TOKEN_B, "BBB"))
    import time
    now = int(time.time())
    for t in (TOKEN_A, TOKEN_B):
        db.x("INSERT INTO outcomes(token,score,verdict,scored_at,price0) VALUES(?,?,?,?,?)", (t, 80, "looks healthy", now - 900, 1e-5))
    db.x("INSERT INTO scores(token,score,verdict,scored_at,metrics) VALUES(?,?,?,?,?)",
         (TOKEN_A, 80, "looks healthy", now - 900, json.dumps({"price0_ts": now - 300})))
    db.x("UPDATE outcomes SET baseline_ts=? WHERE token=?", (now - 300, TOKEN_A))
    assert lab.enroll(db) == 2
    a = db.one("SELECT * FROM lab_cases WHERE token=?", (TOKEN_A,))
    b = db.one("SELECT * FROM lab_cases WHERE token=?", (TOKEN_B,))
    assert abs(a["cost"] - 0.04) < 1e-12 and a["t0"] == now - 300 and a["status"] == "active"
    assert b["cost"] == lab.FEE and b["t0"] == now - 900
    assert db.one("SELECT ts, price FROM ticks WHERE token=?", (TOKEN_A,)) == {"ts": now - 300, "price": 1e-5}
    assert lab.enroll(db) == 0


def test_resolve_uses_the_case_cost(db, monkeypatch):
    clock = Clock(NOW)
    monkeypatch.setattr(lab, "time", clock)
    monkeypatch.setattr(lab, "token_prices", lambda addrs: {})
    t0 = NOW - lab.HORIZON_S
    ticks = [(t0 + i * 1800, p) for i, p in enumerate([1.0, 1.6, 2.0, 1.1] + [1.0] * 93)]      # 97 ticks: up to the horizon
    case(db, TOKEN_A, t0, ticks, cost=0.04)
    lab.tick(db)
    c = db.one("SELECT * FROM lab_cases WHERE token=?", (TOKEN_A,))
    assert c["status"] == "resolved"
    res = json.loads(c["results"])
    assert res["costout_1.5x@0m"] == round(lab.simulate("costout_1.5x@0m", ticks, t0, fee=0.04), 4)
    assert res["costout_1.5x@0m"] != round(lab.simulate("costout_1.5x@0m", ticks, t0), 4)
    assert db.one("SELECT n FROM lab_arms WHERE name='costout_1.5x@0m'")["n"] == 1


# ---- the loop: stale paths, missing prices, pruning -----------------------------------------------

def test_silent_path_is_stale_not_a_fill(db, monkeypatch):
    monkeypatch.setattr(lab, "time", Clock(NOW))
    monkeypatch.setattr(lab, "token_prices", lambda addrs: {})            # the feed fails outright
    t0 = NOW - lab.HORIZON_S
    case(db, TOKEN_A, t0, [(t0, 1.0), (t0 + 300, 1.1), (t0 + 600, 1.2)])   # 3 ticks, then 47 h of nothing
    lab.tick(db)
    c = db.one("SELECT * FROM lab_cases WHERE token=?", (TOKEN_A,))
    assert c["status"] == "stale" and c["resolved_ts"] == NOW and json.loads(c["results"]) == {}
    assert db.one("SELECT SUM(n) n FROM lab_arms")["n"] == 0
    s = lab.summary(db)
    assert s["cases_stale"] == 1 and s["cases_resolved"] == 0 and s["cases_active"] == 0
    assert db.one("SELECT 1 FROM ticks WHERE token=? AND price=0", (TOKEN_A,)) is None


def test_missing_price_never_becomes_a_synthetic_zero(db, monkeypatch):
    clock = Clock(NOW)
    monkeypatch.setattr(lab, 'time', clock)
    monkeypatch.setattr(lab, 'token_prices', lambda addrs: {TOKEN_B: {'price_usd': 2.0}})
    t0 = NOW - lab.HORIZON_S + 600
    case(db, TOKEN_A, t0, [(t0, 1.0), (t0+300, 1.1), (t0+600, 1.2)])
    case(db, TOKEN_B, NOW-3600, [(NOW-3600, 2.0)], sym='BBB')
    for _ in range(3):
        lab.tick(db)
        clock.t += 300
    assert not db.one('SELECT 1 FROM ticks WHERE token=? AND price=0', (TOKEN_A,))
    assert not db.one("SELECT 1 FROM events WHERE text LIKE '%booked at 0%'")
    assert db.one('SELECT status FROM lab_cases WHERE token=?', (TOKEN_A,))['status'] == 'stale'


def test_one_missed_price_is_forgiven(db, monkeypatch):
    clock = Clock(NOW)
    monkeypatch.setattr(lab, "time", clock)
    feed = {TOKEN_B: {"price_usd": 2.0}}
    monkeypatch.setattr(lab, "token_prices", lambda addrs: {a: feed.get(a, {}) for a in addrs})
    case(db, TOKEN_A, NOW - 7200, [(NOW - 7200, 1.0), (NOW - 3600, 1.0)])
    case(db, TOKEN_B, NOW - 3600, [(NOW - 3600, 2.0)], sym="BBB")
    lab.tick(db)
    feed[TOKEN_A] = {"price_usd": 1.05}
    clock.t += 300
    lab.tick(db)
    assert db.one("SELECT misses FROM lab_cases WHERE token=?", (TOKEN_A,))["misses"] == 0
    assert db.one("SELECT COUNT(*) n FROM ticks WHERE token=? AND price=0", (TOKEN_A,))["n"] == 0
    del feed[TOKEN_B]                                                      # the whole batch fails: no strike
    del feed[TOKEN_A]
    clock.t += 300
    lab.tick(db)
    assert db.one("SELECT misses FROM lab_cases WHERE token=?", (TOKEN_A,))["misses"] == 0


def test_ticks_of_finished_cases_are_pruned(db, monkeypatch):
    monkeypatch.setattr(lab, "time", Clock(NOW))
    monkeypatch.setattr(lab, "token_prices", lambda addrs: {})
    old = NOW - 8 * 86400
    case(db, TOKEN_A, old, [(old, 1.0), (old + 300, 1.0)])
    db.x("UPDATE lab_cases SET status='resolved' WHERE token=?", (TOKEN_A,))
    case(db, TOKEN_B, NOW - 3600, [(old, 1.0), (NOW - 3600, 1.0)], sym="BBB")     # active: untouched
    lab.tick(db)
    assert db.one("SELECT COUNT(*) n FROM ticks WHERE token=?", (TOKEN_A,))["n"] == 0
    assert db.one("SELECT COUNT(*) n FROM ticks WHERE token=?", (TOKEN_B,))["n"] == 2


# ---- ranking and the policy ------------------------------------------------------------------

def test_ranking_uses_lower_bound(db, monkeypatch):
    monkeypatch.setattr(lab, "LAB_MIN_N", 10)
    lab.ensure_tables(db)
    arm_stats(db, "hedge_2x@0m", 10, 0.3, 1.0)       # big mean, huge spread
    arm_stats(db, "trail_35@0m", 10, 0.1, 0.1)       # small mean, tight
    rk = lab.ranking(db)
    assert rk[0]["arm"] == "trail_35@0m" and rk[1]["arm"] == "hedge_2x@0m"
    a = next(x for x in rk if x["arm"] == "hedge_2x@0m")
    b = rk[0]
    assert a["lcb"] < 0 < b["lcb"]
    assert abs(a["lcb"] - (0.3 - lab.LCB_Z * 1.0 / math.sqrt(10))) < 1e-3
    assert abs(b["lcb"] - (0.1 - lab.LCB_Z * 0.1 / math.sqrt(10))) < 1e-3
    assert abs(a["stdev"] - 1.0) < 1e-3 and abs(a["mean_ret"] - 0.3) < 1e-9
    assert lab.research_policy(db) == ("trail_35@0m", "learned")


def test_lower_bound_needs_two_cases(db):
    lab.ensure_tables(db)
    arm_stats(db, "hedge_2x@0m", 1, 0.5, 0.0)
    a = next(x for x in lab.ranking(db) if x["arm"] == "hedge_2x@0m")
    assert a["n"] == 1 and a["mean_ret"] == 0.5 and a["lcb"] is None and a["stdev"] is None


def test_ranking_prefers_arms_with_enough_cases(db):
    lab.ensure_tables(db)
    arm_stats(db, "hedge_2x@0m", 3, 0.8, 0.1)         # +80% avg, too few
    arm_stats(db, "trail_35@30m", 40, 0.1, 0.05)      # +10% avg
    arm_stats(db, "time_6h@0m", 45, -0.1, 0.05)       # -10% avg
    top = lab.ranking(db)[0]
    assert top["arm"] == "trail_35@30m" and top["n"] == 40 and abs(top["mean_ret"] - 0.1) < 1e-9
    assert lab.research_policy(db) == ("trail_35@30m", "learned")


def test_current_policy_beats_default(db):
    lab.ensure_tables(db)
    arm_stats(db, lab.DEFAULT, lab.LAB_MIN_N, 0.2, 0.05)
    arm_stats(db, "trail_35@0m", lab.LAB_MIN_N, 0.1, 0.05)       # positive bound, but below the default
    arm, why = lab.research_policy(db)
    assert arm == lab.DEFAULT and "default" in why and "confirmed" in why
    arm_stats(db, "hedge_2x@0m", lab.LAB_MIN_N, 0.5, 3.0)        # above the default, but its bound is below zero
    assert lab.research_policy(db)[0] == lab.DEFAULT
    arm_stats(db, "ladder@0m", lab.LAB_MIN_N, 0.3, 0.05)         # above the default with a positive bound
    assert lab.research_policy(db) == ("ladder@0m", "learned")
    arm_stats(db, "ladder@0m", lab.LAB_MIN_N - 1, 0.3, 0.05)     # one case short
    assert lab.research_policy(db)[0] == lab.DEFAULT


def test_default_policy_until_enough_cases(db):
    lab.ensure_tables(db)
    arm, why = lab.research_policy(db)
    assert arm == lab.DEFAULT and "default" in why
    assert lab.LAB_MIN_N == 30


def test_pick_arm_follows_the_policy_unless_exploring(db, monkeypatch):
    lab.ensure_tables(db)
    monkeypatch.setattr(random, "random", lambda: 0.0)       # would explore every time if it were on
    assert lab.EXPLORE == 0.0
    assert lab.pick_arm(db) == lab.DEFAULT
    monkeypatch.setattr(lab, "EXPLORE", 0.10)
    assert lab.pick_arm(db) != lab.DEFAULT
    monkeypatch.setattr(random, "random", lambda: 0.5)
    assert lab.pick_arm(db) == lab.DEFAULT


def test_summary_keys(db):
    s = lab.summary(db)
    assert s["in_use"] == lab.DEFAULT and s["min_n"] == lab.LAB_MIN_N and s["cost_per_side"] == lab.FEE
    assert s["cases_active"] == 0 and s["cases_resolved"] == 0 and s["cases_stale"] == 0 and s["explore"] == 0.0
    assert len(s["arms"]) == len(lab.ARMS) and all("lcb" in a and "mean_ret" in a for a in s["arms"])


def _zero_edge_paths(rng, n, ticks=97, step=1800, sigma=0.10):
    """Martingale price paths (E[next] = current): no edge for any exit rule, only costs."""
    out = []
    for _ in range(n):
        p, pth = 1.0, []
        for i in range(ticks):
            pth.append((i * step, p))
            p *= math.exp(sigma * rng.gauss(0, 1) - sigma * sigma / 2)
        out.append(pth)
    return out


def test_zero_edge_paths_do_not_prove_an_edge(db):
    """The best of 24 arms on LAB_MIN_N zero-edge paths: its raw mean is often above zero (winner's curse);
    its lower bound must not be. Measured over 200 trials: the top bound is positive in 23% of trials
    with LCB_Z=1.0, 12% with 1.5, 4% with 2.0."""
    lab.ensure_tables(db)
    rng = random.Random(11)
    trials = 40
    top_lcb_positive = learned = best_mean_positive = 0
    for _ in range(trials):
        stats = {a: [0, 0.0, 0.0, 0] for a in lab.ARMS}
        for pth in _zero_edge_paths(rng, lab.LAB_MIN_N):
            for a in lab.ARMS:
                r = lab.simulate(a, pth, 0)
                assert r is not None
                s = stats[a]
                s[0] += 1
                s[1] += r
                s[2] += r * r
                s[3] += r > 0
        db.many("UPDATE lab_arms SET n=?, sum_ret=?, sum_sq=?, wins=? WHERE name=?", [(*s, a) for a, s in stats.items()])
        rk = lab.ranking(db)
        top_lcb_positive += rk[0]["lcb"] > 0
        learned += lab.research_policy(db)[1] == "learned"
        best_mean_positive += max(a["mean_ret"] for a in rk) > 0
    assert top_lcb_positive / trials < 0.20
    assert learned / trials < 0.20
    assert best_mean_positive > top_lcb_positive        # the bound is what removes the winner's curse


def test_research_winner_cannot_promote_without_prospective_evidence(db):
    lab.ensure_tables(db)
    arm_stats(db, "trail_35@0m", 300, 0.9, 0.01)
    assert lab.research_policy(db)[0] == "trail_35@0m"
    assert lab.current_policy(db)[0] == lab.DEFAULT
