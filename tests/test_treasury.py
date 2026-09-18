"""The treasury cycle: claims settled from the escrow log, the owed balance, forwards, pending rows."""
import time
import pytest

from eth_abi import decode

from eth_utils import keccak

from fakes import auto_receipts, claimed_log, decode_tx, transfer_log, tx_hash, uint_result, word
from wormhole import config as C, trader, treasury as T
from wormhole.chain import selector

@pytest.fixture(autouse=True)
def fresh_claim_price(monkeypatch):
    from wormhole import prices, claim_policy
    monkeypatch.setattr(prices, 'eth_usd', lambda **kw: 2500.0)
    monkeypatch.setattr(claim_policy, 'eth_usd', lambda **kw: 2500.0)
    monkeypatch.setattr(claim_policy, 'funded_runway', lambda *args: None)


BAL_OF_TOKEN = selector("balanceOfToken(address,address)")
BAL_OF = selector("balanceOf(address)")
TRANSFER = bytes.fromhex(selector("transfer(address,uint256)")[2:])
OWNER = C.OWNER_WALLET
S = C.OWNER_SHARE


def ledger(db, kind, amount, ts=None, tx=None, note=""):
    T.ensure_tables(db)
    db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note,owner_share,burn_share,gold_share) VALUES(?,?,?,?,?,?,?,?,?)",
         (ts or int(time.time()), kind, "USDG", amount, tx, note, C.OWNER_SHARE, C.BURN_SHARE, C.GOLD_SHARE))


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


def test_claim_without_event_keeps_funds_reserved(db, rpc, acct, live):
    chain(rpc, claimable=5.0, usdg=50)
    auto_receipts(rpc, C.WALLET)                             # status 0x1, no ClaimedToken log
    T.cycle(rpc, db, acct)
    assert rows(db, 'claim') == []
    assert len(rows(db, 'claim_pending')) == 1
    assert T.free_usd(db, 50) == 0


def test_a_transfer_needs_its_transfer_log(db, rpc, acct, live):
    for a in (1.0, 1.1, 1.45):
        ledger(db, "claim", a)
    chain(rpc, claimable=0, usdg=50)                         # status 0x1 but no log: the token returned false
    T.cycle(rpc, db, acct)
    assert rows(db, "forward") == [] and len(rows(db, "forward_pending")) == 1
    assert abs(T.owed_to_owner(db)) < 1e-6        # reserved in pending, not due for another send
    assert "receipt evidence incomplete" in db.one("SELECT text FROM events WHERE kind='error'")["text"]


def test_a_transfer_log_with_another_amount_does_not_count(db, rpc, acct, live):
    ledger(db, "claim", 10.0)
    chain(rpc, claimable=0, usdg=50)
    rpc.logs = [transfer_log(C.USDG, C.WALLET, OWNER, 1_999_999)]
    T.cycle(rpc, db, acct)
    assert rows(db, "forward") == [] and len(rows(db, "forward_pending")) == 1


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


def test_reconcile_preserves_unknown_transaction_regardless_of_age(db, rpc, acct, live):
    h = "0x" + "cc" * 32
    ledger(db, "forward_pending", 0.5, ts=int(time.time()) - 7200, tx=h, note="20% of income to the creator")
    rpc.receipts[h] = None
    chain(rpc, claimable=0, usdg=50)
    T.cycle(rpc, db, acct)
    assert rows(db, "forward_dropped") == [] and len(rows(db, "forward_pending")) == 1
    assert rpc.raw == []  # absence on one node is not proof of failure


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
    approval_reads(rpc, 6_000_000 if approved else 0,
                   6_000_000 if approved else 0, int(time.time()) + T.PERMIT_TTL_S if approved else 0)


