"""Offline tests for the web layer: FastAPI's TestClient over make_app with a temporary database, a fake
RPC that refuses every call and requests monkeypatched per test. Every socket connect is blocked too, so
no test can reach a node, a gateway or an API by accident."""
import re
import socket
import time

import pytest
import requests
from fastapi.testclient import TestClient

from wormhole import server
from wormhole.server import Hub, make_app

ADDR = "0x" + "ab" * 20
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 24
CID0 = "Qm" + "Y" * 44                      # CIDv0 shape: 46 base58 characters
CID1 = "bafybei" + "a" * 52                 # CIDv1 shape: 59 characters


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test tried to use the network")
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(requests.Session, "request", refuse)


class FakeRpc:
    """Every call fails: nothing in these tests may reach a node."""
    def __getattr__(self, name):
        def refuse(*a, **k):
            raise RuntimeError("rpc is off in tests")
        return refuse


class FakeBrain:
    def summary(self):
        return {"rules": [], "counts": {}, "scorecard": {}, "tracked": 0, "resolved": 0}

    def weights(self):
        return {}


class FakePaper:
    def summary(self):
        return {"open": [], "closed": []}


def small_snapshot(*a, **k):
    return {"now": int(time.time()), "stats": {"scored": 1}}


@pytest.fixture
def site(db, monkeypatch):
    """(client, hub, queued) over a stubbed snapshot. The TestClient's peer is 'testclient', not loopback."""
    monkeypatch.setattr(server, "snapshot", small_snapshot)
    hub = Hub()
    queued = []
    hub.rescan = queued.append
    app = make_app(FakeRpc(), db, FakeBrain(), FakePaper(), hub)
    return TestClient(app), hub, queued


def loopback(site):
    """The same app seen from a real loopback TCP peer."""
    client, hub, queued = site
    return TestClient(client.app, client=("127.0.0.1", 40000)), hub, queued


def add_grad(db, token, scored_at=None):
    now = int(time.time())
    db.x("INSERT INTO launches(token,deployer,block,ts,graduated,grad_block,grad_ts,name,symbol) VALUES(?,?,?,?,1,?,?,?,?)",
         (token, "0x" + "cd" * 20, 1, now - 600, 2, now - 300, "Tok", "TOK"))
    if scored_at:
        db.x("INSERT INTO scores(token,score,verdict,reasons,metrics,scored_at) VALUES(?,?,?,?,?,?)",
             (token, 50, "mixed", "[]", "{}", scored_at))


# ---- /api/rescan --------------------------------------------------------------------------------

