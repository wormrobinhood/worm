"""Launch the worm's own token on pons V2, paired with USDG. Dry run by default.

  python -m wormhole.launch            dry run: encode, simulate with eth_call, show the predicted addresses
  python -m wormhole.launch --live     send it (needs WH_LIVE=1 and ETH in the wallet)

Everything about the token lives on-chain in the launch call: name, ticker, logo link, description,
socials, creator fee recipient (the worm's wallet), creator tax. Limits enforced by pons's deployer:
name 64, symbol 16, logo 512, description 2048, each social 256 bytes."""
import argparse
import json
import os
import time

from eth_abi import decode, encode
from eth_utils import keccak

from . import config as C
from .chain import Rpc, call_data, call_fn, selector
from .pons import TOKEN_LAUNCHED
from .wallet import account

TOKEN_PARAMS_T = "(string,string,string,string,(string,string,string,string,string),address,uint16,bool,bytes32,bytes32)"
LAUNCH_SIG = f"launchToken({TOKEN_PARAMS_T},uint256,address)"
LIMITS = {"name": 64, "symbol": 16, "logo": 512, "description": 2048, "social": 256}

DEFAULT_DESCRIPTION = (
    "Worm is a small hooded worm that lives at IRL Wormhole on Robinhood Chain. It digs through every pons "
    "graduation, flags bad actors, and tells the community first. Its fees pay for its own compute; 20% of what "
    "it earns goes to its creator. Internet money, real life impact. It is a screening aid, not advice.")


def params(wallet):
    env = os.environ.get
    p = {
        "name": env("WH_TOKEN_NAME", "Worm"),
        "symbol": env("WH_TOKEN_SYMBOL", "WORM"),
        "logo": env("WH_TOKEN_LOGO", f"{C.SITE_URL}/logo.png"),
        "description": env("WH_TOKEN_DESCRIPTION", DEFAULT_DESCRIPTION),
        "socials": (env("WH_TOKEN_X", ""), env("WH_TOKEN_TELEGRAM", ""), env("WH_TOKEN_DISCORD", ""),
                    env("WH_TOKEN_WEBSITE", C.SITE_URL), env("WH_TOKEN_FARCASTER", "")),
        "creatorFeeRecipient": wallet,
        "creatorTaxBps": int(env("WH_CREATOR_TAX_BPS", "100")),
        "buybackEnabled": env("WH_BUYBACK", "0") == "1",
    }
    for k in ("name", "symbol", "logo", "description"):
        if len(p[k].encode()) > LIMITS[k]:
            raise ValueError(f"{k} is longer than {LIMITS[k]} bytes")
    for s_ in p["socials"]:
        if len(s_.encode()) > LIMITS["social"]:
            raise ValueError("a social link is longer than 256 bytes")
    return p


def encode_call(rpc, p, wallet):
    try:
        econ = call_fn(rpc, C.FACTORY, "previewLaunchEconomics(uint256,address)", ("bytes32",),
                       ("uint256", "address"), (0, C.USDG))
    except Exception:
        econ = None
    econ = econ or b"\x00" * 32
    salt = keccak(text=f"irl-wormhole:{p['symbol']}:{wallet}:{int(time.time())}")
    tup = (p["name"], p["symbol"], p["logo"], p["description"], tuple(p["socials"]), p["creatorFeeRecipient"],
           p["creatorTaxBps"], p["buybackEnabled"], econ, salt)
    data = selector(LAUNCH_SIG) + encode([TOKEN_PARAMS_T, "uint256", "address"], [tup, 0, C.USDG]).hex()
    return data, econ, salt


def launch_fee(rpc):
    return int(call_fn(rpc, C.FACTORY, "launchFee()", ("uint256",)))


def dry_run(rpc, wallet, data, fee):
    """Simulate with a pretend balance so the check works before the wallet is funded."""
    call = {"from": wallet, "to": C.FACTORY, "value": hex(fee), "data": data}
    override = {wallet: {"balance": hex(10 ** 18)}}
    try:
        raw = rpc.call("eth_call", [call, "latest", override])
    except Exception as e:
        return None, f"simulation failed: {str(e)[:300]}"
    token, curve = decode(["address", "address"], bytes.fromhex(raw[2:]))
    return (token, curve), None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="send the launch transaction")
    a = ap.parse_args()
    rpc = Rpc(C.RPC)
    acct = account()
    wallet = acct.address
    p = params(wallet)
    fee = launch_fee(rpc)
    data, econ, salt = encode_call(rpc, p, wallet)
    print("token     ", p["name"], f"(${p['symbol']})")
    print("logo      ", p["logo"])
    print("website   ", p["socials"][3], "| x:", p["socials"][0] or "-", "| telegram:", p["socials"][1] or "-")
    print("pair      ", "USDG", C.USDG)
    print("creator   ", wallet, f"| creator tax {p['creatorTaxBps'] / 100:.2f}% | buyback {'on' if p['buybackEnabled'] else 'off'}")
    print("fee       ", fee / 1e18, "ETH | economics pin", econ.hex()[:18] + "…", "| salt", salt.hex()[:18] + "…")
    print("calldata  ", len(data) // 2, "bytes")
    pred, err = dry_run(rpc, wallet, data, fee)
    if err:
        print("dry run   ", err)
    else:
        print("dry run    OK: token", pred[0], "curve", pred[1])
        print("page      ", f"https://www.ponsfamily.com/launchpad/{pred[0]}")
    if not a.live:
        print("\nnothing sent (dry run). Add --live with WH_LIVE=1 and ETH in the wallet to launch.")
        return
    if err:
        raise SystemExit("refusing to send: the dry run failed")
    from .tx import send_tx
    h, rc = send_tx(rpc, acct, C.FACTORY, data, value=fee, gas=4_500_000, say=print)
    token = None
    for lg in rc.get("logs", []):
        if lg["address"].lower() == C.FACTORY and lg["topics"][0] == TOKEN_LAUNCHED.topic:
            token = TOKEN_LAUNCHED.decode(lg)["token"]
    if rc.get("status") != "0x1" or not token:
        raise SystemExit("launch reverted or no TokenLaunched event; check the tx on the explorer")
    with open(C.ROOT / ".env", "a") as f:
        f.write(f"WH_TOKEN={token}\n")
    print("\nLAUNCHED", p["name"], f"(${p['symbol']})", "token", token)
    print("page      ", f"https://www.ponsfamily.com/launchpad/{token}")
    print("tx        ", f"https://robinhoodchain.blockscout.com/tx/{h}")


if __name__ == "__main__":
    main()
