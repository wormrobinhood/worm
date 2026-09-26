"""The strategy lab: every candidate token is traded on paper by many policies at once.

A case starts when a verdict scores at least LAB_MIN_SCORE and has a price, or when the paper book enters a
token at a second look (watch.py). Prices are sampled every mark cycle for 48 hours, then every arm (exit policy x entry delay) is simulated on the same path with
that token's own costs (creator tax + curve fee + slippage per side). Arms keep a running net return
per dollar risked. Historical rankings nominate candidates. Promotion requires the separate fixed future cohort in
strategy_validation; historical fit alone never promotes an arm. Exploration (a random arm on a share of new positions) is off by
default: the lab already scores every arm on every case, so an explorer position teaches it nothing.

Everything here is a simulation on sampled prices, and it is built to err low (SIM_VERSION 2): a take-profit fills
at its level, never at a sample that jumped past it; stops and trails fill at the sample that crossed them; every
leg pays the paper book's gas; a gain earned across a hole in the data is capped; and a path is priced from one
source only (the pool on-chain for a paper entry, the price API for a verdict case). Version 1 did none of that and
its rankings (fixed_40_25@0m at +34% with a spread of 2.1: impossible for a rule that sells everything at +40%)
are kept aside in lab_arms_v1, never ranked."""
import json
import logging
import math
import os
import random
import time

from . import config as C
from .prices import token_prices, usable_price, observed_at

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
DEFAULT = "lock_20@0m"         # changing this voids every paper cohort (strategy_validation freezes it): deliberate, never casual
SIM_VERSION = 2                # results of another version are never ranked with these
GAS_PER_SIDE = 0.004           # gas per leg as a fraction of the position when the paper book has no measurement: about
                               # $0.04 on its $10 (the quoted swap plus two approvals it charges, at recent chain prices)
MAX_GAP_S = 900                # a position held across a longer silence in its path ...
GAP_CAP = 1.0                  # ... is credited at most +100%: what happened in the hole is unknown

# tp: list of (multiple, fraction of the initial tokens to sell); trail: drawdown from peak that sells the
# rest; trail_from_start: trailing active before any take-profit; stop: loss that sells everything before
# the first take-profit; max_age: seconds.
# Optional, for the profit-lock family: arm_at: the multiple at which the trailing stop arms without selling
# anything; trail_tiers: [(peak multiple, drawdown)], the drawdown in force once the peak reached that
# multiple (overrides trail); floor: once armed, the stop never sits below entry * floor.
POLICIES = {
    "hedge_2x":     {"tp": [(2.0, 0.60)], "trail": 0.50, "trail_from_start": False, "stop": -0.50, "max_age": HORIZON_S},
    "costout_1.5x": {"tp": [(1.5, 0.667)], "trail": 0.40, "trail_from_start": False, "stop": -0.35, "max_age": HORIZON_S},
    "ladder":       {"tp": [(1.5, 0.25), (2.0, 0.25), (3.0, 0.25)], "trail": 0.50, "trail_from_start": False, "stop": -0.40, "max_age": HORIZON_S},
    "fixed_100_40": {"tp": [(2.0, 1.0)], "trail": None, "trail_from_start": False, "stop": -0.40, "max_age": HORIZON_S},
    "fixed_40_25":  {"tp": [(1.4, 1.0)], "trail": None, "trail_from_start": False, "stop": -0.25, "max_age": HORIZON_S},
    "trail_35":     {"tp": [], "trail": 0.35, "trail_from_start": True, "stop": None, "max_age": HORIZON_S},
    "time_6h":      {"tp": [], "trail": None, "trail_from_start": False, "stop": -0.40, "max_age": 6 * 3600},
    "time_24h":     {"tp": [], "trail": None, "trail_from_start": False, "stop": -0.40, "max_age": 24 * 3600},
    # The profit-lock family (the creator's design, 2026-09-19): nothing is sold into strength. A hard stop cuts
    # the loss; once the position is 20% up a trailing stop follows the peak, wider after a big pump so a runner
    # can run; a position that has done neither within 12 hours is dead money and is closed.
    "lock_20":       {"tp": [], "trail": 0.15, "trail_from_start": False, "stop": -0.30, "max_age": 12 * 3600,
                      "arm_at": 1.2, "trail_tiers": [(2.0, 0.20), (4.0, 0.25)]},
    "lock_20_tight": {"tp": [], "trail": 0.10, "trail_from_start": False, "stop": -0.20, "max_age": 12 * 3600,
                      "arm_at": 1.2, "trail_tiers": [(2.0, 0.15), (4.0, 0.20)]},
    "lock_20_wide":  {"tp": [], "trail": 0.20, "trail_from_start": False, "stop": -0.30, "max_age": 12 * 3600,
                      "arm_at": 1.2, "trail_tiers": [(2.0, 0.30), (4.0, 0.40)]},
    "lock_50":       {"tp": [], "trail": 0.20, "trail_from_start": False, "stop": -0.30, "max_age": 12 * 3600,
                      "arm_at": 1.5, "trail_tiers": [(3.0, 0.30)]},
}
DELAYS = (0, 30, 60)
ARMS = [f"{p}@{d}m" for p in POLICIES for d in DELAYS]
LEARNED = {}                   # policies the advisor adopted, by name; loaded at startup (advisor.load)


