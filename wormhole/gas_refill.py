"""Bounded operations-funded USDG -> native ETH, with atomic swap/unwrap and receipt recovery."""
import json
import math
import os
import time

from eth_abi import decode, encode

from . import finality
from . import config as C, outbox, treasury as T, tx
from .chain import call_data, call_fn, selector, topic
from .claim_policy import setting
from .prices import eth_usd

WITHDRAWAL = topic('Withdrawal(address,uint256)')
POOL_FEE = 100


def ensure(db):
    db.x('CREATE TABLE IF NOT EXISTS gas_refills(id INTEGER PRIMARY KEY, ts INTEGER, amount INTEGER, '
         'minimum TEXT, received TEXT, tx TEXT UNIQUE, state TEXT)')


def status(db, reason):
    db.meta_set('gas_refill_status', json.dumps({'reason': reason, 'checked_at': int(time.time())}))


def pending(db):
    ensure(db)
    return db.one("SELECT 1 FROM gas_refills WHERE state='pending'") is not None


def quote(rpc, amount):
    data = selector(f'quoteExactInputSingle({T.QUOTE_V3_T})') + encode(
        [T.QUOTE_V3_T], [(C.USDG, C.WETH, amount, POOL_FEE, 0)]).hex()
    return int(decode(['uint256', 'uint160', 'uint32', 'uint256'], bytes.fromhex(rpc.eth_call(C.QUOTER_V3, data)[2:]))[0])


def calldata(amount, minimum, deadline):
    swap = selector(f'exactInputSingle({T.SWAP_V3_T})') + encode([T.SWAP_V3_T],
        [(C.USDG, C.WETH, POOL_FEE, C.SWAP_ROUTER_V3, amount, minimum, 0)]).hex()
    unwrap = call_data('unwrapWETH9(uint256,address)', ('uint256', 'address'), (minimum, C.WALLET))
    return call_data('multicall(uint256,bytes[])', ('uint256', 'bytes[]'),
                     (deadline, [bytes.fromhex(swap[2:]), bytes.fromhex(unwrap[2:])]))


def settle(db, row, rc):
    if not rc or rc.get('status') not in ('0x0', '0x1'):
        return False
    if rc['status'] == '0x0':
        db.x("UPDATE gas_refills SET state='reverted' WHERE id=? AND state='pending'", (row['id'],))
        return True
    spent = 0
    received = 0
    burned = 0
    for ev in rc.get('logs', []):
        topics = ev.get('topics', [])
        address = ev.get('address', '').lower()
        if address == C.USDG and len(topics) == 3 and topics[0] == T.TRANSFER_TOPIC:
            if ('0x' + topics[1][-40:]).lower() == C.WALLET:
                spent += int(ev['data'], 16)
        # The deployed WETH on this chain burns ERC-20 supply during withdraw, emitting
        # Transfer(router, zero, amount). Some WETH implementations also emit Withdrawal.
        if address == C.WETH and len(topics) == 3 and topics[0] == T.TRANSFER_TOPIC:
            if ('0x' + topics[1][-40:]).lower() == C.SWAP_ROUTER_V3 and ('0x' + topics[2][-40:]).lower() == C.ZERO:
                burned += int(ev['data'], 16)
        if address == C.WETH and len(topics) == 2 and topics[0] == WITHDRAWAL:
            if ('0x' + topics[1][-40:]).lower() == C.SWAP_ROUTER_V3:
                received += int(ev['data'], 16)
    received = max(received, burned)  # never double-count implementations emitting both events
    if spent != row['amount'] or received < int(row['minimum']):
        return False
    with db.transaction():
        if db.xc("UPDATE gas_refills SET state='settled',received=? WHERE id=? AND state='pending'", (str(received), row['id'])):
            db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note,qty) VALUES(?,?,?,?,?,?,?)",
                 (int(time.time()), 'gas', 'USDG', row['amount']/1e6, row['tx'], 'operations-funded native ETH refill', received/1e18))
    T.watch('gas', 'native ETH gas reserve replenished', row['tx'], done=True)
    return True


def reconcile(rpc, db):
    ensure(db)
    for row in db.q("SELECT * FROM gas_refills WHERE state='pending'"):
        if not settle(db, row, finality.receipt(rpc, row['tx'])):
            return False
    return True


