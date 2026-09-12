"""The exit rule and the simulator: pure functions, checked against hand-computed numbers."""
import random

from wormhole import lab

F = lab.FEE
QS = 1.0 / (1 + F)       # entry fee buys fewer tokens


def path(prices, step=300, t0=0):
    return [(t0 + i * step, p) for i, p in enumerate(prices)]


def net(cash_legs):
    """cash from a list of (fraction, price) legs, net of both fees, minus the 1.0 risked"""
    return sum(frac * QS * price * (1 - F) for frac, price in cash_legs) - 1.0


def test_parse_arm():
    pol, delay = lab.parse_arm("costout_1.5x@30m")
    assert pol is lab.POLICIES["costout_1.5x"] and delay == 1800
    assert lab.parse_arm("hedge_2x@0m")[1] == 0


def test_costout_takes_two_thirds_then_trails():
    # 1.0 -> 1.6 (take profit at 1.5x, sell 2/3) -> 2.0 (new peak) -> 1.1 (40% under the peak: trailing stop)
    r = lab.simulate("costout_1.5x@0m", path([1.0, 1.6, 2.0, 1.1, 1.0]), 0)
    assert abs(r - net([(0.667, 1.6), (0.333, 1.1)])) < 1e-9


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
    assert abs(r - net([(1.0, 1.3)])) < 1e-9


def test_costs_alone_make_a_flat_path_slightly_negative():
    r = lab.simulate("fixed_100_40@0m", path([1.0, 1.0, 1.0]), 0)
    assert abs(r - (QS * (1 - F) - 1.0)) < 1e-12 and r < 0


def test_too_short_a_path_is_no_case():
    assert lab.simulate("hedge_2x@0m", path([1.0]), 0) is None
    assert lab.simulate("hedge_2x@60m", path([1.0, 1.1], step=300), 0) is None


def test_exit_step_never_sells_more_than_is_left():
    pol = lab.POLICIES["ladder"]
    st = {"entry": 1.0, "entry_ts": 0, "qty_left": 0.2, "tp_done": [], "peak": 1.0, "trail_on": False}
    frac, why = lab.exit_step(pol, st, 1.6, 300)
    assert frac == 0.2 and why.startswith("take profit")


def test_ranking_prefers_arms_with_enough_cases(db):
    lab.ensure_tables(db)
    db.x("UPDATE lab_arms SET n=3, sum_ret=2.4, sum_sq=2.0, wins=3 WHERE name='hedge_2x@0m'")       # +80% avg, too few
    db.x("UPDATE lab_arms SET n=12, sum_ret=1.2, sum_sq=0.5, wins=7 WHERE name='trail_35@30m'")     # +10% avg
    db.x("UPDATE lab_arms SET n=15, sum_ret=-1.5, sum_sq=0.9, wins=4 WHERE name='time_6h@0m'")      # -10% avg
    top = lab.ranking(db)[0]
    assert top["arm"] == "trail_35@30m" and top["n"] == 12 and abs(top["mean_ret"] - 0.1) < 1e-9
    assert lab.current_policy(db) == ("trail_35@30m", "learned")


def test_default_policy_until_enough_cases(db):
    lab.ensure_tables(db)
    arm, why = lab.current_policy(db)
    assert arm == lab.DEFAULT and "default" in why


def test_pick_arm_explores_one_time_in_ten(db, monkeypatch):
    lab.ensure_tables(db)
    monkeypatch.setattr(random, "random", lambda: 0.5)
    assert lab.pick_arm(db) == lab.DEFAULT
    monkeypatch.setattr(random, "random", lambda: 0.05)
    assert lab.pick_arm(db) != lab.DEFAULT
