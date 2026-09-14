"""Fee accounting and treasury actions.

Claims retain their original allocation policy. Outstanding obligations and pending spends are
reserved; an accounting error pauses spending. Pending records are persisted before submission.
Successful receipts require event evidence, confirmed reverts release obligations, and unknown
outcomes remain pending until reconciliation. New chain submissions use the private durable outbox.
"""
import logging
import json
import os
import time

from eth_abi import decode, encode

from . import finality
from .tx import ReceiptPending
from . import config as C
from .chain import addr_from_topic, call_data, call_fn, selector, topic

log = logging.getLogger("wormhole.treasury")
MIN_FORWARD_USD = 0.50
WATCH = None               # set by run.py: callable(ev) that tells the screen and the page what the worm is doing right now
BUSY_SINCE = 0.0           # when the transaction now in flight was broadcast; 0 when none is
BUSY_MAX_S = 120           # a transaction that never settles does not stop the digging for longer than this
MIN_BURN_USD = float(os.environ.get("WH_MIN_BURN_USD", "5.0"))      # a burn is one pool swap: the share is batched so gas and slippage stay small
BURN_SLIPPAGE = 0.03       # the swap reverts if the pool delivers less than the quote minus this
MIN_GOLD_USD = float(os.environ.get("WH_MIN_GOLD_USD", "5.0"))      # a gold buy is one v3 swap: batched like the burn
GOLD_SLIPPAGE = 0.02       # gold's pools are deep; the swap reverts below the quote minus this
QUOTE_V3_T = "(address,address,uint256,uint24,uint160)"        # QuoterV2.quoteExactInputSingle's struct
SWAP_V3_T = "(address,address,uint24,address,uint256,uint256,uint160)"   # SwapRouter02.exactInputSingle's struct (no deadline)
GOLD_PRICE_TTL_S = 300
_gold_price = (0.0, None)
SWAP_TTL_S = 180           # on-chain deadline, measured from the refreshed quote request
QUOTE_MAX_AGE_S = 30       # refuse to sign after slow quote/preflight RPC calls
PERMIT_TTL_S = 600         # router authorization expires even if no swap is submitted
TAKE = b"\x0e"             # Uniswap v4 router action: take a currency to a recipient (amount 0 = the whole open delta)
CLAIMED_TOPIC = topic("ClaimedToken(address,address,uint256)")     # PonsV2FeeEscrow: recipient, token indexed
TRANSFER_TOPIC = topic("Transfer(address,address,uint256)")


def ensure_tables(db):
    db.x("CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT, asset TEXT,"
         " amount REAL, tx TEXT, note TEXT, qty REAL)")
    for col in ("qty REAL", "to_addr TEXT", "authorization TEXT", "owner_share REAL", "burn_share REAL", "gold_share REAL"):                # tokens burned / bought; the recipient a transfer was sent to
        name = col.split()[0]
        if name not in {r['name'] for r in db.q('PRAGMA table_info(ledger)')}:
            db.x(f"ALTER TABLE ledger ADD COLUMN {col}")


def pin_claim_shares(db):
    """Legacy claims need an explicit migration policy; never guess from current settings."""
    ensure_tables(db)
    if db.one("SELECT 1 FROM ledger WHERE kind IN ('claim','claim_pending') AND owner_share IS NULL"):
        values = [os.environ.get('WH_LEGACY_' + k + '_SHARE') for k in ('OWNER','BURN','GOLD')]
        if any(v is None for v in values):
            raise RuntimeError('legacy claim allocations require WH_LEGACY_OWNER/BURN/GOLD_SHARE before payments resume')
        owner, burn_, gold_ = map(float, values)
        if not (all(0 <= v <= 1 for v in (owner,burn_,gold_)) and owner+burn_+gold_ <= 1):
            raise RuntimeError('invalid legacy claim split')
        db.x("UPDATE ledger SET owner_share=?,burn_share=?,gold_share=? WHERE kind IN ('claim','claim_pending') AND owner_share IS NULL", (owner,burn_,gold_))


def units(usd):
    """USDG/USDC have 6 decimals. Round, never truncate: 0.29 * 1e6 is 289999.99999999994."""
    return int(round(usd * 1e6))


def claimable_usdg(rpc, wallet):
    v = call_fn(rpc, C.FEE_ESCROW, "balanceOfToken(address,address)", ("uint256",), ("address", "address"),
                (wallet, C.USDG))
    return (v or 0) / 1e6


def usdg_balance(rpc, wallet):
    v = call_fn(rpc, C.USDG, "balanceOf(address)", ("uint256",), ("address",), (wallet,))
    return (v or 0) / 1e6


