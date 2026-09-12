"""Financial self-sufficiency: measure what comes in, estimate what goes out, project 90 days.

Phase 1 spends nothing, so the costs below are the planned phase-3 bills (narrator compute on
Surplus, gas on Robinhood Chain, bridge fees to Base). Income is what the worm actually claimed:
the ledger's 'claim' rows (USDG creator fees) over the last week, per day. Treasury balance samples
are kept for the chart only; a balance moves with deposits, ETH's price and the worm's own buys, none
of which is income. The policy is simple and written down: keep a 90-day reserve; compute, gas and
bridging come only from the operations share of every claim (the rest is forwarded to the creator or
burned); no trading by policy until the brain is mature; give only from the surplus above the reserve."""
import os
import time

from . import config as C
from . import treasury as T

COMPUTE_USD_DAY = float(os.environ.get("WH_COMPUTE_USD_DAY", "0.75"))
GAS_USD_DAY = float(os.environ.get("WH_GAS_USD_DAY", "0.10"))
BRIDGE_USD_MONTH = float(os.environ.get("WH_BRIDGE_USD_MONTH", "0.20"))
RESERVE_DAYS = 90
HORIZON = 90
INCOME_WINDOW_DAYS = 7


def sample(db, usd, demo=False):
    """One treasury balance an hour, for the chart. A pretend (demo) treasury is not sampled."""
    if demo:
        return
    last = db.one("SELECT ts FROM samples ORDER BY ts DESC LIMIT 1")
    if last and time.time() - last["ts"] < 3600:
        return
    db.x("INSERT INTO samples(ts, usd) VALUES(?,?)", (int(time.time()), usd))


def net_flow_per_day(db):
    """Change of the sampled treasury balance per day over the last week: chart only, not income."""
    rows = db.q("SELECT ts, usd FROM samples WHERE ts>=? ORDER BY ts", (int(time.time()) - 7 * 86400,))
    if len(rows) < 2 or rows[-1]["ts"] - rows[0]["ts"] < 3600:
        return None
    return (rows[-1]["usd"] - rows[0]["usd"]) / ((rows[-1]["ts"] - rows[0]["ts"]) / 86400)


def income_per_day(db, days=INCOME_WINDOW_DAYS):
    """Claimed creator fees per day: the ledger's 'claim' rows in the window divided by the days since
    the first of them. None until the first claim in the window is a day old (one claim says nothing
    about a rate)."""
    T.ensure_tables(db)
    now = time.time()
    r = db.one("SELECT COALESCE(SUM(amount),0) s, MIN(ts) t FROM ledger WHERE kind='claim' AND ts>=?", (int(now) - days * 86400,))
    if not r or not r["t"] or now - r["t"] < 86400:
        return None
    return r["s"] / ((now - r["t"]) / 86400)


def _run(balance, income, cost, days=HORIZON):
    out_day = None
    for d in range(1, days + 1):
        balance += income - cost
        if balance < 0 and out_day is None:
            out_day = d
    return {"end_balance_usd": round(balance, 2), "runs_out_day": out_day}


def projection(db, treasury_usd):
    """treasury_usd is the worm's own money (the wallet minus what is owed to the creator and to the burn).
    Income is the operations share of the claims measured in the ledger: the rest leaves the wallet."""
    cost_day = COMPUTE_USD_DAY + GAS_USD_DAY + BRIDGE_USD_MONTH / 30
    measured = income_per_day(db)
    claims = max(0.0, measured) if measured is not None else 0.0
    income = claims * C.OPS_SHARE
    flow = net_flow_per_day(db)
    reserve = cost_day * RESERVE_DAYS
    surplus = treasury_usd - reserve
    compute_budget = COMPUTE_USD_DAY if claims == 0 else min(COMPUTE_USD_DAY, max(0.10, income))
    ops_pct = int(round(C.OPS_SHARE * 100))
    return {
        "treasury_usd": round(treasury_usd, 2),
        "cost_per_day_usd": round(cost_day, 3),
        "cost_parts": {"compute": COMPUTE_USD_DAY, "gas": GAS_USD_DAY, "bridge": round(BRIDGE_USD_MONTH / 30, 3)},
        "income_per_day_usd": round(income, 3),
        "claims_per_day_usd": round(claims, 3),
        "ops_share": C.OPS_SHARE,
        "income_measured": measured is not None,
        "income_window_days": INCOME_WINDOW_DAYS,
        "balance_change_per_day_usd": round(flow, 3) if flow is not None else None,   # chart only
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
        "rule": f"keep a 90-day reserve; compute, gas and bridging come only from the operations share ({ops_pct}% of every claim); "
                "no trading by policy until the brain is mature; give only from the surplus above the reserve",
    }
