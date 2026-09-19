"""A curve's buyers, judged by what their earlier picks became.

Every scored token's buyers are remembered (curve_buyers, the wallets that ended up with the tokens). A day
later the token's outcome is resolved, and each of those wallets gets one more pick on its record: good (flat
or grew) or bad (rugged or dumped). Nothing is counted before it is known, so a record is never wiser than the
worm was at that moment.

What the records are good for was measured on 1,451 graduations with every router buy traced to the wallet
behind it (docs/SECOND-LOOK.md): wallets with a GOOD record predict nothing (copying them lost 11-18% a trade,
no better than the rest), but the share of the curve bought by wallets with a LOSING record does. 30% or more:
6% of those tokens turned out not bad and none grew; under 5%: 18% not bad. A warning, not a buy signal.

Read-only on the chain; writes only its own two tables."""
import collections
import json
import logging
import time

from . import config as C

log = logging.getLogger("wormhole.crowd")

MIN_PICKS = 3             # resolved picks a wallet needs before its record is read
LOSER_MAX_GOOD = 0.05     # at most this share of them not bad: with fewer than 20 picks that means none
MIN_HISTORY = 150         # resolved tokens on record before the read is trusted with points
GOOD, BAD = ("flat", "grew"), ("rugged", "dumped")
BACKFILL_PER_TICK = 4     # resolved tokens from before the buyers were followed, re-read from the chain per cycle
_failed = collections.Counter()


def history(db):
    return db.one("SELECT COUNT(*) n FROM wallet_folded WHERE source!='skipped'")["n"]


def _fold(db, token, outcome, wallets, source):
    """One more pick for every wallet, once per token."""
    now = int(time.time())
    good, grew = int(outcome in GOOD), int(outcome == "grew")
    with db.transaction():
        if db.one("SELECT 1 FROM wallet_folded WHERE token=?", (token,)):
            return False
        db.many("INSERT INTO wallet_records(wallet,picks,good,grew,updated) VALUES(?,1,?,?,?)"
                " ON CONFLICT(wallet) DO UPDATE SET picks=picks+1, good=good+excluded.good, grew=grew+excluded.grew,"
                " updated=excluded.updated", [(w, good, grew, now) for w in wallets])
        db.x("INSERT INTO wallet_folded(token,ts,buyers,source) VALUES(?,?,?,?)", (token, now, len(wallets), source))
    return True


def _skip(db, token, why):
    db.x("INSERT OR IGNORE INTO wallet_folded(token,ts,buyers,source) VALUES(?,?,0,'skipped')", (token, int(time.time())))
    log.info("crowd: %s left out of the records (%s)", token[:10], why)


def read(db, buyers):
    """buyers: {wallet: tokens bought}, the creator already left out. Shares are of that buy volume."""
    total = sum(buyers.values())
    out = {"losing_pct": None, "losing_buyers": 0, "known_buyers_pct": None, "crowd_history": history(db)}
    if not buyers or total <= 0:
        return out
    known = losing = 0
    wallets = list(buyers)
    for i in range(0, len(wallets), 400):
        chunk = wallets[i:i + 400]
        for r in db.q(f"SELECT wallet, picks, good FROM wallet_records WHERE wallet IN ({','.join('?' * len(chunk))}) AND picks>=?",
                      (*chunk, MIN_PICKS)):
            known += buyers[r["wallet"]]
            if r["good"] <= LOSER_MAX_GOOD * r["picks"]:
                losing += buyers[r["wallet"]]
                out["losing_buyers"] += 1
    out["losing_pct"] = round(100.0 * losing / total, 1)
    out["known_buyers_pct"] = round(100.0 * known / total, 1)
    return out


