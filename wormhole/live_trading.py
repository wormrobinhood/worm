"""Bounded USDG trading pilot. Integration remains disabled pending a funded exit rehearsal.

Only policy-confirmed Transfer evidence changes holdings. Ambiguous submissions retain their slot/hash;
tx.recover rebroadcasts the same signed transaction. A REVIEW row blocks new entries, not other exits.
"""
import json
import time
from decimal import Decimal

from eth_abi import encode
from . import config as C, finality, lab, strategy_validation, trade_checks as execution, trade_risk
from .chain import call_fn, selector, addr_from_topic
from .pons import TRANSFER
from .tx import send_tx


def allowance(rpc, wallet, currency):
    erc = call_fn(rpc, currency, 'allowance(address,address)', ['uint256'], ['address', 'address'], [wallet, C.PERMIT2])[0]
    amount, expiry, _ = call_fn(rpc, C.PERMIT2, 'allowance(address,address,address)', ['uint160', 'uint48', 'uint48'],
                              ['address', 'address', 'address'], [wallet, currency, C.UNIVERSAL_ROUTER])
    return int(erc), int(amount), int(expiry)


def approve_exact(rpc, acct, currency, need):
    if acct.address.lower() != C.WALLET or not 0 < need < 2**160 - 1:
        raise ValueError('invalid trading approval')
    erc, amount, expiry = allowance(rpc, C.WALLET, currency)
    if erc != need:
        data = selector('approve(address,uint256)') + encode(['address', 'uint256'], [C.PERMIT2, need]).hex()
        _, rc = send_tx(rpc, acct, currency, data)
        if not rc or rc.get('status') != '0x1':
            raise RuntimeError('token approval did not settle')
    now = int(time.time())
    if amount != need or not now + execution.SWAP_TTL <= expiry <= now + execution.PERMIT_TTL:
        data = selector('approve(address,address,uint160,uint48)') + encode(
            ['address', 'address', 'uint160', 'uint48'], [currency, C.UNIVERSAL_ROUTER, need, now + execution.PERMIT_TTL]).hex()
        _, rc = send_tx(rpc, acct, C.PERMIT2, data)
        if not rc or rc.get('status') != '0x1':
            raise RuntimeError('router approval did not settle')
    erc, amount, expiry = allowance(rpc, C.WALLET, currency)
    now = int(time.time())
    if erc != need or amount != need or not now + execution.SWAP_TTL <= expiry <= now + execution.PERMIT_TTL:
        raise RuntimeError('bounded approvals failed readback')
    return expiry


def net_transfer(receipt, currency, wallet):
    value, seen = 0, False
    for lg in receipt.get('logs') or []:
        topics = lg.get('topics') or []
        if (lg.get('address') or '').lower() != currency.lower() or len(topics) != 3 or topics[0] != TRANSFER.topic:
            continue
        src, dst = addr_from_topic(topics[1]), addr_from_topic(topics[2])
        n = int(lg['data'], 16)
        if src == wallet or dst == wallet:
            seen = True
            value += n * (int(dst == wallet) - int(src == wallet))
    return value if seen else None


