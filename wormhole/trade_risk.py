"""Loss circuit breaker: stops new entries; never disables exits or treasury operations."""
import math
import os
import time


def loss_limit():
    value = float(os.environ.get('WH_MAX_DAILY_LOSS_USD', '10'))
    if not math.isfinite(value) or not 0 < value <= 100:
        raise ValueError('daily trading loss limit must be between zero and $100')
    return value


def check(db, book='live', marks=None, *, latch=True):
    now = int(time.time())
    key = 'loss_pause_until_' + book
    limit = loss_limit()
    rows = (db.q("SELECT size_usd,realized_usd,qty_left,last_usd,entry_usd,status,closed_ts,execution_model,liquidation_usd,marked_ts FROM paper")
            if book == 'paper' else db.q("SELECT size_usd,realized_usd,qty_left,entry_usd,status,closed_ts,token FROM positions WHERE mode=?", (book,)))
    losses, unknown = 0.0, False
    for row in rows:
        if row['status'] != 'open' and (row['closed_ts'] or 0) < now - 86400:
            continue
        pnl = float(row['realized_usd'] or 0) - float(row['size_usd'] or 0)
        if row['status'] == 'open':
            # Callers provide current executable liquidation values for live positions.
            if book == 'paper':
                value = (row['liquidation_usd'] if row.get('execution_model')
                         else float(row['qty_left'] or 0) * float(row['last_usd'] or row['entry_usd'] or 0))
                if row.get('execution_model') and (not row['marked_ts'] or now - row['marked_ts'] > 600):
                    unknown = True
            else:
                value = (marks or {}).get(row['token'])
            if value is None or not math.isfinite(value):
                unknown = True
                continue
            pnl += value
        losses += max(0.0, -pnl)  # winning positions do not replenish the daily loss allowance
    if book == 'live':
        # Reserve the full estimated gas for attempts that may have spent approval gas without
        # reaching a journaled swap. A crash may over-reserve; it must never erase that exposure.
        losses += float(db.one("SELECT COALESCE(SUM(gas_usd),0) n FROM trade_attempts WHERE trade_id IS NULL AND ts>=?", (now - 86400,))['n'])
        losses += float(db.one("SELECT COALESCE(SUM(gas_usd),0) n FROM trades WHERE mode='live' AND side='buy' AND note='REVERTED' AND ts>=?", (now - 86400,))['n'])
    if latch and losses >= limit:
        db.meta_set(key, max(int(db.meta_get(key, '0')), now + 86400))
    until = int(db.meta_get(key, '0'))
    return {'allowed': not unknown and losses < limit and now >= until,
            'loss_usd': round(losses, 4), 'limit_usd': limit, 'paused_until': until,
            'unpriced_positions': unknown}
