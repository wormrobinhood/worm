"""The strategy lab: every candidate token is traded on paper by many policies at once.

A case starts when a verdict scores at least LAB_MIN_SCORE and has a price. Prices are sampled every
mark cycle for 48 hours, then every arm (exit policy x entry delay) is simulated on the same path with
real costs (fee, tax, slippage). Arms keep a running net return per dollar risked. The best arm with
enough cases becomes the policy the paper book and the trader use; one position in ten runs a
different arm so the lab keeps learning."""
import json
import logging
import os
import random
import time

from .prices import token_prices

log = logging.getLogger("wormhole.lab")
LAB_MIN_SCORE = int(os.environ.get("WH_LAB_MIN_SCORE", "60"))
LAB_MIN_N = int(os.environ.get("WH_LAB_MIN_N", "10"))
HORIZON_S = 48 * 3600
FEE = float(os.environ.get("WH_TRADE_COST", "0.03")) / 2      # per side: fee + creator tax + slippage
EXPLORE = 0.10
DEFAULT = "costout_1.5x@0m"

# tp: list of (multiple, fraction of the initial tokens to sell); trail: drawdown from peak that sells the
# rest; trail_from_start: trailing active before any take-profit; stop: loss that sells everything before
# the first take-profit; max_age: seconds.
POLICIES = {
    "hedge_2x":     {"tp": [(2.0, 0.60)], "trail": 0.50, "trail_from_start": False, "stop": -0.50, "max_age": HORIZON_S},
    "costout_1.5x": {"tp": [(1.5, 0.667)], "trail": 0.40, "trail_from_start": False, "stop": -0.35, "max_age": HORIZON_S},
    "ladder":       {"tp": [(1.5, 0.25), (2.0, 0.25), (3.0, 0.25)], "trail": 0.50, "trail_from_start": False, "stop": -0.40, "max_age": HORIZON_S},
    "fixed_100_40": {"tp": [(2.0, 1.0)], "trail": None, "trail_from_start": False, "stop": -0.40, "max_age": HORIZON_S},
    "fixed_40_25":  {"tp": [(1.4, 1.0)], "trail": None, "trail_from_start": False, "stop": -0.25, "max_age": HORIZON_S},
    "trail_35":     {"tp": [], "trail": 0.35, "trail_from_start": True, "stop": None, "max_age": HORIZON_S},
    "time_6h":      {"tp": [], "trail": None, "trail_from_start": False, "stop": -0.40, "max_age": 6 * 3600},
    "time_24h":     {"tp": [], "trail": None, "trail_from_start": False, "stop": -0.40, "max_age": 24 * 3600},
}
DELAYS = (0, 30, 60)
ARMS = [f"{p}@{d}m" for p in POLICIES for d in DELAYS]


def ensure_tables(db):
    db.x("CREATE TABLE IF NOT EXISTS lab_cases(token TEXT PRIMARY KEY, symbol TEXT, score INTEGER, verdict TEXT, t0 INTEGER,"
         " p0 REAL, status TEXT, resolved_ts INTEGER, results TEXT)")
    db.x("CREATE TABLE IF NOT EXISTS ticks(token TEXT, ts INTEGER, price REAL, PRIMARY KEY(token, ts))")
    db.x("CREATE TABLE IF NOT EXISTS lab_arms(name TEXT PRIMARY KEY, n INTEGER DEFAULT 0, sum_ret REAL DEFAULT 0,"
         " sum_sq REAL DEFAULT 0, wins INTEGER DEFAULT 0, updated INTEGER)")
    db.many("INSERT OR IGNORE INTO lab_arms(name) VALUES(?)", [(a,) for a in ARMS])


def parse_arm(name):
    p, _, d = name.partition("@")
    return POLICIES[p], int(d.rstrip("m")) * 60


# ---- the exit rule shared by simulation, the paper book and the trader ---------------------------

def exit_step(policy, st, price, ts):
    """Advance one position by one price. st: {entry, entry_ts, qty_left, tp_done, peak, trail_on}.
    Returns (fraction_of_initial_to_sell, reason) or (0, None)."""
    st["peak"] = max(st.get("peak") or 0, price)
    mult = price / st["entry"]
    for m, frac in policy["tp"]:
        if m not in st["tp_done"] and mult >= m and st["qty_left"] > 0:
            st["tp_done"].append(m)
            st["trail_on"] = True
            return min(frac, st["qty_left"]), f"take profit at {m:g}x"
    if not st["tp_done"] and policy["stop"] is not None and mult <= 1 + policy["stop"] and st["qty_left"] > 0:
        return st["qty_left"], f"stop at {int(policy['stop'] * 100)}%"
    trail_on = st.get("trail_on") or policy["trail_from_start"]
    if trail_on and policy["trail"] and st["qty_left"] > 0 and price <= st["peak"] * (1 - policy["trail"]):
        return st["qty_left"], f"trailing stop {int(policy['trail'] * 100)}% below the peak"
    if policy["max_age"] and ts - st["entry_ts"] >= policy["max_age"] and st["qty_left"] > 0:
        return st["qty_left"], "time limit"
    return 0.0, None


