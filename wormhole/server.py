"""The live site: one page, a JSON snapshot, and a websocket that pushes fresh snapshots."""
import asyncio
import json
import logging
import queue
import time

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
import re
import requests
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from . import config as C
from .growth import treasury
from .learn import creator_trust
from .budget import projection
from . import treasury as T
from . import voice as V, trader as TR, giving as G, compute as CP, lab as LB, readiness as RD

log = logging.getLogger("wormhole.server")
WEB = C.ROOT / "web"

DISCLOSURE = {
    "real": "Every number on this page is read from Robinhood Chain or GeckoTerminal: launches, graduations, "
            "curve buys, holders, swaps, prices.",
    "not_real": "The verdicts are rules written by a person and re-weighted by outcomes. They are a screening aid, "
                "not an audit and not advice. IRL Worm has bought nothing; the training book is paper trades that teach its exit rules.",
}


class Hub:
    def __init__(self):
        self.pending = queue.Queue()
        self.dig = []            # events of the dig in progress (or the last one)
        self.rescan = None       # set by run.py: callable(token) that queues a rescore
        self.frame_latest = None

    def notify(self, kind, payload=None):
        self.pending.put({"kind": kind, "payload": payload, "ts": int(time.time())})

    def frame(self, data):
        self.frame_latest = data
        self.pending.put({"kind": "frame", "payload": None, "ts": data.get("ts")})

    def scan(self, ev):
        if ev.get("step") == "start":
            self.dig = []
        self.dig = (self.dig + [ev])[-8:]
        self.pending.put({"kind": "scan", "payload": ev, "ts": ev.get("ts")})


def _js(row, keys):
    for k in keys:
        if isinstance(row.get(k), str):
            try:
                row[k] = json.loads(row[k])
            except ValueError:
                row[k] = None
    return row


def snapshot(rpc, db, brain, paper, hub=None):
    now = int(time.time())
    day = now - 86400
    st = {
        "launches_24h": db.one("SELECT COUNT(*) n FROM launches WHERE ts>=?", (day,))["n"],
        "grads_24h": db.one("SELECT COUNT(*) n FROM launches WHERE graduated=1 AND grad_ts>=?", (day,))["n"],
        "launches_total": db.one("SELECT COUNT(*) n FROM launches")["n"],
        "scored": db.one("SELECT COUNT(*) n FROM scores")["n"],
        "queued": db.one("SELECT COUNT(*) n FROM launches WHERE graduated=1 AND grad_ts>=? AND token NOT IN"
                         " (SELECT token FROM scores)", (now - int(C.SCORE_HOURS * 3600),))["n"],
        "verdicts": {r["verdict"]: r["n"] for r in db.q("SELECT verdict, COUNT(*) n FROM scores GROUP BY verdict")},
        "avg_score": db.one("SELECT ROUND(AVG(score)) a FROM scores")["a"],
        "last_block": db.meta_get("last_block"),
        "window_hours": C.BACKFILL_HOURS,
    }
    feed = db.q("SELECT l.token,l.name,l.symbol,l.logo,l.description,l.twitter,l.telegram,l.website,l.pair_symbol,"
                " l.deployer,l.ts,l.grad_ts,l.grad_tx,l.creator_tax_bps, s.score,s.verdict,s.reasons,s.metrics,s.partial,"
                " s.scored_at, o.outcome,o.change_pct FROM launches l JOIN scores s ON s.token=l.token"
                " LEFT JOIN outcomes o ON o.token=l.token ORDER BY l.grad_block DESC LIMIT ?", (C.MAX_FEED,))
    feed = [_js(r, ("reasons", "metrics")) for r in feed]
    trust_cache = {}
    for r in feed:
        if r["deployer"] not in trust_cache:
            trust_cache[r["deployer"]] = creator_trust(db, r["deployer"])[0]
        r["trust"] = trust_cache[r["deployer"]]
    ticker = db.q("SELECT token,name,symbol,pair_symbol,ts,deployer FROM launches ORDER BY block DESC LIMIT 15")
    serial = db.q("SELECT deployer, COUNT(*) launches, SUM(graduated) grads, MAX(ts) last_ts FROM launches"
                  " GROUP BY deployer HAVING launches>=5 ORDER BY launches DESC LIMIT 10")
    rugs = {r["deployer"]: r["n"] for r in db.q("SELECT l.deployer, COUNT(*) n FROM outcomes o JOIN launches l"
                                                 " ON l.token=o.token WHERE o.outcome IN ('rugged','dumped') GROUP BY l.deployer")}
    for s in serial:
        s["rugged"] = rugs.get(s["deployer"], 0)
        s["trust"] = creator_trust(db, s["deployer"])[0]
    worst = db.q("SELECT l.token,l.name,l.symbol,l.deployer,s.score,s.verdict,s.reasons,l.grad_ts FROM scores s"
                 " JOIN launches l ON l.token=s.token WHERE s.verdict='avoid' ORDER BY s.scored_at DESC LIMIT 8")
    worst = [_js(r, ("reasons",)) for r in worst]
    char = treasury(rpc)
    runway = projection(db, char["usd"])
    brain_sum, lab_sum = brain.summary(), LB.summary(db)
    ready = RD.compute(brain_sum, lab_sum, runway, bool(char.get("demo")), TR.MAX_POSITION_USD)
    return {"now": now, "stats": st, "scout": _scout(db), "readiness": ready, "feed": feed, "ticker": ticker,
            "dig": (hub.dig if hub else []),
            "treasury": _treasury_cached(rpc, db), "voice": V.summary(db), "trader": TR.summary(db),
            "giving": G.summary(db), "compute": _compute_cached(), "live": C.LIVE, "lab": lab_sum,
            "bad_actors": {"serial": serial, "worst": worst},
            "paper": paper.summary(), "brain": brain_sum, "events": db.events(40),
            "character": char, "runway": runway, "disclosure": DISCLOSURE,
            "links": {"pons": "https://www.ponsfamily.com/launchpad/", "explorer": "https://robinhoodchain.blockscout.com/"}}


