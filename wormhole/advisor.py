"""The advisor: the writer reads the worm's own records and proposes; the records decide.

Every WH_ADVISOR_EVERY_MIN (120) minutes, once at least WH_ADVISOR_MIN_NEW (3) more verdicts have
resolved since the last run, the worm hands its writer a packet of measurements (how each rule has
done, the scorecard, the at-scan metrics of the last resolved cases, the lab's ranking) and asks for
hypotheses in a strict form: a scoring rule is at most three conditions over whitelisted at-scan
metrics plus points; an exit arm is a lab policy within bounds. Nothing the writer says runs as code.
Every rule is screened historically and then must pass a frozen future cohort before adoption. Historical screening checks if the tokens it
fires on went on to move differently from the rest: a median difference in the outcome change of at
least MIN_SEP points that fewer than P_MAX of random splits of the same sizes would show (a permutation
test), on at least MIN_N cases each side, and it is not a copy of a rule the worm already has. An adopted rule scores future tokens as `ai_<n>` and is re-weighted by the brain like
every other rule; an adopted arm joins the lab, where the usual 30-case bar still decides whether it
is ever used. With the stub writer the worm runs its own one-metric threshold search through the same
gate, so the loop learns without a model too. Every proposal, its backtest and its fate are kept and
shown. Token names and symbols never enter the packet: they are written by strangers."""
import json
import logging
import os
import random
import re
import statistics
import time

from . import config as C
from . import lab
from . import voice
from . import shadow

log = logging.getLogger("wormhole.advisor")
EVERY_MIN = int(os.environ.get("WH_ADVISOR_EVERY_MIN", "120"))
MIN_NEW = int(os.environ.get("WH_ADVISOR_MIN_NEW", "3"))       # newly resolved verdicts a run needs
MODEL = os.environ.get("WH_ADVISOR_MODEL", "").strip() or voice.MODEL
MIN_N = 20                     # cases a rule must fire on, and leave, to be judged
MIN_SEP = 10.0                 # points of outcome change the medians must differ by
PERMS = 400                    # random splits the observed separation is compared with
P_MAX = 0.02                   # share of random splits allowed to separate as much: past this it is chance
MAX_PROPOSALS = 5
MAX_RULES = 8                  # learned rules in use at once
MAX_ARMS = 6                   # learned exit arms in the lab at once
RETIRE_N = 40                  # fired cases after which a rule that no longer separates is retired
DUPLICATE_JACCARD = 0.8        # a candidate firing on (nearly) the same tokens as an existing rule is that rule
POINTS_WARN = (-20, -3)        # points a warning rule may carry
POINTS_GOOD = (3, 10)          # points a reassuring rule may carry
RECENT_CASES = 30
SCAN_WINDOW_S = 3600           # a token's metrics count only if its latest scan is this close to the verdict

# At-scan metrics a rule may look at: what the scorer measured when it gave the verdict. Prices, FDV and
# volume are left out because they move with time and would let the outcome leak into the rule.
METRICS = {
    "holders": ("holders at scan", 0, 1e6),
    "top10_pct": ("top-10 holders' share of circulating supply, percent", 0, 100),
    "deployer_hold_pct": ("creator's current share of circulating supply, percent", 0, 100),
    "outside_pool_pct": ("supply held outside the pool, percent", 0, 100),
    "unique_buyers": ("unique buyers on the bonding curve", 0, 1e6),
    "curve_buys": ("buys on the bonding curve", 0, 1e6),
    "curve_sells": ("sells on the bonding curve", 0, 1e6),
    "snipe_pct": ("curve supply bought in the 3-second snipe window, percent", 0, 100),
    "top_buyer_pct": ("one wallet's share of all curve buys, percent", 0, 100),
    "deployer_buy_pct": ("creator's share of curve buys, percent", 0, 100),
    "creator_prev_launches": ("other tokens this creator launched in the window", 0, 1e6),
    "creator_prev_grads": ("other tokens this creator graduated in the window", 0, 1e6),
    "creator_tax_bps": ("creator tax in basis points (100 = 1 percent)", 0, 1000),
    "swaps_1h": ("pool swaps in the hour after graduation", 0, 1e7),
    "buys_1h": ("pool buys in the hour after graduation", 0, 1e7),
    "sells_1h": ("pool sells in the hour after graduation", 0, 1e7),
    "swaps_since_grad": ("pool swaps since graduation at scan", 0, 1e7),
    "launch_to_grad_s": ("seconds from launch to graduation", 0, 1e7),
    "grad_age_s": ("seconds from graduation to the scan", 0, 1e7),
    "fresh_buyers_pct": ("share of curve buy volume from throwaway wallets, percent", 0, 100),
    "fleet_pct": ("share of curve buy volume from fleet wallets, percent", 0, 100),
    "top_funder_pct": ("share of buyers funded by one wallet before launch, percent", 0, 100),
    "transfers": ("token transfers read for the holder map", 0, 1e7),
}
OPS = {">=", "<=", ">", "<"}
ARM_BOUNDS = {"tp_mult": (1.1, 5.0), "trail": (0.10, 0.80), "stop": (-0.70, -0.10), "max_age_h": (1, 48)}