def all_arms():
    return ARMS + [f"{p}@{d}m" for p in LEARNED for d in DELAYS]


def ensure_tables(db):
    db.x("CREATE TABLE IF NOT EXISTS lab_cases(token TEXT PRIMARY KEY, symbol TEXT, score INTEGER, verdict TEXT, t0 INTEGER,"
         " p0 REAL, status TEXT, resolved_ts INTEGER, results TEXT, cost REAL, misses INTEGER DEFAULT 0)")
    # source: where the path's prices come from ('api', 'pool', or 'mixed' for a v1 case that got a pool tick
    # among API ticks: never ranked). gas: per-leg gas as a fraction of the position. sim: the SIM_VERSION of
    # `results`. gap_s: the longest silence in the resolved path.
    for col in ("cost REAL", "misses INTEGER DEFAULT 0", "source TEXT", "pool_key TEXT", "pair TEXT", "gas REAL",
                "sim INTEGER", "gap_s INTEGER"):
        try:
            db.x(f"ALTER TABLE lab_cases ADD COLUMN {col}")
        except Exception:
            pass
    db.x("CREATE TABLE IF NOT EXISTS ticks(token TEXT, ts INTEGER, price REAL, PRIMARY KEY(token, ts))")
    for table in ("lab_arms", "lab_arms_usdg"):    # every case, and the USDG-pool cases only (what live could trade)
        db.x(f"CREATE TABLE IF NOT EXISTS {table}(name TEXT PRIMARY KEY, n INTEGER DEFAULT 0, sum_ret REAL DEFAULT 0,"
             " sum_sq REAL DEFAULT 0, wins INTEGER DEFAULT 0, updated INTEGER)")
        db.many(f"INSERT OR IGNORE INTO {table}(name) VALUES(?)", [(a,) for a in ARMS])
    if str(db.meta_get("lab_sim", "")) != str(SIM_VERSION):
        with db.transaction():
            # Once: v1 sums move aside; recent cases are re-simulated from their kept ticks (resimulate), older
            # ones stay legacy. A case the book entered got a pool tick among API ticks: mixed, never ranked.
            old = int(db.meta_get("lab_sim", "1") or 1)
            if db.one("SELECT 1 FROM lab_arms WHERE n>0"):
                db.x(f"DROP TABLE IF EXISTS lab_arms_v{old}")
                db.x(f"CREATE TABLE lab_arms_v{old} AS SELECT * FROM lab_arms")
            db.x("UPDATE lab_arms SET n=0, sum_ret=0, sum_sq=0, wins=0")
            if "strategy" in {r["name"] for r in db.q("PRAGMA table_info(paper)")}:
                db.x("UPDATE lab_cases SET source='mixed' WHERE source IS NULL AND token IN"
                     " (SELECT token FROM paper WHERE strategy IS NOT NULL AND strategy<>'verdict-healthy-v1')")
            db.x("UPDATE lab_cases SET source='api' WHERE source IS NULL")
            db.meta_set("lab_sim", SIM_VERSION)


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

