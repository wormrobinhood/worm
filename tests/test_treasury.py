"""The treasury cycle: claims settled from the escrow log, the owed balance, forwards, pending rows."""
import time

from eth_abi import decode

from eth_utils import keccak

from fakes import auto_receipts, claimed_log, decode_tx, transfer_log, tx_hash, uint_result, word
from wormhole import config as C, trader, treasury as T
from wormhole.chain import selector

BAL_OF_TOKEN = selector("balanceOfToken(address,address)")
BAL_OF = selector("balanceOf(address)")
TRANSFER = bytes.fromhex(selector("transfer(address,uint256)")[2:])
OWNER = C.OWNER_WALLET
S = C.OWNER_SHARE


def ledger(db, kind, amount, ts=None, tx=None, note=""):
    T.ensure_tables(db)
    db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note) VALUES(?,?,?,?,?,?)",
         (ts or int(time.time()), kind, "USDG", amount, tx, note))


def rows(db, kind):
    return db.q("SELECT * FROM ledger WHERE kind=? ORDER BY id", (kind,))


def chain(rpc, claimable=0.0, usdg=100.0):
    rpc.eth_calls[BAL_OF_TOKEN] = word(int(claimable * 1e6))
    rpc.eth_calls[BAL_OF] = word(int(usdg * 1e6))


def transfer_in(raw):
    t = decode_tx(raw)
    assert t["to"] == C.USDG and t["data"][:4] == TRANSFER
    to, n = decode(["address", "uint256"], t["data"][4:])
    return to.lower(), n


def test_units_round_never_truncate():
    assert [T.units(x) for x in (1, 0.29, 33.333333, 0.71)] == [1_000_000, 290_000, 33_333_333, 710_000]


def test_owed_accumulates_small_claims(db):
    for a in (1.0, 1.1, 1.45):
        ledger(db, "claim", a)
    assert abs(T.owed_to_owner(db) - 3.55 * S) < 1e-9
    ledger(db, "forward_pending", 0.5)
    assert abs(T.owed_to_owner(db) - (3.55 * S - 0.5)) < 1e-9
    ledger(db, "forward", 3.55 * S - 0.5)
    assert abs(T.owed_to_owner(db)) < 1e-9
    ledger(db, "forward_failed", 5.0)                     # failed and dropped rows do not count
    ledger(db, "claim_pending", 5.0)
    assert abs(T.owed_to_owner(db)) < 1e-9


def test_forward_is_sent_from_the_owed_balance(db, rpc, acct, live):
    for a in (1.0, 1.1, 1.45):
        ledger(db, "claim", a)
    chain(rpc, claimable=0.5, usdg=50)                    # dust in the escrow: no claim this cycle
    auto_receipts(rpc, C.WALLET)
    T.cycle(rpc, db, acct)
    assert len(rpc.raw) == 1
    assert transfer_in(rpc.raw[0]) == (OWNER, T.units(3.55 * S))
    f = rows(db, "forward")
    assert len(f) == 1 and abs(f[0]["amount"] - 3.55 * S) < 1e-6 and f[0]["tx"] == tx_hash(rpc.raw[0])
    assert rows(db, "forward_pending") == [] and abs(T.owed_to_owner(db)) < 1e-6
    ev = db.one("SELECT text FROM events WHERE kind='treasury' ORDER BY id DESC LIMIT 1")["text"]
    assert ev.startswith(f"forwarded {3.55 * S:.2f} USDG ({int(round(S * 100))}%)")


def test_pending_forward_blocks_a_second_one(db, rpc, acct, live):
    for a in (1.0, 1.1, 1.45):
        ledger(db, "claim", a)
    h = "0x" + "aa" * 32
    ledger(db, "forward_pending", 0.71, tx=h, note="20% of income to the creator")
    rpc.receipts[h] = None                                # broadcast, not mined yet
    chain(rpc, claimable=0.5, usdg=50)
    T.cycle(rpc, db, acct)
    assert rpc.raw == [] and len(rows(db, "forward_pending")) == 1
    assert ("eth_getTransactionReceipt", [h]) in rpc.calls


def test_reverted_claim_leaves_no_claim_row_and_no_forward(db, rpc, acct, live):
    chain(rpc, claimable=5.0, usdg=50)
    rpc.status = "0x0"
    T.cycle(rpc, db, acct)
    assert len(rpc.raw) == 1 and decode_tx(rpc.raw[0])["to"] == C.FEE_ESCROW
    assert rows(db, "claim") == [] and rows(db, "claim_pending") == [] and rows(db, "forward") == []
    assert len(rows(db, "claim_failed")) == 1 and abs(T.owed_to_owner(db)) < 1e-9
    assert db.one("SELECT text FROM events WHERE kind='error'")["text"].startswith("claim reverted")


