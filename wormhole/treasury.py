"""Fee accounting and treasury actions.

Claims retain their original allocation policy. Outstanding obligations and pending spends are
reserved; an accounting error pauses spending. Pending records are persisted before submission.
Successful receipts require event evidence, confirmed reverts release obligations, and unknown
outcomes remain pending until reconciliation. New chain submissions use the private durable outbox.
"""
import logging
import json
import math
import os
import random
import time

from eth_abi import decode, encode

from . import finality, outbox
from .tx import ReceiptPending
from . import config as C
from .chain import addr_from_topic, call_data, call_fn, selector, topic

log = logging.getLogger("wormhole.treasury")


def _knob(name, default, low, high):
    """A treasury setting from the environment, checked once at start: a typo stops the process instead of
    buying at the wrong size or pace."""
    try:
        v = float(os.environ.get(name, default))
    except ValueError:
        raise SystemExit(f"{name} must be a number") from None
    if not (math.isfinite(v) and low <= v <= high):
        raise SystemExit(f"{name} must be between {low:g} and {high:g}")
    return v


MIN_FORWARD_USD = 0.50
WATCH = None               # set by run.py: callable(ev) that tells the screen and the page what the worm is doing right now
BUSY_SINCE = 0.0           # when the transaction now in flight was broadcast; 0 when none is
BUSY_MAX_S = 120           # a transaction that never settles does not stop the digging for longer than this
# The burn buys small and often: no slice above BURN_MAX_USD, no two burns closer than BURN_EVERY_MIN minutes
# plus a random 0-50% (so the moment cannot be predicted and traded against), and no slice whose own price
# impact on the pool, measured against a tiny reference quote, exceeds BURN_MAX_IMPACT. The last $249 single
# burn moved the pool about 14%; a $25 slice on the same pool moves it about 1.4%.
MIN_BURN_USD = _knob("WH_MIN_BURN_USD", "2.0", 0.5, 1000)        # below this a slice waits: gas stays a small part of it
BURN_MAX_USD = _knob("WH_BURN_MAX_USD", "25", MIN_BURN_USD, 10000)
BURN_EVERY_MIN = _knob("WH_BURN_EVERY_MIN", "180", 10, 10080)
BURN_JITTER = 0.5          # the spacing is BURN_EVERY_MIN times 1 to 1.5, drawn per burn
BURN_MAX_IMPACT = _knob("WH_BURN_MAX_IMPACT", "0.02", 0.001, 0.2)
# The quote the swap is checked against already includes the pool fee, the pons hook fee and the token's
# creator tax (the hook takes them inside the swap and the v4 quoter runs the hook: the quoted round trip rises
# with the tax, see CLAUDE_HANDOFF 2026-09-19), and the slice's own impact. What is left for this tolerance is
# other swaps landing between the refreshed quote (at most QUOTE_MAX_AGE_S old at signing) and inclusion. 2% of
# a $25 slice caps what a bad fill can cost at $0.50; a tighter bound mostly adds reverts, which cost gas and
# leave the slice owed for the next burn.
BURN_SLIPPAGE = _knob("WH_BURN_SLIPPAGE", "0.02", 0.005, 0.05)
MIN_SWEEP_USD = 1.0        # realised trading profit is swept to the burn once it is this far above the high-water mark
_rng = random.SystemRandom()   # burn spacing: not reproducible from anything public
BURN_HISTORY_MAX = 200     # burns listed in the public snapshot
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
PRIVATE_CLAIM_KEYS = ("next_claim_after", "balance_since")        # claim timing: kept in meta, left out of the page
TRANSFER_TOPIC = topic("Transfer(address,address,uint256)")


def ensure_tables(db):
    db.x("CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT, asset TEXT,"
         " amount REAL, tx TEXT, note TEXT, qty REAL)")
    # tokens burned / bought; the recipient a transfer was sent to; the fee split a claim was made under; the
    # surplus burn program a release or a burn belongs to, and how much of a burn was program money; the ETH its
    # transactions paid in gas (a burn's approvals included)
    for col in ("qty REAL", "to_addr TEXT", "authorization TEXT", "owner_share REAL", "burn_share REAL", "gold_share REAL",
                "program TEXT", "program_usd REAL", "gas_eth REAL"):
        name = col.split()[0]
        if name not in {r['name'] for r in db.q('PRAGMA table_info(ledger)')}:
            db.x(f"ALTER TABLE ledger ADD COLUMN {col}")


