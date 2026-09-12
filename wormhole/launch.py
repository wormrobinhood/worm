"""Launch the worm's own token on pons V2, paired with USDG. Dry run by default.

  python -m wormhole.launch                dry run: encode, simulate with eth_call, show the predicted addresses
  python -m wormhole.launch --live         send it (needs WH_LIVE=1 and ETH in the wallet)
  python -m wormhole.launch --live --unpinned   send even if the factory's economics preview cannot be read

Everything about the token lives on-chain in the launch call: name, ticker, logo link, description,
socials, creator fee recipient (the worm's wallet), creator tax. Limits enforced by pons's deployer:
name 64, symbol 16, logo 512, description 2048, each social 256 bytes.

One launch only. The moment the node has the transaction its hash is written to .env as
WH_TOKEN_PENDING_TX; a second run refuses while that line exists, so a lost receipt can never turn
into a second $WORM. On success WH_TOKEN replaces it."""
import argparse
import os
import time

from eth_abi import decode, encode
from eth_utils import keccak

from . import config as C
from .chain import Rpc, RpcError, call_fn, selector
from .pons import TOKEN_LAUNCHED
from .wallet import account, update_env

TOKEN_PARAMS_T = "(string,string,string,string,(string,string,string,string,string),address,uint16,bool,bytes32,bytes32)"
LAUNCH_SIG = f"launchToken({TOKEN_PARAMS_T},uint256,address)"
LIMITS = {"name": 64, "symbol": 16, "logo": 512, "description": 2048, "social": 256}
GAS_FLOOR = 4_500_000        # a real launch+buy used 3.85M; the estimate (+30%) is used when it is higher

DEFAULT_DESCRIPTION = (
    "Worm is a small worm that lives on Robinhood Chain. It digs through every pons "
    "graduation, flags bad actors, and tells the community first. Of its fees, 60% go to its creator, 20% buy "
    "back and burn $WORM, and 20% pay for its own compute. It knows the dirt on every launch. It is a screening "
    "aid, not advice.")


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
        "creatorTaxBps": int(env("WH_CREATOR_TAX_BPS", "200")),
        "buybackEnabled": env("WH_BUYBACK", "0") == "1",
    }
    for k in ("name", "symbol", "logo", "description"):
        if len(p[k].encode()) > LIMITS[k]:
            raise ValueError(f"{k} is longer than {LIMITS[k]} bytes")
    for s_ in p["socials"]:
        if len(s_.encode()) > LIMITS["social"]:
            raise ValueError("a social link is longer than 256 bytes")
    return p


def economics_pin(rpc, unpinned=False):
    """The factory's previewLaunchEconomics hash: the launch reverts if the owner changes the terms
    (supply, curve fee, pool fee tier, threshold) between this read and the send. A zero pin waives
    that check, so a failed read is fatal unless --unpinned says otherwise."""
    try:
        econ = call_fn(rpc, C.FACTORY, "previewLaunchEconomics(uint256,address)", ("bytes32",),
                       ("uint256", "address"), (0, C.USDG))
    except Exception as e:
        econ = None
        print("economics ", f"preview failed: {str(e)[:120]}")
    if econ is None:
        if not unpinned:
            raise SystemExit("previewLaunchEconomics failed; refusing to launch unpinned (pass --unpinned to waive the check)")
        return b"\x00" * 32
    return econ


def calldata(p, econ, salt):
    tup = (p["name"], p["symbol"], p["logo"], p["description"], tuple(p["socials"]), p["creatorFeeRecipient"],
           p["creatorTaxBps"], p["buybackEnabled"], econ, salt)
    return selector(LAUNCH_SIG) + encode([TOKEN_PARAMS_T, "uint256", "address"], [tup, 0, C.USDG]).hex()


def encode_call(rpc, p, wallet, unpinned=False):
    econ = economics_pin(rpc, unpinned)
    salt = keccak(text=f"irl-wormhole:{p['symbol']}:{wallet}:{int(time.time())}")
    return calldata(p, econ, salt), econ, salt


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


def launched_token(rc):
    """The token address from the factory's TokenLaunched log in a receipt, or None."""
    for lg in rc.get("logs", []):
        if lg["address"].lower() == C.FACTORY and lg["topics"][0] == TOKEN_LAUNCHED.topic:
            return TOKEN_LAUNCHED.decode(lg)["token"]
    return None


def refuse_if_done(live):
    """A launch in flight or a token already set: refuse to send, explain in a dry run."""
    pending = os.environ.get("WH_TOKEN_PENDING_TX", "").strip()
    if pending:
        msg = (f"a launch is already in flight: WH_TOKEN_PENDING_TX={pending}. Check it on the explorer "
               f"(https://robinhoodchain.blockscout.com/tx/{pending}); then either set WH_TOKEN=<token> and "
               "remove the pending line from .env, or remove the line to try again.")
        if live:
            raise SystemExit(msg)
        print("NOTE      ", msg)
    if C.TOKEN:
        msg = f"WH_TOKEN={C.TOKEN} is already set: the worm has its token. Remove the line from .env to launch another."
        if live:
            raise SystemExit(msg)
        print("NOTE      ", msg)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="send the launch transaction")
    ap.add_argument("--unpinned", action="store_true", help="launch even if previewLaunchEconomics cannot be read")
    a = ap.parse_args(argv)
    refuse_if_done(a.live)
    rpc = Rpc(C.RPC)
    acct = account()
    wallet = acct.address
    p = params(wallet)
    fee = launch_fee(rpc)
    data, econ, salt = encode_call(rpc, p, wallet, a.unpinned)
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

    def note_pending(h):
        update_env({"WH_TOKEN_PENDING_TX": h})
        print("pending   ", f"WH_TOKEN_PENDING_TX={h} written to .env; it goes away once the receipt confirms")

    try:
        h, rc = send_tx(rpc, acct, C.FACTORY, data, value=fee, gas=None, gas_floor=GAS_FLOOR, say=print, on_broadcast=note_pending)
    except RpcError as e:
        raise SystemExit(f"{e}\nThe pending line stays in .env. Check the explorer; then set WH_TOKEN=<token> and remove "
                         "WH_TOKEN_PENDING_TX, or remove the line to try again.")
    token = launched_token(rc)
    if rc.get("status") != "0x1":
        update_env(remove=["WH_TOKEN_PENDING_TX"])
        raise SystemExit(f"launch reverted: https://robinhoodchain.blockscout.com/tx/{h} (fee refunded, gas spent; nothing pending)")
    if not token:
        raise SystemExit(f"mined but no TokenLaunched event: check https://robinhoodchain.blockscout.com/tx/{h}; "
                         "the pending line stays in .env until you set WH_TOKEN or remove it")
    update_env({"WH_TOKEN": token}, remove=["WH_TOKEN_PENDING_TX"])
    print("\nLAUNCHED", p["name"], f"(${p['symbol']})", "token", token)
    print("page      ", f"https://www.ponsfamily.com/launchpad/{token}")
    print("tx        ", f"https://robinhoodchain.blockscout.com/tx/{h}")


if __name__ == "__main__":
    main()
