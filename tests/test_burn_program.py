"""The surplus burn program (linear release, frozen in the database, never below the reserve) and small, spaced,
impact-checked burn slices. Offline: the fake chain and a throwaway database only."""
import json
import time
from types import SimpleNamespace

import pytest
from eth_abi import decode

from fakes import decode_tx
from wormhole import burn_program as BP, config as C, trader, treasury as T
from test_treasury import (QUOTE_SEL, TOKEN, burn_chain, burn_receipts, chain, ledger, linear_quote, pool, rows)

DAY = 86400
T0 = 1_900_000_000


@pytest.fixture(autouse=True)
def fresh_prices(monkeypatch):
    from wormhole import prices, claim_policy
    monkeypatch.setattr(prices, 'eth_usd', lambda **kw: 2500.0)
    monkeypatch.setattr(claim_policy, 'eth_usd', lambda **kw: 2500.0)
    monkeypatch.setattr(claim_policy, 'funded_runway', lambda *args: None)


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv('WH_SURPLUS_BURN_USD', '530')
    monkeypatch.setenv('WH_SURPLUS_BURN_DAYS', '7')
    monkeypatch.delenv('WH_SURPLUS_BURN_ID', raising=False)


def released(db):
    return BP.released_usd(db, 'surplus-1')


def settled_burn(db, usd, program_usd, qty=1000.0, pid='surplus-1'):
    T.ensure_tables(db)
    db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note,qty,program,program_usd) VALUES(?,?,?,?,?,?,?,?,?)",
         (T0, 'burn', 'USDG', usd, '0x' + 'ab' * 32, 'burn', qty, pid, program_usd))


# ---- the program: linear, idempotent, never below the reserve ------------------------------------------------------

def test_nothing_is_armed_by_default(db, rpc):
    chain(rpc, usdg=5000)
    assert BP.release(rpc, db, T0) == 0.0 and BP.saved(db) is None
    st = BP.status(db, T0)
    assert st['active'] is False and st['state'] == 'off' and st['burns'] == [] and st['reason'] is None


def test_release_is_linear_over_seven_days(db, rpc, armed):
    chain(rpc, usdg=5000)
    assert BP.release(rpc, db, T0) == 0.0                         # armed now: nothing is due yet
    p = BP.saved(db)
    assert (p['total'], p['days'], p['started_ts'], p['ends_ts']) == (530, 7, T0, T0 + 7 * DAY)
    assert BP.release(rpc, db, T0 + DAY) == pytest.approx(75.71)
    assert BP.release(rpc, db, T0 + 3.5 * DAY) == pytest.approx(265 - 75.71)
    assert released(db) == pytest.approx(265.0)
    assert T.owed_to_burn(db) == pytest.approx(265.0)            # released money is owed to the burn, reserved
    assert BP.release(rpc, db, T0 + 7 * DAY) == pytest.approx(265.0)
    assert BP.release(rpc, db, T0 + 30 * DAY) == 0.0              # never more than the total
    assert released(db) == pytest.approx(530.0)
    notes = [r['note'] for r in rows(db, 'surplus_burn')]
    assert len(notes) == 3 and '$530.00 of $530.00 released' in notes[-1]


def test_restart_and_redeploy_never_restart_or_double_count(db, rpc, armed, monkeypatch, tmp_path):
    from wormhole.db import DB
    chain(rpc, usdg=5000)
    BP.release(rpc, db, T0)
    assert BP.release(rpc, db, T0 + DAY) == pytest.approx(75.71)
    assert BP.release(rpc, db, T0 + DAY) == 0.0                   # the same moment again: nothing new
    again = DB(tmp_path / 'test.db')                              # the process restarts on the same volume
    monkeypatch.setenv('WH_SURPLUS_BURN_USD', '600')              # and the operator edits the amount and length
    monkeypatch.setenv('WH_SURPLUS_BURN_DAYS', '3')
    assert BP.release(rpc, again, T0 + DAY) == 0.0
    p = BP.saved(again)
    assert (p['total'], p['days'], p['started_ts']) == (530, 7, T0)   # frozen at arming
    assert BP.release(rpc, again, T0 + 2 * DAY) == pytest.approx(75.71)       # floored to the cent, never over
    assert released(again) == pytest.approx(530 * 2 / 7, abs=0.01)