SYSTEM = """You advise a small program that screens token launches on Robinhood Chain and re-weights its
rules from outcomes. You get its measurements as JSON. Propose hypotheses only, in the exact JSON form
requested; nothing else. Rules may only use the listed metrics with the listed operators and value
ranges, at most three conditions each, and points within the stated bounds: negative points for a
warning about tokens that tend to fall further, positive points for tokens that tend to hold up. Exit
arms must stay within the stated bounds. Do not repeat rules the program already has or has tried. Each
proposal carries one plain "why" under 140 characters, no hype, no advice, no token names. The program
backtests every proposal on its own history and adopts only what passes; be specific, not clever."""


# ---- tables ---------------------------------------------------------------------------------

def ensure_tables(db):
    db.x("CREATE TABLE IF NOT EXISTS advisor_runs(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, model TEXT,"
         " tokens_in INTEGER, tokens_out INTEGER, resolved INTEGER, proposed INTEGER, adopted INTEGER, note TEXT)")
    db.x("CREATE TABLE IF NOT EXISTS advisor_suggestions(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, ts INTEGER,"
         " kind TEXT, spec TEXT, why TEXT, backtest TEXT, status TEXT, reason TEXT, model TEXT)")
    db.x("CREATE TABLE IF NOT EXISTS learned_rules(id TEXT PRIMARY KEY, spec TEXT, points REAL, text TEXT, created INTEGER,"
         " run_id INTEGER, active INTEGER DEFAULT 1, backtest TEXT, retired INTEGER)")
    db.x("CREATE TABLE IF NOT EXISTS learned_arms(name TEXT PRIMARY KEY, policy TEXT, why TEXT, created INTEGER, run_id INTEGER,"
         " active INTEGER DEFAULT 1, backtest TEXT)")


# ---- rules: form, matching, text ------------------------------------------------------------

def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def validate_rule(spec):
    """(clean spec, None) or (None, why). The clean spec is {"conditions": [{"metric","op","value"}], "points": int}."""
    if not isinstance(spec, dict):
        return None, "not an object"
    conds = spec.get("conditions")
    if not isinstance(conds, list) or not 1 <= len(conds) <= 3:
        return None, "1 to 3 conditions"
    clean = []
    for c in conds:
        if not isinstance(c, dict):
            return None, "condition is not an object"
        metric, op, value = c.get("metric"), c.get("op"), _num(c.get("value"))
        if metric not in METRICS:
            return None, f"metric {str(metric)[:30]!r} is not measured at scan"
        if op not in OPS:
            return None, f"operator {str(op)[:10]!r}"
        lo, hi = METRICS[metric][1], METRICS[metric][2]
        if value is None or not lo <= value <= hi:
            return None, f"{metric} value out of range"
        clean.append({"metric": metric, "op": ">=" if op == ">" else "<=" if op == "<" else op, "value": round(value, 4)})
    pts = _num(spec.get("points"))
    if pts is None or pts == 0 or pts != int(pts):
        return None, "points must be a whole non-zero number"
    pts = int(pts)
    lo, hi = (POINTS_WARN if pts < 0 else POINTS_GOOD)
    if not lo <= pts <= hi:
        return None, f"points outside {lo}..{hi}"
    if len({(c["metric"], c["op"]) for c in clean}) != len(clean):
        return None, "a metric and operator repeat"
    return {"conditions": clean, "points": pts}, None


def matches(spec, m):
    """True when every condition holds on the metrics dict; a missing or non-numeric metric never matches."""
    for c in spec["conditions"]:
        v = _num(m.get(c["metric"]))
        if v is None:
            return False
        if c["op"] == ">=" and not v >= c["value"]:
            return False
        if c["op"] == "<=" and not v <= c["value"]:
            return False
    return True


def rule_text(spec):
    """One line a stranger can read next to the verdict."""
    parts = []
    for c in spec["conditions"]:
        v = c["value"]
        vs = f"{int(v)}" if v == int(v) else f"{v:g}"
        parts.append(f"{c['metric']} {'at least' if c['op'] == '>=' else 'at most'} {vs}")
    return " and ".join(parts) + " (learned)"