def tx_fee_eth(rc):
    """ETH a mined transaction paid: gas used times the effective price, plus any L1 data fee. 0 when unknown."""
    try:
        fee = int(rc.get("gasUsed") or "0x0", 16) * int(rc.get("effectiveGasPrice") or "0x0", 16)
        return (fee + int(rc.get("l1Fee") or "0x0", 16)) / 1e18
    except (AttributeError, TypeError, ValueError):
        return 0.0


def carry_burn_gas(db, rc):
    """A burn approval's gas, kept until the burn row it belongs to is written (the approval can succeed and the
    burn still wait), so every ETH a burn spent is booked on a burn row."""
    fee = tx_fee_eth(rc) if rc else 0.0
    if fee > 0:
        db.meta_set("burn_gas_carry", repr(float(db.meta_get("burn_gas_carry") or 0) + fee))


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
    """The burn share of every claim so far plus the trading profit swept to the burn and the surplus the burn
    program released (wormhole/burn_program.py), minus what was burned or is on its way, in USDG."""
    pin_claim_shares(db)
    c = db.one("SELECT COALESCE(SUM(amount * burn_share),0) s FROM ledger WHERE kind='claim'")["s"]
    t = db.one("SELECT COALESCE(SUM(amount),0) s FROM ledger WHERE kind IN ('trade_profit','surplus_burn')")["s"]
    b = db.one("SELECT COALESCE(SUM(amount),0) s FROM ledger WHERE kind IN ('burn','burn_pending')")["s"]
    return c + t - b


def sweep_trading_profit(db):
    """Realised trading profit goes to the burn, above a high-water mark: the live book's lifetime realised
    result (closed positions, gas and failed attempts included) is compared with the highest level already
    swept, and only the part above it is owed to the burn. After a drawdown nothing is swept until the old
    high is passed again, so a win is never burnt while an earlier loss is still unrecovered. Bookkeeping
    only: the burn itself is the usual swap once MIN_BURN_USD is owed. Returns the USDG newly owed."""
    from . import live_trading
    ensure_tables(db)
    if not db.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='positions'"):
        return 0.0
    try:
        share = float(os.environ.get("WH_TRADING_BURN_SHARE", "1.0"))
    except ValueError:
        share = 1.0
    share = share if 0 <= share <= 1 else 1.0
    pnl = live_trading.realized_pnl(db)
    high = float(db.meta_get("trading_profit_high_water", "0") or 0)
    gain = pnl - high
    if gain < MIN_SWEEP_USD:
        return 0.0
    owed = round(gain * share, 6)
    with db.transaction():
        db.meta_set("trading_profit_high_water", repr(pnl))
        if owed > 0:
            db.x("INSERT INTO ledger(ts,kind,asset,amount,note) VALUES(?,?,?,?,?)",
                 (int(time.time()), "trade_profit", "USDG", owed,
                  f"realised trading profit above the high-water mark (${high:.2f} -> ${pnl:.2f}); {share * 100:.0f}% owed to the burn"))
    if owed > 0:
        db.add_event("treasury", f"trading profit ${gain:.2f} above the high-water mark: ${owed:.2f} owed to the burn")
    return owed


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


def claim_without_event(rc, wallet):
    """A successful claim receipt that lacks the escrow's ClaimedToken log: the USDG that moved decides.
    Returns (amount, evidence): the sum of the escrow's USDG transfers to the wallet; 0.0 when no USDG reached
    the wallet at all (a USDG balance cannot change without a Transfer log, so nothing was paid); None when
    USDG reached the wallet from some other address in the same transaction, which this cannot account for."""
    paid, other = 0, 0
    for lg in rc.get("logs", []):
        t = lg.get("topics", [])
        if (lg.get("address", "").lower() == C.USDG and len(t) == 3 and t[0] == TRANSFER_TOPIC
                and addr_from_topic(t[2]) == wallet.lower()):
            if addr_from_topic(t[1]) == C.FEE_ESCROW:
                paid += _word(lg["data"])
            else:
                other += 1
    if other:
        return None, "USDG reached the wallet from another address in the claim transaction"
    if paid:
        return paid / 1e6, "amount from the escrow's USDG transfer; it emitted no ClaimedToken"
    return 0.0, "the escrow paid nothing: no ClaimedToken and no USDG transfer"