def approval_reads(rpc, erc20=0, permit=0, expiry=0):
    """Model allowance reads from successful approval receipts, including ERC-20 false/no-op cases."""
    def mined_approvals():
        for raw in rpc.raw:
            receipt = rpc.receipt(tx_hash(raw))
            if receipt and receipt.get('status') == '0x1':
                yield decode_tx(raw)
    def erc_read(params):
        _, spender = decode(['address', 'address'], bytes.fromhex(params[0]['data'][10:]))
        amount = erc20
        for t in mined_approvals():
            if t['to'] == C.USDG and t['data'][:4] == APPROVE_SEL:
                target, value = decode(['address', 'uint256'], t['data'][4:])
                if target == spender:
                    amount = value
        return word(amount)
    def permit_read(params):
        amount, expiration = permit, expiry
        for t in mined_approvals():
            if t['to'] == C.PERMIT2 and t['data'][:4] == P2_APPROVE_SEL:
                _, _, amount, expiration = decode(['address', 'address', 'uint160', 'uint48'], t['data'][4:])
        return uint_result(amount, expiration, 0)
    rpc.eth_calls[ALLOWANCE] = erc_read
    rpc.eth_calls[P2_ALLOWANCE] = permit_read


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
    assert abs(T.free_usd(db, 10.0) - round(10.0 - 3.55 * (S + C.BURN_SHARE + C.GOLD_SHARE), 2)) < 1e-6
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
    assert spender.lower() == C.PERMIT2 and allowance == 6_000_000
    tok, spender2, amt, exp = decode(["address", "address", "uint160", "uint48"], txs[2]["data"][4:])
    assert (tok.lower(), spender2.lower(), amt) == (C.USDG, C.UNIVERSAL_ROUTER, 6_000_000)
    assert int(time.time()) < exp <= int(time.time()) + T.PERMIT_TTL_S
    assert txs[3]["data"][:4] == EXECUTE_SEL and txs[3]["value"] == 0
    commands, inputs, deadline = decode(["bytes", "bytes[]", "uint256"], txs[3]["data"][4:])
    assert int(time.time()) < deadline <= min(exp, int(time.time()) + T.SWAP_TTL_S)
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
    assert rows(db, "burn") == [] and len(rows(db, "burn_pending")) == 1
    assert abs(T.owed_to_burn(db)) < 1e-9            # reserved in pending, not due for another send
    assert "receipt evidence incomplete" in db.one("SELECT text FROM events WHERE kind='error'")["text"]


def test_burn_is_capped_by_the_wallet_balance_and_a_pending_burn_blocks_another(db, rpc, acct, live, monkeypatch):
    monkeypatch.setattr(C, "TOKEN", TOKEN)
    pool(db)
    monkeypatch.setattr(C, "OWNER_WALLET", "")             # no forward: the wallet's USDG is for the burn alone
    ledger(db, "claim", 100.0)                             # owed 20.00
    burn_chain(rpc, usdg=7.5, approved=True)
    approval_reads(rpc, 7_500_000, 7_500_000, int(time.time()) + T.PERMIT_TTL_S)
    burn_receipts(rpc, qty_tokens=999)
    T.cycle(rpc, db, acct)
    b = rows(db, "burn")
    assert len(b) == 1 and abs(b[0]["amount"] - 7.5) < 1e-9 and abs(T.owed_to_burn(db) - 12.5) < 1e-9
    h = "0x" + "ee" * 32
    ledger(db, "burn_pending", 5.0, tx=h, note="in flight")
    rpc.receipts[h] = None
    T.cycle(rpc, db, acct)
    assert len(rpc.raw) == 1                               # nothing new while a burn is in flight


# ---- gold: the gold share buys GLD on its v3 pool and keeps it as a reserve ----

QUOTE_V3_SEL = selector(f"quoteExactInputSingle({T.QUOTE_V3_T})")
GLD_UNITS = 12_500_000_000_000_000                        # 0.0125 GLD for 5 USDG at $400


SWAP_V3_SEL = bytes.fromhex(selector(f"exactInputSingle({T.SWAP_V3_T})")[2:])


def gold_chain(rpc, usdg=50.0, out_units=GLD_UNITS, approved=False):
    chain(rpc, claimable=0, usdg=usdg)
    rpc.eth_calls[QUOTE_V3_SEL] = uint_result(out_units, 0, 0, 90_000)
    approval_reads(rpc, 6_000_000 if approved else 0)


def gold_receipts(rpc, qty_units=GLD_UNITS):
    auto_receipts(rpc, C.WALLET)
    base = rpc.receipt_for

    def receipt(h):
        r = base(h)
        raw = next((x for x in rpc.raw if tx_hash(x) == h), None)
        if r and raw and decode_tx(raw)["to"] == C.SWAP_ROUTER_V3 and qty_units:
            r["logs"] = [transfer_log(C.GLD, "0x" + "55" * 20, C.WALLET, qty_units)]
        return r
    rpc.receipt_for = receipt


def test_reconcile_leaves_rows_without_a_hash_alone(db, rpc, acct, live):
    """An x402 payment in flight has no chain hash: it is compute's own row, never written off by the treasury
    (which would reopen the daily cap), and it does not block the cycle."""
    ledger(db, "compute_pending", 5.0, tx=None, note="top-up in flight")
    chain(rpc, claimable=0)
    auto_receipts(rpc, C.WALLET)
    assert T.reconcile(rpc, db, ("compute_pending",), C.WALLET) == 0
    assert rows(db, "compute_pending") and rows(db, "compute_dropped") == []


