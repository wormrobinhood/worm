"""Phase 4: buys from the surplus above the 90-day reserve, hedged exits, every decision public.

Demo (WH_LIVE=0): the same decisions with real quotes from the Uniswap v4 Quoter and a simulated swap,
written as 'would'. Live: swaps through the Uniswap v4 Universal Router.
Policy: verdict 'looks healthy' and score >= WH_BUY_MIN_SCORE (70); one position per token; size
min(WH_MAX_POSITION_USD, 10% of the surplus); at most WH_MAX_OPEN open; WH_MAX_DAILY_USD a day.
Exits: at 2x sell the cost plus 20% and keep the rest free (50% trailing stop); before that a 50% stop.
Pools: quote asset USDG or ETH, single hop. Others are skipped and say so."""
import json
import logging
import os
import time

from eth_abi import decode, encode
from eth_utils import keccak

from . import config as C
from .chain import call_data, selector
from . import lab
from . import readiness as RD
from .pons import POOL_REGISTERED
from .prices import eth_usd, token_prices

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
    db.x("CREATE TABLE IF NOT EXISTS trade_intents(token TEXT PRIMARY KEY, arm TEXT, scored_at INTEGER)")
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


def quote_units(pk, usd):
    """Amount of the quote asset for `usd`, in base units, or None if the quote is unsupported."""
    if pk["quote"] == C.USDG:
        return int(usd * 1e6), "USDG"
    if pk["quote"] == C.ZERO:
        return int(usd / eth_usd() * 1e18), "ETH"
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
    spent_today = db.one("SELECT COALESCE(SUM(usd),0) s FROM trades WHERE side='buy' AND ts>=?", (int(time.time()) - 86400,))["s"]
    n_open = db.one("SELECT COUNT(*) n FROM positions WHERE status='open'")["n"]
    if n_open >= MAX_OPEN or spent_today >= MAX_DAILY_USD:
        return
    size = min(MAX_POSITION_USD, 0.10 * float(runway.get("surplus_usd") or 0))
    if size < 1:
        return
    rows = db.q("SELECT s.token, s.score, s.scored_at, l.symbol FROM scores s JOIN launches l ON l.token=s.token WHERE s.verdict='looks healthy'"
                " AND s.score>=? AND s.scored_at>=? AND s.token NOT IN (SELECT token FROM positions) ORDER BY s.scored_at DESC LIMIT 3",
                (MIN_SCORE, int(time.time()) - 3 * 3600))
    for r in rows:
        token, sym = r["token"], r["symbol"] or r["token"][:8]
        it = db.one("SELECT * FROM trade_intents WHERE token=?", (token,))
        if not it:
            db.x("INSERT OR REPLACE INTO trade_intents(token,arm,scored_at) VALUES(?,?,?)", (token, lab.pick_arm(db), r["scored_at"]))
            it = db.one("SELECT * FROM trade_intents WHERE token=?", (token,))
        arm = it["arm"]
        if time.time() < it["scored_at"] + lab.parse_arm(arm)[1]:
            continue                                   # this arm enters later
        pk = pool_key(rpc, db, token)
        if not pk:
            db.add_event("trade", f"skip ${sym}: pool not found")
            continue
        amount_in, qname = quote_units(pk, size)
        if amount_in is None:
            db.add_event("trade", f"skip ${sym}: paired with an asset the worm does not hold yet")
            continue
        try:
            out, gas, zfo = quote_buy(rpc, pk, token, amount_in)
        except Exception as e:
            db.add_event("trade", f"skip ${sym}: quote failed ({str(e)[:80]})")
            continue
        min_out = int(out * 0.97)
        px = size / (out / 1e18) if out else None
        if live and acct:
            from .tx import send_tx
            if pk["quote"] != C.ZERO:
                db.add_event("trade", f"skip ${sym}: live USDG-quoted buys need the Permit2 setup (phase 4 live step)")
                continue
            try:
                data = swap_calldata(pk, zfo, amount_in, min_out, C.ZERO, token)
                h, rc = send_tx(rpc, acct, C.UNIVERSAL_ROUTER, data, value=amount_in)
                ok = rc.get("status") == "0x1"
                db.x("INSERT INTO trades(ts,token,symbol,side,usd,qty,price_usd,tx,mode,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (int(time.time()), token, sym, "buy", size, out / 1e18, px, h, "live", "SUCCESS" if ok else "REVERTED"))
                if ok:
                    db.x("INSERT OR REPLACE INTO positions(token,symbol,opened_ts,entry_usd,size_usd,qty,qty_left,peak_usd,status,mode,quote,pool_key,policy,tp_done)"
                         " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (token, sym, int(time.time()), px, size, out / 1e18, out / 1e18, px, "open", "live", qname, json.dumps(dict(pk)), arm, "[]"))
                    db.add_event("trade", f"bought ${size:.2f} of ${sym} ({out / 1e18:,.0f} tokens)")
            except Exception as e:
                db.add_event("error", f"buy ${sym} failed: {str(e)[:120]}")
        else:
            sim = simulate_buy(rpc, wallet, pk, token, amount_in, min_out, zfo)
            db.x("INSERT INTO trades(ts,token,symbol,side,usd,qty,price_usd,tx,mode,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (int(time.time()), token, sym, "buy", size, out / 1e18, px, None, "demo", f"quote {out / 1e18:,.0f} tokens for {size:.2f} {qname}; {sim}"))
            db.x("INSERT OR REPLACE INTO positions(token,symbol,opened_ts,entry_usd,size_usd,qty,qty_left,peak_usd,status,mode,quote,pool_key,policy,tp_done)"
                 " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (token, sym, int(time.time()), px, size, out / 1e18, out / 1e18, px, "open", "demo", qname, json.dumps(dict(pk)), arm, "[]"))
            db.add_event("trade", f"demo: would buy ${size:.2f} of ${sym} for {out / 1e18:,.0f} tokens, arm {arm} ({sim})")


def mark(rpc, db, live, acct=None):
    """Exits by the lab's arm on each position. Live sells wait for the token approval step; demo
    sells are recorded as 'would sell'."""
    ensure_tables(db)
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
        if live and acct and p["mode"] == "live":
            db.add_event("trade", f"sell ${p['symbol']} ({why}): live selling needs the token approval step, queued for the live phase")
            continue
        while frac > 0:
            usd = frac * p["qty"] * px * (1 - lab.FEE)
            realized += usd
            st["qty_left"] -= frac
            db.x("INSERT INTO trades(ts,token,symbol,side,usd,qty,price_usd,tx,mode,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (now, p["token"], p["symbol"], "sell", usd, frac * p["qty"], px, None, "demo", f"demo: would sell ({why})"))
            db.add_event("trade", f"demo: would sell {frac * 100:.0f}% of ${p['symbol']} at {px / p['entry_usd']:.2f}x for ${usd:.2f} ({why})")
            frac, why = lab.exit_step(policy, st, px, now) if st["qty_left"] > 1e-9 else (0, None)
        closed = st["qty_left"] <= 1e-9
        db.x("UPDATE positions SET qty_left=?, tp_done=?, trail_on=?, realized_usd=?, recovered_usd=?, peak_usd=?, status=?, closed_ts=?, reason=? WHERE token=?",
             (max(0.0, st["qty_left"]) * p["qty"], json.dumps(st["tp_done"]), 1 if st["trail_on"] else 0, realized,
              realized if st["tp_done"] else 0, st["peak"], "closed" if closed else "open", now if closed else None,
              (why if closed else None), p["token"]))


def summary(db):
    ensure_tables(db)
    return {"positions": db.q("SELECT * FROM positions ORDER BY opened_ts DESC LIMIT 20"),
            "trades": db.q("SELECT * FROM trades ORDER BY id DESC LIMIT 20"),
            "policy": f"readiness ≥ {RD.READY_AT}% first (evidence only, see the readiness panel); then verdict looks healthy and score ≥ {MIN_SCORE}; size min(${MAX_POSITION_USD:.0f}, 10% of surplus); "
                      f"≤ {MAX_OPEN} open; ≤ ${MAX_DAILY_USD:.0f} a day; only from the surplus above the 90-day reserve"}
