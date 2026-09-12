import time

from wormhole import budget as B


def test_projection_without_samples_assumes_no_income(db):
    p = B.projection(db, 100.0)
    assert p["income_per_day_usd"] == 0 and p["income_measured"] is False
    cost = B.COMPUTE_USD_DAY + B.GAS_USD_DAY + B.BRIDGE_USD_MONTH / 30
    assert abs(p["cost_per_day_usd"] - round(cost, 3)) < 1e-6
    assert abs(p["reserve_needed_usd"] - round(cost * B.RESERVE_DAYS, 2)) < 1e-6
    assert p["can_invest"] == (100.0 - cost * B.RESERVE_DAYS > 0)
    assert p["scenarios"]["no income"]["runs_out_day"] == 1 + int(100.0 // cost) or p["scenarios"]["no income"]["runs_out_day"] is None


def test_zero_treasury_runs_out_on_day_one(db):
    p = B.projection(db, 0.0)
    assert p["scenarios"]["no income"]["runs_out_day"] == 1
    assert p["can_invest"] is False and p["surplus_usd"] < 0


def test_income_is_measured_from_samples(db):
    now = int(time.time())
    db.x("INSERT INTO samples(ts, usd) VALUES(?,?)", (now - 2 * 86400, 100.0))
    db.x("INSERT INTO samples(ts, usd) VALUES(?,?)", (now, 104.0))
    assert abs(B.net_flow_per_day(db) - 2.0) < 1e-6
    p = B.projection(db, 104.0)
    assert p["income_measured"] is True and abs(p["income_per_day_usd"] - 2.0) < 1e-6
    assert p["compute_budget_per_day_usd"] <= B.COMPUTE_USD_DAY
    assert p["compute_budget_per_day_usd"] <= max(0.10, 0.5 * 2.0) + 1e-9


def test_sample_is_hourly(db):
    B.sample(db, 1.0)
    B.sample(db, 2.0)
    assert db.one("SELECT COUNT(*) n FROM samples")["n"] == 1
