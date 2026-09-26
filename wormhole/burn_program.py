"""The surplus burn program: an operator-armed sum from the treasury's surplus, released to the burn linearly
over a number of days and bought back in the same small, spaced slices as the burn share of every claim.

It follows the trading-profit sweep (treasury.sweep_trading_profit): a release is a 'surplus_burn' ledger row
that treasury.owed_to_burn counts, so the money is reserved from that moment and the burn itself is the usual
pool swap. The rows are tagged with the program's id, and each burn row carries how much of it was program
money (burns spend program money first), so released, burned and still-owed are exact sums of the ledger.

Arming and idempotency. The program lives in the database (meta 'burn_program'), never in the environment alone:
- WH_SURPLUS_BURN_USD > 0 arms the program named WH_SURPLUS_BURN_ID (default 'surplus-1'). The first cycle that
  sees it records the id, the amount, WH_SURPLUS_BURN_DAYS (default 7) and the start time. From then on those
  are frozen: a restart or redeploy, or an edited amount or length, never restarts, stretches or resizes it.
- What is released is the sum of the program's own ledger rows, written in one step with nothing else, so a
  crash between cycles can neither lose nor repeat a release.
- WH_SURPLUS_BURN_USD=0 (or unset) pauses releases. Money already released stays owed to the burn and is burned.
  Setting any amount again resumes on the original schedule: what fell due meanwhile is released at the next
  burn opportunity and still leaves in slices of at most WH_BURN_MAX_USD.
- An id is used once. A finished program stays finished whatever the amount says later; arming its id again
  does nothing. A later program needs a new WH_SURPLUS_BURN_ID and starts only after the current one has
  released and burned everything. While the environment names another id, an unfinished program is paused (never
  resumed by the new id, which would release its catch-up without being asked); naming it again resumes it.
- A release never takes the wallet's USDG below what is spoken for: every owed bucket and pending spend, the
  protected launch allocation and gas refills (treasury.owed_total), plus the 90-day runway reserve (budget).
  When the wallet is short the program waits and releases the rest later; it never borrows.

Always-on rounds (WH_SURPLUS_BURN_AUTO, on by default). Whenever no program is running and the wallet's USDG above
every reservation and the 90-day reserve reaches WH_SURPLUS_BURN_AUTO_MIN_USD (25), a round named 'auto-<start>'
is armed for exactly that spare, over WH_SURPLUS_BURN_DAYS, and released like any program. A round runs to its
end; the next one starts once new fees have built up spare again. A manual program still works and takes the
wallet first; an automatic round never starts while a manual one is unfinished, paused or not.

Releases happen at burn opportunities (treasury.burn_step, every WH_BURN_EVERY_MIN plus jitter) and at most once
per WH_BURN_EVERY_MIN, not on every five-minute tick: the ledger gets one release row per burn instead of hundreds
of cent-sized ones, and the public schedule (`scheduled_usd`) is exact to the second anyway."""
import json
import math
import os
import re
import time

from . import config as C
from . import treasury as T

KEY, SPENT = 'burn_program', 'burn_program_ids'
DONE_EPS = 0.01          # a program is released or burned in full within one cent


def settings():
    """(amount, days, id) from the environment; ValueError on anything invalid, which pauses the program."""
    from .claim_policy import setting
    amount = setting('WH_SURPLUS_BURN_USD', '0', 0, 1_000_000)
    days = setting('WH_SURPLUS_BURN_DAYS', '7', 1, 365)
    pid = os.environ.get('WH_SURPLUS_BURN_ID', 'surplus-1').strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,40}', pid):
        raise ValueError('invalid WH_SURPLUS_BURN_ID')
    return amount, days, pid


def auto_on():
    """Always-on rounds: every spare dollar above the reserve goes to the burn, a week at a time."""
    return os.environ.get('WH_SURPLUS_BURN_AUTO', '1').strip() == '1'


def auto_min():
    from .claim_policy import setting
    return setting('WH_SURPLUS_BURN_AUTO_MIN_USD', '25', 2, 100_000)


def is_auto(p):
    return bool(p) and str(p.get('id', '')).startswith('auto-')


def saved(db):
    try:
        return json.loads(db.meta_get(KEY) or 'null')
    except ValueError:
        return None


def released_usd(db, pid):
    T.ensure_tables(db)
    return db.one("SELECT COALESCE(SUM(amount),0) s FROM ledger WHERE kind='surplus_burn' AND program=?", (pid,))['s']


def burned_usd(db, pid, pending=False):
    kinds = ('burn', 'burn_pending') if pending else ('burn',)
    return db.one(f"SELECT COALESCE(SUM(program_usd),0) s FROM ledger WHERE kind IN ({','.join('?' * len(kinds))})"
                  " AND program=?", kinds + (pid,))['s']


