"""Bounded USDG trading pilot. Integration remains disabled pending a funded exit rehearsal.

Live follows paper: the only thing ever bought is a token the paper book has just bought under an entry
rule whose prospective paper cohort holds a fresh pass, and only from its verified USDG pool. Entries are
capped three ways: per position, per day, and by a lifetime budget that losses use up and profits never
refill. Only policy-confirmed Transfer evidence changes holdings. An order that failed before the key was
used is released; one whose hash was not stored is matched to the private journal by its calldata, and
only a truly ambiguous one is held for review. A held order blocks new entries, never other exits.
"""
import json
import os
import time
from decimal import Decimal

import rlp
from eth_abi import encode
from eth_utils import keccak
from . import config as C, finality, lab, outbox, strategy_validation, trade_checks as execution, trade_risk
from .chain import call_fn, selector, addr_from_topic
from .pons import TRANSFER
from .tx import send_tx

ENTRY_MAX_AGE_S = 120         # a paper entry older than this is no longer the same trade
TRADE_POLL_S = 1.0            # receipts of trading transactions are polled this often (blocks are ~0.1 s)
EXIT_APPROVAL_S = 49 * 3600   # a position's standing sell approval outlives the longest holding period
UNSENT_AFTER_S = 60           # an order with no stored hash is matched against the journal after this long
RETRY_UNSENT_S = 20           # a sell that was refused before signing is tried again after this long


def budget_usd():
    """The lifetime trading budget: the most the pilot may ever have at risk or lose. 0 (the default) means no
    live buys, whatever else is switched on."""
    try:
        value = float(os.environ.get('WH_TRADING_BUDGET_USD', '0'))
    except ValueError:
        return 0.0
    return value if value == value and 0 < value <= 1000 else 0.0


def budget(db):
    """{budget, at_risk, lost, room}. at_risk: what open positions and unsettled buys cost. lost: the lifetime
    net loss of closed positions after profit distributions plus gas burnt on failed attempts, never below
    zero. Profits allocated to the burn cannot replenish principal; room never exceeds the original budget."""
    at_risk = db.one("SELECT COALESCE(SUM(size_usd),0) n FROM positions WHERE mode='live' AND status='open'")['n']
    at_risk += db.one("SELECT COALESCE(SUM(usd),0) n FROM trades WHERE mode='live' AND side='buy' AND note IN ('PENDING','REVIEW')")['n']
    total = budget_usd()
    # Allocated profits are no longer trading capital, even before their burn settles.
    # Count the original obligation exactly once, not both the obligation and its payment.
    distributed = 0.0
    if db.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger'"):
        distributed = db.one("SELECT COALESCE(SUM(amount),0) n FROM ledger WHERE kind='trade_profit'")['n']
    lost = max(0.0, float(distributed) - realized_pnl(db))
    return {'budget': total, 'at_risk': round(at_risk, 4), 'lost': round(lost, 4), 'room': round(max(0.0, total - at_risk - lost), 4)}


def realized_pnl(db):
    """Lifetime realised result of the live book in USD: closed positions (their cash back minus their cost,
    gas included) less the gas of buys that reverted or never settled into a position."""
    closed = db.one("SELECT COALESCE(SUM(realized_usd - size_usd),0) n FROM positions WHERE mode='live' AND status='closed'")['n']
    wasted = db.one("SELECT COALESCE(SUM(gas_usd),0) n FROM trades WHERE mode='live' AND side='buy' AND note='REVERTED'")['n']
    wasted += db.one("SELECT COALESCE(SUM(gas_usd),0) n FROM trade_attempts WHERE trade_id IS NULL")['n']
    return float(closed) - float(wasted)


def allowance(rpc, wallet, currency):
    erc = call_fn(rpc, currency, 'allowance(address,address)', ['uint256'], ['address', 'address'], [wallet, C.PERMIT2])[0]
    amount, expiry, _ = call_fn(rpc, C.PERMIT2, 'allowance(address,address,address)', ['uint160', 'uint48', 'uint48'],
                              ['address', 'address', 'address'], [wallet, currency, C.UNIVERSAL_ROUTER])
    return int(erc), int(amount), int(expiry)


