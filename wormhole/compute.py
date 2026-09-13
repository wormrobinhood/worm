"""Compute: the worm buys its own inference and pays for it from its own money.

Two providers, chosen by WH_COMPUTE_PROVIDER:
- aisurplus (default): AI Surplus, an OpenAI-compatible API. The balance is read from the key's own status
  endpoint; a top-up is a plain USDG transfer on Robinhood Chain from the worm's wallet to the account's
  deposit address (WH_AISURPLUS_DEPOSIT), the same chain and token its fees arrive in, so nothing is bridged.
  The open models are free during the pilot but share one weekly quota; when it is used up the call falls
  back once to WH_AISURPLUS_FALLBACK (gpt-5.6-luna, a fraction of a cent per run), which is paid, so the
  balance is watched and topped up like any other.
- venice: Venice AI, paid in USDC on Base through x402, described below.

Venice:

- sign-in: every request carries an X-Sign-In-With-X header, a SIWE message signed by the worm's key.
- balance: GET /api/v1/x402/balance/{address}. Free to read.
- top-up: POST /api/v1/x402/top-up with an x402 "exact" USDC payment (EIP-3009 authorization signed
  by the key, relayed by Venice). Only ever signed when WH_LIVE=1; otherwise it is described, not signed.
- policy: top up WH_TOPUP_USD (5) when the balance drops under WH_TOPUP_BELOW_USD (1), if the runway
  rule allows, the Base wallet holds the USDC, and something actually spends the balance (the voice
  writer is venice:<model>, or WH_TOPUP_ALWAYS=1). At most WH_TOPUP_MAX_PER_DAY (2) top-ups a day and
  one per WH_TOPUP_COOLDOWN_S (6 h): a balance that never rises, for whatever reason, cannot drain
  the wallet. A 'compute_pending' ledger row is written before a payment leaves and counts too."""
import base64
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

import requests
from eth_abi import encode
from eth_account.messages import encode_defunct, encode_typed_data
from eth_utils import keccak

from . import config as C
from .treasury import ensure_tables, units, watch

log = logging.getLogger("wormhole.compute")
API = "https://api.venice.ai"
DOMAIN = "api.venice.ai"
BASE_CHAIN_ID = 8453
TOPUP_USD = float(os.environ.get("WH_TOPUP_USD", "5"))
TOPUP_BELOW_USD = float(os.environ.get("WH_TOPUP_BELOW_USD", "1"))
TOPUP_COOLDOWN_S = int(os.environ.get("WH_TOPUP_COOLDOWN_S", "21600"))
TOPUP_MAX_PER_DAY = int(os.environ.get("WH_TOPUP_MAX_PER_DAY", "2"))
TOPUP_ALWAYS = os.environ.get("WH_TOPUP_ALWAYS", "0") == "1"
VALID_AFTER_SKEW_S = 600          # what Venice's own x402 client uses; 60 s broke on a slightly fast clock
PROVIDER = C.COMPUTE_PROVIDER
PAYS_WITH = {"aisurplus": "USDG on Robinhood Chain", "venice": "USDC on Base"}
AISURPLUS_API = "https://aisurplus.io"
AISURPLUS_FALLBACK = os.environ.get("WH_AISURPLUS_FALLBACK", "gpt-5.6-luna").strip()   # empty: no fallback
MARKETS_TTL_S = 600
_markets = (0.0, {})


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def siwe_header(acct, url):
    now = datetime.now(timezone.utc)
    nonce = secrets.token_hex(8)
    msg = (f"{DOMAIN} wants you to sign in with your Ethereum account:\n{acct.address}\n\n"
           f"Sign in to Venice AI\n\nURI: {url}\nVersion: 1\nChain ID: {BASE_CHAIN_ID}\nNonce: {nonce}\n"
           f"Issued At: {_iso(now)}\nExpiration Time: {_iso(now + timedelta(minutes=5))}")
    sig = acct.sign_message(encode_defunct(text=msg)).signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    body = {"address": acct.address, "message": msg, "signature": sig, "timestamp": int(now.timestamp() * 1000),
            "chainId": BASE_CHAIN_ID}
    return base64.b64encode(json.dumps(body).encode()).decode()