def owed_to_owner(db):
    """The creator's share of every claim so far, minus what was forwarded or is on its way."""
    pin_claim_shares(db)
    c = db.one("SELECT COALESCE(SUM(amount * owner_share),0) s FROM ledger WHERE kind='claim'")["s"]
    f = db.one("SELECT COALESCE(SUM(amount),0) s FROM ledger WHERE kind IN ('forward','forward_pending')")["s"]
    return c - f


def owed_to_burn(db):
    """The burn share of every claim so far, minus what was burned or is on its way, in USDG."""
    pin_claim_shares(db)
    c = db.one("SELECT COALESCE(SUM(amount * burn_share),0) s FROM ledger WHERE kind='claim'")["s"]
    b = db.one("SELECT COALESCE(SUM(amount),0) s FROM ledger WHERE kind IN ('burn','burn_pending')")["s"]
    return c - b


def owed_to_gold(db):
    """The gold share of every claim so far, minus what bought gold or is on its way, in USDG."""
    pin_claim_shares(db)
    c = db.one("SELECT COALESCE(SUM(amount * gold_share),0) s FROM ledger WHERE kind='claim'")["s"]
    g = db.one("SELECT COALESCE(SUM(amount),0) s FROM ledger WHERE kind IN ('gold','gold_pending')")["s"]
    return c - g


def owed_total(db):
    outstanding = max(0.0, owed_to_owner(db)) + max(0.0, owed_to_burn(db)) + max(0.0, owed_to_gold(db))
    # Pending sends are not owed a second time, but their funds must remain reserved until settlement.
    pending = db.one("SELECT COALESCE(SUM(amount),0) n FROM ledger WHERE kind IN ('forward_pending','burn_pending','gold_pending','compute_pending') AND asset='USDG'")['n']
    # Refills reserve their exact USDG input until event-backed settlement.
    if db.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gas_refills'"):
        pending += db.one("SELECT COALESCE(SUM(amount),0) n FROM gas_refills WHERE state='pending'")['n'] / 1e6
    from .launch_allocation import reserved_usdg
    return outstanding + pending + reserved_usdg(db)


def free_usd(db, usd_real):
    """The part of the real treasury that is the worm's own: the balance minus what is owed to the creator and
    to the burn and to gold. Runway, surplus and readiness are sized from this, never from money passing through."""
    try:
        ensure_tables(db)
        if db.one("SELECT 1 FROM ledger WHERE kind='claim_pending'"):
            return 0.0
        return round(max(0.0, float(usd_real or 0.0) - owed_total(db)), 2)
    except Exception as e:
        log.info("owed read failed: %s", e)
        return 0.0


# ---- receipts ----------------------------------------------------------------

def _word(data):
    return int(data, 16) if data and len(data) > 2 else 0


def claimed_in(rc, wallet):
    """USDG amount from the escrow's ClaimedToken(recipient, token, amount) log, or None if absent."""
    for lg in rc.get("logs", []):
        t = lg.get("topics", [])
        if (lg["address"].lower() == C.FEE_ESCROW and len(t) == 3 and t[0] == CLAIMED_TOPIC
                and addr_from_topic(t[1]) == wallet.lower() and addr_from_topic(t[2]) == C.USDG):
            return _word(lg["data"]) / 1e6
    return None


def transferred(rc, token, frm, to, value):
    """True when the receipt carries the ERC-20 Transfer(from, to, value) log for exactly this transfer.
    A status of 0x1 alone is not enough: a token that returns false does not revert."""
    for lg in rc.get("logs", []):
        t = lg.get("topics", [])
        if (lg["address"].lower() == token and len(t) == 3 and t[0] == TRANSFER_TOPIC
                and addr_from_topic(t[1]) == frm.lower() and (to is None or addr_from_topic(t[2]) == to.lower())
                and _word(lg["data"]) == value):
            return True
    return False


def burned_in(rc):
    """$WORM delivered to the burn address in this receipt, in whole tokens, or None when nothing reached it."""
    total, seen = 0, False
    for lg in rc.get("logs", []):
        t = lg.get("topics", [])
        if (lg["address"].lower() == C.TOKEN and len(t) == 3 and t[0] == TRANSFER_TOPIC
                and addr_from_topic(t[2]) == C.DEAD):
            total += _word(lg["data"])
            seen = True
    return total / 1e18 if seen else None


def gold_in(rc, wallet):
    """GLD delivered to the wallet in this receipt, in whole GLD, or None when none arrived."""
    total, seen = 0, False
    for lg in rc.get("logs", []):
        t = lg.get("topics", [])
        if (lg["address"].lower() == C.GLD and len(t) == 3 and t[0] == TRANSFER_TOPIC
                and addr_from_topic(t[2]) == wallet.lower()):
            total += _word(lg["data"])
            seen = True
    return total / 1e18 if seen else None


