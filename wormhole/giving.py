"""Causes: giving from the surplus above the 90-day reserve, on-chain and public.

WH_CAUSES="Name|0xaddress|weight,Other|0xaddress|weight"   WH_GIVE_SHARE=0.10 (of the surplus, monthly)
Live: USDG transfers written to the ledger. Demo: the same decision, written as 'would give'."""
import logging
import os
import time

from eth_abi import encode

from . import config as C
from .chain import selector

log = logging.getLogger("wormhole.giving")
SHARE = float(os.environ.get("WH_GIVE_SHARE", "0.10"))
EVERY_S = 30 * 86400
MIN_USD = 1.0


def causes():
    out = []
    for item in os.environ.get("WH_CAUSES", "").split(","):
        parts = [x.strip() for x in item.split("|")]
        if len(parts) >= 2 and parts[1].startswith("0x") and len(parts[1]) == 42:
            out.append({"name": parts[0][:40], "address": parts[1].lower(), "weight": float(parts[2]) if len(parts) > 2 else 1.0})
    return out


def cycle(rpc, db, acct, runway, live):
    cs = causes()
    if not cs:
        return
    surplus = float(runway.get("surplus_usd") or 0)
    if surplus <= 0:
        return
    last = db.one("SELECT ts FROM ledger WHERE kind IN ('give','give_demo') ORDER BY ts DESC LIMIT 1")
    if last and time.time() - last["ts"] < EVERY_S:
        return
    total = surplus * SHARE
    if total < MIN_USD:
        return
    wsum = sum(c["weight"] for c in cs) or 1
    for c in cs:
        amt = total * c["weight"] / wsum
        if amt < 0.01:
            continue
        if live and acct:
            from .tx import send_tx
            try:
                data = selector("transfer(address,uint256)") + encode(["address", "uint256"], [c["address"], int(amt * 1e6)]).hex()
                h, rc = send_tx(rpc, acct, C.USDG, data)
                if rc.get("status") == "0x1":
                    db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)",
                         (int(time.time()), "give", "USDG", amt, h, f"to {c['name']}"))
                    db.add_event("giving", f"gave {amt:.2f} USDG to {c['name']}")
            except Exception as e:
                db.add_event("error", f"giving to {c['name']} failed: {str(e)[:100]}")
        else:
            db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)",
                 (int(time.time()), "give_demo", "USDG", amt, None, f"demo: would give to {c['name']}"))
            db.add_event("giving", f"demo: would give {amt:.2f} USDG to {c['name']} ({int(SHARE * 100)}% of the surplus)")


def summary(db):
    cs = causes()
    given = {r["note"]: r["s"] for r in db.q("SELECT note, SUM(amount) s FROM ledger WHERE kind='give' GROUP BY note")}
    return {"causes": cs, "share": SHARE, "given_total": round(sum(given.values()), 2),
            "rule": f"every 30 days, {int(SHARE * 100)}% of the surplus above the 90-day reserve, split by weight"}