def _review(db, tx, reason=None):
    """Keep private health's list of payments waiting on receipt evidence: reason=None takes tx off it."""
    try:
        items = json.loads(db.meta_get("payment_review") or "{}")
    except ValueError:
        items = {}
    if reason is None:
        if tx not in items:
            return
        items.pop(tx)
    elif tx in items and items[tx]["reason"] == reason:
        return
    else:
        items[tx] = {"reason": reason, "since": items.get(tx, {}).get("since") or int(time.time())}
    db.meta_set("payment_review", json.dumps(items))


def settle(db, row, rc, wallet, to=None):
    """Apply a receipt to a '<kind>_pending' ledger row and write the event. Returns (kind, amount)."""
    base = row["kind"].removesuffix("_pending")
    ok = rc.get("status") == "0x1"
    amount, why, qty, extra = row["amount"], "reverted", None, ""
    to = row.get("to_addr") or to                  # the recipient it was sent to, not whoever is configured now
    if ok and base == "claim":
        got = claimed_in(rc, wallet)               # what the escrow actually paid, not the pre-tx read
        if got is None:
            got, evidence = claim_without_event(rc, wallet)
            if got is None:
                _review(db, row["tx"], f"claim receipt incomplete: {evidence}")
                _say_hourly(db, 'error', f'claim receipt incomplete; funds reserved pending verification: {row["tx"]}')
                return row['kind'], row['amount']
            if got <= 0:
                ok, why = False, evidence
            else:
                extra = f" ({evidence})"
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
    if rc.get('status') not in ('0x0', '0x1') or (rc.get('status') == '0x1' and not ok and base != "claim"):
        _review(db, row["tx"], f"{base} receipt evidence incomplete: "
                               f"{why if rc.get('status') == '0x1' else 'invalid receipt status'}")
        _say_hourly(db, 'error', f'{base} receipt evidence incomplete; retained pending: {row["tx"]}')
        return row['kind'], row['amount']
    kind = base if ok else f"{base}_failed"
    note = ((row["note"] or "") + extra if extra else row["note"]) if ok else f"{row['note']} ({why})"
    db.x("UPDATE ledger SET kind=?, amount=?, note=?, qty=?, gas_eth=COALESCE(gas_eth,0)+? WHERE id=?",
         (kind, amount, note, qty, tx_fee_eth(rc), row["id"]))
    _review(db, row["tx"])
    if ok:
        text = _event_text(kind, amount, dict(row, qty=qty))
        db.add_event("treasury", text)
        watch(base, text, row["tx"], done=True)
    else:
        db.add_event("error", f"{base} {why}: {row['tx']}")
        watch(base, f"{base} did not go through: {why}", row["tx"], done=True)
    return kind, amount


def finish(db, h, rc, wallet, to=None):
    """Settle the pending row written at broadcast for hash h (send_tx's on_broadcast). The row always exists:
    send_tx submits only after the callback wrote it, and raises before any broadcast when it could not. A
    missing row therefore means something else settled it already; nothing is rebuilt (a rebuilt claim would
    lack its fee split, a rebuilt pending row would reserve the money twice)."""
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
        elif outbox.gone(row["tx"]):
            unsent(db, row, *outbox.gone(row["tx"]))
        else:
            still += 1
    return still


def unsent(db, row, end, why):
    """A pending row whose transaction the journal proves never executes (tx.recover closed it from chain
    evidence: 'abandoned' before broadcast, or 'dropped' because our own settled transaction used its nonce).
    The row becomes '<kind>_failed', so the amount is owed again and is sent at most once more."""
    base = row["kind"].removesuffix("_pending")
    text = "never broadcast" if end == "abandoned" else "never mined"
    db.x("UPDATE ledger SET kind=?, note=? WHERE id=? AND kind=?",
         (f"{base}_failed", f"{row['note']} ({text})", row["id"], row["kind"]))
    _review(db, row["tx"])
    log.warning("%s %s %s: %s", base, row["tx"], text, why)
    db.add_event("error", f"{base} {text}; its amount is owed again: {row['tx']}")
    watch(base, f"{base} did not go through: {text}", row["tx"], done=True)