def working(max_s=None):
    """True while a transaction the worm sent is still in flight: the digging waits for it."""
    return BUSY_SINCE > 0 and time.time() - BUSY_SINCE < (BUSY_MAX_S if max_s is None else max_s)


def watch(action, text, tx=None, done=False):
    """Tell the watchers what the worm is doing: a transaction just broadcast (done=False) or settled (done=True).
    Marks the worm busy in between, so no dig starts while money is moving. Never raises: watching is a
    courtesy, not a step of the money path."""
    global BUSY_SINCE
    BUSY_SINCE = 0.0 if done else time.time()
    if not WATCH:
        return
    try:
        WATCH({"action": action, "text": text, "tx": tx, "token": C.TOKEN or None, "done": done, "ts": int(time.time())})
    except Exception as e:
        log.info("watch failed: %s", e)


def _event_text(kind, amount, row):
    if kind == "claim":
        return f"claimed {amount:.2f} USDG of creator fees"
    if kind == "burn":
        return f"burned {row.get('qty') or 0:,.0f} $WORM bought with {amount:.2f} USDG"
    if kind == "forward":
        return f"forwarded {amount:.2f} USDG ({int(C.OWNER_SHARE * 100)}%) to the creator"
    if kind == "gold":
        return f"bought {row.get('qty') or 0:.4f} GLD of gold with {amount:.2f} USDG for its reserve"
    if kind == "compute":
        return f"paid {amount:.2f} USDG of compute to AI Surplus"
    return f"{kind}: {amount:.2f} USDG"


def settle(db, row, rc, wallet, to=None):
    """Apply a receipt to a '<kind>_pending' ledger row and write the event. Returns (kind, amount)."""
    base = row["kind"].removesuffix("_pending")
    ok = rc.get("status") == "0x1"
    amount, why, qty = row["amount"], "reverted", None
    to = row.get("to_addr") or to                  # the recipient it was sent to, not whoever is configured now
    if ok and base == "claim":
        got = claimed_in(rc, wallet)               # what the escrow actually paid, not the pre-tx read
        if got is None:
            _say_hourly(db, 'error', f'claim receipt incomplete; funds reserved pending verification: {row["tx"]}')
            return row['kind'], row['amount']
        amount = got
    elif ok and base == "burn":
        qty = burned_in(rc)                        # the tokens that reached the burn address, from the receipt
        ok, why = qty is not None, "no $WORM reached the burn address"
    elif ok and base == "gold":
        qty = gold_in(rc, wallet)                  # the GLD that reached the wallet, from the receipt
        ok, why = qty is not None, "no GLD reached the wallet"
    elif ok:
        ok = transferred(rc, C.USDG, wallet, to, units(row["amount"]))
        why = "no Transfer log for the amount"
    if rc.get('status') not in ('0x0', '0x1') or (rc.get('status') == '0x1' and not ok):
        _say_hourly(db, 'error', f'{base} receipt evidence incomplete; retained pending: {row["tx"]}')
        return row['kind'], row['amount']
    kind = base if ok else f"{base}_failed"
    note = row["note"] if ok else f"{row['note']} ({why})"
    db.x("UPDATE ledger SET kind=?, amount=?, note=?, qty=? WHERE id=?", (kind, amount, note, qty, row["id"]))
    if ok:
        text = _event_text(kind, amount, dict(row, qty=qty))
        db.add_event("treasury", text)
        watch(base, text, row["tx"], done=True)
    else:
        db.add_event("error", f"{base} {why}: {row['tx']}")
        watch(base, f"{base} did not go through: {why}", row["tx"], done=True)
    return kind, amount


def finish(db, h, rc, wallet, to=None, fallback=None):
    """Settle the pending row written at broadcast for hash h (send_tx's on_broadcast). fallback: the row's
    (kind, amount, note, to_addr) to write now if the broadcast callback failed to write it, so a transfer
    that left the wallet is never missing from the ledger (and never sent twice for lack of a row)."""
    row = db.one("SELECT * FROM ledger WHERE tx=? AND kind LIKE '%_pending' ORDER BY id DESC LIMIT 1", (h,))
    if not row and fallback:
        db.add_event("error", f"the ledger row for {h} was not written at broadcast; rebuilt from the receipt")
        db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note,to_addr) VALUES(?,?,?,?,?,?,?)",
             (int(time.time()), fallback["kind"], "USDG", fallback["amount"], h, fallback["note"], fallback.get("to_addr")))
        row = db.one("SELECT * FROM ledger WHERE tx=? AND kind LIKE '%_pending' ORDER BY id DESC LIMIT 1", (h,))
    if not row:
        db.add_event("error", f"no pending ledger row for {h}")
        return None, None
    return settle(db, row, rc, wallet, to)