def trail_pct(policy, peak_mult):
    """The drawdown from the peak that sells: the last tier whose multiple the peak has reached, else the
    policy's flat trail."""
    pct = policy.get("trail")
    for m, t in policy.get("trail_tiers") or []:
        if peak_mult >= m:
            pct = t
    return pct


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
    if policy.get("arm_at") and not st.get("trail_on") and st["peak"] >= st["entry"] * policy["arm_at"]:
        st["trail_on"] = True                  # in profit: from here the trailing stop guards it, nothing is sold yet
    if not st["tp_done"] and policy["stop"] is not None and mult <= 1 + policy["stop"] and st["qty_left"] > 0:
        return st["qty_left"], f"stop at {int(policy['stop'] * 100)}%"
    trail_on = st.get("trail_on") or policy["trail_from_start"]
    pct = trail_pct(policy, st["peak"] / st["entry"])
    if trail_on and pct and st["qty_left"] > 0:
        level = st["peak"] * (1 - pct)
        if policy.get("floor") and st.get("trail_on") and st["entry"] * policy["floor"] > level:
            if price <= st["entry"] * policy["floor"]:
                return st["qty_left"], f"profit lock at {policy['floor']:g}x"
        elif price <= level:
            return st["qty_left"], f"trailing stop {int(round(pct * 100))}% below the peak"
    if policy["max_age"] and ts - st["entry_ts"] >= policy["max_age"] and st["qty_left"] > 0:
        return st["qty_left"], "time limit"
    return 0.0, None


def simulate(arm, path, t0, fee=FEE, *, policy=None, delay=None, gas_per_side=0.0, max_gap_s=None, notes=None):
    """Net return per 1 unit risked for `arm` on `path` [(ts, price), ...], scale-free: every cash leg is
    a fraction of the position times price/entry, so a 1e-5 token and a $100 token with the same shape
    return the same number. `fee` is the per-side cost, `gas_per_side` each leg's gas as a fraction of the
    position. None when the arm has no usable entry: the first tick at or after its delay is missing, more
    than ENTRY_SLACK_S late, at a zero price, or the last.

    Fills err low: a take-profit is a limit at its level, so a sample that jumped past it between readings
    earns the level, not the jump; a stop or trail fills at the sample that crossed it (worse than the level
    when the price gapped through). With `max_gap_s`, a gain made while holding across a longer silence in the
    path is capped at GAP_CAP (`notes['capped']` says so): a hole in the data is never a windfall."""
    if policy is None:
        policy, delay = parse_arm(arm)
    entry_i = next((i for i, (ts, _) in enumerate(path) if ts >= t0 + delay), None)
    if entry_i is None or entry_i >= len(path) - 1:
        return None
    ets, e = path[entry_i]
    if ets > t0 + delay + ENTRY_SLACK_S or not e or e <= 0:
        return None
    st = {"entry": e, "entry_ts": ets, "qty_left": 1.0, "tp_done": [], "peak": e, "trail_on": False}
    cash = -gas_per_side
    qty_scale = 1.0 / (1 + fee)          # the entry fee buys fewer tokens
    held_across_gap, last_ts = False, ets
    for ts, p in path[entry_i + 1:]:
        held_across_gap = held_across_gap or (max_gap_s is not None and ts - last_ts > max_gap_s)
        last_ts = ts
        frac, why = exit_step(policy, st, p, ts)
        while frac > 0:
            fill = p
            if why and why.startswith("take profit"):
                fill = min(p, e * st["tp_done"][-1])
            cash += frac * qty_scale * (fill / e) * (1 - fee) - gas_per_side
            st["qty_left"] -= frac
            frac, why = exit_step(policy, st, p, ts) if st["qty_left"] > 0 else (0, None)
        if st["qty_left"] <= 1e-9:
            break
    if st["qty_left"] > 1e-9:
        cash += st["qty_left"] * qty_scale * (path[-1][1] / e) * (1 - fee) - gas_per_side
    ret = cash - 1.0                     # net return per 1 unit risked
    if held_across_gap and ret > GAP_CAP:
        ret = GAP_CAP
        if notes is not None:
            notes["capped"] = True
    return ret