def unburned(db):
    """(id, USDG) of released program money not yet burned or on its way; (None, 0) without a program."""
    p = saved(db)
    if not p:
        return None, 0.0
    return p['id'], max(0.0, released_usd(db, p['id']) - burned_usd(db, p['id'], pending=True))


def finished(db, p):
    return (released_usd(db, p['id']) >= p['total'] - DONE_EPS
            and burned_usd(db, p['id']) >= p['total'] - DONE_EPS)


def scheduled(p, now):
    """What the linear schedule has made due by `now`, in USDG."""
    span = p['days'] * 86400
    return p['total'] * min(1.0, max(0.0, now - p['started_ts']) / span)


def arm(db, now=None):
    """The program in force, starting it when the environment arms a new id. Returns (program or None, paused)."""
    now = int(time.time() if now is None else now)
    amount, days, pid = settings()
    p = saved(db)
    if is_auto(p) and not finished(db, p):
        return p, not auto_on()          # an automatic round runs to its end; a manual program waits for it
    if p and p['id'] == pid:
        return p, amount <= 0
    if amount <= 0:
        return p, True
    spent = json.loads(db.meta_get(SPENT) or '[]')
    # From here the environment names another id than the saved program's. That program is paused, never resumed
    # by the new id: resuming would release its whole catch-up at once, more than the operator asked for.
    if pid in spent:
        T._say_hourly(db, 'treasury', f'burn program {pid} already ran; a new program needs a new id')
        return p, True
    if p and not finished(db, p):
        T._say_hourly(db, 'treasury', f'burn program {pid} waits for {p["id"]} to finish; {p["id"]} is paused '
                                      f'until WH_SURPLUS_BURN_ID names it again')
        return p, True
    new = {'id': pid, 'total': round(amount, 2), 'days': days, 'started_ts': now, 'ends_ts': now + int(days * 86400)}
    with db.transaction():
        db.meta_set(KEY, json.dumps(new))
        db.meta_set(SPENT, json.dumps(spent + [pid]))
    db.add_event('treasury', f'burn program armed: ${new["total"]:,.2f} of surplus goes to the burn over {days:g} days, '
                             f'in small buys a few times a day')
    return new, False


def spare_usd(rpc, db):
    """USDG above every reservation and the 90-day reserve, to the cent below; None while a payment settles."""
    T.ensure_tables(db)
    if db.one("SELECT 1 FROM ledger WHERE kind LIKE '%_pending' AND tx IS NOT NULL"):
        return None
    room = T.usdg_balance(rpc, C.WALLET) - T.owed_total(db) - reserve_usd()
    return math.floor(room * 100 + 1e-6) / 100 if math.isfinite(room) else None


def arm_auto(rpc, db, now):
    """Start an automatic round for the spare above the reserve, when there is at least auto_min() of it.
    Returns the new program or None."""
    _amount, days, _pid = settings()
    spare = spare_usd(rpc, db)
    if spare is None or spare < auto_min():
        return None
    now = int(now)
    new = {'id': f'auto-{now}', 'total': spare, 'days': days, 'started_ts': now, 'ends_ts': now + int(days * 86400)}
    spent = json.loads(db.meta_get(SPENT) or '[]')
    with db.transaction():
        db.meta_set(KEY, json.dumps(new))
        db.meta_set(SPENT, json.dumps(spent + [new['id']]))
    db.add_event('treasury', f'burn round started: ${spare:,.2f} of spare money above the 90-day reserve goes to the '
                             f'burn over {days:g} days, in small buys a few times a day')
    return new


def reserve_usd():
    """The runway reserve a release may never touch: the planned costs of RESERVE_DAYS days."""
    from . import budget
    return (budget.COMPUTE_USD_DAY + budget.GAS_USD_DAY + budget.BRIDGE_USD_MONTH / 30) * budget.RESERVE_DAYS


