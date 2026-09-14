"""Audit regressions: deterministic failures in temporary databases, never live transactions."""
import json
import time

import pytest

from wormhole import advisor, lab, learn, prices, readiness
from wormhole.db import DB
from wormhole.jobs import ScanQueue, enqueue, LEASE_S
from wormhole.paper import Paper
from test_learn import result
from test_lab import case

TOKEN = '0x' + 'a7' * 20


def row(db):
    return db.one('SELECT * FROM outcomes WHERE token=?', (TOKEN,))


@pytest.mark.parametrize('entry', [
    {'price_usd': 1, 'age_s': 901}, {'price_usd': 1, 'age_s': None},
    {'price_usd': 1, 'age_s': -1}, {'price_usd': float('nan'), 'age_s': 0},
    {'price_usd': float('inf'), 'age_s': 0}, {'price_usd': -1, 'age_s': 0},
    {'price_usd': 0, 'age_s': 0}, {'price_usd': 1, 'age_s': float('nan')},
])
def test_unusable_quotes_are_rejected(entry):
    assert prices.usable_price(entry) is None


def test_successful_omission_invalidates_old_quote(monkeypatch):
    from test_prices import Resp
    monkeypatch.setattr(prices, '_cache', {TOKEN: (time.time()-7200, {'price_usd': 1})})
    monkeypatch.setattr(prices, '_get', lambda *a: Resp(200, {'data': []}))
    assert prices.usable_price(prices.token_prices([TOKEN])[TOKEN]) is None


def test_stale_quotes_cannot_open_paper_or_create_lab_ticks(db, monkeypatch):
    from wormhole import paper
    now = int(time.time())
    stale = {TOKEN: {'price_usd': 2, 'age_s': 7200}}
    monkeypatch.setattr(lab, 'token_prices', lambda tokens: stale)
    monkeypatch.setattr(paper, 'token_prices', lambda tokens: stale)
    case(db, TOKEN, now-300, [(now-300, 1)])
    lab.tick(db)
    assert db.one('SELECT COUNT(*) n FROM ticks')['n'] == 1
    Paper(db).consider(TOKEN, result(80, 'looks healthy', 2))
    assert db.one('SELECT COUNT(*) n FROM paper')['n'] == 0


def test_cached_quote_keeps_its_observation_time(db, monkeypatch):
    now = int(time.time())
    case(db, TOKEN, now-300, [(now-300, 1)])
    monkeypatch.setattr(lab, 'token_prices', lambda tokens: {TOKEN: {'price_usd': 2, 'age_s': 120}})
    lab.tick(db)
    assert abs(db.one('SELECT MAX(ts) t FROM ticks')['t'] - (now-120)) <= 1


def test_baseline_alone_never_proves_a_flat_outcome(db):
    b = learn.Brain(db)
    b.record(TOKEN, result(80, 'looks healthy', None))
    start = row(db)['scored_at']
    b._advance(row(db), 1, start+600)
    b._advance(row(db), None, start+30*3600+1)
    assert row(db)['outcome'] == 'unknown' and row(db)['change_pct'] is None


def test_rescan_cannot_claim_credit_for_original_loss(db):
    b = learn.Brain(db)
    b.record(TOKEN, result(80, 'looks healthy', 1, buyers=10))
    before = row(db)
    b.record(TOKEN, result(5, 'avoid', .1, snipe=-15))
    assert row(db) == before
    b._advance(row(db), .1, before['scored_at']+86401)
    assert 'missed it' in db.one("SELECT text FROM events WHERE kind='lesson'")['text']


def test_failure_rolls_back_all_learning_and_retry_counts_once(db, monkeypatch):
    b = learn.Brain(db)
    b.record(TOKEN, result(5, 'avoid', 1, snipe=-15))
    original = db.x
    def fail(sql, args=()):
        if sql.startswith('UPDATE outcomes SET outcome='):
            raise RuntimeError('injected failure')
        return original(sql, args)
    with monkeypatch.context() as m:
        m.setattr(db, 'x', fail)
        with pytest.raises(RuntimeError):
            b._resolve(row(db), 'rugged', -90)
    assert b.weights()['snipe'] == 1 and not row(db)['resolved']
    b._resolve(row(db), 'rugged', -90)
    b._resolve(row(db), 'rugged', -90)
    assert b.weights()['snipe'] == 1.02
    assert db.one("SELECT hits FROM rules WHERE id='snipe'")['hits'] == 1
    assert db.one("SELECT COUNT(*) n FROM events WHERE kind='lesson'")['n'] == 1


