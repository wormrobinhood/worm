"""Best-of routing: real provider answers recorded on 2026-09-28 (read-only quotes for a dummy address), replayed
offline. Nothing here reaches the network."""
import json
import pathlib
import time

import pytest
from eth_abi import decode, encode

from wormhole import config as C, route as R

FIX = pathlib.Path(__file__).parent / 'fixtures' / 'route'
KLV = '0xc4741f151afec238fd3a17c25b1be83b8e3c1646'        # ETH-paired
GLORY = '0x653588d01e4bf5f8b3c04a52ecfdbbb18c1083b3'      # META-paired
USDG = C.USDG
WALLET = '0x' + '77' * 20


def fx(name):
    return json.loads((FIX / f'{name}.json').read_text())


def provider_of(url):
    return 'kyber' if 'kyberswap' in url else 'lifi' if 'li.quest' in url else 'relay'


def answer(monkeypatch, answers):
    """Replace every provider request: answers[provider] is a JSON body, an exception, or f(method, url, kw)."""
    calls = []
    def req(method, url, **kw):
        calls.append((method, url, kw))
        a = answers[provider_of(url)]
        if isinstance(a, Exception):
            raise a
        return a(method, url, kw) if callable(a) else a
    monkeypatch.setattr(R, '_request', req)
    return calls


def buys(token='KLV'):
    return {p: fx(f'{p}_buy_{token}') for p in ('kyber', 'lifi', 'relay')}


# ---- parsing ---------------------------------------------------------------------------------------

@pytest.mark.parametrize('token,addr', [('KLV', KLV), ('GLORY', GLORY)])
def test_each_provider_answer_is_read_as_recorded(token, addr):
    k = R.parse_kyber(fx(f'kyber_buy_{token}'), USDG, addr, 10_000_000)
    l = R.parse_lifi(fx(f'lifi_buy_{token}'), USDG, addr, 10_000_000)
    r = R.parse_relay(fx(f'relay_buy_{token}'), USDG, addr, 10_000_000)
    assert k['to'] == k['spender'] == R.KYBER_ROUTER and k['executable'] and k['fee_usd'] == 0
    assert l['to'] == l['spender'] == R.LIFI_DIAMOND and l['executable'] and l['fee_usd'] == pytest.approx(.025)
    assert r['to'] == r['spender'] == R.RELAY_PROXY and not r['executable']     # quote-only: its minimum cannot be checked
    assert r['fee_usd'] == pytest.approx(.015, abs=.001)
    for c in (k, l, r):
        assert c['out'] > 10**23 and c['gas'] > 100_000 and c['expires_at'] - c['quoted_at'] == R.QUOTE_TTL


def test_an_answer_to_another_question_is_refused():
    with pytest.raises(R.ProviderError):
        R.parse_kyber(fx('kyber_buy_KLV'), USDG, GLORY, 10_000_000)
    with pytest.raises(R.ProviderError):
        R.parse_lifi(fx('lifi_buy_KLV'), USDG, KLV, 5_000_000)
    with pytest.raises(R.ProviderError):
        R.parse_relay(fx('relay_buy_KLV'), KLV, USDG, 10_000_000)
    with pytest.raises(R.ProviderError):
        R.parse_kyber({'code': 4008, 'message': 'route not found'}, USDG, KLV, 1)


def test_a_fee_charged_on_top_or_a_moved_router_is_not_executable():
    j = fx('lifi_buy_KLV')
    j['estimate']['feeCosts'][0]['included'] = False
    with pytest.raises(R.ProviderError, match='on top'):
        R.parse_lifi(j, USDG, KLV, 10_000_000)
    k = fx('kyber_buy_KLV')
    k['data']['routerAddress'] = '0x' + '66' * 20
    assert not R.parse_kyber(k, USDG, KLV, 10_000_000)['executable']
    k = fx('kyber_buy_KLV')
    k['data']['routeSummary']['extraFee']['feeAmount'] = '10'
    with pytest.raises(R.ProviderError, match='fee'):
        R.parse_kyber(k, USDG, KLV, 10_000_000)


# ---- best of -------------------------------------------------------------------------------------

