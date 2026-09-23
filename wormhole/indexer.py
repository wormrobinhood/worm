"""Reads Pons launches and graduations from the chain into SQLite, then follows the head."""
import bisect
import logging
import threading
import time

from . import config as C
from .chain import call_data
from .pons import TOKEN_LAUNCHED, POOL_GRADUATED, token_metadata, pair_symbols

log = logging.getLogger("wormhole.indexer")

BLOCK_TIME = C.BLOCK_TIME   # seconds per block, the default; each Indexer measures its own from the chain at start
CONFIRM = 3                 # blocks of lag behind the head (0.3 s): a node behind another cannot clamp our window
RESCAN = 600                # blocks re-read every tick (a minute): what a lagging node hid last time is picked up
SAMPLE_EVERY = 10_000       # backfill: real timestamps every 10k blocks, launches interpolated between them
POISON_FAILS = 20           # identical follow failures (about a minute) before the range is skipped and reported
CUT = {"name": 80, "symbol": 24, "logo": 400, "description": 600, "twitter": 120, "telegram": 120, "website": 200}

INSERT_LAUNCH = ("INSERT INTO launches(token,curve,deployer,pair_token,pair_symbol,config_id,grad_threshold,block,ts,tx)"
                 " VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(token) DO UPDATE SET block=COALESCE(excluded.block,block),"
                 " ts=COALESCE(excluded.ts,ts), tx=COALESCE(excluded.tx,tx), config_id=COALESCE(excluded.config_id,config_id)")


