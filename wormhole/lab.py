"""The strategy lab: every candidate token is traded on paper by many policies at once.

A case starts when a verdict scores at least LAB_MIN_SCORE and has a price. Prices are sampled every
mark cycle for 48 hours, then every arm (exit policy x entry delay) is simulated on the same path with
that token's own costs (creator tax + curve fee + slippage per side). Arms keep a running net return
per dollar risked. An arm becomes the policy the paper book and the trader use only when it has
LAB_MIN_N cases, its lower confidence bound (mean minus one standard error) is above zero and its mean
beats the default's on the same cases. Exploration (a random arm on a share of new positions) is off by
default: the lab already scores every arm on every case, so an explorer position teaches it nothing."""
import json
import logging
import math
import os
import random
import time

from .prices import token_prices

log = logging.getLogger("wormhole.lab")
LAB_MIN_SCORE = int(os.environ.get("WH_LAB_MIN_SCORE", "60"))
LAB_MIN_N = int(os.environ.get("WH_LAB_MIN_N", "30"))
HORIZON_S = 48 * 3600
FEE = float(os.environ.get("WH_TRADE_COST", "0.03")) / 2      # per side, used when a token's own costs are unknown
SLIPPAGE = 0.01                                                # per side, on top of the token's tax and fee
EXPLORE = float(os.environ.get("WH_LAB_EXPLORE", "0"))         # share of new positions that run a random arm
LCB_Z = float(os.environ.get("WH_LAB_LCB_Z", "1.5"))          # standard errors below the mean for the ranking bound
# 1.0 lets the best of 24 arms pass on zero-edge paths in ~23% of trials at n=30 (tests/test_lab.py measures it), 1.5 in ~12%
ENTRY_SLACK_S = 900                                            # an arm needs a tick within 15 min of its entry time
STALE_TAIL_S = 2 * 3600                                        # a path whose last tick is older than this before the horizon is no case
ZERO_AFTER_MISSES = 2                                          # cycles a priced token may vanish from the feed before it is booked at 0
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
LEARNED = {}                   # policies the advisor adopted, by name; loaded at startup (advisor.load)


def all_arms():
    return ARMS + [f"{p}@{d}m" for p in LEARNED for d in DELAYS]


def ensure_tables(db):
    db.x("CREATE TABLE IF NOT EXISTS lab_cases(token TEXT PRIMARY KEY, symbol TEXT, score INTEGER, verdict TEXT, t0 INTEGER,"
         " p0 REAL, status TEXT, resolved_ts INTEGER, results TEXT, cost REAL, misses INTEGER DEFAULT 0)")
    for col in ("cost REAL", "misses INTEGER DEFAULT 0"):
        try:
            db.x(f"ALTER TABLE lab_cases ADD COLUMN {col}")
        except Exception:
            pass
    db.x("CREATE TABLE IF NOT EXISTS ticks(token TEXT, ts INTEGER, price REAL, PRIMARY KEY(token, ts))")
    db.x("CREATE TABLE IF NOT EXISTS lab_arms(name TEXT PRIMARY KEY, n INTEGER DEFAULT 0, sum_ret REAL DEFAULT 0,"
         " sum_sq REAL DEFAULT 0, wins INTEGER DEFAULT 0, updated INTEGER)")
    db.many("INSERT OR IGNORE INTO lab_arms(name) VALUES(?)", [(a,) for a in ARMS])


def parse_arm(name):
    p, _, d = name.partition("@")
    policy = POLICIES.get(p) or LEARNED.get(p)
    if policy is None:
        raise KeyError(f"unknown arm {name}")
    return policy, int(d.rstrip("m")) * 60


# ---- costs ------------------------------------------------------------------------------------

def side_cost(creator_tax_bps, curve_fee_bps):
    """Per-side cost of trading one token: its creator tax and curve fee plus slippage. None when the
    launch's fees are unknown (the caller falls back to FEE)."""
    if creator_tax_bps is None and curve_fee_bps is None:
        return None
    return (int(creator_tax_bps or 0) + int(curve_fee_bps or 0)) / 1e4 + SLIPPAGE


def token_cost(db, token):
    """Per-side cost for a token from its launch row, FEE when unknown."""
    L = db.one("SELECT creator_tax_bps, curve_fee_bps FROM launches WHERE token=?", (token,))
    c = side_cost(L["creator_tax_bps"], L["curve_fee_bps"]) if L else None
    return c if c is not None else FEE


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