def test_best_quote_takes_the_most_out_among_executable_routes(monkeypatch):
    calls = answer(monkeypatch, buys())
    q = R.best_quote(USDG, KLV, 10_000_000)
    assert q['provider'] == 'kyber' and q['out'] == 2367719523394184099659776
    assert {c['provider'] for c in q['compared']} == {'kyber', 'lifi'} and q['failed'] == {}
    # Relay is quote-only: it is not even asked unless named (scripts/route-compare.py)
    assert not any(provider_of(u) == 'relay' for _, u, _ in calls)
    # a read-only quote never names a real wallet, and carries no client id or name
    assert len(calls) == 2 and C.WALLET and 'x-client-id' not in R.HEADERS and 'User-Agent' not in R.HEADERS
    for method, url, kw in calls:
        body = json.dumps(kw).lower()
        assert C.WALLET[2:] not in body
        assert provider_of(url) == 'kyber' or R.DUMMY in body


def test_a_quote_only_provider_never_wins_even_when_it_offers_more(monkeypatch):
    answers = buys()
    answers['relay'] = json.loads(json.dumps(answers['relay']).replace('2364013931076494517338112', '9364013931076494517338112'))
    answer(monkeypatch, answers)
    assert R.best_quote(USDG, KLV, 10_000_000, providers=R.PROVIDERS)['provider'] == 'kyber'


def test_the_pool_itself_is_a_fourth_candidate_and_wins_a_tie(monkeypatch):
    answer(monkeypatch, buys())
    best = 2367719523394184099659776
    pons = R.candidate('pons', USDG, KLV, 10_000_000, best, 150_000, 0, C.UNIVERSAL_ROUTER, C.PERMIT2)
    assert R.best_quote(USDG, KLV, 10_000_000, extra=[pons])['provider'] == 'pons'
    worse = R.candidate('pons', USDG, KLV, 10_000_000, best - 1, 150_000, 0, C.UNIVERSAL_ROUTER, C.PERMIT2)
    assert R.best_quote(USDG, KLV, 10_000_000, extra=[worse])['provider'] == 'kyber'


def test_a_route_far_from_the_pools_mid_is_not_believed(monkeypatch):
    answer(monkeypatch, buys())
    # the pool's mid says ~2.08M tokens: kyber and lifi are within 15%, a crazy one is not
    crazy = R.candidate('pons', USDG, KLV, 10_000_000, 4 * 10**24, 1, 0, C.UNIVERSAL_ROUTER, C.PERMIT2)
    q = R.best_quote(USDG, KLV, 10_000_000, extra=[crazy], expect=2.08e24)
    assert q['provider'] == 'kyber' and any(c['provider'] == 'pons' and not c['band_ok'] for c in q['compared'])
    with pytest.raises(R.NoRoute):                     # every route disagrees with the mid by more than the band
        R.best_quote(USDG, KLV, 10_000_000, expect=1e24)


def test_an_aggregator_fee_above_the_cap_is_left_out(monkeypatch):
    answer(monkeypatch, {**buys(), 'kyber': R.ProviderError('down')})
    with pytest.raises(R.NoRoute):
        R.best_quote(USDG, KLV, 10_000_000, max_fee_usd=.02)    # lifi charges $0.025 and kyber is down
    assert R.best_quote(USDG, KLV, 10_000_000, max_fee_usd=.10)['provider'] == 'lifi'


def test_one_provider_down_leaves_the_others(monkeypatch):
    answer(monkeypatch, {**buys(), 'kyber': R.ProviderError('http 500')})
    q = R.best_quote(USDG, KLV, 10_000_000)
    assert q['provider'] == 'lifi' and q['failed'] == {'kyber': 'http 500'}


def test_every_provider_down_is_no_route_and_says_why(monkeypatch):
    answer(monkeypatch, {'kyber': R.ProviderError('http 500'), 'lifi': R.RateLimited('rate limited'), 'relay': ValueError('bad json')})
    with pytest.raises(R.NoRoute) as e:
        R.best_quote(USDG, KLV, 10_000_000, providers=R.PROVIDERS)
    assert e.value.failed == {'kyber': 'http 500', 'lifi': 'rate limited', 'relay': 'ValueError'}


def test_a_slow_provider_is_dropped_not_waited_for(monkeypatch):
    def slow(method, url, kw):
        time.sleep(.5)
        return fx('lifi_buy_KLV')
    answer(monkeypatch, {**buys(), 'lifi': slow})
    started = time.time()
    q = R.best_quote(USDG, KLV, 10_000_000, wait_s=.1)
    assert time.time() - started < .45 and q['failed'].get('lifi') == 'timed out' and q['provider'] == 'kyber'