def test_claim_amount_comes_from_the_escrow_log_then_the_share_is_forwarded(db, rpc, acct, live):
    chain(rpc, claimable=5.0, usdg=50)
    auto_receipts(rpc, C.WALLET, claimed_units=5_250_000)    # the escrow paid a little more than the pre-tx read
    T.cycle(rpc, db, acct)
    assert len(rpc.raw) == 2
    c = rows(db, "claim")
    assert len(c) == 1 and abs(c[0]["amount"] - 5.25) < 1e-9 and c[0]["tx"] == tx_hash(rpc.raw[0])
    assert transfer_in(rpc.raw[1]) == (OWNER, T.units(5.25 * S))
    f = rows(db, "forward")
    assert len(f) == 1 and abs(f[0]["amount"] - 5.25 * S) < 1e-6
    texts = [e["text"] for e in db.q("SELECT text FROM events WHERE kind='treasury' ORDER BY id")]
    assert texts == ["claimed 5.25 USDG of creator fees", f"forwarded {5.25 * S:.2f} USDG ({int(round(S * 100))}%) to the creator"]


def test_claim_without_the_log_keeps_the_pre_tx_amount(db, rpc, acct, live):
    chain(rpc, claimable=2.0, usdg=50)
    auto_receipts(rpc, C.WALLET)                             # status 0x1, no ClaimedToken log
    T.cycle(rpc, db, acct)
    c = rows(db, "claim")
    assert len(c) == 1 and c[0]["amount"] == 2.0


def test_a_transfer_needs_its_transfer_log(db, rpc, acct, live):
    for a in (1.0, 1.1, 1.45):
        ledger(db, "claim", a)
    chain(rpc, claimable=0, usdg=50)                         # status 0x1 but no log: the token returned false
    T.cycle(rpc, db, acct)
    assert rows(db, "forward") == [] and len(rows(db, "forward_failed")) == 1
    assert abs(T.owed_to_owner(db) - 3.55 * S) < 1e-6        # still owed
    assert "no Transfer log" in db.one("SELECT text FROM events WHERE kind='error'")["text"]


def test_a_transfer_log_with_another_amount_does_not_count(db, rpc, acct, live):
    ledger(db, "claim", 10.0)
    chain(rpc, claimable=0, usdg=50)
    rpc.logs = [transfer_log(C.USDG, C.WALLET, OWNER, 1_999_999)]
    T.cycle(rpc, db, acct)
    assert rows(db, "forward") == [] and len(rows(db, "forward_failed")) == 1


def test_reconcile_settles_a_pending_claim_from_its_receipt(db, rpc, acct, live):
    h = "0x" + "bb" * 32
    ledger(db, "claim_pending", 2.9, tx=h, note="creator fees claimed from pons escrow")
    rpc.receipts[h] = {"status": "0x1", "blockNumber": "0x10", "logs": [claimed_log(C.WALLET, C.USDG, 3_000_000)]}
    chain(rpc, claimable=0, usdg=50)
    auto_receipts(rpc, C.WALLET)
    T.cycle(rpc, db, acct)
    c = rows(db, "claim")
    assert len(c) == 1 and c[0]["amount"] == 3.0 and c[0]["tx"] == h
    assert transfer_in(rpc.raw[0]) == (OWNER, T.units(3.0 * S))
    assert len(rows(db, "forward")) == 1


def test_reconcile_reverted_pending_forward_returns_the_amount_to_owed(db, rpc, acct, live):
    ledger(db, "claim", 10.0)
    h = "0x" + "cd" * 32
    ledger(db, "forward_pending", 2.0, tx=h, note="20% of income to the creator")
    rpc.receipts[h] = {"status": "0x0", "blockNumber": "0x10", "logs": []}
    chain(rpc, claimable=0, usdg=50)
    auto_receipts(rpc, C.WALLET)
    T.cycle(rpc, db, acct)
    assert len(rows(db, "forward_failed")) == 1
    assert transfer_in(rpc.raw[0]) == (OWNER, T.units(10.0 * S))   # sent again in the same cycle
    assert len(rows(db, "forward")) == 1 and abs(T.owed_to_owner(db)) < 1e-6


def test_reconcile_writes_off_a_tx_the_node_dropped(db, rpc, acct, live):
    h = "0x" + "cc" * 32
    ledger(db, "forward_pending", 0.5, ts=int(time.time()) - 7200, tx=h, note="20% of income to the creator")
    rpc.receipts[h] = None
    chain(rpc, claimable=0, usdg=50)
    T.cycle(rpc, db, acct)
    assert len(rows(db, "forward_dropped")) == 1 and rows(db, "forward_pending") == []
    assert "written off" in db.one("SELECT text FROM events WHERE kind='error'")["text"]


