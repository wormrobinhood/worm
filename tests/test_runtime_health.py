import threading
from types import SimpleNamespace

from wormhole import runtime_health as H


def hub(now=1000):
    return SimpleNamespace(started_at=now, indexer=SimpleNamespace(last_ok=now, ready=threading.Event()))


def test_backfill_must_progress_and_no_progress_restarts(db):
    h = hub()
    assert H.status(db, h, now=1100)['ok']
    assert not H.status(db, h, now=1300)['ok']
    assert H.should_restart(db, h, now=2000)


def test_full_disk_blocks_readiness_without_restart_loop(db, monkeypatch):
    monkeypatch.setattr(H.shutil, 'disk_usage', lambda _: SimpleNamespace(total=500 * H.MIB, free=0))
    h = hub()
    st = H.status(db, h, now=2000)
    assert not st['ok'] and 'storage_critical' in st['codes']
    assert not H.should_restart(db, h, now=2000)


def test_finishing_startup_backfill_does_not_hide_the_remaining_chain_gap(db, monkeypatch):
    h = hub()
    h.indexer.ready.set()
    h.indexer.last_ok = 2000
    h.indexer.anchor = (20_000, 2000)
    h.indexer.block_time = .1
    h.indexer.last_indexed_block = 10_000
    monkeypatch.setattr(H.time, 'time', lambda: 2000)
    assert H.status(db, h)['chain_lag_s'] == 1000
    assert H.status(db, h)['catching_up']
    assert not H.trading_ready(db, h)
    h.indexer.last_indexed_block = 19_990
    assert not H.status(db, h)['catching_up']
    assert H.trading_ready(db, h)


def test_worker_and_write_failures_do_not_need_rpc_or_database_reads(db):
    h = hub()
    h.worker_limits = {'positions': 180}
    h.worker_heartbeats = {'positions': 1000}
    h.database_probe = {'at': 1290, 'ok': False}
    h.indexer.last_ok = 1300
    assert set(H.status(db, h, now=1300)['codes']) == {'positions_stale', 'database_unwritable'}
    h.worker_heartbeats['positions'] = 1300
    h.database_probe = {'at': 1300, 'ok': True}
    assert H.status(db, h, now=1300)['ok']


def test_write_probe_checks_a_real_commit_and_missing_database_fails_closed(db, tmp_path):
    assert H.probe_database(db.path)
    assert db.meta_get('health_write_at') is not None
    assert not H.probe_database(tmp_path / 'missing.db')
    assert not (tmp_path / 'missing.db').exists()


def test_monitor_logs_even_when_database_cannot_be_written(db, monkeypatch, caplog):
    h = hub()
    monkeypatch.setattr(H, 'probe_database', lambda _: False)
    monkeypatch.setattr(H.time, 'time', lambda: 1000)
    def stop(_):
        raise InterruptedError
    monkeypatch.setattr(H.time, 'sleep', stop)
    import pytest
    with pytest.raises(InterruptedError):
        H.monitor(db, h)
    assert 'database_unwritable' in caplog.text
    assert 'worm_health_alert' in caplog.text


def test_sqlite_full_does_not_mask_error_or_leave_partial_transaction(db):
    import sqlite3
    import pytest
    db.x('CREATE TABLE fill_test(value BLOB)')
    pages = db.one('PRAGMA page_count')['page_count']
    db.c.execute(f'PRAGMA max_page_count={pages + 1}')
    with pytest.raises(sqlite3.OperationalError, match='full'):
        with db.transaction():
            db.meta_set('must_rollback', 'temporary')
            db.x('INSERT INTO fill_test(value) VALUES(?)', (bytes(1024 * 1024),))
    assert db._depth == 0 and not db.c.in_transaction
    assert db.meta_get('must_rollback') is None
    db.c.execute('PRAGMA max_page_count=10000')
    db.meta_set('recovered', 'yes')
    assert db.meta_get('recovered') == 'yes'