def sanitize_why(text):
    t = re.sub(r"[^\w\s.,;:%()+\-/']", "", str(text or ""))[:140].strip()
    return "" if voice.BANNED.search(t) else t


# ---- history and the backtest ---------------------------------------------------------------

def history(db):
    """Resolved outcomes joined to complete immutable assessments. Legacy rows without a
    verified assessment link remain stored but do not supply training features."""
    rows = db.q("SELECT s.token, s.metrics, s.fired, s.scored_at s_at, o.scored_at o_at, o.change_pct,l.deployer creator FROM assessments s"
                " JOIN outcomes o ON o.assessment_id=s.id LEFT JOIN launches l ON l.token=s.token WHERE o.resolved=1 AND o.outcome!='unknown'"
                " AND o.change_pct IS NOT NULL AND s.partial=0")
    out = []
    for r in rows:
        if abs(int(r["s_at"] or 0) - int(r["o_at"] or 0)) > SCAN_WINDOW_S:
            continue
        try:
            m = json.loads(r["metrics"] or "{}")
            fired = json.loads(r["fired"] or "[]")
        except ValueError:
            continue
        if not isinstance(m, dict):
            continue
        out.append({"token": r["token"], "creator": r["creator"], "m": m, "change": float(r["change_pct"]),
                    "fired": {f["rule"]: f.get("points", 0) for f in fired if isinstance(f, dict) and f.get("rule")}})
    return out


def judge(fired, other, perms=PERMS, seed=7):
    """The median change of the fired cases minus the rest, and the share of random splits of the same
    sizes whose difference is at least as large on the same side: how easily chance alone does this.
    Deterministic for a given history."""
    rnd = random.Random(seed)
    diff = statistics.median(fired) - statistics.median(other)
    pool, k, hits = list(fired) + list(other), len(fired), 0
    for _ in range(perms):
        rnd.shuffle(pool)
        d = statistics.median(pool[:k]) - statistics.median(pool[k:])
        if diff == 0 or (diff < 0 and d <= diff) or (diff > 0 and d >= diff):
            hits += 1
    return diff, hits / perms


def backtest_rule(hist, spec, points, taken):
    """Judge a rule on the history. `taken` maps an existing rule id to (sign, set of tokens it fired on).
    Returns {n_fired, n_other, median_fired, median_other, diff, p, accepted, why}."""
    fired = [h for h in hist if matches(spec, h["m"])]
    other = [h for h in hist if not matches(spec, h["m"])]
    bt = {"n_fired": len(fired), "n_other": len(other), "accepted": False}
    if len(fired) < MIN_N or len(other) < MIN_N:
        bt["why"] = f"needs {MIN_N} cases each side; fires on {len(fired)} of {len(hist)}"
        return bt
    mine = {h["token"] for h in fired}
    everyone = {h["token"] for h in hist}
    for rid, (sign, toks) in taken.items():
        if not toks:
            continue
        # the same sign on the same tokens is the same rule; the opposite sign on the other tokens is its mirror image
        other_set = toks if sign == (points > 0) else everyone - toks
        if other_set and len(mine & other_set) / len(mine | other_set) >= DUPLICATE_JACCARD:
            bt["why"] = f"fires on the same tokens as {rid}" if sign == (points > 0) else f"the mirror image of {rid}"
            return bt
    diff, p = judge([h["change"] for h in fired], [h["change"] for h in other])
    bt.update(median_fired=round(statistics.median(h["change"] for h in fired), 1),
              median_other=round(statistics.median(h["change"] for h in other), 1), diff=round(diff, 1), p=round(p, 3))
    chance = f"{int(round(p * 100))} in 100 random splits do as much"
    if points < 0:
        ok = p <= P_MAX and diff <= -MIN_SEP
        bt["why"] = (f"fired tokens fell {abs(diff):.0f} points further; {chance}" if ok
                     else f"fired tokens did not fall clearly further (median difference {diff:+.0f} points; {chance})")
    else:
        ok = p <= P_MAX and diff >= MIN_SEP
        bt["why"] = (f"fired tokens held up {diff:.0f} points better; {chance}" if ok
                     else f"fired tokens did not hold up clearly better (median difference {diff:+.0f} points; {chance})")
    bt["accepted"] = ok
    return bt


