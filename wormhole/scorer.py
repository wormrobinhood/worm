"""Rule-based rug screening for a graduated Pons token. Every rule is named, every point is explained,
and the brain (learn.py) can re-weight each rule from what happened afterwards."""
import collections
import json
import logging
import math
import time

from . import config as C
from .pons import CURVE_BUY, CURVE_SELL, TRANSFER, POOL_REGISTERED, SWAP

log = logging.getLogger("wormhole.scorer")

# Observed on chain 4663's PoolManager (Uniswap v4 Swap). Positive amount = the swapper received it.
SWAP_TOPIC = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"

RULES = {
    "creator_history": "other tokens this creator launched in our window",
    "creator_grads": "other tokens this creator graduated",
    "creator_rugs": "earlier tokens by this creator that rugged or dumped after our verdict",
    "creator_tax": "creator tax on every trade",
    "snipe": "share of curve supply bought in the 3 second snipe window",
    "buyers": "unique buyers on the bonding curve",
    "deployer_buy": "creator buying its own curve",
    "top_buyer": "one wallet's share of all curve buys",
    "fresh_buyers": "share of curve buy volume from throwaway wallets with no other history",
    "funding_cluster": "curve buyers funded by the same wallet just before they bought (one buyer wearing many wallets)",
    "bot_fleet": "share of curve buy volume from wallets that buy on many curves (fleets that sell at graduation)",
    "top10": "top-10 holders' share of circulating supply",
    "deployer_hold": "creator's current share of circulating supply",
    "activity": "trades since graduation",
    "socials": "links attached to the token",
    "pace": "time from launch to graduation",
}
HARD = {("deployer_hold", -30), ("snipe", -15), ("funding_cluster", -20)}   # one of these (or creator_rugs) demotes healthy to mixed
PARTIAL_CAP = 69                                   # a score on incomplete data can never read "looks healthy"
PARTIAL_TEXT = "incomplete data: some chain reads failed, will re-check"
SWAP_CAP = 10_000                                  # swaps read per window (the node's own cap per query)
DUST_DIVISOR = 2000                                # a curve buyer must receive at least 1/2000 of the curve output
PROVENANCE_MAX = 120                               # biggest buyers whose history and funding are read
FRESH_NONCE = 3                                    # a wallet with at most this many transactions ever is a throwaway
FUND_LOOKBACK = 20_000                             # blocks (~35 min) before launch in which funding transfers are looked for
FLEET_MIN_CURVES = 5                               # a wallet seen buying on this many other curves in a day is a fleet wallet
FLEET_WINDOW_S = 24 * 3600
FLEET_MIN_KNOWN = 20                               # other curves the worm must have on record before the fleet read counts
BUYERS_KEPT = 300                                  # biggest buyers remembered per token, for the fleet read of later tokens


def _topic_addr(a):
    return "0x" + a[2:].lower().rjust(64, "0")


def _pct(a, b):
    return (100.0 * a / b) if b else 0.0


def final_score(total):
    """0-100 from the weighted sum: float noise cleared first, then half-up, so the verdict does not depend
    on the order the rules were added in or on banker's rounding at .5."""
    return int(max(0, min(100, math.floor(round(total, 6) + 0.5))))


def verdict_for(score):
    return "looks healthy" if score >= 70 else ("mixed" if score >= 45 else "avoid")


