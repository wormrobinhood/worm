#!/usr/bin/env python3
"""Compare what KyberSwap, LI.FI and Relay quote for one token on Robinhood Chain, both ways, for the operator.

Read-only: every quote names a dummy address, nothing is signed or sent, and no .env is read (the quotes need no
key and no wallet).

  route-compare.py TOKEN [--usd 10]

Buys `--usd` USDG worth of TOKEN (a Pons token, 18 decimals) with each provider, then sells back the smallest
amount any of them offered, so the round trips compare like for like. "best" marks the most out among the
providers the worm may execute; Relay is compared, never executed (its minimum cannot be checked in its calldata).
The token's own USDG pool, when it has one, is the fourth candidate the worm also weighs; it needs the node and
the pool key, so it is not shown here."""
import argparse
import os
import sys
import time
from pathlib import Path

os.environ.setdefault('WH_ENV_FILE', os.devnull)          # nothing here needs the operator's settings or secrets
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wormhole import config as C, route  # noqa: E402


def ask_all(token_in, token_out, amount):
    """{provider: (candidate or None, seconds, why it failed)} asked one after another."""
    out = {}
    for p in route.PROVIDERS:
        started = time.time()
        try:
            out[p] = (route.ASK[p](token_in, token_out, amount), time.time() - started, None)
        except Exception as e:
            out[p] = (None, time.time() - started, route._why(e))
    return out


def show(title, found, unit, decimals):
    top = max((c['out'] for c, _, _ in found.values() if c), default=0)
    best = max((c['out'] for c, _, _ in found.values() if c and c['executable']), default=None)
    print(title)
    for p, (c, took, why) in found.items():
        if c is None:
            print(f"  {p:<6} no quote: {why} ({took:.1f}s)")
            continue
        mark = '  best' if c['executable'] and c['out'] == best else ''
        print(f"  {p:<6} {c['out'] / 10 ** decimals:>22,.6f} {unit}  {100 * c['out'] / top:6.2f}%  fee ${c['fee_usd']:.3f}"
              f"  gas {c['gas']:>9,}  {'executable' if c['executable'] else 'quote only':<10}  {took:.1f}s  {c['to']}{mark}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('token')
    ap.add_argument('--usd', type=float, default=10.0)
    a = ap.parse_args()
    token = a.token.lower()
    if not C.valid_address(token) or not 0 < a.usd <= 10_000:
        sys.exit('a token address and 0 < --usd <= 10000, please')
    amount = int(round(a.usd * 10 ** 6))
    buys = ask_all(C.USDG, token, amount)
    show(f"buy {a.usd:g} USDG of {token}", buys, 'tokens', 18)
    got = [c['out'] for c, _, _ in buys.values() if c]
    if not got:
        sys.exit('no provider quoted a buy')
    back = ask_all(token, C.USDG, min(got))
    show(f"sell {min(got) / 1e18:,.6f} tokens back", back, 'USDG', 6)
    rt = [c['out'] / amount for c, _, _ in back.values() if c]
    if rt:
        print(f"round trip, best sell: {100 * max(rt):.2f}% of the USDG spent (before gas)")


if __name__ == '__main__':
    main()