def test_young_pending_rows_wait(db, rpc, acct, live):
    h = "0x" + "dd" * 32
    ledger(db, "claim_pending", 2.0, tx=h, note="creator fees claimed from pons escrow")
    rpc.receipts[h] = None
    chain(rpc, claimable=9.0, usdg=50)
    T.cycle(rpc, db, acct)
    assert rpc.raw == [] and len(rows(db, "claim_pending")) == 1


def test_forward_is_capped_by_the_wallet_balance(db, rpc, acct, live):
    ledger(db, "claim", 10.0)                                # owed 2.00
    chain(rpc, claimable=0, usdg=1.25)
    auto_receipts(rpc, C.WALLET)
    T.cycle(rpc, db, acct)
    assert transfer_in(rpc.raw[0]) == (OWNER, 1_250_000)
    assert abs(T.owed_to_owner(db) - (10.0 * S - 1.25)) < 1e-6


def test_below_the_minimum_nothing_is_forwarded(db, rpc, acct, live):
    ledger(db, "claim", 0.5)                                 # owed 0.30 < 0.50
    chain(rpc, claimable=0, usdg=50)
    T.cycle(rpc, db, acct)
    assert rpc.raw == []


def test_nothing_happens_unarmed(db, rpc, acct):
    chain(rpc, claimable=50, usdg=50)
    T.cycle(rpc, db, acct)
    assert rpc.calls == []


def test_no_owner_means_no_forward(db, rpc, acct, live, monkeypatch):
    monkeypatch.setattr(C, "OWNER_WALLET", "")
    ledger(db, "claim", 10.0)
    chain(rpc, claimable=0, usdg=50)
    T.cycle(rpc, db, acct)
    assert rpc.raw == []


def test_summary_reports_totals_and_owed(db, rpc):
    ledger(db, "claim", 10.0)
    ledger(db, "forward", 1.0)
    ledger(db, "compute", 5.0)
    chain(rpc, claimable=1.5, usdg=9.0)
    s = T.summary(rpc, db)
    assert s["claimed_total"] == 10.0 and s["forwarded_total"] == 1.0 and s["compute_total"] == 5.0
    assert abs(s["owed_to_owner"] - (10.0 * S - 1.0)) < 1e-6 and s["claimable_usdg"] == 1.5 and s["usdg"] == 9.0
    assert abs(s["owed_to_burn"] - 10.0 * C.BURN_SHARE) < 1e-6 and s["burn_state"] == "waits for the token"
    assert s["burn_share"] == C.BURN_SHARE and s["ops_share"] == C.OPS_SHARE and s["trading"] is False
    assert len(s["ledger"]) == 3


# ---- the burn: the burn share of every claim buys $WORM on its pool and sends it to the burn address ----

TOKEN = "0x" + "77" * 20
QUOTE_SEL = "0x" + keccak(text=f"quoteExactInputSingle({trader.QUOTE_T})")[:4].hex()
ALLOWANCE = selector("allowance(address,address)")
P2_ALLOWANCE = selector("allowance(address,address,address)")
APPROVE_SEL = bytes.fromhex(selector("approve(address,uint256)")[2:])
P2_APPROVE_SEL = bytes.fromhex(selector("approve(address,address,uint160,uint48)")[2:])
EXECUTE_SEL = bytes.fromhex(selector("execute(bytes,bytes[],uint256)")[2:])


def pool(db):
    trader.ensure_tables(db)
    db.x("INSERT OR REPLACE INTO pools(token,c0,c1,fee,tick_spacing,hooks,quote) VALUES(?,?,?,?,?,?,?)",
         (TOKEN, C.USDG, TOKEN, 10000, 200, C.HOOK, C.USDG))


def burn_chain(rpc, usdg=50.0, out_tokens=12_345, approved=False):
    chain(rpc, claimable=0, usdg=usdg)
    rpc.eth_calls[QUOTE_SEL] = uint_result(out_tokens * 10 ** 18, 100_000)
    rpc.eth_calls[ALLOWANCE] = word(2 ** 200 if approved else 0)
    rpc.eth_calls[P2_ALLOWANCE] = uint_result(2 ** 160 - 1 if approved else 0, 2 ** 48 - 1 if approved else 0, 0)


