"""The live site: one page, a JSON snapshot, and a websocket that pushes fresh snapshots."""
import asyncio
from urllib.parse import urlparse
import collections
import hashlib
from contextlib import asynccontextmanager
import json
import logging
import os
import re
import secrets
import threading
import time

import requests
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.datastructures import MutableHeaders

from . import config as C
from . import launch_schedule
from .growth import treasury
from .learn import creator_trust
from .budget import projection
from . import treasury as T
from . import voice as V, trader as TR, compute as CP, lab as LB, readiness as RD, advisor as ADV
from . import watch as SL
from . import crowd as CROWD

log = logging.getLogger("wormhole.server")
WEB = C.ROOT / "web"
ASSET_TYPES = {"design.css": "text/css", "design.js": "application/javascript"}   # the only files under web/ served by name

DISCLOSURE = {
    "real": "Every number on this page is read from Robinhood Chain or GeckoTerminal: launches, graduations, "
            "curve buys, holders, swaps, prices. A score describes the assessment at scan time; what happened afterwards "
            "is shown separately, and incomplete chain reads are marked.",
    "not_real": "The verdicts are rules written by a person and re-weighted by outcomes: a screening aid, not an audit "
                "and not advice. Paper trades are simulated, the runway is a projection, and the worm trades nothing "
                "by policy.",
}

RESCAN_MAX = 100            # addresses waiting for a rescore before /api/rescan answers 429
RESCAN_DEDUPE_S = 600       # an address queued, being scored or scored this recently is not queued again
PENDING_MAX = 20            # broadcaster inbox: past this many waiting items the oldest frame kick is dropped
PENDING_HARD_MAX = 200      # and past this the oldest item of any kind is dropped (memory backstop)
SNAPSHOT_MAX_AGE_S = 3.0    # /api/state and the websocket reuse one built snapshot for this long
LOOPBACK = ("127.0.0.1", "::1")
WS_MAX_CLIENTS = 300        # live viewers at once; beyond this the page falls back to polling /api/state
WS_MAX_PER_IP = 8

# The page's own inline script and styles, Google Fonts, the same-origin API and websocket, and token
# logos (same-origin /ipfs proxy, data: frames from the screen, and the https logos the launchpad lists).
PAGE_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com data:; "
            "img-src 'self' data: blob: https:; connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")

IPFS_GATEWAYS = ("https://ipfs.io/ipfs/", "https://dweb.link/ipfs/", "https://w3s.link/ipfs/")
IMG_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"}   # raster only: no SVG, no HTML
IMG_MAX_BYTES = 400_000
IMG_TIMEOUT_S = 6           # per gateway
IMG_BUDGET_S = 10           # for all gateways together, so one slot is never held for three full timeouts
IMG_NEG_TTL_S = 600         # a CID that yielded nothing is not asked for again for this long
IMG_CACHE_MAX = 60          # at most 60 x IMG_MAX_BYTES in memory: the container has 1 GB with Chromium in it
IMG_HEADERS = {"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff",
               "Content-Security-Policy": "default-src 'none'; sandbox", "Content-Disposition": "inline"}
_CID = re.compile(r"^[A-Za-z0-9]{40,100}(?:/(?!\.+$)[A-Za-z0-9._-]{1,80})?$")   # one optional file segment, never just dots


class _Pending:
    """The broadcaster's inbox, filled from worker threads and drained by the event loop. Bounded: past
    PENDING_MAX waiting items the oldest frame kick goes first (a frame item carries no payload, the newest
    frame is always hub.frame_latest); state and scan messages are kept up to PENDING_HARD_MAX."""

    def __init__(self, limit=PENDING_MAX, hard=PENDING_HARD_MAX):
        self._d = collections.deque()
        self._lock = threading.Lock()
        self.limit, self.hard = limit, hard
        self.dropped = 0

    def put(self, item):
        with self._lock:
            self._d.append(item)
            while len(self._d) > self.limit:
                i = next((i for i, it in enumerate(self._d) if it.get("kind") == "frame"), None)
                if i is None:
                    break
                del self._d[i]
                self.dropped += 1
            while len(self._d) > self.hard:
                self._d.popleft()
                self.dropped += 1

    def get(self):
        with self._lock:
            return self._d.popleft()     # IndexError when empty, like deque

    def empty(self):
        with self._lock:
            return not self._d

    def qsize(self):
        with self._lock:
            return len(self._d)


