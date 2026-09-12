"""Phase 4: buys from the surplus above the 90-day reserve, exits by the lab's policy, every decision public.

Demo (WH_LIVE=0): the same decisions with real quotes from the Uniswap v4 Quoter and a simulated swap,
written as 'would'. Live: swaps through the Uniswap v4 Universal Router, and only once live sells exist
(LIVE_SELL_READY): a buy without a stop is not a strategy.
Policy: verdict 'looks healthy' with complete data and score >= WH_BUY_MIN_SCORE (70); one position per
token; size min(WH_MAX_POSITION_USD, 10% of the surplus); at most WH_MAX_OPEN open; WH_MAX_DAILY_USD a
day, both caps re-checked before every buy. Exits: the strategy lab's current policy.
Pools: quote asset USDG or ETH, single hop. Others are skipped once and say so.
Live buys are written as PENDING before the broadcast and reconciled from the receipt on the next mark,
so a failure between the two can never re-buy the same token or escape the daily cap."""
import json
import logging
import os
import time

from eth_abi import decode, encode
from eth_utils import keccak

from . import config as C
from .chain import addr_from_topic, selector
from . import lab
from . import readiness as RD
from .pons import POOL_REGISTERED, TRANSFER
from .prices import eth_usd, token_prices
from .tx import send_tx

log = logging.getLogger("wormhole.trader")
QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"
INIT_TOPIC = "0x" + keccak(text="Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)").hex()
KEY_T = "(address,address,uint24,int24,address)"
QUOTE_T = f"({KEY_T},bool,uint128,bytes)"
SWAP_T = f"({KEY_T},bool,uint128,uint128,bytes)"
V4_SWAP, SWAP_EXACT_IN_SINGLE, SETTLE_ALL, TAKE_ALL = b"\x10", b"\x06", b"\x0c", b"\x0f"
MIN_SCORE = int(os.environ.get("WH_BUY_MIN_SCORE", "70"))
MAX_POSITION_USD = float(os.environ.get("WH_MAX_POSITION_USD", "10"))
MAX_OPEN = int(os.environ.get("WH_MAX_OPEN", "5"))
MAX_DAILY_USD = float(os.environ.get("WH_MAX_DAILY_USD", "30"))
LIVE_SELL_READY = False       # flip only when mark() can sell live positions (Permit2 approvals + V4 sell path)
BLOCK_AFTER_FAIL_S = 6 * 3600 # a token whose buy failed before broadcast is not retried for this long
SAY_EVERY_S = 3600            # repeated skip messages are written at most this often


def ensure_tables(db):
    db.x("CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, token TEXT, symbol TEXT,"
         " side TEXT, usd REAL, qty REAL, price_usd REAL, tx TEXT, mode TEXT, note TEXT)")
    db.x("CREATE TABLE IF NOT EXISTS positions(token TEXT PRIMARY KEY, symbol TEXT, opened_ts INTEGER, entry_usd REAL,"
         " size_usd REAL, qty REAL, qty_left REAL, recovered_usd REAL DEFAULT 0, peak_usd REAL, realized_usd REAL DEFAULT 0,"
         " status TEXT, mode TEXT, quote TEXT, pool_key TEXT, closed_ts INTEGER, reason TEXT, policy TEXT, tp_done TEXT, trail_on INTEGER DEFAULT 0)")
    for col in ("policy TEXT", "tp_done TEXT", "trail_on INTEGER DEFAULT 0"):
        try:
            db.x(f"ALTER TABLE positions ADD COLUMN {col}")
        except Exception:
            pass
    db.x("CREATE TABLE IF NOT EXISTS trade_intents(token TEXT PRIMARY KEY, arm TEXT, scored_at INTEGER, status TEXT, blocked_until INTEGER)")
    for col in ("status TEXT", "blocked_until INTEGER"):
        try:
            db.x(f"ALTER TABLE trade_intents ADD COLUMN {col}")
        except Exception:
            pass
    db.x("CREATE TABLE IF NOT EXISTS pools(token TEXT PRIMARY KEY, c0 TEXT, c1 TEXT, fee INTEGER, tick_spacing INTEGER,"
         " hooks TEXT, quote TEXT)")


