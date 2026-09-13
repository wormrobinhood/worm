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
import logging
import os
import time

from eth_abi import decode, encode
from eth_utils import keccak

from . import config as C
from .chain import Rpc, RpcError, call_fn, selector
from .pons import TOKEN_LAUNCHED
from .wallet import account, update_env

log = logging.getLogger("wormhole.launch")
CREATE_PAGE = "https://www.ponsfamily.com/launchpad/create"

TOKEN_PARAMS_T = "(string,string,string,string,(string,string,string,string,string),address,uint16,bool,bytes32,bytes32)"
LAUNCH_SIG = f"launchToken({TOKEN_PARAMS_T},uint256,address)"
LIMITS = {"name": 64, "symbol": 16, "logo": 512, "description": 2048, "social": 256}
GAS_FLOOR = 4_500_000
PENDING_MAX_AGE_S = 3600     # a launch the node no longer knows after this long is written off        # a real launch+buy used 3.85M; the estimate (+30%) is used when it is higher

def _pct(x):
    return int(round(x * 100))


DEFAULT_DESCRIPTION = (
    "Worm is a small worm that lives on Robinhood Chain. It digs through every pons "
    f"graduation, flags bad actors, and tells the community first. Of its fees, {_pct(C.OWNER_SHARE)}% go to its creator, "
    f"{_pct(C.GOLD_SHARE)}% buy gold it keeps as a reserve, {_pct(C.BURN_SHARE)}% buy back and burn $WORM, and "
    f"{_pct(C.OPS_SHARE)}% pay for its own compute. It knows the dirt on every launch. It is a screening aid, not advice.")


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
    try:                                              # the worm may have launched from inside: its database knows
        from .db import DB
        db = DB()
        own, inflight = db.meta_get("own_token") or "", db.meta_get("launch_pending") or ""
    except Exception:
        own, inflight = "", ""
    if own or inflight:
        msg = (f"the worm already launched its token ({own})" if own
               else f"the worm's own launch is in flight ({inflight})") + ": refusing to launch another"
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


# ---- after the launch: the worm's own token, as the chain shows it ------------------------------------

def own_token(db):
    """The launches row of the worm's own token (WH_TOKEN), or None until the indexer has it."""
    if not C.TOKEN:
        return None
    try:
        return db.one("SELECT token, name, symbol, ts, tx, deployer, graduated, grad_ts FROM launches WHERE token=?", (C.TOKEN,))
    except Exception:
        return None


def announce(db):
    """Once: the log line that the worm launched its own token, written when the indexer has the launch.
    Returns True the one time it writes it."""
    row = own_token(db)
    if not row or not row["name"]:                # wait for the name: the indexer fills the metadata a moment after the launch
        return False
    if db.one("SELECT 1 FROM events WHERE kind='launch' AND token=?", (C.TOKEN,)):
        return False
    name, sym = row["name"] or "its token", row["symbol"] or "?"
    who = "" if (row["deployer"] or "").lower() == C.WALLET else " (launched by another wallet)"
    db.add_event("launch", f"launched its own token {name} (${sym}){who} · tx {row['tx'] or '?'}", C.TOKEN)
    from .treasury import watch
    watch("launch", f"launched its own token {name} (${sym}) {_ago(row['ts'])}", row["tx"], done=True)
    return True


def _ago(ts):
    d = max(0, int(time.time()) - int(ts or 0))
    if not ts:
        return ""
    return f"{d}s ago" if d < 60 else f"{d // 60}m ago" if d < 3600 else f"{d // 3600}h {d % 3600 // 60}m ago"


# ---- the launch from inside the running worm --------------------------------------------------------

def go(rpc, db, acct, say=None):
    """The command's steps, done by the worm itself so the screen and the page follow them as they happen.
    The token is kept in the database (a host's environment cannot be rewritten) and picked up from there
    at the next start. Never sends twice: a token already set, or a launch in flight, ends it."""
    from .treasury import watch
    from .tx import send_tx
    say = say or log.info
    if C.TOKEN:
        return None
    pending = db.meta_get("launch_pending") or ""
    if pending:                                       # sent before a restart: settle it from the receipt, send nothing
        rc = rpc.call("eth_getTransactionReceipt", [pending])
        if rc:
            return _settle(db, pending, rc, db.meta_get("launch_label") or "its token")
        return None
    if not C.LIVE or acct is None:
        db.add_event("launch", "a launch was asked for, but WH_LIVE=0: nothing signed, nothing sent")
        return None
    wallet = acct.address
    p = params(wallet)
    fee = launch_fee(rpc)
    data, econ, salt = encode_call(rpc, p, wallet, False)
    pred, err = dry_run(rpc, wallet, data, fee)
    if err:
        db.add_event("error", f"launch dry run failed: {err[:120]}")
        return None
    label = f"{p['name']} (${p['symbol']})"
    db.meta_set("launch_label", label)
    watch("launch", f"launching its own token {label}: signing the launch for pons", None)

    def pending_(h):
        db.meta_set("launch_pending", h)
        db.meta_set("launch_pending_ts", int(time.time()))
        watch("launch", f"launching its own token {label}: sent as {h[:12]}…, waiting for the block", h)

    try:
        h, rc = send_tx(rpc, acct, C.FACTORY, data, value=fee, gas=None, gas_floor=GAS_FLOOR, say=say, on_broadcast=pending_)
    except Exception as e:
        db.add_event("error", f"launch failed: {str(e)[:120]}")
        watch("launch", f"launch outcome needs reconciliation: {str(e)[:80]}", None, done=True)
        return None
    return _settle(db, h, rc, label)


def _settle(db, h, rc, label):
    from .treasury import watch
    token = launched_token(rc) if rc.get("status") == "0x1" else None
    if rc.get('status') == '0x1' and not token:
        db.add_event('error', f'launch receipt missing token identity; retained pending: {h}')
        return None
    if not token:
        db.meta_set('launch_pending', '')
        db.add_event("error", f"launch reverted or no TokenLaunched event: {h}")
        watch("launch", f"the launch reverted: {h[:12]}…", h, done=True)
        return None
    token = token.lower()
    db.atomic([
        ("INSERT OR REPLACE INTO meta(key,value) VALUES('own_token',?)", (token,)),
        ("INSERT OR REPLACE INTO meta(key,value) VALUES('launch_pending','')", ()),
    ])
    C.TOKEN = token
    db.add_event("launch", f"launched its own token {label} · tx {h}", token)
    watch("launch", f"launched its own token {label} just now: {h[:12]}…", h, done=True)
    return token