# ---- breakers and pacing -------------------------------------------------------------------------

def test_the_breaker_stands_a_failing_provider_down_and_backs_off(monkeypatch):
    calls = answer(monkeypatch, {**buys(), 'kyber': R.ProviderError('http 502')})
    for _ in range(R.BREAK_AFTER):
        R.best_quote(USDG, KLV, 10_000_000)
    asked = sum(provider_of(u) == 'kyber' for _, u, _ in calls)
    q = R.best_quote(USDG, KLV, 10_000_000)
    assert q['failed']['kyber'] == 'backing off' and sum(provider_of(u) == 'kyber' for _, u, _ in calls) == asked
    first = R._state[('kyber', 'paper')].open_until - time.time()
    assert R.BREAK_BASE_S - 2 < first <= R.BREAK_BASE_S
    R._state[('kyber', 'paper')].open_until = 0                    # the pause is over: one probe, which fails again
    R.best_quote(USDG, KLV, 10_000_000)
    assert R._state[('kyber', 'paper')].open_until - time.time() > 2 * R.BREAK_BASE_S - 2     # doubled
    R._state[('kyber', 'paper')].fails = 99
    R._record('kyber', False)
    assert R._state[('kyber', 'paper')].open_until - time.time() <= R.BREAK_MAX_S             # capped
    answer(monkeypatch, buys())                          # recovered: the probe succeeds and closes the breaker
    R._state[('kyber', 'paper')].open_until = 0
    assert R.best_quote(USDG, KLV, 10_000_000)['provider'] == 'kyber' and R._state[('kyber', 'paper')].fails == 0


def test_a_rate_limit_stands_the_provider_down_at_once(monkeypatch):
    answer(monkeypatch, {**buys(), 'lifi': R.RateLimited('rate limited')})
    R.best_quote(USDG, KLV, 10_000_000)
    assert not R._available('lifi') and R.status()['paper']['lifi']['backing_off_s'] > 0


def test_requests_to_one_provider_are_spaced(monkeypatch):
    monkeypatch.setattr(R, 'MIN_GAP_S', .2)
    waits = []
    monkeypatch.setattr(R.time, 'sleep', lambda s: waits.append(s))
    R._pace('kyber'); R._pace('kyber'); R._pace('kyber')
    assert len(waits) == 2 and waits[1] > waits[0] > .1


# ---- live checks (dormant) -----------------------------------------------------------------------

def kyber_quote():
    return R.parse_kyber(fx('kyber_buy_KLV'), USDG, KLV, 10_000_000)


def kyber_tx(receiver=WALLET, min_out=None, amount=10_000_000, to=R.KYBER_ROUTER, value=0):
    """The recorded build, re-encoded for `receiver` with the given minimum."""
    data = fx('kyber_build_KLV')['data']['data']
    (p,) = decode([R.KYBER_T], bytes.fromhex(data[10:]))
    d = list(p[3])
    d[6], d[7] = receiver, amount
    if min_out is not None:
        d[8] = min_out
    p = list(p)
    p[3] = tuple(d)
    return {'to': to, 'data': R.KYBER_SWAP + encode([R.KYBER_T], [tuple(p)]).hex(), 'value': value, 'spender': R.KYBER_ROUTER}


def test_a_built_transaction_that_pays_us_our_minimum_passes():
    q = kyber_quote()
    ours = q['out'] * 97 // 100
    assert R.check(q, kyber_tx(min_out=ours), WALLET, ours)['receiver'] == WALLET


@pytest.mark.parametrize('bad,match', [
    (dict(to='0x' + '66' * 20), 'router'),
    (dict(value=1), 'ETH'),
    (dict(receiver='0x' + '88' * 20), 'someone else'),
    (dict(amount=10_000_001), 'not the one quoted'),
    (dict(min_out=1), 'minimum'),
])
def test_an_unsafe_built_transaction_is_refused(bad, match):
    q = kyber_quote()
    ours = q['out'] * 97 // 100
    args = {'min_out': ours, **bad}
    with pytest.raises(R.UnsafeRoute, match=match):
        R.check(q, kyber_tx(**args), WALLET, ours)