# ---- pools and quotes -------------------------------------------------------

def pool_key(rpc, db, token):
    row = db.one("SELECT * FROM pools WHERE token=?", (token,))
    if row:
        return row
    L = db.one("SELECT grad_block FROM launches WHERE token=?", (token,))
    if not L or not L["grad_block"]:
        return None
    gb = L["grad_block"]
    pid = None
    for lg in rpc.get_logs(C.HOOK, [POOL_REGISTERED.topic], max(0, gb - 30), gb + 30, 1000):
        p = POOL_REGISTERED.decode(lg)
        if p["memecoin"] == token:
            pid = p["poolId"]
            break
    if not pid:
        return None
    for lg in rpc.get_logs(C.POOL_MANAGER, [INIT_TOPIC, pid], max(0, gb - 30), gb + 30, 1000):
        c0, c1 = "0x" + lg["topics"][2][26:], "0x" + lg["topics"][3][26:]
        fee, ts, hooks, _, _ = decode(["uint24", "int24", "address", "uint160", "int24"], bytes.fromhex(lg["data"][2:]))
        quote = c1 if c0 == token else c0
        db.x("INSERT OR REPLACE INTO pools(token,c0,c1,fee,tick_spacing,hooks,quote) VALUES(?,?,?,?,?,?,?)",
             (token, c0, c1, int(fee), int(ts), hooks.lower(), quote))
        return db.one("SELECT * FROM pools WHERE token=?", (token,))
    return None


def _key_tuple(pk):
    return (pk["c0"], pk["c1"], int(pk["fee"]), int(pk["tick_spacing"]), pk["hooks"])


def _fresh_eth_usd():
    """ETH in USD only when the price feed fetched it within its freshness window; None otherwise, so a
    stale or seeded number never sizes a buy."""
    try:
        return eth_usd(strict=True)
    except TypeError:            # a price module without the strict parameter: nothing fresh to trust
        return None


def quote_units(pk, usd):
    """Amount of the quote asset for `usd`, in base units, or (None, None) if the quote is unsupported or,
    for ETH, no fresh ETH price is known."""
    if pk["quote"] == C.USDG:
        return int(usd * 1e6), "USDG"
    if pk["quote"] == C.ZERO:
        e = _fresh_eth_usd()
        if not e:
            return None, None
        return int(usd / e * 1e18), "ETH"
    return None, None


def quote_buy(rpc, pk, token, amount_in):
    zero_for_one = pk["c0"] != token          # spend the quote (currency0 if it sorts first) for the token
    data = "0x" + keccak(text=f"quoteExactInputSingle({QUOTE_T})")[:4].hex() + encode(
        [QUOTE_T], [(_key_tuple(pk), zero_for_one, amount_in, b"")]).hex()
    raw = rpc.eth_call(QUOTER, data)
    out, gas = decode(["uint256", "uint256"], bytes.fromhex(raw[2:]))
    return int(out), int(gas), zero_for_one


def swap_calldata(pk, zero_for_one, amount_in, min_out, currency_in, currency_out):
    actions = SWAP_EXACT_IN_SINGLE + SETTLE_ALL + TAKE_ALL
    params = [encode([SWAP_T], [(_key_tuple(pk), zero_for_one, amount_in, min_out, b"")]),
              encode(["address", "uint256"], [currency_in, amount_in]),
              encode(["address", "uint256"], [currency_out, min_out])]
    inputs = [encode(["bytes", "bytes[]"], [actions, params])]
    return selector("execute(bytes,bytes[],uint256)") + encode(["bytes", "bytes[]", "uint256"],
                                                             [V4_SWAP, inputs, int(time.time()) + 600]).hex()