def test_rescan_rejects_forged_forwarded_for(site, db):
    client, hub, queued = site
    add_grad(db, ADDR)
    r = client.get(f"/api/rescan/{ADDR}", headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"})
    assert r.status_code == 403 and queued == []
    assert client.get(f"/api/rescan/{ADDR}").status_code == 403


def test_rescan_loopback_peer_is_queued(site, db):
    client, hub, queued = loopback(site)
    add_grad(db, ADDR)
    r = client.get(f"/api/rescan/{ADDR}")
    assert r.status_code == 200 and r.json() == {"queued": ADDR} and queued == [ADDR]


def test_rescan_token(site, db, monkeypatch):
    client, hub, queued = site
    add_grad(db, ADDR)
    monkeypatch.delenv("WH_RESCAN_TOKEN", raising=False)
    assert client.get(f"/api/rescan/{ADDR}", headers={"X-Rescan-Token": "anything"}).status_code == 403   # unset: loopback only
    monkeypatch.setenv("WH_RESCAN_TOKEN", "t0k3n-for-tests-only")
    assert client.get(f"/api/rescan/{ADDR}", headers={"X-Rescan-Token": "wrong"}).status_code == 403
    assert client.get(f"/api/rescan/{ADDR}", headers={"X-Rescan-Token": ""}).status_code == 403
    assert client.get(f"/api/rescan/{ADDR}").status_code == 403
    r = client.get(f"/api/rescan/{ADDR}", headers={"X-Rescan-Token": "t0k3n-for-tests-only"})
    assert r.status_code == 200 and r.json() == {"queued": ADDR} and queued == [ADDR]


def test_rescan_unknown_token_is_404(site):
    client, hub, queued = loopback(site)
    assert client.get(f"/api/rescan/{ADDR}").status_code == 404
    assert client.get("/api/rescan/not-an-address").status_code == 404
    assert queued == []


def test_rescan_queue_is_capped_and_deduplicated(site, db):
    client, hub, queued = loopback(site)
    addrs = ["0x" + ("%040x" % i) for i in range(1, server.RESCAN_MAX + 2)]
    for a in addrs:
        add_grad(db, a)
    for a in addrs[:server.RESCAN_MAX]:
        assert client.get(f"/api/rescan/{a}").json() == {"queued": a}
    r = client.get(f"/api/rescan/{addrs[-1]}")
    assert r.status_code == 429 and addrs[-1] not in queued
    # the same address is not queued twice while it waits
    r = client.get(f"/api/rescan/{addrs[0]}")
    assert r.status_code == 200 and r.json() == {"queued": False, "why": "already queued"}
    assert queued.count(addrs[0]) == 1
    # the worker took it: the slot frees up, the address itself stays deduplicated for a while
    hub.mark_scoring(addrs[0])
    assert hub.queue_size() == server.RESCAN_MAX - 1
    assert client.get(f"/api/rescan/{addrs[0]}").json()["why"].startswith("scoring started")
    assert client.get(f"/api/rescan/{addrs[-1]}").json() == {"queued": addrs[-1]}


def test_rescan_recently_scored_is_not_requeued(site, db):
    client, hub, queued = loopback(site)
    add_grad(db, ADDR, scored_at=int(time.time()) - 30)
    r = client.get(f"/api/rescan/{ADDR}")
    assert r.json()["queued"] is False and r.json()["why"].startswith("scored ") and queued == []
    old = "0x" + "ef" * 20
    add_grad(db, old, scored_at=int(time.time()) - 3600)
    assert client.get(f"/api/rescan/{old}").json() == {"queued": old}


def test_hub_registry_expires_stale_entries(monkeypatch):
    hub = Hub()
    hub.rescan = lambda t: None
    assert hub.enqueue(ADDR) == (True, None) and hub.enqueue(ADDR) == (False, "already queued")
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + server.RESCAN_DEDUPE_S + 1)
    assert hub.queue_size() == 0 and hub.enqueue(ADDR) == (True, None)
    assert Hub().enqueue(ADDR) == (False, "no worker")


# ---- /ipfs ----------------------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, ctype="image/png", body=PNG):
        self.status_code, self.headers, self._body = status, {"content-type": ctype}, body
        self.raw = self

    def read(self, n=-1, **kw):
        return self._body if n is None or n < 0 else self._body[:n]

    def close(self):
        pass


def gateways(monkeypatch, answers):
    """requests.get that answers per gateway host (an exception value is raised). Returns the urls asked."""
    seen = []

    def get(url, **kw):
        seen.append(url)
        assert 0 < kw["timeout"] <= server.IMG_TIMEOUT_S and kw["stream"] is True
        for host, ans in answers.items():
            if host in url:
                if isinstance(ans, Exception):
                    raise ans
                return ans
        raise AssertionError("unexpected gateway " + url)
    monkeypatch.setattr(server.requests, "get", get)
    return seen


def test_ipfs_rejects_svg(site, monkeypatch):
    client = site[0]
    seen = gateways(monkeypatch, {"ipfs.io": FakeResponse(ctype="image/svg+xml", body=b"<svg onload=alert(1)></svg>")})
    cid = "Qm" + "S" * 44
    r = client.get(f"/ipfs/{cid}")
    assert r.status_code == 404 and b"svg" not in r.content
    assert seen == [f"https://ipfs.io/ipfs/{cid}"]          # wrong type: every gateway serves the same bytes
    assert client.get(f"/ipfs/{cid}").status_code == 404 and len(seen) == 1