def test_a_forward_is_reconciled_against_the_wallet_it_went_to(db, rpc, acct, live, monkeypatch):
    """The recipient is stored with the row: changing WH_OWNER_WALLET later cannot fail an old forward and pay twice."""
    old = "0x" + "aa" * 20
    h = "0x" + "bb" * 32
    T.ensure_tables(db)
    db.x("INSERT INTO ledger(ts,kind,asset,amount,tx,note,to_addr) VALUES(?,?,?,?,?,?,?)",
         (int(time.time()), "forward_pending", "USDG", 2.0, h, "60% of income to the creator", old))
    rpc.receipts[h] = {"transactionHash": h, "status": "0x1", "blockNumber": "0x10", "logs": [transfer_log(C.USDG, C.WALLET, old, 2_000_000)]}
    monkeypatch.setattr(C, "OWNER_WALLET", "0x" + "cc" * 20)
    T.reconcile(rpc, db, ("forward_pending",), C.WALLET, lambda r: C.OWNER_WALLET)
    assert [r["kind"] for r in db.q("SELECT kind FROM ledger")] == ["forward"]


def test_finish_rebuilds_a_row_the_broadcast_callback_failed_to_write(db, rpc, acct, live):
    T.ensure_tables(db)
    h = "0x" + "dd" * 32
    rc = {"transactionHash": h, "status": "0x1", "blockNumber": "0x10", "logs": [transfer_log(C.USDG, C.WALLET, OWNER, 3_000_000)]}
    kind, amount = T.finish(db, h, rc, C.WALLET, to=OWNER, fallback={"kind": "forward_pending", "amount": 3.0, "note": "n", "to_addr": OWNER})
    assert kind == "forward" and amount == 3.0 and rows(db, "forward")[0]["tx"] == h
    assert "rebuilt from the receipt" in db.one("SELECT text FROM events WHERE kind='error'")["text"]


def test_owed_to_gold_accumulates_and_waits_for_the_minimum(db, rpc, acct, live):
    ledger(db, "claim", 30.0)                              # gold share 3.00, under the 5.00 minimum
    gold_chain(rpc)
    gold_receipts(rpc)
    T.cycle(rpc, db, acct)
    assert [decode_tx(r)["to"] for r in rpc.raw] == [C.USDG]        # the creator's forward only
    assert abs(T.owed_to_gold(db) - 30.0 * C.GOLD_SHARE) < 1e-9 and rows(db, "gold") == []
    assert T.summary(rpc, db)["gold_state"] == "buys gold once $5 is owed"


def test_gold_buys_gld_on_the_v3_pool_and_keeps_it(db, rpc, acct, live):
    ledger(db, "claim", 60.0)                              # forward 30, gold 6.00 (no token: no burn)
    gold_chain(rpc)
    gold_receipts(rpc)
    T.cycle(rpc, db, acct)
    txs = [decode_tx(r) for r in rpc.raw]
    assert [t["to"] for t in txs] == [C.USDG, C.USDG, C.SWAP_ROUTER_V3]          # forward, exact approval, swap
    assert txs[1]["data"][:4] == APPROVE_SEL
    spender, allowance = decode(["address", "uint256"], txs[1]["data"][4:])
    assert spender.lower() == C.SWAP_ROUTER_V3 and allowance == 6_000_000       # this buy's amount, nothing unbounded
    assert txs[2]["data"][:4] == bytes.fromhex(selector('multicall(uint256,bytes[])')[2:]) and txs[2]["value"] == 0
    deadline, calls = decode(['uint256', 'bytes[]'], txs[2]['data'][4:])
    assert int(time.time()) < deadline <= int(time.time()) + T.SWAP_TTL_S
    assert len(calls) == 1 and calls[0][:4] == SWAP_V3_SEL
    token_in, token_out, fee, recipient, amount_in, min_out, limit = decode([T.SWAP_V3_T], calls[0][4:])[0]
    assert (token_in.lower(), token_out.lower(), fee, recipient.lower(), amount_in, limit) == (C.USDG, C.GLD, 500, C.WALLET, 6_000_000, 0)
    assert min_out == int(GLD_UNITS * (1 - T.GOLD_SLIPPAGE))
    g = rows(db, "gold")
    assert len(g) == 1 and abs(g[0]["amount"] - 6.0) < 1e-9 and abs(g[0]["qty"] - 0.0125) < 1e-12 and g[0]["tx"] == tx_hash(rpc.raw[2])
    assert rows(db, "gold_pending") == [] and abs(T.owed_to_gold(db)) < 1e-9
    texts = [e["text"] for e in db.q("SELECT text FROM events WHERE kind='treasury' ORDER BY id")]
    assert texts[-1] == "bought 0.0125 GLD of gold with 6.00 USDG for its reserve"
    s = T.summary(rpc, db)
    assert s["gold_total"] == 6.0 and s["gold_qty"] == 0.0125 and s["owed_to_gold"] == 0.0 and s["gold_share"] == C.GOLD_SHARE