# ---- the cycle ----------------------------------------------------------------

def summary(rpc, db):
    ensure_tables(db)
    out = {"wallet": C.WALLET or None, "owner": C.OWNER_WALLET or None, "share": C.OWNER_SHARE, "token": C.TOKEN or None,
           "burn_share": C.BURN_SHARE, "gold_share": C.GOLD_SHARE, "ops_share": C.OPS_SHARE, "trading": C.TRADING,
           "burn_min_usd": MIN_BURN_USD, "burn_max_usd": BURN_MAX_USD, "gold_min_usd": MIN_GOLD_USD,
           "live": C.LIVE, "claimable_usdg": None, "usdg": None, "eth": None,
           "claimed_total": 0.0, "forwarded_total": 0.0, "compute_total": 0.0, "burned_total": 0.0, "burned_qty": 0.0,
           "gold_total": 0.0, "gold_qty": 0.0, "gold_held": None, "gold_usd": None, "trading_profit_to_burn": 0.0,
           "owed_to_owner": 0.0, "owed_to_burn": 0.0, "owed_to_gold": 0.0, "burn_state": "", "gold_state": "", "ledger": []}
    try:
        # The claim decision without its clock: when the next claim may happen (and so when its burn and gold
        # buys follow) is not published, only why it waits.
        out['claim_policy'] = {k: v for k, v in json.loads(db.meta_get('claim_policy_status') or '{}').items()
                               if k not in PRIVATE_CLAIM_KEYS}
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
        elif r["kind"] == "trade_profit":
            out["trading_profit_to_burn"] = round(r["s"], 4)
    out["burned_qty"] = round(db.one("SELECT COALESCE(SUM(qty),0) q FROM ledger WHERE kind='burn'")["q"] or 0.0, 2)
    # the ETH burns paid in gas (booked since the gas column exists; older burns count 0), failed tries included
    out["burn_gas_eth"] = round(db.one("SELECT COALESCE(SUM(gas_eth),0) g FROM ledger WHERE kind IN ('burn','burn_failed')")["g"] or 0.0, 8)
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
    try:
        from . import burn_program
        out["burn_program"] = burn_program.status(db)
    except Exception as e:
        log.info("burn program status failed: %s", e)
        out["burn_program"] = {"active": False, "state": "unavailable"}
    # Every settled burn, oldest first, for the page's track (the newest BURN_HISTORY_MAX): what was spent,
    # what was burned and where to verify it. Only the past; nothing about the next one.
    out["burn_history"] = [{"ts": r["ts"], "usd": round(r["amount"], 2), "qty": round(r["qty"] or 0.0, 2), "tx": r["tx"]}
                           for r in reversed(db.q("SELECT ts, amount, qty, tx FROM ledger WHERE kind='burn'"
                                                  " ORDER BY id DESC LIMIT ?", (BURN_HISTORY_MAX,)))]
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
        db.add_event('treasury', 'fee claim submitted; waiting for chain confirmation')
        return False
    except Exception as e:
        log.warning("fee claim failed: %s", e)
        db.add_event("error", "fee claim failed; see private logs")
        return False
    return finish(db, h, rc, C.WALLET)[0] == "claim"


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
        db.add_event('treasury', 'forward submitted; waiting for chain confirmation')
        return False
    except Exception as e:
        log.warning("forward failed: %s", e)
        db.add_event("error", "forward failed; see private logs")
        return False
    return finish(db, h, rc, C.WALLET, to=to)[0] == "forward"


