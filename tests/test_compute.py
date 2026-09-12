"""Compute at Venice: the SIWE header, the x402 payment authorization, rail selection, the top-up policy."""
import base64
import json
import re
from datetime import datetime, timedelta

import pytest
from eth_abi import encode
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak

from wormhole import compute as CP, treasury as T

DS = "0x02fa7265e7c5d81118673727957699e4d68f74cd74b7db77da710fe8a2c7834f"      # Base USDC DOMAIN_SEPARATOR()
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAY_TO = "0x000000000000000000000000000000000000dEaD"
RAIL = {"scheme": "exact", "network": "eip155:8453", "amount": "5000000", "payTo": PAY_TO, "asset": USDC,
        "maxTimeoutSeconds": 300, "extra": {"name": "USD Coin", "version": "2"}}
SOLANA = {"scheme": "exact", "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "amount": "5000000", "payTo": "x",
          "asset": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}
SIWE = re.compile(r"^(?P<domain>[^\n]+) wants you to sign in with your Ethereum account:\n(?P<address>0x[0-9a-fA-F]{40})\n\n"
                  r"(?P<statement>[^\n]*)\n\nURI: (?P<uri>\S+)\nVersion: (?P<version>1)\nChain ID: (?P<chain>\d+)\n"
                  r"Nonce: (?P<nonce>[A-Za-z0-9]{8,})\nIssued At: (?P<iat>\S+)(\nExpiration Time: (?P<exp>\S+))?$")
# signature of the independently computed digest (keccak(0x1901 ‖ DS ‖ structHash)) for key 0x..01, fixed time and nonce
GOLDEN_SIG = ("0xeaf6a45f9460dd08a88943523125561f33be47dff2e559ee25d175e3ef6286fe"
              "707d3088ecb5161366bd30259000c0bb644ae3a95884e42ebedee49794e0e0741c")


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.ok = status < 400
        self.text = json.dumps(body)

    def json(self):
        return self._body


def authorization(payload):
    return payload["payload"]["authorization"]


# ---- sign-in ------------------------------------------------------------------

def test_siwe_header_follows_eip4361(acct):
    url = f"https://api.venice.ai/api/v1/x402/balance/{acct.address}"
    body = json.loads(base64.b64decode(CP.siwe_header(acct, url)))
    m = SIWE.match(body["message"])
    assert m
    assert m["domain"] == "api.venice.ai" and m["address"] == acct.address and m["statement"] == "Sign in to Venice AI"
    assert m["uri"] == url and m["chain"] == "8453" and len(m["nonce"]) >= 8
    iat = datetime.strptime(m["iat"], "%Y-%m-%dT%H:%M:%S.%fZ")
    exp = datetime.strptime(m["exp"], "%Y-%m-%dT%H:%M:%S.%fZ")
    assert exp - iat == timedelta(minutes=5)
    assert Account.recover_message(encode_defunct(text=body["message"]), signature=body["signature"]) == acct.address
    assert body["timestamp"] > 1e12 and body["chainId"] == 8453 and body["address"] == acct.address


# ---- the payment authorization --------------------------------------------------

def independent_digest(a):
    th = keccak(text="TransferWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)")
    dh = keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
    ds = keccak(encode(["bytes32", "bytes32", "bytes32", "uint256", "address"], [dh, keccak(text="USD Coin"), keccak(text="2"), 8453, USDC]))
    assert "0x" + ds.hex() == DS
    sh = keccak(encode(["bytes32", "address", "address", "uint256", "uint256", "uint256", "bytes32"],
                       [th, a["from"], a["to"], int(a["value"]), int(a["validAfter"]), int(a["validBefore"]), bytes.fromhex(a["nonce"][2:])]))
    return keccak(b"\x19\x01" + ds + sh)


def test_payment_header_signs_the_onchain_domain(acct):
    header, payload = CP.payment_header(acct, RAIL, 5.0)
    a = authorization(payload)
    assert Account._recover_hash(independent_digest(a), signature=payload["payload"]["signature"]) == acct.address
    assert json.loads(base64.b64decode(header)) == payload
    assert payload["x402Version"] == 2 and payload["scheme"] == "exact"
    assert a["from"] == acct.address and a["to"] == PAY_TO and a["value"] == "5000000"
    assert int(a["validBefore"]) - int(a["validAfter"]) == 300 + CP.VALID_AFTER_SKEW_S
    assert len(bytes.fromhex(a["nonce"][2:])) == 32


def test_payment_header_golden():
    acct = Account.from_key("0x" + "00" * 31 + "01")
    _, p = CP.payment_header(acct, RAIL, 5.0, now=1_700_000_000, nonce="0x" + "11" * 32)
    assert authorization(p) == {"from": acct.address, "to": PAY_TO, "value": "5000000", "validAfter": "1699999400",
                                "validBefore": "1700000300", "nonce": "0x" + "11" * 32}
    assert p["payload"]["signature"] == GOLDEN_SIG
    assert Account._recover_hash(independent_digest(authorization(p)), signature=GOLDEN_SIG) == acct.address