class Indexer:
    def __init__(self, rpc, db, on_graduation=None, on_launch=None):
        self.rpc, self.db = rpc, db
        self.on_graduation, self.on_launch = on_graduation, on_launch
        self.anchor = None              # (block, timestamp) refreshed every loop
        self.block_time = BLOCK_TIME    # measured over the last million blocks on the first anchor
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.last_ok = time.time()      # when last_block last advanced; a health check reads it
        self.last_indexed_block = int(self.db.meta_get('last_block') or 0)
        self.fail_range, self.fails = None, 0

    # -- time -------------------------------------------------------------
    def _refresh_anchor(self):
        n = self.rpc.block_number()
        ts = self.rpc.block_ts(n) or int(time.time())
        if self.anchor is None:
            self._measure_block_time(n, ts)
        self.anchor = (n, ts)
        return n

    def _measure_block_time(self, n, ts):
        """Mean block time over the last million blocks (about a day): one extra block read."""
        back = min(n, 1_000_000)
        if back <= 0:
            return
        try:
            t0 = self.rpc.block_ts(n - back)
        except Exception as e:
            log.info("block time not measured: %s", e)
            return
        if t0 and 0.01 <= (ts - t0) / back <= 60:
            self.block_time = (ts - t0) / back
            log.info("block time %.5f s over the last %d blocks", self.block_time, back)

    def est_ts(self, block):
        """Block timestamp estimated from the measured block time: the fallback when no exact one is at hand."""
        if not self.anchor:
            self._refresh_anchor()
        n, ts = self.anchor
        return int(ts - (n - block) * self.block_time)

    def _timestamps(self, a, b, blocks, live):
        """Timestamps for blocks in a..b. Exact for a live slice or a handful of blocks; in the backfill,
        real timestamps every SAMPLE_EVERY blocks and a straight line between them (the block time drifts,
        so extrapolating from the head would put a launch minutes off). A block without a value gets None."""
        blocks = sorted(set(blocks))
        if not blocks:
            return {}
        if live or len(blocks) <= 25:
            return self.rpc.block_timestamps(blocks)
        pts = sorted(set(range(a, b + 1, SAMPLE_EVERY)) | {b})
        real = self.rpc.block_timestamps(pts)
        out = {}
        for n in blocks:
            i = max(0, bisect.bisect_right(pts, n) - 1)
            lo, hi = pts[i], pts[min(i + 1, len(pts) - 1)]
            t0, t1 = real.get(lo), real.get(hi)
            if hi == lo:
                out[n] = t0
            elif t0 is None or t1 is None:
                out[n] = None
            else:
                out[n] = int(t0 + (t1 - t0) * (n - lo) / (hi - lo))
        return out

    # -- loops ------------------------------------------------------------
    def backfill(self):
        latest = self._refresh_anchor() - CONFIRM
        last = self.db.meta_get("last_block")
        if self.db.meta_get("scan_jobs_start_block") is None:
            self.db.meta_set("scan_jobs_start_block", last if last is not None else latest)
        start = int(last) + 1 if last is not None else latest - int(C.BACKFILL_HOURS * 3600 / self.block_time)
        if start <= latest:
            log.info("backfill blocks %s..%s (%d blocks, ~%.1f h)", start, latest, latest - start + 1,
                     (latest - start + 1) * self.block_time / 3600)
            a = start
            while a <= latest:                       # resumable slices: last_block advances per slice
                b = min(a + C.LOG_CHUNK - 1, latest)
                self._ingest(a, b, live=False)
                a = b + 1
        self.ready.set()

    def follow(self):
        while not self.stop.is_set():
            rng = None
            try:
                head = self._refresh_anchor() - CONFIRM
                last = int(self.db.meta_get("last_block") or head)
                if head > last:
                    rng = (max(0, last + 1 - RESCAN), head)
                    self._ingest(rng[0], rng[1], live=True)
                self.fails, self.fail_range = 0, None
            except Exception as e:
                self._follow_failed(rng, e)
                self.stop.wait(3)
            self.stop.wait(C.FOLLOW_EVERY_S)

    def _follow_failed(self, rng, e):
        """Count failures on the same range; after POISON_FAILS skip it and say so, rather than loop forever."""
        key = rng[0] if rng else None            # the start stays put while stuck; the head keeps moving
        self.fails = self.fails + 1 if key == self.fail_range else 1
        self.fail_range = key
        log.warning("follow error (%d) on %s: %s", self.fails, rng, e)
        if rng and self.fails >= POISON_FAILS:
            self.db.add_event("error", f"skipping blocks {rng[0]}..{rng[1]} after repeated RPC failures; repair required")
            self.db.meta_set("last_block", rng[1])
            self.fails, self.fail_range = 0, None

    # -- ingest -----------------------------------------------------------
    def _ingest(self, a, b, live):
        """Blocks a..b into the table. Safe to run twice over the same range: rows are upserted, a
        graduation counts once, and events and callbacks fire only for what is new."""
        t0 = time.time()
        launches = [TOKEN_LAUNCHED.decode(l) for l in
                    self.rpc.get_logs(C.FACTORY, [TOKEN_LAUNCHED.topic], a, b, C.LOG_CHUNK)]
        grads = [POOL_GRADUATED.decode(l) for l in
                 self.rpc.get_logs(C.FACTORY, [POOL_GRADUATED.topic], a, b, C.LOG_CHUNK)]
        ts = self._timestamps(a, b, [l["_block"] for l in launches] + [g["_block"] for g in grads], live)
        when = lambda n: ts.get(n) or self.est_ts(n)
        new_launches = []
        if launches:
            known = self._known([l["token"] for l in launches])
            new_launches = [l for l in launches if l["token"] not in known]
            syms = pair_symbols(self.rpc, [l["pairToken"] for l in launches])
            self.db.many(INSERT_LAUNCH, [(l["token"], l["curve"], l["deployer"], l["pairToken"], syms[l["pairToken"]],
                                          str(l["launchConfigId"]), str(l["graduationThreshold"]), l["_block"],
                                          when(l["_block"]), l["_tx"]) for l in launches])
        new_grads = []
        for g in grads:
            if not self._ensure_launch(g["token"], g["_block"]):
                log.warning("graduation of %s at block %s: no launch record on the factory", g["token"][:10], g["_block"])
                continue
            with self.db.transaction():
                if self.db.xc("UPDATE launches SET graduated=1, grad_block=?, grad_ts=?, grad_tx=?"
                              " WHERE token=? AND graduated=0",
                              (g["_block"], when(g["_block"]), g["_tx"], g["token"])) == 1:
                    new_grads.append(g)
                    boundary = self.db.meta_get("scan_jobs_start_block")
                    if live or (boundary is not None and g["_block"] > int(boundary)):
                        self.db.x("INSERT OR IGNORE INTO scan_jobs(token,available_at) VALUES(?,?)",
                                  (g['token'], int(time.time())))
        if new_grads:
            self._metadata([g["token"] for g in new_grads])
        if live and new_launches:
            # names for the live ticker only; the backfill skips this to spare the public node
            recent = new_launches[-40:]
            self._metadata([l["token"] for l in recent], curve=False)
            if self.on_launch:
                for l in recent:
                    self.on_launch(l["token"])
        self.db.meta_set("last_block", b)
        self.last_indexed_block = b
        self.last_ok = time.time()
        if new_launches or new_grads:
            log.info("blocks %s..%s: %d launches, %d graduations (%.1fs)", a, b, len(new_launches), len(new_grads),
                     time.time() - t0)
        if live:
            for g in new_grads:
                self.db.add_event("graduation", f"graduated: {self.label(g['token'])}", g["token"])
                if self.on_graduation:
                    self.on_graduation(g["token"])

    def _known(self, tokens):
        out = set()
        for i in range(0, len(tokens), 500):
            part = tokens[i:i + 500]
            out |= {r["token"] for r in
                    self.db.q(f"SELECT token FROM launches WHERE token IN ({','.join('?' * len(part))})", part)}
        return out

    def label(self, token):
        r = self.db.one("SELECT name, symbol FROM launches WHERE token=?", (token,))
        if r and r.get("symbol"):
            return f"{r['name']} (${r['symbol']})"
        return token[:10]

    def _ensure_launch(self, token, grad_block=None):
        """A graduation of a token launched before our window: read the factory's own record.
        True when a row exists afterwards."""
        if self.db.one("SELECT 1 FROM launches WHERE token=?", (token,)):
            return True
        raw = self.rpc.eth_call(C.FACTORY, call_data("getLaunchedToken(address)", ("address",), (token,)))
        if not raw or len(raw) < 2 + 64 * 6:
            return False
        # LaunchedToken (ILaunchpadV2.sol), one word each: token, curve, deployer, creatorFeeRecipient,
        # pairToken, graduationThreshold, poolFee, tickSpacing, creatorTaxBps, buybackEnabled, phase, ...
        w = [raw[2 + i * 64: 2 + (i + 1) * 64] for i in range((len(raw) - 2) // 64)]
        curve, deployer, pair = "0x" + w[1][24:], "0x" + w[2][24:], "0x" + w[4][24:]
        if curve == C.ZERO:
            return False
        thr = int(w[5], 16)
        tax = int(w[8], 16) if len(w) > 8 else None
        buyback = int(w[9], 16) & 1 if len(w) > 9 else None
        sym = pair_symbols(self.rpc, [pair])[pair]
        blk, tx = self._find_launch(token, grad_block)
        ts = None
        if blk:
            try:
                ts = self.rpc.block_ts(blk)
            except Exception as e:
                log.info("launch timestamp lookup failed for %s: %s", token[:10], e)
            ts = ts or self.est_ts(blk)
        self.db.x("INSERT OR IGNORE INTO launches(token,curve,deployer,pair_token,pair_symbol,config_id,grad_threshold,"
                  "block,ts,tx,creator_tax_bps,buyback) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (token, curve, deployer, pair, sym, None, str(thr), blk, ts, tx, tax, buyback))
        return True

    def _find_launch(self, token, grad_block=None):
        """Block and tx of a token's TokenLaunched log, searching backwards from its graduation:
        most tokens launch within hours of graduating."""
        try:
            if grad_block is None:
                if not self.anchor:
                    self._refresh_anchor()
                grad_block = self.anchor[0]
            hi = grad_block
            for _ in range(8):
                lo = max(0, hi - 300_000)
                for lg in self.rpc.get_logs(C.FACTORY, [TOKEN_LAUNCHED.topic, "0x" + token[2:].rjust(64, "0")],
                                            lo, hi, 300_000):
                    return int(lg["blockNumber"], 16), lg["transactionHash"]
                if lo == 0:
                    break
                hi = lo - 1
        except Exception as e:
            log.info("launch block lookup failed for %s: %s", token[:10], e)
        return None, None

    def _metadata(self, tokens, curve=True):
        """Names, socials and curve settings for tokens we hold. A read that failed keeps the old value and
        leaves meta at 0, so a later pass asks again; meta becomes 1 once the core reads succeeded."""
        tokens = [t for t in tokens if t]
        if not tokens:
            return
        rows = self.db.q(f"SELECT token, curve FROM launches WHERE token IN ({','.join('?' * len(tokens))})", tokens)
        curves = {r["token"]: r["curve"] for r in rows}
        md = token_metadata(self.rpc, [t for t in tokens if t in curves], with_curve=curves if curve else None)
        cut = lambda s, n: None if s is None else s[:n]
        for t, m in md.items():
            ok = m["name"] is not None and m["symbol"] is not None and (not curve or m.get("creator_tax_bps") is not None)
            bb = m.get("buyback")
            self.db.x("UPDATE launches SET name=COALESCE(?,name), symbol=COALESCE(?,symbol), logo=COALESCE(?,logo),"
                      " description=COALESCE(?,description), twitter=COALESCE(?,twitter), telegram=COALESCE(?,telegram),"
                      " website=COALESCE(?,website), creator_tax_bps=COALESCE(?,creator_tax_bps),"
                      " curve_fee_bps=COALESCE(?,curve_fee_bps), buyback=COALESCE(?,buyback),"
                      " meta=MAX(COALESCE(meta,0),?) WHERE token=?",
                      (cut(m["name"], CUT["name"]), cut(m["symbol"], CUT["symbol"]), cut(m["logo"], CUT["logo"]),
                       cut(m["description"], CUT["description"]), cut(m["twitter"], CUT["twitter"]),
                       cut(m["telegram"], CUT["telegram"]), cut(m["website"], CUT["website"]),
                       m.get("creator_tax_bps"), m.get("curve_fee_bps"), None if bb is None else int(bb), int(ok), t))

    def refetch_metadata(self, limit=20):
        """Ask again for graduated tokens whose metadata reads failed (meta still 0). Not scheduled yet."""
        rows = self.db.q("SELECT token FROM launches WHERE graduated=1 AND meta=0 ORDER BY grad_block DESC LIMIT ?",
                         (limit,))
        self._metadata([r["token"] for r in rows])
        return len(rows)
