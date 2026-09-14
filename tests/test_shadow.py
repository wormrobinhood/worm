import json
import time
from wormhole import advisor as A, shadow, config as C, trader

SPEC={'conditions':[{'metric':'top10_pct','op':'>=','value':60}],'points':-8}


def stage(db):
    A.ensure_tables(db)
    sid=shadow.stage(db,SPEC,1,[])
    return sid,db.one('SELECT created FROM shadow_rules WHERE id=?',(sid,))['created']


def future(db, start, i, high, resolved=True, creator=None, unknown=False):
    token=f'case-{i}'
    creator=creator or f'creator-{i}'
    db.x('INSERT INTO launches(token,deployer,ts) VALUES(?,?,?)',(token,creator,start+1))
    aid=db.insert("INSERT INTO assessments(token,metrics,scored_at,partial,engine_version) VALUES(?,?,?,0,'evidence-v2')",
                  (token,json.dumps({'top10_pct':90 if high else 10}),start+2))
    db.x('INSERT INTO outcomes(token,assessment_id,scored_at,baseline_ts,resolved,outcome,change_pct) VALUES(?,?,?,?,?,?,?)',
         (token,aid,start+2,start+2,int(resolved),'unknown' if unknown else ('rugged' if high else 'flat'),None if unknown else (-90-i*.01 if high else i*.01)))


def test_no_promotion_before_complete_fixed_future_cohort(db):
    sid,start=stage(db)
    for i in range(80): future(db,start,i,i%2==0,resolved=i!=79)
    shadow.evaluate(db)
    assert db.one('SELECT status FROM shadow_rules')['status']=='shadow'
    assert not A.apply(db,{'top10_pct':90})
    before=json.loads(db.one('SELECT cohort FROM shadow_rules')['cohort'])
    # Later results cannot replace a still-pending selected member.
    future(db,start,80,False)
    shadow.evaluate(db)
    assert json.loads(db.one('SELECT cohort FROM shadow_rules')['cohort'])==before
    db.x("UPDATE outcomes SET resolved=1 WHERE token='case-79'")
    shadow.evaluate(db)
    assert db.one('SELECT status FROM shadow_rules')['status']=='promoted'
    assert A.apply(db,{'top10_pct':90})
    assert db.one('SELECT COUNT(*) n FROM learned_rules')['n']==1
    shadow.evaluate(db)
    assert db.one('SELECT COUNT(*) n FROM learned_rules')['n']==1
    result=json.loads(db.one('SELECT result FROM shadow_rules')['result'])
    assert 0<result['p']<=result['alpha']


def test_old_launches_repeated_creators_and_unknowns_cannot_supply_evidence(db):
    _,start=stage(db)
    for i in range(80): future(db,start,i,i%2==0,creator='same')
    shadow.evaluate(db)
    assert db.one('SELECT cohort FROM shadow_rules')['cohort'] is None
    db.x("UPDATE launches SET deployer=token,ts=?",(start-1,))
    shadow.evaluate(db)
    assert db.one('SELECT cohort FROM shadow_rules')['cohort'] is None
    db.x('UPDATE launches SET ts=?',(start+1,))
    db.x("UPDATE outcomes SET outcome='unknown',change_pct=NULL")
    shadow.evaluate(db)
    assert db.one('SELECT status FROM shadow_rules')['status']=='rejected'
    assert not A.apply(db,{'top10_pct':90})


def test_failed_future_rule_stays_rejected_when_later_data_looks_good(db):
    _,start=stage(db)
    for i in range(80): future(db,start,i,i%2==0)
    db.x('UPDATE outcomes SET change_pct=0')
    shadow.evaluate(db)
    assert db.one('SELECT status FROM shadow_rules')['status']=='rejected'
    for i in range(80,160): future(db,start,i,i%2==0)
    shadow.evaluate(db)
    assert db.one('SELECT status FROM shadow_rules')['status']=='rejected'


def test_trading_off_blocks_entries_even_with_perfect_readiness(db,monkeypatch):
    monkeypatch.setattr(C,'TRADING',False)
    monkeypatch.setattr(C,'WALLET','0x'+'22'*20)
    class NoRPC:
        def __getattr__(self,name):
            raise AssertionError('Trading off must not reach RPC')
    class NoSigner:
        def sign_transaction(self,*args):
            raise AssertionError('Trading off must not sign')
    trader.decide(NoRPC(),db,{'can_invest':True,'surplus_usd':10000},True,NoSigner(),{'ready':True,'score':100})
    assert db.one("SELECT text FROM events WHERE kind='trade'")['text'].startswith('trading is off')
