"""The brain: links every verdict to what the token did afterwards and re-weights the rules.

Bounded and transparent on purpose. A rule that warned about a token that later rugged gains 2%
weight; one that reassured about a rug loses 2%. Weights stay between 0.5x and 1.5x, and every
lesson is written to the activity log so the site can show what it learned and why."""
import json
import logging
import time

from . import config as C
from .prices import token_prices
from .scorer import RULES

log = logging.getLogger("wormhole.brain")

CHECKPOINTS = (3600, 6 * 3600, 24 * 3600)
STEP = 0.02
LO, HI = 0.5, 1.5


class Brain:
    def __init__(self, db):
        self.db = db
        now = int(time.time())
        self.db.many("INSERT OR IGNORE INTO rules(id,weight,hits,misses,updated) VALUES(?,?,?,?,?)",
                     [(r, 1.0, 0, 0, now) for r in RULES])

    def weights(self):
        return {r["id"]: float(r["weight"]) for r in self.db.q("SELECT id, weight FROM rules")}

    def record(self, token, result):
        price0 = (result.get("metrics") or {}).get("price_usd")
        self.db.x("INSERT OR REPLACE INTO outcomes(token,score,verdict,scored_at,price0,checks,outcome,change_pct,"
                  "resolved,fired) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (token, result["score"], result["verdict"], int(time.time()), price0, json.dumps({}), "pending",
                   None, 0, json.dumps(result.get("fired") or [])))

    def check(self):
        """Advance every pending outcome whose next checkpoint is due."""
        now = int(time.time())
        pending = self.db.q("SELECT * FROM outcomes WHERE resolved=0")
        if not pending:
            return
        due = [o for o in pending if any(now - o["scored_at"] >= cp and str(cp) not in json.loads(o["checks"] or "{}")
                                         for cp in CHECKPOINTS) or o["price0"] is None]
        if not due:
            return
        prices = token_prices([o["token"] for o in due])
        for o in due:
            p = (prices.get(o["token"]) or {}).get("price_usd")
            checks = json.loads(o["checks"] or "{}")
            age = now - o["scored_at"]
            if o["price0"] is None and p and age < 1800:
                self.db.x("UPDATE outcomes SET price0=? WHERE token=?", (p, o["token"]))
                o["price0"] = p
            for cp in CHECKPOINTS:
                if age >= cp and str(cp) not in checks:
                    checks[str(cp)] = {"price": p, "ts": now}
            change = ((p / o["price0"]) - 1) * 100 if (p and o["price0"]) else None
            self.db.x("UPDATE outcomes SET checks=?, change_pct=? WHERE token=?",
                      (json.dumps(checks), change, o["token"]))
            if change is not None and change <= -80:
                self._resolve(o, "rugged", change)
            elif age >= CHECKPOINTS[-1]:
                if change is None:
                    self._resolve(o, "unknown", None)
                elif change <= -50:
                    self._resolve(o, "dumped", change)
                elif change < 50:
                    self._resolve(o, "flat", change)
                else:
                    self._resolve(o, "grew", change)

    def _resolve(self, o, outcome, change):
        bad, good = outcome in ("rugged", "dumped"), outcome == "grew"
        fired = json.loads(o["fired"] or "[]")
        now = int(time.time())
        moved = []
        if bad or good:
            for f in fired:
                pts = f.get("points", 0)
                if pts == 0:
                    continue
                hit = (pts < 0 and bad) or (pts > 0 and good)
                r = self.db.one("SELECT * FROM rules WHERE id=?", (f["rule"],))
                if not r:
                    continue
                w = float(r["weight"])
                w = min(HI, w + STEP) if hit else max(LO, w - STEP)
                self.db.x("UPDATE rules SET weight=?, hits=hits+?, misses=misses+?, updated=? WHERE id=?",
                          (w, 1 if hit else 0, 0 if hit else 1, now, f["rule"]))
                moved.append(f"{f['rule']} {'+' if hit else '-'}")
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
        for r in rules:
            r["about"] = RULES.get(r["id"], "")
            n = r["hits"] + r["misses"]
            r["hit_rate"] = round(100.0 * r["hits"] / n) if n else None
        res = self.db.q("SELECT o.*, l.symbol, l.name FROM outcomes o LEFT JOIN launches l ON l.token=o.token"
                        " ORDER BY o.scored_at DESC LIMIT 200")
        counts = {}
        for o in res:
            counts[o["outcome"]] = counts.get(o["outcome"], 0) + 1
        card = {}
        for r in self.db.q("SELECT verdict, outcome, COUNT(*) n FROM outcomes WHERE resolved=1 GROUP BY verdict, outcome"):
            card.setdefault(r["verdict"], {})[r["outcome"]] = r["n"]
        return {"rules": rules, "outcomes": res[:40], "counts": counts, "scorecard": card,
                "tracked": len(res), "resolved": sum(1 for o in res if o["resolved"])}


def creator_trust(db, deployer):
    """A 0-100 trust value for a creator wallet from what we have seen it do."""
    r = db.one("SELECT COUNT(*) n, COALESCE(SUM(graduated),0) g FROM launches WHERE deployer=?", (deployer,))
    o = db.q("SELECT o.outcome FROM outcomes o JOIN launches l ON l.token=o.token WHERE l.deployer=?", (deployer,))
    n, g = int(r["n"]), int(r["g"])
    rug = sum(1 for x in o if x["outcome"] in ("rugged", "dumped"))
    grew = sum(1 for x in o if x["outcome"] == "grew")
    t = 50 - min(30, 3 * max(0, n - 1)) + min(24, 8 * g) - 20 * rug + min(20, 10 * grew)
    return max(0, min(100, int(t))), {"launches": n, "grads": g, "rugged": rug, "grew": grew}
