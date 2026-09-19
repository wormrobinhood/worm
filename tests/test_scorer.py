"""The scorer on synthetic chains: incomplete data, a gamed token, dust buyers, unknown launch blocks, the
swap cap, the creator's own launch-block buy, the creator's record, socials, and rounding. No network."""
import itertools
import time

import pytest

from wormhole import config as C
from wormhole import scorer as S
from wormhole.learn import Brain

from fakerpc import POOL_ID, FakeRpc, addr, launch, run

NOW = int(time.time())
BPH = C.BLOCKS_PER_HOUR
LB = 1_000_000                    # launch block
GB = LB + 2 * BPH                 # graduated two hours after launch: organic pace
LATEST = GB + BPH // 2            # half an hour after graduation
TOKEN, CURVE, DEP = addr(0x10), addr(0x20), addr(0x30)
WINDOW = int(C.SNIPE_WINDOW_S / C.BLOCK_TIME)
BONUS_WEIGHTS = {"creator_history": 1.5, "creator_tax": 1.5, "pace": 1.5}   # 50 + 4.5 + 7.5 + 7.5 = 69.5 -> 70


def setup(db, block=LB, grad_block=GB, grad_age_s=1800, pace_s=7200, socials=True, tax=0):
    launch(db, TOKEN, CURVE, DEP, block, NOW - grad_age_s - pace_s, grad_block, NOW - grad_age_s, tax=tax, socials=socials)


def points(r):
    return {f["rule"]: f["points"] for f in r["fired"]}


def prior_token(db, i, outcome):
    """An earlier graduation by the same creator with a resolved outcome."""
    t = addr(0x100 + i)
    launch(db, t, addr(0x200 + i), DEP, LB - 1000 - i, NOW - 90000, LB - 500 - i, NOW - 86400, graduated=1)
    db.x("INSERT INTO outcomes(token, verdict, outcome, resolved, change_pct) VALUES(?,?,?,?,?)", (t, "avoid", outcome, 1, -95.0))
    return t


def crowd(rpc, n=200, first_block=LB + 40, tokens=10**21):
    """n buyers after the snipe window, each also holding what it bought."""
    for i in range(n):
        b = addr(0x1000 + i)
        rpc.curve_buy(CURVE, first_block + i, b, b, tokens)
        rpc.transfer(TOKEN, first_block + i, CURVE, b, tokens)


# ---- incomplete data (F2) ----------------------------------------------------------------------