def reconcile(rpc, db, kinds, wallet, to_for=None):
    """Settle verified receipts; preserve unknown outcomes regardless of age.
    to_for(row) supplies the expected recipient. Returns unresolved chain rows."""
    still = 0
    marks = ",".join("?" * len(kinds))
    for row in db.q(f"SELECT * FROM ledger WHERE kind IN ({marks}) ORDER BY id", tuple(kinds)):
        if not row["tx"]:                          # not a chain transaction of ours (an x402 payment in flight): compute's own business
            continue
        rc = finality.receipt(rpc, row["tx"])
        if rc:
            kind, _ = settle(db, row, rc, wallet, to_for(row) if to_for else None)
            if kind.endswith('_pending'):
                still += 1
        else:
            still += 1
    return still


# ---- the cycle ----------------------------------------------------------------

def summary(rpc, db):
    ensure_tables(db)
    out = {"wallet": C.WALLET or None, "owner": C.OWNER_WALLET or None, "share": C.OWNER_SHARE, "token": C.TOKEN or None,
           "burn_share": C.BURN_SHARE, "gold_share": C.GOLD_SHARE, "ops_share": C.OPS_SHARE, "trading": C.TRADING,
           "burn_min_usd": MIN_BURN_USD, "gold_min_usd": MIN_GOLD_USD,
           "live": C.LIVE, "claimable_usdg": None, "usdg": None, "eth": None,
           "claimed_total": 0.0, "forwarded_total": 0.0, "compute_total": 0.0, "burned_total": 0.0, "burned_qty": 0.0,
           "gold_total": 0.0, "gold_qty": 0.0, "gold_held": None, "gold_usd": None,
           "owed_to_owner": 0.0, "owed_to_burn": 0.0, "owed_to_gold": 0.0, "burn_state": "", "gold_state": "", "ledger": []}
    try:
        out['claim_policy'] = json.loads(db.meta_get('claim_policy_status') or '{}')
        out['fee_sweep'] = json.loads(db.meta_get('fee_sweep_status') or '{}')
        out['gas_refill'] = json.loads(db.meta_get('gas_refill_status') or '{}')
    except (ValueError, TypeError):
        out['claim_policy'] = {'allowed': False, 'reason': 'claim policy status unavailable'}
    if C.WALLET:
        try:
            out["claimable_usdg"] = round(claimable_usdg(rpc, C.WALLET), 4)
            out["usdg"] = round(usdg_balance(rpc, C.WALLET), 4)
            out["eth"] = round(int(rpc.call("eth_getBalance", [C.WALLET, "latest"]), 16) / 1e18, 6)
        except Exception as e:
            log.info("treasury read failed: %s", e)
        try:
            out["gold_held"] = round(gold_held(rpc, C.WALLET), 6)
            out["gold_usd"] = round(out["gold_held"] * gold_price_usd(rpc), 2)
        except Exception as e:
            log.info("gold read failed: %s", e)
    for r in db.q("SELECT kind, COALESCE(SUM(amount),0) s FROM ledger GROUP BY kind"):
        if r["kind"] == "claim":
            out["claimed_total"] = round(r["s"], 4)
        elif r["kind"] == "forward":
            out["forwarded_total"] = round(r["s"], 4)
        elif r["kind"] == "compute":
            out["compute_total"] = round(r["s"], 4)
        elif r["kind"] == "burn":
            out["burned_total"] = round(r["s"], 4)
        elif r["kind"] == "gold":
            out["gold_total"] = round(r["s"], 4)
    out["burned_qty"] = round(db.one("SELECT COALESCE(SUM(qty),0) q FROM ledger WHERE kind='burn'")["q"] or 0.0, 2)
    out["gold_qty"] = round(db.one("SELECT COALESCE(SUM(qty),0) q FROM ledger WHERE kind='gold'")["q"] or 0.0, 6)
    try:
        out["owed_to_owner"] = round(max(0.0, owed_to_owner(db)), 4)
        out["owed_to_burn"] = round(max(0.0, owed_to_burn(db)), 4)
        out["owed_to_gold"] = round(max(0.0, owed_to_gold(db)), 4)
        out["burn_state"] = burn_state(out["owed_to_burn"])
        out["gold_state"] = gold_state(out["owed_to_gold"])
    except RuntimeError as e:
        log.warning("accounting summary unavailable: %s", e)
        out.update(owed_to_owner=None, owed_to_burn=None, owed_to_gold=None,
                   burn_state='paused: accounting needs review', gold_state='paused: accounting needs review',
                   accounting_error="accounting unavailable; operator review required")
    out["ledger"] = db.q("SELECT id,ts,kind,asset,amount,tx,note,qty FROM ledger ORDER BY id DESC LIMIT 20")
    return out