def test_a_gold_buy_settles_only_when_gld_reaches_the_wallet(db, rpc, acct, live, monkeypatch):
    monkeypatch.setattr(C, "OWNER_WALLET", "")
    ledger(db, "claim", 60.0)
    gold_chain(rpc)
    gold_receipts(rpc, qty_units=0)                        # status 0x1, no GLD arrived
    T.cycle(rpc, db, acct)
    assert rows(db, "gold") == [] and len(rows(db, "gold_pending")) == 1
    assert [decode_tx(r)["to"] for r in rpc.raw] == [C.USDG, C.SWAP_ROUTER_V3]  # standing allowance reused, no new approval
    assert abs(T.owed_to_gold(db)) < 1e-9
    assert "receipt evidence incomplete" in db.one("SELECT text FROM events WHERE kind='error'")["text"]
    h = "0x" + "cc" * 32
    ledger(db, "gold_pending", 5.0, tx=h, note="in flight")
    rpc.receipts[h] = None
    T.cycle(rpc, db, acct)
    assert len(rpc.raw) == 2                               # nothing new while a gold buy is in flight


def test_gold_never_counts_for_money_decisions(db):
    ledger(db, "claim", 20.0)
    ledger(db, "gold", 2.0)
    assert abs(T.owed_to_gold(db)) < 1e-9
    assert abs(T.free_usd(db, 50.0) - round(50.0 - 20.0 * (S + C.BURN_SHARE), 2)) < 1e-6


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


# ---- trading profit to the burn, above a high-water mark ------------------------------------------

def live_position(db, i, size, realized, status="closed"):
    trader.ensure_tables(db)
    db.x("INSERT INTO positions(token,symbol,size_usd,realized_usd,status,mode) VALUES(?,?,?,?,?,'live')",
         ("0x" + f"{i:040x}", f"P{i}", size, realized, status))


def test_trading_profit_is_owed_to_the_burn_only_above_the_high_water_mark(db):
    T.ensure_tables(db)
    assert T.sweep_trading_profit(db) == 0.0                         # no trader tables yet: nothing to sweep
    live_position(db, 1, 10.0, 16.0)                                 # +6
    live_position(db, 2, 10.0, 99.0, status="open")                  # an open position is no profit yet
    assert T.sweep_trading_profit(db) == pytest.approx(6.0) and T.owed_to_burn(db) == pytest.approx(6.0)
    assert T.sweep_trading_profit(db) == 0.0                         # the same profit is never swept twice
    live_position(db, 3, 10.0, 6.5)                                  # -3.5: lifetime +2.5, under the +6 high
    assert T.sweep_trading_profit(db) == 0.0
    live_position(db, 4, 10.0, 13.0)                                 # +3: lifetime +5.5, still under the high
    assert T.sweep_trading_profit(db) == 0.0 and T.owed_to_burn(db) == pytest.approx(6.0)
    live_position(db, 5, 10.0, 12.2)                                 # +2.2: lifetime +7.7, 1.7 above the old high
    assert T.sweep_trading_profit(db) == pytest.approx(1.7) and T.owed_to_burn(db) == pytest.approx(7.7)
    notes = [r["note"] for r in rows(db, "trade_profit")]
    assert len(notes) == 2 and "high-water mark" in notes[0]
    assert "owed to the burn" in db.one("SELECT text FROM events WHERE kind='treasury' ORDER BY id DESC LIMIT 1")["text"]


def test_wasted_gas_counts_against_trading_profit_and_small_gains_wait(db):
    live_position(db, 1, 10.0, 10.8)                                 # +0.8: under the sweep minimum
    assert T.sweep_trading_profit(db) == 0.0
    live_position(db, 2, 10.0, 11.0)                                 # lifetime +1.8
    db.x("INSERT INTO trades(ts,token,side,mode,note,gas_usd) VALUES(?,?,'buy','live','REVERTED',0.5)", (int(time.time()), "0x" + "77" * 20))
    assert T.sweep_trading_profit(db) == pytest.approx(1.3)


def test_the_burn_share_of_trading_profit_is_configurable_and_bounded(db, monkeypatch):
    live_position(db, 1, 10.0, 20.0)
    monkeypatch.setenv("WH_TRADING_BURN_SHARE", "0.5")
    assert T.sweep_trading_profit(db) == pytest.approx(5.0)
    live_position(db, 2, 10.0, 20.0)
    monkeypatch.setenv("WH_TRADING_BURN_SHARE", "7")                 # nonsense falls back to all of it
    assert T.sweep_trading_profit(db) == pytest.approx(10.0)


def test_swept_profit_is_reserved_like_any_other_burn_money(db):
    ledger(db, "claim", 10.0)
    live_position(db, 1, 10.0, 18.0)
    T.sweep_trading_profit(db)
    assert T.owed_to_burn(db) == pytest.approx(10.0 * C.BURN_SHARE + 8.0)
    assert T.free_usd(db, 100.0) == pytest.approx(100.0 - T.owed_total(db))
