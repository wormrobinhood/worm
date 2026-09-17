import time
from wormhole import trader, trade_risk as R
from test_trader import position, tok


def test_loss_pause_is_latched_and_wins_do_not_cancel_losses(db):
    trader.ensure_tables(db)
    position(db, tok(1), 'live', 1, qty=10)
    position(db, tok(2), 'live', 1, qty=10)
    db.x("UPDATE positions SET status='closed',closed_ts=?,realized_usd=0 WHERE token=?", (int(time.time()), tok(1)))
    db.x("UPDATE positions SET status='closed',closed_ts=?,realized_usd=100 WHERE token=?", (int(time.time()), tok(2)))
    risk = R.check(db)
    assert not risk['allowed'] and risk['loss_usd'] == 10 and risk['paused_until'] > time.time()
    db.x('UPDATE positions SET closed_ts=?', (int(time.time())-86401,))
    assert not R.check(db)['allowed']


def test_unknown_live_holdings_block_only_entries(db):
    trader.ensure_tables(db)
    position(db, tok(1), 'live', 1, qty=10)
    assert not R.check(db)['allowed']
    assert R.check(db, marks={tok(1): 10})['allowed']


def test_status_read_does_not_mutate_circuit_breaker(db):
    trader.ensure_tables(db)
    position(db, tok(1), 'live', 1, qty=10)
    assert not R.check(db, marks={tok(1): 0}, latch=False)['allowed']
    assert db.meta_get('loss_pause_until_live') is None


def test_failed_approval_attempts_reserve_gas_without_a_position(db):
    trader.ensure_tables(db)
    db.x("INSERT INTO trade_attempts(ts,token,side,gas_usd) VALUES(?,?,'buy',10)", (int(time.time()), tok(1)))
    assert not R.check(db)['allowed'] and R.check(db)['loss_usd'] == 10