def _from_chain(rpc, L, db=None):
    """The wallets that ended up with a token's curve buys, for a token scored before buys were followed."""
    from .pons import CURVE_BUY, TRANSFER
    from .scorer import BUYERS_KEPT, DUST_DIVISOR, real_buyers
    gb = int(L["grad_block"])
    lb = int(L["block"]) if L["block"] else max(0, gb - 24 * C.BLOCKS_PER_HOUR)
    buys = [CURVE_BUY.decode(lg) for lg in rpc.get_logs(L["curve"], [[CURVE_BUY.topic]], lb, gb, 200_000, cap=20_000)]
    txs = {b["_tx"] for b in buys}
    sent = [TRANSFER.decode(lg) for lg in rpc.get_logs(L["token"], [TRANSFER.topic], lb, gb, 200_000, cap=C.TRANSFER_LOG_CAP)]
    moves = [t for t in sent if t["_tx"] in txs]
    if db is not None:                                # the same read teaches the linked-wallet check who is a service
        from . import linked
        linked.remember_senders(db, L["token"], [(t["from"], t["to"], t["value"]) for t in sent], L["curve"], L.get("grad_ts"))
    bought = collections.Counter()
    for parts in real_buyers(buys, moves, L["curve"], set(C.INFRA) | {L["curve"], L["token"]}):
        for wallet, tokens in parts:
            bought[wallet] += tokens
    dust = sum(bought.values()) // DUST_DIVISOR
    top = sorted(((w, v) for w, v in bought.items() if w not in C.INFRA and w != L["deployer"] and v > 0 and v >= dust),
                 key=lambda kv: -kv[1])[:BUYERS_KEPT]
    return [w for w, _ in top]


def tick(rpc, db, backfill=BACKFILL_PER_TICK):
    """Fold every newly resolved token into the records. Tokens whose buyers were remembered by recipient (before
    buys were followed through their transaction) are re-read from the chain, a few per cycle, newest first."""
    rows = db.q("SELECT o.token, o.outcome, s.metrics, l.curve, l.block, l.grad_block, l.grad_ts, l.deployer FROM outcomes o"
                " JOIN scores s ON s.token=o.token JOIN launches l ON l.token=o.token"
                " WHERE o.resolved=1 AND o.token NOT IN (SELECT token FROM wallet_folded) ORDER BY o.scored_at DESC")
    folded = reread = 0
    for r in rows:
        if r["outcome"] not in GOOD + BAD:
            _skip(db, r["token"], "no usable outcome")
            continue
        try:
            by_holder = (json.loads(r["metrics"] or "{}") or {}).get("buyers_by") == "holder"
        except ValueError:
            by_holder = False
        if by_holder:
            wallets = [x["wallet"] for x in db.q("SELECT wallet FROM curve_buyers WHERE token=? AND wallet!=?",
                                                 (r["token"], r["deployer"]))]
            if wallets:
                folded += _fold(db, r["token"], r["outcome"], wallets, "remembered")
                continue
        if reread >= backfill or not r["curve"] or not r["grad_block"] or rpc is None:
            continue
        reread += 1
        try:
            wallets = _from_chain(rpc, dict(r), db)
        except Exception as e:
            _failed[r["token"]] += 1
            log.info("crowd: re-read of %s failed (%s)", r["token"][:10], e)
            if _failed[r["token"]] >= 3:
                _skip(db, r["token"], "chain read kept failing")
            continue
        if wallets:
            folded += _fold(db, r["token"], r["outcome"], wallets, "chain")
        else:
            _skip(db, r["token"], "no buyers found")
    return folded


def summary(db):
    rec = db.one("SELECT COUNT(*) n, COALESCE(SUM(CASE WHEN picks>=? THEN 1 ELSE 0 END),0) judged,"
                 " COALESCE(SUM(CASE WHEN picks>=? AND good<=?*picks THEN 1 ELSE 0 END),0) losing FROM wallet_records",
                 (MIN_PICKS, MIN_PICKS, LOSER_MAX_GOOD))
    return {"tokens_on_record": history(db), "needed": MIN_HISTORY, "wallets": rec["n"], "wallets_judged": rec["judged"],
            "wallets_losing": rec["losing"], "min_picks": MIN_PICKS}
