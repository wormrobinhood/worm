"""The worm's own keys.

  python -m wormhole.wallet new     create the key, write it to .env (never printed), show the address
  python -m wormhole.wallet show    address and balances on Robinhood Chain and Base

One secp256k1 key serves both chains: the same address holds ETH and USDG on Robinhood Chain
(launch fee, gas, creator fees) and USDC on Base (compute). Never paste the secret anywhere."""
import os
import secrets
import sys

from eth_account import Account

from . import config as C
from .chain import Rpc, call_data, decode_result

ENV = C.ROOT / ".env"


def account():
    if not C.SECRET:
        sys.exit("no wallet yet: run  python -m wormhole.wallet new")
    return Account.from_key(C.SECRET)


def new():
    if C.SECRET:
        print("WH_SECRET is already set; refusing to overwrite. Address:", account().address)
        return
    key = "0x" + secrets.token_hex(32)
    acct = Account.from_key(key)
    existed = ENV.exists()
    with open(ENV, "a") as f:
        if not existed:
            f.write("# IRL Worm secrets. Never commit this file.\n")
        f.write(f"WH_SECRET={key}\n")
    os.chmod(ENV, 0o600)
    print("wallet created. Address (same on Robinhood Chain and Base):")
    print("  ", acct.address)
    print("Fund it with ETH on Robinhood Chain for the launch (0.0005 ETH fee + gas) and USDC on Base for compute.")


def balances(addr):
    out = {}
    try:
        rh = Rpc(C.RPC, timeout=30)
        out["eth_rh"] = int(rh.call("eth_getBalance", [addr, "latest"]), 16) / 1e18
        raw = rh.eth_call(C.USDG, call_data("balanceOf(address)", ("address",), (addr,)))
        out["usdg_rh"] = (decode_result(raw, ("uint256",)) or 0) / 1e6
    except Exception as e:
        out["rh_error"] = str(e)[:80]
    try:
        base = Rpc(C.BASE_RPC, timeout=30)
        out["eth_base"] = int(base.call("eth_getBalance", [addr, "latest"]), 16) / 1e18
        raw = base.eth_call(C.BASE_USDC, call_data("balanceOf(address)", ("address",), (addr,)))
        out["usdc_base"] = (decode_result(raw, ("uint256",)) or 0) / 1e6
    except Exception as e:
        out["base_error"] = str(e)[:80]
    return out


def show():
    acct = account()
    print("address  ", acct.address)
    for k, v in balances(acct.address).items():
        print(f"  {k:10} {v}")
    print("live     ", "ARMED (WH_LIVE=1)" if C.LIVE else "no (WH_LIVE=0): nothing will be signed")
    print("owner    ", C.OWNER_WALLET or "(WH_OWNER_WALLET not set: the 20% forward is off)")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    {"new": new, "show": show}.get(cmd, show)()
