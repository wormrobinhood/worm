import json
import time
import pytest
from wormhole import config as C, lab, strategy_validation as V
from test_trader import tok


def assessment(db, i, when, creator=None):
    token = tok(i)
    db.x('INSERT INTO launches(token,deployer,ts,symbol) VALUES(?,?,?,?)', (token, creator or tok(i + 1000), when, 'T'))
    aid = db.insert("INSERT INTO assessments(token,score,verdict,scored_at,partial,metrics) VALUES(?,80,'looks healthy',?,0,'{}')", (token, when))
    db.x("INSERT INTO outcomes(token,score,verdict,scored_at,price0,assessment_id) VALUES(?,80,'looks healthy',?,1,?)", (token, when, aid))
    return token


def trial(db):
    V.stage(db)
    return db.one('SELECT * FROM strategy_trials')


def finish(db, t, n=30, candidate=.2, baseline=.1, missing=False):
    for i in range(n):
        result = {'valid': not (missing and i == n-1), 'candidate': candidate + i * .0001, 'baseline': baseline}
        db.x('INSERT INTO strategy_members(trial,token,creator,t0,cost,gas,result) VALUES(?,?,?,?,?,?,?)',
             (t['id'], tok(i), tok(i+1000), t['created']+1, .04, .01, json.dumps(result)))
    V.evaluate(db, t)


def test_no_peeking_or_replacement(db):
    t = trial(db)
    finish(db, t, n=29)
    assert V.summary(db)['status'] == 'collecting' and not V.summary(db)['passed']
    db.x('INSERT INTO strategy_members(trial,token,creator,result) VALUES(?,?,?,?)',
         (t['id'], tok(99), tok(999), json.dumps({'valid': False})))
    V.evaluate(db, t)
    assert V.summary(db)['status'] == 'failed' and not V.summary(db)['passed']


def test_frozen_winner_must_beat_baseline_and_expire(db, monkeypatch):
    monkeypatch.setattr(lab, 'research_policy', lambda db: ('trail_35@0m', 'learned'))
    t = trial(db)
    finish(db, t)
    assert V.summary(db)['passed'] and lab.current_policy(db)[0] == 'trail_35@0m'
    monkeypatch.setattr(V.time, 'time', lambda: t['created'] + V.VALID_FOR + 10)
    assert not V.summary(db)['passed'] and lab.current_policy(db)[0] == lab.DEFAULT


def test_mutating_policy_invalidates_promotion(db, monkeypatch):
    t = trial(db)
    finish(db, t)
    assert V.summary(db)['passed']
    monkeypatch.setitem(lab.POLICIES, 'costout_1.5x', {**lab.POLICIES['costout_1.5x'], 'stop': -.99})
    assert not V.summary(db)['passed']


def test_positive_but_inferior_candidate_fails(db, monkeypatch):
    monkeypatch.setattr(lab, 'research_policy', lambda db: ('trail_35@0m', 'learned'))
    t = trial(db)
    finish(db, t, candidate=.1, baseline=.2)
    assert not V.summary(db)['passed']


def test_admission_excludes_old_tokens_and_seen_or_duplicate_creators(db, monkeypatch):
    now = int(time.time())
    assessment(db, 1, now-100, tok(1001))
    t = trial(db)
    db.x('UPDATE strategy_trials SET created=?', (now-2,))
    t = db.one('SELECT * FROM strategy_trials')
    assessment(db, 2, now-1, tok(1001))  # creator in research history
    assessment(db, 3, now-1, tok(1003))
    assessment(db, 4, now-1, tok(1003))  # same creator in future cohort
    monkeypatch.setattr(V.execution, 'entry', lambda *a: {'gas_usd': .1, 'price': 1})
    V.admit(db, t, object())
    assert [r['token'] for r in db.q('SELECT token FROM strategy_members')] == [tok(3)]
    assert V.summary(db)['enrolled'] == 1


def test_full_forward_path_resolves_and_missing_ticks_fail(db, monkeypatch):
    t = trial(db)
    start = t['created']
    db.x('INSERT INTO strategy_members(trial,token,creator,t0,cost,gas) VALUES(?,?,?,?,?,?)', (t['id'], tok(1), tok(1001), start, .04, .01))
    db.many('INSERT INTO strategy_ticks VALUES(?,?,?,?)', [(t['id'], tok(1), start + step, 1 + step / lab.HORIZON_S)
                                                       for step in range(0, lab.HORIZON_S + 1, 300)])
    monkeypatch.setattr(V.time, 'time', lambda: start + lab.HORIZON_S + 1)
    monkeypatch.setattr(V, 'token_prices', lambda tokens: {})
    V.tick(db)
    assert json.loads(db.one('SELECT result FROM strategy_members')['result'])['valid']
    assert not V.summary(db)['passed']  # one outcome cannot unlock 30-case gate


def test_gas_cost_applies_to_every_cash_leg():
    path = [(0, 1), (300, 1.6), (600, 2), (900, .9)]
    spec, delay = lab.parse_arm(lab.DEFAULT)
    normal = lab.simulate(lab.DEFAULT, path, 0)
    withgas = lab.simulate(lab.DEFAULT, path, 0, policy=spec, delay=delay, gas_per_side=.01)
    assert normal - withgas == pytest.approx(.03)  # buy, take profit, trailing exit
