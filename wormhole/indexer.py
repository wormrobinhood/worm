"""Reads Pons launches and graduations from the chain into SQLite, then follows the head."""
import logging
import threading
import time

from . import config as C
from .chain import call_data
from .pons import TOKEN_LAUNCHED, POOL_GRADUATED, token_metadata, pair_symbols

log = logging.getLogger("wormhole.indexer")


class Indexer:
    def __init__(self, rpc, db, on_graduation=None, on_launch=None):
        self.rpc, self.db = rpc, db
        self.on_graduation, self.on_launch = on_graduation, on_launch
        self.anchor = None            # (block, timestamp) refreshed every loop
        self.stop = threading.Event()
        self.ready = threading.Event()

    # -- time -------------------------------------------------------------
    def _refresh_anchor(self):
        n = self.rpc.block_number()
        ts = self.rpc.block_ts(n) or int(time.time())
        self.anchor = (n, ts)
        return n

    def est_ts(self, block):
        """Block timestamp estimated from the measured block time; exact enough for a feed."""
        n, ts = self.anchor
        return int(ts - (n - block) * C.BLOCK_TIME)

    # -- loops ------------------------------------------------------------
    def backfill(self):
        latest = self._refresh_anchor()
        last = self.db.meta_get("last_block")
        start = int(last) + 1 if last is not None else latest - int(C.BACKFILL_HOURS * C.BLOCKS_PER_HOUR)
        if start <= latest:
            log.info("backfill blocks %s..%s (%d blocks, ~%.1f h)", start, latest, latest - start + 1,
                     (latest - start + 1) / C.BLOCKS_PER_HOUR)
            a = start
            while a <= latest:                       # resumable slices: last_block advances per slice
                b = min(a + C.LOG_CHUNK - 1, latest)
                self._ingest(a, b, live=False)
                a = b + 1
        self.ready.set()

    def follow(self):
        while not self.stop.is_set():
            try:
                latest = self._refresh_anchor()
                last = int(self.db.meta_get("last_block") or latest)
                if latest > last:
                    self._ingest(last + 1, latest, live=True)
            except Exception as e:
                log.warning("follow error: %s", e)
                time.sleep(3)
            self.stop.wait(C.FOLLOW_EVERY_S)

    # -- ingest -----------------------------------------------------------
    def _ingest(self, a, b, live):
        t0 = time.time()
        launches = [TOKEN_LAUNCHED.decode(l) for l in
                    self.rpc.get_logs(C.FACTORY, [TOKEN_LAUNCHED.topic], a, b, C.LOG_CHUNK)]
        grads = [POOL_GRADUATED.decode(l) for l in
                 self.rpc.get_logs(C.FACTORY, [POOL_GRADUATED.topic], a, b, C.LOG_CHUNK)]
        if launches:
            syms = pair_symbols(self.rpc, [l["pairToken"] for l in launches])
            rows = [(l["token"], l["curve"], l["deployer"], l["pairToken"], syms[l["pairToken"]],
                     l["launchConfigId"], str(l["graduationThreshold"]), l["_block"], self.est_ts(l["_block"]), l["_tx"])
                    for l in launches]
            self.db.many("INSERT OR IGNORE INTO launches(token,curve,deployer,pair_token,pair_symbol,config_id,"
                         "grad_threshold,block,ts,tx) VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
        if grads:
            # exact timestamps only when following live; the backfill estimates to spare the node
            ts = self.rpc.block_timestamps([g["_block"] for g in grads]) if live else {}
            for g in grads:
                self._ensure_launch(g["token"], g["_block"])
                self.db.x("UPDATE launches SET graduated=1, grad_block=?, grad_ts=?, grad_tx=? WHERE token=?",
                          (g["_block"], ts.get(g["_block"]) or self.est_ts(g["_block"]), g["_tx"], g["token"]))
            self._metadata([g["token"] for g in grads])
        if live and launches:
            # names for the live ticker only; the backfill skips this to spare the public node
            recent = launches[-40:]
            self._metadata([l["token"] for l in recent], curve=False)
            if self.on_launch:
                for l in recent:
                    self.on_launch(l["token"])
        self.db.meta_set("last_block", b)
        if launches or grads:
            log.info("blocks %s..%s: %d launches, %d graduations (%.1fs)", a, b, len(launches), len(grads),
                     time.time() - t0)
        if live:
            for g in grads:
                self.db.add_event("graduation", f"graduated: {self.label(g['token'])}", g["token"])
                if self.on_graduation:
                    self.on_graduation(g["token"])

    def label(self, token):
        r = self.db.one("SELECT name, symbol FROM launches WHERE token=?", (token,))
        if r and r.get("symbol"):
            return f"{r['name']} (${r['symbol']})"
        return token[:10]

    def _ensure_launch(self, token, grad_block=None):
        """A graduation of a token launched before our window: read the factory's own record."""
        if self.db.one("SELECT 1 FROM launches WHERE token=?", (token,)):
            return
        raw = self.rpc.eth_call(C.FACTORY, call_data("getLaunchedToken(address)", ("address",), (token,)))
        if not raw or len(raw) < 2 + 64 * 6:
            return
        w = [raw[2 + i * 64: 2 + (i + 1) * 64] for i in range(6)]
        curve, deployer, pair = "0x" + w[1][24:], "0x" + w[2][24:], "0x" + w[4][24:]
        thr = int(w[5], 16)
        sym = pair_symbols(self.rpc, [pair])[pair]
        blk = None
        try:
            # search backwards from the graduation: most tokens launch within hours of graduating
            hi = grad_block or self.anchor[0]
            for _ in range(8):
                lo = max(0, hi - 300_000)
                for lg in self.rpc.get_logs(C.FACTORY, [TOKEN_LAUNCHED.topic, "0x" + token[2:].rjust(64, "0")],
                                            lo, hi, 300_000):
                    blk = int(lg["blockNumber"], 16)
                    break
                if blk or lo == 0:
                    break
                hi = lo - 1
        except Exception as e:
            log.info("launch block lookup failed for %s: %s", token[:10], e)
        self.db.x("INSERT OR IGNORE INTO launches(token,curve,deployer,pair_token,pair_symbol,config_id,"
                  "grad_threshold,block,ts,tx) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (token, curve, deployer, pair, sym, 0, str(thr), blk, self.est_ts(blk) if blk else None, None))

    def _metadata(self, tokens, curve=True):
        tokens = [t for t in tokens if t]
        if not tokens:
            return
        rows = self.db.q(f"SELECT token, curve FROM launches WHERE token IN ({','.join('?' * len(tokens))})", tokens)
        curves = {r["token"]: r["curve"] for r in rows}
        md = token_metadata(self.rpc, [t for t in tokens if t in curves], with_curve=curves if curve else None)
        for t, m in md.items():
            self.db.x("UPDATE launches SET name=?, symbol=?, logo=?, description=?, twitter=?, telegram=?, website=?,"
                      " creator_tax_bps=COALESCE(?, creator_tax_bps), curve_fee_bps=COALESCE(?, curve_fee_bps), meta=1"
                      " WHERE token=?",
                      (m["name"][:80], m["symbol"][:24], m["logo"][:400], m["description"][:600],
                       m["twitter"][:120], m["telegram"][:120], m["website"][:200],
                       m.get("creator_tax_bps"), m.get("curve_fee_bps"), t))