def test_release_never_takes_usdg_below_the_reserve(db, rpc, armed, monkeypatch):
    from wormhole import launch_allocation
    ledger(db, 'claim', 100.0)                                     # 50 creator + 20 burn + 10 gold owed: 80
    db.meta_set(launch_allocation.KEY, json.dumps({'state': 'prepared', 'quote_units': 10_000_000}))  # 10 protected
    chain(rpc, usdg=250)
    BP.release(rpc, db, T0)
    reserve = BP.reserve_usd()
    assert reserve == pytest.approx((0.75 + 0.35) * 90)            # compute and gas, burns' gas included
    got = BP.release(rpc, db, T0 + 7 * DAY)                        # all 530 due, 250 in the wallet
    assert got == pytest.approx(250 - 90 - reserve, abs=0.01)
    assert 250 - T.owed_total(db) >= reserve - 1e-9               # the reserve is intact
    assert BP.release(rpc, db, T0 + 7 * DAY) == 0.0                # short: it waits, it never borrows
    assert 'burn program waits' in db.one("SELECT text FROM events ORDER BY id DESC LIMIT 1")['text']
    chain(rpc, usdg=1000)                                          # fees arrive: the rest follows
    assert released(db) + BP.release(rpc, db, T0 + 7 * DAY + 60) == pytest.approx(530.0)
    assert 1000 - T.owed_total(db) >= reserve - 1e-9


def test_at_most_one_release_per_burn_interval_until_the_end(db, rpc, armed):
    chain(rpc, usdg=5000)
    BP.release(rpc, db, T0)
    assert BP.release(rpc, db, T0 + DAY) > 0
    assert BP.release(rpc, db, T0 + DAY + 300) == 0.0              # a waiting burn: no row every five minutes
    assert BP.release(rpc, db, T0 + DAY + T.BURN_EVERY_MIN * 60) > 0
    assert BP.release(rpc, db, T0 + 7 * DAY) > 0
    assert BP.release(rpc, db, T0 + 7 * DAY + 60) == 0.0 and released(db) == pytest.approx(530.0)


def test_nothing_is_released_while_a_payment_is_unsettled(db, rpc, armed):
    chain(rpc, usdg=5000)
    BP.release(rpc, db, T0)
    ledger(db, 'forward_pending', 1.0, tx='0x' + 'cc' * 32)
    assert BP.release(rpc, db, T0 + DAY) == 0.0 and released(db) == 0.0


def test_zero_pauses_and_resumes_on_the_original_schedule(db, rpc, armed, monkeypatch):
    chain(rpc, usdg=5000)
    BP.release(rpc, db, T0)
    monkeypatch.setenv('WH_SURPLUS_BURN_USD', '0')
    assert BP.release(rpc, db, T0 + DAY) == 0.0 and BP.status(db, T0 + DAY)['state'] == 'paused'
    monkeypatch.setenv('WH_SURPLUS_BURN_USD', '530')
    assert BP.release(rpc, db, T0 + 2 * DAY) == pytest.approx(151.42)   # what fell due meanwhile, no stretch
    assert BP.saved(db)['ends_ts'] == T0 + 7 * DAY


def test_program_finishes_and_changing_the_amount_never_reruns_it(db, rpc, armed, monkeypatch):
    chain(rpc, usdg=5000)
    BP.release(rpc, db, T0)
    BP.release(rpc, db, T0 + 8 * DAY)
    assert BP.status(db, T0 + 8 * DAY)['state'] == 'burning the rest'
    settled_burn(db, 25.0, 25.0, qty=2000.0)
    settled_burn(db, 505.0, 505.0, qty=40000.0)
    st = BP.status(db, T0 + 8 * DAY)
    assert st['active'] is False and st['state'] == 'finished' and st['burned_usd'] == 530.0
    for amount in ('530', '900'):                                   # the same id: never again, whatever the amount
        monkeypatch.setenv('WH_SURPLUS_BURN_USD', amount)
        assert BP.release(rpc, db, T0 + 20 * DAY) == 0.0
    assert released(db) == pytest.approx(530.0) and BP.saved(db)['total'] == 530
    monkeypatch.setenv('WH_SURPLUS_BURN_ID', 'surplus-2')           # a new id starts a new program after the first
    BP.release(rpc, db, T0 + 20 * DAY)
    assert BP.saved(db)['id'] == 'surplus-2' and BP.saved(db)['total'] == 900
    monkeypatch.setenv('WH_SURPLUS_BURN_ID', 'surplus-1')           # an id is spent once
    BP.release(rpc, db, T0 + 21 * DAY)
    assert BP.saved(db)['id'] == 'surplus-2'


