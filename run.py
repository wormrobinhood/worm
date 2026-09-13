"""Worm: index Pons, score graduations, learn from outcomes, serve the live page.

  python run.py                 # http://127.0.0.1:4670
  python run.py --once 5        # backfill, score the 5 newest graduations, print them, exit
"""
import argparse
import logging
import queue
import threading
import time

from wormhole import config as C
from wormhole.chain import Rpc
from wormhole.db import DB, prune_launches
from wormhole.indexer import Indexer
from wormhole.learn import Brain
from wormhole.paper import Paper
from wormhole.scorer import Scorer
from wormhole.server import Hub, make_app
from wormhole.screen import Screen
from wormhole import treasury as T
from wormhole import voice, trader, giving, compute, lab, advisor
from wormhole import launch as L
from wormhole.budget import projection
from wormhole import readiness
import os
from wormhole.growth import treasury
from wormhole import budget
from wormhole.prices import refresh_scored

log = logging.getLogger("wormhole")


def build():
    rpc = Rpc(C.RPC)
    db = DB()
    advisor.load(db)                              # learned exit arms into the lab before anything parses an arm name
    brain = Brain(db)
    paper = Paper(db)
    hub = Hub()
    newest = lambda: db.one("SELECT token,name,symbol,grad_ts FROM launches WHERE graduated=1"
                            " ORDER BY grad_block DESC LIMIT 1")
    own = lambda: L.own_token(db)                # its own token, once launched and indexed: a page the screen visits too
    screen = Screen(hub, newest=newest, own=own) if os.environ.get("WH_SCREEN", "1") != "0" else None
    hub.screen_on = screen is not None            # the page hides the screen panel on a host that runs no browser

    hub.restore(db)                               # the last dig survives a restart

    def watch(ev):                                # every transaction the worm sends: the page and the screen follow it
        hub.act(ev)
        if screen:
            screen.on_action(ev)
    T.WATCH = watch

    def progress(ev):
        hub.scan(ev)
        hub.persist(db)
        if screen:
            screen.on_dig(ev)

    scorer = Scorer(rpc, db, brain.weights, progress=progress)
    acct = None
    if C.SECRET:
        from wormhole.wallet import account
        acct = account()
    return rpc, db, brain, scorer, paper, hub, screen, acct


RETRY_S = 600          # a token scored on incomplete data, or whose scoring crashed, is looked at again
MAX_TRIES = 3