def test_a_wrong_spender_or_a_quote_only_provider_is_refused():
    q = kyber_quote()
    ours = q['out'] * 97 // 100
    with pytest.raises(R.UnsafeRoute, match='spender'):
        R.check({**q, 'spender': '0x' + '66' * 20}, kyber_tx(min_out=ours), WALLET, ours)
    with pytest.raises(R.UnsafeRoute, match='spender'):
        R.check(q, {**kyber_tx(min_out=ours), 'spender': '0x' + '66' * 20}, WALLET, ours)
    relay = R.parse_relay(fx('relay_buy_KLV'), USDG, KLV, 10_000_000)
    with pytest.raises(R.UnsafeRoute, match='not executed'):
        R.check(relay, {'to': R.RELAY_PROXY, 'data': '0x', 'value': 0}, WALLET, 1)
    with pytest.raises(R.UnsafeRoute):
        R.check(q, {**kyber_tx(min_out=ours), 'data': '0xdeadbeef'}, WALLET, ours)


def test_lifi_calldata_is_decoded_to_its_receiver_and_minimum():
    j = fx('lifi_buy_KLV')
    q = R.parse_lifi(j, USDG, KLV, 10_000_000)
    d = R.decode_lifi(j['transactionRequest']['data'])
    assert {k: d[k] for k in ('token_in', 'token_out', 'receiver', 'amount_in', 'min_out')} == {
        'token_in': USDG, 'token_out': KLV, 'receiver': R.DUMMY, 'amount_in': 10_000_000, 'min_out': int(j['estimate']['toAmountMin'])}
    R.check_lifi(d)                                              # the real build is exactly a trade of ours
    with pytest.raises(R.UnsafeRoute, match='someone else'):     # built for the dummy, so never for us
        R.check(q, {**q['tx'], 'spender': q['spender']}, WALLET, 1)


def test_build_names_the_wallet_only_when_building(monkeypatch):
    seen = []
    def kyber(method, url, kw):
        seen.append((method, url, kw))
        return fx('kyber_build_KLV')
    answer(monkeypatch, {'kyber': kyber, 'lifi': R.ProviderError('x'), 'relay': R.ProviderError('x')})
    tx = R.build(kyber_quote(), WALLET, 1_800_000_000)
    method, url, kw = seen[0]
    assert method == 'POST' and url.endswith('/route/build')
    assert kw['json']['sender'] == kw['json']['recipient'] == WALLET and kw['json']['slippageTolerance'] == R.BUILD_SLIPPAGE_BPS
    assert tx['to'] == R.KYBER_ROUTER and tx['value'] == 0 and tx['data'].startswith(R.KYBER_SWAP)
    with pytest.raises(R.UnsafeRoute):
        R.build(R.parse_relay(fx('relay_buy_KLV'), USDG, KLV, 10_000_000), WALLET, 1)


class SimRpc:
    """Answers eth_simulateV1 with the balances a swap would leave."""
    def __init__(self, got, spent, revert=False):
        self.got, self.spent, self.revert, self.seen = got, spent, revert, []

    def call(self, method, params):
        assert method == 'eth_simulateV1'
        calls = params[0]['blockStateCalls'][0]['calls']
        self.seen.append(calls)
        out, before = [], {KLV: 5, USDG: 50_000_000}
        swapped = False
        for c in calls:
            if c['data'].startswith(R.BALANCE_OF):
                bal = before[c['to']] + ((self.got if c['to'] == KLV else -self.spent) if swapped else 0)
                out.append({'status': '0x1', 'returnData': '0x' + bal.to_bytes(32, 'big').hex()})
            elif c['data'].startswith(R.APPROVE):
                out.append({'status': '0x1', 'returnData': '0x' + '00' * 31 + '01'})
            else:
                swapped = True
                out.append({'status': '0x0' if self.revert else '0x1', 'returnData': '0x'})
        return [{'calls': out}]


def test_simulation_must_deliver_the_minimum_and_spend_exactly_the_amount():
    q = kyber_quote()
    tx = kyber_tx(min_out=100)
    assert R.verify_simulation(SimRpc(1000, 10_000_000), WALLET, q, tx, 900) == 1000
    with pytest.raises(R.UnsafeRoute, match='less than the minimum'):
        R.verify_simulation(SimRpc(899, 10_000_000), WALLET, q, tx, 900)
    with pytest.raises(R.UnsafeRoute, match='another amount'):
        R.verify_simulation(SimRpc(1000, 10_000_001), WALLET, q, tx, 900)
    with pytest.raises(R.UnsafeRoute, match='reverted'):
        R.verify_simulation(SimRpc(1000, 10_000_000, revert=True), WALLET, q, tx, 900)