def test_a_new_id_waits_for_the_running_program(db, rpc, armed, monkeypatch):
    chain(rpc, usdg=5000)
    BP.release(rpc, db, T0)
    monkeypatch.setenv('WH_SURPLUS_BURN_ID', 'surplus-2')
    BP.release(rpc, db, T0 + DAY)
    assert BP.saved(db)['id'] == 'surplus-1' and released(db) == 0.0         # another id pauses the running one
    assert BP.status(db, T0 + DAY)['state'] == 'paused'
    assert 'waits for surplus-1' in db.one("SELECT text FROM events ORDER BY id DESC LIMIT 1")['text']
    monkeypatch.setenv('WH_SURPLUS_BURN_ID', 'surplus-1')                  # naming it again resumes it
    BP.release(rpc, db, T0 + DAY)
    assert released(db) == pytest.approx(75.71)


def test_invalid_settings_release_nothing(db, rpc, monkeypatch):
    chain(rpc, usdg=5000)
    T.ensure_tables(db)
    monkeypatch.setenv('WH_SURPLUS_BURN_USD', 'lots')
    with pytest.raises(ValueError):
        BP.release(rpc, db, T0)
    assert BP.saved(db) is None and rows(db, 'surplus_burn') == []


# ---- the burn: slices, spacing, impact -----------------------------------------------------------------------------

def burn_setup(db, rpc, monkeypatch, claim=500.0, usdg=1000.0):
    monkeypatch.setattr(C, 'TOKEN', TOKEN)
    monkeypatch.setattr(C, 'OWNER_WALLET', '')                     # no forward: the cycle's transactions are the burn's
    pool(db)
    ledger(db, 'claim', claim)
    burn_chain(rpc, usdg=usdg)
    burn_receipts(rpc)


def swap_amount(raw):
    commands, inputs, _deadline = decode(['bytes', 'bytes[]', 'uint256'], decode_tx(raw)['data'][4:])
    _actions, params = decode(['bytes', 'bytes[]'], inputs[0])
    return decode([trader.SWAP_T], params[0])[0][2]


def router_swaps(rpc):
    return [r for r in rpc.raw if decode_tx(r)['to'] == C.UNIVERSAL_ROUTER]


def test_defaults_are_small_and_often():
    assert (T.MIN_BURN_USD, T.BURN_MAX_USD, T.BURN_EVERY_MIN, T.BURN_MAX_IMPACT, T.BURN_SLIPPAGE) == (2, 25, 180, 0.02, 0.02)


def test_a_slice_is_capped_and_the_next_waits_for_its_spaced_moment(db, rpc, acct, live, monkeypatch):
    burn_setup(db, rpc, monkeypatch)                               # 100 owed to the burn
    before = time.time()
    T.cycle(rpc, db, acct)
    swaps = router_swaps(rpc)
    assert len(swaps) == 1 and swap_amount(swaps[0]) == 25_000_000
    assert rows(db, 'burn')[0]['amount'] == 25.0 and T.owed_to_burn(db) == pytest.approx(75.0)
    at = float(db.meta_get('burn_next_at'))
    assert before + 180 * 60 <= at <= time.time() + 270 * 60
    T.cycle(rpc, db, acct)                                         # the next cycle is too early
    assert len(router_swaps(rpc)) == 1
    db.meta_set('burn_next_at', repr(time.time() - 1))             # its moment has come
    T.cycle(rpc, db, acct)
    assert len(router_swaps(rpc)) == 2 and T.owed_to_burn(db) == pytest.approx(50.0)