def claim(rpc, db, acct, claimable):
    from .tx import send_tx

    note = "creator fees claimed from pons escrow"

    def pending(h):
        db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note,owner_share,burn_share,gold_share) VALUES(?,?,?,?,?,?,?,?,?)",
             (int(time.time()), "claim_pending", "USDG", claimable, h, note, C.OWNER_SHARE, C.BURN_SHARE, C.GOLD_SHARE))
        watch("claim", f"claiming {claimable:.2f} USDG of creator fees from the pons escrow", h)

    try:
        h, rc = send_tx(rpc, acct, C.FEE_ESCROW, call_data("claimToken(address)", ("address",), (C.USDG,)),
                        on_broadcast=pending)
    except ReceiptPending:
        db.add_event('treasury', 'fee claim submitted; waiting for chain finality')
        return False
    except Exception as e:
        log.warning("fee claim failed: %s", e)
        db.add_event("error", "fee claim failed; see private logs")
        return False
    return finish(db, h, rc, C.WALLET, fallback={"kind": "claim_pending", "amount": claimable, "note": note})[0] == "claim"


def forward(rpc, db, acct):
    """Send the owner what is owed, as far as the wallet's USDG goes; the rest stays owed."""
    if not C.OWNER_WALLET:
        return False
    owed = owed_to_owner(db)
    if owed < MIN_FORWARD_USD:
        return False
    try:
        have = usdg_balance(rpc, C.WALLET)
    except Exception as e:
        log.info("usdg balance read failed: %s", e)
        return False
    n = units(min(owed, have))
    if n < units(MIN_FORWARD_USD):
        return False
    from .tx import send_tx
    data = selector("transfer(address,uint256)") + encode(["address", "uint256"], [C.OWNER_WALLET, n]).hex()

    note, to = f"{int(C.OWNER_SHARE * 100)}% of income to the creator", C.OWNER_WALLET

    def pending(h):
        db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note,to_addr) VALUES(?,?,?,?,?,?,?)",
             (int(time.time()), "forward_pending", "USDG", n / 1e6, h, note, to))
        watch("forward", f"sending {n / 1e6:.2f} USDG, the creator's {int(C.OWNER_SHARE * 100)}%, to the creator's wallet", h)

    try:
        h, rc = send_tx(rpc, acct, C.USDG, data, on_broadcast=pending)
    except ReceiptPending:
        db.add_event('treasury', 'forward submitted; waiting for chain finality')
        return False
    except Exception as e:
        log.warning("forward failed: %s", e)
        db.add_event("error", "forward failed; see private logs")
        return False
    return finish(db, h, rc, C.WALLET, to=to,
                  fallback={"kind": "forward_pending", "amount": n / 1e6, "note": note, "to_addr": to})[0] == "forward"


def burn_state(owed):
    """One line for the page: why nothing has burned yet, or that a burn is due."""
    if not C.TOKEN:
        return "waits for the token"
    if owed < MIN_BURN_USD:
        return f"burns once ${MIN_BURN_USD:.0f} is owed"
    return "due: burns on the next cycle once $WORM has its pool"


def _say_hourly(db, kind, text):
    last = db.one("SELECT ts FROM events WHERE text=? ORDER BY id DESC LIMIT 1", (text[:300],))
    if last and time.time() - last["ts"] < 3600:
        return
    db.add_event(kind, text)


# ---- the burn: buy $WORM on its pool and send it to the burn address in one transaction ----

def router_allowance(rpc, wallet):
    """What a Universal Router pull of USDG needs: (USDG allowance to Permit2, Permit2 allowance to the router,
    its expiry)."""
    a = call_fn(rpc, C.USDG, "allowance(address,address)", ("uint256",), ("address", "address"), (wallet, C.PERMIT2)) or 0
    amt, exp, _nonce = call_fn(rpc, C.PERMIT2, "allowance(address,address,address)", ("uint160", "uint48", "uint48"),
                               ("address", "address", "address"), (wallet, C.USDG, C.UNIVERSAL_ROUTER))
    return int(a), int(amt), int(exp)


