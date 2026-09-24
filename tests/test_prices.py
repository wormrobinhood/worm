"""The GeckoTerminal client: the rate gate across threads, 429 retries, entry ages, the ETH fallback, and the
refresh/rescore race. No network: requests.get is replaced in every test."""
import json
import threading
import time

import pytest

from wormhole import prices as P


class Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def gecko(addr, price):
    return {"data": [{"attributes": {"address": addr, "price_usd": str(price), "fdv_usd": "1000",
                                     "volume_usd": {"h24": "50"}, "total_reserve_in_usd": "20"}}]}


def down(*args, **kwargs):
    raise P.requests.ConnectionError("down")


@pytest.fixture(autouse=True)
def fresh_module(monkeypatch):
    monkeypatch.setattr(P, "_cache", {})
    monkeypatch.setattr(P, "_last_call", 0.0)
    monkeypatch.setattr(P, "_eth", (0.0, 2500.0))
    monkeypatch.setattr(P, "_eth_failed", 0.0)
    monkeypatch.setattr(P, "MIN_GAP", 0.0)


A = "0x" + "ab" * 20


def test_429_is_retried_with_backoff(monkeypatch):
    answers = [Resp(429, text="slow down"), Resp(429, text="slow down"), Resp(200, gecko(A, 0.5))]
    calls, sleeps = [], []
    monkeypatch.setattr(P.requests, "get", lambda url, timeout, headers: (calls.append(url), answers.pop(0))[1])
    monkeypatch.setattr(P.time, "sleep", lambda s: sleeps.append(s))
    out = P.token_prices([A])
    assert out[A]["price_usd"] == 0.5 and out[A]["fdv_usd"] == 1000.0 and out[A]["age_s"] == 0
    assert len(calls) == 3 and sleeps == [5.0, 10.0]


def test_429_three_times_gives_up_without_a_price(monkeypatch):
    answers = [Resp(429, text="slow down")] * 3
    sleeps = []
    monkeypatch.setattr(P.requests, "get", lambda url, timeout, headers: answers.pop(0))
    monkeypatch.setattr(P.time, "sleep", lambda s: sleeps.append(s))
    assert P.token_prices([A]) == {A: {"age_s": None}}
    assert sleeps == [5.0, 10.0] and answers == []


def test_stale_entry_is_returned_with_its_age(monkeypatch):
    P._cache[A] = (time.time() - 36000, {"price_usd": 1.0})
    monkeypatch.setattr(P.requests, "get", down)
    out = P.token_prices([A, "0x" + "ef" * 20])
    assert out[A]["price_usd"] == 1.0 and out[A]["age_s"] >= 36000
    assert out["0x" + "ef" * 20] == {"age_s": None}


def test_fresh_entries_are_served_from_the_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(P.requests, "get", lambda url, timeout, headers: (calls.append(url), Resp(200, gecko(A, 2.0)))[1])
    assert P.token_prices([A.upper()])[A]["price_usd"] == 2.0
    assert P.token_prices([A])[A]["price_usd"] == 2.0 and len(calls) == 1
    entry = P.token_prices([A])[A]
    entry["price_usd"] = 999                                     # a caller cannot poison the cache
    assert P.token_prices([A])[A]["price_usd"] == 2.0