def test_spacing_is_jittered_between_one_and_one_and_a_half_intervals():
    gaps = [T.next_burn_at(0) for _ in range(200)]
    assert all(180 * 60 <= g <= 270 * 60 for g in gaps)
    assert max(gaps) - min(gaps) > 30 * 60                         # not one fixed, predictable moment


def test_a_slice_shrinks_until_its_price_impact_fits(db, rpc, acct, live, monkeypatch):
    burn_setup(db, rpc, monkeypatch)
    rpc.eth_calls[QUOTE_SEL] = linear_quote(12_345, impact=0.012)  # 1.2% at $6, 5% at $25
    assert T.burn(rpc, db, acct)
    n = swap_amount(router_swaps(rpc)[0])
    assert T.units(T.MIN_BURN_USD) <= n < 25_000_000
    assert 0.012 * n / 6_000_000 <= T.BURN_MAX_IMPACT               # the slice sent fits the limit
    assert rows(db, 'burn')[0]['amount'] == n / 1e6


def test_a_pool_too_thin_for_the_minimum_slice_makes_the_burn_wait(db, rpc, acct, live, monkeypatch):
    burn_setup(db, rpc, monkeypatch)
    rpc.eth_calls[QUOTE_SEL] = linear_quote(12_345, impact=0.2)    # 20% at $6: nothing at $2 fits 2%
    assert not T.burn(rpc, db, acct)
    assert rpc.raw == [] and rows(db, 'burn_pending') == [] and T.owed_to_burn(db) == pytest.approx(100.0)
    assert 'too thin' in db.one("SELECT text FROM events ORDER BY id DESC LIMIT 1")['text']
    assert db.meta_get('burn_next_at') is None                      # nothing was sent: the clock is untouched


def test_a_pool_that_moved_during_the_approvals_makes_the_burn_wait(db, rpc, acct, live, monkeypatch):
    burn_setup(db, rpc, monkeypatch)
    fine, thin = linear_quote(12_345), linear_quote(12_345, impact=0.2)
    rpc.eth_calls[QUOTE_SEL] = lambda params: (thin if rpc.raw else fine)(params)
    assert not T.burn(rpc, db, acct)
    assert router_swaps(rpc) == [] and rows(db, 'burn_pending') == []
    assert 'pool moved' in db.one("SELECT text FROM events ORDER BY id DESC LIMIT 1")['text']


def test_program_money_is_burned_first_and_counted_exactly(db, rpc, acct, live, monkeypatch, armed):
    burn_setup(db, rpc, monkeypatch, claim=50.0, usdg=5000)         # the regular share owes 10
    BP.release(rpc, db, time.time() - 3 * DAY)                     # armed three days ago
    BP.release(rpc, db, time.time())                               # 227.14 released now
    assert T.owed_to_burn(db) == pytest.approx(10 + 530 * 3 / 7, abs=0.02)
    assert T.burn(rpc, db, acct)
    b = rows(db, 'burn')[0]
    assert b['amount'] == 25.0 and b['program'] == 'surplus-1' and b['program_usd'] == 25.0
    assert 'from the surplus burn program' in b['note']
    st = BP.status(db)
    assert st['burned_usd'] == 25.0
    assert st['burns'] == [{'ts': b['ts'], 'usd': 25.0, 'qty': 12_345.0, 'tx': b['tx']}]   # the fake receipt burns 12,345
    assert st['burned_qty'] == 12_345.0


def test_a_failed_burn_returns_its_program_money(db, rpc, acct, live, monkeypatch, armed):
    burn_setup(db, rpc, monkeypatch, claim=0.0, usdg=5000)
    db.x("DELETE FROM ledger")
    BP.release(rpc, db, time.time() - DAY)
    BP.release(rpc, db, time.time())
    before = BP.unburned(db)[1]
    base = rpc.receipt_for

    def reverted_swap(h):                                           # approvals succeed, the swap reverts
        r = base(h)
        raw = next((x for x in rpc.raw if T.outbox.get(h) and x == T.outbox.get(h)['raw']), None)
        if r and raw and decode_tx(raw)['to'] == C.UNIVERSAL_ROUTER:
            r = dict(r, status='0x0', logs=[])
        return r
    rpc.receipt_for = reverted_swap
    assert not T.burn(rpc, db, acct)
    assert rows(db, 'burn_failed') and BP.unburned(db)[1] == pytest.approx(before)
    assert BP.status(db)['burned_usd'] == 0.0


