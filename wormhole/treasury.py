"""Money in, money out, in the open.

- income: USDG creator fees. pons credits them to an escrow; the worm claims them with claimToken(USDG).
- the 20% forward: after every claim, WH_OWNER_SHARE of the claimed amount goes to WH_OWNER_WALLET.
- every movement is written to the ledger table and shown on the site."""
import logging
import time

from eth_abi import encode

from . import config as C
from .chain import call_data, call_fn, selector

log = logging.getLogger("wormhole.treasury")
MIN_CLAIM_USD = 1.0        # do not spend gas on dust
MIN_FORWARD_USD = 0.50


def ensure_tables(db):
    db.x("CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT, asset TEXT,"
         " amount REAL, tx TEXT, note TEXT)")


def claimable_usdg(rpc, wallet):
    v = call_fn(rpc, C.FEE_ESCROW, "balanceOfToken(address,address)", ("uint256",), ("address", "address"),
                (wallet, C.USDG))
    return (v or 0) / 1e6


def usdg_balance(rpc, wallet):
    v = call_fn(rpc, C.USDG, "balanceOf(address)", ("uint256",), ("address",), (wallet,))
    return (v or 0) / 1e6


def summary(rpc, db):
    ensure_tables(db)
    out = {"wallet": C.WALLET or None, "owner": C.OWNER_WALLET or None, "share": C.OWNER_SHARE, "token": C.TOKEN or None,
           "live": C.LIVE, "claimable_usdg": None, "usdg": None, "eth": None,
           "claimed_total": 0.0, "forwarded_total": 0.0, "compute_total": 0.0, "ledger": []}
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
    out["ledger"] = db.q("SELECT * FROM ledger ORDER BY id DESC LIMIT 20")
    return out


def cycle(rpc, db, acct):
    """Claim fees when worth it, then forward the owner's share. Only ever runs armed (WH_LIVE=1)."""
    ensure_tables(db)
    if not (C.LIVE and acct and C.WALLET):
        return
    from .tx import send_tx
    try:
        claimable = claimable_usdg(rpc, C.WALLET)
    except Exception as e:
        log.info("claimable read failed: %s", e)
        return
    if claimable < MIN_CLAIM_USD:
        return
    try:
        h, rc = send_tx(rpc, acct, C.FEE_ESCROW, call_data("claimToken(address)", ("address",), (C.USDG,)))
        if rc.get("status") != "0x1":
            db.add_event("error", f"fee claim reverted: {h}")
            return
        db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)",
             (int(time.time()), "claim", "USDG", claimable, h, "creator fees claimed from pons escrow"))
        db.add_event("treasury", f"claimed {claimable:.2f} USDG of creator fees")
    except Exception as e:
        db.add_event("error", f"fee claim failed: {str(e)[:120]}")
        return
    share = claimable * C.OWNER_SHARE
    if C.OWNER_WALLET and share >= MIN_FORWARD_USD:
        try:
            data = selector("transfer(address,uint256)") + encode(["address", "uint256"],
                                                                  [C.OWNER_WALLET, int(share * 1e6)]).hex()
            h, rc = send_tx(rpc, acct, C.USDG, data)
            if rc.get("status") == "0x1":
                db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)",
                     (int(time.time()), "forward", "USDG", share, h, f"{int(C.OWNER_SHARE * 100)}% of income to the creator"))
                db.add_event("treasury", f"forwarded {share:.2f} USDG ({int(C.OWNER_SHARE * 100)}%) to the creator")
        except Exception as e:
            db.add_event("error", f"forward failed: {str(e)[:120]}")
