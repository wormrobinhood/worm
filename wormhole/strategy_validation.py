"""Prospective paper cohorts: the gate between the paper book and real money.

A trial freezes one strategy when it starts: an entry rule (watch.STRATEGIES, by name) and the exit policy
the book uses. Its members are the paper positions that rule opens afterwards, the first one per creator,
and a member's result is what the paper book realised: pool-quoted fills, gas and the token's own costs.
When the first COHORT_N members have all closed, the trial is evaluated once. Nothing is replaced and no
running bound is peeked at. All rules share one error budget: the k-th attempt (a rule's first cohort, or a
retry after a failure or an edit) is judged at TOTAL_ALPHA/(k(k+1)), so trying more rules, or retrying until
luck wins, gets harder each time. The next cohort starts as soon as one ends, so a pass is renewed by fresh
evidence (at the bar it passed at) or expires; editing the rule or the exit policy voids it.

These are paper fills against live pool quotes, not proof of executable profit at size."""
import json
import math
from statistics import NormalDist, mean, stdev
import time

from . import lab

COHORT_N = 50
VALID_FOR = 21 * 86400
TOTAL_ALPHA = .10               # one-sided; the k-th attempt may use 1/(k(k+1)) of it
LEGACY = 'superseded'           # trials of the sampled-path design this module replaced


def ensure_tables(db):
    db.x("CREATE TABLE IF NOT EXISTS strategy_trials(id INTEGER PRIMARY KEY, created INTEGER, cutoff INTEGER,"
         " arm TEXT, spec TEXT, baseline TEXT, status TEXT, result TEXT, completed INTEGER)")
    db.x("CREATE TABLE IF NOT EXISTS strategy_members(trial INTEGER,token TEXT,creator TEXT,t0 INTEGER,cost REAL,gas REAL,"
         " result TEXT,CONSTRAINT member_identity PRIMARY KEY(trial,token),UNIQUE(trial,creator))")
    db.x("CREATE TABLE IF NOT EXISTS strategy_ticks(trial INTEGER,token TEXT,ts INTEGER,price REAL,PRIMARY KEY(trial,token,ts))")
    have = {r['name'] for r in db.q('PRAGMA table_info(strategy_trials)')}
    for col in ('rule TEXT', 'k INTEGER'):
        if col.split()[0] not in have:
            db.x(f'ALTER TABLE strategy_trials ADD COLUMN {col}')
    db.x("UPDATE strategy_trials SET status=? WHERE rule IS NULL AND status='collecting'", (LEGACY,))


def frozen(rule):
    """The strategy as one canonical string: the entry rule and the exit policy new positions get."""
    policy, _ = lab.parse_arm(lab.DEFAULT)
    return json.dumps({'entry': rule, 'exit': policy, 'arm': lab.DEFAULT}, sort_keys=True)


def rules():
    from . import watch
    return list(watch.STRATEGIES)


def attempt(db, rule, spec):
    """The k a new cohort is judged at. Every attempt takes the next k from one budget shared by all rules:
    a rule's first cohort, and any retry after a failure or an edit. The renewal of a standing pass is no new
    attempt and keeps the k it passed at."""
    last = db.one('SELECT status,spec,k FROM strategy_trials WHERE rule=? ORDER BY id DESC LIMIT 1', (rule,))
    if last and last['status'] == 'passed' and last['spec'] == spec and last['k']:
        return last['k']
    return 1 + db.one('SELECT COALESCE(MAX(k),0) n FROM strategy_trials WHERE rule IS NOT NULL')['n']