def test_bonus_only_fixture_with_complete_data_reads_healthy(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    r = run(rpc, db, TOKEN, weights=lambda: BONUS_WEIGHTS)
    assert r["score"] == 70 and r["verdict"] == "looks healthy"
    assert r["metrics"]["partial"] is False and r["retry"] is False and S.PARTIAL_TEXT not in r["reasons"]
    assert S.final_score(50 + sum(f["applied"] for f in r["fired"])) == r["score"]     # the score recomputes from fired


def test_failed_chain_reads_never_read_healthy(db):
    rpc = FakeRpc(LATEST)
    rpc.fail_addr = {CURVE, TOKEN, C.HOOK}
    setup(db)
    r = run(rpc, db, TOKEN, weights=lambda: BONUS_WEIGHTS)
    assert r["metrics"]["partial"] is True and r["retry"] is True
    assert r["score"] <= 69 and r["verdict"] != "looks healthy"
    assert r["reasons"][0] == S.PARTIAL_TEXT
    row = db.one("SELECT partial, verdict, score FROM scores WHERE token=?", (TOKEN,))
    assert row["partial"] == 1 and row["verdict"] == "mixed" and row["score"] == 69


@pytest.mark.parametrize("fails", [{CURVE}, {TOKEN}, {C.HOOK}, {C.POOL_MANAGER}])
def test_any_failed_read_marks_the_score_partial(db, fails):
    rpc = FakeRpc(LATEST)
    rpc.fail_addr = set(fails)
    setup(db)
    rpc.pool(GB, POOL_ID, TOKEN, C.USDG, DEP)
    r = run(rpc, db, TOKEN)
    assert r["metrics"]["partial"] is True and r["retry"] is True and r["verdict"] != "looks healthy"


# ---- the gamed token (F8) ----------------------------------------------------------------------

def test_gamed_token_is_not_healthy(db):
    """Creator holds 26%, its two earlier tokens rugged, 200 bundled buyers after the window, 60 trades in
    the hour, no tax, a link, a 2 h pace: the old rules read 70 'looks healthy'."""
    rpc = FakeRpc(LATEST)
    setup(db)
    for i in range(2):
        prior_token(db, i, "rugged")
    crowd(rpc)
    rpc.transfer(TOKEN, GB, CURVE, DEP, 7 * 10**22)          # 26% of circulating supply stays with the creator
    rpc.pool(GB, POOL_ID, TOKEN, C.USDG, DEP)
    for i in range(60):
        rpc.swap(LATEST - 100 + i, POOL_ID, 10**18, -10**6)
    r = run(rpc, db, TOKEN)
    pts = points(r)
    assert r["metrics"]["partial"] is False
    assert pts["creator_rugs"] == -50 and "creator_grads" not in pts
    assert pts["deployer_hold"] == 0 and pts["buyers"] == 10 and pts["activity"] == 5     # concentration is shown, not scored
    assert r["verdict"] == "avoid" and r["score"] < 45
    assert r["metrics"]["creator_rugged"] == 2 and r["metrics"]["creator_trust"] < 50


def test_hard_warning_demotes_healthy_to_mixed(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    crowd(rpc)
    rpc.transfer(TOKEN, GB, CURVE, DEP, 7 * 10**22)
    rpc.pool(GB, POOL_ID, TOKEN, C.USDG, DEP)
    for i in range(60):
        rpc.swap(LATEST - 100 + i, POOL_ID, 10**18, -10**6)
    r = run(rpc, db, TOKEN)
    held = [f for f in r["fired"] if f["rule"] == "deployer_hold"][0]
    assert held["points"] == 0 and held["hard"] is True and "can be sold into everyone" in held["text"] and r["score"] >= 70
    assert r["verdict"] == "mixed" and r["metrics"]["demoted"] is True        # no points, but never a healthy verdict
    assert any("demoted" in x for x in r["reasons"])
    assert db.one("SELECT verdict FROM scores WHERE token=?", (TOKEN,))["verdict"] == "mixed"


def test_creator_rugs_rule_and_the_grads_bonus(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    prior = prior_token(db, 0, "flat")
    pts = points(run(rpc, db, TOKEN))
    assert pts["creator_grads"] == 5 and "creator_rugs" not in pts
    db.x("UPDATE outcomes SET outcome='dumped' WHERE token=?", (prior,))
    pts = points(run(rpc, db, TOKEN))
    assert pts["creator_rugs"] == -25 and "creator_grads" not in pts
    # the token's own earlier verdict is not its creator's prior record
    db.x("INSERT INTO outcomes(token, verdict, outcome, resolved, change_pct) VALUES(?,?,?,?,?)", (TOKEN, "avoid", "rugged", 1, -95.0))
    assert points(run(rpc, db, TOKEN))["creator_rugs"] == -25
    prior_token(db, 1, "rugged")
    prior_token(db, 2, "rugged")
    assert points(run(rpc, db, TOKEN))["creator_rugs"] == -50                # capped at two


def test_brain_weights_cover_every_rule(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    b = Brain(db)
    assert "creator_rugs" in b.weights()
    r = run(rpc, db, TOKEN, weights=b.weights)
    assert all(f["weight"] == 1.0 for f in r["fired"])


# ---- the curve: dust, snipe window, the creator's own buy (F8, F10, F15) -----------------------

def test_dust_buys_are_not_buyers(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    real = addr(0x500)
    rpc.curve_buy(CURVE, LB + 100, real, real, 10**21)
    for i in range(150):
        d = addr(0x2000 + i)
        rpc.curve_buy(CURVE, LB + 200 + i, d, d, 0)
    r = run(rpc, db, TOKEN)
    m = r["metrics"]
    assert m["curve_buys"] == 151 and m["unique_buyers"] == 1 and m["dust_buyers"] == 150
    assert points(r)["buyers"] == -10


def test_unknown_launch_block_is_taken_from_the_first_buy(db):
    rpc = FakeRpc(LATEST)
    setup(db, block=None)
    for i in range(30):                                   # every buy in the 20 blocks before graduation
        b = addr(0x3000 + i)
        rpc.curve_buy(CURVE, GB - 20 + (i % 20), b, b, 10**21)
    r = run(rpc, db, TOKEN)
    m, pts = r["metrics"], points(r)
    assert m["launch_block_known"] is False and m["launch_block_derived"] is True and m["partial"] is False
    assert pts["snipe"] == -15 and m["snipe_pct"] == 100.0     # no 'quiet launch window' bonus from a fake window


def test_unknown_launch_block_without_buys_is_partial(db):
    rpc = FakeRpc(LATEST)
    setup(db, block=None)
    r = run(rpc, db, TOKEN)
    assert "snipe" not in points(r) and r["metrics"]["partial"] is True and r["retry"] is True


def test_creators_launch_block_buy_is_charged_once(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    rpc.curve_buy(CURVE, LB, DEP, DEP, 4 * 10**21)              # 40% of the curve in the launch block
    for i in range(30):
        b = addr(0x4000 + i)
        rpc.curve_buy(CURVE, LB + 100 + i, b, b, 2 * 10**20)    # the other 60%, later
    r = run(rpc, db, TOKEN)
    pts, m = points(r), r["metrics"]
    assert pts["deployer_buy"] == -20 and m["deployer_buy_pct"] == 40.0
    assert pts["snipe"] == 5 and m["snipe_pct"] == 0.0
    assert [k for k in ("snipe", "deployer_buy") if pts.get(k, 0) < 0] == ["deployer_buy"]


def test_snipe_window_counts_other_wallets(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    sniper = addr(0x600)
    rpc.curve_buy(CURVE, LB + WINDOW, sniper, sniper, 4 * 10**21)
    for i in range(30):
        b = addr(0x4000 + i)
        rpc.curve_buy(CURVE, LB + 100 + i, b, b, 2 * 10**20)
    r = run(rpc, db, TOKEN)
    assert points(r)["snipe"] == -15 and r["metrics"]["snipe_pct"] == 40.0 and points(r)["top_buyer"] == -8


# ---- the pool: the last hour is read first (F9) -------------------------------------------------

def test_last_hour_swaps_survive_the_node_cap(db):
    latest = GB + 2 * BPH
    rpc = FakeRpc(latest, node_cap=10_000)
    setup(db, grad_age_s=7200)
    rpc.pool(GB, POOL_ID, TOKEN, C.USDG, DEP)
    for i in range(10_050):                                       # a busy first hour, all before hour_ago
        rpc.swap(GB + (i * (BPH - 2)) // 10_050, POOL_ID, 10**18, -10**6)
    for i in range(100):
        rpc.swap(latest - 100 + i, POOL_ID, 10**18, -10**6)
    r = run(rpc, db, TOKEN)
    m = r["metrics"]
    assert m["swaps_1h"] == 100 and m["buys_1h"] == 100
    assert points(r)["activity"] == 5                              # not 'no trades in the last hour'
    assert m["partial"] is True and m["swaps_since_grad"] >= 10_100
    assert any(a == C.POOL_MANAGER and lo == latest - BPH for a, lo, hi in rpc.calls)   # the last hour had its own query


# ---- creator history window, socials (F19, F8) --------------------------------------------------

def test_creator_history_counts_only_the_block_window(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    span = int(C.BACKFILL_HOURS * 3600 / C.BLOCK_TIME)
    for i in range(5):                                            # five launches just outside the window
        launch(db, addr(0x600 + i), addr(0x700 + i), DEP, LB - span - 10 - i, NOW - 400000, None, None, graduated=0)
    r = run(rpc, db, TOKEN)
    assert points(r)["creator_history"] == 3 and r["metrics"]["creator_prev_launches"] == 0
    for i in range(5):                                            # five inside
        launch(db, addr(0x800 + i), addr(0x900 + i), DEP, LB - span + 10 + i, NOW - 200000, None, None, graduated=0)
    r = run(rpc, db, TOKEN)
    assert points(r)["creator_history"] == -10 and r["metrics"]["creator_prev_launches"] == 5
    assert "in the last 72h" in next(f["text"] for f in r["fired"] if f["rule"] == "creator_history")


def test_unknown_creator_tax_is_neither_bonus_nor_penalty(db):
    """The indexer stores NULL when the tax read failed; that is unknown, not a 0% tax."""
    rpc = FakeRpc(LATEST)
    steps = []
    setup(db, tax=None)
    r = run(rpc, db, TOKEN, progress=steps.append)
    tax = next(f for f in r["fired"] if f["rule"] == "creator_tax")
    assert tax["points"] == 0 and tax["text"] == "creator tax unknown (read failed)"
    assert r["metrics"]["creator_tax_bps"] is None and r["metrics"]["partial"] is False
    creator_step = next(e for e in steps if e["step"] == "creator")
    assert "tax unknown" in creator_step["text"] and creator_step["data"]["tax_bps"] is None
    for bps, pts in ((0, 5), (150, 0), (400, -10), (900, -25)):
        setup(db, tax=bps)
        assert points(run(rpc, db, TOKEN))["creator_tax"] == pts


def test_socials_never_add_points(db):
    rpc = FakeRpc(LATEST)
    setup(db, socials=True)
    assert points(run(rpc, db, TOKEN))["socials"] == 0
    setup(db, socials=False)
    assert points(run(rpc, db, TOKEN))["socials"] == -5


# ---- rounding (F16) ----------------------------------------------------------------------------

def test_score_rounding_is_half_up_and_order_independent():
    vals = [50.0, 4.5, 0.1, 0.2, 0.3, 14.4]                       # 69.5 in decimal, not exactly in binary
    assert {S.final_score(sum(p)) for p in itertools.permutations(vals)} == {70}
    vals = [50.0, 4.5, 0.1, 0.2, 0.3, 15.4]                       # 70.5: half-up, not banker's
    assert {S.final_score(sum(p)) for p in itertools.permutations(vals)} == {71}
    assert S.final_score(69.4999) == 69 and S.final_score(-3) == 0 and S.final_score(140) == 100
    assert S.verdict_for(70) == "looks healthy" and S.verdict_for(69) == "mixed" and S.verdict_for(44) == "avoid"


# ---- who is behind the buyers: throwaway wallets and shared funding ----------------------------

FUNDER = addr(0x9999)


def crowd_of_throwaways(rpc, n=200, funded_by=None, nonce=1):
    """n buyers with almost no history, optionally all funded in USDG by one wallet just before launch."""
    for i in range(n):
        b = addr(0x1000 + i)
        rpc.nonces[b] = nonce
        rpc.curve_buy(CURVE, LB + 40 + i, b, b, 10**21)
        rpc.transfer(TOKEN, LB + 40 + i, CURVE, b, 10**21)
        if funded_by:
            rpc.transfer(C.USDG, LB - 300 + i, funded_by, b, 5 * 10**6)


def test_one_wallet_funding_the_buyers_is_one_buyer(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    crowd_of_throwaways(rpc, 200, funded_by=FUNDER)
    r = run(rpc, db, TOKEN)
    m, pts = r["metrics"], points(r)
    assert m["unique_buyers"] == 200 and pts["buyers"] == 10           # the old rules still see a crowd
    assert m["funding_visible"] is True and m["top_funder"] == FUNDER and m["top_funder_pct"] >= 50
    assert pts["funding_cluster"] == -20 and pts["fresh_buyers"] == -12
    assert r["verdict"] != "looks healthy"                            # a funding cluster is a hard warning


def test_organic_crowd_passes_both_reads(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    for i in range(200):                                              # well-used wallets, each funded by a different one
        b = addr(0x1000 + i)
        rpc.curve_buy(CURVE, LB + 40 + i, b, b, 10**21)
        rpc.transfer(TOKEN, LB + 40 + i, CURVE, b, 10**21)
        rpc.transfer(C.USDG, LB - 300 + i, addr(0x5000 + i), b, 5 * 10**6)
    r = run(rpc, db, TOKEN)
    pts = r["metrics"], points(r)
    m, pts = r["metrics"], points(r)
    assert pts["funding_cluster"] == 0 and pts["fresh_buyers"] == 0 and m["fresh_buyers_pct"] == 0.0
    assert m["partial"] is False


def test_eth_pairs_only_get_the_history_read(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    db.x("UPDATE launches SET pair_token=?, pair_symbol='ETH' WHERE token=?", (C.ZERO, TOKEN))
    crowd_of_throwaways(rpc, 200)
    r = run(rpc, db, TOKEN)
    m, pts = r["metrics"], points(r)
    assert pts["fresh_buyers"] == -12 and "funding_cluster" not in pts and m["funding_visible"] is False


def test_failed_history_read_marks_the_score_partial(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    crowd(rpc, 200)
    rpc.fail_batch = True
    r = run(rpc, db, TOKEN)
    assert r["metrics"]["partial"] is True and "fresh_buyers" not in points(r)
    assert r["verdict"] != "looks healthy"


def test_infrastructure_is_never_a_funder(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    crowd_of_throwaways(rpc, 60, funded_by=C.PONS_ROUTER, nonce=40)   # sells paid out through the router look like funding
    r = run(rpc, db, TOKEN)
    assert points(r)["funding_cluster"] == 0


# ---- fleets: the same wallets on every curve -------------------------------------------------------

def other_curves(db, wallets, n_tokens=25):
    """n_tokens earlier curves in the last day, each bought by the same wallets."""
    for k in range(n_tokens):
        t = addr(0x7000 + k)
        db.many("INSERT OR REPLACE INTO curve_buyers(token,wallet,tokens_out,ts) VALUES(?,?,?,?)",
                [(t, w, 1e21, NOW - 3600 * (k % 20 + 1)) for w in wallets])


def test_a_fleet_of_veteran_wallets_is_not_a_crowd(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    fleet = [addr(0x1000 + i) for i in range(150)]
    other_curves(db, fleet)
    crowd(rpc, 200)                                   # 150 fleet wallets + 50 organic buyers, all well used
    r = run(rpc, db, TOKEN)
    m, pts = r["metrics"], points(r)
    assert m["unique_buyers"] == 200 and m["fleet_buyers"] == 150 and m["organic_buyers"] == 50
    assert m["fleet_known_curves"] == 25 and pts["bot_fleet"] == -15
    assert pts["buyers"] == 0                          # 50 organic buyers earn nothing, the fleet earns nothing


def test_fleet_read_stays_silent_until_enough_curves_are_known(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    other_curves(db, [addr(0x1000 + i) for i in range(150)], n_tokens=8)
    crowd(rpc, 200)
    r = run(rpc, db, TOKEN)
    m, pts = r["metrics"], points(r)
    assert pts["bot_fleet"] == 0 and "needs" in [f["text"] for f in r["fired"] if f["rule"] == "bot_fleet"][0]
    assert pts["buyers"] == 0                          # the fleet is still discounted from the buyer count
    assert m["fleet_buyers"] == 150


def test_buyers_are_remembered_for_later_tokens(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    crowd(rpc, 40)
    run(rpc, db, TOKEN)
    assert db.one("SELECT COUNT(*) n FROM curve_buyers WHERE token=?", (TOKEN,))["n"] == 40


# ---- routers: a trading bot buys in its own name and hands the tokens on in the same transaction ------------

ROUTER = addr(0x9000)


def routed_crowd(rpc, n=60, first_block=LB + 40, tokens=10**21, router=ROUTER):
    """n people buying through one router: the curve pays the router, the router pays the user, one transaction each."""
    users = [addr(0xA000 + i) for i in range(n)]
    for i, u in enumerate(users):
        tx = "0x" + format(0xF000 + i, "064x")
        rpc.transfer(TOKEN, first_block + i, CURVE, router, tokens, tx=tx)
        rpc.curve_buy(CURVE, first_block + i, router, router, tokens, tx=tx)
        rpc.transfer(TOKEN, first_block + i, router, u, tokens, tx=tx)
    return users


def buy(tx, recipient, tokens, index=0):
    return {"_tx": tx, "_index": index, "recipient": recipient, "tokensOut": tokens}


def move(tx, index, frm, to, value):
    return {"_tx": tx, "_index": index, "from": frm, "to": to, "value": value}


def test_router_buys_are_followed_to_the_people_behind_them(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    users = routed_crowd(rpc, 60)
    r = run(rpc, db, TOKEN)
    m, pts = r["metrics"], points(r)
    assert m["unique_buyers"] == 60 and m["top_buyer"] in users and m["top_buyer_pct"] == round(100 / 60, 1)
    assert "top_buyer" not in pts                       # read by recipient, the router "bought 100% of the curve"
    assert m["routed_pct"] == 100.0 and m["buyers_by"] == "holder" and m["partial"] is False
    kept = {x["wallet"] for x in db.q("SELECT wallet FROM curve_buyers WHERE token=?", (TOKEN,))}
    assert kept == set(users)                           # later fleet reads compare people, never the router


def test_a_router_seen_on_every_curve_is_not_a_fleet(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    other_curves(db, [ROUTER])                          # remembered by recipient before the buyers were followed
    routed_crowd(rpc, 60)
    r = run(rpc, db, TOKEN)
    m, pts = r["metrics"], points(r)
    assert m["fleet_known_curves"] == 25 and m["fleet_buyers"] == 0 and m["fleet_pct"] == 0.0 and pts["bot_fleet"] == 0
    assert m["organic_buyers"] == 60


def test_a_fleet_behind_a_router_is_still_a_fleet(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    users = routed_crowd(rpc, 60)
    other_curves(db, users[:45])                        # 45 of the 60 people buy every curve through the same bot
    r = run(rpc, db, TOKEN)
    m, pts = r["metrics"], points(r)
    assert m["fleet_buyers"] == 45 and m["fleet_pct"] == 75.0 and pts["bot_fleet"] == -15 and m["organic_buyers"] == 15


def test_history_and_funding_are_read_for_the_people_not_the_router(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    users = routed_crowd(rpc, 60)
    funder = addr(0xFEED)
    for u in users:
        rpc.nonces[u] = 1
        rpc.transfer(C.USDG, LB - 500, funder, u, 5 * 10**6)
    r = run(rpc, db, TOKEN)
    m, pts = r["metrics"], points(r)
    assert pts["fresh_buyers"] == -12 and m["fresh_buyers_pct"] == 100.0
    assert pts["funding_cluster"] == -20 and m["top_funder"] == funder and m["top_funder_pct"] == 100.0


def test_the_creator_buying_through_a_router_is_still_the_creator(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    tx = "0x" + format(0xD00D, "064x")
    rpc.transfer(TOKEN, LB, CURVE, ROUTER, 4 * 10**21, tx=tx)
    rpc.curve_buy(CURVE, LB, ROUTER, ROUTER, 4 * 10**21, tx=tx)
    rpc.transfer(TOKEN, LB, ROUTER, DEP, 4 * 10**21, tx=tx)
    for i in range(30):
        b = addr(0x4000 + i)
        rpc.curve_buy(CURVE, LB + 100 + i, b, b, 2 * 10**20)
    r = run(rpc, db, TOKEN)
    pts, m = points(r), r["metrics"]
    assert pts["deployer_buy"] == -20 and m["deployer_buy_pct"] == 40.0
    assert pts["snipe"] == 5 and m["snipe_pct"] == 0.0 and "top_buyer" not in pts


def test_without_the_transfers_a_buy_stays_with_its_recipient_and_the_score_is_partial(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    routed_crowd(rpc, 60)
    rpc.fail_addr.add(TOKEN)
    r = run(rpc, db, TOKEN)
    m = r["metrics"]
    assert m["partial"] is True and r["retry"] is True and m["unique_buyers"] == 1 and m["routed_pct"] == 0.0


def test_one_transaction_buying_for_many_wallets_is_handed_on_in_order():
    u1, u2 = addr(1), addr(2)
    buys = [buy("0xa", ROUTER, 100), buy("0xa", ROUTER, 250)]
    moves = [move("0xa", 1, CURVE, ROUTER, 100), move("0xa", 2, ROUTER, u1, 100),
             move("0xa", 3, CURVE, ROUTER, 250), move("0xa", 4, ROUTER, u2, 250)]
    assert S.real_buyers(buys, moves, CURVE) == [[(u1, 100)], [(u2, 250)]]
    bought_first = [moves[0], moves[2], moves[1], moves[3]]      # both buys first, then both hand-overs
    for k, t in enumerate(bought_first):
        t["_index"] = k + 1
    assert S.real_buyers(buys, bought_first, CURVE) == [[(u1, 100)], [(u2, 250)]]


def test_a_hand_over_through_two_contracts_and_a_fee_cut():
    hop, user, fee = addr(3), addr(4), addr(5)
    buys = [buy("0xb", ROUTER, 1000)]
    moves = [move("0xb", 1, CURVE, ROUTER, 1000), move("0xb", 2, ROUTER, fee, 10),
             move("0xb", 3, ROUTER, hop, 990), move("0xb", 4, hop, user, 990)]
    assert sorted(S.real_buyers(buys, moves, CURVE)[0]) == sorted([(fee, 10), (user, 990)])


def test_selling_back_or_feeding_the_pool_is_not_a_hand_over():
    buys = [buy("0xc", ROUTER, 1000)]
    moves = [move("0xc", 1, CURVE, ROUTER, 1000), move("0xc", 2, ROUTER, CURVE, 400), move("0xc", 3, ROUTER, C.POOL_MANAGER, 600)]
    assert S.real_buyers(buys, moves, CURVE, set(C.INFRA) | {CURVE}) == [[(ROUTER, 1000)]]


def test_tokens_the_recipient_already_held_are_not_the_buy():
    other = addr(6)
    buys = [buy("0xd", ROUTER, 500)]
    moves = [move("0xd", 1, ROUTER, other, 500), move("0xd", 2, CURVE, ROUTER, 500)]      # sent before the curve paid
    assert S.real_buyers(buys, moves, CURVE) == [[(ROUTER, 500)]]
    assert S.real_buyers(buys, [], CURVE) == [[(ROUTER, 500)]]                             # and with nothing read
    assert S.real_buyers(buys, [move("0xe", 1, ROUTER, other, 500)], CURVE) == [[(ROUTER, 500)]]   # another transaction


# ---- the crowd's record: wallets whose earlier picks all went bad ------------------------------------------------

def losing_records(db, wallets, picks=3, good=0, tokens_on_record=200):
    db.many("INSERT OR REPLACE INTO wallet_records(wallet,picks,good,grew,updated) VALUES(?,?,?,0,?)", [(w, picks, good, NOW) for w in wallets])
    db.many("INSERT OR IGNORE INTO wallet_folded(token,ts,buyers,source) VALUES(?,?,1,'remembered')", [(addr(0xB000 + k), NOW) for k in range(tokens_on_record)])


def test_a_crowd_of_wallets_with_a_losing_record_is_a_warning(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    crowd(rpc, 100)
    losing_records(db, [addr(0x1000 + i) for i in range(40)])
    r = run(rpc, db, TOKEN)
    m, pts = r["metrics"], points(r)
    assert m["losing_pct"] == 40.0 and m["losing_buyers"] == 40 and m["crowd_history"] == 200 and pts["losing_crowd"] == -12
    assert "wallets whose earlier picks all went bad" in [f["text"] for f in r["fired"] if f["rule"] == "losing_crowd"][0]


def test_a_clean_crowd_earns_a_little_and_a_good_record_earns_nothing_more(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    crowd(rpc, 100)
    losing_records(db, [addr(0x1000 + i) for i in range(2)])
    losing_records(db, [addr(0x1000 + i) for i in range(50, 90)], picks=5, good=4)      # proven winners: measured to predict nothing
    r = run(rpc, db, TOKEN)
    assert r["metrics"]["losing_pct"] == 2.0 and r["metrics"]["known_buyers_pct"] == 42.0 and points(r)["losing_crowd"] == 3


def test_the_crowd_read_stays_silent_until_enough_tokens_are_on_record(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    crowd(rpc, 100)
    losing_records(db, [addr(0x1000 + i) for i in range(60)], tokens_on_record=20)
    r = run(rpc, db, TOKEN)
    fired = [f for f in r["fired"] if f["rule"] == "losing_crowd"][0]
    assert fired["points"] == 0 and "needs" in fired["text"] and r["metrics"]["losing_pct"] == 60.0


# ---- concentration is shown without points; linked wallets are read like a bubble map ---------------------------

def test_concentration_is_said_out_loud_and_costs_nothing(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    crowd(rpc, 40)
    whale = addr(0x777)
    rpc.transfer(TOKEN, GB, CURVE, whale, 6 * 10**22)            # one wallet holds 60% of what is in circulation
    rpc.transfer(TOKEN, GB, CURVE, DEP, 15 * 10**21)             # the creator 15%: said, not scored, and no bar below 20%
    r = run(rpc, db, TOKEN)
    top = [f for f in r["fired"] if f["rule"] == "top10"][0]
    dep = [f for f in r["fired"] if f["rule"] == "deployer_hold"][0]
    assert top["points"] == 0 and "big holders can sell into everyone" in top["text"] and r["metrics"]["top10_pct"] >= 50
    assert dep["points"] == 0 and "hard" not in dep and r["metrics"]["demoted"] is False


def senders_on_record(db, n=200, services=()):
    from wormhole import linked
    linked.ensure_tables(db)
    db.many("INSERT OR IGNORE INTO token_senders(wallet,token,ts) VALUES(?,?,?)",
            [(addr(0xD000 + k), addr(0xE000 + k), NOW) for k in range(n)] + [(w, addr(0xE000 + k), NOW) for w in services for k in range(12)])


def test_holders_who_passed_tokens_to_each_other_are_one_group(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    crowd(rpc, 60)                                               # 60 wallets, 10**21 each
    senders_on_record(db)
    boss = addr(0x1000)
    for i in range(1, 12):                                       # the first buyer tops up eleven of the others
        rpc.transfer(TOKEN, GB - 50 + i, boss, addr(0x1000 + i), 5 * 10**19)
    r = run(rpc, db, TOKEN)
    m = r["metrics"]
    said = [f for f in r["fired"] if f["rule"] == "linked_wallets"][0]
    assert m["linked_group_wallets"] == 12 and m["linked_group_pct"] == 20.0 and said["points"] == 0
    assert "one linked group of 12 wallets holds 20%" in said["text"]


def test_a_service_or_a_contract_in_the_middle_links_nobody(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    crowd(rpc, 60)
    bot, vault = addr(0x5E1), addr(0x5E2)
    senders_on_record(db, services=[bot])                        # the bot passed tokens on in twelve earlier tokens
    rpc.code[vault] = "0x6080"                                   # the vault is a contract
    for i in range(12):
        rpc.transfer(TOKEN, GB - 80 + i, addr(0x1000 + i), bot, 10**19)
        rpc.transfer(TOKEN, GB - 60 + i, addr(0x1000 + 20 + i), vault, 10**19)
    r = run(rpc, db, TOKEN)
    assert r["metrics"]["linked_group_pct"] == 0.0 and "no linked group" in [f["text"] for f in r["fired"] if f["rule"] == "linked_wallets"][0]
    assert db.one("SELECT is_contract FROM code_cache WHERE address=?", (vault,))["is_contract"] == 1


def test_a_delegating_wallet_is_still_a_person_and_a_failed_code_read_says_nothing(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    crowd(rpc, 60)
    senders_on_record(db)
    boss = addr(0x1000)
    rpc.code[boss] = "0xef0100" + "11" * 20                      # EIP-7702: an ordinary wallet that delegates
    for i in range(1, 12):
        rpc.transfer(TOKEN, GB - 50 + i, boss, addr(0x1000 + i), 5 * 10**19)
    assert run(rpc, db, TOKEN)["metrics"]["linked_group_pct"] == 20.0
    db.x("DELETE FROM code_cache")
    rpc.fail_batch = True                                        # the chain cannot be asked: no line rather than a guess
    r = run(rpc, db, TOKEN)
    assert r["metrics"].get("linked_group_pct") is None and "linked_wallets" not in points(r)


def test_the_linked_wallet_read_waits_for_history_and_remembers_this_tokens_senders_once(db):
    rpc = FakeRpc(LATEST)
    setup(db)
    crowd(rpc, 60)
    rpc.transfer(TOKEN, GB - 5, addr(0x1000), addr(0x1001), 10**20)
    r = run(rpc, db, TOKEN)
    assert "needs 150 tokens on record" in [f["text"] for f in r["fired"] if f["rule"] == "linked_wallets"][0]
    kept = db.q("SELECT wallet FROM token_senders WHERE token=?", (TOKEN,))
    assert [x["wallet"] for x in kept] == [addr(0x1000)]         # the curve paying its buyers is not a sender
    run(rpc, db, TOKEN)
    assert len(db.q("SELECT wallet FROM token_senders WHERE token=?", (TOKEN,))) == 1