def test_gate_spaces_calls_across_threads(monkeypatch):
    monkeypatch.setattr(P, "MIN_GAP", 0.1)
    stamps, lock = [], threading.Lock()

    def fake_get(url, timeout, headers):
        with lock:
            stamps.append(time.time())
        return Resp(200, {"data": []})

    monkeypatch.setattr(P.requests, "get", fake_get)
    threads = [threading.Thread(target=P._get, args=("http://fake.invalid/x",)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(stamps) == 4 and min(gaps) >= 0.1 - 0.03


def test_eth_usd_strict_refuses_seed_and_stale_values(monkeypatch):
    calls = []

    def fail(url, timeout, headers):
        calls.append(url)
        raise P.requests.ConnectionError("down")

    monkeypatch.setattr(P.requests, "get", fail)
    assert P.eth_usd(strict=True) is None and P.eth_usd() == 2500.0
    assert len(calls) == 1                                        # a failed refresh is not retried on every call
    monkeypatch.setattr(P, "_eth_failed", 0.0)
    monkeypatch.setattr(P.requests, "get", lambda url, timeout, headers: Resp(200, {"ethereum": {"usd": 3000.0}}))
    assert P.eth_usd(strict=True) == 3000.0 and P.eth_usd() == 3000.0
    monkeypatch.setattr(P, "_eth", (time.time() - 700, 3000.0))  # eleven minutes old
    monkeypatch.setattr(P.requests, "get", fail)
    assert P.eth_usd() == 3000.0                                  # the last fetched value, never the seed
    assert P.eth_usd(strict=True) is None


def test_refresh_never_overwrites_a_rescore(db, monkeypatch):
    now = int(time.time())
    tok = "0x" + "12" * 20
    db.x("INSERT INTO scores(token,score,verdict,reasons,metrics,scored_at,partial,fired) VALUES(?,?,?,?,?,?,?,?)",
         (tok, 40, "avoid", "[]", json.dumps({"holders": 5, "price_usd": None}), now - 100, 0, "[]"))
    db.x("INSERT INTO outcomes(token,score,verdict,scored_at,price0,checks,outcome,resolved,fired) VALUES(?,?,?,?,?,?,?,?,?)",
         (tok, 40, "avoid", now - 100, None, "{}", "pending", 0, "[]"))

    def prices_then_rescore(addrs):
        db.x("UPDATE scores SET metrics=?, scored_at=?, score=? WHERE token=?",      # a rescore lands mid-refresh
             (json.dumps({"holders": 9, "price_usd": None}), now, 55, tok))
        return {tok: {"price_usd": 2.0, "age_s": 3}}

    monkeypatch.setattr(P, "token_prices", prices_then_rescore)
    P.refresh_scored(db)
    m = json.loads(db.one("SELECT metrics FROM scores WHERE token=?", (tok,))["metrics"])
    assert m["holders"] == 9 and m["price_usd"] is None                       # the rescore survived untouched
    assert db.one("SELECT price0 FROM outcomes WHERE token=?", (tok,))["price0"] == 2.0   # the baseline still landed
    monkeypatch.setattr(P, "token_prices", lambda addrs: {tok: {"price_usd": 2.5, "age_s": 3}})
    assert P.refresh_scored(db) == 1
    m = json.loads(db.one("SELECT metrics FROM scores WHERE token=?", (tok,))["metrics"])
    assert m["holders"] == 9 and m["price_usd"] == 2.5 and "price0_ts" not in m  # cached quote predates the new scan
    assert db.one("SELECT price0 FROM outcomes WHERE token=?", (tok,))["price0"] == 2.0   # a baseline is set once


def test_refresh_sets_the_baseline_within_six_hours_only(db, monkeypatch):
    now = int(time.time())
    for tok, age in (("0x" + "31" * 20, 5 * 3600), ("0x" + "32" * 20, 7 * 3600)):
        db.x("INSERT INTO scores(token,score,verdict,reasons,metrics,scored_at,partial,fired) VALUES(?,?,?,?,?,?,?,?)",
             (tok, 40, "avoid", "[]", "{}", now - age, 0, "[]"))
        db.x("INSERT INTO outcomes(token,score,verdict,scored_at,price0,checks,outcome,resolved,fired) VALUES(?,?,?,?,?,?,?,?,?)",
             (tok, 40, "avoid", now - age, None, "{}", "pending", 0, "[]"))
    monkeypatch.setattr(P, "token_prices", lambda addrs: {a: {"price_usd": 1.0, "age_s": 0} for a in addrs})
    assert P.refresh_scored(db) == 2
    assert db.one("SELECT price0 FROM outcomes WHERE token=?", ("0x" + "31" * 20,))["price0"] == 1.0
    assert db.one("SELECT price0 FROM outcomes WHERE token=?", ("0x" + "32" * 20,))["price0"] is None


def test_eth_usd_last_is_none_until_a_real_fetch(monkeypatch):
    from wormhole import prices as PR
    monkeypatch.setattr(PR, "_eth", (0.0, 2500.0))
    monkeypatch.setattr(PR, "_eth_failed", 0.0)
    monkeypatch.setattr(PR, "_get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert PR.eth_usd_last() is None                       # the seed is never a price
    monkeypatch.setattr(PR, "_eth", (1.0, 2600.0))         # fetched long ago, the API is still down
    monkeypatch.setattr(PR, "_eth_failed", 0.0)
    assert PR.eth_usd_last() == 2600.0 and PR.eth_usd(strict=True) is None


def test_exit_price_cache_never_refreshes_or_returns_seed_stale_nan(monkeypatch):
    monkeypatch.setattr(P, '_get', lambda *a: pytest.fail('network access on cached exit path'))
    assert P.eth_usd_cached() is None
    monkeypatch.setattr(P, '_eth', (time.time(), 2000))
    assert P.eth_usd_cached() == 2000
    for ts, v in [(time.time()-P.ETH_MAX_AGE_S-1, 2000), (time.time(), float('nan')),
                  (time.time()+500, 2000), (time.time(), 0)]:
        monkeypatch.setattr(P, '_eth', (ts, v))
        assert P.eth_usd_cached() is None
