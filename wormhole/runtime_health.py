"""Bounded local health checks, independent of RPC and the operations loop.

The public response contains status codes only. No paths, balances or exceptions.
Disk pressure never triggers automatic deletion of financial state or backups.
"""
import json
from contextlib import closing
import logging
import shutil
import sqlite3
import time

log = logging.getLogger('wormhole.health')
MIB = 1024 * 1024


def storage(path):
    try:
        usage = shutil.disk_usage(path.parent)
        critical = usage.free < max(16 * MIB, usage.total * .05)
        warning = usage.free < max(32 * MIB, usage.total * .10)
        return {'ok': not critical, 'warning': warning, 'available_bytes': usage.free,
                'total_bytes': usage.total}
    except OSError:
        return {'ok': False, 'warning': True, 'available_bytes': None, 'total_bytes': None}


def probe_database(path):
    """Commit a small heartbeat with our own bounded connection, never the shared DB lock."""
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=1)) as conn:
            with conn:
                conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('health_write_at',?)", (str(time.time()),))
        return True
    except sqlite3.Error:
        return False


def status(db, hub, now=None):
    now = time.time() if now is None else now
    codes = []
    disk = storage(db.path)
    if not disk['ok']:
        codes.append('storage_unavailable' if disk['available_bytes'] is None else 'storage_critical')
    elif disk['warning']:
        codes.append('storage_low')
    idx = getattr(hub, 'indexer', None)
    last = getattr(idx, 'last_ok', None)
    age = max(0, round(now - last)) if last else None
    ready = getattr(idx, 'ready', None)
    catching_up = bool(ready is not None and not ready.is_set())
    # Finishing startup backfill is not necessarily reaching today's head: its target was
    # fixed at startup. Use the cached anchor and committed checkpoint without a DB/RPC read.
    anchor = getattr(idx, 'anchor', None)
    checkpoint = getattr(idx, 'last_indexed_block', None)
    chain_lag = None
    if anchor and checkpoint is not None:
        checkpoint_time = anchor[1] - max(0, anchor[0] - checkpoint) * idx.block_time
        chain_lag = max(0, round(now - checkpoint_time))
        catching_up = catching_up or chain_lag >= 180
    if idx is not None and (age is not None and age >= 180 or
                            age is None and now - hub.started_at >= 180):
        codes.append('indexer_stale')
    for name, limit in getattr(hub, 'worker_limits', {}).items():
        at = getattr(hub, 'worker_heartbeats', {}).get(name, hub.started_at)
        if now - at > limit:
            codes.append(name + '_stale')
    probe = getattr(hub, 'database_probe', None)
    if probe is not None and (not probe['ok'] or now - probe['at'] > 120):
        codes.append('database_unwritable')
    blocked = [c for c in codes if c != 'storage_low']
    return {'ok': not blocked, 'codes': codes, 'indexer_age_s': age,
            'chain_lag_s': chain_lag, 'catching_up': catching_up}


def monitor(db, hub):
    """Run from a dedicated thread. Emit transitions without requiring a working database."""
    previous, emitted = None, 0
    while True:
        now = time.time()
        hub.database_probe = {'at': now, 'ok': storage(db.path)['ok'] and probe_database(db.path)}
        current = status(db, hub, now)
        codes = sorted(current['codes'])
        if codes != previous or codes and now - emitted >= 3600:
            (log.error if codes else log.info)(json.dumps({
                'event': 'worm_health_alert' if codes else 'worm_health_recovered', 'codes': codes}))
            previous, emitted = codes, now
        time.sleep(30)


def trading_ready(db, hub):
    current = status(db, hub)
    return current['ok'] and not current['catching_up']


def should_restart(db, hub, now=None):
    """Restart stale collection even during backfill, but don't loop on exhausted storage."""
    current = status(db, hub, now)
    return (current['indexer_age_s'] or 0) > 900 and not any(
        c in current['codes'] for c in ('storage_critical', 'storage_unavailable', 'database_unwritable'))