def balance(acct):
    url = f"{API}/api/v1/x402/balance/{acct.address}"
    r = requests.get(url, headers={"X-Sign-In-With-X": siwe_header(acct, url), "Accept": "application/json"}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"venice balance {r.status_code}: {r.text[:160]}")
    return r.json().get("data", r.json())


def chat(acct, model, messages, max_tokens=400, temperature=0.8):
    """One inference call paid from the Venice balance. Returns (text, usage) or raises."""
    url = f"{API}/api/v1/chat/completions"
    r = requests.post(url, headers={"X-Sign-In-With-X": siwe_header(acct, url), "Content-Type": "application/json"},
                      json={"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature},
                      timeout=120)
    if r.status_code == 402:
        raise RuntimeError("venice: no compute balance (402)")
    if r.status_code != 200:
        raise RuntimeError(f"venice chat {r.status_code}: {r.text[:160]}")
    j = r.json()
    return j["choices"][0]["message"]["content"], j.get("usage", {})


def pick_rail(accepts):
    """The one payment option the worm will pay: x402 'exact', USDC on Base. Anything else is refused,
    never signed with the wrong domain."""
    for a in accepts or []:
        if (a.get("scheme") == "exact" and a.get("network") in (f"eip155:{BASE_CHAIN_ID}", "base")
                and str(a.get("asset", "")).lower() == C.BASE_USDC):
            if "amount" not in a:
                raise RuntimeError("payment option has no 'amount' (x402 v1 shape?); refusing")
            return a
    raise RuntimeError("no exact Base-USDC rail offered")


def payment_header(acct, requirement, amount_usd, x402_version=2, now=None, nonce=None):
    """x402 'exact' scheme: an EIP-3009 transferWithAuthorization signed for USDC on Base.
    This is a spendable instrument: only top_up calls it, and only when WH_LIVE=1."""
    value = int(round(amount_usd * 1e6))
    now = int(time.time()) if now is None else int(now)
    nonce = nonce or ("0x" + secrets.token_hex(32))
    auth = {"from": acct.address, "to": requirement["payTo"], "value": str(value), "validAfter": str(now - VALID_AFTER_SKEW_S),
            "validBefore": str(now + int(requirement.get("maxTimeoutSeconds", 300))), "nonce": nonce}
    typed = {
        "types": {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                                   {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
                  "TransferWithAuthorization": [{"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                                                {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
                                                {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}]},
        "primaryType": "TransferWithAuthorization",
        "domain": {"name": requirement.get("extra", {}).get("name", "USD Coin"),
                   "version": requirement.get("extra", {}).get("version", "2"),
                   "chainId": BASE_CHAIN_ID, "verifyingContract": requirement["asset"]},
        "message": {**auth, "value": value, "validAfter": int(auth["validAfter"]), "validBefore": int(auth["validBefore"]),
                    "nonce": bytes.fromhex(auth["nonce"][2:])},
    }
    sig = acct.sign_message(encode_typed_data(full_message=typed)).signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    payload = {"x402Version": x402_version, "scheme": "exact", "network": "base",
               "payload": {"signature": sig, "authorization": auth}}
    return base64.b64encode(json.dumps(payload).encode()).decode(), payload


def top_up(acct, amount_usd, live):
    """Returns a dict describing what happened (or what would have). Dry run: the rail and the amount
    are worked out and nothing is signed. Live needs WH_LIVE=1 as well as live=True."""
    if live and not C.LIVE:
        raise RuntimeError("WH_LIVE=0: refusing to sign a payment authorization")
    r = requests.post(f"{API}/api/v1/x402/top-up", timeout=30)
    if r.status_code != 402:
        raise RuntimeError(f"unexpected {r.status_code} from top-up requirements")
    req = r.json()
    a = pick_rail(req.get("accepts", []))
    minimum = int(a["amount"]) / 1e6
    amount = max(amount_usd, minimum)
    if not live:
        return {"sent": False, "amount_usd": amount, "payTo": a["payTo"], "rail": a,
                "note": "dry run: nothing signed, nothing sent"}
    header, payload = payment_header(acct, a, amount, req.get("x402Version", 2))
    r2 = requests.post(f"{API}/api/v1/x402/top-up", headers={"X-402-Payment": header}, timeout=60)
    if not r2.ok:
        raise RuntimeError(f"top-up failed {r2.status_code}: {r2.text[:200]}")
    return {"sent": True, "amount_usd": amount, "payTo": a["payTo"], "result": r2.json()}


# ---- AI Surplus: an OpenAI-compatible API, paid by plain USDG transfers on Robinhood Chain -----------------

def _aisurplus_headers():
    if not C.AISURPLUS_KEY:
        raise RuntimeError("WH_AISURPLUS_KEY is not set")
    return {"Authorization": f"Bearer {C.AISURPLUS_KEY}", "Accept": "application/json"}


def aisurplus_key():
    """The key's own status: the balance it can spend, whether it is active, its weekly cap."""
    r = requests.get(f"{AISURPLUS_API}/portal-api/key", headers=_aisurplus_headers(), timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"aisurplus key {r.status_code}: {r.text[:160]}")
    j = r.json()
    bal, key = j.get("balance") or {}, j.get("key") or {}
    return {"available_usd": float(bal.get("available_usd") or 0), "key_status": str(key.get("status") or "unknown"),
            "weekly_cap_usd": key.get("weekly_cap_usd"), "models": key.get("models") or []}


def aisurplus_markets():
    """Every model AI Surplus lists, by id: whether it is free and whether it is serving. No key needed; cached."""
    global _markets
    if time.time() - _markets[0] < MARKETS_TTL_S and _markets[1]:
        return _markets[1]
    r = requests.get(f"{AISURPLUS_API}/portal-api/markets", timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"aisurplus markets {r.status_code}: {r.text[:160]}")
    j = r.json()
    items = j if isinstance(j, list) else (j.get("markets") or j.get("models") or j.get("data") or [])
    out = {str(m["id"]): {"free": bool(m.get("free")), "status": str(m.get("status") or "")}
           for m in items if isinstance(m, dict) and m.get("id")}
    _markets = (time.time(), out)
    return out


def aisurplus_free(model):
    """True when the model costs nothing (the open models during the pilot), False when it is paid, None when unknown."""
    try:
        m = aisurplus_markets().get(model)
    except Exception as e:
        log.info("aisurplus markets read failed: %s", e)
        return None
    return None if m is None else m["free"]


def _aisurplus_call(model, messages, max_tokens, temperature):
    r = requests.post(f"{AISURPLUS_API}/v1/chat/completions", headers={**_aisurplus_headers(), "Content-Type": "application/json"},
                      json={"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}, timeout=120)
    if r.status_code == 401:
        raise RuntimeError("aisurplus: the key was rejected (401)")
    if r.status_code == 402:
        raise RuntimeError("aisurplus: no compute balance (402)")
    if r.status_code != 200:
        raise RuntimeError(f"aisurplus chat {r.status_code}: {r.text[:160]}")
    j = r.json()
    usage = dict(j.get("usage") or {})
    usage["served_by"] = model
    return j["choices"][0]["message"]["content"], usage


def _lane_trouble(err):
    """A model that is out of quota or whose source is down, as opposed to a bad key or an empty balance."""
    t = str(err)
    return any(m in t for m in ("429", "503", "usage limit", "upstream_error", "no_source"))


def aisurplus_chat(model, messages, max_tokens=400, temperature=0.8, fallback=None):
    """One inference call at AI Surplus. The free open lane shares one weekly quota; when that is used up,
    or the lane is down, the call is retried once on the fallback model. Returns (text, usage); the usage
    carries served_by, the model that actually answered."""
    fallback = AISURPLUS_FALLBACK if fallback is None else fallback
    try:
        return _aisurplus_call(model, messages, max_tokens, temperature)
    except RuntimeError as e:
        if fallback and fallback != model and _lane_trouble(e):
            log.info("aisurplus %s unavailable (%s); trying %s", model, str(e)[:80], fallback)
            return _aisurplus_call(fallback, messages, max_tokens, temperature)
        raise


def deposit_address():
    """The AI Surplus deposit address, checked: set, and not one of the worm's own, the creator's or the burn address."""
    to = C.AISURPLUS_DEPOSIT
    if not to:
        raise RuntimeError("WH_AISURPLUS_DEPOSIT is not set")
    if to in (C.WALLET, C.OWNER_WALLET, C.DEAD, "0x" + "0" * 40):
        raise RuntimeError("WH_AISURPLUS_DEPOSIT must be the deposit address AI Surplus shows, not a wallet of the worm's or the creator's")
    return to


def transfer_calldata(to, amount_usd):
    """ERC-20 transfer(to, amount) of USDG, six decimals, rounded the way the ledger rounds."""
    return "0x" + keccak(text="transfer(address,uint256)")[:4].hex() + encode(["address", "uint256"], [to, units(amount_usd)]).hex()


def aisurplus_top_up(rpc, db, acct, amount_usd, live):
    """Send USDG from the worm's wallet on Robinhood Chain to the account's deposit address. Dry run: the
    transfer is described and nothing is signed. Live needs WH_LIVE=1 as well as live=True. The
    'compute_pending' ledger row is written at broadcast and settled from the receipt, like a forward."""
    to = deposit_address()
    if not live:
        return {"sent": False, "amount_usd": amount_usd, "payTo": to, "note": "dry run: nothing signed, nothing sent"}
    if not C.LIVE:
        raise RuntimeError("WH_LIVE=0: refusing to sign a transfer")
    from .treasury import finish
    from .tx import send_tx

    def pending(h):
        db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)",
             (int(time.time()), "compute_pending", "USDG", amount_usd, h, "compute top-up on its way to AI Surplus"))
        watch("compute", f"paying {amount_usd:.2f} USDG of compute to AI Surplus", h)

    h, rc = send_tx(rpc, acct, C.USDG, transfer_calldata(to, amount_usd), on_broadcast=pending)
    kind, amount = finish(db, h, rc, C.WALLET, to=to)
    if kind != "compute":
        raise RuntimeError(f"the top-up transfer did not settle ({kind}): {h}")
    return {"sent": True, "amount_usd": amount, "payTo": to, "tx": h}


# ---- what both providers share ----------------------------------------------------------------------

def spending_models():
    """The writers configured: the journal's and the advisor's, as '<provider>:<model>' strings."""
    return [m.strip() for m in (os.environ.get("WH_VOICE_MODEL", "stub"), os.environ.get("WH_ADVISOR_MODEL", "")) if m.strip()]


def provider_models():
    """The configured models that run at the current provider, without the prefix; at AI Surplus the
    fallback counts too, since it spends the balance whenever the free lane is out."""
    out = list(dict.fromkeys(m.partition(":")[2] for m in spending_models() if m.startswith(PROVIDER + ":")))
    if out and PROVIDER == "aisurplus" and AISURPLUS_FALLBACK and AISURPLUS_FALLBACK not in out:
        out.append(AISURPLUS_FALLBACK)
    return out


def uses_compute():
    """True when something spends the balance: a writer runs at the provider (or WH_TOPUP_ALWAYS=1)."""
    return TOPUP_ALWAYS or bool(provider_models())


uses_venice = uses_compute      # the older name


def all_free():
    """True when every model in use at AI Surplus is known to be free (the pilot): nothing to pay for."""
    models = provider_models()
    return bool(models) and all(aisurplus_free(m) is True for m in models)


def status(acct, wallet_usd=None):
    """What the page and the plan see: the provider, the balance it can spend, what a top-up costs and needs.
    wallet_usd: what the wallet could put into a top-up (free USDG here for AI Surplus, USDC on Base for Venice)."""
    out = {"provider": PROVIDER, "pays_with": PAYS_WITH[PROVIDER], "balance_usd": None, "wallet_usd": wallet_usd,
           "topup_usd": TOPUP_USD, "topup_below_usd": TOPUP_BELOW_USD, "error": None}
    if PROVIDER == "aisurplus":
        out["models"] = provider_models()
        out["free"] = all_free()
        out["deposit"] = C.AISURPLUS_DEPOSIT or None
        if not C.AISURPLUS_KEY:
            out["error"] = "no key yet"
            return out
        try:
            k = aisurplus_key()
            out.update(balance_usd=k["available_usd"], key_status=k["key_status"], weekly_cap_usd=k["weekly_cap_usd"])
        except Exception as e:
            out["error"] = str(e)[:120]
        return out
    out.update(minimum_topup_usd=None, suggested_topup_usd=None, usdc_base=wallet_usd)
    if not acct:
        out["error"] = "no wallet"
        return out
    try:
        b = balance(acct)
        out["balance_usd"] = float(b.get("balanceUsd", b.get("balance", 0)) or 0)
        out["minimum_topup_usd"] = b.get("minimumTopUpUsd")
        out["suggested_topup_usd"] = b.get("suggestedTopUpUsd")
    except Exception as e:
        out["error"] = str(e)[:120]
    return out


def _say_hourly(db, text):
    """Say it once an hour, not every cycle."""
    last = db.one("SELECT ts FROM events WHERE kind='compute' ORDER BY id DESC LIMIT 1")
    if not last or time.time() - last["ts"] > 3600:
        db.add_event("compute", text)


def topups_today(db, now):
    """Top-ups (done or in flight) in the last 24 h: their count and the time of the latest."""
    ensure_tables(db)
    r = db.one("SELECT COUNT(*) n, MAX(ts) t FROM ledger WHERE kind IN ('compute','compute_pending') AND ts>=?",
               (now - 86400,))
    return (r["n"], r["t"]) if r else (0, None)


def plan(db, acct, wallet_usd, runway_ok, live, budget_per_day=None, rpc=None):
    """Top up when low. Live: signs and sends. Demo: writes what it would do.
    wallet_usd: what the wallet could put into a top-up (free USDG on Robinhood Chain for AI Surplus, USDC on
    Base for Venice). budget_per_day: the runway's compute budget (the operations share of measured income);
    a month of it must cover one top-up. rpc: the Robinhood Chain node, needed to send a USDG transfer."""
    st = status(acct, wallet_usd)
    if st["balance_usd"] is None or st["balance_usd"] >= TOPUP_BELOW_USD:
        return st
    low = f"compute balance ${st['balance_usd']:.2f}"
    if not uses_compute():
        _say_hourly(db, f"{low} is low but nothing spends it (no writer runs at {PROVIDER}); no top-up")
        return st
    if PROVIDER == "aisurplus" and not TOPUP_ALWAYS and st.get("free"):
        _say_hourly(db, f"{low} but every model in use is free during the pilot; nothing to pay")
        return st
    if not runway_ok:
        _say_hourly(db, f"{low} is low but the runway rule blocks a top-up")
        return st
    if budget_per_day is not None and budget_per_day * 30 < TOPUP_USD:
        _say_hourly(db, f"{low} is low but the compute budget (${budget_per_day:.2f}/day, the operations share of income) does not cover a ${TOPUP_USD:.0f} top-up")
        return st
    if (wallet_usd or 0) < TOPUP_USD:
        _say_hourly(db, f"{low}: would top up ${TOPUP_USD:.0f} but the wallet has ${wallet_usd or 0:.2f} free in {st['pays_with']}")
        return st
    now = int(time.time())
    n, last = topups_today(db, now)
    if n >= TOPUP_MAX_PER_DAY or (last and now - last < TOPUP_COOLDOWN_S):
        _say_hourly(db, f"{low}: top-up wanted but the cooldown ({TOPUP_COOLDOWN_S // 3600} h) or the daily cap ({TOPUP_MAX_PER_DAY}) blocks it")
        return st
    if PROVIDER == "aisurplus":            # the pending row is written at broadcast and settled from the receipt
        try:
            r = aisurplus_top_up(rpc, db, acct, TOPUP_USD, live)
            if not r["sent"]:
                _say_hourly(db, f"demo: would send ${r['amount_usd']:.2f} USDG to AI Surplus for compute (nothing signed)")
        except Exception as e:
            db.add_event("error", f"compute top-up failed: {str(e)[:120]}")
        return st
    if live:                                # the row exists before any payment leaves; it stays if the call dies half way
        db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)",
             (now, "compute_pending", "USDC", TOPUP_USD, None, "top-up in flight"))
    try:
        r = top_up(acct, TOPUP_USD, live)
        if r["sent"]:
            db.x("UPDATE ledger SET kind='compute', amount=?, note=? WHERE kind='compute_pending' AND ts=?",
                 (r["amount_usd"], "topped up the Venice compute balance", now))
            db.add_event("compute", f"topped up ${r['amount_usd']:.2f} of compute at Venice")
        else:
            _say_hourly(db, f"demo: would top up ${r['amount_usd']:.2f} of compute at Venice (nothing signed)")
    except Exception as e:
        db.add_event("error", f"compute top-up failed: {str(e)[:120]}")
    return st
