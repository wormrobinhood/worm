"""Financial self-sufficiency: measure what comes in, estimate what goes out, project 90 days.

Phase 1 spends nothing, so the costs below are the planned phase-3 bills (narrator compute on
Surplus, gas on Robinhood Chain, bridge fees to Base). Income is measured from hourly treasury
samples once a wallet exists. The policy is simple and written down: keep a 90-day reserve,
spend on compute at most half of what it earns, invest only from the surplus above the reserve."""
import os
import time

COMPUTE_USD_DAY = float(os.environ.get("WH_COMPUTE_USD_DAY", "0.75"))
GAS_USD_DAY = float(os.environ.get("WH_GAS_USD_DAY", "0.10"))
BRIDGE_USD_MONTH = float(os.environ.get("WH_BRIDGE_USD_MONTH", "0.20"))
RESERVE_DAYS = 90
HORIZON = 90


def sample(db, usd):
    last = db.one("SELECT ts FROM samples ORDER BY ts DESC LIMIT 1")
    if last and time.time() - last["ts"] < 3600:
        return
    db.x("INSERT INTO samples(ts, usd) VALUES(?,?)", (int(time.time()), usd))


def net_flow_per_day(db):
    rows = db.q("SELECT ts, usd FROM samples WHERE ts>=? ORDER BY ts", (int(time.time()) - 7 * 86400,))
    if len(rows) < 2 or rows[-1]["ts"] - rows[0]["ts"] < 3600:
        return None
    return (rows[-1]["usd"] - rows[0]["usd"]) / ((rows[-1]["ts"] - rows[0]["ts"]) / 86400)


def _run(balance, income, cost, days=HORIZON):
    out_day = None
    for d in range(1, days + 1):
        balance += income - cost
        if balance < 0 and out_day is None:
            out_day = d
    return {"end_balance_usd": round(balance, 2), "runs_out_day": out_day}


def projection(db, treasury_usd):
    cost_day = COMPUTE_USD_DAY + GAS_USD_DAY + BRIDGE_USD_MONTH / 30
    measured = net_flow_per_day(db)
    income = max(0.0, measured) if measured is not None else 0.0
    reserve = cost_day * RESERVE_DAYS
    surplus = treasury_usd - reserve
    compute_budget = COMPUTE_USD_DAY if income == 0 else min(COMPUTE_USD_DAY, max(0.10, 0.5 * income))
    return {
        "treasury_usd": round(treasury_usd, 2),
        "cost_per_day_usd": round(cost_day, 3),
        "cost_parts": {"compute": COMPUTE_USD_DAY, "gas": GAS_USD_DAY, "bridge": round(BRIDGE_USD_MONTH / 30, 3)},
        "income_per_day_usd": round(income, 3),
        "income_measured": measured is not None,
        "runway_days_no_income": round(treasury_usd / cost_day, 1) if cost_day else None,
        "scenarios": {
            "no income": _run(treasury_usd, 0.0, cost_day),
            "current income": _run(treasury_usd, income, cost_day),
            "half income": _run(treasury_usd, income / 2, cost_day),
        },
        "reserve_days": RESERVE_DAYS,
        "reserve_needed_usd": round(reserve, 2),
        "surplus_usd": round(surplus, 2),
        "can_invest": surplus > 0,
        "compute_budget_per_day_usd": round(compute_budget, 3),
        "rule": "keep a 90-day reserve; spend on compute at most half of what it earns; "
                "invest only from the surplus above the reserve",
    }