# ---- the public snapshot: shape, and no clock ----------------------------------------------------------------------

def test_public_treasury_has_the_program_the_history_and_no_next_moment(db, rpc, armed, monkeypatch):
    chain(rpc, usdg=5000)
    BP.release(rpc, db, T0)
    BP.release(rpc, db, T0 + DAY)
    settled_burn(db, 20.0, 12.0, qty=1000.0)
    settled_burn(db, 5.0, 0.0, qty=300.0, pid=None)
    db.meta_set('burn_next_at', repr(time.time() + 12345))
    db.meta_set('claim_policy_status', json.dumps({'mode': 'funded_daily', 'reason': 'daily interval: 90 days funded',
                                                   'next_claim_after': 1_900_086_400, 'balance_since': 1_900_000_000,
                                                   'threshold_usdg': 5, 'due': False, 'allowed': False}))
    s = T.summary(rpc, db)
    for key in ('burned_total', 'burned_qty', 'owed_to_burn', 'burn_min_usd', 'ledger', 'claimed_total', 'gold_usd'):
        assert key in s
    assert s['burn_max_usd'] == 25 and s['burn_min_usd'] == 2
    bp = s['burn_program']
    assert set(bp) >= {'active', 'total_usd', 'released_usd', 'burned_usd', 'started_ts', 'ends_ts', 'days',
                       'burns', 'burned_qty', 'interval_s', 'reason'}
    assert (bp['active'], bp['total_usd'], bp['released_usd'], bp['burned_usd']) == (True, 530.0, 75.71, 12.0)
    assert (bp['started_ts'], bp['ends_ts'], bp['days'], bp['interval_s']) == (T0, T0 + 7 * DAY, 7, 10800)
    assert bp['burns'] == [{'ts': T0, 'usd': 12.0, 'qty': 600.0, 'tx': '0x' + 'ab' * 32}] and bp['burned_qty'] == 600.0
    assert bp['reason'].startswith('The treasury holds more than its 90-day reserve needs') and 'a week' in bp['reason']
    assert [h['usd'] for h in s['burn_history']] == [20.0, 5.0] and set(s['burn_history'][0]) == {'ts', 'usd', 'qty', 'tx'}
    assert s['claim_policy'] == {'mode': 'funded_daily', 'reason': 'daily interval: 90 days funded',
                                 'threshold_usdg': 5, 'due': False, 'allowed': False}
    text = json.dumps(s)
    assert 'next_claim_after' not in text and 'balance_since' not in text and 'burn_next_at' not in text
    assert str(int(float(db.meta_get('burn_next_at')))) not in text


def test_burn_history_is_bounded_and_oldest_first(db, rpc):
    chain(rpc)
    T.ensure_tables(db)
    for i in range(T.BURN_HISTORY_MAX + 5):
        db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,qty) VALUES(?,?,?,?,?,?)", (i, 'burn', 'USDG', 1.0, f'0x{i:064x}', 10.0))
    h = T.summary(rpc, db)['burn_history']
    assert len(h) == T.BURN_HISTORY_MAX and h[0]['ts'] == 5 and h[-1]['ts'] == T.BURN_HISTORY_MAX + 4


def test_state_endpoint_carries_the_program_without_timing(db, monkeypatch, armed):
    from fastapi.testclient import TestClient
    from wormhole import server
    from wormhole.learn import Brain
    from wormhole.paper import Paper
    from fakes import FakeRpc
    monkeypatch.setattr(server, 'treasury', lambda rpc: {'usd': 0.0, 'usd_real': 0.0, 'stage': 0, 'stage_name': 'hatchling',
                                                         'stages': 7, 'next_usd': 20})
    monkeypatch.setattr(server, '_compute_cached', lambda: {'provider': 'venice', 'balance_usd': None})
    monkeypatch.setattr(server, '_tcache', (0, None))
    rpc = FakeRpc()
    chain(rpc, usdg=5000)
    BP.release(rpc, db, T0)
    db.meta_set('burn_next_at', '1999999999.5')
    db.meta_set('claim_policy_status', json.dumps({'reason': 'x', 'next_claim_after': 1_888_888_888}))
    body = TestClient(server.make_app(rpc, db, Brain(db), Paper(db), server.Hub())).get('/api/state')
    tr = body.json()['treasury']
    assert tr['burn_program']['total_usd'] == 530.0 and tr['burn_program']['interval_s'] == 10800
    assert tr['burn_history'] == [] and tr['burn_max_usd'] == 25
    assert '1999999999' not in body.text and '1888888888' not in body.text and 'next_claim_after' not in body.text