def stage(db):
    """Every entry rule always has one cohort collecting. A trial whose frozen strategy no longer matches
    the code is voided, whether it was collecting or had passed."""
    ensure_tables(db)
    with db.transaction():
        for rule in rules():
            spec = frozen(rule)
            db.x("UPDATE strategy_trials SET status='voided', completed=? WHERE rule=? AND spec!=? AND status IN ('collecting','passed')",
                 (int(time.time()), rule['name'], spec))
            if db.one("SELECT 1 FROM strategy_trials WHERE rule=? AND status='collecting'", (rule['name'],)):
                continue
            # The next cohort of an unchanged strategy continues where the last one's members ended, so the
            # positions opened while that cohort was already full are not lost. They are taken in opening
            # order, never chosen. Anything else starts from the positions opened from now on.
            previous = db.one("SELECT id FROM strategy_trials WHERE rule=? AND spec=? AND status IN ('passed','failed') ORDER BY id DESC LIMIT 1",
                              (rule['name'], spec))
            cutoff = db.one('SELECT COALESCE(MAX(id),0) n FROM paper')['n']
            if previous:
                cutoff = db.one("SELECT COALESCE(MAX(p.id),?) n FROM strategy_members m JOIN paper p ON p.token=m.token WHERE m.trial=?",
                                (cutoff, previous['id']))['n']
            db.x("INSERT INTO strategy_trials(created,cutoff,arm,spec,baseline,status,rule,k) VALUES(?,?,?,?,'','collecting',?,?)",
                 (int(time.time()), cutoff, lab.DEFAULT, spec, rule['name'], attempt(db, rule['name'], spec)))


def _fresh(row):
    return time.time() - (row['completed'] or 0) < VALID_FOR


def admit(db, trial):
    """Paper positions this rule opened after the trial began, oldest first, the first one per creator."""
    if 'strategy' not in {r['name'] for r in db.q('PRAGMA table_info(paper)')}:
        return                                       # the paper book adds its columns when it starts
    exit_spec = json.dumps(json.loads(trial['spec'])['exit'], sort_keys=True)
    have = db.one('SELECT COUNT(*) n FROM strategy_members WHERE trial=?', (trial['id'],))['n']
    rows = db.q("SELECT p.id,p.token,p.opened_ts,p.policy_spec,p.gas_usd,p.cost,l.deployer FROM paper p LEFT JOIN launches l ON l.token=p.token"
                " WHERE p.id>? AND p.strategy=? AND p.token NOT IN (SELECT token FROM strategy_members WHERE trial=?) ORDER BY p.id",
                (trial['cutoff'], trial['rule'], trial['id']))
    for r in rows:
        if have >= COHORT_N:
            return
        try:
            same_exit = json.dumps(json.loads(r['policy_spec'] or 'null'), sort_keys=True) == exit_spec
        except ValueError:
            same_exit = False
        if not same_exit:
            continue
        creator = (r['deployer'] or r['token']).lower()          # an unknown creator can only ever count once per token
        have += db.xc('INSERT OR IGNORE INTO strategy_members(trial,token,creator,t0,cost,gas) VALUES(?,?,?,?,?,?)',
                      (trial['id'], r['token'], creator, r['opened_ts'], r['cost'], r['gas_usd']))


def settle(db, trial):
    for m in db.q('SELECT token FROM strategy_members WHERE trial=? AND result IS NULL', (trial['id'],)):
        p = db.one("SELECT status,pnl_usd,size_usd FROM paper WHERE token=?", (m['token'],))
        if not p or p['status'] != 'closed':
            continue
        ok = p['pnl_usd'] is not None and p['size_usd'] and math.isfinite(p['pnl_usd'] / p['size_usd'])
        result = {'valid': True, 'ret': p['pnl_usd'] / p['size_usd']} if ok else {'valid': False}
        db.x('UPDATE strategy_members SET result=? WHERE trial=? AND token=? AND result IS NULL',
             (json.dumps(result), trial['id'], m['token']))


def bound(values, z):
    return mean(values) - z * stdev(values) / math.sqrt(len(values))