def burn_receipts(rpc, qty_tokens=12_345):
    """auto_receipts for claims and transfers; the router call's receipt carries $WORM's Transfer to the burn address."""
    auto_receipts(rpc, C.WALLET)
    base = rpc.receipt_for

    def receipt(h):
        r = base(h)
        raw = next((x for x in rpc.raw if tx_hash(x) == h), None)
        if r and raw and decode_tx(raw)["to"] == C.UNIVERSAL_ROUTER and qty_tokens:
            r["logs"] = [transfer_log(TOKEN, C.POOL_MANAGER, C.DEAD, qty_tokens * 10 ** 18)]
        return r
    rpc.receipt_for = receipt


def test_owed_to_burn_accumulates_and_the_free_treasury_excludes_what_is_owed(db):
    for a in (1.0, 1.1, 1.45):
        ledger(db, "claim", a)
    assert abs(T.owed_to_burn(db) - 3.55 * C.BURN_SHARE) < 1e-9
    assert abs(T.free_usd(db, 10.0) - round(10.0 - 3.55 * (S + C.BURN_SHARE), 2)) < 1e-6
    ledger(db, "burn_pending", 0.5)
    assert abs(T.owed_to_burn(db) - (3.55 * C.BURN_SHARE - 0.5)) < 1e-9
    ledger(db, "burn", 3.55 * C.BURN_SHARE - 0.5)
    assert abs(T.owed_to_burn(db)) < 1e-9
    ledger(db, "burn_failed", 4.0)                         # failed and dropped rows do not count
    assert abs(T.owed_to_burn(db)) < 1e-9
    assert T.free_usd(db, 1.0) == 0.0                      # never negative


def test_burn_waits_for_the_token_the_minimum_and_the_pool(db, rpc, acct, live, monkeypatch):
    ledger(db, "claim", 30.0)                              # owed to the burn: 6.00
    burn_chain(rpc)
    auto_receipts(rpc, C.WALLET)
    T.cycle(rpc, db, acct)                                 # no token yet: the creator's forward only
    assert len(rpc.raw) == 1 and rows(db, "burn_pending") == [] and rows(db, "burn") == []
    monkeypatch.setattr(C, "TOKEN", TOKEN)
    db.x("DELETE FROM ledger")
    ledger(db, "claim", 10.0)                              # owed 2.00, under the 5.00 minimum
    T.cycle(rpc, db, acct)
    assert len(rpc.raw) == 2 and rows(db, "burn") == []
    db.x("DELETE FROM ledger")
    ledger(db, "claim", 30.0)                              # owed 6.00 but no pool yet
    T.cycle(rpc, db, acct)
    assert len(rpc.raw) == 3 and rows(db, "burn") == [] and rows(db, "burn_pending") == []
    assert "waits to be burned" in db.one("SELECT text FROM events WHERE kind='treasury' ORDER BY id DESC LIMIT 1")["text"]
    assert abs(T.owed_to_burn(db) - 6.0) < 1e-9


def test_burn_buys_on_the_pool_and_sends_the_tokens_to_the_burn_address(db, rpc, acct, live, monkeypatch):
    monkeypatch.setattr(C, "TOKEN", TOKEN)
    pool(db)
    ledger(db, "claim", 30.0)                              # forward 18.00, burn 6.00
    burn_chain(rpc, out_tokens=12_345)
    burn_receipts(rpc, qty_tokens=12_345)
    T.cycle(rpc, db, acct)
    txs = [decode_tx(r) for r in rpc.raw]
    assert [t["to"] for t in txs] == [C.USDG, C.USDG, C.PERMIT2, C.UNIVERSAL_ROUTER]
    assert txs[0]["data"][:4] == TRANSFER and txs[1]["data"][:4] == APPROVE_SEL and txs[2]["data"][:4] == P2_APPROVE_SEL
    spender, allowance = decode(["address", "uint256"], txs[1]["data"][4:])
    assert spender.lower() == C.PERMIT2 and allowance == 2 ** 256 - 1
    tok, spender2, amt, exp = decode(["address", "address", "uint160", "uint48"], txs[2]["data"][4:])
    assert (tok.lower(), spender2.lower(), amt, exp) == (C.USDG, C.UNIVERSAL_ROUTER, 2 ** 160 - 1, 2 ** 48 - 1)
    assert txs[3]["data"][:4] == EXECUTE_SEL and txs[3]["value"] == 0
    commands, inputs, _deadline = decode(["bytes", "bytes[]", "uint256"], txs[3]["data"][4:])
    actions, params = decode(["bytes", "bytes[]"], inputs[0])
    assert commands == trader.V4_SWAP and actions == trader.SWAP_EXACT_IN_SINGLE + trader.SETTLE_ALL + T.TAKE
    key, zero_for_one, amount_in, min_out, price_limit, _hook = decode([trader.SWAP_T], params[0])[0]
    assert price_limit == 0
    assert (key[0].lower(), key[1].lower(), zero_for_one, amount_in) == (C.USDG, TOKEN, True, 6_000_000)
    assert min_out == int(12_345 * 10 ** 18 * (1 - T.BURN_SLIPPAGE))
    settle_cur, settle_amt = decode(["address", "uint256"], params[1])
    assert settle_cur.lower() == C.USDG and settle_amt == 6_000_000
    cur, to, amount = decode(["address", "address", "uint256"], params[2])
    assert (cur.lower(), to.lower(), amount) == (TOKEN, C.DEAD, 0)   # the whole output, straight to the burn address
    b = rows(db, "burn")
    assert len(b) == 1 and abs(b[0]["amount"] - 6.0) < 1e-9 and b[0]["qty"] == 12_345.0 and b[0]["tx"] == tx_hash(rpc.raw[3])
    assert rows(db, "burn_pending") == [] and abs(T.owed_to_burn(db)) < 1e-9
    texts = [e["text"] for e in db.q("SELECT text FROM events WHERE kind='treasury' ORDER BY id")]
    assert texts[-1] == "burned 12,345 $WORM bought with 6.00 USDG"
    assert any(x.startswith("approved USDG for Permit2") for x in texts) and any("Universal Router" in x for x in texts)
    s = T.summary(rpc, db)
    assert s["burned_total"] == 6.0 and s["burned_qty"] == 12_345.0 and s["owed_to_burn"] == 0.0