def score_one(token, db, scorer, brain, paper, hub, label, requeue=None, tries=1):
    try:
        if hasattr(hub, "mark_scoring"):
            hub.mark_scoring(token)
        r = scorer.score(token)
        if not r:
            return None
        brain.record(token, r)
        paper.consider(token, r)
        db.add_event("verdict", f"{label(token)}: {r['verdict']} [{r['score']}]"
                     + (" (partial data)" if r["metrics"].get("partial") else ""), token)
        hub.notify("score", token)
        log.info("scored %s -> %s [%s] in %ss", label(token), r["verdict"], r["score"], r["metrics"].get("scored_in_s"))
        if r.get("retry") and requeue and tries < MAX_TRIES:
            requeue(token, tries + 1)
        return r
    except Exception as e:
        log.exception("scoring %s failed: %s", token[:10], e)
        db.add_event("error", f"scoring {token[:10]} failed: {str(e)[:120]}", token)
        if requeue and tries < MAX_TRIES:
            requeue(token, tries + 1)
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", type=int, default=0, help="score N newest graduations and exit")
    ap.add_argument("--host", default=C.HOST)
    ap.add_argument("--port", type=int, default=C.PORT)
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    rpc, db, brain, scorer, paper, hub, screen, acct = build()
    work = queue.Queue()
    idx = Indexer(rpc, db, on_graduation=work.put, on_launch=lambda t: hub.notify("launch", t))
    hub.rescan = work.put
    hub.indexer = idx

    def requeue(token, tries):
        threading.Timer(RETRY_S, work.put, [(token, tries)]).start()

    if a.once:
        idx.backfill()
        rows = db.q("SELECT token FROM launches WHERE graduated=1 ORDER BY grad_block DESC LIMIT ?", (a.once,))
        for r in rows:
            res = score_one(r["token"], db, scorer, brain, paper, hub, idx.label)
            if res:
                print(f"\n{idx.label(r['token'])}  score {res['score']}  {res['verdict']}")
                for line in res["reasons"]:
                    print("   -", line)
                m = res["metrics"]
                print(f"   holders {m.get('holders')}  top10 {m.get('top10_pct')}%  buyers {m.get('unique_buyers')}"
                      f"  snipe {m.get('snipe_pct')}%  swaps1h {m.get('swaps_1h')}  price ${m.get('price_usd')}")
        return

    def pipeline():
        while True:                                   # a node hiccup during catch-up must not kill indexing for good
            try:
                idx.backfill()
                break
            except Exception as e:
                log.exception("backfill failed, retry in 30s: %s", e)
                db.add_event("error", f"backfill failed: {str(e)[:120]}")
                time.sleep(30)
        if C.SCORE_HOURS > 0:                     # by default the worm does not dig old graduations: it starts from now
            cutoff = int(time.time()) - int(C.SCORE_HOURS * 3600)
            for r in db.q("SELECT token FROM launches WHERE graduated=1 AND grad_ts>=? AND token NOT IN"
                          " (SELECT token FROM scores) ORDER BY grad_block DESC", (cutoff,)):
                work.put(r["token"])
        idx.follow()

    def worker():
        while True:
            item = work.get()
            token, tries = item if isinstance(item, tuple) else (item, 1)
            while T.working():                    # a transaction is out: no digging until it settles
                time.sleep(1)
            score_one(token, db, scorer, brain, paper, hub, idx.label, requeue, tries)

    def watchdog():
        """If the indexer has not advanced for 15 minutes, exit so the host restarts the process."""
        while True:
            time.sleep(60)
            last_ok = getattr(idx, "last_ok", None)
            if idx.ready.is_set() and last_ok and time.time() - last_ok > 900:
                log.error("indexer stalled for 15 min, exiting for a restart")
                os._exit(3)

    def marker():
        time.sleep(45)
        try:
            voice.ensure_tables(db)
            if not db.one("SELECT 1 FROM posts WHERE ok=1"):
                voice.cycle(db, force=True)
        except Exception as e:
            log.warning("first journal entry failed: %s", e)
        cycle_n = 0
        while True:
            cycle_n += 1
            box = {}                                  # what the money stages share, real money only

            def books():
                tre = box["tre"] = treasury(rpc)
                budget.sample(db, tre["usd_real"], bool(tre.get("demo")))
                box["rw"] = projection(db, T.free_usd(db, tre["usd_real"]))   # the wallet minus what is owed to the creator and the burn

            def compute_stage():
                tre, rw = box["tre"], box["rw"]
                try:
                    from wormhole.wallet import balances
                    bal = balances(C.WALLET) if C.WALLET else {}
                except Exception:
                    bal = {}
                if compute.PROVIDER == "aisurplus":     # USDG here, minus what is owed to the creator and the burn
                    usdg = bal.get("usdg_rh")
                    spendable = None if usdg is None else max(0.0, usdg - T.owed_total(db))
                else:
                    spendable = bal.get("usdc_base")
                compute.plan(db, acct, spendable, rw.get("can_invest") or (tre["usd_real"] > 0), C.LIVE,
                             budget_per_day=rw.get("compute_budget_per_day_usd"), rpc=rpc)

            def entries():
                tre, rw = box["tre"], box["rw"]
                rd = readiness.compute(brain.summary(), lab.summary(db), rw, bool(tre.get("demo")), trader.MAX_POSITION_USD)
                trader.decide(rpc, db, rw, C.LIVE, acct, rd)

            def housekeeping():
                if cycle_n % 12 == 1:                 # once an hour
                    prune_launches(db)
                    if hasattr(idx, "refetch_metadata"):
                        idx.refetch_metadata()

            # exits and bookkeeping run before entries; every stage is isolated so one failure cannot skip the rest
            stages = [("own", lambda: L.announce(db)), ("prices", lambda: refresh_scored(db)), ("lab", lambda: lab.tick(db)),
                      ("paper", paper.retry_pending), ("paper mark", paper.mark), ("brain", brain.check),
                      ("advisor", lambda: advisor.due(db)[0] and advisor.run(db, brain.summary(), lab.summary(db))),
                      ("exits", lambda: trader.mark(rpc, db, C.LIVE, acct)), ("treasury", lambda: T.cycle(rpc, db, acct)),
                      ("books", books), ("compute", compute_stage), ("entries", entries),
                      ("giving", lambda: giving.cycle(rpc, db, acct, box["rw"], C.LIVE and not box["tre"].get("demo"))),
                      ("voice", lambda: voice.cycle(db, extra={"stage": box["tre"].get("stage_name"), "treasury_usd": box["tre"].get("usd_real")})),
                      ("housekeeping", housekeeping)]
            for name, fn in stages:
                try:
                    fn()
                except KeyError as e:
                    log.warning("%s skipped: books did not run (%s)", name, e)
                except Exception as e:
                    log.warning("%s failed: %s", name, e)
            hub.notify("mark")
            time.sleep(C.MARK_EVERY_S)

    for fn in (pipeline, worker, marker, watchdog):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
    if screen:
        screen.start()

    import uvicorn
    # proxy_headers=False: request.client.host is the real TCP peer, never a spoofable X-Forwarded-For
    uvicorn.run(make_app(rpc, db, brain, paper, hub), host=a.host, port=a.port, log_level="warning", proxy_headers=False)


if __name__ == "__main__":
    main()