def simulate(arm, path, t0, fee=FEE):
    """Net return per 1 unit risked for `arm` on `path` [(ts, price), ...], scale-free: every cash leg is
    a fraction of the position times price/entry, so a 1e-5 token and a $100 token with the same shape
    return the same number. `fee` is the per-side cost. None when the arm has no usable entry: the first
    tick at or after its delay is missing, more than ENTRY_SLACK_S late, at a zero price, or the last."""
    policy, delay = parse_arm(arm)
    entry_i = next((i for i, (ts, _) in enumerate(path) if ts >= t0 + delay), None)
    if entry_i is None or entry_i >= len(path) - 1:
        return None
    ets, e = path[entry_i]
    if ets > t0 + delay + ENTRY_SLACK_S or not e or e <= 0:
        return None
    st = {"entry": e, "entry_ts": ets, "qty_left": 1.0, "tp_done": [], "peak": e, "trail_on": False}
    cash = 0.0
    qty_scale = 1.0 / (1 + fee)          # the entry fee buys fewer tokens
    for ts, p in path[entry_i + 1:]:
        frac, why = exit_step(policy, st, p, ts)
        while frac > 0:
            cash += frac * qty_scale * (p / e) * (1 - fee)
            st["qty_left"] -= frac
            frac, why = exit_step(policy, st, p, ts) if st["qty_left"] > 0 else (0, None)
        if st["qty_left"] <= 1e-9:
            break
    if st["qty_left"] > 1e-9:
        cash += st["qty_left"] * qty_scale * (path[-1][1] / e) * (1 - fee)
    return cash - 1.0                    # net return per 1 unit risked


# ---- the loop ---------------------------------------------------------------------------------

def enroll(db):
    """New cases from verdicts that have a price and a score worth trading. The first tick is the
    scorer's baseline price at the time it was observed (price0_ts in the score's metrics when the
    price feed stores it, else the verdict time)."""
    ensure_tables(db)
    rows = db.q("SELECT o.token, o.score, o.verdict, o.scored_at, o.price0, l.symbol, l.creator_tax_bps, l.curve_fee_bps, s.metrics"
                " FROM outcomes o JOIN launches l ON l.token=o.token LEFT JOIN scores s ON s.token=o.token"
                " WHERE o.score>=? AND o.price0 IS NOT NULL AND o.scored_at>=? AND o.token NOT IN (SELECT token FROM lab_cases)",
                (LAB_MIN_SCORE, int(time.time()) - 6 * 3600))
    for r in rows:
        try:
            m = json.loads(r["metrics"] or "{}")
        except ValueError:
            m = {}
        t0 = int(m["price0_ts"]) if m.get("price0_ts") else r["scored_at"]
        cost = side_cost(r["creator_tax_bps"], r["curve_fee_bps"])
        db.x("INSERT OR IGNORE INTO lab_cases(token,symbol,score,verdict,t0,p0,status,cost,misses) VALUES(?,?,?,?,?,?,?,?,0)",
             (r["token"], r["symbol"], r["score"], r["verdict"], t0, r["price0"], "active", cost if cost is not None else FEE))
        db.x("INSERT OR IGNORE INTO ticks(token,ts,price) VALUES(?,?,?)", (r["token"], t0, r["price0"]))
    return len(rows)


def tick(db):
    """Sample prices for active cases; resolve the ones past the horizon.

    A token that the feed priced before and now leaves out while the batch priced others is counted as a
    miss; at ZERO_AFTER_MISSES misses in a row a 0-price tick is written so the simulation books the loss
    of a drained pool instead of treating the last good price as a fill. One miss is forgiven because a
    partly failed batch must not turn a healthy case into a total loss."""
    ensure_tables(db)
    enroll(db)
    now = int(time.time())
    db.x("DELETE FROM ticks WHERE ts<? AND token IN (SELECT token FROM lab_cases WHERE status<>'active')", (now - 7 * 86400,))
    active = db.q("SELECT * FROM lab_cases WHERE status='active'")
    if not active:
        return
    px = token_prices([c["token"] for c in active])
    got_any = any((px.get(c["token"]) or {}).get("price_usd") for c in active)
    for c in active:
        p = (px.get(c["token"]) or {}).get("price_usd")
        if p:
            db.x("INSERT OR IGNORE INTO ticks(token,ts,price) VALUES(?,?,?)", (c["token"], now, p))
            if c["misses"]:
                db.x("UPDATE lab_cases SET misses=0 WHERE token=?", (c["token"],))
        elif got_any and db.one("SELECT 1 FROM ticks WHERE token=? AND price>0", (c["token"],)):
            misses = int(c["misses"] or 0) + 1
            db.x("UPDATE lab_cases SET misses=? WHERE token=?", (misses, c["token"]))
            if misses >= ZERO_AFTER_MISSES:
                db.x("INSERT OR IGNORE INTO ticks(token,ts,price) VALUES(?,?,0)", (c["token"], now))
                if misses == ZERO_AFTER_MISSES:
                    db.add_event("lab", f"lab case ${c['symbol']}: no price from the feed for {misses} cycles, booked at 0", c["token"])
        if now - c["t0"] >= HORIZON_S:
            _resolve(db, c)