# ---- the loop ---------------------------------------------------------------------------------

def enroll(db):
    """Enroll from the original outcome baseline, never the mutable latest token card.
    Legacy rows fall back to their recorded verdict time; their provenance is not reconstructed."""
    ensure_tables(db)
    rows = db.q("SELECT o.token, o.score, o.verdict, o.scored_at, o.price0, o.baseline_ts, l.symbol, l.creator_tax_bps, l.curve_fee_bps, s.metrics"
                " FROM outcomes o JOIN launches l ON l.token=o.token LEFT JOIN assessments s ON s.id=o.assessment_id"
                " WHERE o.score>=? AND o.price0 IS NOT NULL AND o.scored_at>=? AND o.token NOT IN (SELECT token FROM lab_cases)",
                (LAB_MIN_SCORE, int(time.time()) - 6 * 3600))
    for r in rows:
        try:
            m = json.loads(r["metrics"] or "{}")
        except ValueError:
            m = {}
        if m.get('partial'):
            continue
        t0 = int(r['baseline_ts'] or r['scored_at'])
        cost = side_cost(r["creator_tax_bps"], r["curve_fee_bps"])
        db.x("INSERT OR IGNORE INTO lab_cases(token,symbol,score,verdict,t0,p0,status,cost,misses) VALUES(?,?,?,?,?,?,?,?,0)",
             (r["token"], r["symbol"], r["score"], r["verdict"], t0, r["price0"], "active", cost if cost is not None else FEE))
        db.x("INSERT OR IGNORE INTO ticks(token,ts,price) VALUES(?,?,?)", (r["token"], t0, r["price0"]))
    return len(rows)


def tick(db):
    """Sample fresh observations only; incomplete paths expire without fabricated fills."""
    ensure_tables(db)
    enroll(db)
    now = int(time.time())
    db.x("DELETE FROM ticks WHERE ts<? AND token IN (SELECT token FROM lab_cases WHERE status<>'active')", (now - 7 * 86400,))
    # A path the paper book entered is priced from its pool on-chain by the watcher (watch.sample_lab); only the
    # verdict cases are read from the price API. One source per path: two sources disagree by more than a stop.
    active = db.q("SELECT * FROM lab_cases WHERE status='active'")
    if not active:
        resimulate(db)
        return
    px = token_prices([c["token"] for c in active if c.get("source") != "pool"])
    now = int(time.time())
    for c in active:
        entry = px.get(c["token"]) or {}
        p = usable_price(entry) if c.get("source") != "pool" else None
        if p is not None:
            ts = observed_at(entry, now)
            if ts >= c['t0']:
                db.x("INSERT OR IGNORE INTO ticks(token,ts,price) VALUES(?,?,?)", (c["token"], ts, p))
        # An API omission/outage is missing evidence, not a confirmed loss or an executable fill.
        if now - c["t0"] >= HORIZON_S:
            _resolve(db, c)
    resimulate(db)


def _resolve(db, c):
    with db.transaction():
        current = db.one("SELECT * FROM lab_cases WHERE token=? AND status='active'", (c['token'],))
        if current:
            _resolve_once(db, current)


