"""Sweep only the configured token's verified, non-buyback USDG curve into Pons escrow.

A separate sweep journal records intent before broadcast. It is never income: the later escrow
claim alone creates an allocation. Unknown/missing receipt evidence blocks new monetary work.
"""
import json
import math
import time

from eth_abi import decode

from . import config as C, claim_policy as P
from .chain import call_data, call_fn, topic

SWEPT = topic('FeesSwept(uint256,uint256,uint256)')


def ensure(db):
    db.x('CREATE TABLE IF NOT EXISTS fee_sweeps(id INTEGER PRIMARY KEY, ts INTEGER, token TEXT, curve TEXT, '
         'expected_usdg REAL, credited_usdg REAL, tx TEXT UNIQUE, state TEXT)')


def candidate(rpc):
    if not C.TOKEN:
        return None
    raw = rpc.eth_call(C.FACTORY, call_data('getLaunchedToken(address)', ('address',), (C.TOKEN,)))
    words = [raw[2+i*64:2+(i+1)*64] for i in range((len(raw)-2)//64)]
    if len(words) < 10:
        raise ValueError('factory record unavailable')
    addr = lambda i: '0x' + words[i][-40:]
    if (addr(0) != C.TOKEN.lower() or addr(2) != C.WALLET.lower()
            or addr(3) != C.WALLET.lower() or addr(4) != C.USDG or addr(1) == C.ZERO):
        raise ValueError('factory ownership or asset mismatch')
    curve = addr(1)
    # No automatic internal buyback swaps. This implementation only distributes quote fees.
    if call_fn(rpc, curve, 'graduated()', ('bool',)):
        return None
    if call_fn(rpc, curve, 'buybackEnabled()', ('bool',)):
        raise ValueError('curve buyback needs separate operator handling')
    for sig, expected in [('factory()', C.FACTORY), ('token()', C.TOKEN), ('pairToken()', C.USDG),
                          ('deployer()', C.WALLET), ('feeEscrow()', C.FEE_ESCROW)]:
        if str(call_fn(rpc, curve, sig, ('address',))).lower() != expected.lower():
            raise ValueError('curve identity mismatch')
    if call_fn(rpc, curve, 'buybackQuoteBalance()', ('uint256',)) != 0:
        raise ValueError('curve has pending buyback obligation')
    base = int(call_fn(rpc, curve, 'quoteFeeBalance()', ('uint256',)))
    tax = int(call_fn(rpc, curve, 'creatorTaxBalance()', ('uint256',)))
    share = int(call_fn(rpc, curve, 'protocolFeeShareBps()', ('uint16',)))
    if not 0 <= share <= 10000:
        raise ValueError('invalid curve fee policy')
    amount = (base - base * share // 10000 + tax) / 1e6
    return {'curve': curve, 'amount': amount} if amount > 0 else None


def settle(db, row, receipt):
    if not receipt or receipt.get('status') not in ('0x0', '0x1'):
        return False
    if receipt['status'] == '0x0':
        db.x("UPDATE fee_sweeps SET state='reverted' WHERE id=? AND state='pending'", (row['id'],))
        return True
    # The verified curve only emits this after depositing its creator amount to the escrow.
    # Missing evidence is never treated as a successful sweep or a confirmed failure.
    for event in receipt.get('logs', []):
        if event.get('address', '').lower() == row['curve'] and event.get('topics', []) == [SWEPT]:
            protocol, buyback, creator = decode(['uint256']*3, bytes.fromhex(event['data'][2:]))
            if buyback != 0:
                return False
            db.x("UPDATE fee_sweeps SET state='settled',credited_usdg=? WHERE id=? AND state='pending'",
                 (creator/1e6, row['id']))
            return True
    return False


def reconcile(rpc, db):
    ensure(db)
    for row in db.q("SELECT * FROM fee_sweeps WHERE state='pending'"):
        if not settle(db, row, rpc.call('eth_getTransactionReceipt', [row['tx']])):
            return False
    return True


def status(db, reason):
    db.meta_set('fee_sweep_status', json.dumps({'reason': reason, 'checked_at': int(time.time())}))


def cycle(rpc, db, acct, escrow):
    """Return False only for unresolved sweep state; known skips may still claim existing escrow."""
    from . import tx, treasury as T, outbox
    ensure(db)
    try:
        if not reconcile(rpc, db):
            status(db, 'waiting for sweep receipt evidence')
            return False
        if not C.LIVE or acct is None or not C.TOKEN:
            return True
        if acct.address.lower() != C.WALLET.lower() or outbox.pending():
            return False
        latest = db.one('SELECT ts FROM fee_sweeps ORDER BY id DESC LIMIT 1')
        if latest and time.time() - latest['ts'] < 3600:
            status(db, 'sweep cooldown')
            return True
        item = candidate(rpc)
        if not item:
            status(db, 'no unswept curve fees; escrow claims remain available')
            return True
        decision = P.batching(db, escrow + item['amount'], runway_days=P.funded_runway(rpc, db), balance_key='sweep_balance_since')
        if not decision['due']:
            status(db, decision['reason'])
            return True
        data = call_data('sweepFees(uint256)', ('uint256',), (0,))
        # Full contract simulation checks role eligibility and current curve phase before any signature.
        rpc.call('eth_call', [{'from': C.WALLET, 'to': item['curve'], 'data': data}, 'latest'])
        gas = int(rpc.call('eth_estimateGas', [{'from': C.WALLET, 'to': item['curve'], 'data': data}]), 16)
        price = P.eth_usd(strict=True)
        gp = int(rpc.call('eth_gasPrice', []), 16)
        if gas <= 0 or gp <= 0 or price is None or not math.isfinite(price) or price <= 0:
            raise ValueError('missing gas price')
        # Reserve room for the subsequent claim even when empty escrow cannot yet be estimated.
        claim_gas = P.claim_gas_reserve(rpc, db)
        cost = math.ceil((int(gas*1.3) + claim_gas) * int(gp*1.25) * 1.1)
        reserve = P.setting('WH_CLAIM_ETH_RESERVE', '0.0001', 0, 1)
        fraction = P.setting('WH_CLAIM_MAX_GAS_FRACTION', '0.02', 0.0001, 0.1)
        balance = int(rpc.call('eth_getBalance', [C.WALLET, 'pending']), 16)
        if cost/1e18*price > item['amount']*fraction or balance < cost + math.ceil(reserve*1e18):
            status(db, 'waiting for economical sweep gas and ETH reserve')
            return True
        def pending(h):
            db.x("INSERT INTO fee_sweeps(ts,token,curve,expected_usdg,tx,state) VALUES(?,?,?,?,?,'pending')",
                 (int(time.time()), C.TOKEN, item['curve'], item['amount'], h))
            T.watch('claim', 'moving earned curve fees into claimable escrow', h)
        h, receipt = tx.send_tx(rpc, acct, item['curve'], data, gas=int(gas*1.3), on_broadcast=pending,
                                min_remaining_eth=reserve + claim_gas*int(gp*1.25)*1.1/1e18,
                                fee_limit_eth=item['amount']*fraction/price - claim_gas*int(gp*1.25)*1.1/1e18)
        row = db.one('SELECT * FROM fee_sweeps WHERE tx=?', (h,))
        done = settle(db, row, receipt)
        if done and receipt['status']=='0x1':
            T.watch('claim', 'curve fees moved into claimable escrow', h, done=True)
        status(db, 'curve sweep confirmed' if done and receipt['status']=='0x1' else 'sweep needs receipt review')
        return done
    except Exception:
        status(db, 'sweep paused: configuration, chain data or receipt needs review')
        return not (outbox.pending() or db.one("SELECT 1 FROM fee_sweeps WHERE state='pending'"))