def _resolve(db, c):
    now = int(time.time())
    path = [(r["ts"], r["price"]) for r in db.q("SELECT ts, price FROM ticks WHERE token=? ORDER BY ts", (c["token"],))]
    if len(path) < 3 or path[-1][0] < c["t0"] + HORIZON_S - STALE_TAIL_S:
        # a path that went silent is not a fill at its last price: no arm learns from it
        db.x("UPDATE lab_cases SET status='stale', resolved_ts=?, results=? WHERE token=?", (now, "{}", c["token"]))
        db.add_event("lab", f"lab case ${c['symbol']} stale: {len(path)} ticks, last one {(now - path[-1][0]) // 3600 if path else '?'}h ago", c["token"])
        return
    fee = c["cost"] if c.get("cost") is not None else FEE
    results = {}
    for arm in all_arms():
        r = simulate(arm, path, c["t0"], fee)
        if r is None:
            continue
        results[arm] = round(r, 4)
        db.x("UPDATE lab_arms SET n=n+1, sum_ret=sum_ret+?, sum_sq=sum_sq+?, wins=wins+?, updated=? WHERE name=?",
             (r, r * r, 1 if r > 0 else 0, now, arm))
    db.x("UPDATE lab_cases SET status='resolved', resolved_ts=?, results=? WHERE token=?", (now, json.dumps(results), c["token"]))
    if results:
        best = max(results, key=results.get)
        db.add_event("lab", f"lab case ${c['symbol']} resolved: best arm {best} {results[best] * 100:+.0f}%, hedge_2x@0m {results.get('hedge_2x@0m', 0) * 100:+.0f}%", c["token"])


def ranking(db):
    """Arms with mean, population stdev and a lower confidence bound (mean - LCB_Z standard errors);
    sorted by the bound among arms with LAB_MIN_N cases, the rest after them by case count."""
    ensure_tables(db)
    rows = db.q("SELECT * FROM lab_arms")
    out = []
    for r in rows:
        n = r["n"] or 0
        mean = (r["sum_ret"] / n) if n else None
        sd = math.sqrt(max(0.0, r["sum_sq"] / n - mean * mean)) if n >= 2 else None
        lcb = (mean - LCB_Z * sd / math.sqrt(n)) if sd is not None else None
        out.append({"arm": r["name"], "n": n, "mean_ret": round(mean, 4) if mean is not None else None,
                    "win_rate": round(100 * r["wins"] / n) if n else None,
                    "stdev": round(sd, 4) if sd is not None else None,
                    "lcb": round(lcb, 4) if lcb is not None else None})
    out.sort(key=lambda a: (-(a["lcb"] if a["lcb"] is not None and a["n"] >= LAB_MIN_N else -9), -(a["n"] or 0)))
    return out


def current_policy(db):
    """The arm in use: the best-bounded arm with enough cases whose lower bound is above zero and whose
    mean beats the default's on the same table, else the default."""
    arms = ranking(db)
    base = next((a for a in arms if a["arm"] == DEFAULT), None)
    for a in arms:
        if a["n"] < LAB_MIN_N or a["lcb"] is None or a["lcb"] <= 0 or a["arm"] == DEFAULT:
            continue
        if base is None or base["mean_ret"] is None or a["mean_ret"] > base["mean_ret"]:
            return a["arm"], "learned"
    if base and base["n"] >= LAB_MIN_N and base["lcb"] is not None and base["lcb"] > 0:
        return DEFAULT, "default, confirmed by the lab"
    return DEFAULT, "default until the lab has enough cases"


def pick_arm(db):
    """Arm for a new position: the current policy, or a random other arm when exploration is on."""
    best, _ = current_policy(db)
    if EXPLORE > 0 and random.random() < EXPLORE:
        return random.choice([a for a in all_arms() if a != best])
    return best


def summary(db):
    ensure_tables(db)
    cur, why = current_policy(db)
    return {"in_use": cur, "why": why, "arms": ranking(db), "min_n": LAB_MIN_N, "cost_per_side": FEE, "explore": EXPLORE,
            "cases_active": db.one("SELECT COUNT(*) n FROM lab_cases WHERE status='active'")["n"],
            "cases_resolved": db.one("SELECT COUNT(*) n FROM lab_cases WHERE status='resolved'")["n"],
            "cases_stale": db.one("SELECT COUNT(*) n FROM lab_cases WHERE status='stale'")["n"],
            "policies": {**POLICIES, **LEARNED}, "delays_min": list(DELAYS)}
