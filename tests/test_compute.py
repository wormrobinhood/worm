"""Compute at Venice: the SIWE header, the x402 payment authorization, rail selection, the top-up policy."""
import base64
import time
import json
import re
from datetime import datetime, timedelta

import pytest
from eth_abi import encode
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak

from wormhole import compute as CP, config as C, treasury as T

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


def test_top_up_rejects_a_minimum_above_the_approved_amount(acct, monkeypatch, live):
    posts = []

    def post(url, **kw):
        posts.append(kw)
        if "headers" not in kw:
            return Resp(402, {"x402Version": 2, "accepts": [{**RAIL, "amount": "7000000"}]})
        return Resp(200, {"data": {"balanceUsd": 7.0}})

    monkeypatch.setattr(CP.requests, "post", post)
    with pytest.raises(RuntimeError, match='minimum exceeds'):
        CP.top_up(acct, 5.0, live=True)
    assert len(posts) == 1  # never sign more than the approved amount


def test_top_up_refuses_a_non_402_answer(acct, monkeypatch):
    monkeypatch.setattr(CP.requests, "post", lambda url, **kw: Resp(200, {}))
    with pytest.raises(RuntimeError, match="unexpected 200"):
        CP.top_up(acct, 5.0, live=False)


# ---- the policy -----------------------------------------------------------------------

@pytest.fixture
def venice(monkeypatch):
    monkeypatch.setattr(CP, "PROVIDER", "venice")
    monkeypatch.setenv("WH_VOICE_MODEL", "venice:llama-3.3-70b")
    monkeypatch.setenv("WH_ADVISOR_MODEL", "")
    monkeypatch.setattr(CP, "TOPUP_ALWAYS", False)


def planner(monkeypatch, balance=0.5, fail=None):
    """Fake Venice: a balance read and a top_up that records its calls."""
    monkeypatch.setattr(CP, "balance", lambda acct: {"balanceUsd": balance})
    calls = []

    def top_up(acct, amount, live, before_payment=None):
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


def test_plan_venice_top_up_is_watched(db, acct, live, venice, monkeypatch):
    """The screen and the busy flag follow a Venice top-up like any other transaction: once when the
    payment goes out, once when it is done (or failed), and the worm is not left busy."""
    seen = []
    monkeypatch.setattr(T, "WATCH", lambda ev: seen.append((ev["action"], ev["done"], ev["text"])))
    monkeypatch.setattr(CP, "TOPUP_COOLDOWN_S", 0)
    planner(monkeypatch)
    CP.plan(db, acct, 20.0, True, True)
    assert [s[:2] for s in seen] == [("compute", False), ("compute", True)]
    assert "buying $5.00 of compute at Venice" in seen[0][2] and "bought $5.00" in seen[1][2]
    assert T.BUSY_SINCE == 0.0
    seen.clear()
    planner(monkeypatch, fail=RuntimeError("socket closed"))
    CP.plan(db, acct, 20.0, True, True)
    assert [s[:2] for s in seen] == [("compute", False), ("compute", True)] and "needs reconciliation" in seen[1][2]
    assert T.BUSY_SINCE == 0.0
    seen.clear()
    CP.plan(db, acct, 20.0, True, False)                                # demo: nothing goes out, nothing to watch
    assert seen == []


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
    assert calls == [] and st["balance_usd"] is None and st["error"] == "compute provider unavailable"


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
    monkeypatch.setattr(CP, "PROVIDER", "venice")
    monkeypatch.setenv("WH_VOICE_MODEL", "anthropic:claude")
    monkeypatch.setenv("WH_ADVISOR_MODEL", "")
    monkeypatch.setattr(CP, "TOPUP_ALWAYS", False)
    calls = planner(monkeypatch)
    CP.plan(db, acct, 20.0, True, True)
    assert calls == [] and "nothing spends it" in db.one("SELECT text FROM events WHERE kind='compute'")["text"]
    monkeypatch.setattr(CP, "TOPUP_ALWAYS", True)
    CP.plan(db, acct, 20.0, True, True)
    assert len(calls) == 1


def test_topups_today_counts_failed_and_written_off_rows_too(db):
    T.ensure_tables(db)
    now = int(time.time())
    for kind in ("compute_failed", "compute_dropped"):
        db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)", (now - 60, kind, "USDC", 5.0, None, "x"))
    assert CP.topups_today(db, now)[0] == 2


