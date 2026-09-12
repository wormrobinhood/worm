"""Money in, money out, in the open.

- income: USDG creator fees. pons credits them to an escrow; the worm claims them with claimToken(USDG).
- the 20% forward: WH_OWNER_SHARE of everything claimed is owed to WH_OWNER_WALLET. The owed balance
  is derived from the ledger (claims minus forwards, in flight included), so small claims add up
  instead of being dropped and a crash between two steps changes nothing.
- every movement is written to the ledger table and shown on the site. A row is written as
  '<kind>_pending' the moment the node has the transaction and settled from the receipt: 'claim',
  'forward' or 'give' on success (amount taken from the ClaimedToken or Transfer log), '<kind>_failed'
  on a revert, '<kind>_dropped' when the node lost it. Pending rows are reconciled at the top of every
  cycle, before anything new is sent."""
import logging
import time

from eth_abi import encode

from . import config as C
from .chain import addr_from_topic, call_data, call_fn, selector, topic

log = logging.getLogger("wormhole.treasury")
MIN_CLAIM_USD = 1.0        # do not spend gas on dust
MIN_FORWARD_USD = 0.50
PENDING_MAX_AGE_S = 3600   # a pending tx the node no longer knows after this long is written off
CLAIMED_TOPIC = topic("ClaimedToken(address,address,uint256)")     # PonsV2FeeEscrow: recipient, token indexed
TRANSFER_TOPIC = topic("Transfer(address,address,uint256)")


def ensure_tables(db):
    db.x("CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT, asset TEXT,"
         " amount REAL, tx TEXT, note TEXT)")


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
    c = db.one("SELECT COALESCE(SUM(amount),0) s FROM ledger WHERE kind='claim'")["s"]
    f = db.one("SELECT COALESCE(SUM(amount),0) s FROM ledger WHERE kind IN ('forward','forward_pending')")["s"]
    return c * C.OWNER_SHARE - f


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


def _event_text(kind, amount, row):
    if kind == "claim":
        return f"claimed {amount:.2f} USDG of creator fees"
    if kind == "forward":
        return f"forwarded {amount:.2f} USDG ({int(C.OWNER_SHARE * 100)}%) to the creator"
    if kind == "give":
        return f"gave {amount:.2f} USDG {row['note']}"
    return f"{kind}: {amount:.2f} USDG"


def settle(db, row, rc, wallet, to=None):
    """Apply a receipt to a '<kind>_pending' ledger row and write the event. Returns (kind, amount)."""
    base = row["kind"].removesuffix("_pending")
    ok = rc.get("status") == "0x1"
    amount, why = row["amount"], "reverted"
    if ok and base == "claim":
        got = claimed_in(rc, wallet)               # what the escrow actually paid, not the pre-tx read
        if got is not None:
            amount = got
    elif ok:
        ok = transferred(rc, C.USDG, wallet, to, units(row["amount"]))
        why = "no Transfer log for the amount"
    kind = base if ok else f"{base}_failed"
    note = row["note"] if ok else f"{row['note']} ({why})"
    db.x("UPDATE ledger SET kind=?, amount=?, note=? WHERE id=?", (kind, amount, note, row["id"]))
    if ok:
        db.add_event("giving" if base == "give" else "treasury", _event_text(kind, amount, row))
    else:
        db.add_event("error", f"{base} {why}: {row['tx']}")
    return kind, amount


def finish(db, h, rc, wallet, to=None):
    """Settle the pending row written at broadcast for hash h (send_tx's on_broadcast)."""
    row = db.one("SELECT * FROM ledger WHERE tx=? AND kind LIKE '%_pending' ORDER BY id DESC LIMIT 1", (h,))
    if not row:
        db.add_event("error", f"no pending ledger row for {h}")
        return None, None
    return settle(db, row, rc, wallet, to)


