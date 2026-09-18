"""Frozen prospective scoring-rule evaluation. Never controls trading or signs transactions."""
import json
import time

COHORT_N = 80
MAX_PENDING = 5
TOTAL_ALPHA = 0.02


def ensure_tables(db):
    db.x("CREATE TABLE IF NOT EXISTS shadow_rules(id INTEGER PRIMARY KEY AUTOINCREMENT, created INTEGER,"
         " cutoff INTEGER, run_id INTEGER, spec TEXT, excluded_creators TEXT, status TEXT DEFAULT 'shadow',"
         " cohort TEXT, result TEXT, rule_id TEXT)")


def stage(db, spec, run_id, history):
    ensure_tables(db)
    with db.transaction():
        if db.one("SELECT COUNT(*) n FROM shadow_rules WHERE status='shadow'")['n'] >= MAX_PENDING:
            return None
        cutoff = db.one('SELECT COALESCE(MAX(id),0) n FROM assessments')['n']
        return db.insert("INSERT INTO shadow_rules(created,cutoff,run_id,spec,excluded_creators) VALUES(?,?,?,?,?)",
                         (int(time.time()), cutoff, run_id, json.dumps(spec,sort_keys=True),
                          json.dumps(sorted({h['creator'] for h in history if h.get('creator')}))))


def evaluate(db):
    from . import advisor as A
    ensure_tables(db)
    for candidate in db.q("SELECT * FROM shadow_rules WHERE status='shadow' ORDER BY id"):
        # Select the cohort BEFORE inspecting any outcome; use first assessment/launch per creator.
        cohort = json.loads(candidate['cohort'] or '[]')
        if not cohort:
            excluded = set(json.loads(candidate['excluded_creators'] or '[]'))
            seen, chosen = set(), []
            for r in db.q("SELECT a.id,l.deployer FROM assessments a JOIN outcomes o ON o.assessment_id=a.id"
                          " JOIN launches l ON l.token=a.token WHERE a.id>? AND a.scored_at>? AND l.ts>?"
                          " AND a.partial=0 AND a.engine_version='evidence-v2' ORDER BY a.id",
                          (candidate['cutoff'],candidate['created'],candidate['created'])):
                creator = r['deployer']
                if not creator or creator in excluded or creator in seen:
                    continue
                seen.add(creator); chosen.append(r['id'])
                if len(chosen) == COHORT_N:
                    break
            if len(chosen) < COHORT_N:
                continue
            cohort = chosen
            db.x("UPDATE shadow_rules SET cohort=? WHERE id=? AND cohort IS NULL", (json.dumps(cohort),candidate['id']))
        placeholders = ','.join('?' for _ in cohort)
        rows = db.q("SELECT a.token,a.metrics,o.change_pct,o.outcome,o.resolved,o.baseline_ts FROM assessments a"
                    f" JOIN outcomes o ON o.assessment_id=a.id WHERE a.id IN ({placeholders})", tuple(cohort))
        if len(rows) != COHORT_N or any(not r['resolved'] for r in rows):
            continue
        hist = [{'token':r['token'],'m':json.loads(r['metrics']),'change':r['change_pct']}
                for r in rows if r['outcome'] != 'unknown' and r['change_pct'] is not None and r['baseline_ts'] is not None]
        spec = json.loads(candidate['spec'])
        fired = [h['change'] for h in hist if A.matches(spec,h['m'])]
        other = [h['change'] for h in hist if not A.matches(spec,h['m'])]
        # Alpha spending across the lifetime of candidates; never repeatedly test a growing sample.
        ordinal = candidate['id']
        alpha = TOTAL_ALPHA/(ordinal*(ordinal+1))
        result = {'n_fired':len(fired),'n_other':len(other),'cohort_n':COHORT_N,
                  'unknown':COHORT_N-len(hist),'alpha':alpha,'accepted':False}
        if len(fired) >= A.MIN_N and len(other) >= A.MIN_N:
            perms = min(20000,max(2000,int(2/alpha)))
            test = spec.get('test') if spec.get('test') in A.TESTS else 'median'   # the measure frozen at staging
            diff,p = A.judge(fired,other,perms=perms,seed=ordinal,stat=test)
            p = (round(p*perms)+1)/(perms+1)  # finite randomization must never report probability zero
            result.update(diff=diff,p=p,permutations=perms,test=test)
            result['accepted'] = bool(A.separated(test,diff,spec['points']) and p <= alpha)
        result['why'] = 'passed fixed future cohort' if result['accepted'] else 'future cohort did not establish sufficient evidence'
        with db.transaction():
            current = db.one("SELECT status FROM shadow_rules WHERE id=?",(ordinal,))
            if current['status'] != 'shadow':
                continue
            rid = None
            if result['accepted'] and db.one("SELECT COUNT(*) n FROM learned_rules WHERE active=1")['n'] < A.MAX_RULES:
                rid = A.adopt_rule(db,spec,result,candidate['run_id'])
            status = 'promoted' if rid else ('validated' if result['accepted'] else 'rejected')
            db.x('UPDATE shadow_rules SET status=?,result=?,rule_id=? WHERE id=?',
                 (status,json.dumps(result),rid,ordinal))
            db.add_event('advisor',f"shadow rule {ordinal}: {status}; {result['why']}")


def summary(db):
    ensure_tables(db)
    rows = db.q('SELECT id,created,status,result,rule_id FROM shadow_rules ORDER BY id DESC LIMIT 12')
    for r in rows:
        r['result'] = json.loads(r['result'] or '{}')
    return {'cohort_size':COHORT_N,'pending':db.one("SELECT COUNT(*) n FROM shadow_rules WHERE status='shadow'")['n'],
            'rules':rows,'policy':'Frozen future launches, one per creator; unknown results excluded, no repeated peeking. Trading remains operator-controlled.'}
