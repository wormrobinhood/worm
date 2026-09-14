"""Private operational checks. Reading status never signs, retries payments, or restarts services."""
import json
import logging
import os
import time
from . import config as C, outbox, treasury as T, fee_sweep, gas_refill

log = logging.getLogger('wormhole.ops')


def check(rpc, db, hub, now=None):
    now = time.time() if now is None else now
    alerts = []
    def add(code, message):
        alerts.append({'code':code, 'message':message})
    try:
        T.ensure_tables(db); fee_sweep.ensure(db); gas_refill.ensure(db)
        from .launch_allocation import saved as allocation_saved
        allocation = allocation_saved(db)
        if allocation and (allocation.get('state') == 'review' or
                           (allocation.get('state') not in ('complete','reverted') and
                            allocation.get('created_at',now) < now-3600)):
            add('launch_allocation_unresolved','The initial token allocation needs reconciliation. Review the saved launch and transfer receipts.')
        if outbox.finality_value('incident'):
            add('finality_changed','Accepted chain evidence changed. Payments require private operator review.')
        if any(r['state']=='preparing' or r['ts']<now-3600 for r in outbox.pending()):add('transaction_unresolved','A transaction needs reconciliation. Keep one signer and inspect its private journal.')
        if db.one("SELECT 1 FROM ledger WHERE kind LIKE '%_pending' AND ts<?", (now-3600,)):
            add('payment_unresolved','A payment has not settled within 60 minutes. Review its chain/provider evidence.')
        if db.one("SELECT 1 FROM fee_sweeps WHERE state='pending' AND ts<?", (now-3600,)) or db.one("SELECT 1 FROM gas_refills WHERE state='pending' AND ts<?", (now-3600,)):
            add('treasury_unresolved','A sweep or gas refill needs receipt evidence.')
        if C.LIVE and C.WALLET:
            balance=int(rpc.call('eth_getBalance',[C.WALLET,'pending']),16)/1e18
            threshold=float(os.environ.get('WH_GAS_REFILL_BELOW_ETH','0.0003'))
            if balance<threshold:add('low_eth','Native ETH is low. Check refill status and bootstrap funding.')
        if (C.DATA_DIR/'payments.paused').exists():add('payments_paused','The operator pause marker is set.')
        idx=getattr(hub,'indexer',None)
        if idx is not None:
            last=getattr(idx,'last_ok',None)
            if (last and now-last>180) or (not last and getattr(hub,'started_at',now)<now-900):
                add('indexer_stale','The indexer is stale. Check RPC connectivity and durable work queues.')
        if os.environ.get('WH_SCREEN_CDP_URL'):
            frame=getattr(hub,'frame_latest',None) or {}
            if now-float(frame.get('ts') or getattr(hub,'started_at',now))>180:
                add('screen_stale','Browser frames are stale. Inspect the isolated browser service.')
        heartbeat=db.meta_get('ops_cycle_at')
        last_cycle = float(heartbeat) if heartbeat else getattr(hub,'started_at',now)
        if now-last_cycle>max(900,C.MARK_EVERY_S*3):
            add('operations_stale','The operations loop heartbeat is stale.')
    except Exception:
        add('health_unavailable','Operational status could not be verified. Inspect the service privately.')
    return {'ok':not alerts,'checked_at':int(now),'alerts':alerts,'live':C.LIVE,'trading':C.TRADING}


def record(db, status):
    """Structured, secret-free transitions for an external alert receiver; hourly reminders."""
    codes=sorted(x['code'] for x in status['alerts'])
    old=db.meta_get('ops_alert_codes')
    last=float(db.meta_get('ops_alert_emitted_at') or 0)
    if json.dumps(codes)!=old or (codes and time.time()-last>=3600):
        payload={'event':'worm_ops_alert' if codes else 'worm_ops_recovered', 'codes':codes}
        (log.error if codes else log.info)(json.dumps(payload))
        db.meta_set('ops_alert_codes',json.dumps(codes))
        db.meta_set('ops_alert_emitted_at',str(time.time()))
    db.meta_set('ops_health_status',json.dumps(status))