def simulate_buy(rpc, wallet, pk, token, amount_in, min_out, zero_for_one):
    """eth_call of the real router call with a pretend ETH balance. Only meaningful for ETH-quoted pools."""
    if pk["quote"] != C.ZERO:
        return "simulation skipped: USDG-quoted pools need Permit2 approvals first"
    data = swap_calldata(pk, zero_for_one, amount_in, min_out, C.ZERO, token)
    try:
        rpc.call("eth_call", [{"from": wallet, "to": C.UNIVERSAL_ROUTER, "value": hex(amount_in), "data": data}, "latest",
                              {wallet: {"balance": hex(amount_in + 10 ** 17)}}])
        return "simulated swap OK"
    except Exception as e:
        return f"simulation reverted: {str(e)[:120]}"


def received_qty(receipt, token, wallet):
    """Tokens delivered to `wallet` by a swap, summed from the token's Transfer logs in the receipt (in
    whole tokens, 18 decimals). None when the receipt shows no such transfer."""
    total = 0
    seen = False
    for lg in receipt.get("logs") or []:
        topics = lg.get("topics") or []
        if (lg.get("address") or "").lower() != token or len(topics) < 3 or topics[0] != TRANSFER.topic:
            continue
        if addr_from_topic(topics[2]) != wallet:
            continue
        total += int(lg["data"], 16)
        seen = True
    return total / 1e18 if seen else None


# ---- bookkeeping ------------------------------------------------------------

def _say_once(db, kind, text, token=None, every=SAY_EVERY_S):
    """Write an event unless the same text was written less than `every` seconds ago."""
    last = db.one("SELECT ts FROM events WHERE text=? ORDER BY id DESC LIMIT 1", (text[:300],))
    if last and time.time() - last["ts"] < every:
        return False
    db.add_event(kind, text, token)
    return True


def _skip(db, token, sym, status, text):
    """Mark a candidate as skipped for good ('unsupported', 'no_pool', 'no_price'): it leaves the candidate
    window and its reason is written once."""
    db.x("INSERT OR IGNORE INTO trade_intents(token,arm,scored_at) VALUES(?,?,?)", (token, lab.DEFAULT, int(time.time())))
    prev = db.one("SELECT status FROM trade_intents WHERE token=?", (token,))
    db.x("UPDATE trade_intents SET status=? WHERE token=?", (status, token))
    if not prev or prev["status"] != status:
        db.add_event("trade", text, token)


def spent_today(db):
    """Dollars committed to buys in the last 24 h: demo, pending, mined and reverted alike. Only a buy that
    never reached the network (FAILED) does not count."""
    return db.one("SELECT COALESCE(SUM(usd),0) s FROM trades WHERE side='buy' AND ts>=? AND COALESCE(note,'') NOT LIKE 'FAILED%'",
                  (int(time.time()) - 86400,))["s"]


def open_count(db):
    """Open positions plus live buys still waiting for their receipt: both hold a slot."""
    return (db.one("SELECT COUNT(*) n FROM positions WHERE status='open'")["n"]
            + db.one("SELECT COUNT(*) n FROM trades WHERE side='buy' AND note='PENDING'")["n"])


def candidates(db, limit=3):
    """Newest healthy verdicts with complete data that are not held, not pending, not skipped and not
    blocked after a failed buy."""
    now = int(time.time())
    return db.q("SELECT s.token, s.score, s.scored_at, l.symbol FROM scores s JOIN launches l ON l.token=s.token"
                " WHERE s.verdict='looks healthy' AND s.score>=? AND s.scored_at>=? AND s.partial=0"
                " AND s.token NOT IN (SELECT token FROM positions)"
                " AND s.token NOT IN (SELECT token FROM trades WHERE note='PENDING' AND token IS NOT NULL)"
                " AND s.token NOT IN (SELECT token FROM trade_intents WHERE status IS NOT NULL OR COALESCE(blocked_until,0)>?)"
                " ORDER BY s.scored_at DESC LIMIT ?", (MIN_SCORE, now - 3 * 3600, now, limit))


# ---- decisions --------------------------------------------------------------

