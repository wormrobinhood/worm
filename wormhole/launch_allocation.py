"""Opt-in, bounded 2% developer buy: atomic launch/buy, then a durable 1% transfer.

The router has no multi-recipient method. The creator transfer is a separate confirmed
transaction; a restart never repeats a completed leg. No trading switch is changed.
Verified interface: PonsV2LaunchAndBuy at config.PONS_ROUTER, Robinhood Chain.
"""
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import json
import logging
import os
import time

from eth_abi import encode, decode
from eth_utils import keccak

from . import config as C, finality, tx, outbox
from .chain import call_fn, call_data, Event
from .pons import TOKEN_LAUNCHED, TRANSFER

log = logging.getLogger('wormhole.launch_allocation')
KEY = 'launch_allocation'
ROUTER_EVENT = Event('Launched', [
    ('token', 'address', True), ('curve', 'address', True), ('recipient', 'address', True),
    ('launcher', 'address', False), ('quoteSpent', 'uint256', False), ('tokensReceived', 'uint256', False)])
TERMINAL = {'complete', 'review', 'reverted'}
RESERVED = {'prepared', 'approval_pending', 'approved', 'launch_pending'}


def enabled():
    return os.environ.get('WH_LAUNCH_BUY', '0') == '1'


def saved(db):
    return json.loads(db.meta_get(KEY) or '{}')


def store(db, p):
    db.meta_set(KEY, json.dumps(p))


def active(db):
    p = saved(db)
    return bool(p and p['state'] not in TERMINAL)


def reserved_usdg(db):
    p = saved(db)
    return p['quote_units'] / 1e6 if p.get('state') in RESERVED else 0.0


def public_status(db):
    p = saved(db)
    return ({'state': p['state'], 'target_percent': 2, 'creator_percent': 1,
             'worm_percent': 1, 'purchase_usdg': p['quote_units'] / 1e6,
             'launch_tx': p.get('launch_tx'), 'transfer_tx': p.get('transfer_tx')}
            if p else None)


def setting_units(name, decimals, *, positive=False):
    try:
        n = Decimal(os.environ[name])
        scaled = n * (10 ** decimals)
        if not n.is_finite() or n < 0 or (positive and n <= 0) or scaled != scaled.to_integral_value():
            raise ValueError()
        return int(scaled)
    except (KeyError, ValueError, InvalidOperation, OverflowError):
        raise ValueError(f'{name} must be explicitly set to a valid amount') from None


def quote_amount(supply, phantom, fee_bps, tax_bps):
    """Integer-only quote for at least 2% of the launch supply, including both fees.

    The gross-up rounds upward. USDG has six decimals, so the resulting WORM
    position can exceed exactly 1% by the tiny input-rounding remainder.
    """
    if supply <= 0 or supply % 100 or phantom <= 0 or not (0 <= fee_bps + tax_bps < 10_000):
        raise ValueError('unsupported launch economics')
    if min(fee_bps, tax_bps) < 0:
        raise ValueError('invalid fee')
    target = supply * 2 // 100
    net = (target * phantom + supply - target - 1) // (supply - target)
    gross = (net * 10_000 + 10_000 - fee_bps - tax_bps - 1) // (10_000 - fee_bps - tax_bps)
    credited = gross - gross * fee_bps // 10_000 - gross * tax_bps // 10_000
    output = credited * supply // (phantom + credited)
    if output < target:
        raise ValueError('initial purchase quote falls short')
    return gross, target, output