def test_a_simulated_approval_is_exact_never_unlimited():
    rpc = SimRpc(1000, 10_000_000)
    R.verify_simulation(rpc, WALLET, kyber_quote(), kyber_tx(min_out=100), 900, approve_to=R.KYBER_ROUTER)
    approve = next(c for c in rpc.seen[0] if c['data'].startswith(R.APPROVE))
    spender, amount = decode(['address', 'uint256'], bytes.fromhex(approve['data'][10:]))
    assert spender.lower() == R.KYBER_ROUTER and amount == 10_000_000 and approve['to'] == USDG
    swap = next(c for c in rpc.seen[0] if c['to'] == R.KYBER_ROUTER)
    assert swap['value'] == '0x0' and swap['from'] == WALLET


def test_the_evidence_names_every_provider_rule():
    e = R.evidence()
    assert e['executable'] == ['pons', 'pons-v3', 'kyber', 'lifi'] and e['mid_band'] == .15
    assert e['allowed']['kyber']['to'] == [R.KYBER_ROUTER]


# ---- lanes, budgets and the exit band ---------------------------------------------------------------

def test_paper_failures_never_stand_a_provider_down_for_live(monkeypatch):
    answer(monkeypatch, {**buys(), 'kyber': R.ProviderError('http 502')})
    for _ in range(R.BREAK_AFTER + 1):
        R.best_quote(USDG, KLV, 10_000_000)
    assert not R._available('kyber', 'paper') and R._available('kyber', 'live')
    answer(monkeypatch, buys())
    assert R.best_quote(USDG, KLV, 10_000_000, lane='live')['provider'] == 'kyber'
    assert R.best_quote(USDG, KLV, 10_000_000)['failed']['kyber'] == 'backing off'


def test_lifi_without_a_key_keeps_part_of_its_two_hour_budget_for_live(monkeypatch):
    calls = answer(monkeypatch, buys())
    monkeypatch.setattr(R.C, 'LIFI_KEY', '')
    for _ in range(R.LIFI_FREE_LIMIT - R.LIFI_LIVE_RESERVE):
        R.best_quote(USDG, KLV, 10_000_000, providers=('lifi',))
    q = R.best_quote(USDG, KLV, 10_000_000, providers=('kyber', 'lifi'))
    assert q['failed'] == {'lifi': 'request budget spent'}                   # paper has used its share
    for _ in range(R.LIFI_LIVE_RESERVE):
        assert R.best_quote(USDG, KLV, 10_000_000, providers=('lifi',), lane='live')['provider'] == 'lifi'
    with pytest.raises(R.NoRoute) as e:
        R.best_quote(USDG, KLV, 10_000_000, providers=('lifi',), lane='live')
    assert e.value.failed == {'lifi': 'request budget spent'}
    assert sum(provider_of(u) == 'lifi' for _, u, _ in calls) == R.LIFI_FREE_LIMIT
    assert R.status()['lifi_budget'] == {'limit': 75, 'used': 75, 'paper': 50}
    R._spent['lifi'].rotate(0)
    for i in range(len(R._spent['lifi'])):                                    # two hours later the window is free again
        ts, lane = R._spent['lifi'][i]
        R._spent['lifi'][i] = (ts - R.LIFI_WINDOW_S, lane)
    assert R.best_quote(USDG, KLV, 10_000_000, providers=('lifi',))['provider'] == 'lifi'


def test_a_lifi_key_lifts_the_budget_and_goes_to_lifi_only(monkeypatch):
    sent = []
    class Session:
        def request(self, method, url, timeout=None, headers=None, **kw):
            sent.append((url, dict(headers)))
            return type('R', (), {'status_code': 200, 'json': lambda self: {}})()
    monkeypatch.setattr(R, '_session', Session())
    monkeypatch.undo()                                   # the real _request, the fake session
    monkeypatch.setattr(R, '_session', Session())
    monkeypatch.setattr(R.C, 'LIFI_KEY', 'test-key-not-real')
    R._request('GET', R.LIFI + '/quote')
    R._request('GET', R.KYBER + '/routes')
    assert sent[0][1].get('x-lifi-api-key') == 'test-key-not-real' and 'x-lifi-api-key' not in sent[1][1]
    assert all(R._admit('lifi', 'paper') for _ in range(R.LIFI_FREE_LIMIT + 5))
    R.reset()