def decide(rpc, db, runway, live, acct=None, ready=None):
    ensure_tables(db)
    wallet = C.WALLET
    if not wallet:
        return
    if not runway.get("can_invest"):
        return
    if ready is not None and not ready.get("ready"):
        return                                         # readiness gate: evidence first, money later
    if live and acct and not LIVE_SELL_READY:
        _say_once(db, "trade", "live buys stay off until live sells exist")
        return
    spent = spent_today(db)
    n_open = open_count(db)
    if n_open >= MAX_OPEN or spent >= MAX_DAILY_USD:
        return
    size = min(MAX_POSITION_USD, 0.10 * float(runway.get("surplus_usd") or 0))
    if size < 1:
        return
    arm = lab.current_policy(db)[0]
    now = int(time.time())
    for r in candidates(db):
        if n_open >= MAX_OPEN or spent + size > MAX_DAILY_USD:
            break                                      # the caps hold inside a cycle, not only between cycles
        token, sym = r["token"], r["symbol"] or r["token"][:8]
        db.x("INSERT OR IGNORE INTO trade_intents(token,arm,scored_at) VALUES(?,?,?)", (token, arm, r["scored_at"]))
        it = db.one("SELECT * FROM trade_intents WHERE token=?", (token,))
        if time.time() < it["scored_at"] + lab.parse_arm(it["arm"])[1]:
            continue                                   # this arm enters later
        try:
            pk = pool_key(rpc, db, token)
        except Exception as e:
            _skip(db, token, sym, "no_pool", f"skip ${sym}: pool lookup failed ({str(e)[:80]})")
            continue
        if not pk:
            _skip(db, token, sym, "no_pool", f"skip ${sym}: pool not found")
            continue
        amount_in, qname = quote_units(pk, size)
        if amount_in is None:
            if pk["quote"] == C.ZERO:
                _say_once(db, "trade", f"skip ${sym}: no fresh ETH price", token)   # transient: try again later
            else:
                _skip(db, token, sym, "unsupported", f"skip ${sym}: paired with an asset the worm does not hold yet")
            continue
        try:
            out, gas, zfo = quote_buy(rpc, pk, token, amount_in)
        except Exception as e:
            _say_once(db, "trade", f"skip ${sym}: quote failed ({str(e)[:80]})", token)
            continue
        if not out:
            _skip(db, token, sym, "no_price", f"skip ${sym}: no liquidity quoted")
            continue
        min_out = int(out * 0.97)
        px = size / (out / 1e18)
        if live and acct:
            if pk["quote"] != C.ZERO:
                _skip(db, token, sym, "unsupported", f"skip ${sym}: live USDG-quoted buys need the Permit2 setup (phase 4 live step)")
                continue
            # the row exists before the network hears of the buy: a crash or a raise in between leaves a
            # PENDING/FAILED trace that blocks a second buy and counts against the daily cap
            db.x("INSERT INTO trades(ts,token,symbol,side,usd,qty,price_usd,tx,mode,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (now, token, sym, "buy", size, out / 1e18, px, None, "live", "PENDING"))
            tid = db.one("SELECT MAX(id) id FROM trades")["id"]
            try:
                data = swap_calldata(pk, zfo, amount_in, min_out, C.ZERO, token)
                h, _ = send_tx(rpc, acct, C.UNIVERSAL_ROUTER, data, value=amount_in, wait=False)
                db.x("UPDATE trades SET tx=? WHERE id=?", (h, tid))
                db.add_event("trade", f"buying ${size:.2f} of ${sym} (about {out / 1e18:,.0f} tokens, arm {it['arm']}): sent {h}", token)
                n_open += 1
                spent += size
            except Exception as e:
                db.x("UPDATE trades SET note=? WHERE id=?", (f"FAILED: {str(e)[:80]}", tid))
                db.x("UPDATE trade_intents SET blocked_until=? WHERE token=?", (now + BLOCK_AFTER_FAIL_S, token))
                db.add_event("error", f"buy ${sym} failed before broadcast: {str(e)[:120]}; not retried for {BLOCK_AFTER_FAIL_S // 3600} h", token)
        else:
            sim = simulate_buy(rpc, wallet, pk, token, amount_in, min_out, zfo)
            db.x("INSERT INTO trades(ts,token,symbol,side,usd,qty,price_usd,tx,mode,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (now, token, sym, "buy", size, out / 1e18, px, None, "demo", f"quote {out / 1e18:,.0f} tokens for {size:.2f} {qname}; {sim}"))
            db.x("INSERT OR REPLACE INTO positions(token,symbol,opened_ts,entry_usd,size_usd,qty,qty_left,peak_usd,status,mode,quote,pool_key,policy,tp_done)"
                 " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (token, sym, now, px, size, out / 1e18, out / 1e18, px, "open", "demo", qname, json.dumps(dict(pk)), it["arm"], "[]"))
            db.add_event("trade", f"demo: would buy ${size:.2f} of ${sym} for {out / 1e18:,.0f} tokens, arm {it['arm']} ({sim})", token)
            n_open += 1
            spent += size


def reconcile(rpc, db):
    """Live buys broadcast earlier: read the receipt. Mined and successful opens the position with the
    quantity the wallet actually received (the token's Transfer logs; the quote as a fallback); reverted
    is written as such. No receipt yet leaves the row PENDING, holding its slot and its share of the cap."""
    ensure_tables(db)
    wallet = C.WALLET
    for t in db.q("SELECT * FROM trades WHERE side='buy' AND note='PENDING' AND tx IS NOT NULL"):
        try:
            rc = rpc.call("eth_getTransactionReceipt", [t["tx"]])
        except Exception as e:
            log.info("receipt for %s not read: %s", t["tx"], e)
            continue
        if not rc:
            continue
        if rc.get("status") != "0x1":
            db.x("UPDATE trades SET note='REVERTED' WHERE id=?", (t["id"],))
            db.x("UPDATE trade_intents SET blocked_until=? WHERE token=?", (int(time.time()) + BLOCK_AFTER_FAIL_S, t["token"]))
            db.add_event("error", f"buy ${t['symbol']} reverted on chain: {t['tx']}; not retried for {BLOCK_AFTER_FAIL_S // 3600} h", t["token"])
            continue
        qty = received_qty(rc, t["token"], wallet) if wallet else None
        if not qty:
            qty = t["qty"]
        px = t["usd"] / qty if qty else t["price_usd"]
        db.x("UPDATE trades SET note='SUCCESS', qty=?, price_usd=? WHERE id=?", (qty, px, t["id"]))
        pk = db.one("SELECT * FROM pools WHERE token=?", (t["token"],))
        it = db.one("SELECT arm FROM trade_intents WHERE token=?", (t["token"],))
        qname = "ETH" if pk and pk["quote"] == C.ZERO else ("USDG" if pk and pk["quote"] == C.USDG else None)
        db.x("INSERT OR REPLACE INTO positions(token,symbol,opened_ts,entry_usd,size_usd,qty,qty_left,peak_usd,status,mode,quote,pool_key,policy,tp_done)"
             " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (t["token"], t["symbol"], t["ts"], px, t["usd"], qty, qty, px, "open", "live", qname,
              json.dumps(dict(pk)) if pk else None, (it["arm"] if it else None) or lab.DEFAULT, "[]"))
        db.add_event("trade", f"bought ${t['usd']:.2f} of ${t['symbol']} ({qty:,.0f} tokens received)", t["token"])


def mark(rpc, db, live, acct=None):
    """Exits by the lab's arm on each position. Demo positions are sold as 'would sell'. Live positions
    only track their peak until the live sell path exists (LIVE_SELL_READY); the sell itself is deferred.

    Live sell path, still to build (in this order, each written as PENDING first like the buy):
      1. once per token: token.approve(PERMIT2, max) and PERMIT2.approve(token, UNIVERSAL_ROUTER, uint160.max, expiry)
      2. amountIn = the wallet's balanceOf(token) share to sell (not qty_left, which may be stale)
      3. V4_SWAP with zeroForOne = (pk["c0"] == token), SWAP_EXACT_IN_SINGLE(amountIn, min_out),
         SETTLE_ALL(token, amountIn), TAKE_ALL(quote, min_out); min_out from the Quoter less 3%
      4. reconcile the receipt: proceeds from the quote asset's Transfer log (ETH: balance delta), then
         update qty_left/tp_done/trail_on/realized and close when nothing is left."""
    ensure_tables(db)
    reconcile(rpc, db)
    opens = db.q("SELECT * FROM positions WHERE status='open'")
    if not opens:
        return
    prices = token_prices([p["token"] for p in opens])
    now = int(time.time())
    for p in opens:
        px = (prices.get(p["token"]) or {}).get("price_usd")
        if not px or not p["entry_usd"] or not p["qty"]:
            continue
        policy, _ = lab.parse_arm(p["policy"] or lab.DEFAULT)
        st = {"entry": p["entry_usd"], "entry_ts": p["opened_ts"], "qty_left": (p["qty_left"] if p["qty_left"] is not None else p["qty"]) / p["qty"],
              "tp_done": json.loads(p["tp_done"] or "[]"), "peak": max(p["peak_usd"] or 0, px), "trail_on": bool(p["trail_on"])}
        realized = p["realized_usd"] or 0.0
        frac, why = lab.exit_step(policy, st, px, now)
        if frac <= 0:
            db.x("UPDATE positions SET peak_usd=? WHERE token=?", (st["peak"], p["token"]))
            continue
        if p["mode"] == "live":
            # a live bag can only be sold live: keep the peak so the trailing stop has its history the day
            # sells exist; tp_done/trail_on stay untouched because no sale happened (marking a take-profit
            # as done without selling would switch the stop off on a full bag)
            db.x("UPDATE positions SET peak_usd=? WHERE token=?", (st["peak"], p["token"]))
            _say_once(db, "trade", f"sell ${p['symbol']} ({why}): live selling needs the token approval step, deferred until it exists", p["token"])
            continue
        last_why = None
        while frac > 0:
            usd = frac * p["qty"] * px * (1 - lab.FEE)
            realized += usd
            st["qty_left"] -= frac
            last_why = why
            db.x("INSERT INTO trades(ts,token,symbol,side,usd,qty,price_usd,tx,mode,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (now, p["token"], p["symbol"], "sell", usd, frac * p["qty"], px, None, "demo", f"demo: would sell ({why})"))
            db.add_event("trade", f"demo: would sell {frac * 100:.0f}% of ${p['symbol']} at {px / p['entry_usd']:.2f}x for ${usd:.2f} ({why})", p["token"])
            frac, why = lab.exit_step(policy, st, px, now) if st["qty_left"] > 1e-9 else (0, None)
        closed = st["qty_left"] <= 1e-9
        db.x("UPDATE positions SET qty_left=?, tp_done=?, trail_on=?, realized_usd=?, recovered_usd=?, peak_usd=?, status=?, closed_ts=?, reason=? WHERE token=?",
             (max(0.0, st["qty_left"]) * p["qty"], json.dumps(st["tp_done"]), 1 if st["trail_on"] else 0, realized,
              realized if st["tp_done"] else 0, st["peak"], "closed" if closed else "open", now if closed else None,
              (last_why if closed else None), p["token"]))


def summary(db):
    ensure_tables(db)
    return {"positions": db.q("SELECT * FROM positions ORDER BY opened_ts DESC LIMIT 20"),
            "trades": db.q("SELECT * FROM trades ORDER BY id DESC LIMIT 20"),
            "live_sell_ready": LIVE_SELL_READY,
            "policy": f"readiness ≥ {RD.READY_AT}% first (evidence only, see the readiness panel); then verdict looks healthy and score ≥ {MIN_SCORE}; size min(${MAX_POSITION_USD:.0f}, 10% of surplus); "
                      f"≤ {MAX_OPEN} open; ≤ ${MAX_DAILY_USD:.0f} a day; only from the surplus above the 90-day reserve"
                      + ("" if LIVE_SELL_READY else "; live buys wait for live sells")}
