"""The brain: links every verdict to what the token did afterwards and re-weights the rules.

Bounded and transparent on purpose. When a token goes bad or grows, every rule that fired on it moves
by how surprising that outcome was against the base rate: a rule that warned before a token that rugged
more often than usual gains, one that reassured loses, and the reverse when the token grew. A rule that
warns on everything learns nothing, because its tokens rug at the base rate. Weights stay between 0.5x
and 1.5x, and every lesson is written to the activity log so the site can show what it learned and why."""
import json
import logging
import math
import statistics
import time

from . import advisor as ADV
from .prices import token_prices, PRICE0_WINDOW_S, usable_price, observed_at
from .scorer import RULES

log = logging.getLogger("wormhole.brain")

CHECKPOINTS = (3600, 6 * 3600, 24 * 3600)
STEP = 0.02
LO, HI = 0.5, 1.5
STAMP_WINDOW_S = 1800          # a checkpoint is stamped only within 30 min of its due time, else marked skipped
GRACE_S = 6 * 3600             # after the last checkpoint: how long to keep waiting for a price before "unknown"
RUG_CONFIRM_S = 300            # a rug needs two readings at least this far apart
MAX_PRICE_AGE_S = 900          # a cached price older than this is not a reading
RUG_PCT, DUMP_PCT, GREW_PCT = -80.0, -50.0, 100.0
BAD, GOOD = ("rugged", "dumped"), ("grew",)
LIFT_MIN_N = 5                 # warnings a rule must have given before its lift is shown


def _price_of(entry):
    """A usable current price from a token_prices entry; None when missing or stale."""
    return usable_price(entry)


def _checks(o):
    try:
        c = json.loads(o.get("checks") or "{}")
        return c if isinstance(c, dict) else {}
    except ValueError:
        return {}


def _label(change, at_24h):
    """Outcome label for a return at (or after) the last checkpoint."""
    if change <= RUG_PCT:
        return "rugged"
    if change <= DUMP_PCT:
        return "dumped"
    if change >= GREW_PCT:
        return "grew"
    return "flat"