def test_an_exit_refuses_only_a_route_paying_suspiciously_more_than_the_mid(monkeypatch):
    answer(monkeypatch, {'kyber': R.ProviderError('x'), 'lifi': R.ProviderError('x'), 'relay': R.ProviderError('x')})
    thin = R.candidate('kyber', KLV, USDG, 10**24, 8_000_000, 1, 0, R.KYBER_ROUTER, R.KYBER_ROUTER)
    rich = R.candidate('lifi', KLV, USDG, 10**24, 12_000_000, 1, 0, R.LIFI_DIAMOND, R.LIFI_DIAMOND)
    own = R.candidate('pons', KLV, USDG, 10**24, 30_000_000, 1, 0, C.UNIVERSAL_ROUTER, C.PERMIT2)
    # the mid says $10: 20% under is a dump, and still sells; 20% over is not believed
    assert R.best_quote(KLV, USDG, 10**24, extra=[thin, rich], expect=10_000_000, side='exit')['provider'] == 'kyber'
    with pytest.raises(R.NoRoute):
        R.best_quote(KLV, USDG, 10**24, extra=[thin, rich], expect=10_000_000)            # an entry: both ways
    assert R.best_quote(KLV, USDG, 10**24, extra=[own], expect=10_000_000, side='exit')['provider'] == 'pons'   # never banded



# ---- exactly our trade: malicious calldata the reviewer built, and more ------------------------------

EVIL = '0x' + 'ee' * 20


def kyber_built(**change):
    """The recorded build with fields of its swap description changed, and a quote matching the recorded amounts."""
    data = fx('kyber_build_KLV')['data']['data']
    (p,) = decode([R.KYBER_T], bytes.fromhex(data[10:]))
    p, d = list(p), list(p[3])
    keys = {'srcReceivers': 2, 'srcAmounts': 3, 'feeReceivers': 4, 'feeAmounts': 5, 'flags': 9, 'permit': 10}
    for k, v in change.items():
        if k in keys:
            d[keys[k]] = v
    p[3] = tuple(d)
    if 'callTarget' in change:
        p[0] = change['callTarget']
    if 'approveTarget' in change:
        p[1] = change['approveTarget']
    q = {'provider': 'kyber', 'token_in': USDG, 'token_out': KLV, 'amount_in': d[7], 'spender': R.KYBER_ROUTER}
    tx = {'to': R.KYBER_ROUTER, 'value': 0, 'spender': R.KYBER_ROUTER, 'data': R.KYBER_SWAP + encode([R.KYBER_T], [tuple(p)]).hex()}
    return q, tx, d[6], d[8]


def test_the_recorded_kyber_build_is_exactly_our_trade():
    q, tx, receiver, minimum = kyber_built()
    assert R.check(q, tx, receiver, minimum)['flags'] == 0x200


@pytest.mark.parametrize('change,match', [
    (dict(flags=0x201), 'flag'),                                              # a partial fill
    (dict(feeReceivers=(EVIL,), feeAmounts=(290,), flags=0x200 | 0x40 | 0x80), 'fee'),
    (dict(srcReceivers=(R.KYBER_EXECUTOR, EVIL), srcAmounts=(10_000_000 - 1, 1)), 'split'),
    (dict(srcReceivers=(EVIL,)), 'split'),
    (dict(srcAmounts=(9_000_000,)), 'split'),
    (dict(callTarget=EVIL, srcReceivers=(EVIL,)), 'executor'),
    (dict(approveTarget=EVIL), 'executor'),
    (dict(permit=b'\x01' * 32), 'permit'),
])
def test_kyber_calldata_that_is_not_exactly_our_trade_is_refused(change, match):
    q, tx, receiver, minimum = kyber_built(**change)
    with pytest.raises(R.UnsafeRoute, match=match):
        R.check(q, tx, receiver, minimum)


LIFI_T = ["bytes32", "string", "string", "address", "uint256", f"{R.LIFI_SWAP_T}[]"]


def lifi_built(edit):
    j = fx('lifi_buy_KLV')
    v = list(decode(LIFI_T, bytes.fromhex(j['transactionRequest']['data'][10:])))
    swaps = [list(x) for x in v[5]]
    swaps = edit(swaps) or swaps
    v[5] = [tuple(x) for x in swaps]
    q = {'provider': 'lifi', 'token_in': USDG, 'token_out': KLV, 'amount_in': 10_000_000, 'spender': R.LIFI_DIAMOND}
    tx = {'to': R.LIFI_DIAMOND, 'value': 0, 'spender': R.LIFI_DIAMOND, 'data': R.LIFI_MULTI + encode(LIFI_T, v).hex()}
    return q, tx, v[3], v[4]