def test_amounts_are_rounded_to_usdc_units(acct):
    for usd, want in ((5, "5000000"), (0.1 + 0.2, "300000"), (4.999999, "4999999"), (0.29, "290000")):
        assert authorization(CP.payment_header(acct, RAIL, usd)[1])["value"] == want


def test_nonces_are_fresh(acct):
    assert len({authorization(CP.payment_header(acct, RAIL, 5)[1])["nonce"] for _ in range(5)}) == 5


# ---- rails -----------------------------------------------------------------------

def test_pick_rail_takes_only_exact_base_usdc():
    assert CP.pick_rail([SOLANA, RAIL]) is RAIL
    assert CP.pick_rail([{**RAIL, "network": "base"}])["network"] == "base"
    assert CP.pick_rail([{**RAIL, "asset": USDC.lower()}])["asset"] == USDC.lower()
    v1 = {k: v for k, v in RAIL.items() if k != "amount"}
    with pytest.raises(RuntimeError, match="v1"):
        CP.pick_rail([{**v1, "maxAmountRequired": "5000000"}])
    for bad in ({**RAIL, "scheme": "upto"}, {**RAIL, "asset": "0x" + "ab" * 20}, {**RAIL, "network": "eip155:1"}, SOLANA):
        with pytest.raises(RuntimeError, match="no exact Base-USDC rail"):
            CP.pick_rail([bad])
    with pytest.raises(RuntimeError):
        CP.pick_rail([])


# ---- top_up -----------------------------------------------------------------------

def test_top_up_dry_run_signs_nothing(acct, monkeypatch):
    posts = []
    monkeypatch.setattr(CP.requests, "post", lambda url, **kw: posts.append(kw) or Resp(402, {"x402Version": 2, "accepts": [SOLANA, RAIL]}))
    monkeypatch.setattr(CP, "payment_header", lambda *a, **k: pytest.fail("signed in a dry run"))
    r = CP.top_up(acct, 5.0, live=False)
    assert r["sent"] is False and r["amount_usd"] == 5.0 and r["payTo"] == PAY_TO and r["rail"] is RAIL
    assert len(posts) == 1 and "headers" not in posts[0]


def test_top_up_live_needs_wh_live(acct, monkeypatch):
    monkeypatch.setattr(CP.requests, "post", lambda *a, **k: pytest.fail("no request may leave"))
    with pytest.raises(RuntimeError, match="WH_LIVE=0"):
        CP.top_up(acct, 5.0, live=True)


def test_top_up_live_sends_the_payment_header_and_respects_the_minimum(acct, monkeypatch, live):
    posts = []

    def post(url, **kw):
        posts.append(kw)
        if "headers" not in kw:
            return Resp(402, {"x402Version": 2, "accepts": [{**RAIL, "amount": "7000000"}]})
        return Resp(200, {"data": {"balanceUsd": 7.0}})

    monkeypatch.setattr(CP.requests, "post", post)
    r = CP.top_up(acct, 5.0, live=True)
    assert r["sent"] and r["amount_usd"] == 7.0
    payload = json.loads(base64.b64decode(posts[1]["headers"]["X-402-Payment"]))
    assert authorization(payload)["value"] == "7000000" and authorization(payload)["to"] == PAY_TO


def test_top_up_refuses_a_non_402_answer(acct, monkeypatch):
    monkeypatch.setattr(CP.requests, "post", lambda url, **kw: Resp(200, {}))
    with pytest.raises(RuntimeError, match="unexpected 200"):
        CP.top_up(acct, 5.0, live=False)


# ---- the policy -----------------------------------------------------------------------

@pytest.fixture
def venice(monkeypatch):
    monkeypatch.setenv("WH_VOICE_MODEL", "venice:llama-3.3-70b")
    monkeypatch.setattr(CP, "TOPUP_ALWAYS", False)


def planner(monkeypatch, balance=0.5, fail=None):
    """Fake Venice: a balance read and a top_up that records its calls."""
    monkeypatch.setattr(CP, "balance", lambda acct: {"balanceUsd": balance})
    calls = []

    def top_up(acct, amount, live):
        calls.append((amount, live))
        if fail:
            raise fail
        return {"sent": bool(live), "amount_usd": amount, "payTo": PAY_TO}

    monkeypatch.setattr(CP, "top_up", top_up)
    return calls


def kinds(db):
    T.ensure_tables(db)
    return [r["kind"] for r in db.q("SELECT kind FROM ledger ORDER BY id")]