def evaluate(db, trial):
    with db.transaction():
        members = db.q('SELECT result FROM strategy_members WHERE trial=? ORDER BY t0, token LIMIT ?', (trial['id'], COHORT_N))
        if len(members) != COHORT_N or any(m['result'] is None for m in members):
            return
        results = [json.loads(m['result']) for m in members]
        good = [r['ret'] for r in results if r['valid']]
        k = max(1, int(trial['k'] or 1))
        alpha = TOTAL_ALPHA / (k * (k + 1))
        z = NormalDist().inv_cdf(1 - alpha)
        out = {'n': len(good), 'required': COHORT_N, 'lcb': None, 'z': z, 'alpha': alpha, 'attempt': k}
        passed = False
        if len(good) == COHORT_N:                     # a member without a usable result is never replaced by a winner
            out.update(mean_ret=mean(good), lcb=bound(good, z), win_rate=sum(r > 0 for r in good) / len(good))
            passed = out['lcb'] > 0
        db.x("UPDATE strategy_trials SET status=?,result=?,completed=? WHERE id=? AND status='collecting'",
             ('passed' if passed else 'failed', json.dumps(out), int(time.time()), trial['id']))


def tick(db, rpc=None):
    stage(db)
    for trial in db.q("SELECT * FROM strategy_trials WHERE status='collecting' AND rule IS NOT NULL ORDER BY id"):
        admit(db, trial)
        settle(db, trial)
        evaluate(db, trial)
    stage(db)                                        # a cohort that just ended is followed by the next at once


def _view(db, row, rule):
    members = db.q('SELECT result FROM strategy_members WHERE trial=?', (row['id'],))
    result = json.loads(row['result'] or '{}')
    current = frozen(rule) == row['spec']
    passed = bool(row['status'] == 'passed' and current and _fresh(row))
    status = row['status'] if row['status'] != 'passed' or passed else ('expired' if current else 'voided')
    return {**result, 'trial': row['id'], 'attempt': row['k'], 'passed': passed, 'status': status,
            'n': sum(m['result'] is not None for m in members), 'enrolled': len(members), 'required': COHORT_N,
            'completed': row['completed']}


def summary(db):
    """`passed` is true while any entry rule holds a fresh pass (`passed_rules` names them). Per rule: `last`
    is its most recent finished cohort, `collecting` the one now filling. The top level repeats the rule that
    is furthest along, for the readiness panel."""
    ensure_tables(db)
    views = []
    for rule in rules():
        rows = [_view(db, r, rule) for r in db.q('SELECT * FROM strategy_trials WHERE rule=? ORDER BY id DESC', (rule['name'],))]
        if not rows:
            continue
        standing = next((v for v in rows if v['passed']), None)
        views.append({'rule': rule['name'], 'passed': bool(standing), 'standing_pass': standing,
                      'last': next((v for v in rows if v['status'] not in ('collecting', 'voided')), None),
                      'collecting': next((v for v in rows if v['status'] == 'collecting'), None)})
    passed_rules = [v['rule'] for v in views if v['passed']]

    def progress(v):
        c = v['collecting'] or {}
        return (v['passed'], c.get('n', 0), c.get('enrolled', 0))
    lead = max(views, key=progress, default=None)
    if not lead:
        return {'passed': False, 'status': 'not started', 'n': 0, 'enrolled': 0, 'required': COHORT_N,
                'arm': lab.DEFAULT, 'rules': [], 'passed_rules': []}
    shown = lead['standing_pass'] or lead['collecting'] or lead['last']
    last = lead['standing_pass'] or lead['last'] or {}
    return {'passed': bool(passed_rules), 'status': 'passed' if lead['passed'] else shown['status'], 'rule': lead['rule'],
            'n': (lead['collecting'] or shown)['n'], 'enrolled': (lead['collecting'] or shown)['enrolled'], 'required': COHORT_N,
            'mean_ret': last.get('mean_ret'), 'lcb': last.get('lcb'), 'arm': lab.DEFAULT, 'rules': views, 'passed_rules': passed_rules}
