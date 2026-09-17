"""Prospective strategy trials, separate from the lab's exploratory rankings.

Freeze one candidate and its baseline before admission. Observe 30 future tokens from distinct,
previously unseen creators. Never replace missing outcomes with winners or repeatedly peek at a
running confidence bound. These remain sampled simulations, not proof of executable profits.
"""
import json
import math
from statistics import NormalDist, mean, stdev
import time

from . import config as C, lab, trade_checks as execution
from .prices import token_prices, usable_price, observed_at

COHORT_N = 30
VALID_FOR = 7 * 86400
MAX_GAP = 15 * 60
TOTAL_ALPHA = .025


def ensure_tables(db):
    db.x("CREATE TABLE IF NOT EXISTS strategy_trials(id INTEGER PRIMARY KEY, created INTEGER, cutoff INTEGER,"
         " arm TEXT, spec TEXT, baseline TEXT, status TEXT, result TEXT, completed INTEGER)")
    db.x("CREATE TABLE IF NOT EXISTS strategy_members(trial INTEGER,token TEXT,creator TEXT,t0 INTEGER,cost REAL,gas REAL,"
         " result TEXT,CONSTRAINT member_identity PRIMARY KEY(trial,token),UNIQUE(trial,creator))")
    db.x("CREATE TABLE IF NOT EXISTS strategy_ticks(trial INTEGER,token TEXT,ts INTEGER,price REAL,PRIMARY KEY(trial,token,ts))")


def frozen(arm):
    policy, delay = lab.parse_arm(arm)
    return json.dumps({'policy': policy, 'delay': delay}, sort_keys=True)


def stage(db):
    ensure_tables(db)
    with db.transaction():
        row = db.one('SELECT * FROM strategy_trials ORDER BY id DESC LIMIT 1')
        cutoff = db.one('SELECT COALESCE(MAX(id),0) n FROM assessments')['n']
        if row:
            if row['status'] == 'collecting' or summary(db)['passed'] or cutoff <= row['cutoff']:
                return
        arm, _ = lab.research_policy(db)
        db.x("INSERT INTO strategy_trials(created,cutoff,arm,spec,baseline,status) VALUES(?,?,?,?,?,'collecting')",
             (int(time.time()), cutoff, arm, frozen(arm), frozen(lab.DEFAULT)))


def admit(db, trial, rpc):
    slots = COHORT_N - db.one('SELECT COUNT(*) n FROM strategy_members WHERE trial=?', (trial['id'],))['n']
    if slots <= 0:
        return
    # Original complete assessments only; neither re-scoring an old token nor reusing a creator
    # from the research history qualifies as unseen evidence.
    rows = db.q("SELECT a.*,l.deployer,l.ts launch_ts FROM outcomes o JOIN assessments a ON a.id=o.assessment_id"
                " JOIN launches l ON l.token=a.token WHERE a.id>? AND a.scored_at>? AND l.ts>? AND a.partial=0"
                " AND a.verdict='looks healthy' AND a.score>=? AND l.deployer IS NOT NULL"
                " AND lower(l.deployer) NOT IN (SELECT lower(l2.deployer) FROM assessments a2 JOIN launches l2 ON l2.token=a2.token"
                " WHERE a2.id<=? AND l2.deployer IS NOT NULL)"
                " AND a.token NOT IN (SELECT token FROM strategy_members WHERE trial=?)"
                " AND lower(l.deployer) NOT IN (SELECT creator FROM strategy_members WHERE trial=?) ORDER BY a.id",
                (trial['cutoff'], trial['created'], trial['created'], execution.MIN_SCORE, trial['cutoff'], trial['id'], trial['id']))
    for row in rows:
        if slots <= 0:
            break
        if time.time() - row['scored_at'] > MAX_GAP or not execution.eligible(row['token'], row):
            continue
        try:
            quote = execution.entry(rpc, db, row['token'], C.PAPER_SIZE_USD)
        except Exception:
            continue
        now = int(time.time())
        with db.transaction():
            # A concurrent admission must not exceed the fixed sample or repeat a creator.
            if db.one('SELECT COUNT(*) n FROM strategy_members WHERE trial=?', (trial['id'],))['n'] >= COHORT_N:
                return
            db.x('INSERT OR IGNORE INTO strategy_members(trial,token,creator,t0,cost,gas) VALUES(?,?,?,?,?,?)',
                 (trial['id'], row['token'], row['deployer'].lower(), now,
                  lab.token_cost(db, row['token']) + .02, quote['gas_usd'] / C.PAPER_SIZE_USD))
            db.x('INSERT OR IGNORE INTO strategy_ticks VALUES(?,?,?,?)', (trial['id'], row['token'], now, quote['price']))
        slots -= 1


