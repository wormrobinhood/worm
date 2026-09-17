"""A paper book: what the worm would have bought, marked to market, exits chosen by the strategy lab.
No real money. Every position records which arm it runs and the token's own per-side cost (creator
tax + curve fee + slippage) so the book and the lab agree on what a trade would have cost."""
import copy
import json
import logging
import threading
import time

from . import config as C
from . import lab
from . import trade_checks as execution, trade_risk
from .prices import token_prices, usable_price

log = logging.getLogger("wormhole.paper")
COST = lab.FEE          # per side, for positions opened before costs were stored per token


class Paper:
    def __init__(self, db, rpc=None):
        self.db = db
        self.rpc = rpc
        self._lock = threading.Lock()        # consider() runs from the scoring worker and the marker at once
        for col in ("qty_left REAL", "recovered_usd REAL DEFAULT 0", "peak_usd REAL", "realized_usd REAL DEFAULT 0",
                    "policy TEXT", "tp_done TEXT", "trail_on INTEGER DEFAULT 0", "cost REAL",
                    "execution_model TEXT", "pool_key TEXT", "policy_spec TEXT", "gas_usd REAL DEFAULT 0",
                    "liquidation_usd REAL", "marked_ts INTEGER"):
            try:
                db.x(f"ALTER TABLE paper ADD COLUMN {col}")
            except Exception:
                pass
        # one row per token: drop duplicates from the old race (keep the first), then enforce it
        db.x("DELETE FROM paper WHERE id NOT IN (SELECT MIN(id) FROM paper GROUP BY token)")
        db.x("CREATE UNIQUE INDEX IF NOT EXISTS paper_token ON paper(token)")
        db.x("CREATE TABLE IF NOT EXISTS intents(token TEXT PRIMARY KEY, arm TEXT, ts INTEGER, scored_at INTEGER, score INTEGER)")

    def consider(self, token, result):
        with self._lock:
            self._consider(token, result)

    def _consider(self, token, result):
        if C.TOKEN and token.lower() == C.TOKEN:       # never its own token, on paper or live
            return
        if not execution.eligible(token, result):
            return
        if self.db.one("SELECT 1 FROM paper WHERE token=?", (token,)):
            return
        if not trade_risk.check(self.db, 'paper')['allowed']:
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
        try:
            quote = execution.entry(self.rpc, self.db, token, C.PAPER_SIZE_USD)
        except Exception:
            # No fabricated fill when the pool, reference price, liquidity or gas cannot be checked.
            self.db.add_event("paper", f"paper entry for ${sym} deferred: execution checks unavailable or outside limits", token)
            return
        price = quote['price']
        cost = lab.token_cost(self.db, token)
        qty = quote['minimum_raw'] / 1e18
        with self.db.transaction():
            if self.db.one("SELECT 1 FROM paper WHERE token=?", (token,)):
                return
            self.db.x("INSERT INTO paper(token,symbol,opened_ts,entry_usd,size_usd,qty,status,last_usd,qty_left,peak_usd,realized_usd,policy,tp_done,trail_on,cost,execution_model,pool_key,policy_spec,gas_usd,liquidation_usd,marked_ts)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (token, sym, int(time.time()), price, C.PAPER_SIZE_USD, qty, "open", price, qty, price,
                       -quote['gas_usd'], it['arm'], '[]', 0, cost, execution.MODEL, json.dumps(quote['pool']),
                       json.dumps(policy), quote['gas_usd'], quote['liquidation_usd'], int(time.time())))
        self.db.add_event("paper", f"paper buy: ${C.PAPER_SIZE_USD:.0f} of ${sym} at ${price:.6g} (score {result['score']}, arm {it['arm']}, cost {cost * 100:.1f}%/side research estimate; paper fill uses conservative pool quotes and gas)", token)

    def retry_pending(self):
        """Delayed arms and tokens that had no price at verdict time."""
        rows = self.db.q("SELECT s.token, s.score, s.verdict, s.metrics FROM scores s WHERE s.score>=? AND s.scored_at>=? AND s.partial=0"
                         " AND s.verdict='looks healthy' AND s.token NOT IN (SELECT token FROM paper)", (execution.MIN_SCORE, int(time.time()) - 3 * 3600))
        for r in rows:
            try:
                m = json.loads(r["metrics"] or "{}")
            except ValueError:
                m = {}
            self.consider(r["token"], {"score": r["score"], "verdict": r["verdict"], "metrics": m})

    def mark(self):
        with self._lock:
            self._mark()

    def _mark(self):
        opens = self.db.q("SELECT * FROM paper WHERE status='open'")
        if not opens:
            return
        prices = token_prices([p["token"] for p in opens])
        now = int(time.time())
        for p in opens:
            px = usable_price(prices.get(p["token"]))
            if not px or not p["entry_usd"] or not p["qty"]:
                continue
            cost = p["cost"] if p.get("cost") is not None else COST
            try:
                policy = json.loads(p['policy_spec']) if p.get('policy_spec') else lab.parse_arm(p["policy"] or lab.DEFAULT)[0]
            except KeyError:                       # an arm that no longer exists: exit by the default rule, never stall the book
                policy, _ = lab.parse_arm(lab.DEFAULT)
            st = {"entry": p["entry_usd"], "entry_ts": p["opened_ts"], "qty_left": (p["qty_left"] if p["qty_left"] is not None else p["qty"]) / p["qty"],
                  "tp_done": json.loads(p["tp_done"] or "[]"), "peak": max(p["peak_usd"] or 0, px), "trail_on": bool(p["trail_on"])}
            realized = p["realized_usd"] or 0.0
            last_why = None
            gas_usd = p.get('gas_usd') or 0
            liquidation, marked = None, None
            # A current bid is needed to value a quoted position, even when no exit is triggered.
            if p.get('execution_model') == execution.MODEL:
                try:
                    bid = execution.exit_quote(self.rpc, json.loads(p['pool_key']), p['token'], int(st['qty_left'] * p['qty'] * 1e18))
                    liquidation, marked = bid['minimum_raw'] / 1e6 - bid['gas_usd'], now
                except Exception:
                    continue
            while st["qty_left"] > 1e-9:
                checkpoint = copy.deepcopy(st)
                frac, why = lab.exit_step(policy, st, px, now)
                if frac <= 0:
                    break
                if p.get('execution_model') == execution.MODEL:
                    try:
                        bid = execution.exit_quote(self.rpc, json.loads(p['pool_key']), p['token'], int(frac * p['qty'] * 1e18))
                    except Exception:
                        # Roll back only the unfilled step, retaining any earlier quoted fills.
                        st = checkpoint
                        break
                    usd = bid['minimum_raw'] / 1e6 - bid['gas_usd']
                    gas_usd += bid['gas_usd']
                else:
                    usd = frac * p["qty"] * px * (1 - cost)
                realized += usd
                st["qty_left"] -= frac
                last_why = why
                self.db.add_event("paper", f"paper sell: {frac * 100:.0f}% of ${p['symbol']} at {px / p['entry_usd']:.2f}x ({why}), +${usd:.2f}", p["token"])
            qty_left = max(0.0, st["qty_left"]) * p["qty"]
            closed = st["qty_left"] <= 1e-9
            pnl = realized - p["size_usd"] if closed else None
            if p.get('execution_model') == execution.MODEL:
                if closed:
                    liquidation, marked = 0.0, now
                elif last_why:
                    liquidation, marked = None, None
                    try:
                        bid = execution.exit_quote(self.rpc, json.loads(p['pool_key']), p['token'], int(qty_left * 1e18))
                        liquidation, marked = bid['minimum_raw'] / 1e6 - bid['gas_usd'], now
                    except Exception:
                        pass
            with self.db.transaction():
                self.db.x("UPDATE paper SET last_usd=?, peak_usd=?, qty_left=?, tp_done=?, trail_on=?, realized_usd=?, recovered_usd=?,"
                          " status=?, closed_ts=?, exit_usd=?, pnl_usd=?, reason=? WHERE id=?",
                          (px, st["peak"], qty_left, json.dumps(st["tp_done"]), 1 if st["trail_on"] else 0, realized,
                           realized if st["tp_done"] else 0, "closed" if closed else "open", now if closed else None,
                           px if closed else None, pnl, last_why if closed else None, p["id"]))
                self.db.x("UPDATE paper SET gas_usd=?,liquidation_usd=?,marked_ts=? WHERE id=?", (gas_usd, liquidation, marked, p['id']))
            if closed:
                self.db.add_event("paper", f"paper close: ${p['symbol']} pnl ${pnl:+.2f} ({last_why}, arm {p['policy']})", p["token"])

    def summary(self):
        opens = self.db.q("SELECT * FROM paper WHERE status='open' ORDER BY opened_ts DESC")
        closed = self.db.q("SELECT * FROM paper WHERE status='closed' ORDER BY closed_ts DESC LIMIT 30")
        unreal = 0.0
        for p in opens:
            qty_left = p["qty_left"] if p["qty_left"] is not None else p["qty"]
            cost = p["cost"] if p.get("cost") is not None else COST
            value = (p.get('liquidation_usd') if p.get('execution_model') == execution.MODEL
                     else qty_left * (p["last_usd"] or p["entry_usd"]) * (1 - cost))
            p['valuation_stale'] = p.get('execution_model') == execution.MODEL and (not p.get('marked_ts') or time.time() - p['marked_ts'] > 600)
            p['valuation_stale'] = p['valuation_stale'] or value is None
            p["hedged"] = bool(json.loads(p["tp_done"] or "[]"))
            p["bag_pct"] = round(100 * qty_left / p["qty"]) if p["qty"] else 0
            p["change_pct"] = ((p["last_usd"] / p["entry_usd"]) - 1) * 100 if p.get("last_usd") else 0.0
            p["pnl_usd"] = None if p["valuation_stale"] else (p["realized_usd"] or 0) + value - p["size_usd"]
            unreal += p["pnl_usd"] or 0
        for p in closed:
            p["change_pct"] = ((p["exit_usd"] / p["entry_usd"]) - 1) * 100 if p.get("exit_usd") else 0.0
        tot = self.db.one("SELECT COALESCE(SUM(pnl_usd),0) s, COALESCE(SUM(pnl_usd>0),0) w, COUNT(*) n FROM paper WHERE status='closed'")
        cur, why = lab.current_policy(self.db)
        return {"open": opens, "closed": closed, "realized_usd": round(tot["s"], 2), "unrealized_usd": round(unreal, 2),
                "closed_count": tot["n"], "win_rate": round(100.0 * tot["w"] / tot["n"]) if tot["n"] else None,
                "size_usd": C.PAPER_SIZE_USD, "min_score": execution.MIN_SCORE,
                "unpriced_count": sum(p["valuation_stale"] for p in opens),
                "risk": trade_risk.check(self.db, 'paper', latch=False), "execution_model": execution.MODEL,
                "rules": f"exits by the strategy lab, in use: {cur} ({why}); new entries need healthy complete scores, USDG pools and round-trip quotes; gas and 3% quote tolerance included; legacy rows retain their original cost model"}