def approve_exact(rpc, acct, currency, need, ttl=None):
    """Exact allowances for one swap: the ERC-20 allowance to Permit2 and Permit2's allowance to the router,
    both equal to `need`, the second expiring within `ttl` (a swap's ten minutes by default; a position's
    standing sell approval lasts as long as the position may). Read back before anything relies on them."""
    ttl = execution.PERMIT_TTL if ttl is None else ttl
    if acct.address.lower() != C.WALLET or not 0 < need < 2**160 - 1:
        raise ValueError('invalid trading approval')
    erc, amount, expiry = allowance(rpc, C.WALLET, currency)
    if erc != need:
        data = selector('approve(address,uint256)') + encode(['address', 'uint256'], [C.PERMIT2, need]).hex()
        _, rc = send_tx(rpc, acct, currency, data, poll_s=TRADE_POLL_S)
        if not rc or rc.get('status') != '0x1':
            raise RuntimeError('token approval did not settle')
    now = int(time.time())
    if amount != need or not now + execution.SWAP_TTL <= expiry <= now + ttl:
        data = selector('approve(address,address,uint160,uint48)') + encode(
            ['address', 'address', 'uint160', 'uint48'], [currency, C.UNIVERSAL_ROUTER, need, now + ttl]).hex()
        _, rc = send_tx(rpc, acct, C.PERMIT2, data, poll_s=TRADE_POLL_S)
        if not rc or rc.get('status') != '0x1':
            raise RuntimeError('router approval did not settle')
    erc, amount, expiry = allowance(rpc, C.WALLET, currency)
    now = int(time.time())
    if erc != need or amount != need or not now + execution.SWAP_TTL <= expiry <= now + ttl:
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
    currency_in, currency_out = (C.USDG, token) if side == 'buy' else (token, C.USDG)
    data = trader.swap_calldata(quote['pool'], quote['direction'], quote['amount_raw'], quote['minimum_raw'],
                               currency_in, currency_out, deadline=deadline)
    snapshot = {**snapshot, 'pool': quote['pool'], 'amount_raw': str(quote['amount_raw']),
                'minimum_raw': str(quote['minimum_raw']), 'gas_usd': quote['gas_usd'], 'wallet': C.WALLET,
                'calldata': '0x' + keccak(hexstr=data).hex()}       # how a journaled transaction is matched back to this order
    with db.transaction():
        if db.one("SELECT 1 FROM trades WHERE token=? AND note IN ('PENDING','REVIEW')", (token,)):
            return
        tid = db.insert("INSERT INTO trades(ts,token,symbol,side,usd,qty,price_usd,mode,note,execution) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (int(time.time()), token, symbol, side, usd, 0, 0, 'live', 'PENDING', json.dumps(snapshot)))
    def persist_hash(h):
        # send_tx invokes this before broadcast; failures leave the private outbox held for review.
        db.x('UPDATE trades SET tx=? WHERE id=? AND note=\'PENDING\'', (h, tid))
    try:
        send_tx(rpc, acct, C.UNIVERSAL_ROUTER, data, wait=True, on_broadcast=persist_hash,
                valid_until=min(deadline, quote['expires_at']), poll_s=TRADE_POLL_S)
    except Exception as e:
        row = db.one('SELECT tx FROM trades WHERE id=?', (tid,))
        if not row['tx'] and getattr(e, 'not_signed', False):
            # Refused before the key was used (paused, another transaction unresolved, a fee cap, a stale
            # quote): no transaction can exist, so the order is released instead of held.
            db.x("UPDATE trades SET note='FAILED: not sent' WHERE id=? AND note='PENDING'", (tid,))
            db.add_event('trade', f'{side} ${symbol}: not sent, nothing was signed', token)
            return None
        if not row['tx']:
            # Even a callback/storage exception may leave a signed private outbox record. Never
            # infer "unsent" from the exception; resolve_unsent() matches it against the journal.
            db.x("UPDATE trades SET note='REVIEW' WHERE id=?", (tid,))
        db.add_event('error', f'{side} ${symbol}: awaiting transaction recovery or operator review', token)
    return tid


def _journaled_calldata():
    """{keccak(calldata): hash} for every transaction in the private journal sent to the router."""
    out = {}
    with outbox.journal() as j:
        rows = j.execute('SELECT hash, raw FROM intents').fetchall()
    for r in rows:
        try:
            fields = rlp.decode(bytes.fromhex(r['raw'][2:]))
            if len(fields) >= 6 and fields[3].hex() == C.UNIVERSAL_ROUTER[2:]:
                out['0x' + keccak(fields[5]).hex()] = r['hash']
        except Exception:
            continue
    return out


def resolve_unsent(db):
    """Orders without a stored hash. send_tx journals signed bytes before it does anything else with them, so
    the journal decides: a journaled transaction with this order's calldata is this order (its hash is
    attached and the receipt settles it as usual); none means nothing was ever signed for it, and the order
    is released. Nothing is decided while the journal itself is mid-write."""
    rows = db.q("SELECT id,token,symbol,side,execution FROM trades WHERE mode='live' AND note IN ('PENDING','REVIEW')"
                " AND tx IS NULL AND ts<?", (int(time.time()) - UNSENT_AFTER_S,))
    if not rows or any(item['state'] == 'preparing' for item in outbox.pending()):
        return
    sent = _journaled_calldata()
    for t in rows:
        try:
            wanted = json.loads(t['execution'] or '{}').get('calldata')
        except ValueError:
            wanted = None
        if not wanted:
            continue                                   # an order from before calldata was recorded stays for the operator
        if wanted in sent:
            db.x("UPDATE trades SET tx=?, note='PENDING' WHERE id=? AND tx IS NULL", (sent[wanted], t['id']))
            db.add_event('trade', f"{t['side']} ${t['symbol']}: found in the transaction journal; settling from its receipt", t['token'])
        else:
            db.x("UPDATE trades SET note='FAILED: not sent' WHERE id=? AND tx IS NULL", (t['id'],))
            db.add_event('trade', f"{t['side']} ${t['symbol']}: never signed; order released", t['token'])


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
    """Buy what the paper book has just bought, when every gate is open: the policy switch, the sell release
    gate, readiness, a fresh paper-cohort pass for that entry rule, real surplus, no unresolved order, the
    loss breaker, the per-position, daily and lifetime limits, and a verified USDG pool with a round-trip quote."""
    from . import trader
    if not C.TRADING or not trader.LIVE_SELL_READY or not acct or acct.address.lower() != C.WALLET or not ready or not ready.get('ready'):
        return
    validation = strategy_validation.summary(db)
    if not validation['passed'] or not runway.get('can_invest'):
        return
    resolve_unsent(db)
    if db.one("SELECT 1 FROM trades WHERE mode='live' AND note IN ('PENDING','REVIEW')"):
        return
    if not trade_risk.check(db, marks=liquidation_marks(rpc, db))['allowed']:
        return
    size = min(trader.MAX_POSITION_USD, .1 * float(runway.get('surplus_usd') or 0), budget(db)['room'])
    if size < 1 or trader.open_count(db) >= trader.MAX_OPEN or trader.spent_today(db) + size > trader.MAX_DAILY_USD:
        return
    policy, _ = lab.parse_arm(validation['arm'])
    for r in trader.candidates(db, validation.get('passed_rules') or []):
        try:
            execution.pool(rpc, db, r['token'])
        except Exception:
            # Not a verified USDG pool: outside the pilot, and not worth a line in the log every cycle.
            db.x("INSERT OR REPLACE INTO trade_intents(token,arm,scored_at,status) VALUES(?,?,?,'unsupported')",
                 (r['token'], validation['arm'], r['opened_ts']))
            continue
        try:
            quote = execution.entry(rpc, db, r['token'], size)
            balance = call_fn(rpc, C.USDG, 'balanceOf(address)', ['uint256'], ['address'], [C.WALLET])[0]
            if balance < quote['amount_raw']:
                continue
            attempt = db.insert('INSERT INTO trade_attempts(ts,token,side,gas_usd) VALUES(?,?,?,?)',
                                (int(time.time()), r['token'], 'buy', quote['gas_usd']))
            expiry = approve_exact(rpc, acct, C.USDG, quote['amount_raw'])
            quote = execution.entry(rpc, db, r['token'], size)  # approvals may take time; refresh both directions
            tid = submit(rpc, db, acct, r['token'], r['symbol'], 'buy', size, quote,
                   {'arm': validation['arm'], 'policy': policy, 'rule': r['strategy']}, expiry)
            db.x('UPDATE trade_attempts SET trade_id=? WHERE id=?', (tid, attempt))
            reconcile(rpc, db, acct)  # the receipt is usually in hand: open the position and stand its exit approval now
            return  # at most one new position per call
        except Exception:
            trader._say_once(db, 'trade', f"entry for ${r['symbol']} deferred: execution checks or approvals unavailable", r['token'])
            db.x('INSERT OR REPLACE INTO trade_intents(token,blocked_until) VALUES(?,?)', (r['token'], int(time.time()) + 21600))
            # An approval may be pending in the outbox. Do not begin another order this cycle.
            return


def prepare_exit(rpc, db, acct, token, qty_raw):
    """Stand the sell approvals as soon as a buy settles, for exactly what was bought, so a stop is one
    transaction instead of three. The router can only pull what this wallet's own swap spends. A failure here
    costs nothing but time: sell() approves again when it has to."""
    try:
        approve_exact(rpc, acct, token, int(qty_raw), ttl=EXIT_APPROVAL_S)
        return True
    except Exception:
        db.add_event('trade', 'exit approval could not be prepared; the sell will approve when it is needed', token)
        return False


def sell(rpc, db, acct, p, st, frac, why):
    from . import trader
    if not trader.LIVE_SELL_READY or not acct or acct.address.lower() != C.WALLET:
        return
    resolve_unsent(db)
    if db.one("SELECT 1 FROM trades WHERE token=? AND note IN ('PENDING','REVIEW')", (p['token'],)):
        return
    last = db.one("SELECT ts, note FROM trades WHERE token=? AND side='sell' AND (note='REVERTED' OR note LIKE 'FAILED%') ORDER BY id DESC LIMIT 1", (p['token'],))
    if last and time.time() - last['ts'] < RETRY_UNSENT_S:
        return                                   # a moment to let whatever refused the last attempt clear
    # A sell that reverted means the pool moved further than the quote allowed. The next try gives up more
    # to get out: a stop that cannot fill protects nothing.
    tolerance = execution.EXIT_TOLERANCE_RETRY if last and last['note'] == 'REVERTED' and time.time() - last['ts'] < 600 else None
    try:
        remaining, initial = int(p['qty_left_raw']), int(p['qty_raw'])
        amount = remaining if frac >= st['qty_left'] - 1e-9 else min(remaining, int(Decimal(str(frac)) * initial))
        balance = call_fn(rpc, p['token'], 'balanceOf(address)', ['uint256'], ['address'], [C.WALLET])[0]
        if balance < remaining or amount <= 0:
            raise ValueError('tracked holdings do not match available balance')
        pk = json.loads(p['pool_key'])
        quote = execution.exit_quote(rpc, pk, p['token'], amount, tolerance=tolerance)  # check liquidity before spending approval gas
        attempt = db.insert('INSERT INTO trade_attempts(ts,token,side,gas_usd) VALUES(?,?,?,?)',
                            (int(time.time()), p['token'], 'sell', quote['gas_usd']))
        expiry = approve_exact(rpc, acct, p['token'], amount, ttl=EXIT_APPROVAL_S)
        quote = execution.exit_quote(rpc, pk, p['token'], amount, tolerance=tolerance)
        tid = submit(rpc, db, acct, p['token'], p['symbol'], 'sell', 0, quote,
               {'state': st, 'reason': why, 'expected_remaining': str(remaining)}, expiry)
        db.x('UPDATE trade_attempts SET trade_id=? WHERE id=?', (tid, attempt))
        reconcile(rpc, db, acct)
    except Exception:
        trader._say_once(db, 'trade', f"exit for ${p['symbol']} deferred: sell quote, holdings or approvals need review", p['token'])


def reconcile(rpc, db, acct=None):
    resolve_unsent(db)
    for t in db.q("SELECT * FROM trades WHERE mode='live' AND note='PENDING' AND tx IS NOT NULL"):
        rc, bought = None, None
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
                    px = execution.entry_basis(dollars, qty)
                    db.x('INSERT INTO positions(token,symbol,opened_ts,entry_usd,size_usd,qty,qty_left,peak_usd,status,mode,quote,pool_key,policy,tp_done,policy_spec,qty_raw,qty_left_raw,realized_usd)'
                         " VALUES(?,?,?,?,?,?,?,?,'open','live','USDG',?,?,'[]',?,?,?,?)",
                         (t['token'], t['symbol'], t['ts'], px, dollars, qty, qty, px, json.dumps(snapshot['pool']),
                          snapshot['arm'], json.dumps(snapshot['policy']), str(token_net), str(token_net), -gas))
                    bought = token_net
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
        if bought and acct is not None and acct.address.lower() == C.WALLET:
            prepare_exit(rpc, db, acct, t['token'], bought)      # outside the bookkeeping transaction: it sends