def burn_state(owed):
    """One line for the page: why nothing has burned yet, or how burns go. Never the moment of the next one."""
    if not C.TOKEN:
        return "waits for the token"
    if owed < MIN_BURN_USD:
        return f"burns once ${MIN_BURN_USD:g} is owed"
    return f"burns a few times a day, in buys of at most ${BURN_MAX_USD:g}"


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
        carry_burn_gas(db, rc)
        if not rc or rc.get("status") != "0x1":
            raise RuntimeError(f"USDG approval for Permit2 reverted: {h}")
        log.info("approved USDG for Permit2 for a burn: %s", h)   # private: a public line would announce the swap
    now = int(time.time())
    if amt != need or not now + SWAP_TTL_S <= exp <= now + PERMIT_TTL_S:
        data = selector("approve(address,address,uint160,uint48)") + encode(
            ["address", "address", "uint160", "uint48"], [C.USDG, C.UNIVERSAL_ROUTER, need, now + PERMIT_TTL_S]).hex()
        h, rc = send_tx(rpc, acct, C.PERMIT2, data)
        carry_burn_gas(db, rc)
        if not rc or rc.get("status") != "0x1":
            raise RuntimeError(f"Permit2 approval for the router reverted: {h}")
        log.info("approved the Universal Router for a burn with a short expiry: %s", h)
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


def price_impact(rpc, pk, n, out):
    """How much worse a slice of n USDG units buys than a tiny reference buy, 0 to 1. The pool fee, the hook
    fee and the creator tax are percentages of the input in both quotes and cancel out: what is left is the
    slice's own push on the pool."""
    from . import trader
    ref = max(10_000, n // 100)
    ref_out, _gas, _zfo = trader.quote_buy(rpc, pk, C.TOKEN, ref)
    if ref_out <= 0 or out <= 0:
        raise ValueError('no liquidity quoted')
    return max(0.0, 1 - (out * ref) / (ref_out * n))


def burn_slice(rpc, pk, n):
    """The largest slice up to n USDG units whose price impact stays within BURN_MAX_IMPACT: quote, and shrink in
    proportion while it does not fit. Returns (n, out, zero_for_one, impact); n is 0 when nothing at or above
    MIN_BURN_USD fits (or nothing is quoted) and the burn waits."""
    from . import trader
    impact = None
    for _ in range(4):
        out, _gas, zfo = trader.quote_buy(rpc, pk, C.TOKEN, n)
        if out <= 0:
            return 0, 0, zfo, None
        impact = price_impact(rpc, pk, n, out)
        if impact <= BURN_MAX_IMPACT:
            return n, out, zfo, impact
        smaller = int(n * BURN_MAX_IMPACT / impact * 0.9)
        if smaller < units(MIN_BURN_USD):
            break
        n = smaller
    return 0, 0, None, impact


def next_burn_at(now):
    """The earliest moment of the next burn: BURN_EVERY_MIN, plus up to BURN_JITTER more, drawn per burn."""
    return now + BURN_EVERY_MIN * 60 * (1 + _rng.uniform(0, BURN_JITTER))


def burn(rpc, db, acct):
    """Buy $WORM with owed burn money and send it to the burn address: one slice of at most BURN_MAX_USD, shrunk
    until its price impact fits BURN_MAX_IMPACT, once at least MIN_BURN_USD is owed and the token has graduated
    to a pool (a curve buy is not a pool swap). The pace between slices is burn_step's. Returns True on a
    settled burn."""
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
    n = units(min(owed, have, BURN_MAX_USD))
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
        n, out, zfo, impact = burn_slice(rpc, pk, n)
    except Exception as e:
        log.warning("burn: quote failed: %s", e)
        db.add_event("error", "burn: quote failed; see private logs")
        return False
    if not n:
        if impact is None:
            db.add_event("error", "burn: no liquidity quoted; the burn share stays owed")
        else:
            _say_hourly(db, "treasury", f"burn waits: the pool is too thin for a ${MIN_BURN_USD:.0f} slice within "
                                        f"{BURN_MAX_IMPACT * 100:g}% price impact")
        return False
    try:
        expiry = approve_for_router(rpc, db, acct, n)
    except Exception as e:
        log.warning("burn: approval failed: %s", e)
        db.add_event("error", "burn: approval failed; see private logs")
        return False
    # Approvals may take minutes. Never reuse the quote obtained before them, and look at the pool again.
    quoted_at = time.time()
    try:
        out, _gas, zfo = trader.quote_buy(rpc, pk, C.TOKEN, n)
        if out <= 0:
            raise ValueError('no liquidity in refreshed quote')
        impact = price_impact(rpc, pk, n, out)
        deadline = min(int(quoted_at) + SWAP_TTL_S, expiry)
        min_out = max(1, int(out * (1 - BURN_SLIPPAGE)))
        data = burn_calldata(pk, zfo, n, min_out, deadline)
    except Exception:
        log.exception('burn quote refresh failed')
        db.add_event('error', 'burn: fresh quote unavailable; the burn share stays owed')
        return False
    if impact > BURN_MAX_IMPACT:
        _say_hourly(db, "treasury", "burn waits: the pool moved while the approvals were mined; the slice stays owed")
        return False
    from .tx import send_tx
    from . import burn_program
    pid, program_owed = burn_program.unburned(db)
    part = round(min(n / 1e6, program_owed), 6) if pid else 0.0      # program money is burned first

    note = f"{int(round(C.BURN_SHARE * 100))}% of income: buy $WORM on its pool and send it to the burn address"
    if part > 0:
        note += f"; ${part:.2f} of it from the surplus burn program"

    def pending(h):
        now = time.time()
        with db.transaction():
            carried = float(db.meta_get("burn_gas_carry") or 0)
            db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note,program,program_usd,gas_eth) VALUES(?,?,?,?,?,?,?,?,?)",
                 (int(now), "burn_pending", "USDG", n / 1e6, h, note, pid if part > 0 else None, part or None,
                  carried or None))
            db.meta_set("burn_gas_carry", "0")
            db.meta_set("burn_next_at", repr(next_burn_at(now)))
        watch("burn", f"buying $WORM with {n / 1e6:.2f} USDG on its pool and sending it to the burn address", h)

    try:
        h, rc = send_tx(rpc, acct, C.UNIVERSAL_ROUTER, data, on_broadcast=pending,
                        valid_until=min(quoted_at + QUOTE_MAX_AGE_S, deadline))
    except ReceiptPending:
        db.add_event('treasury', 'burn submitted; waiting for chain confirmation')
        return False
    except Exception as e:
        log.warning("burn failed: %s", e)
        db.add_event("error", "burn failed; see private logs")
        return False
    settled = finish(db, h, rc, C.WALLET)[0] == "burn"
    if settled:
        # Log only, never acted on: what the best aggregator route would have given for the same slice. The burn
        # itself stays on $WORM's own USDG pool, quoted, approved and swapped exactly as above.
        from . import route
        route.compare_log(C.USDG, C.TOKEN, n, out, "burn route check")
    return settled