def quote(rpc, tax_bps):
    """Read all economic terms at one block. This read-only quote never loads a signer."""
    block = hex(rpc.block_number())
    def read(to, sig, outs, ats=(), args=()):
        result = call_fn(rpc, to, sig, outs, ats, args, block=block)
        if result is None:
            raise ValueError('launch economics unavailable')
        return result
    if (read(C.PONS_ROUTER, 'factory()', ('address',)).lower() != C.FACTORY or
            read(C.FACTORY, 'launchForwarder()', ('address',)).lower() != C.PONS_ROUTER):
        raise ValueError('launch router configuration changed')
    cfg = read(C.FACTORY, 'getLaunchConfig(uint256)',
               ('uint256','uint256','uint256','uint256','uint24','int24','bool'), ('uint256',), (0,))
    phantom, threshold, decimals = read(C.FACTORY, 'pairTokenEconomics(address)',
                                        ('uint256','uint256','uint8'), ('address',), (C.USDG,))
    if not cfg[6] or decimals != 6 or threshold <= 0:
        raise ValueError('USDG launch configuration unavailable')
    amount, target, output = quote_amount(cfg[0], phantom, cfg[1], tax_bps)
    sellable = cfg[0] - cfg[0] * phantom // (phantom + threshold)
    # The curve permits proportional partial fills: reject that case before signing.
    if output >= sellable:
        raise ValueError('initial buy would reach the graduation allocation')
    pin = read(C.FACTORY, 'previewLaunchEconomics(uint256,address)', ('bytes32',),
               ('uint256','address'), (0,C.USDG))
    if pin == bytes(32):
        raise ValueError('empty launch economics pin')
    return {'block': block, 'supply': cfg[0], 'quote_units': amount, 'target_units': target,
            'expected_units': output, 'creator_units': cfg[0] // 100,
            'economics': pin.hex(), 'fee_wei': read(C.FACTORY, 'launchFee()', ('uint256',))}


def calldata(p, metadata):
    from .launch import TOKEN_PARAMS_T
    tup = (metadata['name'], metadata['symbol'], metadata['logo'], metadata['description'],
           tuple(metadata['socials']), p['wallet'], metadata['creatorTaxBps'], metadata['buybackEnabled'],
           bytes.fromhex(p['economics']), bytes.fromhex(p['salt']))
    return call_data(f'launchAndBuy({TOKEN_PARAMS_T},uint256,address,uint256,uint256,address,address[])',
                     (TOKEN_PARAMS_T,'uint256','address','uint256','uint256','address','address[]'),
                     (tup,0,C.USDG,p['quote_units'],p['target_units'],p['wallet'],[]))


def prepare(rpc, db, wallet, metadata):
    creator = C.OWNER_WALLET
    if (not C.valid_address(creator) or creator.lower() in (wallet.lower(),C.ZERO,C.DEAD)
            or not C.valid_address(wallet) or wallet.lower() != C.WALLET):
        raise ValueError('a distinct, valid creator recipient and matching signer are required')
    p = quote(rpc, metadata['creatorTaxBps'])
    p.update(wallet=wallet.lower(), creator=creator.lower(), state='prepared', created_at=int(time.time()),
             max_units=setting_units('WH_LAUNCH_BUY_MAX_USDG',6,positive=True),
             reserve_units=setting_units('WH_LAUNCH_KEEP_USDG',6,positive=True),
             reserve_wei=setting_units('WH_LAUNCH_KEEP_ETH',18,positive=True),
             salt=keccak(os.urandom(32)).hex(), metadata=metadata)
    if p['quote_units'] > p['max_units']:
        raise ValueError('initial buy exceeds its approved USDG cap')
    check_funds(rpc, db, p)
    p['data'] = calldata(p,metadata)
    store(db,p)                         # durable intent exists before any approval/signature
    return p


def balance(rpc, asset, wallet):
    v = call_fn(rpc,asset,'balanceOf(address)',('uint256',),('address',),(wallet,))
    if v is None:
        raise ValueError('token balance unavailable')
    return int(v)


