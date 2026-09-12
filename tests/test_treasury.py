"""The treasury cycle: claims settled from the escrow log, the owed balance, forwards, pending rows."""
import time

from eth_abi import decode

from fakes import auto_receipts, claimed_log, decode_tx, transfer_log, tx_hash, word
from wormhole import config as C, treasury as T
from wormhole.chain import selector

BAL_OF_TOKEN = selector("balanceOfToken(address,address)")
BAL_OF = selector("balanceOf(address)")
TRANSFER = bytes.fromhex(selector("transfer(address,uint256)")[2:])
OWNER = C.OWNER_WALLET


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
    assert abs(T.owed_to_owner(db) - 0.71) < 1e-9
    ledger(db, "forward_pending", 0.5)
    assert abs(T.owed_to_owner(db) - 0.21) < 1e-9
    ledger(db, "forward", 0.21)
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
    assert transfer_in(rpc.raw[0]) == (OWNER, 710_000)
    f = rows(db, "forward")
    assert len(f) == 1 and abs(f[0]["amount"] - 0.71) < 1e-9 and f[0]["tx"] == tx_hash(rpc.raw[0])
    assert rows(db, "forward_pending") == [] and abs(T.owed_to_owner(db)) < 1e-9
    ev = db.one("SELECT text FROM events WHERE kind='treasury' ORDER BY id DESC LIMIT 1")["text"]
    assert ev.startswith("forwarded 0.71 USDG (20%)")


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
    assert transfer_in(rpc.raw[1]) == (OWNER, 1_050_000)
    f = rows(db, "forward")
    assert len(f) == 1 and abs(f[0]["amount"] - 1.05) < 1e-9
    texts = [e["text"] for e in db.q("SELECT text FROM events WHERE kind='treasury' ORDER BY id")]
    assert texts == ["claimed 5.25 USDG of creator fees", "forwarded 1.05 USDG (20%) to the creator"]


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
    assert abs(T.owed_to_owner(db) - 0.71) < 1e-9            # still owed
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
    assert transfer_in(rpc.raw[0]) == (OWNER, 600_000)
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
    assert transfer_in(rpc.raw[0]) == (OWNER, 2_000_000)     # sent again in the same cycle
    assert len(rows(db, "forward")) == 1 and abs(T.owed_to_owner(db)) < 1e-9


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
    assert abs(T.owed_to_owner(db) - 0.75) < 1e-9


def test_below_the_minimum_nothing_is_forwarded(db, rpc, acct, live):
    ledger(db, "claim", 2.0)                                 # owed 0.40 < 0.50
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
    assert abs(s["owed_to_owner"] - 1.0) < 1e-9 and s["claimable_usdg"] == 1.5 and s["usdg"] == 9.0
    assert len(s["ledger"]) == 3