def burn_step(rpc, db, acct, now=None):
    """The burn's pace: nothing before the moment the last burn drew (private, never published), then the burn
    program's release for what its schedule made due, then one slice."""
    now = time.time() if now is None else now
    if not C.TOKEN or now < float(db.meta_get("burn_next_at") or 0):
        return False
    try:
        from . import burn_program
        burn_program.release(rpc, db, now)
    except Exception as e:
        log.warning("burn program release skipped: %s", e)
        _say_hourly(db, "error", "burn program: release skipped (settings or balance unavailable); see private logs")
    return burn(rpc, db, acct)


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
        db.add_event('treasury', 'gold purchase submitted; waiting for chain confirmation')
        return False
    except Exception as e:
        log.warning("gold buy failed: %s", e)
        db.add_event("error", "gold buy failed; see private logs")
        return False
    return finish(db, h, rc, C.WALLET)[0] == "gold"


def cycle(rpc, db, acct):
    """Settle what is in flight, claim fees when worth it, forward the creator's share, then burn (a slice when its
    time has come), then buy gold. Only ever runs armed (WH_LIVE=1)."""
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
    try:
        sweep_trading_profit(db)
    except Exception as e:
        log.warning("trading profit sweep failed: %s", e)
    for action in (forward, burn_step, gold):
        if db.one("SELECT 1 FROM ledger WHERE kind LIKE '%_pending' AND tx IS NOT NULL"):
            return
        action(rpc, db, acct)