_scache = (0, None)


def _scout(db):
    """Headline numbers for the community: what the worm caught, cached 30 s."""
    global _scache
    if time.time() - _scache[0] < 30 and _scache[1]:
        return _scache[1]
    called = db.one("SELECT COUNT(*) n FROM outcomes WHERE resolved=1 AND verdict='avoid' AND outcome IN ('rugged','dumped')")["n"]
    checked = db.one("SELECT COUNT(*) n FROM outcomes WHERE resolved=1 AND verdict='avoid' AND outcome!='unknown'")["n"]
    missed = db.one("SELECT COUNT(*) n FROM outcomes WHERE resolved=1 AND verdict='looks healthy' AND outcome IN ('rugged','dumped')")["n"]
    creators = db.one("SELECT COUNT(*) n FROM (SELECT l.deployer d, COUNT(*) c, SUM(CASE WHEN o.outcome IN ('rugged','dumped')"
                      " THEN 1 ELSE 0 END) r FROM launches l LEFT JOIN outcomes o ON o.token=l.token GROUP BY l.deployer"
                      " HAVING c>=5 OR r>=1)")["n"]
    v = {"called": called, "checked_warnings": checked, "warn_precision": round(100.0 * called / checked) if checked else None,
         "missed": missed, "creators_flagged": creators}
    _scache = (time.time(), v)
    return v


_tcache = (0, None)


def _treasury_cached(rpc, db):
    global _tcache
    if time.time() - _tcache[0] < 60 and _tcache[1]:
        return _tcache[1]
    try:
        v = T.summary(rpc, db)
    except Exception as e:
        log.info("treasury summary failed: %s", e)
        v = _tcache[1] or {}
    _tcache = (time.time(), v)
    return v


_ccache = (0, None)


def _compute_cached():
    global _ccache
    if time.time() - _ccache[0] < 300 and _ccache[1]:
        return _ccache[1]
    try:
        acct = None
        if C.SECRET:
            from .wallet import account
            acct = account()
        v = CP.status(acct)
    except Exception as e:
        v = {"error": str(e)[:100]}
    _ccache = (time.time(), v)
    return v


