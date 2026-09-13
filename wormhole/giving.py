"""Causes: giving from the surplus above the 90-day reserve, on-chain and public.

WH_CAUSES="Name|0xaddress|weight,Other|0xaddress|weight"   WH_GIVE_SHARE=0.10 (of the surplus, monthly)
Live: USDG transfers written to the ledger. Demo: the same decision, written as 'would give'.
The surplus must be real money: a pretend treasury (WH_DEMO_TREASURY) never sizes a real transfer.
Each cause is gated on its own last successful gift, so one failed transfer delays nobody else."""
import logging
import math
import os
import time

from eth_abi import encode

from . import config as C
from .chain import selector
from .growth import demo_treasury
from .treasury import ensure_tables, finish, reconcile, units

log = logging.getLogger("wormhole.giving")
SHARE = float(os.environ.get("WH_GIVE_SHARE", "0.10"))
EVERY_S = 30 * 86400
MIN_USD = 1.0
_warned = set()


def causes():
    out = []
    for item in os.environ.get("WH_CAUSES", "").split(","):
        parts = [x.strip() for x in item.split("|")]
        if len(parts) < 2 or not C.valid_address(parts[1]):
            continue
        try:
            w = float(parts[2]) if len(parts) > 2 and parts[2] else 1.0
        except ValueError:
            w = float("nan")
        if not (math.isfinite(w) and w > 0):
            if item not in _warned:                    # once per bad entry, not once per page view
                _warned.add(item)
                log.warning("cause %r skipped: its weight must be a positive number", parts[0][:40])
            continue
        out.append({"name": parts[0][:40] or parts[1][:10], "address": parts[1].lower(), "weight": w})
    return out


def _last(db, kind, note):
    r = db.one("SELECT MAX(ts) t FROM ledger WHERE kind=? AND note=?", (kind, note))
    return r["t"] if r else None


def _give(rpc, db, acct, cause, n, note):
    from .tx import send_tx
    data = selector("transfer(address,uint256)") + encode(["address", "uint256"], [cause["address"], n]).hex()

    def pending(h):
        db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)",
             (int(time.time()), "give_pending", "USDG", n / 1e6, h, note))
        from .treasury import watch
        watch("give", f"giving {n / 1e6:.2f} USDG {note}", h)

    try:
        h, rc = send_tx(rpc, acct, C.USDG, data, on_broadcast=pending)
    except Exception as e:
        db.add_event("error", f"giving to {cause['name']} failed: {str(e)[:100]}")
        return False
    return finish(db, h, rc, C.WALLET, to=cause["address"])[0] == "give"


def cycle(rpc, db, acct, runway, live):
    ensure_tables(db)
    cs = causes()
    if not cs:
        return
    live = bool(live and acct and C.LIVE)
    if live and (runway.get("treasury_is_demo") or demo_treasury() > 0):
        live = False                                   # pretend money never moves real money
    surplus = float(runway.get("surplus_usd") or 0)
    if surplus <= 0:
        return
    total = surplus * SHARE
    if total < MIN_USD:
        return
    if live:
        by_note = {f"to {c['name']}": c["address"] for c in cs}
        if reconcile(rpc, db, ("give_pending",), C.WALLET, lambda r: by_note.get(r["note"])):
            return                                     # a gift is still in flight: settle it first
    wsum = sum(c["weight"] for c in cs)
    kind = "give" if live else "give_demo"
    for c in cs:
        note = f"to {c['name']}"
        n = units(total * c["weight"] / wsum)
        if n < units(0.01):
            continue
        last = _last(db, kind, note)                   # demo rows gate demo only, live rows gate live only
        if last and time.time() - last < EVERY_S:
            continue
        if live:
            _give(rpc, db, acct, c, n, note)
        else:
            db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)",
                 (int(time.time()), "give_demo", "USDG", n / 1e6, None, note))
            db.add_event("giving", f"demo: would give {n / 1e6:.2f} USDG to {c['name']} ({int(SHARE * 100)}% of the surplus)")


def summary(db):
    cs = causes()
    given = {r["note"]: r["s"] for r in db.q("SELECT note, SUM(amount) s FROM ledger WHERE kind='give' GROUP BY note")}
    return {"causes": cs, "share": SHARE, "given_total": round(sum(given.values()), 2),
            "rule": f"every 30 days, {int(SHARE * 100)}% of the surplus above the 90-day reserve, split by weight"}