class Hub:
    def __init__(self):
        self.started_at = time.time()
        self.pending = _Pending()
        self.dig = []            # events of the dig in progress (or the last one)
        self.rescan = None       # set by run.py: callable(token) that queues a rescore
        self.frame_latest = None
        self.action_latest = None
        self.launch_wanted = False   # set by /api/launch; the runner's launch stage acts on it once
        self._queued = {}        # token -> when /api/rescan queued it; cleared by mark_scoring
        self._started = {}       # token -> when the worker started scoring it
        self._qlock = threading.Lock()

    def notify(self, kind, payload=None):
        self.pending.put({"kind": kind, "payload": payload, "ts": int(time.time())})

    def frame(self, data):
        self.frame_latest = data
        self.pending.put({"kind": "frame", "payload": None, "ts": data.get("ts")})

    def act(self, ev):
        """A transaction the worm sent or settled: the page shows it in the screen's header and the status bar."""
        self.action_latest = ev
        self.pending.put({"kind": "action", "payload": ev, "ts": ev.get("ts")})

    def scan(self, ev):
        if ev.get("step") == "start":
            self.dig = []
        self.dig = (self.dig + [ev])[-8:]
        self.pending.put({"kind": "scan", "payload": ev, "ts": ev.get("ts")})

    def persist(self, db):
        """The dig in progress (or the last one) into the database, so a restart does not blank the panel."""
        try:
            db.meta_set("last_dig", json.dumps(self.dig))
        except Exception as e:
            log.info("dig not persisted: %s", e)

    def restore(self, db):
        """The last dig from the database at startup; nothing when there is none or it is unreadable."""
        try:
            saved = json.loads(db.meta_get("last_dig") or "[]")
            if isinstance(saved, list) and all(isinstance(e, dict) and e.get("step") for e in saved):
                self.dig = saved[-8:]
        except Exception as e:
            log.info("dig not restored: %s", e)

    # ---- the rescan registry: what /api/rescan has queued, bounded and deduplicated ----
    def _prune(self, now):
        """Entries expire after RESCAN_DEDUPE_S, so a worker that never reports back cannot wedge the cap."""
        for d in (self._queued, self._started):
            for k in [k for k, ts in d.items() if now - ts > RESCAN_DEDUPE_S]:
                del d[k]

    def queue_size(self):
        with self._qlock:
            self._prune(time.time())
            return len(self._queued)

    def enqueue(self, token):
        """Queue a rescore. Returns (True, None) or (False, why) when the address is already waiting, was
        started recently, the queue is full, or no worker is attached."""
        with self._qlock:
            now = time.time()
            self._prune(now)
            if token in self._queued:
                return False, "already queued"
            if token in self._started:
                return False, "scoring started %d s ago" % (now - self._started[token])
            if len(self._queued) >= RESCAN_MAX:
                return False, "queue full"
            if not self.rescan:
                return False, "no worker"
            self._queued[token] = now
        self.rescan(token)
        return True, None

    def mark_scoring(self, token):
        """The worker calls this when it starts scoring a token (run.py); the address may be queued again
        RESCAN_DEDUPE_S later, or sooner once a fresh score is in the database."""
        with self._qlock:
            self._queued.pop(token, None)
            self._started[token] = time.time()


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
        "queued": db.one("SELECT COUNT(*) n FROM scan_jobs WHERE state IN ('pending','leased')")["n"],
        "failed_scans": db.one("SELECT COUNT(*) n FROM scan_jobs WHERE state='failed'")["n"],
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
    serial = db.q("SELECT deployer, COUNT(*) launches, SUM(graduated) grads, MAX(ts) last_ts FROM launches WHERE ts>=?"
                  " GROUP BY deployer HAVING launches>=5 ORDER BY launches DESC LIMIT 10", (now - 7 * 86400,))
    rugs = {r["deployer"]: r["n"] for r in db.q("SELECT l.deployer, COUNT(*) n FROM outcomes o JOIN launches l"
                                                 " ON l.token=o.token WHERE o.outcome IN ('rugged','dumped') GROUP BY l.deployer")}
    for s in serial:
        s["rugged"] = rugs.get(s["deployer"], 0)
        s["trust"] = creator_trust(db, s["deployer"])[0]
    worst = db.q("SELECT l.token,l.name,l.symbol,l.deployer,s.score,s.verdict,s.reasons,l.grad_ts FROM scores s"
                 " JOIN launches l ON l.token=s.token WHERE s.verdict='avoid' ORDER BY s.scored_at DESC LIMIT 8")
    worst = [_js(r, ("reasons",)) for r in worst]
    char = treasury(rpc)
    runway = projection(db, T.free_usd(db, char.get("usd_real", char["usd"])))   # real money only, minus what is owed away
    brain_sum, lab_sum = brain.summary(), LB.summary(db)
    ready = RD.compute(brain_sum, lab_sum, runway, TR.MAX_POSITION_USD)
    return {"now": now, "launch": launch_schedule.status(db, now), "stats": st, "scout": _scout(db), "readiness": ready, "lessons": _lessons(db), "feed": feed, "ticker": ticker,
            "dig": (hub.dig if hub else []), "screen_on": bool(getattr(hub, "screen_on", True)) if hub else True,
            "treasury": _treasury_cached(rpc, db), "voice": V.summary(db), "trader": TR.summary(db),
            "compute": _compute_cached(), "live": C.LIVE, "lab": lab_sum,
            "bad_actors": {"serial": serial, "worst": worst},
            "paper": paper.summary(), "second_look": SL.summary(db), "crowd": CROWD.summary(db), "brain": brain_sum, "events": db.events(60), "advisor": ADV.summary(db),
            "character": char, "runway": runway, "disclosure": DISCLOSURE,
            "links": {"pons": "https://www.ponsfamily.com/launchpad/", "explorer": "https://robinhoodchain.blockscout.com/",
                      "x": C.X_URL or None, "site": C.SITE_URL}}