def taken_sets(db, hist):
    """For every rule that has fired with points, the sign and the tokens it fired on, from the history."""
    ensure_tables(db)
    out = {}
    for h in hist:
        for rid, pts in h["fired"].items():
            if pts:
                out.setdefault(rid, (pts > 0, set()))[1].add(h["token"])
    for r in db.q("SELECT id, spec, points FROM learned_rules WHERE active=1"):
        spec = json.loads(r["spec"])
        out[r["id"]] = (float(r["points"]) > 0, {h["token"] for h in hist if matches(spec, h["m"])})
    return out


# ---- the stub: the worm's own one-metric search ---------------------------------------------

def stub_propose(hist, tried, used=()):
    """Thresholds at the deciles of every metric, both directions, judged by the same separation; the
    three strongest candidates that were not tried before and do not re-cut a direction a learned rule
    already covers. Free, and it runs without any model."""
    cands = []
    for metric in METRICS:
        vals = sorted(v for v in (_num(h["m"].get(metric)) for h in hist) if v is not None)
        if len(vals) < 2 * MIN_N:
            continue
        for q in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
            t = vals[int(q * (len(vals) - 1))]
            for op in (">=", "<="):
                if (metric, op) in used:
                    continue
                spec = {"conditions": [{"metric": metric, "op": op, "value": round(t, 4)}]}
                key = json.dumps(spec["conditions"], sort_keys=True)
                if key in tried:
                    continue
                fired = [h["change"] for h in hist if matches(spec, h["m"])]
                other = [h["change"] for h in hist if not matches(spec, h["m"])]
                if len(fired) < MIN_N or len(other) < MIN_N:
                    continue
                diff, p = judge(fired, other, perms=100)
                if p <= P_MAX and diff <= -MIN_SEP:
                    cands.append((-diff, {"conditions": spec["conditions"], "points": -8,
                                          "why": f"{metric} {'high' if op == '>=' else 'low'}: those tokens fell {abs(diff):.0f} points further"}))
                elif p <= P_MAX and diff >= MIN_SEP:
                    cands.append((diff, {"conditions": spec["conditions"], "points": 5,
                                         "why": f"{metric} {'high' if op == '>=' else 'low'}: those tokens held up {diff:.0f} points better"}))
    cands.sort(key=lambda x: -x[0])
    out, seen = [], set()
    for _, spec in cands:
        metric = spec["conditions"][0]["metric"]
        if metric in seen:
            continue
        seen.add(metric)
        out.append(spec)
        if len(out) == 3:
            break
    return {"rules": out, "arms": [], "notes": "the worm's own threshold search (no model)"}


# ---- exit arms ------------------------------------------------------------------------------

def validate_arm(spec):
    """(name, policy, delay_s, None) or (None, None, None, why)."""
    if not isinstance(spec, dict):
        return None, None, None, "not an object"
    name = re.sub(r"[^a-z0-9_]", "", str(spec.get("name", "")).lower())[:24]
    if len(name) < 3:
        return None, None, None, "name"
    tps = spec.get("tp") or []
    if not isinstance(tps, list) or len(tps) > 3:
        return None, None, None, "at most 3 take-profit steps"
    tp = []
    for step in tps:
        if not isinstance(step, (list, tuple)) or len(step) != 2:
            return None, None, None, "take-profit step is [multiple, fraction]"
        mult, frac = _num(step[0]), _num(step[1])
        if mult is None or frac is None or not ARM_BOUNDS["tp_mult"][0] <= mult <= ARM_BOUNDS["tp_mult"][1] or not 0 < frac <= 1:
            return None, None, None, "take-profit step out of bounds"
        tp.append((round(mult, 3), round(frac, 3)))
    if sum(f for _, f in tp) > 1.0001:
        return None, None, None, "take-profit fractions add up to more than one"
    tp.sort()
    trail = _num(spec.get("trail")) if spec.get("trail") is not None else None
    if trail is not None and not ARM_BOUNDS["trail"][0] <= trail <= ARM_BOUNDS["trail"][1]:
        return None, None, None, "trail out of bounds"
    stop = _num(spec.get("stop")) if spec.get("stop") is not None else None
    if stop is not None and not ARM_BOUNDS["stop"][0] <= stop <= ARM_BOUNDS["stop"][1]:
        return None, None, None, "stop out of bounds"
    max_age_h = _num(spec.get("max_age_h"))
    if max_age_h is None or not ARM_BOUNDS["max_age_h"][0] <= max_age_h <= ARM_BOUNDS["max_age_h"][1]:
        return None, None, None, "max_age_h out of bounds"
    delay = _num(spec.get("delay_min"))
    if delay is None or int(delay) not in lab.DELAYS:
        return None, None, None, f"delay_min must be one of {list(lab.DELAYS)}"
    if not tp and trail is None and stop is None:
        return None, None, None, "an arm needs a take-profit, a trail or a stop"
    policy = {"tp": tp, "trail": trail, "trail_from_start": bool(spec.get("trail_from_start", not tp)), "stop": stop,
              "max_age": int(max_age_h * 3600)}
    return "ai_" + name, policy, int(delay) * 60, None