def test_burn_skips_the_approvals_when_they_already_stand(db, rpc, acct, live, monkeypatch):
    monkeypatch.setattr(C, "TOKEN", TOKEN)
    pool(db)
    ledger(db, "claim", 30.0)
    burn_chain(rpc, approved=True)
    burn_receipts(rpc)
    T.cycle(rpc, db, acct)
    assert [decode_tx(r)["to"] for r in rpc.raw] == [C.USDG, C.UNIVERSAL_ROUTER]
    assert len(rows(db, "burn")) == 1


def test_a_burn_settles_only_when_tokens_reach_the_burn_address(db, rpc, acct, live, monkeypatch):
    monkeypatch.setattr(C, "TOKEN", TOKEN)
    pool(db)
    ledger(db, "claim", 30.0)
    burn_chain(rpc, approved=True)
    burn_receipts(rpc, qty_tokens=0)                       # status 0x1, nothing at the burn address
    T.cycle(rpc, db, acct)
    assert rows(db, "burn") == [] and len(rows(db, "burn_failed")) == 1
    assert abs(T.owed_to_burn(db) - 6.0) < 1e-9            # still owed
    assert "no $WORM reached the burn address" in db.one("SELECT text FROM events WHERE kind='error'")["text"]


def test_burn_is_capped_by_the_wallet_balance_and_a_pending_burn_blocks_another(db, rpc, acct, live, monkeypatch):
    monkeypatch.setattr(C, "TOKEN", TOKEN)
    pool(db)
    monkeypatch.setattr(C, "OWNER_WALLET", "")             # no forward: the wallet's USDG is for the burn alone
    ledger(db, "claim", 100.0)                             # owed 20.00
    burn_chain(rpc, usdg=7.5, approved=True)
    burn_receipts(rpc, qty_tokens=999)
    T.cycle(rpc, db, acct)
    b = rows(db, "burn")
    assert len(b) == 1 and abs(b[0]["amount"] - 7.5) < 1e-9 and abs(T.owed_to_burn(db) - 12.5) < 1e-9
    h = "0x" + "ee" * 32
    ledger(db, "burn_pending", 5.0, tx=h, note="in flight")
    rpc.receipts[h] = None
    T.cycle(rpc, db, acct)
    assert len(rpc.raw) == 1                               # nothing new while a burn is in flight


def test_a_transaction_in_flight_marks_the_worm_busy_until_it_settles(monkeypatch):
    monkeypatch.setattr(T, "WATCH", None)
    monkeypatch.setattr(T, "BUSY_SINCE", 0.0)
    assert not T.working()
    T.watch("claim", "claiming", "0x1")                 # broadcast
    assert T.working()
    T.watch("claim", "claimed", "0x1", done=True)        # settled
    assert not T.working()
    T.watch("burn", "burning", "0x2")
    monkeypatch.setattr(T, "BUSY_SINCE", T.BUSY_SINCE - T.BUSY_MAX_S - 1)   # a transaction that never settles
    assert not T.working()
