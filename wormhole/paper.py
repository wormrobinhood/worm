"""A paper book: what the worm would have bought, marked to market, exits chosen by the strategy lab.
No real money. Every position records which arm it runs and the token's own per-side cost (creator
tax + curve fee + slippage) so the book and the lab agree on what a trade would have cost."""
import copy
import json
import logging
import threading
import time

from . import config as C
from . import lab, poolstate, paper_research, route
from . import trade_checks as execution, trade_risk
from .prices import token_prices, usable_price

log = logging.getLogger("wormhole.paper")
COST = lab.FEE          # per side, for positions opened before costs were stored per token
VERDICT_ENTRY = "verdict-healthy-v1"     # the strategy label of rows opened at verdict time


def pair(row):
    """The asset the position's own pool trades against: 'USDG', 'ETH' or a stock's symbol ('META'), recorded at
    entry; for rows from before that, read from the pool key (USDG or ETH). None when unknown."""
    if row.get('pair'):
        return row['pair']
    try:
        quote = (json.loads(row.get('pool_key') or 'null') or {}).get('quote')
    except (ValueError, TypeError, AttributeError):
        return None
    return 'USDG' if quote == C.USDG else 'ETH' if quote == C.ZERO else None


def pair_symbol(db, token, pk):
    """What a pool trades against, named: 'USDG', 'ETH', or the launch's own pair symbol (a stock such as META)."""
    quote = (pk or {}).get('quote')
    if not quote:
        return None
    if quote in (C.USDG, C.ZERO):
        return 'USDG' if quote == C.USDG else 'ETH'
    return (db.one("SELECT pair_symbol FROM launches WHERE token=?", (token,)) or {}).get('pair_symbol') or quote[:10]


def live_comparable(row, strategy=True):
    """Could live have made this trade? Live buys any pair from USDG through a checked route (or the token's own
    USDG pool), only what a second-look rule bought (strategy), with quoted fills, and never while its loss
    breaker is paused. A position filled straight from an ETH pool while every aggregator was down stays in the
    book as learning data: live holds no ETH and could not have made that fill."""
    return (execution.live_fill(row) and bool(row.get('execution_model')) and not row.get('opened_in_pause')
            and (not strategy or (row.get('strategy') or VERDICT_ENTRY) != VERDICT_ENTRY))


def pause_windows(db, limit):
    """[(start, end)] when the paper loss breaker was (at least) paused, rebuilt from closed trades: from the close
    that took the day's realised losses to the limit until 24 hours after they fell back under it (check() re-arms
    the pause each time it sees the limit reached), plus the pause stored now. A lower bound: the breaker also
    counted open positions' marks, which were not kept."""
    events = []
    for r in db.q("SELECT closed_ts, pnl_usd FROM paper WHERE status='closed' AND closed_ts IS NOT NULL AND pnl_usd<0"):
        events += [(r['closed_ts'], -r['pnl_usd']), (r['closed_ts'] + 86400, r['pnl_usd'])]
    windows, total, start = [], 0.0, None
    for ts, delta in sorted(events):
        total += delta
        if start is None and total >= limit - 1e-9:
            start = ts
        elif start is not None and total < limit - 1e-9:
            windows.append((start, ts + 86400))
            start = None
    if start is not None:
        windows.append((start, 2 ** 62))
    until = int(db.meta_get('loss_pause_until_paper', '0') or 0)
    if until:
        windows.append((until - 86400, until))
    return windows


def entry_text():
    from . import watch
    looks = "/".join(str(m) for m in watch.all_looks())
    names = ", ".join(rule["name"] for rule in watch.STRATEGIES)
    return (f"nothing is bought at the verdict: every complete verdict is watched on-chain and judged again {looks} min "
            f"later; a token that passes an entry rule ({names}) is bought from USDG by the best checked route, whatever its pool is paired with")