def test_lab_failure_rolls_back_arm_aggregates(db, monkeypatch):
    now = int(time.time())
    start = now-lab.HORIZON_S
    case(db, TOKEN, start, [(start, 1), (start+300, 2), (now, 2)])
    original = db.x
    def fail(sql, args=()):
        if sql.startswith("UPDATE lab_cases SET status='resolved'"):
            raise RuntimeError('injected failure')
        return original(sql, args)
    c = db.one('SELECT * FROM lab_cases')
    with monkeypatch.context() as m:
        m.setattr(db, 'x', fail)
        with pytest.raises(RuntimeError):
            lab._resolve(db, c)
    assert db.one('SELECT SUM(n) n FROM lab_arms')['n'] == 0
    assert db.one('SELECT status FROM lab_cases')['status'] == 'active'
    lab._resolve(db, c)
    counts = db.q('SELECT name,n FROM lab_arms ORDER BY name')
    lab._resolve(db, c)
    assert db.q('SELECT name,n FROM lab_arms ORDER BY name') == counts
    assert any(a['n'] for a in counts)


def test_readiness_cannot_be_bought_with_treasury():
    from test_readiness import RUNWAY_OK, _lab
    r = readiness.compute({'scorecard': {'avoid': {'rugged': 20}}}, _lab(30, .2, lcb=.1), RUNWAY_OK)
    assert r['score'] == 82 and not r['ready']
    r = readiness.compute({'scorecard': {'avoid': {'flat': 10, 'rugged': 10},
                                        'looks healthy': {'flat': 10, 'rugged': 10}}}, _lab(30, .2, lcb=.1), RUNWAY_OK)
    assert not r['ready']


def test_advisor_uses_original_features_not_latest_card(db):
    b = learn.Brain(db)
    first = result(80, 'looks healthy', 1)
    first['metrics']['top10_pct'] = 10
    b.record(TOKEN, first)
    b._resolve(row(db), 'rugged', -90)
    db.x('INSERT INTO scores(token,metrics,scored_at) VALUES(?,?,?)',
         (TOKEN, json.dumps({'top10_pct': 99}), int(time.time())+10))
    assert advisor.history(db)[0]['m']['top10_pct'] == 10
    db.x('UPDATE outcomes SET assessment_id=NULL WHERE token=?', (TOKEN,))
    assert advisor.history(db) == []  # legacy provenance is not invented


def test_rug_monitoring_between_checkpoints_uses_distinct_observations(db):
    b = learn.Brain(db)
    b.record(TOKEN, result(80, 'looks healthy', 1))
    start = row(db)['scored_at']
    assert b._due(row(db), start+600)
    b._advance(row(db), .1, start+600, start+600)
    b._advance(row(db), .1, start+900, start+600)  # same cached observation cannot confirm
    assert not row(db)['resolved']
    b._advance(row(db), .1, start+901, start+901)
    assert row(db)['outcome'] == 'rugged'


def test_durable_jobs_survive_restart_and_have_bounded_retries(tmp_path):
    path = tmp_path/'restart.db'
    d = DB(path); q = ScanQueue(d)
    enqueue(d, TOKEN, now=100)
    q.put(TOKEN)  # pending duplicate does not reset attempts
    job = q.claim(now=100)
    assert job['attempts'] == 1
    d.c.close()
    d = DB(path); q = ScanQueue(d)
    assert q.claim(now=101) is None
    recovered = q.claim(now=100+LEASE_S)
    assert recovered['attempts'] == 2
    q.finish(job, True)  # stale attempt cannot acknowledge the recovered lease
    assert d.one('SELECT state FROM scan_jobs')['state'] == 'leased'
    q.finish(recovered, False, retry=True, now=2000)
    assert q.claim(now=2500) is None
    last = q.claim(now=2600)
    q.finish(last, False, retry=True, now=2601)
    assert d.one('SELECT state,attempts FROM scan_jobs') == {'state':'failed','attempts':3}
    assert q.claim(now=99999) is None
    q.put(TOKEN)  # explicit request permits retrying failed work
    assert d.one('SELECT state,attempts FROM scan_jobs') == {'state':'pending','attempts':0}


def test_graduation_job_survives_callback_failure(db):
    from test_indexer import Chain, launched, graduated, T1
    from wormhole.indexer import Indexer
    rpc = Chain(head=300, logs=[launched(T1, 100), graduated(T1, 200)])
    def broken(token):
        raise RuntimeError('callback failed')
    idx = Indexer(rpc, db, on_graduation=broken)
    with pytest.raises(RuntimeError):
        idx._ingest(90, 210, live=True)
    assert db.one('SELECT graduated FROM launches WHERE token=?', (T1,))['graduated'] == 1
    assert db.one('SELECT state FROM scan_jobs WHERE token=?', (T1,))['state'] == 'pending'
    idx._ingest(90, 210, live=True)
    assert db.one('SELECT COUNT(*) n FROM scan_jobs')['n'] == 1


