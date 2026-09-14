"""Durable operator-controlled launch time. No browser clock can trigger a transaction."""
from datetime import datetime, timezone
import json
import os
import threading
import time

from . import config as C

LOCK = threading.RLock()
KEY = 'launch_schedule'
ACTIVE = False


def parse_time(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('use an ISO 8601 date with an explicit timezone')
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('use an ISO 8601 date with an explicit timezone') from None
    if dt.tzinfo is None:
        raise ValueError('launch time must include a timezone, such as Z or +03:00')
    return int(dt.timestamp())


def saved(db):
    return json.loads(db.meta_get(KEY) or '{}')


def store(db, value):
    db.meta_set(KEY, json.dumps(value))


def configure(db, at, now=None):
    """Called only by authenticated control or explicit startup environment configuration."""
    with LOCK:
        now = int(time.time()) if now is None else now
        prior = saved(db)
        if C.TOKEN or db.meta_get('own_token') or db.meta_get('launch_pending') or db.meta_get('launch_allocation') or prior.get('state') in ('launching','pending'):
            raise ValueError('a launch already exists or needs reconciliation')
        if at is None:
            store(db, {'state':'cancelled', 'at':None})
        else:
            ts = parse_time(at)
            if ts <= now:
                raise ValueError('choose a future launch time')
            store(db, {'state':'scheduled', 'at':ts})
        return status(db, now)


def initialize(db):
    value = os.environ.get('WH_LAUNCH_AT', '').strip()
    if value and not db.meta_get(KEY):
        configure(db, value)


def status(db, now=None):
    from . import launch_allocation as A
    now = int(time.time()) if now is None else now
    row = saved(db)
    token = C.TOKEN or db.meta_get('own_token')
    state = row.get('state','unscheduled')
    reason = None
    allocation = A.saved(db)
    if token:
        state = 'launched'
    elif db.meta_get('launch_pending'):
        state = 'pending'
    elif allocation.get('state') in ('prepared','approved','approval_pending'):
        state = 'preparing'
        reason = 'Preparing the initial token purchase'
    elif allocation.get('state') in ('review','reverted'):
        state = 'review'
        reason = 'Initial token allocation requires operator review'
    elif state == 'launching' and not ACTIVE:
        state = 'review'
        reason = 'Launch interrupted; operator review required'
    elif state == 'scheduled' and row.get('at', now+1) <= now:
        state = 'due'
        reason = 'Waiting for live execution' if not C.LIVE else 'Waiting for launch checks'
    if (C.DATA_DIR/'payments.paused').exists() and state in ('due','scheduled'):
        reason = 'Launch execution paused by operator'
    return {'at':row.get('at'), 'state':state, 'reason':reason, 'token':token or None,
            'server_now':now, 'live_enabled':C.LIVE, 'allocation':A.public_status(db)}


def tick(rpc, db, acct, hub, now=None):
    from . import launch as L, tx, treasury as T
    global ACTIVE
    with LOCK:
        now = int(time.time()) if now is None else now
        row = saved(db)
        from . import launch_allocation as A
        if A.active(db):
            hub.launch_wanted = False
            return L.go(rpc,db,acct)
        if C.TOKEN or db.meta_get('own_token'):
            hub.launch_wanted = False
            return None
        if db.meta_get('launch_pending'):
            token = L.go(rpc,db,acct)
            if token:
                store(db, {**row, 'state':'launched'})
            elif not db.meta_get('launch_pending'):
                store(db, {**row, 'state':'failed'})
            return token
        due = row.get('state') == 'scheduled' and row.get('at',now+1) <= now
        if not (hub.launch_wanted or due):
            return None
        if not C.LIVE or acct is None or (C.DATA_DIR/'payments.paused').exists():
            return None
        # Finish prior payments before consuming the durable launch request.
        if not tx.recover(rpc):
            return None
        T.ensure_tables(db)
        if db.one("SELECT 1 FROM ledger WHERE kind LIKE '%_pending'"):
            return None
        T.pin_claim_shares(db)
        store(db, {**row, 'state':'launching'})
        hub.launch_wanted = False
        ACTIVE = True
        try:
            token = L.go(rpc,db,acct)
        except Exception:
            # Keep 'launching': interruption here requires evidence, not a blind retry.
            raise
        finally:
            ACTIVE = False
        store(db, {**row, 'state':'launched' if token else 'pending' if db.meta_get('launch_pending') or A.active(db) else 'failed'})
        return token