def approve_for_router(rpc, db, acct, need):
    """Exact input allowance at both layers, including reducing legacy unlimited grants.
    Read back both approvals: a successful receipt alone does not prove ERC-20 approval."""
    from .tx import send_tx
    if not 0 < need < 2 ** 160 - 1:
        raise ValueError('invalid bounded approval amount')
    a, amt, exp = router_allowance(rpc, C.WALLET)
    if a != need:
        data = selector("approve(address,uint256)") + encode(["address", "uint256"], [C.PERMIT2, need]).hex()
        h, rc = send_tx(rpc, acct, C.USDG, data)
        if not rc or rc.get("status") != "0x1":
            raise RuntimeError(f"USDG approval for Permit2 reverted: {h}")
        db.add_event("treasury", f"approved USDG for Permit2 for this burn: {h}")
    now = int(time.time())
    if amt != need or not now + SWAP_TTL_S <= exp <= now + PERMIT_TTL_S:
        data = selector("approve(address,address,uint160,uint48)") + encode(
            ["address", "address", "uint160", "uint48"], [C.USDG, C.UNIVERSAL_ROUTER, need, now + PERMIT_TTL_S]).hex()
        h, rc = send_tx(rpc, acct, C.PERMIT2, data)
        if not rc or rc.get("status") != "0x1":
            raise RuntimeError(f"Permit2 approval for the router reverted: {h}")
        db.add_event("treasury", f"approved the Universal Router for this burn with a short expiry: {h}")
    a, amt, exp = router_allowance(rpc, C.WALLET)
    now = int(time.time())
    if a != need or amt != need or not now + SWAP_TTL_S <= exp <= now + PERMIT_TTL_S:
        raise RuntimeError('bounded burn approvals could not be verified')
    return exp


def burn_calldata(pk, zero_for_one, amount_in, min_out, deadline):
    """One Universal Router call: swap USDG for $WORM on its pool and take the tokens straight to the burn
    address. The swap reverts below min_out, so a burn either happens whole or not at all."""
    from .trader import SETTLE_ALL, SWAP_EXACT_IN_SINGLE, SWAP_T, V4_SWAP, _key_tuple
    actions = SWAP_EXACT_IN_SINGLE + SETTLE_ALL + TAKE
    params = [encode([SWAP_T], [(_key_tuple(pk), zero_for_one, amount_in, min_out, 0, b"")]),
              encode(["address", "uint256"], [C.USDG, amount_in]),
              encode(["address", "address", "uint256"], [C.TOKEN, C.DEAD, 0])]
    inputs = [encode(["bytes", "bytes[]"], [actions, params])]
    return selector("execute(bytes,bytes[],uint256)") + encode(["bytes", "bytes[]", "uint256"],
                                                             [V4_SWAP, inputs, deadline]).hex()


def burn(rpc, db, acct):
    """Buy $WORM with the owed burn share and send it to the burn address, once at least MIN_BURN_USD is owed
    and the token has graduated to a pool (a curve buy is not a pool swap). Returns True on a settled burn."""
    if not C.TOKEN:
        return False
    owed = owed_to_burn(db)
    if owed < MIN_BURN_USD:
        return False
    try:
        have = usdg_balance(rpc, C.WALLET)
    except Exception as e:
        log.info("usdg balance read failed: %s", e)
        return False
    n = units(min(owed, have))
    if n < units(MIN_BURN_USD):
        return False
    from . import trader
    trader.ensure_tables(db)                     # the pools table is the trader's; the burn may run first
    try:
        pk = trader.pool_key(rpc, db, C.TOKEN)
    except Exception as e:
        log.warning("burn: pool lookup failed: %s", e)
        db.add_event("error", "burn: pool lookup failed; see private logs")
        return False
    if not pk:
        _say_hourly(db, "treasury", f"${owed:.2f} waits to be burned: $WORM has not graduated to a pool yet")
        return False
    if pk["quote"] != C.USDG:
        db.add_event("error", "burn: $WORM's pool is not quoted in USDG; the burn share stays owed")
        return False
    try:
        out, _gas, zfo = trader.quote_buy(rpc, pk, C.TOKEN, n)
    except Exception as e:
        log.warning("burn: quote failed: %s", e)
        db.add_event("error", "burn: quote failed; see private logs")
        return False
    if not out:
        db.add_event("error", "burn: no liquidity quoted; the burn share stays owed")
        return False
    try:
        expiry = approve_for_router(rpc, db, acct, n)
    except Exception as e:
        log.warning("burn: approval failed: %s", e)
        db.add_event("error", "burn: approval failed; see private logs")
        return False
    # Approvals may take minutes. Never reuse the quote obtained before them.
    quoted_at = time.time()
    try:
        out, _gas, zfo = trader.quote_buy(rpc, pk, C.TOKEN, n)
        if out <= 0:
            raise ValueError('no liquidity in refreshed quote')
        deadline = min(int(quoted_at) + SWAP_TTL_S, expiry)
        min_out = max(1, int(out * (1 - BURN_SLIPPAGE)))
        data = burn_calldata(pk, zfo, n, min_out, deadline)
    except Exception:
        log.exception('burn quote refresh failed')
        db.add_event('error', 'burn: fresh quote unavailable; the burn share stays owed')
        return False
    from .tx import send_tx

    note = f"{int(round(C.BURN_SHARE * 100))}% of income: buy $WORM on its pool and send it to the burn address"

    def pending(h):
        db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)",
             (int(time.time()), "burn_pending", "USDG", n / 1e6, h, note))
        watch("burn", f"buying $WORM with {n / 1e6:.2f} USDG on its pool and sending it to the burn address", h)

    try:
        h, rc = send_tx(rpc, acct, C.UNIVERSAL_ROUTER, data, on_broadcast=pending,
                        valid_until=min(quoted_at + QUOTE_MAX_AGE_S, deadline))
    except ReceiptPending:
        db.add_event('treasury', 'burn submitted; waiting for chain finality')
        return False
    except Exception as e:
        log.warning("burn failed: %s", e)
        db.add_event("error", "burn failed; see private logs")
        return False
    return finish(db, h, rc, C.WALLET, fallback={"kind": "burn_pending", "amount": n / 1e6, "note": note})[0] == "burn"


