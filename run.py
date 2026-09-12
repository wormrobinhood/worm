"""IRL Worm: index Pons, score graduations, learn from outcomes, serve the live page.

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
from wormhole.db import DB
from wormhole.indexer import Indexer
from wormhole.learn import Brain
from wormhole.paper import Paper
from wormhole.scorer import Scorer
from wormhole.server import Hub, make_app
from wormhole.screen import Screen
from wormhole import treasury as T
from wormhole import voice, trader, giving, compute, lab
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
    brain = Brain(db)
    paper = Paper(db)
    hub = Hub()
    newest = lambda: db.one("SELECT token,name,symbol,grad_ts FROM launches WHERE graduated=1"
                            " ORDER BY grad_block DESC LIMIT 1")
    screen = Screen(hub, newest=newest) if os.environ.get("WH_SCREEN", "1") != "0" else None

    def progress(ev):
        hub.scan(ev)
        if screen:
            screen.on_dig(ev)

    scorer = Scorer(rpc, db, brain.weights, progress=progress)
    acct = None
    if C.SECRET:
        from wormhole.wallet import account
        acct = account()
    return rpc, db, brain, scorer, paper, hub, screen, acct


def score_one(token, db, scorer, brain, paper, hub, label):
    try:
        r = scorer.score(token)
        if not r:
            return None
        brain.record(token, r)
        paper.consider(token, r)
        db.add_event("verdict", f"{label(token)}: {r['verdict']} [{r['score']}]"
                     + (" (partial data)" if r["metrics"].get("partial") else ""), token)
        hub.notify("score", token)
        log.info("scored %s -> %s [%s] in %ss", label(token), r["verdict"], r["score"], r["metrics"].get("scored_in_s"))
        return r
    except Exception as e:
        log.exception("scoring %s failed: %s", token[:10], e)
        db.add_event("error", f"scoring {token[:10]} failed: {str(e)[:120]}", token)
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
        idx.backfill()
        cutoff = int(time.time()) - int(C.SCORE_HOURS * 3600)
        for r in db.q("SELECT token FROM launches WHERE graduated=1 AND grad_ts>=? AND token NOT IN"
                      " (SELECT token FROM scores) ORDER BY grad_block DESC", (cutoff,)):
            work.put(r["token"])
        idx.follow()

    def worker():
        while True:
            token = work.get()
            score_one(token, db, scorer, brain, paper, hub, idx.label)

    def marker():
        time.sleep(45)
        try:
            voice.ensure_tables(db)
            if not db.one("SELECT 1 FROM posts WHERE ok=1"):
                voice.cycle(db, force=True)
        except Exception as e:
            log.warning("first journal entry failed: %s", e)
        while True:
            try:
                refresh_scored(db)
                lab.tick(db)
                paper.retry_pending()
                paper.mark()
                brain.check()
                budget.sample(db, treasury(rpc)["usd"])
                T.cycle(rpc, db, acct)
                tre = treasury(rpc)
                rw = projection(db, tre["usd"])
                try:
                    from wormhole.wallet import balances
                    usdc = balances(C.WALLET).get("usdc_base") if C.WALLET else None
                except Exception:
                    usdc = None
                compute.plan(db, acct, usdc, rw.get("can_invest") or (tre["usd"] > 0), C.LIVE)
                rd = readiness.compute(brain.summary(), lab.summary(db), rw, bool(tre.get("demo")), trader.MAX_POSITION_USD)
                trader.decide(rpc, db, rw, C.LIVE, acct, rd)
                trader.mark(rpc, db, C.LIVE, acct)
                giving.cycle(rpc, db, acct, rw, C.LIVE)
                voice.cycle(db, extra={"stage": tre.get("stage_name"), "treasury_usd": tre.get("usd")})
                hub.notify("mark")
            except Exception as e:
                log.warning("mark failed: %s", e)
            time.sleep(C.MARK_EVERY_S)

    for fn in (pipeline, worker, marker):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
    if screen:
        screen.start()

    import uvicorn
    uvicorn.run(make_app(rpc, db, brain, paper, hub), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