class Brain:
    def __init__(self, db):
        self.db = db
        now = int(time.time())
        for col in ("fired_bad INTEGER DEFAULT 0", "fired_good INTEGER DEFAULT 0"):
            try:
                self.db.x(f"ALTER TABLE rules ADD COLUMN {col}")
            except Exception:
                pass
        self.db.many("INSERT OR IGNORE INTO rules(id,weight,hits,misses,updated) VALUES(?,?,?,?,?)",
                     [(r, 1.0, 0, 0, now) for r in RULES])

    def weights(self):
        return {r["id"]: float(r["weight"]) for r in self.db.q("SELECT id, weight FROM rules")}

    def record(self, token, result):
        """Keep the first prediction immutable. Rescans are separate assessments, not revised history."""
        with self.db.transaction():
            if self.db.one("SELECT 1 FROM outcomes WHERE token=?", (token,)):
                return
            # If the process died after persisting a score but before recording its outcome,
            # recover the earliest saved assessment instead of evaluating a later retry.
            saved = self.db.one("SELECT * FROM assessments WHERE token=? ORDER BY id LIMIT 1", (token,))
            if saved:
                result = dict(saved, assessment_id=saved['id'], metrics=json.loads(saved['metrics'] or '{}'),
                              fired=json.loads(saved['fired'] or '[]'), reasons=json.loads(saved['reasons'] or '[]'))
            now = int(result.get('scored_at') or time.time())
            fired = json.dumps(result.get('fired') or [])
            metrics = result.get('metrics') or {}
            assessment_id = result.get('assessment_id')
            if not assessment_id:
                assessment_id = self.db.insert(
                    "INSERT INTO assessments(token,score,verdict,reasons,metrics,scored_at,partial,fired,engine_version)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (token, result['score'], result['verdict'], json.dumps(result.get('reasons') or []),
                     json.dumps(metrics), now, int(bool(metrics.get('partial'))), fired, 'evidence-v2'))
            price0 = usable_price(metrics)
            baseline_ts = now if price0 is not None else None
            if metrics.get('price_ts') is not None:
                baseline_ts = int(metrics['price_ts']) if price0 is not None else None
                if baseline_ts is not None and (baseline_ts < now or baseline_ts > time.time()):
                    price0, baseline_ts = None, None
            self.db.x("INSERT OR IGNORE INTO outcomes(token,score,verdict,scored_at,price0,checks,outcome,change_pct,"
                      "resolved,fired,assessment_id,baseline_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (token, result['score'], result['verdict'], now, price0, '{}', 'pending', None, 0, fired,
                       assessment_id, baseline_ts))

    # ---- checking outcomes ---------------------------------------------------------------------

    def _due(self, o, now):
        # Monitoring is continuous at the marker cadence; checkpoints remain separately timestamped.
        return not o.get('resolved')

    def check(self):
        """Advance every pending outcome that has something to read."""
        now = int(time.time())
        pending = self.db.q("SELECT * FROM outcomes WHERE resolved=0")
        due = [o for o in pending if self._due(o, now)]
        if not due:
            return
        prices = token_prices([o["token"] for o in due])
        now = int(time.time())
        for o in due:
            try:
                entry = prices.get(o["token"]) or {}
                self._advance(o, _price_of(entry), now, observed_at(entry, now))
            except Exception as e:
                log.warning("check %s failed: %s", o["token"][:10], e)

    def _advance(self, o, p, now, price_ts=None):
        with self.db.transaction():
            current = self.db.one("SELECT * FROM outcomes WHERE token=? AND resolved=0", (o['token'],))
            if current:
                self._advance_once(current, p, now, price_ts)

    def _advance_once(self, o, p, now, price_ts=None):
        price_ts = now if price_ts is None else price_ts
        if price_ts < int(o['scored_at']) or price_ts > now:
            p = None

        age = now - int(o["scored_at"])
        checks = _checks(o)
        if o["price0"] is None and p is not None and age < PRICE0_WINDOW_S:
            self.db.x("UPDATE outcomes SET price0=?, baseline_ts=? WHERE token=? AND price0 IS NULL", (p, price_ts, o["token"]))
            o["price0"] = p
            checks["baseline"] = {"price": p, "ts": price_ts, "age_s": price_ts - int(o["scored_at"])}
        price0 = o["price0"]
        change = ((p / price0) - 1) * 100 if (p is not None and price0) else None
        for cp in CHECKPOINTS:
            key = str(cp)
            if age >= cp and key not in checks:
                if age - cp >= STAMP_WINDOW_S:
                    checks[key] = {"skipped": True, "ts": now}       # too late to call this reading the checkpoint
                elif p is not None and price_ts >= int(o["scored_at"]) + cp:
                    checks[key] = {"price": p, "ts": price_ts, "change_pct": round(change, 2) if change is not None else None}
        outcome = None
        if change is not None:
            if change <= RUG_PCT:
                seen = checks.get("rug_seen")
                if (int(o["scored_at"]) + CHECKPOINTS[-1] <= price_ts < int(o["scored_at"]) + CHECKPOINTS[-1] + STAMP_WINDOW_S) or (seen and price_ts - int(seen.get("ts", price_ts)) >= RUG_CONFIRM_S):
                    outcome = "rugged"
                elif not seen:
                    checks["rug_seen"] = {"ts": price_ts, "price": p, "change_pct": round(change, 2)}
            else:
                checks.pop("rug_seen", None)
        if outcome is None and age >= CHECKPOINTS[-1]:
            # A final return must actually be observed inside the final checkpoint window.
            final = checks.get(str(CHECKPOINTS[-1])) or {}
            final_change = final.get('change_pct')
            if final_change is not None:
                change = final_change
                outcome = _label(change, True)
            elif age >= CHECKPOINTS[-1] + GRACE_S:
                change, outcome = None, 'unknown'
        self.db.x("UPDATE outcomes SET checks=?, change_pct=? WHERE token=? AND resolved=0",
                  (json.dumps(checks), change, o["token"]))
        if outcome:
            self._resolve(o, outcome, change)

    # ---- lessons -------------------------------------------------------------------------------

    def base_rate(self):
        """Share of bad among resolved bad+good outcomes so far; 0.5 until there are any."""
        r = self.db.one("SELECT SUM(CASE WHEN outcome IN ('rugged','dumped') THEN 1 ELSE 0 END) b,"
                        " SUM(CASE WHEN outcome='grew' THEN 1 ELSE 0 END) g FROM outcomes WHERE resolved=1")
        nb, ng = int((r or {}).get("b") or 0), int((r or {}).get("g") or 0)
        return (nb / (nb + ng)) if (nb + ng) else 0.5

    def _resolve(self, o, outcome, change):
        with self.db.transaction():
            current = self.db.one("SELECT * FROM outcomes WHERE token=? AND resolved=0", (o['token'],))
            if current:
                self._resolve_once(current, outcome, change)

    def _resolve_once(self, o, outcome, change):
        bad, good = outcome in BAD, outcome in GOOD
        try:
            fired = json.loads(o["fired"] or "[]")
        except ValueError:
            fired = []
        now = int(time.time())
        moved = []
        if bad or good:
            base = self.base_rate()
            signal = (1.0 if bad else 0.0) - base            # > 0: worse than typical, < 0: better
            for f in fired:
                pts = f.get("points", 0) or 0
                if not pts:
                    continue
                r = self.db.one("SELECT * FROM rules WHERE id=?", (f["rule"],))
                if not r:
                    continue
                delta = 2 * STEP * signal * (1 if pts < 0 else -1)
                w = min(HI, max(LO, float(r["weight"]) + delta))
                self.db.x("UPDATE rules SET weight=?, hits=hits+?, misses=misses+?, fired_bad=fired_bad+?,"
                          " fired_good=fired_good+?, updated=? WHERE id=?",
                          (w, 1 if delta > 0 else 0, 1 if delta < 0 else 0,
                           1 if (pts < 0 and bad) else 0, 1 if (pts < 0 and good) else 0, now, f["rule"]))
                if delta:
                    moved.append(f"{f['rule']} {'+' if delta > 0 else '-'}")
        self.db.x("UPDATE outcomes SET outcome=?, change_pct=?, resolved=1 WHERE token=?", (outcome, change, o["token"]))
        lab = self.db.one("SELECT name, symbol FROM launches WHERE token=?", (o["token"],)) or {}
        name = f"${lab.get('symbol')}" if lab.get("symbol") else o["token"][:10]
        chg = f"{change:+.0f}%" if change is not None else "no price"
        called = ((o["verdict"] == "avoid" and bad) or (o["verdict"] == "looks healthy" and good))
        missed = ((o["verdict"] == "looks healthy" and bad) or (o["verdict"] == "avoid" and good))
        tag = "called it" if called else ("missed it" if missed else "no lesson")
        self.db.add_event("lesson", f"{name}: {outcome} ({chg}) after verdict '{o['verdict']}' [{o['score']}]: {tag}"
                          + (f"; nudged {', '.join(moved)}" if moved else ""), o["token"])
        log.info("resolved %s %s %s", name, outcome, chg)

    def summary(self):
        rules = self.db.q("SELECT * FROM rules ORDER BY id")
        base = self.base_rate()
        for r in rules:
            r["about"] = RULES.get(r["id"]) or (ADV.describe(self.db, r["id"]) if str(r["id"]).startswith("ai_") else "")
            r["learned"] = str(r["id"]).startswith("ai_")
            n = (r.get("hits") or 0) + (r.get("misses") or 0)
            r["hit_rate"] = round(100.0 * r["hits"] / n) if n else None
            fb, fg = int(r.get("fired_bad") or 0), int(r.get("fired_good") or 0)
            # lift: how much more often a token went bad when this rule warned, against the base rate
            r["lift"] = round((fb / (fb + fg)) / base, 2) if (fb + fg >= LIFT_MIN_N and base > 0) else None
        counts = {row["outcome"]: row["n"] for row in self.db.q("SELECT outcome, COUNT(*) n FROM outcomes GROUP BY outcome")}
        tracked = sum(counts.values())
        resolved = self.db.one("SELECT COUNT(*) n FROM outcomes WHERE resolved=1")["n"]
        res = self.db.q("SELECT o.*, l.symbol, l.name FROM outcomes o LEFT JOIN launches l ON l.token=o.token"
                        " ORDER BY o.scored_at DESC LIMIT 40")
        card = {}
        for r in self.db.q("SELECT verdict, outcome, COUNT(*) n FROM outcomes WHERE resolved=1 GROUP BY verdict, outcome"):
            card.setdefault(r["verdict"], {})[r["outcome"]] = r["n"]
        returns = {}
        by_verdict = {}
        for r in self.db.q("SELECT verdict, checks, change_pct FROM outcomes WHERE resolved=1 AND outcome!='unknown'"):
            c = _checks(r).get(str(CHECKPOINTS[-1])) or {}
            v = c.get("change_pct") if c.get("change_pct") is not None else r["change_pct"]
            if v is not None:
                by_verdict.setdefault(r["verdict"], []).append(float(v))
        for v, xs in by_verdict.items():
            returns[v] = {"n": len(xs), "mean_pct": round(statistics.fmean(xs), 1), "median_pct": round(statistics.median(xs), 1)}
        validated_card = {}
        for r in self.db.q("SELECT o.verdict, o.outcome, COUNT(*) n FROM outcomes o JOIN assessments a ON a.id=o.assessment_id"
                          " WHERE o.resolved=1 AND a.partial=0 AND a.engine_version='evidence-v2'"
                          " AND o.baseline_ts IS NOT NULL GROUP BY o.verdict,o.outcome"):
            validated_card.setdefault(r['verdict'], {})[r['outcome']] = r['n']
        return {"rules": rules, "outcomes": res, "counts": counts, "scorecard": card, "validated_scorecard": validated_card, "returns": returns,
                "base_rate_pct": round(100 * base), "tracked": tracked, "resolved": resolved}