def _same_policy(a, b):
    return (sorted(map(tuple, a["tp"])) == sorted(map(tuple, b["tp"])) and a["trail"] == b["trail"] and a["stop"] == b["stop"]
            and a["max_age"] == b["max_age"] and bool(a["trail_from_start"]) == bool(b["trail_from_start"]))


def backtest_arm(db, name, policy, delay):
    """The arm against the lab's default on every resolved case whose ticks are still kept."""
    lab.LEARNED[name] = policy
    rets, base = [], []
    try:
        for c in db.q("SELECT token, t0, cost FROM lab_cases WHERE status='resolved'"):
            path = [(r["ts"], r["price"]) for r in db.q("SELECT ts, price FROM ticks WHERE token=? ORDER BY ts", (c["token"],))]
            if len(path) < 3:
                continue
            fee = c["cost"] if c.get("cost") is not None else lab.FEE
            r = lab.simulate(f"{name}@{delay // 60}m", path, c["t0"], fee)
            d = lab.simulate(lab.DEFAULT, path, c["t0"], fee)
            if r is None or d is None:
                continue
            rets.append(r)
            base.append(d)
    finally:
        lab.LEARNED.pop(name, None)
    if not rets:
        return {"n": 0, "why": "no resolved lab case to test on yet; the lab will rank it as cases resolve"}
    return {"n": len(rets), "mean_ret": round(statistics.fmean(rets), 4), "default_mean_ret": round(statistics.fmean(base), 4),
            "why": f"{len(rets)} cases: {statistics.fmean(rets) * 100:+.0f}% against the default's {statistics.fmean(base) * 100:+.0f}%"}


# ---- the writer -----------------------------------------------------------------------------

