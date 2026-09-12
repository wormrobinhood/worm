"""USD prices from GeckoTerminal (free, no key) with a short cache."""
import logging
import time

import requests

from . import config as C

log = logging.getLogger("wormhole.prices")
_cache = {}          # addr -> (ts, dict)
_eth = (0, 2500.0)
TTL = 120
_last_call = 0.0
MIN_GAP = 2.5        # GeckoTerminal's free tier allows about 30 calls a minute


def _get(url, timeout=25):
    global _last_call
    gap = MIN_GAP - (time.time() - _last_call)
    if gap > 0:
        time.sleep(gap)
    _last_call = time.time()
    return requests.get(url, timeout=timeout, headers={"User-Agent": C.UA, "Accept": "application/json"})


def token_prices(addrs):
    """{addr: {price_usd, fdv_usd, volume_24h_usd, reserve_usd}} for the addresses it knows."""
    now = time.time()
    addrs = [a.lower() for a in addrs]
    need = [a for a in dict.fromkeys(addrs) if a not in _cache or now - _cache[a][0] > TTL]
    for i in range(0, len(need), 30):
        part = need[i:i + 30]
        try:
            r = _get(f"{C.GECKO}/tokens/multi/{','.join(part)}")
            if r.status_code != 200:
                log.info("gecko %s: %s", r.status_code, r.text[:80])
                continue
            for t in r.json().get("data", []):
                a = t.get("attributes", {})
                addr = (a.get("address") or "").lower()
                vol = a.get("volume_usd") or {}
                _cache[addr] = (now, {
                    "price_usd": float(a["price_usd"]) if a.get("price_usd") else None,
                    "fdv_usd": float(a["fdv_usd"]) if a.get("fdv_usd") else None,
                    "volume_24h_usd": float(vol["h24"]) if vol.get("h24") else None,
                    "reserve_usd": float(a["total_reserve_in_usd"]) if a.get("total_reserve_in_usd") else None})
            for a in part:
                _cache.setdefault(a, (now, {}))
        except Exception as e:
            log.info("gecko error: %s", e)
    return {a: _cache.get(a, (0, {}))[1] for a in addrs}


def eth_usd():
    global _eth
    if time.time() - _eth[0] < 600:
        return _eth[1]
    try:
        r = _get("https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd", 15)
        v = float(r.json()["ethereum"]["usd"])
        _eth = (time.time(), v)
    except Exception as e:
        log.info("eth price error: %s", e)
    return _eth[1]


def refresh_scored(db, hours=24):
    """Batch-price every token scored in the last `hours`: fills price/fdv/volume on the cards and the
    outcome baseline the brain compares against. One or two API calls per cycle instead of one per token."""
    import json
    rows = db.q("SELECT token, metrics FROM scores WHERE scored_at>=?", (int(time.time()) - hours * 3600,))
    if not rows:
        return 0
    px = token_prices([r["token"] for r in rows])
    n = 0
    for r in rows:
        p = px.get(r["token"]) or {}
        if not p.get("price_usd"):
            continue
        try:
            m = json.loads(r["metrics"] or "{}")
        except ValueError:
            m = {}
        m.update(price_usd=p.get("price_usd"), fdv_usd=p.get("fdv_usd"), volume_24h_usd=p.get("volume_24h_usd"),
                 reserve_usd=p.get("reserve_usd"))
        db.x("UPDATE scores SET metrics=? WHERE token=?", (json.dumps(m), r["token"]))
        db.x("UPDATE outcomes SET price0=? WHERE token=? AND price0 IS NULL AND scored_at>=?",
             (p["price_usd"], r["token"], int(time.time()) - 1800))
        n += 1
    return n
