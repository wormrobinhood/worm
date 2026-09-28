"""Loss circuit breaker: stops new entries; never disables exits or treasury operations."""
import math
import os
import time

STALE_HAIRCUT = 0.15     # a paper position whose quoted valuation is stale but whose pool mid is fresh is counted at that
                         # mid less the worst round trip an entry may cost: one slow provider must not pause every entry
MARK_MAX_AGE_S = 600


def loss_limit():
    value = float(os.environ.get('WH_MAX_DAILY_LOSS_USD', '10'))
    if not math.isfinite(value) or not 0 < value <= 100:
        raise ValueError('daily trading loss limit must be between zero and $100')
    return value


def check(db, book='live', marks=None, *, latch=True, scope=None):
    """scope='live' (paper only): the breaker live would run, over the paper positions whose fills live could have
    made (a checked route from USDG, any pair: trade_checks.live_fill), with its own pause. Second-look entries
    answer to it, so losses on fills live could not have made (an ETH pool quoted directly while the aggregators
    were down) do not pause the evidence live is judged on. A position flagged as opened during an earlier pause
    still counts here: it is out of the evidence, not out of the day's losses, and leaving an underwater one out
    would reopen entries. It replaces the USDG-only breaker and honours a pause that one still holds."""
    now = int(time.time())
    key = 'loss_pause_until_' + book + ('_' + scope if scope else '')
    limit = loss_limit()
    rows = (db.q("SELECT * FROM paper") if book == 'paper'
            else db.q("SELECT size_usd,realized_usd,qty_left,entry_usd,status,closed_ts,token FROM positions WHERE mode=?", (book,)))
    if scope == 'live':
        from . import trade_checks
        rows = [r for r in rows if trade_checks.live_fill(r) and r.get('execution_model')]
    elif scope is not None:
        raise ValueError('unknown breaker scope')
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
                if row.get('execution_model') and (not row['marked_ts'] or now - row['marked_ts'] > MARK_MAX_AGE_S or value is None):
                    # The quote is stale; the pool's own mid, read every 15 s, stands in at a conservative discount.
                    # Only with neither is the position unknown (and entries wait).
                    fresh_mid = row.get('monitor_ts') and now - row['monitor_ts'] <= MARK_MAX_AGE_S and row.get('last_usd')
                    if fresh_mid:
                        value = float(row['qty_left'] if row['qty_left'] is not None else row['qty'] or 0) * float(row['last_usd']) * (1 - STALE_HAIRCUT)
                    else:
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
    if scope == 'live':
        until = max(until, int(db.meta_get('loss_pause_until_paper_usdg', '0')))   # the USDG breaker's pause, if one stands
    return {'allowed': not unknown and losses < limit and now >= until,
            'loss_usd': round(losses, 4), 'limit_usd': limit, 'paused_until': until,
            'unpriced_positions': unknown}