def test_arming_a_new_id_never_resumes_a_paused_program_with_its_catch_up(db, rpc, monkeypatch):
    chain(rpc, usdg=5000)
    monkeypatch.setenv('WH_SURPLUS_BURN_USD', '530')
    monkeypatch.delenv('WH_SURPLUS_BURN_ID', raising=False)
    BP.release(rpc, db, T0)
    assert BP.release(rpc, db, T0 + DAY) == pytest.approx(75.71)
    monkeypatch.setenv('WH_SURPLUS_BURN_USD', '0')                  # the operator pauses
    assert BP.release(rpc, db, T0 + 2 * DAY) == 0.0
    monkeypatch.setenv('WH_SURPLUS_BURN_USD', '50')                 # and arms a new, smaller program instead
    monkeypatch.setenv('WH_SURPLUS_BURN_ID', 'surplus-2')
    assert BP.release(rpc, db, T0 + 5 * DAY) == 0.0                 # the old one stays paused: nothing released
    assert any('is paused until WH_SURPLUS_BURN_ID names it again' in e['text'] for e in db.q("SELECT text FROM events"))


def test_a_pending_row_without_a_transaction_does_not_stall_the_program(db, rpc, monkeypatch):
    chain(rpc, usdg=5000)
    monkeypatch.setenv('WH_SURPLUS_BURN_USD', '530')
    monkeypatch.delenv('WH_SURPLUS_BURN_ID', raising=False)
    T.ensure_tables(db)
    db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)",
         (T0 - 10, 'compute_pending', 'USDC', 5.0, None, 'top-up on Base in flight'))
    BP.release(rpc, db, T0)
    assert BP.release(rpc, db, T0 + 3 * DAY) > 0


def test_a_settling_payment_holds_the_release_and_says_so(db, rpc, monkeypatch):
    chain(rpc, usdg=5000)
    monkeypatch.setenv('WH_SURPLUS_BURN_USD', '530')
    monkeypatch.delenv('WH_SURPLUS_BURN_ID', raising=False)
    T.ensure_tables(db)
    BP.release(rpc, db, T0)
    db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)",
         (T0 + 10, 'forward_pending', 'USDG', 5.0, '0x' + 'ab' * 32, 'to the creator'))
    assert BP.release(rpc, db, T0 + 3 * DAY) == 0.0
    assert any(e['text'] == 'burn program waits: a payment is still settling' for e in db.q("SELECT text FROM events"))


# ---- always-on rounds: every spare dollar above the reserve, a week at a time ------------------------------------

@pytest.fixture
def auto(monkeypatch):
    monkeypatch.setenv('WH_SURPLUS_BURN_AUTO', '1')
    monkeypatch.delenv('WH_SURPLUS_BURN_USD', raising=False)
    monkeypatch.delenv('WH_SURPLUS_BURN_ID', raising=False)


def test_rounds_are_on_by_default_in_production_settings(monkeypatch):
    monkeypatch.delenv('WH_SURPLUS_BURN_AUTO', raising=False)
    assert BP.auto_on() and BP.auto_min() == 25


def test_a_round_starts_for_exactly_the_spare_above_the_reserve(db, rpc, auto):
    ledger(db, 'claim', 100.0)                                      # 80 owed: creator, burn, gold
    chain(rpc, usdg=700)
    assert BP.release(rpc, db, T0) == 0.0                          # armed now, nothing due yet
    p = BP.saved(db)
    spare = 700 - T.owed_total(db) - BP.reserve_usd()
    assert p['id'] == f'auto-{T0}' and p['total'] == pytest.approx(spare, abs=0.01) and p['days'] == 7
    assert 'burn round started' in db.one("SELECT text FROM events ORDER BY id DESC LIMIT 1")['text']
    assert BP.release(rpc, db, T0 + DAY) == pytest.approx(spare / 7, abs=0.01)
    st = BP.status(db, T0 + DAY)
    assert st['auto'] is True and st['state'] == 'releasing' and 'Whenever' in st['reason']