# ---- gold: buy tokenized gold (GLD) with the gold share and keep it as a reserve ----

def gold_held(rpc, wallet):
    v = call_fn(rpc, C.GLD, "balanceOf(address)", ("uint256",), ("address",), (wallet,))
    return (v or 0) / 1e18


def _quote_v3(rpc, token_in, token_out, amount_in):
    """QuoterV2.quoteExactInputSingle on the GLD/USDG v3 pool: the amount out, or 0 when nothing is quoted."""
    data = selector(f"quoteExactInputSingle({QUOTE_V3_T})") + encode(
        [QUOTE_V3_T], [(token_in, token_out, amount_in, C.GOLD_POOL_FEE, 0)]).hex()
    raw = rpc.eth_call(C.QUOTER_V3, data)
    out, _price_after, _ticks, _gas = decode(["uint256", "uint160", "uint32", "uint256"], bytes.fromhex(raw[2:]))
    return int(out)


def quote_gold(rpc, amount_in):
    """GLD (18 decimals) for amount_in USDG units, from the pool."""
    return _quote_v3(rpc, C.USDG, C.GLD, amount_in)


def gold_price_usd(rpc):
    """What one GLD sells for in USDG on the pool, cached for a few minutes: the reserve's display value."""
    global _gold_price
    if time.time() - _gold_price[0] < GOLD_PRICE_TTL_S and _gold_price[1]:
        return _gold_price[1]
    price = _quote_v3(rpc, C.GLD, C.USDG, 10 ** 18) / 1e6
    _gold_price = (time.time(), price)
    return price


def gold_calldata(amount_in, min_out, recipient, deadline):
    """One SwapRouter02 call: an exact-input Uniswap v3 swap of USDG for GLD on the 0.05% pool, the GLD
    delivered to the wallet, wrapped in its deadline-checked multicall. The swap reverts below min_out."""
    swap = selector(f"exactInputSingle({SWAP_V3_T})") + encode(
        [SWAP_V3_T], [(C.USDG, C.GLD, C.GOLD_POOL_FEE, recipient, amount_in, min_out, 0)]).hex()
    return call_data('multicall(uint256,bytes[])', ('uint256', 'bytes[]'),
                     (deadline, [bytes.fromhex(swap[2:])]))


def approve_for_gold(rpc, db, acct, need):
    """SwapRouter02 may spend exactly this buy's USDG; reduce excess allowances too."""
    from .tx import send_tx
    have = call_fn(rpc, C.USDG, "allowance(address,address)", ("uint256",), ("address", "address"), (C.WALLET, C.SWAP_ROUTER_V3)) or 0
    if not 0 < need < 2 ** 256 - 1:
        raise ValueError('invalid bounded approval amount')
    if int(have) == need:
        return
    data = selector("approve(address,uint256)") + encode(["address", "uint256"], [C.SWAP_ROUTER_V3, need]).hex()
    h, rc = send_tx(rpc, acct, C.USDG, data)
    if not rc or rc.get("status") != "0x1":
        raise RuntimeError(f"USDG approval for the gold swap reverted: {h}")
    have = call_fn(rpc, C.USDG, "allowance(address,address)", ("uint256",), ("address", "address"), (C.WALLET, C.SWAP_ROUTER_V3))
    if have != need:
        raise RuntimeError('bounded gold approval could not be verified')