class Scorer:
    def __init__(self, rpc, db, weights=None, progress=None):
        self.rpc, self.db = rpc, db
        self.weights = weights or (lambda: {})
        self.progress = progress or (lambda ev: None)

    def _emit(self, token, step, text, **data):
        try:
            self.progress({"token": token, "step": step, "text": text, "ts": int(time.time()), "data": data})
        except Exception as e:
            log.info("progress callback failed: %s", e)

    def _bot_fleet(self, token, L, humans):
        """Wallets that buy on many curves are fleets: they buy every launch and sell into the pool at
        graduation, which is what a 15-minute rug after a clean-looking curve usually is. The biggest buyers of
        every token scored are remembered, so a buyer seen on FLEET_MIN_CURVES other curves within a day is a
        fleet wallet. Returns (fleet set, share of buy volume from the fleet, other curves known in the window)."""
        now = int(time.time())
        since = now - FLEET_WINDOW_S
        rows = sorted(humans.items(), key=lambda kv: -kv[1])[:BUYERS_KEPT]
        if rows:
            self.db.many("INSERT OR REPLACE INTO curve_buyers(token,wallet,tokens_out,ts) VALUES(?,?,?,?)",
                         [(token, a, float(v), int(L.get("grad_ts") or now)) for a, v in rows])
        known = self.db.one("SELECT COUNT(DISTINCT token) n FROM curve_buyers WHERE token!=? AND ts>=?", (token, since))["n"]
        fleet = set()
        addrs = [a for a, _ in rows]
        for i in range(0, len(addrs), 400):
            chunk = addrs[i:i + 400]
            qs = ",".join("?" * len(chunk))
            for r in self.db.q(f"SELECT wallet, COUNT(DISTINCT token) n FROM curve_buyers WHERE wallet IN ({qs})"
                               f" AND token!=? AND ts>=? GROUP BY wallet HAVING n>=?", (*chunk, token, since, FLEET_MIN_CURVES)):
                fleet.add(r["wallet"])
        vol = sum(humans.values())
        return fleet, _pct(sum(humans[a] for a in fleet), vol), known

    def _buyer_provenance(self, token, L, humans, lb, gb, m):
        """Two reads behind the buyer count. (1) Transaction counts: a wallet whose whole history is this buy
        and maybe one sell is a throwaway; fresh_pct is the share of buy volume from such wallets. (2) When the
        pair is an ERC-20 (USDG or a stock token), that token's Transfer logs into the buyers during the half
        hour before launch show who funded them; many buyers fed by one wallet are one buyer. ETH funding is a
        plain value transfer with no log, so for ETH pairs only the first read is possible.
        Returns (fresh_pct or None, funders Counter or None); a failed read marks the score partial."""
        top = sorted(humans.items(), key=lambda kv: -kv[1])[:PROVENANCE_MAX]     # the biggest buyers carry the volume
        fresh_pct = None
        try:
            counts = self.rpc.batch([("eth_getTransactionCount", [a, "latest"]) for a, _ in top])
        except Exception as e:
            log.info("nonce reads failed for %s: %s", token[:10], e)
            counts = None
        known = [(v, int(c, 16)) for (a, v), c in zip(top, counts or []) if c is not None]
        if known:
            fresh_pct = _pct(sum(v for v, n in known if n <= FRESH_NONCE), sum(v for v, n in known))
            m["fresh_buyers"] = sum(1 for v, n in known if n <= FRESH_NONCE)
        else:
            m["partial"] = True
        pair = (L.get("pair_token") or "").lower()
        if not pair or pair == C.ZERO or not top:
            return fresh_pct, None
        funders = collections.Counter()
        exclude = set(C.INFRA) | {L["curve"], token, pair}
        addrs = [a for a, _ in top]
        try:
            for i in range(0, len(addrs), 40):
                chunk = addrs[i:i + 40]
                seen = set()
                for lg in self.rpc.get_logs(pair, [TRANSFER.topic, None, [_topic_addr(a) for a in chunk]],
                                            max(0, lb - FUND_LOOKBACK), gb, 200_000, cap=5000):
                    t = TRANSFER.decode(lg)
                    if t["from"] in exclude or (t["from"], t["to"]) in seen:
                        continue
                    seen.add((t["from"], t["to"]))
                    funders[t["from"]] += 1
        except Exception as e:
            log.info("funding logs failed for %s: %s", token[:10], e)
            m["partial"] = True
            return fresh_pct, None
        return fresh_pct, funders

    def score(self, token):
        from .learn import creator_trust          # learn imports RULES from here, so this import stays local
        L = self.db.one("SELECT * FROM launches WHERE token=?", (token,))
        if not L or not L.get("graduated"):
            return None
        t0 = time.time()
        latest = self.rpc.block_number()
        fired = []          # {rule, points, text}
        m = {"partial": False}
        self._emit(token, "start", f"digging into {L.get('name') or token[:10]} (${L.get('symbol') or '?'})",
                   name=L.get("name"), symbol=L.get("symbol"), pair=L.get("pair_symbol"), grad_ts=L.get("grad_ts"),
                   deployer=L.get("deployer"), logo=L.get("logo"))

        def rule(rid, points, text):
            fired.append({"rule": rid, "points": points, "text": text})

        # 1. the creator ------------------------------------------------------
        lb, gb = L.get("block"), L.get("grad_block")
        since = int(lb or gb or 0) - int(C.BACKFILL_HOURS * 3600 / C.BLOCK_TIME)   # the window, in blocks
        h = self.db.one("SELECT COUNT(*) n, COALESCE(SUM(graduated),0) g FROM launches WHERE deployer=? AND token!=?"
                        " AND COALESCE(block, grad_block)>=?", (L["deployer"], token, since))
        n_prev, g_prev = int(h["n"]), int(h["g"])
        trust, cmeta = creator_trust(self.db, L["deployer"], exclude_token=token)
        n_rug = int(cmeta.get("rugged") or 0)
        m.update(creator=L["deployer"], creator_prev_launches=n_prev, creator_prev_grads=g_prev, creator_rugged=n_rug,
                 creator_trust=trust)
        if n_prev == 0:
            rule("creator_history", 3, "first launch we have seen from this wallet (fresh wallets are common for bots)")
        elif n_prev <= 3:
            rule("creator_history", 0, f"creator launched {n_prev} other token(s) in the last {int(C.BACKFILL_HOURS)}h")
        elif n_prev <= 10:
            rule("creator_history", -10, f"creator launched {n_prev} other tokens in the last {int(C.BACKFILL_HOURS)}h")
        else:
            rule("creator_history", -25, f"serial launcher: {n_prev} other tokens in the last {int(C.BACKFILL_HOURS)}h")
        if n_rug:
            rule("creator_rugs", -25 * min(2, n_rug), f"{n_rug} earlier token(s) by this creator rugged or dumped after our verdict")
        if g_prev and not n_rug:
            rule("creator_grads", min(10, 5 * g_prev), f"creator graduated {g_prev} other token(s), none rugged on our watch")

        tax = L.get("creator_tax_bps")                 # NULL when the indexer's chain read failed: unknown, not zero
        tax = int(tax) if tax is not None else None
        m["creator_tax_bps"] = tax
        if tax is None:
            rule("creator_tax", 0, "creator tax unknown (read failed)")
        elif tax == 0:
            rule("creator_tax", 5, "no creator tax")
        elif tax <= 200:
            rule("creator_tax", 0, f"creator tax {tax / 100:.2f}%")
        elif tax <= 500:
            rule("creator_tax", -10, f"creator tax {tax / 100:.2f}%")
        else:
            rule("creator_tax", -25, f"heavy creator tax {tax / 100:.2f}%")

        self._emit(token, "creator", f"creator {L['deployer'][:8]}: {n_prev} other launches, {g_prev} graduated,"
                   f" {n_rug} rugged, tax {'unknown' if tax is None else f'{tax / 100:.2f}%'}",
                   prev_launches=n_prev, prev_grads=g_prev, prev_rugs=n_rug, tax_bps=tax, trust=trust)

        # 2. the bonding curve ------------------------------------------------
        lb_known = bool(lb)
        if not lb_known:
            lb = max(0, gb - 24 * C.BLOCKS_PER_HOUR)     # how far back to read the curve
        buys, sells = [], []
        try:
            for lg in self.rpc.get_logs(L["curve"], [[CURVE_BUY.topic, CURVE_SELL.topic]], lb, gb, 200_000, cap=20_000):
                (buys if lg["topics"][0] == CURVE_BUY.topic else sells).append(
                    (CURVE_BUY if lg["topics"][0] == CURVE_BUY.topic else CURVE_SELL).decode(lg))
        except Exception as e:
            log.info("curve logs failed for %s: %s", token[:10], e)
            m["partial"] = True
        if not lb_known:
            if buys:
                lb = min(b["_block"] for b in buys)      # the first curve buy stands in for the launch block
                m["launch_block_derived"] = True
            else:
                m["partial"] = True                      # no launch block and nothing to derive it from
        total_out = sum(b["tokensOut"] for b in buys)
        by_rec = collections.Counter()
        for b in buys:
            by_rec[b["recipient"]] += b["tokensOut"]
        dust = total_out // DUST_DIVISOR
        humans = {a: v for a, v in by_rec.items() if a not in C.INFRA and v > 0 and v >= dust}
        uniq = len(humans)
        fleet, fleet_pct, fleet_known = self._bot_fleet(token, L, humans)
        organic = uniq - len(fleet)
        window = int(C.SNIPE_WINDOW_S / C.BLOCK_TIME)
        # the creator's own launch-block buy is charged by deployer_buy, not counted as a snipe
        snipe_out = sum(b["tokensOut"] for b in buys if b["_block"] <= lb + window and b["recipient"] != L["deployer"])
        snipe_pct = _pct(snipe_out, total_out)
        top_addr, top_val = (max(humans.items(), key=lambda kv: kv[1]) if humans else (None, 0))
        top_pct = _pct(top_val, total_out)
        dep_pct = _pct(by_rec.get(L["deployer"], 0), total_out)
        m.update(curve_buys=len(buys), curve_sells=len(sells), unique_buyers=uniq, organic_buyers=organic,
                 fleet_buyers=len(fleet), fleet_pct=round(fleet_pct, 1), fleet_known_curves=fleet_known,
                 dust_buyers=sum(1 for a, v in by_rec.items() if a not in C.INFRA and not (v > 0 and v >= dust)),
                 snipe_pct=round(snipe_pct, 1), top_buyer_pct=round(top_pct, 1), top_buyer=top_addr,
                 deployer_buy_pct=round(dep_pct, 1), launch_block_known=lb_known)
        # timeline of curve buys for the live view: 30 buckets from launch to graduation, share of tokens bought
        span = max(1, gb - lb)
        buckets = [0.0] * 30
        for b in buys:
            k = min(29, max(0, int(29 * (b["_block"] - lb) / span)))
            buckets[k] += b["tokensOut"]
        timeline = [round(_pct(v, total_out), 2) for v in buckets]
        self._emit(token, "curve", f"curve: {len(buys)} buys, {uniq} unique buyers, {snipe_pct:.0f}% sniped in {C.SNIPE_WINDOW_S}s",
                   buys=len(buys), sells=len(sells), unique_buyers=uniq, snipe_pct=round(snipe_pct, 1),
                   top_buyer_pct=round(top_pct, 1), deployer_buy_pct=round(dep_pct, 1), timeline=timeline,
                   span_s=round(span * C.BLOCK_TIME), snipe_bucket_end=min(29, int(29 * window / span)),
                   launch_block_known=lb_known)
        if buys:
            if snipe_pct >= 30:
                rule("snipe", -15, f"{snipe_pct:.0f}% of the curve was bought in the first {C.SNIPE_WINDOW_S}s: sniped or bundled")
            elif snipe_pct >= 10:
                rule("snipe", -8, f"{snipe_pct:.0f}% of the curve was bought in the snipe window")
            else:
                rule("snipe", 5, f"quiet launch window, {snipe_pct:.0f}% sniped")
            besides = f" besides {len(fleet)} fleet wallets" if fleet else ""
            if organic < 25:
                rule("buyers", -10, f"only {organic} unique buyers on the curve{besides}")
            elif organic >= 150:
                rule("buyers", 10, f"{organic} unique buyers on the curve{besides}")
            else:
                rule("buyers", 0, f"{organic} unique buyers on the curve{besides}")
            if dep_pct >= 20:
                rule("deployer_buy", -20, f"creator bought {dep_pct:.0f}% of the curve supply itself")
            elif dep_pct >= 5:
                rule("deployer_buy", -8, f"creator bought {dep_pct:.0f}% of the curve supply itself")
            if top_pct >= 25 and top_addr != L["deployer"]:
                rule("top_buyer", -8, f"one wallet bought {top_pct:.0f}% of the curve supply")

        # 2b. who is behind the buyers: throwaway wallets and shared funding ------------------------
        # 271 wallets funded by one hand look like 271 people to every rule above. Two reads see through it.
        if buys and uniq >= 10:
            if fleet_known < FLEET_MIN_KNOWN:
                rule("bot_fleet", 0, f"fleet check needs {FLEET_MIN_KNOWN} recent curves on record, has {fleet_known}")
            elif fleet_pct >= 50:
                rule("bot_fleet", -15, f"{fleet_pct:.0f}% of the buy volume came from {len(fleet)} wallets that buy on many curves and sell at graduation")
            elif fleet_pct >= 25:
                rule("bot_fleet", -8, f"{fleet_pct:.0f}% of the buy volume came from wallets that buy on many curves")
            else:
                rule("bot_fleet", 0, f"{fleet_pct:.0f}% of the buy volume came from fleet wallets")
            fresh_pct, funders = self._buyer_provenance(token, L, humans, lb, gb, m)
            if fresh_pct is not None:
                m["fresh_buyers_pct"] = round(fresh_pct, 1)
                if fresh_pct >= 60:
                    rule("fresh_buyers", -12, f"{fresh_pct:.0f}% of the buy volume came from throwaway wallets with no other history")
                elif fresh_pct >= 40:
                    rule("fresh_buyers", -6, f"{fresh_pct:.0f}% of the buy volume came from wallets with almost no history")
                else:
                    rule("fresh_buyers", 0, f"{fresh_pct:.0f}% of the buy volume came from new wallets")
            if funders is not None:
                top_f = funders.most_common(3)
                share1 = _pct(top_f[0][1], uniq) if top_f else 0.0
                share3 = _pct(sum(n for _, n in top_f), uniq)
                m.update(funding_visible=True, top_funder=(top_f[0][0] if top_f else None),
                         top_funder_pct=round(share1, 1), top3_funders_pct=round(share3, 1))
                if share1 >= 25:
                    rule("funding_cluster", -20, f"one wallet funded {top_f[0][1]} of the {uniq} buyers just before they bought: one buyer wearing many wallets")
                elif share3 >= 50:
                    rule("funding_cluster", -12, f"three wallets funded {share3:.0f}% of the buyers just before they bought")
                else:
                    rule("funding_cluster", 0, "no shared funding found among the buyers")
            else:
                m["funding_visible"] = False

        # 3. holders now --------------------------------------------------------
        bal = collections.Counter()
        n_logs = 0
        try:
            for lg in self.rpc.get_logs(token, [TRANSFER.topic], lb, latest, 200_000, cap=C.TRANSFER_LOG_CAP):
                t = TRANSFER.decode(lg)
                bal[t["from"]] -= t["value"]
                bal[t["to"]] += t["value"]
                n_logs += 1
        except Exception as e:
            log.info("transfer logs failed for %s: %s", token[:10], e)
            m["partial"] = True
        if n_logs >= C.TRANSFER_LOG_CAP:
            m["partial"] = True
        held = {a: v for a, v in bal.items() if v > 0 and a not in C.INFRA and a not in (L["curve"], token)}
        circ = sum(held.values())
        total = sum(v for v in bal.values() if v > 0)
        m["outside_pool_pct"] = round(_pct(circ, total), 1)
        top = sorted(held.values(), reverse=True)
        top10 = _pct(sum(top[:10]), circ)
        dep_hold = _pct(held.get(L["deployer"], 0), circ)
        m.update(holders=len(held), top10_pct=round(top10, 1), deployer_hold_pct=round(dep_hold, 1), transfers=n_logs)
        bubbles = [{"a": a, "pct": round(_pct(v, circ), 2), "dep": a == L["deployer"], "top": a == top_addr}
                   for a, v in sorted(held.items(), key=lambda kv: -kv[1])[:40]]
        self._emit(token, "holders", f"{len(held)} holders, top-10 own {top10:.0f}%, creator holds {dep_hold:.0f}%",
                   holders=len(held), top10_pct=round(top10, 1), deployer_hold_pct=round(dep_hold, 1),
                   outside_pool_pct=m.get("outside_pool_pct"), bubbles=bubbles, partial=m["partial"])
        if held and not m["partial"]:
            if top10 < 20:
                rule("top10", 15, f"top-10 holders own {top10:.0f}% of circulating supply")
            elif top10 < 35:
                rule("top10", 5, f"top-10 holders own {top10:.0f}%")
            elif top10 < 50:
                rule("top10", -10, f"top-10 holders own {top10:.0f}%")
            else:
                rule("top10", -20, f"top-10 holders own {top10:.0f}% of circulating supply")
        elif held:
            rule("top10", 0, f"holder map is partial ({n_logs} transfers read); top-10 looks like {top10:.0f}%")
        if dep_hold >= 20:
            rule("deployer_hold", -30, f"creator still holds {dep_hold:.0f}% of circulating supply")
        elif dep_hold >= 10:
            rule("deployer_hold", -15, f"creator still holds {dep_hold:.0f}% of circulating supply")

        # 4. after graduation ---------------------------------------------------
        pool = None
        try:
            for lg in self.rpc.get_logs(C.HOOK, [POOL_REGISTERED.topic], max(0, gb - 30), gb + 30, 1000):
                p = POOL_REGISTERED.decode(lg)
                if p["memecoin"] == token:
                    pool = p
                    break
        except Exception as e:
            log.info("pool lookup failed for %s: %s", token[:10], e)
            m["partial"] = True
        hour_ago = latest - C.BLOCKS_PER_HOUR
        recent, older = [], []       # the last hour is read first with its own cap, so a busy pool never hides it
        if pool:
            topics = [SWAP_TOPIC, pool["poolId"]]
            lo = max(gb, hour_ago)
            try:
                recent = [SWAP.decode(lg) for lg in self.rpc.get_logs(C.POOL_MANAGER, topics, lo, latest, 200_000, cap=SWAP_CAP)]
                if len(recent) >= SWAP_CAP:
                    m["partial"] = True
                if lo > gb:
                    older = [SWAP.decode(lg) for lg in self.rpc.get_logs(C.POOL_MANAGER, topics, gb, lo - 1, 200_000, cap=SWAP_CAP)]
                    if len(older) >= SWAP_CAP:
                        m["partial"] = True
            except Exception as e:
                log.info("swap logs failed for %s: %s", token[:10], e)
                m["partial"] = True
        swaps = recent + older
        token_is_0 = int(token, 16) < int(pool["quoteToken"], 16) if pool else True
        n1h = sum(1 for s in recent if s["_block"] >= hour_ago)
        buys1h = sum(1 for s in recent if s["_block"] >= hour_ago and (s["amount0"] if token_is_0 else s["amount1"]) > 0)
        m.update(pool_id=pool["poolId"] if pool else None, quote=pool["quoteToken"] if pool else L.get("pair_token"),
                 swaps_since_grad=len(swaps), swaps_1h=n1h, buys_1h=buys1h, sells_1h=n1h - buys1h)
        grad_age = max(0, int(time.time()) - int(L.get("grad_ts") or time.time()))
        m["grad_age_s"] = grad_age
        self._emit(token, "pool", f"pool: {len(swaps)} trades since graduation, {n1h} in the last hour ({buys1h} buys)",
                   swaps=len(swaps), swaps_1h=n1h, buys_1h=buys1h, sells_1h=n1h - buys1h, has_pool=bool(pool))
        if 3600 < grad_age <= 6 * 3600 and n1h == 0:
            rule("activity", -8, "no trades in the last hour")
        elif grad_age > 6 * 3600 and n1h == 0:
            rule("activity", 0, f"{len(swaps)} trades since graduation, none in the last hour")
        elif n1h >= 50:
            rule("activity", 5, f"{n1h} trades in the last hour")
        elif swaps:
            rule("activity", 0, f"{len(swaps)} trades since graduation, {n1h} in the last hour")

        # 5. links and pace -----------------------------------------------------
        has_links = any((L.get(k) or "").strip() for k in ("twitter", "telegram", "website"))
        m["has_links"] = has_links
        rule("socials", 0 if has_links else -5, "has socials attached" if has_links else "no socials attached")
        if L.get("ts") and L.get("grad_ts"):
            dt = int(L["grad_ts"]) - int(L["ts"])
            m["launch_to_grad_s"] = dt
            if dt < 600:
                rule("pace", -8, f"graduated {dt // 60} minutes after launch: bot-driven pace")
            elif dt < 3600:
                rule("pace", 0, f"graduated {dt // 60} minutes after launch")
            else:
                rule("pace", 5, f"graduated {dt // 3600}h {dt % 3600 // 60}m after launch: organic pace")

        # 6. market snapshot is filled in batches by prices.refresh_scored (GeckoTerminal rate limits)
        m.update(price_usd=None, fdv_usd=None, volume_24h_usd=None, reserve_usd=None)

        # total ---------------------------------------------------------------
        w = self.weights()
        total = 50.0
        for f in fired:
            f["weight"] = round(w.get(f["rule"], 1.0), 3)
            f["applied"] = round(f["points"] * f["weight"], 1)
            total += f["applied"]
        score = final_score(total)
        if m["partial"]:
            score = min(score, PARTIAL_CAP)
        verdict = verdict_for(score)
        demoted = verdict == "looks healthy" and any((f["rule"], f["points"]) in HARD or f["rule"] == "creator_rugs" for f in fired)
        if demoted:
            verdict = "mixed"
        m["demoted"] = bool(demoted)
        reasons = [f"{f['text']} ({'+' if f['applied'] > 0 else ''}{f['applied']:g})" for f in fired if f["applied"] != 0]
        reasons += [f["text"] for f in fired if f["applied"] == 0]
        if demoted:
            reasons.append("a hard warning fired (creator holding, snipe or a creator rug): healthy demoted to mixed")
        if m["partial"]:
            reasons.insert(0, PARTIAL_TEXT)
        m["scored_in_s"] = round(time.time() - t0, 1)
        self._emit(token, "verdict", f"verdict: {verdict} [{score}]", score=score, verdict=verdict, reasons=reasons,
                   seconds=m["scored_in_s"], partial=m["partial"])
        self.db.x("INSERT OR REPLACE INTO scores(token,score,verdict,reasons,metrics,scored_at,partial,fired)"
                  " VALUES(?,?,?,?,?,?,?,?)",
                  (token, score, verdict, json.dumps(reasons), json.dumps(m), int(time.time()),
                   1 if m["partial"] else 0, json.dumps(fired)))
        return {"token": token, "score": score, "verdict": verdict, "reasons": reasons, "metrics": m, "fired": fired,
                "retry": bool(m["partial"])}