def test_topups_today_counts_done_and_pending(db):
    T.ensure_tables(db)
    now = 1_800_000_000
    for ts, kind in ((now - 100, "compute"), (now - 200, "compute_pending"), (now - 90_000, "compute"), (now - 50, "claim")):
        db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)", (ts, kind, "USDC", 5.0, None, ""))
    assert CP.topups_today(db, now) == (2, now - 100)


def test_status_without_a_wallet(monkeypatch):
    monkeypatch.setattr(CP, "PROVIDER", "venice")
    assert CP.status(None)["error"] == "no wallet"


def test_topup_blocked_when_the_compute_budget_cannot_cover_it(db, monkeypatch):
    """The runway rule 'spend on compute at most half of what it earns' is enforced, not just displayed."""
    from wormhole import compute as CP
    T.ensure_tables(db)
    monkeypatch.setattr(CP, "PROVIDER", "venice")
    monkeypatch.setattr(CP, "status", lambda acct, usdc=None: {"balance_usd": 0.2, "usdc_base": 20.0, "error": None})
    monkeypatch.setattr(CP, "uses_compute", lambda: True)
    monkeypatch.setattr(CP, "top_up", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called")))
    CP.plan(db, object(), 20.0, True, False, budget_per_day=0.10)          # $3 a month cannot cover a $5 top-up
    assert db.one("SELECT COUNT(*) n FROM ledger")["n"] == 0
    ev = db.one("SELECT text FROM events WHERE kind='compute' ORDER BY id DESC LIMIT 1")
    assert ev and "compute budget" in ev["text"]


# ---- AI Surplus: paid in USDG on Robinhood Chain, no bridge ------------------------------------------------

DEPOSIT = "0x" + "33" * 20
KEY = "sk-clb-" + "k" * 40


@pytest.fixture
def aisurplus(monkeypatch):
    monkeypatch.setattr(CP, "PROVIDER", "aisurplus")
    monkeypatch.setattr(C, "AISURPLUS_KEY", KEY)
    monkeypatch.setattr(C, "AISURPLUS_DEPOSIT", DEPOSIT)
    monkeypatch.setenv("WH_VOICE_MODEL", "aisurplus:deepseek-v4-flash")
    monkeypatch.setenv("WH_ADVISOR_MODEL", "")
    monkeypatch.setattr(CP, "TOPUP_ALWAYS", False)
    monkeypatch.setattr(CP, "AISURPLUS_FALLBACK", "")
    monkeypatch.setattr(CP, "_markets", (0.0, {}))


def surplus_api(monkeypatch, free=True, balance=0.25, markets_status=200):
    """Fake AI Surplus: the public market list and the key's status."""
    body = {"markets": [{"id": "deepseek-v4-flash", "status": "serving", "lane": "metered", "free": free},
                        {"id": "gpt-6-astra", "status": "serving", "lane": "chatgpt", "free": False}]}

    def get(url, **kw):
        if url.endswith("/portal-api/markets"):
            assert "headers" not in kw or "Authorization" not in kw["headers"]      # public: the key never goes there
            return Resp(markets_status, body)
        if url.endswith("/portal-api/key"):
            assert kw["headers"]["Authorization"] == "Bearer " + KEY
            return Resp(200, {"balance": {"available_usd": balance}, "key": {"status": "active", "weekly_cap_usd": 5, "models": ["*"]}})
        raise AssertionError(url)
    monkeypatch.setattr(CP.requests, "get", get)


def test_aisurplus_key_status_markets_and_status(aisurplus, monkeypatch):
    surplus_api(monkeypatch)
    assert CP.aisurplus_key() == {"available_usd": 0.25, "key_status": "active", "weekly_cap_usd": 5, "models": ["*"]}
    assert CP.aisurplus_free("deepseek-v4-flash") is True and CP.aisurplus_free("gpt-6-astra") is False
    assert CP.aisurplus_free("nope") is None
    st = CP.status(None, 12.0)
    assert st["provider"] == "aisurplus" and st["pays_with"] == "USDG on Robinhood Chain"
    assert st["balance_usd"] == 0.25 and st["free"] is True and st["deposit"] == DEPOSIT and st["wallet_usd"] == 12.0
    assert st["key_status"] == "active" and st["weekly_cap_usd"] == 5 and st["models"] == ["deepseek-v4-flash"]
    surplus_api(monkeypatch, markets_status=500)
    monkeypatch.setattr(CP, "_markets", (0.0, {}))
    assert CP.aisurplus_free("deepseek-v4-flash") is None and CP.status(None)["free"] is False   # unknown is not free


def test_aisurplus_chat_sends_the_key_and_reads_the_answer(aisurplus, monkeypatch):
    seen = {}

    def post(url, **kw):
        seen.update(url=url, **kw)
        return Resp(200, {"choices": [{"message": {"content": "hello worm"}}], "usage": {"total_tokens": 7}})
    monkeypatch.setattr(CP.requests, "post", post)
    text, usage = CP.aisurplus_chat("deepseek-v4-flash", [{"role": "user", "content": "hi"}], max_tokens=50)
    assert text == "hello worm" and usage["total_tokens"] == 7
    assert seen["url"] == "https://aisurplus.io/v1/chat/completions" and seen["headers"]["Authorization"] == "Bearer " + KEY
    assert seen["json"] == {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 50, "temperature": 0.8}
    for code, msg in ((401, "rejected"), (402, "no compute balance"), (503, "aisurplus chat 503")):
        monkeypatch.setattr(CP.requests, "post", lambda url, **kw: Resp(code, {"error": "x"}))
        with pytest.raises(RuntimeError, match=msg):
            CP.aisurplus_chat("deepseek-v4-flash", [])


def test_aisurplus_needs_a_key(aisurplus, monkeypatch):
    monkeypatch.setattr(C, "AISURPLUS_KEY", "")
    monkeypatch.setattr(CP.requests, "post", lambda *a, **k: pytest.fail("no request may leave without a key"))
    with pytest.raises(RuntimeError, match="WH_AISURPLUS_KEY"):
        CP.aisurplus_chat("deepseek-v4-flash", [])
    surplus_api(monkeypatch)
    assert CP.status(None)["error"] == "no key yet"


def test_voice_routes_aisurplus_models(aisurplus, monkeypatch):
    from wormhole import voice
    monkeypatch.setattr(CP, "aisurplus_chat", lambda model, messages, max_tokens=400, temperature=0.8:
                        (f"{model}|{messages[0]['role']}:{messages[0]['content']}|{messages[1]['role']}:{messages[1]['content']}|{max_tokens}", {"n": 1}))
    assert voice._llm("aisurplus", "qwen3.8-flash", "sys", "usr", max_tokens=99) == ("qwen3.8-flash|system:sys|user:usr|99", {"n": 1})


def test_deposit_address_is_checked(aisurplus, monkeypatch):
    assert CP.deposit_address() == DEPOSIT
    for bad in ("", C.WALLET, C.OWNER_WALLET, C.DEAD, "0x" + "0" * 40):
        monkeypatch.setattr(C, "AISURPLUS_DEPOSIT", bad)
        with pytest.raises(RuntimeError, match="WH_AISURPLUS_DEPOSIT"):
            CP.deposit_address()


def test_transfer_calldata_is_an_erc20_transfer():
    data = CP.transfer_calldata(DEPOSIT, 5.0)
    assert data.startswith("0xa9059cbb") and data[10:74] == "00" * 12 + DEPOSIT[2:] and int(data[74:], 16) == 5_000_000
    assert int(CP.transfer_calldata(DEPOSIT, 0.1234567)[74:], 16) == T.units(0.1234567)


def test_uses_compute_looks_at_both_writers(aisurplus, monkeypatch):
    assert CP.uses_compute() and CP.provider_models() == ["deepseek-v4-flash"]
    monkeypatch.setenv("WH_VOICE_MODEL", "stub")
    assert not CP.uses_compute()
    monkeypatch.setenv("WH_ADVISOR_MODEL", "aisurplus:qwen3.8-flash")
    assert CP.uses_compute() and CP.provider_models() == ["qwen3.8-flash"]
    monkeypatch.setenv("WH_ADVISOR_MODEL", "venice:llama")
    assert not CP.uses_compute()


def test_aisurplus_top_up_is_a_usdg_transfer_to_the_deposit(db, rpc, acct, live, aisurplus, monkeypatch):
    """A paid model, a low balance, free USDG in the wallet: one ERC-20 transfer to the deposit address,
    a pending ledger row at broadcast, settled from the receipt's Transfer log; then the cooldown holds."""
    from eth_abi import decode
    from fakes import auto_receipts, decode_tx
    surplus_api(monkeypatch, free=False, balance=0.25)
    monkeypatch.setenv("WH_VOICE_MODEL", "aisurplus:gpt-6-astra")
    auto_receipts(rpc, C.WALLET)
    st = CP.plan(db, acct, 20.0, True, True, budget_per_day=1.0, rpc=rpc)
    assert st["balance_usd"] == 0.25 and st["free"] is False
    assert kinds(db) == ["compute"]
    row = db.one("SELECT * FROM ledger")
    assert row["asset"] == "USDG" and row["amount"] == 5.0 and row["tx"]
    t = decode_tx(rpc.raw[-1])
    assert t["to"] == C.USDG
    to, n = decode(["address", "uint256"], t["data"][4:])
    assert to.lower() == DEPOSIT and n == 5_000_000
    assert "paid 5.00 USDG of compute to AI Surplus" in db.one("SELECT text FROM events WHERE kind='treasury'")["text"]
    CP.plan(db, acct, 20.0, True, True, budget_per_day=1.0, rpc=rpc)
    assert kinds(db) == ["compute"] and len(rpc.raw) == 1                 # the cooldown: nothing else leaves


def test_aisurplus_pays_nothing_while_its_models_are_free(db, acct, live, aisurplus, monkeypatch):
    surplus_api(monkeypatch, free=True, balance=0.0)
    monkeypatch.setattr(CP, "aisurplus_top_up", lambda *a, **k: pytest.fail("no transfer while the models are free"))
    st = CP.plan(db, acct, 20.0, True, True, budget_per_day=1.0, rpc=object())
    assert st["free"] is True and kinds(db) == []
    assert "free during the pilot" in db.one("SELECT text FROM events WHERE kind='compute'")["text"]


def test_aisurplus_demo_describes_the_transfer_and_signs_nothing(db, acct, aisurplus, monkeypatch):
    import wormhole.tx as tx
    surplus_api(monkeypatch, free=False, balance=0.0)
    monkeypatch.setenv("WH_VOICE_MODEL", "aisurplus:gpt-6-astra")
    monkeypatch.setattr(tx, "send_tx", lambda *a, **k: pytest.fail("nothing may be signed in demo"))
    CP.plan(db, acct, 20.0, True, False, budget_per_day=1.0, rpc=object())
    assert kinds(db) == []
    assert "would send $5.00 USDG to AI Surplus" in db.one("SELECT text FROM events WHERE kind='compute'")["text"]


def test_aisurplus_top_up_needs_free_usdg_and_a_deposit_address(db, rpc, acct, live, aisurplus, monkeypatch):
    surplus_api(monkeypatch, free=False, balance=0.0)
    monkeypatch.setenv("WH_VOICE_MODEL", "aisurplus:gpt-6-astra")
    CP.plan(db, acct, 4.0, True, True, budget_per_day=1.0, rpc=rpc)          # $4 free cannot fund a $5 top-up
    assert kinds(db) == [] and rpc.raw == []
    assert "free in USDG on Robinhood Chain" in db.one("SELECT text FROM events WHERE kind='compute'")["text"]
    monkeypatch.setattr(C, "AISURPLUS_DEPOSIT", "")
    CP.plan(db, acct, 20.0, True, True, budget_per_day=1.0, rpc=rpc)
    assert kinds(db) == [] and rpc.raw == []
    assert db.one("SELECT text FROM events WHERE kind='error'")["text"] == "compute top-up failed; operator review required"


@pytest.mark.parametrize("provider", ["venice", "aisurplus"])
def test_status_never_exposes_provider_exception_details(monkeypatch, acct, provider):
    monkeypatch.setattr(CP, "PROVIDER", provider)
    monkeypatch.setattr(C, "AISURPLUS_KEY", KEY)
    monkeypatch.setattr(CP, "provider_models", lambda: [])
    monkeypatch.setattr(CP, "all_free", lambda: False)

    def fail(*args, **kwargs):
        raise RuntimeError("private provider diagnostic marker")

    monkeypatch.setattr(CP, "balance", fail)
    monkeypatch.setattr(CP, "aisurplus_key", fail)
    result = CP.status(acct)
    assert result["error"] == "compute provider unavailable"
    assert "private provider" not in str(result)


def test_compute_pending_rows_are_reconciled_by_the_treasury(db, rpc, aisurplus):
    from fakes import transfer_log
    T.ensure_tables(db)
    h = "0x" + "ab" * 32
    db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)",
         (1, "compute_pending", "USDG", 5.0, h, "compute top-up on its way to AI Surplus"))
    rpc.receipts[h] = {"transactionHash": h, "status": "0x1", "blockNumber": "0x10",
                       "logs": [transfer_log(C.USDG, C.WALLET, DEPOSIT, 5_000_000)]}
    to_for = lambda r: C.AISURPLUS_DEPOSIT if r["kind"].startswith("compute") else C.OWNER_WALLET
    assert T.reconcile(rpc, db, ("compute_pending", "forward_pending"), C.WALLET, to_for) == 0
    assert kinds(db) == ["compute"]
    assert "paid 5.00 USDG of compute to AI Surplus" in db.one("SELECT text FROM events WHERE kind='treasury'")["text"]