def submit(rpc, db, acct, token, symbol, side, usd, quote, snapshot, approval_expiry):
    from . import trader
    deadline = min(int(time.time()) + execution.SWAP_TTL, approval_expiry)
    if time.time() >= quote['expires_at'] or deadline <= time.time():
        raise ValueError('stale swap quote')
    snapshot = {**snapshot, 'pool': quote['pool'], 'amount_raw': str(quote['amount_raw']),
                'minimum_raw': str(quote['minimum_raw']), 'gas_usd': quote['gas_usd'], 'wallet': C.WALLET}
    with db.transaction():
        if db.one("SELECT 1 FROM trades WHERE token=? AND note IN ('PENDING','REVIEW')", (token,)):
            return
        tid = db.insert("INSERT INTO trades(ts,token,symbol,side,usd,qty,price_usd,mode,note,execution) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (int(time.time()), token, symbol, side, usd, 0, 0, 'live', 'PENDING', json.dumps(snapshot)))
    currency_in, currency_out = (C.USDG, token) if side == 'buy' else (token, C.USDG)
    data = trader.swap_calldata(quote['pool'], quote['direction'], quote['amount_raw'], quote['minimum_raw'],
                               currency_in, currency_out, deadline=deadline)
    def persist_hash(h):
        # send_tx invokes this before broadcast; failures leave the private outbox held for review.
        db.x('UPDATE trades SET tx=? WHERE id=? AND note=\'PENDING\'', (h, tid))
    try:
        send_tx(rpc, acct, C.UNIVERSAL_ROUTER, data, wait=False, on_broadcast=persist_hash,
                valid_until=min(deadline, quote['expires_at']))
    except Exception:
        row = db.one('SELECT tx FROM trades WHERE id=?', (tid,))
        if not row['tx']:
            # Even a callback/storage exception may leave a signed private outbox record. Never
            # infer "unsent" from the exception or release the order for automatic retry.
            db.x("UPDATE trades SET note='REVIEW' WHERE id=?", (tid,))
        db.add_event('error', f'{side} ${symbol}: awaiting transaction recovery or operator review', token)
    return tid


def liquidation_marks(rpc, db):
    marks = {}
    for p in db.q("SELECT * FROM positions WHERE mode='live' AND status='open'"):
        try:
            quote = execution.exit_quote(rpc, json.loads(p['pool_key']), p['token'], int(p['qty_left_raw']))
            marks[p['token']] = quote['minimum_raw'] / 1e6 - quote['gas_usd']
        except Exception:
            pass
    return marks


def decide(rpc, db, runway, acct, ready):
    from . import trader
    if not C.TRADING or not trader.LIVE_SELL_READY or not acct or acct.address.lower() != C.WALLET or not ready or not ready.get('ready'):
        return
    validation = strategy_validation.summary(db)
    if not validation['passed'] or not runway.get('can_invest'):
        return
    if db.one("SELECT 1 FROM trades WHERE mode='live' AND note IN ('PENDING','REVIEW')"):
        return
    if not trade_risk.check(db, marks=liquidation_marks(rpc, db))['allowed']:
        return
    size = min(trader.MAX_POSITION_USD, .1 * float(runway.get('surplus_usd') or 0))
    if size < 1 or trader.open_count(db) >= trader.MAX_OPEN or trader.spent_today(db) + size > trader.MAX_DAILY_USD:
        return
    for r in trader.candidates(db):
        try:
            policy, delay = lab.parse_arm(validation['arm'])
            if time.time() < r['scored_at'] + delay:
                continue
            quote = execution.entry(rpc, db, r['token'], size)
            balance = call_fn(rpc, C.USDG, 'balanceOf(address)', ['uint256'], ['address'], [C.WALLET])[0]
            if balance < quote['amount_raw']:
                continue
            attempt = db.insert('INSERT INTO trade_attempts(ts,token,side,gas_usd) VALUES(?,?,?,?)',
                                (int(time.time()), r['token'], 'buy', quote['gas_usd']))
            expiry = approve_exact(rpc, acct, C.USDG, quote['amount_raw'])
            quote = execution.entry(rpc, db, r['token'], size)  # approvals may take time; refresh both directions
            tid = submit(rpc, db, acct, r['token'], r['symbol'], 'buy', size, quote,
                   {'arm': validation['arm'], 'policy': policy}, expiry)
            db.x('UPDATE trade_attempts SET trade_id=? WHERE id=?', (tid, attempt))
            return  # at most one new position per marker cycle
        except Exception:
            trader._say_once(db, 'trade', f"entry for ${r['symbol']} deferred: execution checks or approvals unavailable", r['token'])
            db.x('INSERT OR REPLACE INTO trade_intents(token,blocked_until) VALUES(?,?)', (r['token'], int(time.time()) + 21600))
            # An approval may be pending in the outbox. Do not begin another order this cycle.
            return


def sell(rpc, db, acct, p, st, frac, why):
    from . import trader
    if not trader.LIVE_SELL_READY or not acct or acct.address.lower() != C.WALLET:
        return
    if db.one("SELECT 1 FROM trades WHERE token=? AND note IN ('PENDING','REVIEW')", (p['token'],)):
        return
    previous = db.one("SELECT ts FROM trades WHERE token=? AND side='sell' AND note='REVERTED' ORDER BY id DESC LIMIT 1", (p['token'],))
    if previous and time.time() - previous['ts'] < 600:
        return
    try:
        remaining, initial = int(p['qty_left_raw']), int(p['qty_raw'])
        amount = remaining if frac >= st['qty_left'] - 1e-9 else min(remaining, int(Decimal(str(frac)) * initial))
        balance = call_fn(rpc, p['token'], 'balanceOf(address)', ['uint256'], ['address'], [C.WALLET])[0]
        if balance < remaining or amount <= 0:
            raise ValueError('tracked holdings do not match available balance')
        pk = json.loads(p['pool_key'])
        quote = execution.exit_quote(rpc, pk, p['token'], amount)  # check liquidity before spending approval gas
        attempt = db.insert('INSERT INTO trade_attempts(ts,token,side,gas_usd) VALUES(?,?,?,?)',
                            (int(time.time()), p['token'], 'sell', quote['gas_usd']))
        expiry = approve_exact(rpc, acct, p['token'], amount)
        quote = execution.exit_quote(rpc, pk, p['token'], amount)
        tid = submit(rpc, db, acct, p['token'], p['symbol'], 'sell', 0, quote,
               {'state': st, 'reason': why, 'expected_remaining': str(remaining)}, expiry)
        db.x('UPDATE trade_attempts SET trade_id=? WHERE id=?', (tid, attempt))
    except Exception:
        trader._say_once(db, 'trade', f"exit for ${p['symbol']} deferred: sell quote, holdings or approvals need review", p['token'])


def reconcile(rpc, db):
    for t in db.q("SELECT * FROM trades WHERE mode='live' AND note='PENDING' AND tx IS NOT NULL"):
        rc = None
        try:
            rc = finality.receipt(rpc, t['tx'])
            if not rc:
                continue
            # Legacy pending buys lack the bounded intent. Never substitute the old quoted quantity.
            snapshot = json.loads(t['execution'] or '{}')
            if not snapshot or snapshot.get('wallet') != C.WALLET:
                raise ValueError('missing execution intent')
            with db.transaction():
                if db.one('SELECT note FROM trades WHERE id=?', (t['id'],))['note'] != 'PENDING':
                    continue
                if rc.get('status') == '0x0':
                    db.x("UPDATE trades SET note='REVERTED' WHERE id=?", (t['id'],))
                    # Failed swaps still cost gas. Include a conservative gas reserve in the loss book.
                    db.x('UPDATE trades SET gas_usd=? WHERE id=?', (snapshot['gas_usd'], t['id']))
                    if t['side'] == 'sell':
                        db.x('UPDATE positions SET realized_usd=COALESCE(realized_usd,0)-? WHERE token=?', (snapshot['gas_usd'], t['token']))
                    db.x('INSERT OR REPLACE INTO trade_intents(token,blocked_until) VALUES(?,?)', (t['token'], int(time.time()) + 21600))
                    continue
                if rc.get('status') != '0x1':
                    continue
                token_net = net_transfer(rc, t['token'], C.WALLET)
                quote_net = net_transfer(rc, C.USDG, C.WALLET)
                amount, minimum = int(snapshot['amount_raw']), int(snapshot['minimum_raw'])
                if token_net is None or quote_net is None:
                    raise ValueError('missing settlement transfers')
                gas = snapshot['gas_usd']
                if t['side'] == 'buy':
                    if token_net < minimum or quote_net != -amount or db.one('SELECT 1 FROM positions WHERE token=?', (t['token'],)):
                        raise ValueError('buy settlement does not match intent')
                    qty, dollars = token_net / 1e18, -quote_net / 1e6
                    px = dollars / qty
                    db.x('INSERT INTO positions(token,symbol,opened_ts,entry_usd,size_usd,qty,qty_left,peak_usd,status,mode,quote,pool_key,policy,tp_done,policy_spec,qty_raw,qty_left_raw,realized_usd)'
                         " VALUES(?,?,?,?,?,?,?,?,'open','live','USDG',?,?,'[]',?,?,?,?)",
                         (t['token'], t['symbol'], t['ts'], px, dollars, qty, qty, px, json.dumps(snapshot['pool']),
                          snapshot['arm'], json.dumps(snapshot['policy']), str(token_net), str(token_net), -gas))
                else:
                    p = db.one("SELECT * FROM positions WHERE token=? AND mode='live' AND status='open'", (t['token'],))
                    if (not p or token_net != -amount or quote_net < minimum
                            or int(p['qty_left_raw']) != int(snapshot['expected_remaining']) or amount > int(p['qty_left_raw'])):
                        raise ValueError('sell settlement does not match intent')
                    qty, dollars = -token_net / 1e18, quote_net / 1e6
                    px = dollars / qty
                    left = int(p['qty_left_raw']) - amount
                    realized = (p['realized_usd'] or 0) + dollars - gas
                    st = snapshot['state']
                    db.x('UPDATE positions SET qty_left_raw=?,qty_left=?,tp_done=?,trail_on=?,peak_usd=?,realized_usd=?,recovered_usd=?,status=?,closed_ts=?,reason=? WHERE token=?',
                         (str(left), left / 1e18, json.dumps(st['tp_done']), int(st['trail_on']), max(st['peak'], p['peak_usd'] or 0),
                          realized, realized if st['tp_done'] else 0, 'open' if left else 'closed',
                          None if left else int(time.time()), None if left else snapshot['reason'], t['token']))
                db.x("UPDATE trades SET note='SUCCESS',usd=?,qty=?,price_usd=?,gas_usd=? WHERE id=?", (dollars, qty, px, gas, t['id']))
                db.add_event('trade', f"{t['side']} ${t['symbol']} settled from on-chain transfers", t['token'])
        except Exception:
            # RPC/finality outages are retryable. A policy-confirmed receipt with unusable accounting evidence
            # is held for review; it must not be turned into a fictitious fill or another order.
            if rc and rc.get('status') in ('0x0', '0x1'):
                db.x("UPDATE trades SET note='REVIEW' WHERE id=? AND note='PENDING'", (t['id'],))
        finally:
            rc = None
