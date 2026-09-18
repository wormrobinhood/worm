"""The second look and the fast book.

A verdict is a snapshot taken seconds after graduation, when snipers are still selling into the new pool.
So nothing is bought at the verdict. Every complete verdict puts its token on a watch list; the pool's
mid price is read from the chain once a minute; at fixed look times the path since the verdict is judged
by the entry rule, and only a token that passes is bought on paper. Open positions are re-priced from the
chain every few seconds, so a stop or a trailing stop acts on a fresh price, not a five-minute-old one.

Read-only against the chain (batched storage reads, quoter calls). Nothing here signs or sends."""
import json
import logging
import time

from . import config as C, lab, poolstate, trade_checks as execution
from .prices import eth_usd_last
from .scorer import SWAP_TOPIC

log = logging.getLogger("wormhole.watch")
FAST_EVERY_S = 15                 # open positions
SAMPLE_EVERY_S = 60               # watched tokens
DEAD_BELOW = 0.2                  # a pool 80% under its first reading ...
DEAD_AFTER_S = 1800               # ... half an hour after the verdict is dropped: nothing buys a collapse
KEEP_TICKS_S = 3 * 86400
POOLS_PER_STEP = 3                # pool lookups are two log queries each: a few per step, never a burst
MAX_TRIES = 5                     # pool lookups per token before it is given up as unsupported
FLOW_WINDOW_S = 900               # the pool's own swaps are counted over this long, at a look and right after graduation
FLOW_CAP = 10_000                 # the node's cap per log query; a busier window is measured as "at least this"

# Entry rules, each frozen under its name: evidence is only ever pooled per name, so changing a rule means
# renaming it. `looks` are minutes after the verdict; a token is judged once per look per rule and bought by
# the first rule that passes (one position per token). Every condition reads a feature from features().
# The numbers come from the research notes of 2026-09-19 (CLAUDE_HANDOFF.md): nothing measured at graduation
# separates winners, so every rule waits and asks whether the token is still alive and wanted.
STRATEGIES = [
    {"name": "second-look-v1", "looks": [30, 60, 120],
     "conditions": [{"feature": "creator_tax_bps", "op": "<=", "value": 200},
                    {"feature": "creator_prev_launches", "op": "<=", "value": 4},
                    {"feature": "ret_p0", "op": ">=", "value": -0.20},
                    {"feature": "dd_peak", "op": ">=", "value": -0.35},
                    {"feature": "moves_15m", "op": ">=", "value": 5}]},
]
STRATEGY = STRATEGIES[0]           # the rule the summaries name first
OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b}


def ensure_tables(db):
    db.x("CREATE TABLE IF NOT EXISTS watch(token TEXT PRIMARY KEY, symbol TEXT, t0 INTEGER, p0 REAL, pool_key TEXT,"
         " status TEXT, looks_done TEXT DEFAULT '[]', tries INTEGER DEFAULT 0, metrics TEXT, note TEXT, flow0 TEXT)")
    db.x("CREATE INDEX IF NOT EXISTS watch_status ON watch(status, t0)")
    db.x("CREATE TABLE IF NOT EXISTS watch_ticks(token TEXT, ts INTEGER, price REAL, PRIMARY KEY(token, ts))")


def add(db, token, result, symbol=None, now=None):
    """Called for every verdict. Only a complete assessment of someone else's token is watched; a rescan
    never restarts the clock."""
    metrics = result.get("metrics") or {}
    if (C.TOKEN and token.lower() == C.TOKEN) or result.get("partial") or metrics.get("partial"):
        return False
    ensure_tables(db)
    keep = {k: metrics.get(k) for k in ("creator_tax_bps", "creator_prev_launches", "creator_rugged", "top10_pct",
                                        "holders", "snipe_pct", "fleet_pct", "launch_to_grad_s")}
    keep["score"], keep["verdict"] = result.get("score"), result.get("verdict")
    if symbol is None:
        symbol = (db.one("SELECT symbol FROM launches WHERE token=?", (token,)) or {}).get("symbol")
    return bool(db.xc("INSERT OR IGNORE INTO watch(token,symbol,t0,status,metrics) VALUES(?,?,?,'new',?)",
                      (token, symbol or token[:8], int(now or time.time()), json.dumps(keep))))


def _resolve_pools(rpc, db):
    from . import trader
    for row in db.q("SELECT token, tries FROM watch WHERE status='new' ORDER BY t0 DESC LIMIT ?", (POOLS_PER_STEP,)):
        try:
            pk = trader.pool_key(rpc, db, row["token"])
        except Exception as e:
            log.info("watch pool lookup failed: %s", e)
            pk = None
        if pk and execution.verified(pk, row["token"], execution.PAPER_QUOTES):
            db.x("UPDATE watch SET status='watching', pool_key=? WHERE token=?", (json.dumps(dict(pk)), row["token"]))
        elif pk or row["tries"] + 1 >= MAX_TRIES:
            db.x("UPDATE watch SET status='unsupported', note=? WHERE token=?",
                 ("paired with an asset the book does not trade" if pk else "pool not found", row["token"]))
        else:
            db.x("UPDATE watch SET tries=tries+1 WHERE token=?", (row["token"],))