def test_aisurplus_falls_back_once_when_the_free_lane_is_out(aisurplus, monkeypatch):
    """The open models share a weekly quota. A 429 or a dead source moves the call to the paid fallback
    once; a bad key or an empty balance does not; the answer says which model served it."""
    calls = []

    def post(url, **kw):
        calls.append(kw["json"]["model"])
        if kw["json"]["model"] == "deepseek-v4-flash":
            return Resp(429, {"error": {"type": "GoUsageLimitError", "message": "Weekly usage limit reached. Resets in 11hr"}})
        return Resp(200, {"choices": [{"message": {"content": "from luna"}}], "usage": {"total_tokens": 9}})
    monkeypatch.setattr(CP.requests, "post", post)
    text, usage = CP.aisurplus_chat("deepseek-v4-flash", [{"role": "user", "content": "hi"}], fallback="gpt-5.6-luna")
    assert text == "from luna" and usage["served_by"] == "gpt-5.6-luna" and usage["total_tokens"] == 9
    assert calls == ["deepseek-v4-flash", "gpt-5.6-luna"]
    calls.clear()
    text, usage = CP.aisurplus_chat("gpt-5.6-luna", [], fallback="gpt-5.6-luna")     # the fallback itself: one call
    assert usage["served_by"] == "gpt-5.6-luna" and calls == ["gpt-5.6-luna"]
    calls.clear()
    with pytest.raises(RuntimeError, match="429"):
        CP.aisurplus_chat("deepseek-v4-flash", [], fallback="")                         # no fallback configured
    assert calls == ["deepseek-v4-flash"]
    for code, msg in ((401, "rejected"), (402, "no compute balance")):
        calls.clear()
        monkeypatch.setattr(CP.requests, "post", lambda url, **kw: calls.append(kw["json"]["model"]) or Resp(code, {"error": "x"}))
        with pytest.raises(RuntimeError, match=msg):
            CP.aisurplus_chat("deepseek-v4-flash", [], fallback="gpt-5.6-luna")
        assert calls == ["deepseek-v4-flash"]                                           # never retried on auth or money


