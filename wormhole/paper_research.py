"""Offline entry-filter research. No wallet, network, config or database imports.

Only numeric features saved at entry reach a predictor. Outcomes stay in evaluation.
This module is not called by the trading engine and cannot authorize an order.
"""
import hashlib
import json
import math

FILTER_VERSION = 'entry-risk-shadow-v1'
FEATURES = ('age_min', 'ret_p0', 'dd_peak', 'ret_5m', 'ret_15m', 'ret_60m', 'moves_15m',
            'fdv_usd', 'creator_tax_bps', 'creator_prev_launches', 'creator_rugged',
            'top10_pct', 'holders', 'snipe_pct', 'fleet_pct', 'losing_pct', 'crowd_history',
            'holders_kept_pct', 'swaps_15m', 'buy_share_15m', 'vol_15m_usd', 'score')
# Exploratory, deliberately fixed before prospective collection; not an optimized or proven strategy.
LIMITS = (('creator_rugged', '<=', 0), ('creator_prev_launches', '<=', 4),
          ('creator_tax_bps', '<=', 100), ('snipe_pct', '<=', 50), ('fleet_pct', '<=', 20),
          ('top10_pct', '<=', 50), ('holders', '>=', 50),
          ('ret_15m', '>=', -.20), ('dd_peak', '>=', -.40))
QUESTION = {'entry': {'type': 'choice',
    'instructions': 'Assess this proposed post-graduation token paper trade. Which assessment is best supported? '
                    'Positive return means proceeds after fees, slippage and gas exceed cost when the existing '
                    'stop/trailing/time-limit policy closes the position within 12 hours. '
                    'Wallet history is not proof of safety. Do not treat missing features as zero.',
    'criteria': {'A': 'Evidence favors rejecting the entry due to loss or manipulation risk.',
                 'B': 'Evidence favors testing the entry for a positive net return.',
                 'C': 'Evidence is insufficient to distinguish the two.'}}}


def entry_features(row):
    src = row.get('features') or {}
    return {k: float(src[k]) if isinstance(src.get(k), (int, float)) and not isinstance(src[k], bool)
            and math.isfinite(src[k]) else None for k in FEATURES}


def state(row):
    # Explicit allowlist: token names/descriptions, outcomes and current mutable metrics never reach inference.
    return json.dumps({'features_at_entry': entry_features(row),
                       'units': 'returns are fractions; *_pct are percentages; tax is basis points'},
                      sort_keys=True, separators=(',', ':'), allow_nan=False)


def risk_filter(row):
    f = entry_features(row)
    missing = [k for k, _, _ in LIMITS if f[k] is None]
    reasons = [k for k, op, limit in LIMITS if f[k] is not None and
               ((op == '<=' and f[k] > limit) or (op == '>=' and f[k] < limit))]
    return {'decision': 'skip' if reasons else 'abstain' if missing else 'keep',
            'reasons': reasons, 'missing': missing, 'version': FILTER_VERSION}


def laya_decision(result):
    answer = (result.get('answers') or {}).get('entry') or {}
    choice = answer.get('choice')
    return {'decision': {'A': 'skip', 'B': 'keep', 'C': 'abstain'}.get(choice, 'abstain'),
            'choice': choice if choice in ('A', 'B', 'C') else None}


def chronological_split(rows, fraction=.7):
    """Diagnostic split with creator purging and label settlement before holdout starts.

    This is not an untouched test set if the researcher already inspected its outcomes.
    Unknown creators cannot establish independence, so they are excluded.
    """
    ordered = sorted((r for r in rows if r.get('creator_group')), key=lambda r: (r['opened_ts'], r['id']))
    if len(ordered) < 2:
        return [], [], len(rows)
    cut = min(len(ordered)-1, max(1, int(len(ordered)*fraction)))
    cutoff = ordered[cut]['opened_ts']
    training = [r for r in ordered[:cut] if r.get('closed_ts') and r['closed_ts'] < cutoff]
    seen = {r['creator_group'] for r in ordered[:cut]}
    holdout = [r for r in ordered[cut:] if r['creator_group'] not in seen]
    return training, holdout, len(rows)-len(training)-len(holdout)


def report(rows, predictions):
    groups = {}
    for r in rows:
        spec = hashlib.sha256(json.dumps(r.get('execution_spec') or {}, sort_keys=True).encode()).hexdigest()[:16]
        policy = hashlib.sha256(json.dumps(r.get('policy_spec') or {}, sort_keys=True).encode()).hexdigest()[:16]
        key = (r['execution_model'], spec, policy, r['quote_asset'])
        g = groups.setdefault(key, {'model': key[0], 'execution_spec': spec, 'policy_spec': policy,
                                   'quote_asset': key[3], 'closed': 0, 'open': 0, 'baseline_pnl': 0,
                                   'kept': 0, 'kept_pnl': 0, 'kept_wins': 0, 'skipped': 0, 'abstained': 0})
        pnl = r.get('pnl_usd')
        if r['status'] != 'closed' or not isinstance(pnl, (float, int)) or not math.isfinite(pnl):
            g['open'] += 1
            continue
        g['closed'] += 1
        g['baseline_pnl'] += pnl
        decision = predictions.get(str(r['id']), {}).get('decision', 'abstain')
        if decision == 'keep':
            g['kept'] += 1; g['kept_pnl'] += pnl; g['kept_wins'] += int(pnl > 0)
        else:
            g['skipped' if decision == 'skip' else 'abstained'] += 1
    for g in groups.values():
        g['baseline_pnl'] = round(g['baseline_pnl'], 4)
        g['kept_pnl'] = round(g['kept_pnl'], 4)
    return {'status': 'exploratory_only', 'groups': list(groups.values()),
            'limitations': ['Post-hoc replay is not prospective evidence or permission to trade.',
                           'Skipped-trade savings are hypothetical; capacity and reinvestment are not simulated.',
                           'No rug labels are inferred from a losing trade.',
                           'Different execution versions, policies and quote assets remain separate.']}
