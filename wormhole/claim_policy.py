"""Batch escrow claims and reject claims whose gas cost or remaining ETH is unsafe."""
import math
import os
import time

from . import config as C
from .chain import call_data
from .prices import eth_usd


def setting(name, default, low, high):
    value = float(os.environ.get(name, default))
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError('invalid claim policy setting: ' + name)
    return value


def funded_runway(rpc, db):
    """Cash coverage, without assuming future income or selling gold/ETH for operating bills."""
    from . import budget, treasury
    parts = (budget.COMPUTE_USD_DAY, budget.GAS_USD_DAY, budget.BRIDGE_USD_MONTH / 30)
    if any(not math.isfinite(v) or v < 0 for v in parts) or sum(parts) <= 0:
        return None
    if db.one("SELECT 1 FROM ledger WHERE kind LIKE '%_pending'"):
        return None
    cash = treasury.usdg_balance(rpc, C.WALLET)
    free = max(0.0, cash - treasury.owed_total(db))
    if not math.isfinite(free):
        return None
    return free / sum(parts)


def batching(db, amount, now=None, runway_days=None, balance_key="claim_balance_since"):
    now = int(time.time()) if now is None else int(now)
    minimum = setting('WH_CLAIM_MIN_USD', os.environ.get('WH_MIN_CLAIM_USD', '5'), 1, 100)
    maximum = setting('WH_CLAIM_MAX_USD', '100', minimum, 1000)
    interval = setting('WH_CLAIM_MAX_WAIT_HOURS', '24', 1, 168) * 3600
    target_hours = setting('WH_CLAIM_BATCH_HOURS', '6', 1, 24)
    floor = setting('WH_CLAIM_DUST_USD', '1', 0.01, minimum)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError('invalid escrow balance')
    # Start the wait when a worthwhile balance is first observed, never from an old claim.
    first = int(db.meta_get(balance_key) or 0)
    if amount < floor:
        db.meta_set(balance_key, '')
        first = 0
    elif not first:
        first = now
        db.meta_set(balance_key, str(first))
    # Observe at least a day before extrapolating a rate. Include the current unclaimed balance
    # so a high-income account need not claim small amounts before its target can grow.
    started = int(db.meta_get('claim_policy_since') or 0)
    if not started:
        started = now
        db.meta_set('claim_policy_since', str(started))
    earned = db.one("SELECT COALESCE(SUM(amount),0) n FROM ledger WHERE kind='claim' AND ts>=?", (now - 7 * 86400,))['n']
    days = max(1.0, min(7.0, (now - started) / 86400))
    threshold = min(maximum, max(minimum, (earned + amount) / days * target_hours / 24))
    funded = runway_days is not None and math.isfinite(runway_days) and runway_days >= 90
    if funded:
        since = int(db.meta_get('claim_funded_since') or 0)
        if not since:
            since = now
            db.meta_set('claim_funded_since', str(since))
        last = db.one("SELECT MAX(ts) ts FROM ledger WHERE kind='claim'")['ts']
        anchor = int(last) if last is not None else since
        # Funded, there is no hurry: once a day. A large balance is the exception, so it does not
        # sit in escrow for a day and its burn and gold purchases arrive as smaller swaps.
        large = setting('WH_CLAIM_LARGE_USD', '100', minimum, 1000000)
        spacing = setting('WH_CLAIM_LARGE_EVERY_HOURS', '2', 1, 24) * 3600
        waited = now - anchor
        early = amount >= large and spacing <= waited < 86400
        due = amount >= minimum and (waited >= 86400 or early)
        holding = amount >= large and waited < spacing
        return {'threshold_usdg': minimum, 'large_usdg': large, 'balance_since': first or None, 'max_wait_hours': 24,
                'mode': 'funded_daily', 'funded_runway_days': round(runway_days, 1),
                'next_claim_after': anchor + (spacing if amount >= large else 86400), 'due': due,
                'reason': 'large balance claim due' if early else 'daily claim due' if due
                          else 'large balance: spacing claims %g hours apart' % (spacing / 3600) if holding
                          else 'daily interval: 90 days funded' if waited < 86400 else 'accumulating daily minimum'}
    db.meta_set('claim_funded_since', '')
    timed_out = bool(first and now - first >= interval and amount >= floor)
    return {'mode': 'adaptive', 'threshold_usdg': round(threshold, 6), 'balance_since': first or None,
            'max_wait_hours': interval / 3600, 'due': amount >= threshold or timed_out,
            'reason': 'maximum wait reached' if timed_out else 'batch threshold reached' if amount >= threshold else 'accumulating fees'}