def release(rpc, db, now=None):
    """Release what the schedule has made due, as far as the wallet's USDG above every reservation allows.
    Returns the USDG released now (0.0 when nothing was). Never raises for an unarmed program."""
    now = time.time() if now is None else now
    p, paused = arm(db, now)
    if (p is None or finished(db, p)) and auto_on():
        started = arm_auto(rpc, db, now)
        if started:
            p, paused = started, False
    if not p or paused:
        return 0.0
    done = released_usd(db, p['id'])
    due = math.floor((scheduled(p, now) - done) * 100 + 1e-6) / 100
    if due < DONE_EPS:
        return 0.0
    # One release per burn interval: a burn that keeps waiting (a thin pool, no quote) must not turn every
    # five-minute tick into a ledger row. After the end the rest goes at once.
    last = db.one("SELECT MAX(ts) t FROM ledger WHERE kind='surplus_burn' AND program=?", (p['id'],))['t']
    if last is not None and now - last < T.BURN_EVERY_MIN * 60 and now < p['ends_ts']:
        return 0.0
    # Settle first: a pending spend or an unverified claim makes the balance and the reservations unknown.
    # Only sends with a transaction count, as in the treasury cycle: a compute_pending row paid in USDC on Base has
    # no hash and can wait for days, and must not stall the program without a word.
    if db.one("SELECT 1 FROM ledger WHERE kind LIKE '%_pending' AND tx IS NOT NULL"):
        T._say_hourly(db, 'treasury', 'burn program waits: a payment is still settling')
        return 0.0
    usdg = T.usdg_balance(rpc, C.WALLET)
    room = usdg - T.owed_total(db) - reserve_usd()
    amount = math.floor(min(due, room) * 100 + 1e-6) / 100
    if not math.isfinite(amount) or amount < DONE_EPS:
        T._say_hourly(db, 'treasury', f'burn program waits: ${due:,.2f} is due, but the wallet holds nothing above '
                                      'its reserves and what it owes')
        return 0.0
    total = done + amount
    with db.transaction():
        db.x("INSERT INTO ledger(ts,kind,asset,amount,note,program) VALUES(?,?,?,?,?,?)",
             (int(now), 'surplus_burn', 'USDG', amount,
              f'burn program: ${total:,.2f} of ${p["total"]:,.2f} released to the burn, day '
              f'{min(p["days"], (now - p["started_ts"]) / 86400):.1f} of {p["days"]:g}', p['id']))
    if amount < due:
        T._say_hourly(db, 'treasury', f'burn program released ${amount:,.2f} of ${due:,.2f} due: the rest waits above the reserve')
    return amount


def burns(db, pid, limit=500):
    """The program's settled burns, oldest first: [{ts, usd, qty, tx}]. A slice can also carry the regular burn
    share, so usd is the program's part of it and qty the $WORM bought with that part (in proportion)."""
    rows = db.q("SELECT ts, amount, program_usd, qty, tx FROM ledger WHERE kind='burn' AND program=?"
                " AND program_usd>0 ORDER BY id LIMIT ?", (pid, limit))
    return [{'ts': r['ts'], 'usd': round(r['program_usd'], 2),
             'qty': round((r['qty'] or 0.0) * r['program_usd'] / r['amount'], 2) if r['amount'] else 0.0,
             'tx': r['tx']} for r in rows]


def reason(days, auto=False):
    span = 'a week' if days == 7 else f'{days:g} days'
    if auto:
        return (f"Whenever the treasury holds more than its 90-day reserve needs, the extra is burned in small buys "
                f"over {span}.")
    return (f"The treasury holds more than its 90-day reserve needs, so the extra is burned in small buys "
            f"over {span}.")


def status(db, now=None):
    """The public view for the page. Reads only: arming happens in the treasury cycle. interval_s is the
    configured spacing between burns (each gap is 1 to 1.5 times it, drawn privately per burn); nothing here
    says when the next burn happens."""
    now = time.time() if now is None else now
    try:
        _amount, days, _pid = settings()
    except ValueError:
        days = None
    interval = int(T.BURN_EVERY_MIN * 60)
    p = saved(db)
    if not p:
        return {'active': False, 'state': 'off', 'total_usd': 0.0, 'released_usd': 0.0, 'burned_usd': 0.0,
                'burned_qty': 0.0, 'scheduled_usd': 0.0, 'started_ts': None, 'ends_ts': None, 'days': days,
                'interval_s': interval, 'reason': None, 'auto': auto_on(), 'burns': []}
    released, burned = released_usd(db, p['id']), burned_usd(db, p['id'])
    try:
        amount, _days, pid = settings()
    except ValueError:
        amount, pid = 0, None
    if is_auto(p):
        amount = 1 if auto_on() else 0      # an automatic round pauses only when rounds are switched off
    elif pid != p['id']:
        amount = 0          # another id is named: this program is paused (see arm)
    done = released >= p['total'] - DONE_EPS and burned >= p['total'] - DONE_EPS
    state = ('finished' if done else 'burning the rest' if released >= p['total'] - DONE_EPS
             else 'paused' if amount <= 0 else 'releasing')
    listed = burns(db, p['id'])
    return {'active': not done, 'state': state, 'total_usd': round(p['total'], 2), 'released_usd': round(released, 2),
            'burned_usd': round(burned, 2), 'burned_qty': round(sum(b['qty'] for b in listed), 2),
            'scheduled_usd': round(scheduled(p, now), 2), 'started_ts': p['started_ts'], 'ends_ts': p['ends_ts'],
            'days': p['days'], 'interval_s': interval, 'reason': reason(p['days'], is_auto(p)), 'auto': is_auto(p), 'burns': listed}