def packet(db, hist, brain_summary, lab_summary):
    """What the writer sees: measurements only, no names."""
    changes = [h["change"] for h in hist]
    metrics = {}
    for k, (about, lo, hi) in METRICS.items():
        vals = sorted(v for v in (_num(h["m"].get(k)) for h in hist) if v is not None)
        if len(vals) >= MIN_N:
            metrics[k] = {"about": about, "cases": len(vals), "p10": vals[int(0.1 * (len(vals) - 1))],
                          "p50": vals[len(vals) // 2], "p90": vals[int(0.9 * (len(vals) - 1))], "range": [lo, hi]}
    recent = []
    for h in sorted(hist, key=lambda h: h["m"].get("price0_ts") or 0, reverse=True)[:RECENT_CASES]:
        row = {k: h["m"].get(k) for k in METRICS if _num(h["m"].get(k)) is not None}
        row["change_pct"] = round(h["change"])
        recent.append(row)
    rules = [{k: r.get(k) for k in ("id", "about", "weight", "lift", "fired_bad", "fired_good")} for r in brain_summary.get("rules", [])]
    learned = [{"id": r["id"], "conditions": json.loads(r["spec"])["conditions"], "points": r["points"], "backtest": json.loads(r["backtest"] or "{}")}
               for r in db.q("SELECT id, spec, points, backtest FROM learned_rules WHERE active=1")]
    tried = [{"kind": r["kind"], "spec": json.loads(r["spec"]), "status": r["status"], "reason": r["reason"]}
             for r in db.q("SELECT kind, spec, status, reason FROM advisor_suggestions ORDER BY id DESC LIMIT 20")]
    arms = [{k: a.get(k) for k in ("arm", "n", "mean_ret", "lcb", "win_rate")} for a in lab_summary.get("arms", [])[:10]]
    return {
        "outcomes": {"resolved_with_change": len(hist), "median_change_pct": round(statistics.median(changes)) if changes else None,
                     "scorecard": brain_summary.get("scorecard", {}), "base_rate_pct": brain_summary.get("base_rate_pct")},
        "rules": rules, "metrics": metrics, "recent_cases": recent, "learned_rules": learned, "tried": tried,
        "lab": {"in_use": lab_summary.get("in_use"), "arms": arms, "policies": lab_summary.get("policies", {}),
                "delays_min": lab_summary.get("delays_min", list(lab.DELAYS)), "learned_arms": [r["name"] for r in db.q("SELECT name FROM learned_arms WHERE active=1")]},
        "bar": {"min_cases_each_side": MIN_N, "min_median_separation_pct": MIN_SEP,
                "chance": f"at most {P_MAX:g} of random splits of the same sizes may separate outcomes as much (permutation test)",
                "points_warning": list(POINTS_WARN), "points_reassuring": list(POINTS_GOOD), "operators": sorted(OPS),
                "arm_bounds": ARM_BOUNDS, "max_proposals": MAX_PROPOSALS},
        "form": {"rules": [{"conditions": [{"metric": "top10_pct", "op": ">=", "value": 60}], "points": -8, "why": "..."}],
                 "arms": [{"name": "short_name", "tp": [[1.8, 0.5]], "trail": 0.3, "stop": -0.3, "max_age_h": 24, "delay_min": 0, "why": "..."}],
                 "notes": "one or two plain sentences on what the records suggest"},
    }


def ask(pkt):
    """Proposals from the writer: (dict, usage). The stub is the worm's own search."""
    if MODEL == "stub":
        return None, {}
    kind, _, model = MODEL.partition(":")
    raw, usage = voice._llm(kind, model, SYSTEM, "Records:\n" + json.dumps(pkt, indent=1) + "\nAnswer with the JSON form only.",
                            max_tokens=1200)
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        j = json.loads(m.group(0)) if m else None
    except ValueError:
        j = None
    return (j if isinstance(j, dict) else {"rules": [], "arms": [], "notes": "", "unparseable": True}), (usage or {})


def _tokens(usage):
    return (int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0), int(usage.get("output_tokens") or usage.get("completion_tokens") or 0))


# ---- adopting, applying, retiring -----------------------------------------------------------

def _next_rule_id(db):
    n = db.one("SELECT COUNT(*) n FROM learned_rules")["n"]
    return f"ai_{n + 1}"


def adopt_rule(db, spec, bt, run_id):
    rid = _next_rule_id(db)
    now = int(time.time())
    db.x("INSERT INTO learned_rules(id,spec,points,text,created,run_id,active,backtest) VALUES(?,?,?,?,?,?,1,?)",
         (rid, json.dumps(spec), spec["points"], rule_text(spec), now, run_id, json.dumps(bt)))
    db.x("INSERT OR IGNORE INTO rules(id,weight,hits,misses,updated) VALUES(?,1.0,0,0,?)", (rid, now))
    return rid


def adopt_arm(db, name, policy, delay, why, bt, run_id):
    lab.LEARNED[name] = policy
    db.x("INSERT OR REPLACE INTO learned_arms(name,policy,why,created,run_id,active,backtest) VALUES(?,?,?,?,?,1,?)",
         (name, json.dumps(policy), why, int(time.time()), run_id, json.dumps(bt)))
    lab.ensure_tables(db)
    db.many("INSERT OR IGNORE INTO lab_arms(name) VALUES(?)", [(f"{name}@{d}m",) for d in lab.DELAYS])


def load(db):
    """Learned arms into the lab at startup, so parse_arm knows them."""
    ensure_tables(db)
    for r in db.q("SELECT name, policy FROM learned_arms WHERE active=1"):
        p = json.loads(r["policy"])
        p["tp"] = [tuple(x) for x in p.get("tp", [])]
        lab.LEARNED[r["name"]] = p


def apply(db, m):
    """The learned rules that fire on a fresh scan's metrics: {rule, points, text} entries for the scorer."""
    out = []
    try:
        ensure_tables(db)
        for r in db.q("SELECT id, spec, points, text FROM learned_rules WHERE active=1"):
            if matches(json.loads(r["spec"]), m):
                out.append({"rule": r["id"], "points": int(r["points"]), "text": r["text"]})
    except Exception as e:
        log.info("learned rules skipped: %s", e)
    return out


def describe(db, rid):
    r = db.one("SELECT text, active FROM learned_rules WHERE id=?", (rid,))
    return (r["text"] + ("" if r["active"] else " (retired)")) if r else ""


def revalidate(db, hist):
    """Retire a learned rule that, with enough cases behind it, no longer separates outcomes."""
    ensure_tables(db)
    retired = []
    for r in db.q("SELECT id, spec, points FROM learned_rules WHERE active=1"):
        spec = json.loads(r["spec"])
        bt = backtest_rule(hist, spec, float(r["points"]), {})
        if bt["n_fired"] >= RETIRE_N and not bt["accepted"]:
            db.x("UPDATE learned_rules SET active=0, retired=?, backtest=? WHERE id=?", (int(time.time()), json.dumps(bt), r["id"]))
            retired.append(r["id"])
        elif bt["n_fired"] >= MIN_N:
            db.x("UPDATE learned_rules SET backtest=? WHERE id=?", (json.dumps(bt), r["id"]))
    return retired


# ---- the run --------------------------------------------------------------------------------

def due(db, now=None):
    """(True, why) when a run is due: the interval has passed and enough new verdicts have resolved."""
    ensure_tables(db)
    now = now or time.time()
    resolved = db.one("SELECT COUNT(*) n FROM outcomes WHERE resolved=1 AND outcome!='unknown'")["n"]
    if resolved < 2 * MIN_N:
        return False, f"waits for {2 * MIN_N} resolved verdicts ({resolved} so far)"
    last = db.one("SELECT * FROM advisor_runs ORDER BY id DESC LIMIT 1")
    if last:
        if now - int(last["ts"]) < EVERY_MIN * 60:
            return False, f"next run in {int((EVERY_MIN * 60 - (now - int(last['ts']))) // 60)} min"
        if resolved - int(last["resolved"] or 0) < MIN_NEW:
            return False, f"waits for {MIN_NEW} new resolved verdicts ({resolved - int(last['resolved'] or 0)} since the last run)"
    return True, "due"


def run(db, brain_summary=None, lab_summary=None, force=False):
    """One advisor pass: propose, backtest, adopt what passes, write it all down. Returns the run row or None."""
    ensure_tables(db)
    shadow.evaluate(db)
    ok, why = due(db)
    if not ok and not force:
        return None
    hist = history(db)
    if len(hist) < 2 * MIN_N and not force:
        return None
    retired = revalidate(db, hist)
    brain_summary = brain_summary or {}
    lab_summary = lab_summary or lab.summary(db)
    now = int(time.time())
    resolved = db.one("SELECT COUNT(*) n FROM outcomes WHERE resolved=1 AND outcome!='unknown'")["n"]
    tried = {json.dumps(json.loads(r["spec"]).get("conditions"), sort_keys=True)
             for r in db.q("SELECT spec FROM advisor_suggestions WHERE kind='rule'")}
    usage, note = {}, ""
    try:
        proposals, usage = ask(packet(db, hist, brain_summary, lab_summary))
        if proposals is None:
            used = {(c["metric"], c["op"]) for r in db.q("SELECT spec FROM learned_rules WHERE active=1")
                    for c in json.loads(r["spec"])["conditions"]}
            proposals = stub_propose(hist, tried, used)
    except Exception as e:
        proposals, note = {"rules": [], "arms": [], "notes": ""}, "writer unavailable; see private logs"
        log.warning("advisor writer failed: %s", e)
    if proposals.get("unparseable"):
        note = "the writer's answer was not the JSON form"
    tin, tout = _tokens(usage)
    db.x("INSERT INTO advisor_runs(ts,model,tokens_in,tokens_out,resolved,proposed,adopted,note) VALUES(?,?,?,?,?,0,0,?)",
         (now, MODEL, tin, tout, resolved, note))
    run_id = db.one("SELECT MAX(id) id FROM advisor_runs")["id"]
    taken = taken_sets(db, hist)
    proposed = adopted = 0
    rules = proposals.get("rules") if isinstance(proposals.get("rules"), list) else []
    arms = proposals.get("arms") if isinstance(proposals.get("arms"), list) else []
    active_rules = db.one("SELECT COUNT(*) n FROM learned_rules WHERE active=1")["n"]
    active_arms = db.one("SELECT COUNT(*) n FROM learned_arms WHERE active=1")["n"]
    for raw in rules[:MAX_PROPOSALS]:
        proposed += 1
        why_text = sanitize_why(raw.get("why") if isinstance(raw, dict) else "")
        spec, err = validate_rule(raw)
        if err:
            status, reason, bt, spec_out = "rejected", f"form: {err}", {}, raw if isinstance(raw, dict) else {"raw": str(raw)[:200]}
        elif json.dumps(spec["conditions"], sort_keys=True) in tried:
            status, reason, bt, spec_out = "rejected", "tried before", {}, spec
        elif active_rules >= MAX_RULES:
            status, reason, bt, spec_out = "rejected", f"{MAX_RULES} learned rules are already in use", {}, spec
        else:
            bt = backtest_rule(hist, spec, spec["points"], taken)
            spec_out = spec
            if bt["accepted"]:
                sid = shadow.stage(db, spec, run_id, hist)
                if sid is None:
                    status, reason = 'rejected', 'prospective evaluation capacity reached'
                else:
                    taken[f'shadow_{sid}'] = (spec['points'] > 0, {h['token'] for h in hist if matches(spec,h['m'])})
                    status, reason = 'shadow', f'{sid}: historical screen passed; waiting for a fixed future cohort'
            else:
                status, reason = "rejected", bt["why"]
        tried.add(json.dumps(spec_out.get("conditions"), sort_keys=True))
        db.x("INSERT INTO advisor_suggestions(run_id,ts,kind,spec,why,backtest,status,reason,model) VALUES(?,?,?,?,?,?,?,?,?)",
             (run_id, now, "rule", json.dumps(spec_out), why_text, json.dumps(bt), status, reason, MODEL))
    for raw in arms[:MAX_PROPOSALS]:
        proposed += 1
        why_text = sanitize_why(raw.get("why") if isinstance(raw, dict) else "")
        name, policy, delay, err = validate_arm(raw)
        bt = {}
        if err:
            status, reason, spec_out = "rejected", f"form: {err}", raw if isinstance(raw, dict) else {"raw": str(raw)[:200]}
        elif any(_same_policy(policy, p) for p in list(lab.POLICIES.values()) + list(lab.LEARNED.values())):
            status, reason, spec_out = "rejected", "the lab already runs this policy", {"name": name, **policy}
        elif db.one("SELECT 1 FROM learned_arms WHERE name=?", (name,)):
            status, reason, spec_out = "rejected", "an arm with this name exists", {"name": name, **policy}
        elif active_arms >= MAX_ARMS:
            status, reason, spec_out = "rejected", f"{MAX_ARMS} learned arms are already in the lab", {"name": name, **policy}
        else:
            bt = backtest_arm(db, name, policy, delay)
            spec_out = {"name": name, "delay_min": delay // 60, **policy}
            if bt.get("n", 0) >= 10 and bt["mean_ret"] < bt["default_mean_ret"]:
                status, reason = "rejected", "worse than the default on the resolved cases: " + bt["why"]
            else:
                adopt_arm(db, name, policy, delay, why_text, bt, run_id)
                active_arms += 1
                adopted += 1
                status, reason = "adopted", f"joined the lab as {name}@{delay // 60}m: {bt['why']}"
        db.x("INSERT INTO advisor_suggestions(run_id,ts,kind,spec,why,backtest,status,reason,model) VALUES(?,?,?,?,?,?,?,?,?)",
             (run_id, now, "arm", json.dumps(spec_out, default=list), why_text, json.dumps(bt), status, reason, MODEL))
    notes = sanitize_why(proposals.get("notes"))
    db.x("UPDATE advisor_runs SET proposed=?, adopted=?, note=? WHERE id=?",
         (proposed, adopted, (note + ("; " if note and notes else "") + notes)[:300], run_id))
    db.add_event("advisor", f"advisor ({MODEL}): {proposed} proposals, {adopted} adopted"
                 + (f", retired {', '.join(retired)}" if retired else "") + (f" · {note}" if note else ""))
    return db.one("SELECT * FROM advisor_runs WHERE id=?", (run_id,))


def summary(db):
    ensure_tables(db)
    ok, why = due(db)
    last = db.one("SELECT * FROM advisor_runs ORDER BY id DESC LIMIT 1")
    sug = db.q("SELECT * FROM advisor_suggestions ORDER BY id DESC LIMIT 12")
    for s in sug:
        for k in ("spec", "backtest"):
            try:
                s[k] = json.loads(s[k] or "{}")
            except ValueError:
                s[k] = {}
    rules = db.q("SELECT * FROM learned_rules ORDER BY created DESC")
    weights = {r["id"]: r["weight"] for r in db.q("SELECT id, weight FROM rules WHERE id LIKE 'ai_%'")}
    for r in rules:
        for k in ("spec", "backtest"):
            try:
                r[k] = json.loads(r[k] or "{}")
            except ValueError:
                r[k] = {}
        r["weight"] = weights.get(r["id"], 1.0)
    arms = db.q("SELECT * FROM learned_arms ORDER BY created DESC")
    for a in arms:
        for k in ("policy", "backtest"):
            try:
                a[k] = json.loads(a[k] or "{}")
            except ValueError:
                a[k] = {}
    return {"model": MODEL, "every_min": EVERY_MIN, "min_new": MIN_NEW, "due": ok, "state": why, "last_run": last,
            "runs": db.one("SELECT COUNT(*) n FROM advisor_runs")["n"], "suggestions": sug, "rules": rules, "arms": arms, "shadow": shadow.summary(db),
            "bar": {"min_n": MIN_N, "min_sep": MIN_SEP, "max_rules": MAX_RULES, "max_arms": MAX_ARMS}}