def evaluate(rpc, db, amount, now=None):
    """Fail closed on missing estimates/prices; the maximum wait never bypasses gas safety."""
    try:
        result = batching(db, float(amount), now, runway_days=funded_runway(rpc, db))
        if not result['due']:
            return dict(result, allowed=False)
        price = eth_usd(strict=True)
        if price is None or not math.isfinite(float(price)) or price <= 0:
            return dict(result, allowed=False, reason='waiting for a fresh ETH price')
        payload = call_data('claimToken(address)', ('address',), (C.USDG,))
        gas = int(rpc.call('eth_estimateGas', [{'from': C.WALLET, 'to': C.FEE_ESCROW, 'data': payload}]), 16)
        gas_price = int(rpc.call('eth_gasPrice', []), 16)
        if gas <= 0 or gas_price <= 0:
            raise ValueError('invalid gas estimate')
        # Match sender padding and add a further margin for movement between reads.
        cost_wei = math.ceil(int(gas * 1.3) * int(gas_price * 1.25) * 1.1)
        cost_usd = cost_wei / 1e18 * float(price)
        fraction = setting('WH_CLAIM_MAX_GAS_FRACTION', '0.02', 0.0001, 0.1)
        reserve = setting('WH_CLAIM_ETH_RESERVE', '0.0001', 0, 1)
        balance = int(rpc.call('eth_getBalance', [C.WALLET, 'pending']), 16)
        result.update(estimated_gas_usd=round(cost_usd, 8), reserve_eth=reserve)
        if cost_usd > amount * fraction:
            return dict(result, allowed=False, reason='claim gas exceeds cost limit')
        if balance < cost_wei + math.ceil(reserve * 1e18):
            return dict(result, allowed=False, reason='waiting for ETH gas reserve')
        return dict(result, allowed=True)
    except Exception:
        return {'allowed': False, 'reason': 'claim policy or gas data unavailable'}


def remember(db, result):
    import json
    db.meta_set('claim_policy_status', json.dumps(result))


def claim_gas_reserve(rpc, db):
    """Use a verified recent claim for this signer, doubled and padded; otherwise reserve 260k.

    Empty escrow cannot simulate claimToken. A 150k floor covers first-transfer storage costs;
    the actual claim is still independently estimated and checked immediately before signing.
    """
    fallback = 260000
    try:
        row = db.one("SELECT tx FROM ledger WHERE kind='claim' AND ts>=? AND tx IS NOT NULL ORDER BY id DESC LIMIT 1", (time.time()-7*86400,))
        if not row:
            return fallback
        transaction = rpc.call('eth_getTransactionByHash', [row['tx']]) or {}
        receipt = rpc.call('eth_getTransactionReceipt', [row['tx']]) or {}
        expected = call_data('claimToken(address)', ('address',), (C.USDG,))
        if (transaction.get('from','').lower() != C.WALLET or transaction.get('to','').lower() != C.FEE_ESCROW
                or transaction.get('input','').lower() != expected.lower() or receipt.get('status') != '0x1'):
            return fallback
        used = int(receipt['gasUsed'], 16)
        return max(150000, math.ceil(used * 2.6)) if used > 0 else fallback
    except Exception:
        return fallback