def test_ipfs_png_is_served_sandboxed(site, monkeypatch):
    client = site[0]
    gateways(monkeypatch, {"ipfs.io": FakeResponse()})
    cid = "Qm" + "P" * 44
    r = client.get(f"/ipfs/{cid}")
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert r.headers["content-disposition"] == "inline"
    assert "max-age=86400" in r.headers["cache-control"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["referrer-policy"] == "no-referrer"


def test_ipfs_cid_shapes(site, monkeypatch):
    client = site[0]
    seen = gateways(monkeypatch, {"ipfs.io": FakeResponse()})
    ok = "Qm" + "a" * 44
    for bad in ["../etc/passwd", "%2e%2e/%2e%2e/etc/passwd", "Qm", "Q" * 101, ok + "?x=1", ok + "/../x", ok + "/..",
                ok + "/logo.svg/", ok + "/a/b", "Qm" + "a" * 43 + "!"]:
        assert not server._CID.match(bad), bad
    assert client.get("/ipfs/Qm").status_code == 404
    assert client.get("/ipfs/" + "Q" * 101).status_code == 404
    assert client.get(f"/ipfs/{ok}/..").status_code == 404
    assert seen == []
    assert server._CID.match(CID0) and server._CID.match(CID1) and server._CID.match(CID1 + "/logo.png")
    assert client.get(f"/ipfs/{CID1}/logo.png").status_code == 200


def test_ipfs_falls_back_to_the_next_gateway_on_429(site, monkeypatch):
    client = site[0]
    seen = gateways(monkeypatch, {"ipfs.io": FakeResponse(status=429, ctype="text/plain", body=b"slow down"),
                                  "dweb.link": FakeResponse()})
    cid = "Qm" + "F" * 44
    r = client.get(f"/ipfs/{cid}")
    assert r.status_code == 200 and r.content == PNG
    assert seen == [f"https://ipfs.io/ipfs/{cid}", f"https://dweb.link/ipfs/{cid}"]


def test_ipfs_timeouts_and_5xx_fall_through(site, monkeypatch):
    client = site[0]
    seen = gateways(monkeypatch, {"ipfs.io": requests.Timeout("slow"),
                                  "dweb.link": FakeResponse(status=502, ctype="text/html", body=b"bad gateway"),
                                  "cloudflare-ipfs.com": FakeResponse(ctype="image/webp; charset=binary", body=b"RIFF" + b"\0" * 20)})
    cid = "Qm" + "T" * 44
    r = client.get(f"/ipfs/{cid}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert [u.split("/")[2] for u in seen] == ["ipfs.io", "dweb.link", "cloudflare-ipfs.com"]
    cid2 = "Qm" + "U" * 44
    gateways(monkeypatch, {"ipfs.io": requests.ConnectionError("down"), "dweb.link": requests.Timeout("slow"),
                           "cloudflare-ipfs.com": FakeResponse(status=503, ctype="text/plain", body=b"")})
    assert client.get(f"/ipfs/{cid2}").status_code == 404


def test_ipfs_404_and_oversize_are_cached_negatively(site, monkeypatch):
    client = site[0]
    seen = gateways(monkeypatch, {"ipfs.io": FakeResponse(status=404, ctype="text/plain", body=b"nope")})
    cid = "Qm" + "N" * 44
    assert client.get(f"/ipfs/{cid}").status_code == 404
    assert client.get(f"/ipfs/{cid}").status_code == 404
    assert len(seen) == 1                                       # the second answer came from the negative cache
    gateways(monkeypatch, {"ipfs.io": FakeResponse(body=b"\x89PNG" + b"\0" * server.IMG_MAX_BYTES)})
    assert client.get("/ipfs/Qm" + "B" * 44).status_code == 404


def test_ipfs_negative_entries_expire_and_cache_is_capped(monkeypatch):
    server._img_cache.clear()
    server._img_put("x" * 46, "", b"")
    assert server._img_get("x" * 46) is not None
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + server.IMG_NEG_TTL_S + 1)
    assert server._img_get("x" * 46) is None                     # an expired negative entry is asked for again
    for i in range(server.IMG_CACHE_MAX + 50):
        server._img_put("c%045d" % i, "image/png", PNG)
    assert len(server._img_cache) == server.IMG_CACHE_MAX
    assert ("c%045d" % 0) not in server._img_cache and ("c%045d" % (server.IMG_CACHE_MAX + 49)) in server._img_cache
    server._img_cache.clear()


# ---- /api/state and the pages ----------------------------------------------------------------------

def test_state_is_served_from_a_short_lived_cache(db, monkeypatch):
    builds = []

    def counting(*a, **k):
        builds.append(1)
        return {"now": len(builds), "stats": {}}
    monkeypatch.setattr(server, "snapshot", counting)
    app = make_app(FakeRpc(), db, FakeBrain(), FakePaper(), Hub())
    client = TestClient(app)
    first = client.get("/api/state")
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    assert client.get("/api/state").json() == first.json() and client.get("/api/state").json() == first.json()
    assert len(builds) == 1
    app.state.snapshots.ts -= server.SNAPSHOT_MAX_AGE_S + 1       # older than the cache allows: rebuilt once
    assert client.get("/api/state").json()["now"] == 2 and len(builds) == 2
    assert app.state.snapshots.get(force=True)["now"] == 3        # what the broadcaster does after a verdict


def test_state_has_no_secrets_or_local_paths(db, monkeypatch):
    """The real snapshot over a real database: nothing in it names the key, the environment or this machine."""
    from wormhole import config as C
    from wormhole.learn import Brain
    from wormhole.paper import Paper
    monkeypatch.setattr(server, "treasury", lambda rpc: {"usd": 0.0, "usd_real": 0.0, "stage": 0, "stage_name": "hatchling",
                                                         "stages": 7, "next_usd": 20, "demo": False, "wallet": C.WALLET})
    monkeypatch.setattr(server, "_compute_cached", lambda: {"provider": "venice", "balance_usd": None})
    hub = Hub()
    hub.scan({"step": "start", "token": ADDR, "ts": 1, "text": "opening", "data": {"name": "Tok"}})
    add_grad(db, ADDR, scored_at=int(time.time()) - 60)
    client = TestClient(make_app(FakeRpc(), db, Brain(db), Paper(db), hub))
    r = client.get("/api/state")
    assert r.status_code == 200
    body = r.text.lower()
    assert C.SECRET.startswith("0x") and len(C.SECRET) == 66
    leaked = C.SECRET[2:].lower() in body
    assert not leaked
    for needle in ("/users/", "/home/", "/app/", "wh_", "secret", "mnemonic", "private key"):
        assert needle not in body, needle
    data = r.json()
    assert data["feed"][0]["token"] == ADDR and data["dig"][0]["step"] == "start" and data["stats"]["scored"] == 1
    assert client.get(f"/api/token/{ADDR}").json()["score"]["verdict"] == "mixed"


def test_security_headers_on_pages_and_api(site):
    client = site[0]
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-frame-options"] == "DENY"
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "https://fonts.googleapis.com" in csp and "https://fonts.gstatic.com" in csp
    assert "script-src 'self' 'unsafe-inline'" in csp and "connect-src 'self' ws: wss:" in csp and "img-src 'self' data: blob: https:" in csp
    assert r.headers["cache-control"] == "public, max-age=30"
    d = client.get("/docs")
    assert d.status_code == 200 and d.headers["content-security-policy"] == csp
    s = client.get("/api/state")
    assert s.headers["cache-control"] == "no-store" and "content-security-policy" not in s.headers
    assert s.headers["x-content-type-options"] == "nosniff"
    h = client.get("/healthz")
    assert h.status_code == 200 and h.headers["x-frame-options"] == "DENY" and h.headers["referrer-policy"] == "no-referrer"
    assert client.get("/nothing-here").status_code == 404


def test_page_loads_only_origins_the_policy_allows():
    """Every resource the pages load outright (stylesheets, scripts, images, CSS url()) comes from an origin the
    policy allows: the fonts. Links a visitor may click are navigation, which the policy does not govern."""
    loads = re.compile(r'<(?:link|script|img|iframe|source)\b[^>]*?(?:href|src)="(https?://[^"]+)"|url\((?:"|\')?(https?://[^)"\']+)')
    for page in ("index.html", "docs.html", "design.css", "design.js"):
        html = (server.WEB / page).read_text(encoding="utf-8")
        for a, b in loads.findall(html):
            assert (a or b).startswith(("https://fonts.googleapis.com", "https://fonts.gstatic.com")), (page, a or b)


def test_page_assets_are_served_by_exact_name_only(site):
    """The stylesheet and the script the page links are served with their own types and the same nosniff header
    as everything else, revalidated on every load; nothing else under web/ is reachable by name."""
    client = site[0]
    css = client.get("/design.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert css.headers["x-content-type-options"] == "nosniff" and css.headers["cache-control"] == "no-cache"
    assert "content-security-policy" not in css.headers
    js = client.get("/design.js")
    assert js.status_code == 200 and js.headers["content-type"].startswith("application/javascript")
    assert js.headers["cache-control"] == "no-cache" and "etag" in js.headers
    assert client.get("/design.js", headers={"If-None-Match": js.headers["etag"]}).status_code == 304
    for path in ("/index.html", "/docs.html", "/web/design.css", "/design.txt", "/design.css.bak", "/DESIGN.CSS"):
        assert client.get(path).status_code == 404, path
    page = client.get("/").text
    assert 'href="/design.css"' in page and 'src="/design.js"' in page


def test_page_talks_to_its_own_origin_only():
    """The websocket is opened on the page's own host with the scheme that matches the page: no preview
    exception, no fixed port, no loopback address anywhere in the page or its script."""
    html = (server.WEB / "index.html").read_text(encoding="utf-8")
    js = (server.WEB / "design.js").read_text(encoding="utf-8")
    assert "(location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws'" in html
    for text in (html, js):
        assert "4671" not in text and "127.0.0.1" not in text and "localhost" not in text


def test_docs_placeholders_are_all_filled(site):
    client = site[0]
    page = client.get("/docs").text
    assert not re.search(r"\{\{[a-z_]+\}\}", page)
    assert "Worm is a small worm" in page


def test_healthz_reflects_the_indexer(site):
    from types import SimpleNamespace
    client, hub, _ = site
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["indexer_age_s"] is None
    hub.indexer = SimpleNamespace(last_ok=time.time() - 10)
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True and 9 <= r.json()["indexer_age_s"] <= 12
    hub.indexer = SimpleNamespace(last_ok=time.time() - 400)
    r = client.get("/healthz")
    assert r.status_code == 503 and r.json()["ok"] is False and r.json()["indexer_age_s"] >= 399
    assert r.headers["x-content-type-options"] == "nosniff"


def test_websocket_hello_is_the_cached_state(site):
    client, hub, _ = site
    hub.frame({"jpg": "AAAA", "note": "hi", "ts": 5})
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "state" and hello["data"]["stats"] == {"scored": 1}
        assert ws.receive_json() == {"type": "frame", "data": {"jpg": "AAAA", "note": "hi", "ts": 5}}
    assert client.app.state.snapshots.builds == 1


def test_pending_keeps_messages_and_drops_old_frames():
    hub = Hub()
    for i in range(30):
        hub.frame({"jpg": "x", "ts": i})
    assert hub.pending.qsize() == server.PENDING_MAX and hub.pending.dropped == 10
    hub.scan({"step": "start", "token": ADDR, "ts": 1})
    hub.notify("score", ADDR)
    assert hub.pending.qsize() == server.PENDING_MAX
    kinds = [hub.pending.get()["kind"] for _ in range(hub.pending.qsize())]
    assert kinds.count("scan") == 1 and kinds.count("score") == 1 and kinds.count("frame") == server.PENDING_MAX - 2
    assert hub.pending.empty()
    for i in range(server.PENDING_HARD_MAX + 5):
        hub.notify("score", ADDR)                   # messages are never dropped for frames, only past the hard cap
    assert hub.pending.qsize() == server.PENDING_HARD_MAX


def test_healthz_is_ok_while_the_first_backfill_runs(site):
    """Railway's deploy health check must not fail during the catch-up backfill."""
    import threading
    client, hub, _ = site

    class Idx:                      # stalled-looking timestamp, but the first backfill is still running
        last_ok = time.time() - 3600
        ready = threading.Event()

    hub.indexer = Idx()
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["catching_up"] is True
    Idx.ready.set()
    assert client.get("/healthz").status_code == 503


def test_the_last_dig_survives_a_restart(db):
    hub = Hub()
    for step in ("start", "creator", "verdict"):
        hub.scan({"token": ADDR, "step": step, "text": step, "ts": 1, "data": {}})
        hub.persist(db)
    fresh = Hub()
    fresh.restore(db)
    assert [e["step"] for e in fresh.dig] == ["start", "creator", "verdict"]
    db.meta_set("last_dig", "not json")
    other = Hub()
    other.restore(db)
    assert other.dig == []                                                 # unreadable: start empty, never crash
    hub.scan({"token": "0x" + "ef" * 20, "step": "start", "text": "start", "ts": 2, "data": {}})
    assert len(hub.dig) == 1                                               # a new dig replaces the last one