def check_funds(rpc, db, p):
    from . import treasury as T
    T.ensure_tables(db)
    if db.one("SELECT 1 FROM ledger WHERE kind='claim_pending'"):
        raise ValueError('claim reconciliation required before launch')
    # Exclude this operation's own reservation; retain all fee beneficiaries' money.
    liabilities = max(Decimal(0), Decimal(str(T.owed_total(db))) - Decimal(str(reserved_usdg(db))))
    owed = int((liabilities * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    if balance(rpc,C.USDG,p['wallet']) < p['quote_units'] + p['reserve_units'] + owed:
        raise ValueError('fund the developer buy and protected USDG runway before launch')
    if int(rpc.call('eth_getBalance',[p['wallet'],'latest']),16) <= p['fee_wei'] + p['reserve_wei']:
        raise ValueError('fund launch fee, gas and protected ETH reserve before launch')


def _events(rc, contract, event):
    return [event.decode(lg) for lg in rc.get('logs',[])
            if lg.get('address','').lower() == contract.lower() and lg.get('topics',[])[:1] == [event.topic]]


def transferred(rc, token, sender, recipient):
    return sum(e['value'] for e in _events(rc,token,TRANSFER)
               if e['from'] == sender.lower() and e['to'] == recipient.lower())


def review(db, p, reason):
    p.update(state='review', reason=reason)
    store(db,p)
    db.add_event('error','initial token allocation requires operator review; no automatic retry')
    log.error('initial allocation review: %s', reason)


def settle_launch(db, p, rc):
    if rc.get('status') == '0x0':
        p['state'] = 'reverted'
        db.atomic([("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (KEY,json.dumps(p))),
                   ("INSERT OR REPLACE INTO meta(key,value) VALUES('launch_pending','')",())])
        return False
    if rc.get('status') != '0x1':
        return False
    launches = _events(rc,C.FACTORY,TOKEN_LAUNCHED)
    buys = _events(rc,C.PONS_ROUTER,ROUTER_EVENT)
    if len(launches) != 1 or len(buys) != 1:
        review(db,p,'missing or ambiguous launch/buy event'); return False
    a,b = launches[0],buys[0]
    valid = (a['token'] == b['token'] and a['curve'] == b['curve'] and a['deployer'] == p['wallet']
             and a['token'] == p['token_predicted'] and a['curve'] == p['curve_predicted']
             and a['pairToken'] == C.USDG and b['launcher'] == p['wallet'] and b['recipient'] == p['wallet']
             and b['quoteSpent'] == p['quote_units'] and b['tokensReceived'] == p['expected_units']
             and b['tokensReceived'] >= p['target_units']
             and transferred(rc,a['token'],C.ZERO,a['curve']) == p['supply']
             and transferred(rc,a['token'],a['curve'],p['wallet']) == b['tokensReceived'])
    if not valid:
        review(db,p,'launch allocation evidence did not match the pinned intent'); return False
    p.update(state='allocation_ready',token=a['token'],curve=a['curve'],received_units=b['tokensReceived'])
    db.atomic([("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (KEY,json.dumps(p))),
               ("INSERT OR REPLACE INTO meta(key,value) VALUES('own_token',?)", (p['token'],)),
               ("INSERT OR REPLACE INTO meta(key,value) VALUES('launch_pending','')",())])
    C.TOKEN = p['token']
    db.add_event('launch','token launched with its 2% initial purchase; creator allocation awaiting transfer',p['token'])
    return True


def cycle(rpc,db,acct,say=None):
    """Called under launch.operation_lock. Resume the saved intent before reading new settings."""
    from . import launch as L
    from .treasury import watch
    say = say or log.info
    p = saved(db)
    if not p:
        if not C.LIVE or not enabled() or acct is None or C.TOKEN or db.meta_get('own_token'):
            return None
        if db.meta_get('launch_pending'):
            raise ValueError('legacy launch must be reconciled before enabling developer buy')
        p = prepare(rpc,db,acct.address,L.params(acct.address))
    if p['state'] in TERMINAL:
        return p.get('token')
    if C.WALLET != p['wallet'] or (acct and acct.address.lower() != p['wallet']):
        raise ValueError('saved allocation belongs to another signer')
    if C.TOKEN and C.TOKEN != p.get('token'):
        raise ValueError('configured token differs from saved launch')
    # Pending receipts may be read even while payments are off.
    for state,key in [('approval_pending','approval_tx'),('launch_pending','launch_tx'),('transfer_pending','transfer_tx')]:
        if p['state'] != state:
            continue
        rc = finality.receipt(rpc,p[key],approval=state=='approval_pending')
        if not rc:
            return p.get('token')
        if rc.get('status') in ('0x0','0x1'):
            outbox.state(p[key], 'settled')
        if state == 'launch_pending':
            if not settle_launch(db,p,rc):
                return None
        elif rc.get('status') != '0x1':
            if rc.get('status') == '0x0':
                review(db,p,'confirmed transaction revert')
            return p.get('token')
        elif state == 'approval_pending':
            p['state']='approved'; store(db,p)
        else:
            if transferred(rc,p['token'],p['wallet'],p['creator']) != p['creator_units']:
                review(db,p,'creator transfer event missing or incorrect'); return p['token']
            p['state']='complete'; store(db,p)
            db.add_event('launch','initial allocation complete: 1% to creator, 1% plus purchase rounding retained by WORM',p['token'])
            watch('launch','launch and initial token allocation complete',p[key],done=True)
            return p['token']
    if not C.LIVE or acct is None or (C.DATA_DIR/'payments.paused').exists():
        return p.get('token')
    if not tx.recover(rpc):
        return p.get('token')
    if p['state'] in ('prepared','approved'):
        # Restart/config changes cannot silently increase the cap or change the recipient.
        fresh = quote(rpc,p['metadata']['creatorTaxBps'])
        if any(fresh[k] != p[k] for k in ('economics','quote_units','expected_units','supply','fee_wei')):
            review(db,p,'launch economics changed; obtain a new operator-approved plan'); return None
        check_funds(rpc,db,p)
        allowance = call_fn(rpc,C.USDG,'allowance(address,address)',('uint256',),
                            ('address','address'),(p['wallet'],C.PONS_ROUTER))
        if allowance is None:
            raise ValueError('launch router allowance unavailable')
        if allowance != p['quote_units']:
            def approval_pending(h):
                p.update(state='approval_pending',approval_tx=h); store(db,p)
            data=call_data('approve(address,uint256)',('address','uint256'),(C.PONS_ROUTER,p['quote_units']))
            tx.send_tx(rpc,acct,C.USDG,data,on_broadcast=approval_pending,wait=False,
                       min_remaining_eth=(p['reserve_wei']+p['fee_wei'])/1e18,say=say)
            return None
        # Real balances/allowances are required. No funded override can authorize a live send.
        raw = rpc.call('eth_call',[{'from':p['wallet'],'to':C.PONS_ROUTER,
                                   'value':hex(p['fee_wei']),'data':p['data']},'latest'])
        token,curve,amount = decode(['address','address','uint256'],bytes.fromhex(raw[2:]))
        if amount != p['expected_units'] or amount < p['target_units'] or token==C.ZERO or curve==C.ZERO:
            review(db,p,'launch-and-buy simulation differs from quote'); return None
        def launch_pending(h):
            p.update(state='launch_pending',launch_tx=h,token_predicted=token,curve_predicted=curve)
            db.atomic([("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",(KEY,json.dumps(p))),
                       ("INSERT OR REPLACE INTO meta(key,value) VALUES('launch_pending',?)",(h,)),
                       ("INSERT OR REPLACE INTO meta(key,value) VALUES('launch_pending_ts',?)",(str(int(time.time())),))])
            watch('launch','launching token with its 2% initial purchase',h)
        tx.send_tx(rpc,acct,C.PONS_ROUTER,p['data'],value=p['fee_wei'],gas_floor=L.GAS_FLOOR,
                   min_remaining_eth=p['reserve_wei']/1e18,on_broadcast=launch_pending,wait=False,
                   valid_until=time.time()+30,say=say)
        return None
    if p['state'] == 'allocation_ready':
        if balance(rpc,p['token'],p['wallet']) < p['creator_units']*2:
            review(db,p,'insufficient tokens to retain WORM allocation and pay creator'); return p['token']
        data=call_data('transfer(address,uint256)',('address','uint256'),(p['creator'],p['creator_units']))
        raw=rpc.call('eth_call',[{'from':p['wallet'],'to':p['token'],'data':data},'latest'])
        if decode(['bool'],bytes.fromhex(raw[2:])) != (True,):
            review(db,p,'creator transfer simulation failed'); return p['token']
        def transfer_pending(h):
            p.update(state='transfer_pending',transfer_tx=h); store(db,p)
            watch('launch','sending the creator’s 1% initial allocation',h)
        tx.send_tx(rpc,acct,p['token'],data,on_broadcast=transfer_pending,wait=False,
                   min_remaining_eth=p['reserve_wei']/1e18,say=say)
    return p.get('token')