def simulate(arm, path, t0):
    policy, delay = parse_arm(arm)
    entry_i = next((i for i, (ts, _) in enumerate(path) if ts >= t0 + delay), None)
    if entry_i is None or entry_i >= len(path) - 1:
        return None
    ets, e = path[entry_i]
    st = {"entry": e, "entry_ts": ets, "qty_left": 1.0, "tp_done": [], "peak": e, "trail_on": False}
    cash = 0.0
    qty_scale = 1.0 / (1 + FEE)          # the entry fee buys fewer tokens
    for ts, p in path[entry_i + 1:]:
        frac, why = exit_step(policy, st, p, ts)
        while frac > 0:
            cash += frac * qty_scale * p * (1 - FEE)
            st["qty_left"] -= frac
            frac, why = exit_step(policy, st, p, ts) if st["qty_left"] > 0 else (0, None)
        if st["qty_left"] <= 1e-9:
            break
    if st["qty_left"] > 1e-9:
        cash += st["qty_left"] * qty_scale * path[-1][1] * (1 - FEE)
    return cash - 1.0                    # net return per 1 unit risked


# ---- the loop ---------------------------------------------------------------------------------

def enroll(db):
    """New cases from verdicts that have a price and a score worth trading."""
    ensure_tables(db)
    rows = db.q("SELECT o.token, o.score, o.verdict, o.scored_at, o.price0, l.symbol FROM outcomes o JOIN launches l ON l.token=o.token"
                " WHERE o.score>=? AND o.price0 IS NOT NULL AND o.scored_at>=? AND o.token NOT IN (SELECT token FROM lab_cases)",
                (LAB_MIN_SCORE, int(time.time()) - 6 * 3600))
    for r in rows:
        db.x("INSERT OR IGNORE INTO lab_cases(token,symbol,score,verdict,t0,p0,status) VALUES(?,?,?,?,?,?,?)",
             (r["token"], r["symbol"], r["score"], r["verdict"], r["scored_at"], r["price0"], "active"))
        db.x("INSERT OR IGNORE INTO ticks(token,ts,price) VALUES(?,?,?)", (r["token"], r["scored_at"], r["price0"]))
    return len(rows)


def tick(db):
    """Sample prices for active cases; resolve the ones past the horizon."""
    ensure_tables(db)
    enroll(db)
    now = int(time.time())
    active = db.q("SELECT * FROM lab_cases WHERE status='active'")
    if not active:
        return
    px = token_prices([c["token"] for c in active])
    for c in active:
        p = (px.get(c["token"]) or {}).get("price_usd")
        if p:
            db.x("INSERT OR IGNORE INTO ticks(token,ts,price) VALUES(?,?,?)", (c["token"], now, p))
        if now - c["t0"] >= HORIZON_S:
            _resolve(db, c)


def _resolve(db, c):
    path = [(r["ts"], r["price"]) for r in db.q("SELECT ts, price FROM ticks WHERE token=? ORDER BY ts", (c["token"],))]
    results = {}
    if len(path) >= 3:
        for arm in ARMS:
            r = simulate(arm, path, c["t0"])
            if r is None:
                continue
            results[arm] = round(r, 4)
            db.x("UPDATE lab_arms SET n=n+1, sum_ret=sum_ret+?, sum_sq=sum_sq+?, wins=wins+?, updated=? WHERE name=?",
                 (r, r * r, 1 if r > 0 else 0, int(time.time()), arm))
    db.x("UPDATE lab_cases SET status='resolved', resolved_ts=?, results=? WHERE token=?",
         (int(time.time()), json.dumps(results), c["token"]))
    if results:
        best = max(results, key=results.get)
        db.add_event("lab", f"lab case ${c['symbol']} resolved: best arm {best} {results[best] * 100:+.0f}%, hedge_2x@0m {results.get('hedge_2x@0m', 0) * 100:+.0f}%", c["token"])
    db.x("DELETE FROM ticks WHERE token=? AND ts<?", (c["token"], int(time.time()) - 7 * 86400))


def ranking(db):
    ensure_tables(db)
    rows = db.q("SELECT * FROM lab_arms")
    out = []
    for r in rows:
        n = r["n"] or 0
        mean = (r["sum_ret"] / n) if n else None
        var = (r["sum_sq"] / n - mean * mean) if n and mean is not None else None
        out.append({"arm": r["name"], "n": n, "mean_ret": round(mean, 4) if mean is not None else None,
                    "win_rate": round(100 * r["wins"] / n) if n else None,
                    "stdev": round(var ** 0.5, 4) if var is not None and var > 0 else None})
    out.sort(key=lambda a: (-(a["mean_ret"] if a["mean_ret"] is not None and a["n"] >= LAB_MIN_N else -9), -(a["n"] or 0)))
    return out


def current_policy(db):
    """The arm in use: the best with enough cases, else the default."""
    for a in ranking(db):
        if a["n"] >= LAB_MIN_N and a["mean_ret"] is not None:
            return a["arm"], "learned"
    return DEFAULT, "default until the lab has enough cases"


def pick_arm(db):
    """Arm for a new position: the current best, or an explorer one time in ten."""
    best, _ = current_policy(db)
    if random.random() < EXPLORE:
        return random.choice([a for a in ARMS if a != best])
    return best


def summary(db):
    ensure_tables(db)
    cur, why = current_policy(db)
    return {"in_use": cur, "why": why, "arms": ranking(db), "min_n": LAB_MIN_N, "cost_per_side": FEE,
            "cases_active": db.one("SELECT COUNT(*) n FROM lab_cases WHERE status='active'")["n"],
            "cases_resolved": db.one("SELECT COUNT(*) n FROM lab_cases WHERE status='resolved'")["n"],
            "policies": {k: v for k, v in POLICIES.items()}, "delays_min": list(DELAYS)}