def backfill_curve_buyers(rpc, db, hours=24, limit=150):
    """Remember the buyers of tokens scored before the fleet read existed, so it has curves to compare against.
    One curve-log read per token; nothing is re-scored."""
    since = int(time.time()) - hours * 3600
    rows = db.q("SELECT l.token, l.curve, l.block, l.grad_block, l.grad_ts FROM scores s JOIN launches l ON l.token=s.token"
                " WHERE s.scored_at>=? AND l.curve IS NOT NULL AND l.grad_block IS NOT NULL"
                " AND l.token NOT IN (SELECT DISTINCT token FROM curve_buyers) ORDER BY s.scored_at DESC LIMIT ?", (since, limit))
    done = 0
    for L in rows:
        gb = int(L["grad_block"])
        lb = int(L["block"]) if L["block"] else max(0, gb - 24 * C.BLOCKS_PER_HOUR)
        by_rec = collections.Counter()
        try:
            for lg in rpc.get_logs(L["curve"], [[CURVE_BUY.topic]], lb, gb, 200_000, cap=20_000):
                b = CURVE_BUY.decode(lg)
                by_rec[b["recipient"]] += b["tokensOut"]
        except Exception as e:
            log.info("buyer backfill failed for %s: %s", L["token"][:10], e)
            continue
        total = sum(by_rec.values())
        dust = total // DUST_DIVISOR
        humans = sorted(((a, v) for a, v in by_rec.items() if a not in C.INFRA and v > 0 and v >= dust), key=lambda kv: -kv[1])[:BUYERS_KEPT]
        if humans:
            db.many("INSERT OR REPLACE INTO curve_buyers(token,wallet,tokens_out,ts) VALUES(?,?,?,?)",
                    [(L["token"], a, float(v), int(L["grad_ts"] or since)) for a, v in humans])
            done += 1
    return done