class Paper:
    def __init__(self, db, rpc=None):
        self.db = db
        self.rpc = rpc
        self._lock = threading.Lock()        # serialize entries only; exits never wait for entry quotes
        self._position_guard = threading.Lock()
        self._position_locks = {}
        self.last_skip = None
        for col in ("qty_left REAL", "recovered_usd REAL DEFAULT 0", "peak_usd REAL", "realized_usd REAL DEFAULT 0",
                    "policy TEXT", "tp_done TEXT", "trail_on INTEGER DEFAULT 0", "cost REAL",
                    "execution_model TEXT", "pool_key TEXT", "policy_spec TEXT", "gas_usd REAL DEFAULT 0",
                    "liquidation_usd REAL", "marked_ts INTEGER", "strategy TEXT", "features TEXT",
                    "execution_spec TEXT", "observed_ts REAL", "monitor_ts INTEGER", "max_monitor_gap_s INTEGER DEFAULT 0",
                    "entry_shadow TEXT", "exit_trigger_ts INTEGER", "exit_quote_failures INTEGER DEFAULT 0", "exit_quote_failed_ts INTEGER",
                    "opened_in_pause INTEGER DEFAULT 0", "pair TEXT", "route_provider TEXT", "live_fill INTEGER",
                    "route TEXT", "exit_provider TEXT", "fallback_fills INTEGER DEFAULT 0"):
            try:
                db.x(f"ALTER TABLE paper ADD COLUMN {col}")
            except Exception:
                pass
        # A second-look candidate the loss breaker turned away: kept so the skips can be counted per rule and what
        # they would have done followed by the lab, instead of vanishing from the record.
        db.x("CREATE TABLE IF NOT EXISTS paper_skips(id INTEGER PRIMARY KEY, token TEXT, symbol TEXT, ts INTEGER, strategy TEXT,"
             " reason TEXT, pair TEXT, reference REAL, features TEXT, entry_shadow TEXT)")
        if db.meta_get('paper_pause_flags', '') != 'v1':
            # Once: second-look entries used to ignore the breaker (ROYALTY, DURR, XGAS.DEV opened during a pause).
            # Such rows stay in the book and its totals, flagged, and never stand in for what live would have done.
            with db.transaction():
                for start, end in pause_windows(db, trade_risk.loss_limit()):
                    db.x("UPDATE paper SET opened_in_pause=1 WHERE opened_ts>=? AND opened_ts<=?", (start, end))
                db.meta_set('paper_pause_flags', 'v1')
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
        self._open(token, lab_row.get("symbol") or token[:8], it["arm"], policy, f"score {result['score']}", VERDICT_ENTRY)

    def enter(self, token, symbol, reference, strategy, why, features=None, pool=None):
        """Open a position the second look chose (watch.py): no verdict gate, the same fills. `reference` is
        the pool's mid the watcher just read; `features` is what the look measured, kept on the row so the
        next round of research reads what production really saw. Returns True when a row was opened.

        The loss breaker applies here exactly as it would to live: every candidate answers to the breaker over the
        live-comparable positions (live can buy any pair by a route). A candidate it turns away is recorded
        in paper_skips and is no cohort member. That cannot pick winners: the pause is decided from trades that
        closed before the candidate existed, never from the candidate's own path, and live would have skipped it
        too. `last_skip` tells the watcher why."""
        self.last_skip = None
        with self._lock:
            if C.TOKEN and token.lower() == C.TOKEN:
                return False
            if self.db.one("SELECT 1 FROM paper WHERE token=?", (token,)):
                return False
            risk = trade_risk.check(self.db, 'paper', scope='live')
            if not risk['allowed']:
                reason = 'unpriced positions' if risk['unpriced_positions'] and risk['loss_usd'] < risk['limit_usd'] else 'loss breaker'
                shadow = paper_research.risk_filter({'features': features})
                self.db.x("INSERT INTO paper_skips(token,symbol,ts,strategy,reason,pair,reference,features,entry_shadow) VALUES(?,?,?,?,?,?,?,?,?)",
                          (token, symbol, int(time.time()), strategy, reason, pair_symbol(self.db, token, pool), reference,
                           json.dumps(features or {}), json.dumps(shadow, sort_keys=True)))
                self.db.add_event("paper", f"would paper-buy ${symbol} ({why}) but the {reason} pauses entries", token)
                self.last_skip = reason
                return False
            arm = lab.pick_arm(self.db)
            opened = self._open(token, symbol or token[:8], arm, lab.parse_arm(arm)[0], why, strategy, reference=reference)
            if opened:
                shadow = paper_research.risk_filter({'features': features})
                self.db.x("UPDATE paper SET features=?,entry_shadow=? WHERE token=?",
                          (json.dumps(features or {}), json.dumps(shadow, sort_keys=True), token))
            return opened

    def _open(self, token, sym, arm, policy, why, strategy, reference=None):
        n_open = self.db.one("SELECT COUNT(*) n FROM paper WHERE status='open'")["n"]
        if n_open >= C.PAPER_MAX_OPEN:
            self.db.add_event("paper", f"would paper-buy ${sym} ({why}) but the book is full", token)
            return False
        try:
            quote = execution.entry(self.rpc, self.db, token, C.PAPER_SIZE_USD, reference=reference, fallback=True)
        except Exception:
            # No fabricated fill when the pool, reference price, liquidity or gas cannot be checked.
            self.db.add_event("paper", f"paper entry for ${sym} deferred: execution checks unavailable or outside limits", token)
            return False
        cost = lab.token_cost(self.db, token)
        qty = quote.get('paper_fill_raw', quote['minimum_raw']) / 1e18
        price = execution.entry_basis(C.PAPER_SIZE_USD, qty)
        pair_name = pair_symbol(self.db, token, quote['pool'])
        provider = quote.get('provider') or 'pons'
        with self.db.transaction():
            if self.db.one("SELECT 1 FROM paper WHERE token=?", (token,)):
                return False
            self.db.x("INSERT INTO paper(token,symbol,opened_ts,entry_usd,size_usd,qty,status,last_usd,qty_left,peak_usd,realized_usd,policy,tp_done,trail_on,cost,execution_model,pool_key,policy_spec,gas_usd,liquidation_usd,marked_ts,strategy,execution_spec)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (token, sym, int(time.time()), price, C.PAPER_SIZE_USD, qty, "open", quote['price'], qty, price,
                       -quote['gas_usd'], arm, '[]', 0, cost, execution.MODEL, json.dumps(quote['pool']),
                       json.dumps(policy), quote['gas_usd'], quote['liquidation_usd'], int(time.time()), strategy,
                       json.dumps(execution.evidence_spec(), sort_keys=True)))
            self.db.x("UPDATE paper SET pair=?,route_provider=?,live_fill=?,route=? WHERE token=?",
                      (pair_name, provider, 1 if quote.get('live_fill', True) else 0,
                       json.dumps(route.summary(quote.get('route')), sort_keys=True), token))
        self.db.add_event("paper", f"paper buy: ${C.PAPER_SIZE_USD:.0f} of ${sym} at ${price:.6g} ({why}, arm {arm}, {pair_name or '?'} pool, filled via {provider}; cost {cost * 100:.1f}%/side research estimate; paper fill uses conservative route quotes and gas)", token)
        return True

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

    def mark(self, prices=None, value=True):
        """Evaluate exits before optional valuations. Slow valuation I/O holds no book lock.

        Per-position locks prevent duplicate fills while allowing other positions to progress.
        Older observations and valuation responses cannot overwrite newer position state.
        """
        return self._mark(prices, value)

    def _position_lock(self, position_id):
        with self._position_guard:
            return self._position_locks.setdefault(position_id, threading.Lock())

    def _value(self, position_id):
        p = self.db.one("SELECT * FROM paper WHERE id=? AND status='open'", (position_id,))
        if not p or not p.get('execution_model'):
            return
        qty = p['qty_left'] if p['qty_left'] is not None else p['qty']
        started = time.time()
        try:
            liquidation = self._quote(p, int(qty * 1e18), p.get('last_usd'))[0]
        except Exception:
            return                              # retain the last observation and its age, never invent a fill
        lock = self._position_lock(position_id)
        if not lock.acquire(blocking=False):
            return                              # an exit has priority over this optional valuation
        try:
            self.db.x("UPDATE paper SET liquidation_usd=?,marked_ts=? WHERE id=? AND status='open'"
                      " AND COALESCE(qty_left,qty)=? AND COALESCE(marked_ts,0)<=?",
                      (liquidation, int(started), position_id, qty, started))
        finally:
            lock.release()

    def _quote(self, p, amount, reference=None):
        """(proceeds after gas, gas, provider, live_fill) of selling `amount` into USDG by the best route now;
        `reference` is the pool's mid, the band a route must sit in."""
        rpc = self.rpc.read_only(5) if hasattr(self.rpc, 'read_only') else self.rpc
        bid = execution.exit_quote(rpc, json.loads(p['pool_key']), p['token'], amount, cached_prices=True, fallback=True,
                                   reference=reference)
        return (bid.get('paper_fill_usd', bid['minimum_usd']) - bid['gas_usd'], bid['gas_usd'],
                bid.get('provider') or 'pons', bid.get('live_fill', True))

    def _mark(self, mids=None, value=True):
        observed = time.time()
        opens = self.db.q("SELECT * FROM paper WHERE status='open'")
        if not opens:
            return
        own = set()
        if mids is None:
            mids, own = poolstate.position_mids(self.rpc, opens)
            prices = token_prices([p["token"] for p in opens if p["token"] not in own])
        else:
            prices, own = {}, set(mids)
        healthy = True
        for p in opens:
            px = mids.get(p["token"]) if p["token"] in own else usable_price(prices.get(p["token"]))
            if not px or not p["entry_usd"] or not p["qty"]:
                healthy = False
                continue
            lock = self._position_lock(p['id'])
            if not lock.acquire(blocking=False):
                healthy = False
                continue                        # another marker owns this exit; keep checking the other positions
            try:
                if self._mark_position(p['id'], px, observed) is False:
                    healthy = False
            finally:
                lock.release()
        if value:
            for p in opens:                      # no position or entry lock held during valuation network calls
                self._value(p['id'])
        return healthy

    def _mark_position(self, position_id, px, observed):
        p = self.db.one("SELECT * FROM paper WHERE id=? AND status='open'", (position_id,))
        if not p or (p.get('observed_ts') or 0) > observed:
            return
        now = int(time.time())
        previous = p.get('monitor_ts')
        gap = max(0, now - previous) if previous else 0
        self.db.x("UPDATE paper SET observed_ts=?,monitor_ts=?,max_monitor_gap_s=MAX(COALESCE(max_monitor_gap_s,0),?) WHERE id=?",
                  (observed, now, gap, position_id))
        cost = p["cost"] if p.get("cost") is not None else COST
        try:
            policy = json.loads(p['policy_spec']) if p.get('policy_spec') else lab.parse_arm(p["policy"] or lab.DEFAULT)[0]
        except KeyError:                       # an arm that no longer exists: exit by the default rule, never stall the book
            policy, _ = lab.parse_arm(lab.DEFAULT)
        st = {"entry": p["entry_usd"], "entry_ts": p["opened_ts"], "qty_left": (p["qty_left"] if p["qty_left"] is not None else p["qty"]) / p["qty"],
              "tp_done": json.loads(p["tp_done"] or "[]"), "peak": max(p["peak_usd"] or 0, px), "trail_on": bool(p["trail_on"])}
        realized = p["realized_usd"] or 0.0
        last_why = None
        failed_quote = False
        gas_usd = p.get('gas_usd') or 0
        quoted = bool(p.get('execution_model'))  # older quoted rows keep quoted exits, never synthetic fills
        liquidation, marked = p.get('liquidation_usd'), p.get('marked_ts')
        while st["qty_left"] > 1e-9:
            checkpoint = copy.deepcopy(st)
            frac, why = lab.exit_step(policy, st, px, now)
            if frac <= 0:
                break
            if quoted:
                self.db.x("UPDATE paper SET exit_trigger_ts=COALESCE(exit_trigger_ts,?) WHERE id=?", (now, p['id']))
                try:
                    usd, gas, provider, live = self._quote(p, int(frac * p['qty'] * 1e18), px)
                except Exception:
                    # Roll back only the unfilled step, retaining any earlier quoted fills.
                    st = checkpoint
                    failed_quote = True
                    self.db.x("UPDATE paper SET exit_quote_failures=COALESCE(exit_quote_failures,0)+1,exit_quote_failed_ts=? WHERE id=?",
                              (int(time.time()), p['id']))
                    break
                gas_usd += gas
                # Which route filled the exit, and whether live could have used it (an ETH pool filled directly
                # while every aggregator was down could not): kept on the row, never a reason to drop it.
                self.db.x("UPDATE paper SET exit_provider=?,fallback_fills=COALESCE(fallback_fills,0)+? WHERE id=?",
                          (provider, 0 if live else 1, p['id']))
            else:
                usd = frac * p["qty"] * px * (1 - cost)
            realized += usd
            st["qty_left"] -= frac
            last_why = why
            self.db.add_event("paper", f"paper sell: {frac * 100:.0f}% of ${p['symbol']} at {px / p['entry_usd']:.2f}x ({why}), +${usd:.2f}"
                              + (f" via {provider}" if quoted else ""), p["token"])
        qty_left = max(0.0, st["qty_left"]) * p["qty"]
        closed = st["qty_left"] <= 1e-9
        pnl = realized - p["size_usd"] if closed else None
        if quoted:
            if closed:
                liquidation, marked = 0.0, now
            elif last_why:
                liquidation, marked = None, None   # the old quote covers a different quantity; valuation runs later
        with self.db.transaction():
            self.db.x("UPDATE paper SET last_usd=?, peak_usd=?, qty_left=?, tp_done=?, trail_on=?, realized_usd=?, recovered_usd=?,"
                      " status=?, closed_ts=?, exit_usd=?, pnl_usd=?, reason=? WHERE id=?",
                      (px, st["peak"], qty_left, json.dumps(st["tp_done"]), 1 if st["trail_on"] else 0, realized,
                       realized if st["tp_done"] else 0, "closed" if closed else "open", int(time.time()) if closed else None,
                       px if closed else None, pnl, last_why if closed else None, p["id"]))
            self.db.x("UPDATE paper SET gas_usd=?,liquidation_usd=?,marked_ts=? WHERE id=?", (gas_usd, liquidation, marked, p['id']))
        if closed:
            self.db.add_event("paper", f"paper close: ${p['symbol']} pnl ${pnl:+.2f} ({last_why}, arm {p['policy']})", p["token"])

        return not failed_quote

    def summary(self):
        from . import watch
        opens = self.db.q("SELECT * FROM paper WHERE status='open' ORDER BY opened_ts DESC")
        closed = self.db.q("SELECT * FROM paper WHERE status='closed' ORDER BY closed_ts DESC LIMIT 30")
        unreal = 0.0
        for p in opens:
            qty_left = p["qty_left"] if p["qty_left"] is not None else p["qty"]
            cost = p["cost"] if p.get("cost") is not None else COST
            value = (p.get('liquidation_usd') if p.get('execution_model')
                     else qty_left * (p["last_usd"] or p["entry_usd"]) * (1 - cost))
            p['valuation_stale'] = bool(p.get('execution_model')) and (not p.get('marked_ts') or time.time() - p['marked_ts'] > 600)
            p['valuation_stale'] = p['valuation_stale'] or value is None
            p["hedged"] = bool(json.loads(p["tp_done"] or "[]"))
            p["bag_pct"] = round(100 * qty_left / p["qty"]) if p["qty"] else 0
            p["change_pct"] = ((p["last_usd"] / p["entry_usd"]) - 1) * 100 if p.get("last_usd") else 0.0
            p["pnl_usd"] = None if p["valuation_stale"] else (p["realized_usd"] or 0) + value - p["size_usd"]
            unreal += p["pnl_usd"] or 0
        for p in closed:
            p["change_pct"] = ((p["exit_usd"] / p["entry_usd"]) - 1) * 100 if p.get("exit_usd") else 0.0
        for p in opens + closed:
            p["pair"], p["live_comparable"] = pair(p), live_comparable(p)
            p.pop("route", None)                                  # the route detail stays private; the provider is shown
        tot = self.db.one("SELECT COALESCE(SUM(pnl_usd),0) s, COALESCE(SUM(pnl_usd>0),0) w, COUNT(*) n FROM paper WHERE status='closed'")
        cur, why = lab.current_policy(self.db)
        unpriced = sum(p['valuation_stale'] for p in opens)
        every = self.db.q("SELECT status,pnl_usd,strategy,pool_key,execution_model,opened_in_pause,entry_shadow,pair,live_fill FROM paper")
        skips = self.db.q("SELECT strategy, pair, reason, COUNT(*) n FROM paper_skips GROUP BY strategy, pair, reason ORDER BY strategy")
        return {"open": opens, "closed": closed, "realized_usd": round(tot["s"], 2),
                # The headline for anything that stands in for live: fills live could have made (a checked route from
                # USDG, any pair), second-look rules only, never a position opened while the breaker was paused.
                # all_pools is the whole book.
                "live_comparable": _book(every, live_comparable), "all_pools": _book(every, lambda r: True),
                "live_comparable_means": "any pair bought from USDG by a checked route (or the token's own USDG pool), by a second-look rule at quoted fills, not while the loss breaker was paused",
                "skipped": skips,
                # Filtered rules (watch.FILTERED) own no positions: the tallies of the rows their filter kept.
                "filtered": [{"rule": f["name"], "of": f["of"], "filter": f["filter"],
                              "live_comparable": _book(every, lambda r, f=f: live_comparable(r) and watch.filtered_member(f, r)),
                              "all_pools": _book(every, lambda r, f=f: watch.filtered_member(f, r))} for f in watch.FILTERED],
                "risk_live_comparable": trade_risk.check(self.db, 'paper', latch=False, scope='live'),
                "routes": route.status(),
                "unrealized_usd": None if unpriced else round(unreal, 2), "priced_unrealized_usd": round(unreal, 2),
                "closed_count": tot["n"], "win_rate": round(100.0 * tot["w"] / tot["n"]) if tot["n"] else None,
                "size_usd": C.PAPER_SIZE_USD, "min_score": execution.MIN_SCORE,
                "unpriced_count": unpriced,
                "risk": trade_risk.check(self.db, 'paper', latch=False), "execution_model": execution.MODEL,
                "entry": entry_text(),
                "rules": f"exits by the strategy lab, in use: {cur} ({why}); every fill spends or receives USDG through the best of KyberSwap, LI.FI and the token's own USDG pool (Relay is compared, not used), with a round-trip quote, and is booked at the quote less {execution.PAPER_FILL * 100:.0f}% a side plus gas; exit triggers read the pool's own mid, converted through ETH's or the stock's price; when every aggregator is down an ETH pool is quoted directly, marked, and such an entry does not count as could-be-real; exit checks target a 15-second cadence; valuation quotes refresh separately and can become stale; legacy rows retain their original cost model"}


def _book(rows, keep):
    """Closed results of the rows `keep` accepts: totals, per rule (verdict-time rows under their label) and per pair."""
    def tally(rs):
        closed = [r for r in rs if r['status'] == 'closed' and r['pnl_usd'] is not None]
        wins = sum(r['pnl_usd'] > 0 for r in closed)
        return {"closed_count": len(closed), "realized_usd": round(sum(r['pnl_usd'] for r in closed), 2), "wins": wins,
                "win_rate": round(100.0 * wins / len(closed)) if closed else None,
                "open_count": sum(r['status'] == 'open' for r in rs)}
    kept = [r for r in rows if keep(r)]
    names = sorted({r['strategy'] or VERDICT_ENTRY for r in kept})
    pairs = sorted({pair(r) or '?' for r in kept})
    return {**tally(kept), "by_rule": [{"rule": n, **tally([r for r in kept if (r['strategy'] or VERDICT_ENTRY) == n])} for n in names],
            "by_pair": [{"pair": n, **tally([r for r in kept if (pair(r) or '?') == n])} for n in pairs]}
