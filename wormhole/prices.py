"""USD prices from GeckoTerminal (free, no key) with a short cache.

Every entry token_prices() returns carries age_s, the seconds since it was fetched, so a caller can refuse
a stale reading. One gate spaces calls across every thread and a 429 is retried with growing pauses."""
import logging
import math
import threading
import time

import requests

from . import config as C

log = logging.getLogger("wormhole.prices")
_cache = {}            # addr -> (fetched_ts, dict)
_eth = (0.0, 2500.0)   # (fetched_ts, usd); ts 0 is the seed, never fetched
_eth_failed = 0.0      # when the last ETH refresh failed, so a dead API is not hit on every call
TTL = 120
ETH_MAX_AGE_S = 600    # strict callers refuse an ETH price older than this
ETH_RETRY_S = 60       # after a failed ETH refresh, wait this long before trying again
PRICE0_WINDOW_S = 6 * 3600   # a verdict's price baseline may still be taken this long after scoring
_last_call = 0.0
_gate = threading.Lock()
MIN_GAP = 2.5          # GeckoTerminal's free tier allows about 30 calls a minute
TRIES_429 = 3
BACKOFF_429_S = 5.0


MAX_PRICE_AGE_S = 900


def usable_price(entry, max_age=MAX_PRICE_AGE_S):
    """Validate a quote before learning or simulation. Missing age is legacy caller compatibility;
    the production provider always supplies age_s (None means no observation)."""
    if not entry:
        return None
    try:
        age = entry.get('age_s', 0)
        value = float(entry.get('price_usd') or 0)
        if age is None or not math.isfinite(float(age)) or not 0 <= float(age) <= max_age:
            return None
        return value if math.isfinite(value) and value > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def observed_at(entry, now=None):
    now = time.time() if now is None else now
    return int(now - float(entry.get('age_s', 0) or 0))


def _get(url, timeout=25):
    """One rate-limited GET. The gate keeps calls MIN_GAP apart across threads; a 429 is retried up to
    TRIES_429 attempts with pauses of BACKOFF_429_S * attempt. The last response is returned either way."""
    global _last_call
    r = None
    for i in range(TRIES_429):
        with _gate:
            gap = MIN_GAP - (time.time() - _last_call)
            if gap > 0:
                time.sleep(gap)
            _last_call = time.time()
        r = requests.get(url, timeout=timeout, headers={"User-Agent": C.UA, "Accept": "application/json"})
        if r.status_code != 429:
            return r
        log.info("gecko 429 (attempt %d of %d)", i + 1, TRIES_429)
        if i + 1 < TRIES_429:
            time.sleep(BACKOFF_429_S * (i + 1))
    return r


def token_prices(addrs):
    """{addr: {price_usd, fdv_usd, volume_24h_usd, reserve_usd, age_s}} for every address asked.
    age_s is the age of the entry in seconds; None when nothing was ever fetched for that address."""
    addrs = [a.lower() for a in addrs]
    now = time.time()
    need = [a for a in dict.fromkeys(addrs) if a not in _cache or now - _cache[a][0] > TTL]
    for i in range(0, len(need), 30):
        part = need[i:i + 30]
        try:
            r = _get(f"{C.GECKO}/tokens/multi/{','.join(part)}")
            if r.status_code != 200:
                log.info("gecko %s: %s", r.status_code, r.text[:80])
                continue
            fetched = time.time()
            received = set()
            for t in r.json().get("data", []):
                a = t.get("attributes", {})
                addr = (a.get("address") or "").lower()
                if addr not in part:
                    continue
                received.add(addr)
                vol = a.get("volume_usd") or {}
                _cache[addr] = (fetched, {
                    "price_usd": float(a["price_usd"]) if a.get("price_usd") else None,
                    "fdv_usd": float(a["fdv_usd"]) if a.get("fdv_usd") else None,
                    "volume_24h_usd": float(vol["h24"]) if vol.get("h24") else None,
                    "reserve_usd": float(a["total_reserve_in_usd"]) if a.get("total_reserve_in_usd") else None})
            for a in part:
                if a not in received:
                    _cache[a] = (fetched, {})  # absence is unknown, never a synthetic zero price
        except Exception as e:
            log.info("gecko error: %s", e)
    now = time.time()
    out = {}
    for a in addrs:
        ts, d = _cache.get(a, (0, {}))
        entry = dict(d)
        entry["age_s"] = max(0, int(now - ts)) if ts else None
        out[a] = entry
    return out


def eth_usd(strict=False):
    """ETH in USD from CoinGecko, refreshed every ETH_MAX_AGE_S. strict=True returns None instead of a
    value that was never fetched or is older than ETH_MAX_AGE_S; otherwise a failed refresh returns the
    last value fetched (the seed until the first fetch succeeds)."""
    global _eth, _eth_failed
    now = time.time()
    if _eth[0] and now - _eth[0] < ETH_MAX_AGE_S:
        return _eth[1]
    if now - _eth_failed >= ETH_RETRY_S:
        try:
            r = _get("https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd", 15)
            v = float(r.json()["ethereum"]["usd"])
            _eth = (time.time(), v)
            return v
        except Exception as e:
            _eth_failed = time.time()
            log.info("eth price error: %s", e)
    if strict:
        return None
    return _eth[1]


def eth_usd_last():
    """The last ETH price that was really fetched, however old, refreshing it when due; None before the
    first successful fetch. For marking a position between refreshes, never for sizing a trade."""
    eth_usd()
    return _eth[1] if _eth[0] else None


def refresh_scored(db, hours=24):
    """Batch-price every token scored in the last `hours`: fills price/fdv/volume on the cards and the
    outcome baseline the brain compares against. One or two API calls per cycle instead of one per token.
    A row is only written back when its scored_at is unchanged, so a rescore in flight is never overwritten."""
    import json
    rows = db.q("SELECT token, metrics, scored_at FROM scores WHERE scored_at>=?", (int(time.time()) - hours * 3600,))
    if not rows:
        return 0
    px = token_prices([r["token"] for r in rows])
    n = 0
    for r in rows:
        p = px.get(r["token"]) or {}
        if usable_price(p) is None:
            continue
        try:
            m = json.loads(r["metrics"] or "{}")
        except ValueError:
            m = {}
        now = int(time.time())
        fetched_at = now - int(p.get("age_s") or 0)
        m.update(price_usd=p.get("price_usd"), fdv_usd=p.get("fdv_usd"), volume_24h_usd=p.get("volume_24h_usd"),
                 reserve_usd=p.get("reserve_usd"), price_ts=fetched_at)
        if not m.get("price0_ts") and fetched_at >= r["scored_at"]:
            m["price0_ts"] = fetched_at          # when the first price for this verdict was read
            m["price0_usd"], m["fdv0_usd"] = p.get("price_usd"), p.get("fdv_usd")   # the values at scan, never overwritten
        db.x("UPDATE scores SET metrics=? WHERE token=? AND scored_at=?", (json.dumps(m), r["token"], r["scored_at"]))
        db.x("UPDATE outcomes SET price0=?, baseline_ts=? WHERE token=? AND price0 IS NULL"
             " AND scored_at>=? AND scored_at<=? AND resolved=0",
             (p["price_usd"], fetched_at, r["token"], now - PRICE0_WINDOW_S, fetched_at))
        n += 1
    return n