def paper_gas(db):
    """The paper book's own gas per leg, as a fraction of its position: the median over its recent quoted
    positions (entry, exits and the approvals it charges, spread over two legs). GAS_PER_SIDE without any."""
    try:
        rows = db.q("SELECT gas_usd, size_usd FROM paper WHERE status='closed' AND execution_model IS NOT NULL"
                    " AND gas_usd>0 AND size_usd>0 ORDER BY id DESC LIMIT 50")
    except Exception:
        return GAS_PER_SIDE
    values = sorted(r["gas_usd"] / r["size_usd"] / 2 for r in rows)
    return values[len(values) // 2] if values else GAS_PER_SIDE


def case_pair(db, c):
    """'USDG', 'ETH' or another asset's symbol: the case's own pool when it has one, else the token's launch pair."""
    if c.get("pair"):
        return c["pair"]
    pool = db.one("SELECT quote FROM pools WHERE token=?", (c["token"],)) if db.one(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pools'") else None
    if pool and pool.get("quote"):
        return "USDG" if pool["quote"] == C.USDG else "ETH" if pool["quote"] == C.ZERO else pool["quote"]
    launch = db.one("SELECT pair_symbol FROM launches WHERE token=?", (c["token"],)) or {}
    return launch.get("pair_symbol")


def _resolve_once(db, c):
    now = int(time.time())
    path = [(r["ts"], r["price"]) for r in db.q("SELECT ts, price FROM ticks WHERE token=? ORDER BY ts", (c["token"],))]
    if len(path) < 3 or path[-1][0] < c["t0"] + HORIZON_S - STALE_TAIL_S:
        # a path that went silent is not a fill at its last price: no arm learns from it
        db.x("UPDATE lab_cases SET status='stale', resolved_ts=?, results=?, sim=? WHERE token=?", (now, "{}", SIM_VERSION, c["token"]))
        db.add_event("lab", f"lab case ${c['symbol']} stale: {len(path)} ticks, last one {(now - path[-1][0]) // 3600 if path else '?'}h ago", c["token"])
        return
    fee = c["cost"] if c.get("cost") is not None else FEE
    gas = c["gas"] if c.get("gas") is not None else paper_gas(db)
    pair = case_pair(db, c)
    ranked = c.get("source") != "mixed"
    gap = max((b[0] - a[0] for a, b in zip(path, path[1:]) if b[0] >= c["t0"]), default=0)
    results, capped = {}, []
    for arm in all_arms():
        notes = {}
        r = simulate(arm, path, c["t0"], fee, gas_per_side=gas, max_gap_s=MAX_GAP_S, notes=notes)
        if r is None:
            continue
        results[arm] = round(r, 4)
        if notes.get("capped"):
            capped.append(arm)
        if not ranked:
            continue
        for table in ("lab_arms",) + (("lab_arms_usdg",) if pair == "USDG" else ()):
            db.x(f"INSERT OR IGNORE INTO {table}(name) VALUES(?)", (arm,))     # an arm the advisor added later
            db.x(f"UPDATE {table} SET n=n+1, sum_ret=sum_ret+?, sum_sq=sum_sq+?, wins=wins+?, updated=? WHERE name=?",
                 (r, r * r, 1 if r > 0 else 0, now, arm))
    if capped:
        results["_capped"] = capped
    db.x("UPDATE lab_cases SET status='resolved', resolved_ts=?, results=?, sim=?, pair=?, gas=?, gap_s=? WHERE token=?",
         (now, json.dumps(results), SIM_VERSION, pair, gas, gap, c["token"]))
    arms = {a: v for a, v in results.items() if not a.startswith("_")}
    if arms:
        best = max(arms, key=arms.get)
        db.add_event("lab", f"lab case ${c['symbol']} resolved (simulated{'' if ranked else ', mixed prices: not ranked'}):"
                     f" best arm {best} {arms[best] * 100:+.0f}%, {DEFAULT} {arms.get(DEFAULT, 0) * 100:+.0f}%", c["token"])


def resimulate(db, limit=40):
    """Resolved v1 cases whose ticks are still kept are simulated again under SIM_VERSION and ranked; the rest
    (ticks pruned after a week) stay as they were, marked legacy (sim 1), and are never ranked."""
    for c in db.q("SELECT * FROM lab_cases WHERE status='resolved' AND sim IS NULL LIMIT ?", (limit,)):
        with db.transaction():
            path = db.q("SELECT ts FROM ticks WHERE token=? ORDER BY ts", (c["token"],))
            if len(path) < 3 or path[0]["ts"] > c["t0"] or path[-1]["ts"] < c["t0"] + HORIZON_S - STALE_TAIL_S:
                db.x("UPDATE lab_cases SET sim=1 WHERE token=?", (c["token"],))      # legacy v1: not enough left to redo
                continue
            _resolve_once(db, c)
            db.x("UPDATE lab_cases SET resolved_ts=? WHERE token=?", (c["resolved_ts"], c["token"]))


def ranking(db, table="lab_arms"):
    """Arms with mean, population stdev and a lower confidence bound (mean - LCB_Z standard errors);
    sorted by the bound among arms with LAB_MIN_N cases, the rest after them by case count. `table`:
    lab_arms (every ranked case) or lab_arms_usdg (USDG-pool cases only)."""
    ensure_tables(db)
    rows = db.q(f"SELECT * FROM {table}")
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


def research_policy(db):
    """The arm the lab's own table would pick: the best-bounded arm with enough cases whose lower bound is above
    zero and whose mean beats the default's on the same table, else the default. Research only: nothing that
    opens a position calls it. Exits come from current_policy, which is DEFAULT until changed in code."""
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


def current_policy(db):
    """The exit rule new positions get. One default, changed only in code: a change voids every paper
    cohort (strategy_validation freezes it), so the lab's ranking informs a decision and never makes it."""
    from . import strategy_validation
    evidence = strategy_validation.summary(db)
    if evidence['passed']:
        return DEFAULT, 'confirmed by a paper cohort: ' + ', '.join(evidence['passed_rules'])
    return DEFAULT, 'default until a paper cohort passes'


def pick_arm(db):
    """Arm for a new position: the current policy, or a random other arm when exploration is on."""
    best, _ = current_policy(db)
    if EXPLORE > 0 and random.random() < EXPLORE:
        return random.choice([a for a in all_arms() if a != best])
    return best


def summary(db):
    from . import strategy_validation
    ensure_tables(db)
    cur, why = current_policy(db)
    count = lambda where, *a: db.one(f"SELECT COUNT(*) n FROM lab_cases WHERE {where}", a)["n"]
    return {"in_use": cur, "why": why, "validation": strategy_validation.summary(db), "arms": ranking(db), "min_n": LAB_MIN_N, "cost_per_side": FEE, "explore": EXPLORE,
            # USDG-pool cases only: the exits live could run. Both tables are simulations, never paper fills.
            "arms_usdg": ranking(db, "lab_arms_usdg"), "simulated": True, "sim_version": SIM_VERSION,
            "gas_per_side": round(paper_gas(db), 5), "max_gap_s": MAX_GAP_S, "gap_cap": GAP_CAP,
            "method": ("simulated on sampled prices, not traded: take-profits fill at their level, stops and trails at the "
                       "sample that crossed them, every leg pays the paper book's gas, a gain made across a silence of "
                       f"more than {MAX_GAP_S // 60} min is capped at +{GAP_CAP * 100:.0f}%, one price source per path"),
            "cases_ranked": count("status='resolved' AND sim=? AND COALESCE(source,'api')<>'mixed'", SIM_VERSION),
            "cases_mixed": count("status='resolved' AND source='mixed'"),
            "cases_legacy": count("status='resolved' AND (sim IS NULL OR sim<>?)", SIM_VERSION),
            "cases_capped": count("status='resolved' AND sim=? AND results LIKE '%\"_capped\"%'", SIM_VERSION),
            "cases_active": db.one("SELECT COUNT(*) n FROM lab_cases WHERE status='active'")["n"],
            "cases_resolved": db.one("SELECT COUNT(*) n FROM lab_cases WHERE status='resolved'")["n"],
            "cases_stale": db.one("SELECT COUNT(*) n FROM lab_cases WHERE status='stale'")["n"],
            "policies": {**POLICIES, **LEARNED}, "delays_min": list(DELAYS)}
