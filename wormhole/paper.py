"""A paper book: what the worm would have bought, marked to market, exits chosen by the strategy lab.
No real money. Every position records which arm it runs so the lab's choice can be judged later."""
import json
import logging
import time

from . import config as C
from . import lab
from .prices import token_prices

log = logging.getLogger("wormhole.paper")
COST = lab.FEE          # per side


class Paper:
    def __init__(self, db):
        self.db = db
        for col in ("qty_left REAL", "recovered_usd REAL DEFAULT 0", "peak_usd REAL", "realized_usd REAL DEFAULT 0",
                    "policy TEXT", "tp_done TEXT", "trail_on INTEGER DEFAULT 0"):
            try:
                db.x(f"ALTER TABLE paper ADD COLUMN {col}")
            except Exception:
                pass
        db.x("CREATE TABLE IF NOT EXISTS intents(token TEXT PRIMARY KEY, arm TEXT, ts INTEGER, scored_at INTEGER, score INTEGER)")

    def consider(self, token, result):
        if result["score"] < C.PAPER_MIN_SCORE or self.db.one("SELECT 1 FROM paper WHERE token=?", (token,)):
            return
        it = self.db.one("SELECT * FROM intents WHERE token=?", (token,))
        if not it:
            arm = lab.pick_arm(self.db)
            self.db.x("INSERT OR REPLACE INTO intents(token,arm,ts,scored_at,score) VALUES(?,?,?,?,?)",
                      (token, arm, int(time.time()), int(time.time()), result["score"]))
            it = {"arm": arm, "scored_at": int(time.time())}
        policy, delay = lab.parse_arm(it["arm"])
        if time.time() < it["scored_at"] + delay:
            return                                   # the arm waits; retry_pending opens it later
        lab_row = self.db.one("SELECT symbol FROM launches WHERE token=?", (token,)) or {}
        sym = lab_row.get("symbol") or token[:8]
        n_open = self.db.one("SELECT COUNT(*) n FROM paper WHERE status='open'")["n"]
        if n_open >= C.PAPER_MAX_OPEN:
            self.db.add_event("paper", f"would paper-buy ${sym} (score {result['score']}) but the book is full", token)
            return
        price = (token_prices([token]).get(token) or {}).get("price_usd") or (result.get("metrics") or {}).get("price_usd")
        if not price:
            self.db.add_event("paper", f"would paper-buy ${sym} (score {result['score']}) but it has no price yet", token)
            return
        qty = C.PAPER_SIZE_USD / price / (1 + COST)
        self.db.x("INSERT INTO paper(token,symbol,opened_ts,entry_usd,size_usd,qty,status,last_usd,qty_left,peak_usd,realized_usd,policy,tp_done,trail_on)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (token, sym, int(time.time()), price, C.PAPER_SIZE_USD, qty, "open", price, qty, price, 0.0, it["arm"], "[]", 0))
        self.db.add_event("paper", f"paper buy: ${C.PAPER_SIZE_USD:.0f} of ${sym} at ${price:.6g} (score {result['score']}, arm {it['arm']})", token)

    def retry_pending(self):
        """Delayed arms and tokens that had no price at verdict time."""
        rows = self.db.q("SELECT s.token, s.score, s.verdict, s.metrics FROM scores s WHERE s.score>=? AND s.scored_at>=?"
                         " AND s.token NOT IN (SELECT token FROM paper)", (C.PAPER_MIN_SCORE, int(time.time()) - 3 * 3600))
        for r in rows:
            try:
                m = json.loads(r["metrics"] or "{}")
            except ValueError:
                m = {}
            self.consider(r["token"], {"score": r["score"], "verdict": r["verdict"], "metrics": m})

    def mark(self):
        opens = self.db.q("SELECT * FROM paper WHERE status='open'")
        if not opens:
            return
        prices = token_prices([p["token"] for p in opens])
        now = int(time.time())
        for p in opens:
            px = (prices.get(p["token"]) or {}).get("price_usd")
            if not px:
                continue
            policy, _ = lab.parse_arm(p["policy"] or lab.DEFAULT)
            st = {"entry": p["entry_usd"], "entry_ts": p["opened_ts"], "qty_left": (p["qty_left"] if p["qty_left"] is not None else p["qty"]) / p["qty"],
                  "tp_done": json.loads(p["tp_done"] or "[]"), "peak": max(p["peak_usd"] or 0, px), "trail_on": bool(p["trail_on"])}
            realized = p["realized_usd"] or 0.0
            sold_any = False
            frac, why = lab.exit_step(policy, st, px, now)
            while frac > 0:
                usd = frac * p["qty"] * px * (1 - COST)
                realized += usd
                st["qty_left"] -= frac
                sold_any = True
                self.db.add_event("paper", f"paper sell: {frac * 100:.0f}% of ${p['symbol']} at {px / p['entry_usd']:.2f}x ({why}), +${usd:.2f}", p["token"])
                frac, why = lab.exit_step(policy, st, px, now) if st["qty_left"] > 1e-9 else (0, None)
            qty_left = max(0.0, st["qty_left"]) * p["qty"]
            closed = st["qty_left"] <= 1e-9
            pnl = realized - p["size_usd"] if closed else None
            self.db.x("UPDATE paper SET last_usd=?, peak_usd=?, qty_left=?, tp_done=?, trail_on=?, realized_usd=?, recovered_usd=?,"
                      " status=?, closed_ts=?, exit_usd=?, pnl_usd=?, reason=? WHERE id=?",
                      (px, st["peak"], qty_left, json.dumps(st["tp_done"]), 1 if st["trail_on"] else 0, realized,
                       realized if st["tp_done"] else 0, "closed" if closed else "open", now if closed else None,
                       px if closed else None, pnl, why if closed else None, p["id"]))
            if closed:
                self.db.add_event("paper", f"paper close: ${p['symbol']} pnl ${pnl:+.2f} ({why}, arm {p['policy']})", p["token"])

    def summary(self):
        opens = self.db.q("SELECT * FROM paper WHERE status='open' ORDER BY opened_ts DESC")
        closed = self.db.q("SELECT * FROM paper WHERE status='closed' ORDER BY closed_ts DESC LIMIT 30")
        unreal = 0.0
        for p in opens:
            qty_left = p["qty_left"] if p["qty_left"] is not None else p["qty"]
            value = qty_left * (p["last_usd"] or p["entry_usd"]) * (1 - COST)
            p["hedged"] = bool(json.loads(p["tp_done"] or "[]"))
            p["bag_pct"] = round(100 * qty_left / p["qty"]) if p["qty"] else 0
            p["change_pct"] = ((p["last_usd"] / p["entry_usd"]) - 1) * 100 if p.get("last_usd") else 0.0
            p["pnl_usd"] = (p["realized_usd"] or 0) + value - p["size_usd"]
            unreal += p["pnl_usd"]
        for p in closed:
            p["change_pct"] = ((p["exit_usd"] / p["entry_usd"]) - 1) * 100 if p.get("exit_usd") else 0.0
        realized = sum(p["pnl_usd"] or 0 for p in closed)
        wins = sum(1 for p in closed if (p["pnl_usd"] or 0) > 0)
        cur, why = lab.current_policy(self.db)
        return {"open": opens, "closed": closed, "realized_usd": round(realized, 2), "unrealized_usd": round(unreal, 2),
                "closed_count": len(closed), "win_rate": round(100.0 * wins / len(closed)) if closed else None,
                "size_usd": C.PAPER_SIZE_USD, "min_score": C.PAPER_MIN_SCORE,
                "rules": f"exits by the strategy lab, in use: {cur} ({why}); costs {COST * 2 * 100:.0f}% round trip included"}