def test_the_fallback_counts_as_a_paid_model_in_use(aisurplus, monkeypatch):
    monkeypatch.setattr(CP, "AISURPLUS_FALLBACK", "gpt-5.6-luna")
    surplus_api(monkeypatch, free=True)
    assert CP.provider_models() == ["deepseek-v4-flash", "gpt-5.6-luna"]
    monkeypatch.setenv("WH_ADVISOR_MODEL", "aisurplus:deepseek-v4-flash")            # the same model twice is listed once
    assert CP.provider_models() == ["deepseek-v4-flash", "gpt-5.6-luna"]
    assert CP.all_free() is False and CP.status(None)["free"] is False              # so the balance is watched
    monkeypatch.setenv("WH_VOICE_MODEL", "stub")
    monkeypatch.setenv("WH_ADVISOR_MODEL", "")
    assert CP.provider_models() == [] and not CP.uses_compute()                       # nothing at the provider: no fallback either


def test_journal_label_names_the_model_that_answered(aisurplus, monkeypatch):
    from wormhole import voice
    monkeypatch.setattr(voice, "MODEL", "aisurplus:deepseek-v4-flash")
    monkeypatch.setattr(voice, "_llm", lambda *a, **k: ('{"post": "dug a lot today", "mood": "calm"}', {"served_by": "gpt-5.6-luna"}))
    text, mood, label, usage = voice.write({"x": 1})
    assert (text, mood, label) == ("dug a lot today", "calm", "aisurplus:gpt-5.6-luna")
    monkeypatch.setattr(voice, "_llm", lambda *a, **k: ('{"post": "p", "mood": "m"}', {}))
    assert voice.write({"x": 1})[2] == "aisurplus:deepseek-v4-flash"