def gold_state(owed):
    """One line for the page: why nothing was bought yet, or that a buy is due."""
    if owed < MIN_GOLD_USD:
        return f"buys gold once ${MIN_GOLD_USD:.0f} is owed"
    return "due: buys gold on the next cycle"


def gold(rpc, db, acct):
    """Buy GLD with the owed gold share once at least MIN_GOLD_USD is owed. Returns True on a settled buy."""
    owed = owed_to_gold(db)
    if owed < MIN_GOLD_USD:
        return False
    try:
        have = usdg_balance(rpc, C.WALLET)
    except Exception as e:
        log.info("usdg balance read failed: %s", e)
        return False
    n = units(min(owed, have))
    if n < units(MIN_GOLD_USD):
        return False
    try:
        out = quote_gold(rpc, n)
    except Exception as e:
        log.warning("gold quote failed: %s", e)
        _say_hourly(db, "error", "gold: quote failed; see private logs")
        return False
    if not out:
        _say_hourly(db, "error", "gold: no liquidity quoted; the gold share stays owed")
        return False
    try:
        approve_for_gold(rpc, db, acct, n)
    except Exception as e:
        log.warning("gold: approval failed: %s", e)
        db.add_event("error", "gold: approval failed; see private logs")
        return False
    quoted_at = time.time()
    try:
        out = quote_gold(rpc, n)
        if out <= 0:
            raise ValueError('no liquidity in refreshed quote')
        min_out = max(1, int(out * (1 - GOLD_SLIPPAGE)))
        data = gold_calldata(n, min_out, C.WALLET, int(quoted_at) + SWAP_TTL_S)
    except Exception:
        log.exception('gold quote refresh failed')
        db.add_event('error', 'gold: fresh quote unavailable; the gold share stays owed')
        return False
    from .tx import send_tx

    note = f"{int(round(C.GOLD_SHARE * 100))}% of income: buy gold (GLD) for the reserve"

    def pending(h):
        db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)",
             (int(time.time()), "gold_pending", "USDG", n / 1e6, h, note))
        watch("gold", f"buying gold (GLD) for its reserve with {n / 1e6:.2f} USDG", h)

    try:
        h, rc = send_tx(rpc, acct, C.SWAP_ROUTER_V3, data, on_broadcast=pending,
                        valid_until=quoted_at + QUOTE_MAX_AGE_S)
    except ReceiptPending:
        db.add_event('treasury', 'gold purchase submitted; waiting for chain finality')
        return False
    except Exception as e:
        log.warning("gold buy failed: %s", e)
        db.add_event("error", "gold buy failed; see private logs")
        return False
    return finish(db, h, rc, C.WALLET, fallback={"kind": "gold_pending", "amount": n / 1e6, "note": note})[0] == "gold"


def cycle(rpc, db, acct):
    """Settle what is in flight, claim fees when worth it, forward the creator's share, then burn, then buy gold.
    Only ever runs armed (WH_LIVE=1)."""
    ensure_tables(db)
    if not (C.LIVE and acct and C.WALLET):
        return
    try:
        pin_claim_shares(db)
        if reconcile(rpc, db, ("claim_pending", "forward_pending", "burn_pending", "gold_pending", "compute_pending"), C.WALLET,
                     lambda r: C.AISURPLUS_DEPOSIT if r["kind"].startswith("compute") else C.OWNER_WALLET):
            return                      # something is still in flight: settle it before sending more
    except Exception as e:
        log.warning("ledger reconcile failed: %s", e)
        db.add_event("error", "ledger reconcile failed; see private logs")
        return
    from . import gas_refill
    if not gas_refill.cycle(rpc, db, acct):
        return
    try:
        claimable = claimable_usdg(rpc, C.WALLET)
    except Exception as e:
        log.info("claimable read failed: %s", e)
        return
    from . import claim_policy, fee_sweep
    if not fee_sweep.cycle(rpc, db, acct, claimable):
        return
    try:
        claimable = claimable_usdg(rpc, C.WALLET)
    except Exception:
        return
    decision = claim_policy.evaluate(rpc, db, claimable)
    claim_policy.remember(db, decision)
    if decision["allowed"]:
        if not claim(rpc, db, acct, claimable):
            return
        db.meta_set("claim_balance_since", "")
    for action in (forward, burn, gold):
        if db.one("SELECT 1 FROM ledger WHERE kind LIKE '%_pending' AND tx IS NOT NULL"):
            return
        action(rpc, db, acct)
