"""The runway: income only from claimed fees in the ledger, balance samples only for the chart."""
import time

from wormhole import budget as B
from wormhole import treasury as T


def claim(db, ts, amount):
    T.ensure_tables(db)
    db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)", (ts, "claim", "USDG", amount, "0x", "test"))


def test_projection_without_samples_assumes_no_income(db):
    p = B.projection(db, 100.0)
    assert p["income_per_day_usd"] == 0 and p["income_measured"] is False and p["balance_change_per_day_usd"] is None
    cost = B.COMPUTE_USD_DAY + B.GAS_USD_DAY + B.BRIDGE_USD_MONTH / 30
    assert abs(p["cost_per_day_usd"] - round(cost, 3)) < 1e-6
    assert abs(p["reserve_needed_usd"] - round(cost * B.RESERVE_DAYS, 2)) < 1e-6
    assert p["can_invest"] == (100.0 - cost * B.RESERVE_DAYS > 0)
    assert p["scenarios"]["no income"]["runs_out_day"] == 1 + int(100.0 // cost) or p["scenarios"]["no income"]["runs_out_day"] is None


def test_zero_treasury_runs_out_on_day_one(db):
    p = B.projection(db, 0.0)
    assert p["scenarios"]["no income"]["runs_out_day"] == 1
    assert p["can_invest"] is False and p["surplus_usd"] < 0


def test_samples_feed_the_chart_not_the_income(db):
    now = int(time.time())
    for k, usd in ((4, 0.0), (3, 0.0), (2, 200.0), (1, 200.0)):          # a deposit, an ETH tick: not income
        db.x("INSERT INTO samples(ts, usd) VALUES(?,?)", (now - k * 3600, usd))
    assert B.net_flow_per_day(db) > 0
    p = B.projection(db, 200.0)
    assert p["income_measured"] is False and p["income_per_day_usd"] == 0
    assert p["balance_change_per_day_usd"] == round(B.net_flow_per_day(db), 3)
    assert p["scenarios"]["current income"] == p["scenarios"]["no income"]


def test_income_from_ledger_only(db):
    now = int(time.time())
    db.x("INSERT INTO samples(ts, usd) VALUES(?,?)", (now - 2 * 86400, 0.0))
    db.x("INSERT INTO samples(ts, usd) VALUES(?,?)", (now, 200.0))
    assert B.income_per_day(db) is None                                    # no claim at all
    claim(db, now - 7200, 5.0)
    assert B.income_per_day(db) is None                                    # one claim two hours old: no rate yet
    db.x("DELETE FROM ledger")
    for k in range(1, 7):
        claim(db, now - k * 86400, 1.0)                                    # $1 a day for six days
    inc = B.income_per_day(db)
    assert abs(inc - 1.0) < 1e-3
    p = B.projection(db, 200.0)
    assert p["income_measured"] is True and abs(p["income_per_day_usd"] - 1.0) < 1e-3
    assert p["compute_budget_per_day_usd"] <= B.COMPUTE_USD_DAY
    assert p["compute_budget_per_day_usd"] <= max(0.10, 0.5 * 1.0) + 1e-9
    assert p["scenarios"]["current income"]["end_balance_usd"] > p["scenarios"]["no income"]["end_balance_usd"]


def test_income_window_is_seven_days(db):
    now = int(time.time())
    claim(db, now - 10 * 86400, 1000.0)                                    # too old to count
    claim(db, now - 2 * 86400, 2.0)
    claim(db, now - 1 * 86400, 2.0)
    assert abs(B.income_per_day(db) - 2.0) < 1e-3


def test_other_ledger_kinds_are_not_income(db):
    now = int(time.time())
    T.ensure_tables(db)
    for kind in ("forward", "compute", "give", "give_demo"):
        db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)", (now - 2 * 86400, kind, "USDG", 50.0, None, "x"))
    assert B.income_per_day(db) is None
    claim(db, now - 2 * 86400, 4.0)
    assert abs(B.income_per_day(db) - 2.0) < 1e-3


def test_sample_is_hourly(db):
    B.sample(db, 1.0)
    B.sample(db, 2.0)
    assert db.one("SELECT COUNT(*) n FROM samples")["n"] == 1


def test_sample_skips_demo_treasury(db):
    B.sample(db, 500.0, demo=True)
    assert db.one("SELECT COUNT(*) n FROM samples")["n"] == 0
    B.sample(db, 1.0, demo=False)
    assert db.one("SELECT COUNT(*) n FROM samples")["n"] == 1