def test_no_round_below_the_minimum_spare(db, rpc, auto):
    chain(rpc, usdg=BP.reserve_usd() + 24)
    assert BP.release(rpc, db, T0) == 0.0 and BP.saved(db) is None


def test_a_round_runs_to_its_end_and_the_next_starts_from_new_spare(db, rpc, auto):
    chain(rpc, usdg=500)
    BP.release(rpc, db, T0)
    first = BP.saved(db)
    chain(rpc, usdg=5000)                                           # fees arrive mid-round: the round keeps its size
    BP.release(rpc, db, T0 + 3 * DAY)
    assert BP.saved(db)['id'] == first['id'] and BP.saved(db)['total'] == first['total']
    BP.release(rpc, db, T0 + 7 * DAY)                               # released in full
    settled_burn(db, first['total'], first['total'], pid=first['id'])   # and burned
    BP.release(rpc, db, T0 + 8 * DAY)
    nxt = BP.saved(db)
    assert nxt['id'] == f'auto-{T0 + 8 * DAY}' and nxt['total'] > 0


def test_no_round_while_a_manual_program_is_unfinished_even_paused(db, rpc, armed, monkeypatch):
    chain(rpc, usdg=5000)
    BP.release(rpc, db, T0)                                         # the manual program is armed
    monkeypatch.setenv('WH_SURPLUS_BURN_USD', '0')                  # and paused
    monkeypatch.setenv('WH_SURPLUS_BURN_AUTO', '1')
    assert BP.release(rpc, db, T0 + DAY) == 0.0 and BP.saved(db)['id'] == 'surplus-1'


def test_switching_rounds_off_pauses_a_running_round(db, rpc, auto, monkeypatch):
    chain(rpc, usdg=500)
    BP.release(rpc, db, T0)
    monkeypatch.setenv('WH_SURPLUS_BURN_AUTO', '0')
    assert BP.release(rpc, db, T0 + DAY) == 0.0 and BP.status(db, T0 + DAY)['state'] == 'paused'
    monkeypatch.setenv('WH_SURPLUS_BURN_AUTO', '1')
    assert BP.release(rpc, db, T0 + 2 * DAY) > 0


# ---- the ETH a burn pays in gas ------------------------------------------------------------------------------------

def test_a_burn_books_the_gas_of_its_approvals_and_its_swap(db, rpc, acct, live, monkeypatch):
    burn_setup(db, rpc, monkeypatch)
    plain = rpc.receipt_for

    def with_gas(h):
        rc = plain(h)
        if rc:
            rc = dict(rc, gasUsed=hex(100_000), effectiveGasPrice=hex(50_000_000))   # 0.000005 ETH each
        return rc
    rpc.receipt_for = with_gas
    assert T.burn(rpc, db, acct)
    b = rows(db, 'burn')[0]
    assert b['gas_eth'] == pytest.approx(3 * 0.000005)                # two approvals and the swap
    assert float(db.meta_get('burn_gas_carry')) == 0.0
    assert T.summary(rpc, db)['burn_gas_eth'] == pytest.approx(0.000015)


def test_approval_gas_waits_for_the_burn_it_belongs_to(db):
    T.ensure_tables(db)
    T.carry_burn_gas(db, {'gasUsed': hex(50_000), 'effectiveGasPrice': hex(40_000_000)})
    T.carry_burn_gas(db, None)
    assert float(db.meta_get('burn_gas_carry')) == pytest.approx(0.000002)
    assert T.tx_fee_eth({'gasUsed': '0x10', 'effectiveGasPrice': '0x2', 'l1Fee': '0x4'}) == pytest.approx(36 / 1e18)
    assert T.tx_fee_eth({}) == 0.0 and T.tx_fee_eth(None) == 0.0