def plan(rpc, db, *, check_attempt=True):
    """Fail closed, use exact USDG units, and never count reserved allocations as operations cash."""
    ensure(db)
    if db.one("SELECT 1 FROM ledger WHERE kind LIKE '%_pending'") or pending(db) or outbox.pending():
        raise ValueError('payments need reconciliation')
    trigger = setting('WH_GAS_REFILL_BELOW_ETH', '0.0003', 0.00005, 0.01)
    target = setting('WH_GAS_REFILL_TARGET_ETH', '0.0015', trigger + 0.00001, 0.02)
    bootstrap = setting('WH_GAS_BOOTSTRAP_ETH', '0.00005', 0.00001, trigger)
    eth = int(rpc.call('eth_getBalance', [C.WALLET, 'pending']), 16)
    if eth >= math.ceil(trigger * 1e18):
        return None
    price = eth_usd(strict=True)
    if price is None or not math.isfinite(price) or price <= 0:
        raise ValueError('fresh ETH price unavailable')
    cap = setting('WH_GAS_REFILL_MAX_USD', '5', 0.25, 10)
    daily = setting('WH_GAS_REFILL_DAILY_USD', '10', cap, 20)
    cooldown = setting('WH_GAS_REFILL_COOLDOWN_HOURS', '6', 1, 24) * 3600
    attempt = float(db.meta_get('gas_refill_attempt_at') or 0)
    if check_attempt and attempt and time.time() - attempt < cooldown:
        raise ValueError('refill attempt cooldown')
    latest = db.one('SELECT ts FROM gas_refills ORDER BY id DESC LIMIT 1')
    if latest and time.time() - latest['ts'] < cooldown:
        raise ValueError('refill cooldown')
    used = db.one('SELECT COALESCE(SUM(amount),0) n FROM gas_refills WHERE ts>=?', (time.time()-86400,))['n']
    amount = min(math.ceil((target-eth/1e18)*price/0.99*1e6), math.floor(cap*1e6), math.floor(daily*1e6)-used)
    keep = setting('WH_GAS_REFILL_KEEP_USDG', '1', 0, 1000)
    free = math.floor(max(0, T.usdg_balance(rpc, C.WALLET) - T.owed_total(db) - keep) * 1e6)
    amount = min(amount, free)
    if amount < 250000:
        raise ValueError('not enough unreserved operations USDG')
    received = quote(rpc, amount)
    fair = amount/1e6/price*1e18
    if not fair*0.95 <= received <= fair*1.05:
        raise ValueError('quote differs from independent ETH price')
    minimum = received * 99 // 100
    gp = int(rpc.call('eth_gasPrice', []), 16)
    if gp <= 0:
        raise ValueError('gas price unavailable')
    # Budget both the exact allowance transaction and the atomic swap/unwrap. Each sender rechecks.
    cost = math.ceil(500000 * int(gp*1.25) * 1.1)
    fraction = setting('WH_GAS_REFILL_MAX_FEE_FRACTION', '0.05', 0.001, 0.1)
    if cost/1e18*price > amount/1e6*fraction or eth < cost + math.ceil(bootstrap*1e18):
        raise ValueError('refill gas budget or bootstrap reserve unavailable')
    return {'amount': amount, 'minimum': minimum, 'bootstrap': bootstrap,
            'fee_limit_eth': amount/1e6*fraction/price, 'cost_wei': cost}


def cycle(rpc, db, acct):
    """False means monetary work must pause; low gas cannot be repaired from zero ETH."""
    ensure(db)
    try:
        if not reconcile(rpc, db):
            status(db, 'refill receipt evidence needs review')
            return False
        if os.environ.get('WH_GAS_REFILL', '0') != '1' or not C.LIVE or not acct:
            return True
        if acct.address.lower() != C.WALLET or int(rpc.call('eth_chainId', []), 16) != C.CHAIN_ID:
            raise ValueError('signer or chain mismatch')
        # Pin the router WETH identity before entrusting it with an unwrap.
        if str(call_fn(rpc, C.SWAP_ROUTER_V3, 'WETH9()', ('address',))).lower() != C.WETH:
            raise ValueError('router WETH mismatch')
        p = plan(rpc, db)
        if p is None:
            status(db, 'ETH reserve sufficient')
            return True
        have = call_fn(rpc, C.USDG, 'allowance(address,address)', ('uint256',),
                       ('address', 'address'), (C.WALLET, C.SWAP_ROUTER_V3))
        # Count attempts before even an allowance is submitted: a confirmed approval revert
        # must not spend gas again at every runner tick. Preflight-only failures spend nothing.
        db.meta_set('gas_refill_attempt_at', str(time.time()))
        if have < p['amount']:
            _, rc = tx.send_tx(rpc, acct, C.USDG,
                call_data('approve(address,uint256)', ('address', 'uint256'), (C.SWAP_ROUTER_V3, p['amount'])),
                min_remaining_eth=p['bootstrap'] + p['cost_wei']/1e18*0.7,
                fee_limit_eth=p['fee_limit_eth']*0.3)
            if not rc or rc.get('status') != '0x1':
                raise ValueError('allowance failed')
        # Approval may take time. Re-quote and recalculate the spendable budget before signing the swap.
        fresh = plan(rpc, db, check_attempt=False)
        if fresh is None or fresh['amount'] < p['amount']:
            raise ValueError('refill budget changed after approval')
        p['minimum'] = max(p['minimum'], quote(rpc, p['amount']) * 99 // 100)
        data = calldata(p['amount'], p['minimum'], int(time.time())+180)
        def remember(h):
            db.x("INSERT INTO gas_refills(ts,amount,minimum,tx,state) VALUES(?,?,?,?,'pending')",
                 (int(time.time()), p['amount'], str(p['minimum']), h))
            T.watch('gas', 'refilling native ETH from operations USDG', h)
        h, rc = tx.send_tx(rpc, acct, C.SWAP_ROUTER_V3, data, on_broadcast=remember,
            min_remaining_eth=p['bootstrap'], fee_limit_eth=p['fee_limit_eth'] * (0.7 if have < p['amount'] else 1.0))
        done = settle(db, db.one('SELECT * FROM gas_refills WHERE tx=?', (h,)), rc)
        status(db, 'refill confirmed' if done and rc['status']=='0x1' else 'refill requires review')
        return done
    except tx.ReceiptPending:
        status(db, 'refill submitted; waiting for chain confirmation')
        return False
    except Exception:
        status(db, 'refill paused: budget, gas, quote or receipt needs review')
        return not (pending(db) or outbox.pending())