def bound(values, z):
    return mean(values) - z * stdev(values) / math.sqrt(len(values))


def tick(db, rpc=None):
    stage(db)
    trial = db.one("SELECT * FROM strategy_trials WHERE status='collecting' ORDER BY id DESC LIMIT 1")
    if not trial:
        return
    admit(db, trial, rpc)
    members = db.q('SELECT * FROM strategy_members WHERE trial=? AND result IS NULL', (trial['id'],))
    prices = token_prices([m['token'] for m in members]) if members else {}
    now = int(time.time())
    for member in members:
        item = prices.get(member['token']) or {}
        price = usable_price(item)
        if price is not None:
            ts = observed_at(item, now)
            if member['t0'] <= ts <= member['t0'] + lab.HORIZON_S:
                db.x('INSERT OR IGNORE INTO strategy_ticks VALUES(?,?,?,?)', (trial['id'], member['token'], ts, price))
        if now < member['t0'] + lab.HORIZON_S:
            continue
        path = [(r['ts'], r['price']) for r in db.q('SELECT ts,price FROM strategy_ticks WHERE trial=? AND token=? ORDER BY ts',
                                                  (trial['id'], member['token']))]
        complete = (len(path) >= 3 and path[-1][0] >= member['t0'] + lab.HORIZON_S - MAX_GAP
                    and all(b[0] - a[0] <= MAX_GAP for a, b in zip(path, path[1:])))
        result = {'valid': False}
        if complete:
            returns = []
            for spec in (trial['spec'], trial['baseline']):
                spec = json.loads(spec)
                returns.append(lab.simulate(trial['arm'], path, member['t0'], member['cost'],
                                            policy=spec['policy'], delay=spec['delay'], gas_per_side=member['gas']))
            if all(r is not None and math.isfinite(r) for r in returns):
                result = {'valid': True, 'candidate': returns[0], 'baseline': returns[1]}
        db.x('UPDATE strategy_members SET result=? WHERE trial=? AND token=? AND result IS NULL',
             (json.dumps(result), trial['id'], member['token']))
    evaluate(db, trial)


def evaluate(db, trial):
    with db.transaction():
        members = db.q('SELECT result FROM strategy_members WHERE trial=?', (trial['id'],))
        if len(members) != COHORT_N or any(m['result'] is None for m in members):
            return
        results = [json.loads(m['result']) for m in members]
        good = [r for r in results if r['valid']]
        # A decreasing error budget across trials discourages retrying indefinitely until luck wins.
        # The normal approximation assumes independent finite-variance samples, not a profit guarantee.
        alpha = TOTAL_ALPHA / (trial['id'] * (trial['id'] + 1)) / 2
        z = max(2.0, NormalDist().inv_cdf(1 - alpha))
        out = {'n': len(good), 'required': COHORT_N, 'lcb': None, 'paired_lcb': None, 'z': z}
        passed = False
        if len(good) == COHORT_N:
            values = [r['candidate'] for r in good]
            out.update(mean_ret=mean(values), lcb=bound(values, z),
                       paired_lcb=bound([r['candidate'] - r['baseline'] for r in good], z))
            passed = out['lcb'] > 0 and (trial['spec'] == trial['baseline'] or out['paired_lcb'] > 0)
        db.x("UPDATE strategy_trials SET status=?,result=?,completed=? WHERE id=? AND status='collecting'",
             ('passed' if passed else 'failed', json.dumps(out), int(time.time()), trial['id']))


def summary(db):
    ensure_tables(db)
    row = db.one('SELECT * FROM strategy_trials ORDER BY id DESC LIMIT 1')
    if not row:
        return {'passed': False, 'status': 'not started', 'n': 0, 'enrolled': 0, 'required': COHORT_N}
    members = db.q('SELECT result FROM strategy_members WHERE trial=?', (row['id'],))
    result = json.loads(row['result'] or '{}')
    current = False
    try:
        current = frozen(row['arm']) == row['spec'] and frozen(lab.DEFAULT) == row['baseline']
    except KeyError:
        pass
    passed = bool(row['status'] == 'passed' and current and time.time() - (row['completed'] or 0) < VALID_FOR)
    return {**result, 'passed': passed, 'status': row['status'] if row['status'] != 'passed' or passed else 'expired',
            'arm': row['arm'], 'n': result.get('n', sum(m['result'] is not None for m in members)),
            'enrolled': len(members), 'required': COHORT_N, 'completed': row['completed']}