def test_score_saved_before_crash_remains_the_evaluated_assessment(db):
    now = int(time.time())
    aid = db.insert("INSERT INTO assessments(token,score,verdict,metrics,scored_at,partial,fired,engine_version)"
                    " VALUES(?,80,'looks healthy',?, ?,0,'[]','evidence-v2')",
                    (TOKEN, json.dumps({'price_usd': 1}), now-300))
    b = learn.Brain(db)
    b.record(TOKEN, result(5, 'avoid', .1))
    assert row(db)['assessment_id'] == aid and row(db)['score'] == 80
    assert row(db)['scored_at'] == now-300


def test_legacy_results_are_visible_but_not_new_readiness_evidence(db):
    b = learn.Brain(db)
    db.x("INSERT INTO outcomes(token,score,verdict,scored_at,price0,checks,outcome,resolved,fired)"
         " VALUES(?,80,'looks healthy',?,1,'{}','flat',1,'[]')", (TOKEN, int(time.time())-86400))
    summary = b.summary()
    assert summary['scorecard']['looks healthy']['flat'] == 1
    assert summary['validated_scorecard'] == {}
    from test_readiness import RUNWAY_OK, _lab
    assert readiness.compute(summary, _lab(30, .2, lcb=.1), RUNWAY_OK)['parts'][1]['checked'] == 0


def test_db_migration_preserves_legacy_outcomes(tmp_path):
    import sqlite3
    path = tmp_path/'old.db'
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE outcomes(token TEXT PRIMARY KEY,score INTEGER,verdict TEXT,scored_at INTEGER,'
                 'price0 REAL,checks TEXT,outcome TEXT,change_pct REAL,resolved INTEGER,fired TEXT)')
    conn.execute("INSERT INTO outcomes VALUES(?,80,'looks healthy',1,1,'{}','flat',0,1,'[]')", (TOKEN,))
    conn.commit(); conn.close()
    d = DB(path)
    old = row(d)
    assert old['outcome'] == 'flat' and old['resolved'] == 1 and old['assessment_id'] is None
    assert old['baseline_ts'] is None
    d.c.close()
    d = DB(path)
    assert row(d) == old


def test_backfill_after_downtime_enqueues_only_new_graduations(db):
    from test_indexer import Chain, launched, graduated, T1
    from wormhole.indexer import Indexer
    db.meta_set('last_block', 90)
    rpc = Chain(head=300, logs=[launched(T1,100), graduated(T1,200)])
    idx = Indexer(rpc, db)
    idx.backfill()
    assert db.one('SELECT state FROM scan_jobs WHERE token=?', (T1,))['state'] == 'pending'


def test_graduation_and_job_creation_roll_back_together(db, monkeypatch):
    from test_indexer import Chain, launched, graduated, T1
    from wormhole.indexer import Indexer
    rpc = Chain(head=300, logs=[launched(T1,100), graduated(T1,200)])
    idx = Indexer(rpc, db)
    original = db.x
    def fail(sql, args=()):
        if sql.startswith('INSERT OR IGNORE INTO scan_jobs'):
            raise RuntimeError('injected job write failure')
        return original(sql, args)
    with monkeypatch.context() as m:
        m.setattr(db, 'x', fail)
        with pytest.raises(RuntimeError):
            idx._ingest(90,210,live=True)
    assert db.one('SELECT graduated FROM launches WHERE token=?', (T1,))['graduated'] == 0
    assert db.one('SELECT COUNT(*) n FROM scan_jobs')['n'] == 0
    idx._ingest(90,210,live=True)
    assert db.one('SELECT state FROM scan_jobs')['state'] == 'pending'


def test_refresh_rejects_stale_baselines(db, monkeypatch):
    now = int(time.time())
    db.x('INSERT INTO scores(token,scored_at,metrics) VALUES(?,?,?)', (TOKEN,now-100,'{}'))
    db.x('INSERT INTO outcomes(token,scored_at,resolved) VALUES(?,?,0)', (TOKEN,now-100))
    monkeypatch.setattr(prices,'token_prices',lambda tokens: {TOKEN:{'price_usd':1,'age_s':7200}})
    assert prices.refresh_scored(db) == 0
    assert row(db)['price0'] is None
    assert json.loads(db.one('SELECT metrics FROM scores')['metrics']) == {}
