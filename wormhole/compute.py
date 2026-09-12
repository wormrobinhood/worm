"""Compute: the worm pays Venice for inference from a prepaid USDC balance on Base.

- sign-in: every request carries an X-Sign-In-With-X header, a SIWE message signed by the worm's key.
- balance: GET /api/v1/x402/balance/{address}. Free to read.
- top-up: POST /api/v1/x402/top-up with an x402 "exact" USDC payment (EIP-3009 authorization signed
  by the key, relayed by Venice). Only ever sent when WH_LIVE=1; otherwise it is described, not sent.
- policy: top up WH_TOPUP_USD (5) when the balance drops under WH_TOPUP_BELOW_USD (1), if the runway
  rule allows and the Base wallet holds the USDC."""
import base64
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

import requests
from eth_account.messages import encode_defunct, encode_typed_data

from . import config as C

log = logging.getLogger("wormhole.compute")
API = "https://api.venice.ai"
DOMAIN = "api.venice.ai"
BASE_CHAIN_ID = 8453
TOPUP_USD = float(__import__("os").environ.get("WH_TOPUP_USD", "5"))
TOPUP_BELOW_USD = float(__import__("os").environ.get("WH_TOPUP_BELOW_USD", "1"))


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


def payment_header(acct, requirement, amount_usd, x402_version=2):
    """x402 'exact' scheme: an EIP-3009 transferWithAuthorization signed for USDC on Base."""
    value = int(round(amount_usd * 1e6))
    now = int(time.time())
    auth = {"from": acct.address, "to": requirement["payTo"], "value": str(value), "validAfter": str(now - 60),
            "validBefore": str(now + int(requirement.get("maxTimeoutSeconds", 300))), "nonce": "0x" + secrets.token_hex(32)}
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
    """Returns a dict describing what happened (or what would have)."""
    r = requests.post(f"{API}/api/v1/x402/top-up", timeout=30)
    if r.status_code != 402:
        raise RuntimeError(f"unexpected {r.status_code} from top-up requirements")
    req = r.json()
    accepts = [a for a in req.get("accepts", []) if a.get("network") == f"eip155:{BASE_CHAIN_ID}"] or req.get("accepts", [])
    if not accepts:
        raise RuntimeError("no payment options offered")
    a = accepts[0]
    minimum = int(a["amount"]) / 1e6
    amount = max(amount_usd, minimum)
    header, payload = payment_header(acct, a, amount, req.get("x402Version", 2))
    if not live:
        return {"sent": False, "amount_usd": amount, "payTo": a["payTo"], "note": "dry run: authorization signed, not sent"}
    r2 = requests.post(f"{API}/api/v1/x402/top-up", headers={"X-402-Payment": header}, timeout=60)
    if not r2.ok:
        raise RuntimeError(f"top-up failed {r2.status_code}: {r2.text[:200]}")
    return {"sent": True, "amount_usd": amount, "payTo": a["payTo"], "result": r2.json()}


def status(acct, usdc_base=None):
    out = {"provider": "venice", "balance_usd": None, "minimum_topup_usd": None, "suggested_topup_usd": None,
           "usdc_base": usdc_base, "topup_usd": TOPUP_USD, "topup_below_usd": TOPUP_BELOW_USD, "error": None}
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


def plan(db, acct, usdc_base, runway_ok, live):
    """Top up when low. Live: sends the authorization. Demo: writes what it would do."""
    st = status(acct, usdc_base)
    if st["balance_usd"] is None or st["balance_usd"] >= TOPUP_BELOW_USD:
        return st
    if not runway_ok:
        db.add_event("compute", f"compute balance ${st['balance_usd']:.2f} is low but the runway rule blocks a top-up")
        return st
    if (usdc_base or 0) < TOPUP_USD:
        last = db.one("SELECT ts FROM events WHERE kind='compute' ORDER BY id DESC LIMIT 1")
        if not last or time.time() - last["ts"] > 3600:        # say it once an hour, not every cycle
            db.add_event("compute", f"compute balance ${st['balance_usd']:.2f}: would top up ${TOPUP_USD:.0f} but the Base wallet holds ${usdc_base or 0:.2f} USDC")
        return st
    try:
        r = top_up(acct, TOPUP_USD, live)
        if r["sent"]:
            db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)",
                 (int(time.time()), "compute", "USDC", r["amount_usd"], None, "topped up the Venice compute balance"))
            db.add_event("compute", f"topped up ${r['amount_usd']:.2f} of compute at Venice")
        else:
            db.add_event("compute", f"demo: would top up ${r['amount_usd']:.2f} of compute at Venice (authorization signed, not sent)")
    except Exception as e:
        db.add_event("error", f"compute top-up failed: {str(e)[:120]}")
    return st