def creator_trust(db, deployer, exclude_token=None):
    """A 0-100 trust value for a creator wallet from what we have seen it do. Only resolved graduations
    that did not rug or dump earn credit; a token whose outcome could not be read costs a little.
    exclude_token leaves one token out, so a token's own record never counts as its creator's history."""
    if exclude_token:
        r = db.one("SELECT COUNT(*) n, COALESCE(SUM(graduated),0) g FROM launches WHERE deployer=? AND token!=?",
                   (deployer, exclude_token))
        o = db.q("SELECT o.outcome, o.resolved FROM outcomes o JOIN launches l ON l.token=o.token"
                 " WHERE l.deployer=? AND o.token!=?", (deployer, exclude_token))
    else:
        r = db.one("SELECT COUNT(*) n, COALESCE(SUM(graduated),0) g FROM launches WHERE deployer=?", (deployer,))
        o = db.q("SELECT o.outcome, o.resolved FROM outcomes o JOIN launches l ON l.token=o.token WHERE l.deployer=?",
                 (deployer,))
    n, g = int(r["n"]), int(r["g"])
    rug = sum(1 for x in o if x["outcome"] in BAD)
    grew = sum(1 for x in o if x["outcome"] in GOOD)
    unknown = sum(1 for x in o if x["outcome"] == "unknown")
    good = sum(1 for x in o if x["resolved"] and x["outcome"] in ("flat", "grew"))
    pending = sum(1 for x in o if not x["resolved"])
    penalty = min(40.0, 10.0 * math.log10(n)) if n > 1 else 0.0
    t = 50 - penalty + min(24, 8 * good) - 5 * unknown - 20 * rug + min(20, 10 * grew)
    meta = {"launches": n, "grads": g, "rugged": rug, "grew": grew, "good": good, "unknown": unknown, "pending": pending}
    return max(0, min(100, int(round(t)))), meta