def make_app(rpc, db, brain, paper, hub):
    app = FastAPI(title="IRL Worm", docs_url=None, redoc_url=None, openapi_url=None)
    clients = set()

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (WEB / "index.html").read_text(encoding="utf-8")

    @app.get("/docs", response_class=HTMLResponse)
    def docs():
        """How it works, the fee split, the policies. Static text with the live addresses filled in."""
        page = (WEB / "docs.html").read_text(encoding="utf-8")
        try:
            from eth_utils import to_checksum_address
            wallet_shown = to_checksum_address(C.WALLET) if C.WALLET else "not created yet"
        except Exception:
            wallet_shown = C.WALLET or "not created yet"
        fills = {"wallet": wallet_shown, "owner_share": str(int(round(C.OWNER_SHARE * 100))),
                 "treasury_share": str(100 - int(round(C.OWNER_SHARE * 100))), "ready_at": str(RD.READY_AT),
                 "token": (f'<a href="https://www.ponsfamily.com/launchpad/{C.TOKEN}" target="_blank" rel="noopener noreferrer">$WORM on pons</a>'
                           if C.TOKEN else "not launched yet"),
                 "explorer": "https://robinhoodchain.blockscout.com/",
                 "repo": (f'<a href="{C.REPO_URL}" target="_blank" rel="noopener noreferrer">{C.REPO_URL}</a>' if C.REPO_URL else "public repository coming with the launch")}
        for k, v in fills.items():
            page = page.replace("{{%s}}" % k, v)
        return page

    @app.get("/api/state")
    def state():
        return JSONResponse(snapshot(rpc, db, brain, paper, hub))

    @app.get("/api/rescan/{addr}")
    def rescan(addr: str, request: Request):
        """Re-score a token on demand. Loopback clients only: it costs RPC calls."""
        if request.client and request.client.host not in ("127.0.0.1", "::1"):
            return JSONResponse({"error": "local only"}, status_code=403)
        addr = addr.lower()
        if not db.one("SELECT 1 FROM launches WHERE token=? AND graduated=1", (addr,)):
            return JSONResponse({"error": "unknown graduated token"}, status_code=404)
        if hub.rescan:
            hub.rescan(addr)
        return {"queued": addr}

    @app.get("/api/token/{addr}")
    def token(addr: str):
        addr = addr.lower()
        l = db.one("SELECT * FROM launches WHERE token=?", (addr,))
        s = db.one("SELECT * FROM scores WHERE token=?", (addr,))
        o = db.one("SELECT * FROM outcomes WHERE token=?", (addr,))
        if s:
            _js(s, ("reasons", "metrics", "fired"))
        if o:
            _js(o, ("checks", "fired"))
        return JSONResponse({"launch": l, "score": s, "outcome": o})

    _img_cache = {}
    _CID = re.compile(r"^[A-Za-z0-9]{40,100}(?:/[A-Za-z0-9._-]{1,80})?$")

    @app.get("/ipfs/{cid:path}")
    def ipfs(cid: str):
        """Same-origin proxy for token logos on IPFS, so the browser can show them. Small images only."""
        if not _CID.match(cid):
            return Response(status_code=404)
        hit = _img_cache.get(cid)
        if hit is None:
            try:
                r = requests.get(f"https://ipfs.io/ipfs/{cid}", timeout=12, stream=True,
                                 headers={"User-Agent": C.UA})
                ctype = r.headers.get("content-type", "")
                body = r.raw.read(400_000 + 1)
                ok = r.status_code == 200 and ctype.startswith("image/") and len(body) <= 400_000
                hit = (ctype, body) if ok else ("", b"")
            except Exception:
                hit = ("", b"")
            if len(_img_cache) > 600:
                _img_cache.clear()
            _img_cache[cid] = hit
        if not hit[0]:
            return Response(status_code=404)
        return Response(content=hit[1], media_type=hit[0], headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/logo.png")
    def logo():
        return FileResponse(WEB / "logo.png", media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/healthz")
    def health():
        return {"ok": True, "last_block": db.meta_get("last_block")}

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        clients.add(sock)
        try:
            await sock.send_text(json.dumps({"type": "state", "data": snapshot(rpc, db, brain, paper, hub)}))
            if hub.frame_latest:
                await sock.send_text(json.dumps({"type": "frame", "data": hub.frame_latest}))
            while True:
                await asyncio.sleep(30)
                await sock.send_text(json.dumps({"type": "ping"}))
        except (WebSocketDisconnect, RuntimeError, Exception):
            pass
        finally:
            clients.discard(sock)

    async def broadcaster():
        last = 0
        while True:
            await asyncio.sleep(0.5)
            kick = False
            while not hub.pending.empty():
                item = hub.pending.get()
                if item["kind"] == "frame":
                    fr = hub.frame_latest
                    if fr and fr.get("ts") != getattr(broadcaster, "_last_frame_ts", None):
                        broadcaster._last_frame_ts = fr.get("ts")
                        msg = json.dumps({"type": "frame", "data": fr})
                        for s_ in list(clients):
                            try:
                                await s_.send_text(msg)
                            except Exception:
                                clients.discard(s_)
                    continue
                if item["kind"] == "scan":
                    msg = json.dumps({"type": "scan", "data": item["payload"]})
                    for s_ in list(clients):
                        try:
                            await s_.send_text(msg)
                        except Exception:
                            clients.discard(s_)
                    if item["payload"].get("step") != "verdict":
                        continue
                kick = True
            if clients and (kick or time.time() - last > 8):
                last = time.time()
                try:
                    data = await asyncio.get_event_loop().run_in_executor(None, snapshot, rpc, db, brain, paper, hub)
                except Exception as e:
                    log.warning("snapshot failed: %s", e)
                    continue
                msg = json.dumps({"type": "state", "data": data})
                for s in list(clients):
                    try:
                        await s.send_text(msg)
                    except Exception:
                        clients.discard(s)

    @app.on_event("startup")
    async def _start():
        asyncio.create_task(broadcaster())

    return app