def fee_call(token, paid):
    return bytes.fromhex(R.LIFI_FEE_CALL[2:]) + encode(['address', '(address,uint256)[]'], [token, paid])


@pytest.mark.parametrize('edit,match', [
    # the reviewer's: a second deposit, pulling a token the wallet approved elsewhere
    (lambda s: [s[0], [s[1][0], s[1][1], '0x' + 'ab' * 20, KLV, 10**24, b'', True], s[1]], 'deposit'),
    (lambda s: [s[0], [s[1][0], s[1][1], '0x' + 'ab' * 20, KLV, 10**24, b'', False], s[1]], 'did not produce'),
    (lambda s: s.__setitem__(1, [s[1][0], s[1][1], USDG, KLV, s[1][4], s[1][5], True]), 'deposit'),
    (lambda s: s.__setitem__(0, [s[0][0], s[0][1], USDG, USDG, s[0][4], fee_call(USDG, [(EVIL, 25000)]), True]), 'someone else'),
    (lambda s: s.__setitem__(0, [s[0][0], s[0][1], USDG, USDG, s[0][4], fee_call(USDG, [(R.LIFI_FEE_RECIPIENT, 200_000)]), True]), 'too much'),
    (lambda s: s.__setitem__(0, [EVIL, EVIL, USDG, USDG, s[0][4], s[0][5], True]), 'fee step'),
    (lambda s: [s[0], s[1], [R.LIFI_FEE_COLLECTOR, R.LIFI_FEE_COLLECTOR, KLV, KLV, 1, fee_call(KLV, [(R.LIFI_FEE_RECIPIENT, 1)]), False]], 'fee step after'),
    (lambda s: s.__setitem__(1, [s[1][0], s[1][1], USDG, KLV, s[1][4] + 1, s[1][5], False]), 'too much'),   # spends more than is left
])
def test_lifi_calldata_that_is_not_exactly_our_trade_is_refused(edit, match):
    q, tx, receiver, minimum = lifi_built(edit)
    with pytest.raises(R.UnsafeRoute, match=match):
        R.check(q, tx, receiver, minimum)


def test_the_recorded_lifi_build_is_exactly_our_trade():
    q, tx, receiver, minimum = lifi_built(lambda s: None)
    assert R.check(q, tx, receiver, minimum)['steps'][0]['call_to'] == R.LIFI_FEE_COLLECTOR


class CodeRpc:
    """eth_getCode with the pinned code (keccak matching is faked by answering the hash's preimage map), facets."""
    def __init__(self, codes, facet=R.LIFI_FACET):
        self.codes, self.facet = codes, facet

    def call(self, method, params):
        if method == 'eth_getCode':
            return self.codes.get(params[0], '0x')
        assert method == 'eth_call' and params[0]['to'] == R.LIFI_DIAMOND and params[0]['data'].startswith(R.FACET_OF)
        return '0x' + self.facet[2:].rjust(64, '0')


def test_contracts_are_pinned_by_code_and_facet_and_fail_closed(monkeypatch):
    from eth_utils import keccak
    codes = {a: '0x60' + f'{i:02x}' for i, a in enumerate(R.CODE_HASHES)}
    monkeypatch.setattr(R, 'CODE_HASHES', {a: '0x' + keccak(hexstr=c).hex() for a, c in codes.items()})
    R.verify_contracts(CodeRpc(codes), 'kyber')
    R.verify_contracts(CodeRpc(codes), 'lifi')
    with pytest.raises(R.UnsafeRoute, match='code changed'):
        R.verify_contracts(CodeRpc({**codes, R.KYBER_EXECUTOR: '0x6099'}), 'kyber')
    with pytest.raises(R.UnsafeRoute, match='code changed'):
        R.verify_contracts(CodeRpc({**codes, R.LIFI_DIAMOND: '0x'}), 'lifi')
    with pytest.raises(R.UnsafeRoute, match='facet'):
        R.verify_contracts(CodeRpc(codes, facet=EVIL), 'lifi')
    with pytest.raises(R.UnsafeRoute):
        R.verify_contracts(CodeRpc(codes), 'relay')