def reconcile(rpc, db, kinds, wallet, to_for=None):
    """Settle pending rows from the chain: the receipt when there is one, written off when the node no
    longer knows the transaction after PENDING_MAX_AGE_S. to_for(row) gives a transfer's expected
    recipient. Returns how many rows are still pending."""
    still = 0
    marks = ",".join("?" * len(kinds))
    for row in db.q(f"SELECT * FROM ledger WHERE kind IN ({marks}) ORDER BY id", tuple(kinds)):
        rc = rpc.call("eth_getTransactionReceipt", [row["tx"]]) if row["tx"] else None
        if rc:
            settle(db, row, rc, wallet, to_for(row) if to_for else None)
        elif not row["tx"] or (time.time() - row["ts"] > PENDING_MAX_AGE_S
                               and not rpc.call("eth_getTransactionByHash", [row["tx"]])):
            base = row["kind"].removesuffix("_pending")
            db.x("UPDATE ledger SET kind=?, note=? WHERE id=?", (f"{base}_dropped", f"{row['note']} (never mined)", row["id"]))
            db.add_event("error", f"{base} tx {row['tx']} was never mined and the node has dropped it; written off")
        else:
            still += 1
    return still


# ---- the cycle ----------------------------------------------------------------

def summary(rpc, db):
    ensure_tables(db)
    out = {"wallet": C.WALLET or None, "owner": C.OWNER_WALLET or None, "share": C.OWNER_SHARE, "token": C.TOKEN or None,
           "live": C.LIVE, "claimable_usdg": None, "usdg": None, "eth": None,
           "claimed_total": 0.0, "forwarded_total": 0.0, "compute_total": 0.0, "owed_to_owner": 0.0, "ledger": []}
    if C.WALLET:
        try:
            out["claimable_usdg"] = round(claimable_usdg(rpc, C.WALLET), 4)
            out["usdg"] = round(usdg_balance(rpc, C.WALLET), 4)
            out["eth"] = round(int(rpc.call("eth_getBalance", [C.WALLET, "latest"]), 16) / 1e18, 6)
        except Exception as e:
            log.info("treasury read failed: %s", e)
    for r in db.q("SELECT kind, COALESCE(SUM(amount),0) s FROM ledger GROUP BY kind"):
        if r["kind"] == "claim":
            out["claimed_total"] = round(r["s"], 4)
        elif r["kind"] == "forward":
            out["forwarded_total"] = round(r["s"], 4)
        elif r["kind"] == "compute":
            out["compute_total"] = round(r["s"], 4)
    out["owed_to_owner"] = round(max(0.0, owed_to_owner(db)), 4)
    out["ledger"] = db.q("SELECT * FROM ledger ORDER BY id DESC LIMIT 20")
    return out


def claim(rpc, db, acct, claimable):
    from .tx import send_tx

    def pending(h):
        db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)",
             (int(time.time()), "claim_pending", "USDG", claimable, h, "creator fees claimed from pons escrow"))

    try:
        h, rc = send_tx(rpc, acct, C.FEE_ESCROW, call_data("claimToken(address)", ("address",), (C.USDG,)),
                        on_broadcast=pending)
    except Exception as e:
        db.add_event("error", f"fee claim failed: {str(e)[:120]}")
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

    def pending(h):
        db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)",
             (int(time.time()), "forward_pending", "USDG", n / 1e6, h, f"{int(C.OWNER_SHARE * 100)}% of income to the creator"))

    try:
        h, rc = send_tx(rpc, acct, C.USDG, data, on_broadcast=pending)
    except Exception as e:
        db.add_event("error", f"forward failed: {str(e)[:120]}")
        return False
    return finish(db, h, rc, C.WALLET, to=C.OWNER_WALLET)[0] == "forward"


def cycle(rpc, db, acct):
    """Settle what is in flight, claim fees when worth it, then forward the owner's share.
    Only ever runs armed (WH_LIVE=1)."""
    ensure_tables(db)
    if not (C.LIVE and acct and C.WALLET):
        return
    try:
        if reconcile(rpc, db, ("claim_pending", "forward_pending"), C.WALLET, lambda r: C.OWNER_WALLET):
            return                      # something is still in flight: settle it before sending more
    except Exception as e:
        db.add_event("error", f"ledger reconcile failed: {str(e)[:120]}")
        return
    try:
        claimable = claimable_usdg(rpc, C.WALLET)
    except Exception as e:
        log.info("claimable read failed: %s", e)
        return
    if claimable >= MIN_CLAIM_USD:
        claim(rpc, db, acct, claimable)
    forward(rpc, db, acct)