def test_plan_tops_up_once_then_waits_for_the_cooldown(db, acct, live, venice, monkeypatch):
    calls = planner(monkeypatch)
    for _ in range(3):
        CP.plan(db, acct, 20.0, True, True)
    assert calls == [(5.0, True)]
    assert db.q("SELECT kind, amount, asset FROM ledger") == [{"kind": "compute", "amount": 5.0, "asset": "USDC"}]
    texts = [e["text"] for e in db.q("SELECT text FROM events WHERE kind='compute' ORDER BY id")]
    assert texts[0] == "topped up $5.00 of compute at Venice"


def test_plan_daily_cap(db, acct, live, venice, monkeypatch):
    monkeypatch.setattr(CP, "TOPUP_COOLDOWN_S", 0)
    calls = planner(monkeypatch)
    for _ in range(6):
        CP.plan(db, acct, 20.0, True, True)
    assert len(calls) == CP.TOPUP_MAX_PER_DAY == 2 and kinds(db) == ["compute", "compute"]


def test_plan_skips_when_the_balance_is_unknown_or_fine(db, acct, live, venice, monkeypatch):
    calls = planner(monkeypatch, balance=3.0)
    CP.plan(db, acct, 20.0, True, True)
    monkeypatch.setattr(CP, "balance", lambda acct: (_ for _ in ()).throw(RuntimeError("venice balance 500")))
    st = CP.plan(db, acct, 20.0, True, True)
    assert calls == [] and st["balance_usd"] is None and "500" in st["error"]


def test_plan_respects_runway_and_the_base_wallet(db, acct, live, venice, monkeypatch):
    calls = planner(monkeypatch)
    CP.plan(db, acct, 20.0, False, True)
    CP.plan(db, acct, 4.0, True, True)
    CP.plan(db, acct, None, True, True)
    assert calls == [] and kinds(db) == []
    texts = [e["text"] for e in db.q("SELECT text FROM events WHERE kind='compute' ORDER BY id")]
    assert len(texts) == 1 and "runway rule" in texts[0]              # said once an hour, not every cycle


def test_plan_failure_after_payment_leaves_a_pending_row_that_counts(db, acct, live, venice, monkeypatch):
    calls = planner(monkeypatch, fail=RuntimeError("socket closed after the payment"))
    CP.plan(db, acct, 20.0, True, True)
    assert kinds(db) == ["compute_pending"]
    assert "top-up failed" in db.one("SELECT text FROM events WHERE kind='error'")["text"]
    CP.plan(db, acct, 20.0, True, True)
    assert len(calls) == 1                                             # the pending row starts the cooldown


def test_plan_demo_writes_no_ledger_row_and_signs_nothing(db, acct, venice, monkeypatch):
    calls = planner(monkeypatch)
    CP.plan(db, acct, 20.0, True, False)
    assert calls == [(5.0, False)] and kinds(db) == []
    assert "would top up $5.00" in db.one("SELECT text FROM events WHERE kind='compute'")["text"]


def test_plan_needs_something_that_spends_the_balance(db, acct, live, monkeypatch):
    monkeypatch.setenv("WH_VOICE_MODEL", "anthropic:claude")
    monkeypatch.setattr(CP, "TOPUP_ALWAYS", False)
    calls = planner(monkeypatch)
    CP.plan(db, acct, 20.0, True, True)
    assert calls == [] and "nothing spends it" in db.one("SELECT text FROM events WHERE kind='compute'")["text"]
    monkeypatch.setattr(CP, "TOPUP_ALWAYS", True)
    CP.plan(db, acct, 20.0, True, True)
    assert len(calls) == 1


def test_topups_today_counts_done_and_pending(db):
    T.ensure_tables(db)
    now = 1_800_000_000
    for ts, kind in ((now - 100, "compute"), (now - 200, "compute_pending"), (now - 90_000, "compute"), (now - 50, "claim")):
        db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)", (ts, kind, "USDC", 5.0, None, ""))
    assert CP.topups_today(db, now) == (2, now - 100)


def test_status_without_a_wallet():
    assert CP.status(None)["error"] == "no wallet"


def test_topup_blocked_when_the_compute_budget_cannot_cover_it(db, monkeypatch):
    """The runway rule 'spend on compute at most half of what it earns' is enforced, not just displayed."""
    from wormhole import compute as CP
    T.ensure_tables(db)
    monkeypatch.setattr(CP, "status", lambda acct, usdc=None: {"balance_usd": 0.2, "usdc_base": 20.0, "error": None})
    monkeypatch.setattr(CP, "uses_venice", lambda: True)
    monkeypatch.setattr(CP, "top_up", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called")))
    CP.plan(db, object(), 20.0, True, False, budget_per_day=0.10)          # $3 a month cannot cover a $5 top-up
    assert db.one("SELECT COUNT(*) n FROM ledger")["n"] == 0
    ev = db.one("SELECT text FROM events WHERE kind='compute' ORDER BY id DESC LIMIT 1")
    assert ev and "compute budget" in ev["text"]