def _eth():
    """ETH in USD for reading mids: the last real fetch is good enough to follow a price path (an exit is
    a ratio to the entry); fills still demand a fresh price in trade_checks."""
    try:
        return eth_usd_last()
    except Exception:
        return None


def _s256(word):
    v = int(word, 16)
    return v - (1 << 256) if v >> 255 else v


def flow(rpc, pk, token, first, last):
    """The pool's swaps in blocks [first, last]: how many, how many were buys, and the USD that changed hands.
    Amounts are the swapper's deltas: a positive token delta is a buy. None when the node did not answer."""
    try:
        unit, usd = execution.quote_unit(pk)
        logs = list(rpc.get_logs(C.POOL_MANAGER, [SWAP_TOPIC, poolstate.pool_id(pk)], first, last, 20_000, cap=FLOW_CAP))
    except Exception as e:
        log.info("watch flow read failed: %s", e)
        return None
    token_is_0 = pk["c0"] == token
    swaps = buys = 0
    volume = 0.0
    for lg in logs:
        data = lg.get("data", "")[2:]
        if len(data) < 128:
            continue
        a0, a1 = _s256(data[0:64]), _s256(data[64:128])
        swaps += 1
        buys += (a0 if token_is_0 else a1) > 0
        volume += abs(a1 if token_is_0 else a0) / unit * usd
    return {"swaps": swaps, "buys": buys, "usd": volume}


def _flows(rpc, db, row, pk):
    """Swap flow for a look: the last FLOW_WINDOW_S, and (read once, kept) the same span right after graduation."""
    blocks = int(FLOW_WINDOW_S / C.BLOCK_TIME)
    try:
        head = rpc.block_number()
    except Exception:
        return None, None
    recent = flow(rpc, pk, row["token"], head - blocks, head)
    first = None
    try:
        first = json.loads(row.get("flow0") or "null")
    except ValueError:
        pass
    if first is None:
        grad = (db.one("SELECT grad_block FROM launches WHERE token=?", (row["token"],)) or {}).get("grad_block")
        if grad:
            first = flow(rpc, pk, row["token"], grad, grad + blocks)
            if first is not None:
                db.x("UPDATE watch SET flow0=? WHERE token=?", (json.dumps(first), row["token"]))
    return recent, first


def features(db, row, now, mid, recent=None, first=None):
    """What the watcher knows at a look: the path of pool mids since the verdict and what the verdict
    measured. None when the path is too thin to judge."""
    ticks = db.q("SELECT ts, price FROM watch_ticks WHERE token=? AND ts<=? ORDER BY ts", (row["token"], now))
    if len(ticks) < 5 or not row["p0"]:
        return None
    prices = [t["price"] for t in ticks]

    def at(age):
        past = [t["price"] for t in ticks if t["ts"] <= now - age]
        return past[-1] if past else None
    window = [t for t in ticks if t["ts"] >= now - 900]
    moves = sum(1 for a, b in zip(window, window[1:]) if a["price"] != b["price"])
    out = {"ret_p0": mid / row["p0"] - 1, "dd_peak": mid / max(max(prices), mid) - 1,
           "rebound": mid / min(min(prices), mid) - 1, "moves_15m": moves,
           "ret_15m": (mid / at(900) - 1) if at(900) else 0.0, "ret_5m": (mid / at(300) - 1) if at(300) else 0.0,
           "age_min": (now - row["t0"]) / 60.0}
    if recent:
        out.update(swaps_15m=recent["swaps"], vol_15m_usd=recent["usd"],
                   buy_share_15m=(recent["buys"] / recent["swaps"]) if recent["swaps"] else 0.0)
        if first and first["usd"] > 0:
            out["vol_ratio"] = recent["usd"] / first["usd"]
    try:
        out.update({k: v for k, v in json.loads(row["metrics"] or "{}").items() if isinstance(v, (int, float))})
    except ValueError:
        pass
    return out


FLOW_FEATURES = ("swaps_15m", "vol_15m_usd", "buy_share_15m", "vol_ratio")


def _needs_flow(strategy):
    return any(c["feature"] in FLOW_FEATURES for c in strategy["conditions"])


def passes(strategy, f):
    """(True, "") or (False, the first condition that failed). A feature the watcher could not measure fails."""
    for c in strategy["conditions"]:
        v = f.get(c["feature"])
        if c["feature"] in ("creator_tax_bps", "creator_prev_launches") and v is None:
            v = 0
        if v is None or not OPS[c["op"]](v, c["value"]):
            return False, f"{c['feature']} {v if v is None else round(v, 3)} is not {c['op']} {c['value']}"
    return True, ""


def all_looks():
    return sorted({m for rule in STRATEGIES for m in rule["looks"]})


def watch_for_s():
    """A token leaves the list after the last look any rule takes."""
    return max(all_looks()) * 60 + 600