class SnapshotCache:
    """One snapshot, rebuilt at most every SNAPSHOT_MAX_AGE_S and by one thread at a time: /api/state, the
    websocket hello and the broadcaster all read it, so a burst of requests costs one build under the
    database lock instead of one each. force=True (a verdict just landed) rebuilds regardless of age."""

    def __init__(self, build, max_age=SNAPSHOT_MAX_AGE_S):
        self._build, self.max_age = build, max_age
        self._lock = threading.Lock()
        self.ts, self.data, self.builds = 0.0, None, 0

    def get(self, force=False):
        with self._lock:
            if self.data is None or force or time.monotonic() - self.ts > self.max_age:
                self.data = self._build()
                self.ts = time.monotonic()
                self.builds += 1
            return self.data


def _lessons(db, limit=8):
    """The latest resolved verdicts as plain lessons: what happened, was the call right, which rules moved."""
    rows = db.q("SELECT o.token,o.score,o.verdict,o.outcome,o.change_pct,o.fired,o.scored_at,l.symbol,l.name FROM outcomes o"
                " LEFT JOIN launches l ON l.token=o.token WHERE o.resolved=1 AND o.outcome!='unknown' ORDER BY o.scored_at DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        bad, good = r["outcome"] in ("rugged", "dumped"), r["outcome"] == "grew"
        try:
            fired = json.loads(r["fired"] or "[]")
        except Exception:
            fired = []
        up = [f["rule"] for f in fired if f.get("points", 0) and ((f["points"] < 0 and bad) or (f["points"] > 0 and good))]
        down = [f["rule"] for f in fired if f.get("points", 0) and ((f["points"] > 0 and bad) or (f["points"] < 0 and good))]
        tag = ("called it" if ((r["verdict"] == "avoid" and bad) or (r["verdict"] == "looks healthy" and good))
               else "missed it" if ((r["verdict"] == "looks healthy" and bad) or (r["verdict"] == "avoid" and good)) else "no lesson")
        out.append({"token": r["token"], "symbol": r["symbol"], "name": r["name"], "verdict": r["verdict"], "score": r["score"],
                    "outcome": r["outcome"], "change_pct": r["change_pct"], "tag": tag, "up": up, "down": down, "scored_at": r["scored_at"]})
    return out


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
    except Exception:
        v = {"error": "compute status unavailable"}
    _ccache = (time.time(), v)
    return v


# ---- the IPFS logo proxy: raster images only, three public gateways, small bounded cache ----
_img_cache = collections.OrderedDict()   # cid -> (content_type, body, fetched_at); content_type "" = nothing usable
_img_lock = threading.Lock()
_img_slots = threading.Semaphore(4)      # upstream fetches in flight at once


def _img_get(cid):
    with _img_lock:
        hit = _img_cache.get(cid)
        if hit is None:
            return None
        if not hit[0] and time.time() - hit[2] > IMG_NEG_TTL_S:
            del _img_cache[cid]
            return None
        return hit


def _img_put(cid, ctype, body):
    with _img_lock:
        _img_cache[cid] = (ctype, body, time.time())
        _img_cache.move_to_end(cid)
        while len(_img_cache) > IMG_CACHE_MAX:
            _img_cache.popitem(last=False)


def fetch_ipfs(cid):
    """One CID from the public gateways in turn. The next gateway is tried on 429, 5xx, timeouts and
    connection errors; a 404, a non-raster type or an oversized body ends the search, since every gateway
    serves the same bytes. Returns (content_type, body) or ("", b"")."""
    deadline = time.monotonic() + IMG_BUDGET_S
    for base in IPFS_GATEWAYS:
        left = deadline - time.monotonic()
        if left < 1:
            break
        try:
            r = requests.get(base + cid, timeout=min(IMG_TIMEOUT_S, left), stream=True,
                             headers={"User-Agent": C.UA, "Accept": "image/*", "Accept-Encoding": "identity"})
        except Exception as e:
            log.debug("ipfs %s via %s: %s", cid[:12], base, str(e)[:80])
            continue
        try:
            if r.status_code == 429 or r.status_code >= 500:
                continue
            if r.status_code != 200:
                return "", b""
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            if ctype not in IMG_TYPES:
                return "", b""
            body = r.raw.read(IMG_MAX_BYTES + 1)
            if len(body) > IMG_MAX_BYTES:
                return "", b""
            return ctype, body
        except Exception as e:
            log.debug("ipfs %s read via %s: %s", cid[:12], base, str(e)[:80])
            continue
        finally:
            try:
                r.close()
            except Exception:
                pass
    return "", b""


class SecurityHeaders:
    """nosniff, no referrer and no framing on every response, a content security policy on the HTML pages,
    and the cache rule for / and /api/state. A route that sets one of these itself (the /ipfs proxy) wins."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                h.setdefault("x-content-type-options", "nosniff")
                h.setdefault("referrer-policy", "no-referrer")
                h.setdefault("x-frame-options", "DENY")
                h.setdefault("strict-transport-security", "max-age=31536000")
                h.setdefault("permissions-policy", "camera=(), microphone=(), geolocation=(), payment=()")
                if h.get("content-type", "").lower().startswith("text/html"):
                    h.setdefault("content-security-policy", PAGE_CSP)
                if path == "/api/state":
                    h.setdefault("cache-control", "no-store")
            await send(message)

        await self.app(scope, receive, send_with_headers)


def rescan_allowed(request):
    """The real TCP peer is loopback (run.py starts uvicorn with proxy_headers=False, so a forwarded header
    cannot forge it) or the caller presents WH_RESCAN_TOKEN. No token configured: loopback only."""
    host = request.client.host if request.client else ""
    want = os.environ.get("WH_RESCAN_TOKEN", "")
    if host in LOOPBACK and not want:              # once a token is configured, even loopback presents it
        return True
    got = request.headers.get("x-rescan-token", "")
    return bool(want) and bool(got) and secrets.compare_digest(want.encode(), got.encode())


def ops_allowed(request):
    """Every caller presents WH_OPS_TOKEN: the guard on the one route that moves real money."""
    host = request.client.host if request.client else ""
    want = os.environ.get("WH_OPS_TOKEN", "")
    got = request.headers.get("x-ops-token", "")
    return bool(want) and bool(got) and secrets.compare_digest(want.encode(), got.encode())


def make_app(rpc, db, brain, paper, hub):
    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(broadcaster())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(title="Worm", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(SecurityHeaders)
    clients = set()
    snap = SnapshotCache(lambda: snapshot(rpc, db, brain, paper, hub))
    app.state.snapshots = snap

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        """The page, revalidated on every load like its assets: a new build shows at once, never after a cache."""
        return asset("index.html", request, media_type="text/html; charset=utf-8")

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
                 "burn_share": str(int(round(C.BURN_SHARE * 100))), "ops_share": str(int(round(C.OPS_SHARE * 100))),
                 "gold_share": str(int(round(C.GOLD_SHARE * 100))),
                 "treasury_share": str(int(round(C.OPS_SHARE * 100))), "ready_at": str(RD.READY_AT),
                 "token": (f'<a href="https://www.ponsfamily.com/launchpad/{C.TOKEN}" target="_blank" rel="noopener noreferrer">$WORM on pons</a>'
                           if C.TOKEN else "not launched yet"),
                 "explorer": "https://robinhoodchain.blockscout.com/",
                 "x_link": (f'<a href="{C.X_URL}" target="_blank" rel="noopener noreferrer">the worm on X</a>' if C.X_URL else ""),
                 "repo": (f'<a href="{C.REPO_URL}" target="_blank" rel="noopener noreferrer">{C.REPO_URL}</a>' if C.REPO_URL else "public repository coming with the launch")}
        for k, v in fills.items():
            page = page.replace("{{%s}}" % k, v)
        return page

    @app.get("/api/state")
    def state():
        return JSONResponse(snap.get())

    @app.get('/api/launch/status')
    def launch_status():
        return JSONResponse(launch_schedule.status(db), headers={'Cache-Control':'no-store'})

    @app.post('/api/launch/schedule')
    async def set_launch_schedule(request: Request):
        if not ops_allowed(request):
            return JSONResponse({'error':'operations authentication required'}, status_code=403)
        body = await request.body()
        if len(body) > 1024:
            return JSONResponse({'error':'request too large'}, status_code=413)
        try:
            data = json.loads(body)
            if not isinstance(data, dict) or set(data) != {'at'}:
                raise ValueError('provide only at: a timezone-qualified date, or null to cancel')
            with launch_schedule.LOCK:
                if hub.launch_wanted:
                    raise ValueError('a manual launch is already queued')
                result = launch_schedule.configure(db, data['at'])
            return JSONResponse(result)
        except (ValueError, TypeError):
            return JSONResponse({'error':'invalid schedule: use a future ISO date with timezone; an existing launch cannot be replaced'}, status_code=400)

    @app.post("/api/launch")
    def launch_(request: Request):
        """The creator's trigger: the worm launches its own token on its next cycle, from inside its own
        process, so the screen shows it as it happens. WH_OPS_TOKEN required for every caller; nothing signs unless
        WH_LIVE=1; never twice."""
        if not ops_allowed(request):
            return JSONResponse({"error": "operations authentication required"}, status_code=403)
        with launch_schedule.LOCK:
            if C.TOKEN:
                return {"queued": False, "token": C.TOKEN, "why": "the worm already has its token"}
            if not C.LIVE:
                return JSONResponse({"queued": False, "why": "WH_LIVE=0: the worm would not sign"}, status_code=409)
            if db.meta_get("launch_pending"):
                return {"queued": False, "why": "a launch is already in flight"}
            if db.meta_get('launch_allocation'):
                return {"queued": False, "why": "an initial allocation already exists; resume or review its saved state"}
            hub.launch_wanted = True
            return {"queued": True, "why": "the worm launches on its next cycle; watch the screen"}

    @app.get("/api/rescan/{addr}")
    def rescan(addr: str, request: Request):
        """Re-score a token on demand. It costs RPC calls, so only the local machine or a caller holding
        WH_RESCAN_TOKEN may ask; the queue is capped and an address is not queued twice."""
        if not rescan_allowed(request):
            return JSONResponse({"error": "local only"}, status_code=403)
        addr = addr.lower()
        row = db.one("SELECT s.scored_at FROM launches l LEFT JOIN scores s ON s.token=l.token"
                     " WHERE l.token=? AND l.graduated=1", (addr,)) if re.fullmatch(r"0x[0-9a-f]{40}", addr) else None
        if not row:
            return JSONResponse({"error": "unknown graduated token"}, status_code=404)
        if C.TOKEN and addr == C.TOKEN:
            return JSONResponse({"error": "the worm does not grade its own token"}, status_code=400)
        now = int(time.time())
        if row["scored_at"] and now - row["scored_at"] < RESCAN_DEDUPE_S:
            return {"queued": False, "why": "scored %d s ago" % (now - row["scored_at"])}
        ok, why = hub.enqueue(addr)
        if ok:
            return {"queued": addr}
        if why == "queue full":
            return JSONResponse({"error": "rescan queue full", "queued": False}, status_code=429)
        if why == "no worker":
            return JSONResponse({"queued": False, "why": why}, status_code=503)
        return {"queued": False, "why": why}          # already queued, scoring started N s ago

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

    @app.get("/ipfs/{cid:path}")
    def ipfs(cid: str):
        """Same-origin proxy for token logos on IPFS, so the browser can show them. Small raster images only,
        served with nosniff and a sandboxing policy so nothing fetched here can run as this origin."""
        if not _CID.match(cid):
            return Response(status_code=404)
        hit = _img_get(cid)
        if hit is None:
            if not _img_slots.acquire(blocking=False):     # never park a request thread: the pool serves the pages
                return Response(status_code=503, headers={"Retry-After": "5"})
            try:
                hit = _img_get(cid)          # another thread may have fetched it while this one waited
                if hit is None:
                    ctype, body = fetch_ipfs(cid)
                    _img_put(cid, ctype, body)
                    hit = (ctype, body, time.time())
            finally:
                _img_slots.release()
        if not hit[0]:
            return Response(status_code=404, headers={"Cache-Control": "public, max-age=600"})
        return Response(content=hit[1], media_type=hit[0], headers=IMG_HEADERS)

    @app.get("/logo.png")
    def logo():
        return FileResponse(WEB / "logo.png", media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})

    asset_tags = {}   # name -> ((mtime_ns, size), etag): the content hash, recomputed when the file changes

    def asset(name, request, media_type=None):
        """The page's stylesheet and script, each by its exact name: no directory is mounted, so nothing else under
        web/ (or anywhere) is reachable. no-cache makes the browser revalidate on every page load, and a content
        ETag answers 304 while the file is unchanged, so a new build shows with the page instead of after a cache."""
        path = WEB / name
        st = path.stat()
        tag = asset_tags.get(name)
        if not tag or tag[0] != (st.st_mtime_ns, st.st_size):
            tag = ((st.st_mtime_ns, st.st_size), '"%s"' % hashlib.sha256(path.read_bytes()).hexdigest()[:32])
            asset_tags[name] = tag
        headers = {"Cache-Control": "no-cache", "ETag": tag[1]}
        if request.headers.get("if-none-match", "").replace("W/", "").strip() == tag[1]:
            return Response(status_code=304, headers=headers)
        return FileResponse(path, media_type=media_type or ASSET_TYPES[name], headers=headers)

    @app.get("/design.css")
    def design_css(request: Request):
        return asset("design.css", request)

    @app.get("/design.js")
    def design_js(request: Request):
        return asset("design.js", request)

    @app.get("/api/ops/health")
    def operations_health(request: Request):
        expected = os.environ.get('WH_HEALTH_TOKEN', '')
        supplied = request.headers.get('x-health-token', '')
        if not expected or not supplied or not secrets.compare_digest(expected.encode(), supplied.encode()):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from . import ops_health
        status = ops_health.check(rpc, db, hub)
        return JSONResponse(status, status_code=200 if status['ok'] else 503)

    @app.get("/healthz")
    def health():
        """ok while the indexer has read a block in the last 180 s (run.py sets hub.indexer); 503 otherwise, so
        the host restarts a wedged process. Without an indexer attached the answer is ok."""
        idx = getattr(hub, "indexer", None)
        last_ok = getattr(idx, "last_ok", None) if idx else None
        age = round(time.time() - last_ok) if last_ok else None
        ready = getattr(idx, "ready", None)
        catching_up = bool(ready is not None and not ready.is_set())   # first backfill: the host must not restart us
        ok = age is None or age < 180 or catching_up
        return JSONResponse({"ok": ok, "last_block": db.meta_get("last_block"), "indexer_age_s": age,
                             "catching_up": catching_up}, status_code=200 if ok else 503)

    async def _broadcast(msg):
        """One message to every client, each with its own 5 s limit: a stalled client loses its socket
        instead of holding up everyone else's frames."""
        async def one(s):
            try:
                await asyncio.wait_for(s.send_text(msg), timeout=5)
            except Exception:
                clients.discard(s)
        if clients:
            await asyncio.gather(*(one(s) for s in list(clients)))

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        origin, host = sock.headers.get("origin", ""), sock.headers.get("host", "")
        if origin and (urlparse(origin).hostname or "") != host.split(":")[0]:   # the page's own origin only
            await sock.close(code=1008)
            return
        peer = sock.client.host if sock.client else ""
        if len(clients) >= WS_MAX_CLIENTS or sum(1 for c in clients if c.client and c.client.host == peer) >= WS_MAX_PER_IP:
            await sock.close(code=1013)
            return
        await sock.accept()
        clients.add(sock)
        try:
            data = await asyncio.get_running_loop().run_in_executor(None, snap.get)
            await asyncio.wait_for(sock.send_text(json.dumps({"type": "state", "data": data})), timeout=5)
            if hub.frame_latest:
                await asyncio.wait_for(sock.send_text(json.dumps({"type": "frame", "data": hub.frame_latest})), timeout=5)
            while True:
                try:                                  # drain whatever the client sends (nothing is read from it)
                    msg = await asyncio.wait_for(sock.receive(), timeout=30)
                    if msg.get("type") == "websocket.disconnect":
                        break
                    continue
                except asyncio.TimeoutError:
                    pass
                await asyncio.wait_for(sock.send_text(json.dumps({"type": "ping"})), timeout=5)
        except (WebSocketDisconnect, RuntimeError, Exception):
            pass
        finally:
            clients.discard(sock)

    async def broadcaster():
        last, last_frame_ts = 0, None
        while True:
            await asyncio.sleep(0.5)
            kick = False
            while not hub.pending.empty():
                try:
                    item = hub.pending.get()
                except IndexError:
                    break
                if item["kind"] == "frame":
                    fr = hub.frame_latest
                    if fr and fr.get("ts") != last_frame_ts:
                        last_frame_ts = fr.get("ts")
                        await _broadcast(json.dumps({"type": "frame", "data": fr}))
                    continue
                if item["kind"] == "scan":
                    await _broadcast(json.dumps({"type": "scan", "data": item["payload"]}))
                    if (item.get("payload") or {}).get("step") != "verdict":
                        continue
                kick = True
            if clients and (kick or time.time() - last > 8):
                last = time.time()
                try:
                    data = await asyncio.get_running_loop().run_in_executor(None, snap.get, kick)
                except Exception as e:
                    log.warning("snapshot failed: %s", e)
                    continue
                await _broadcast(json.dumps({"type": "state", "data": data}))

    return app