def _sample(rpc, db, paper, now):
    rows = db.q("SELECT * FROM watch WHERE status='watching'")
    if not rows:
        return
    pools = {r["token"]: json.loads(r["pool_key"]) for r in rows}
    mids = poolstate.mids(rpc, pools, _eth())
    looks = all_looks()
    for r in rows:
        mid = mids.get(r["token"])
        if mid:
            db.x("INSERT OR IGNORE INTO watch_ticks(token,ts,price) VALUES(?,?,?)", (r["token"], now, mid))
            if not r["p0"]:
                db.x("UPDATE watch SET p0=? WHERE token=? AND p0 IS NULL", (mid, r["token"]))
                r["p0"] = mid
            if mid <= r["p0"] * DEAD_BELOW and now - r["t0"] >= DEAD_AFTER_S:
                db.x("UPDATE watch SET status='done', note='collapsed: no rule buys this' WHERE token=?", (r["token"],))
                continue
        done = json.loads(r["looks_done"] or "[]")
        due = [m for m in looks if m not in done and now >= r["t0"] + m * 60]
        if due and mid:
            look = due[-1]                         # after an outage only the latest due look is judged
            done = sorted(set(done) | set(due))
            rules = [rule for rule in STRATEGIES if look in rule["looks"]]
            recent, first = (_flows(rpc, db, r, pools[r["token"]]) if any(_needs_flow(rule) for rule in rules)
                             else (None, None))
            f = features(db, r, now, mid, recent, first)
            entered, why = False, "price path too thin"
            for rule in rules if f else []:
                ok, why = passes(rule, f)
                if ok:
                    entered = paper.enter(r["token"], r["symbol"], mid, rule["name"],
                                          f"{rule['name']} at {look} min: {f['ret_p0'] * 100:+.0f}% since the verdict")
                    why = f"{rule['name']} entered" if entered else "book full or quote unavailable"
                    break
            db.x("UPDATE watch SET looks_done=?, status=?, note=? WHERE token=?",
                 (json.dumps(done), "entered" if entered else r["status"], f"look {look}: {why}", r["token"]))
            if entered:
                _enroll(db, r, now, mid)
                continue
        if now - r["t0"] > watch_for_s() or len(done) == len(looks):
            db.x("UPDATE watch SET status='done' WHERE token=? AND status='watching'", (r["token"],))


def _enroll(db, row, now, mid):
    """The lab ranks exit rules on what the book really buys: a case from the entry, at the entry price."""
    lab.ensure_tables(db)
    try:
        m = json.loads(row["metrics"] or "{}")
    except ValueError:
        m = {}
    db.x("INSERT OR IGNORE INTO lab_cases(token,symbol,score,verdict,t0,p0,status,cost,misses) VALUES(?,?,?,?,?,?,?,?,0)",
         (row["token"], row["symbol"], m.get("score"), m.get("verdict"), now, mid, "active", lab.token_cost(db, row["token"])))
    db.x("INSERT OR IGNORE INTO ticks(token,ts,price) VALUES(?,?,?)", (row["token"], now, mid))


class Watcher:
    """One thread (run.py): every FAST_EVERY_S the open paper positions, every SAMPLE_EVERY_S the watch list."""

    def __init__(self, rpc, db, paper):
        self.rpc, self.db, self.paper = rpc, db, paper
        self.sampled = 0.0
        ensure_tables(db)

    def step(self, now=None):
        now = int(now or time.time())
        opens = self.db.q("SELECT token, pool_key FROM paper WHERE status='open' AND execution_model=? AND pool_key IS NOT NULL",
                          (execution.MODEL,))
        pools = {}
        for p in opens:
            try:
                pk = json.loads(p["pool_key"])
            except ValueError:
                continue
            if execution.verified(pk, p["token"], execution.PAPER_QUOTES) and "fee" in pk:
                pools[p["token"]] = pk
        if pools:
            mids = {t: m for t, m in poolstate.mids(self.rpc, pools, _eth()).items() if m}
            if mids:
                self.paper.mark(prices=mids, value=False)
        if now - self.sampled >= SAMPLE_EVERY_S:
            self.sampled = now
            _resolve_pools(self.rpc, self.db)
            _sample(self.rpc, self.db, self.paper, now)
            self.db.x("DELETE FROM watch_ticks WHERE ts<?", (now - KEEP_TICKS_S,))
            self.db.x("DELETE FROM watch WHERE t0<? AND status!='watching'", (now - KEEP_TICKS_S,))


def summary(db):
    ensure_tables(db)
    day = int(time.time()) - 86400
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM watch WHERE t0>=? GROUP BY status", (day,))}
    return {"strategies": [{"name": rule["name"], "looks_min": rule["looks"]} for rule in STRATEGIES],
            "watching": counts.get("watching", 0) + counts.get("new", 0),
            "entered_24h": counts.get("entered", 0), "passed_over_24h": counts.get("done", 0),
            "unsupported_24h": counts.get("unsupported", 0),
            "rule": "nothing is bought at the verdict; the pool is watched on-chain and judged again at each look"}
