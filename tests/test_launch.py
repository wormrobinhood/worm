"""The $WORM launch: exact calldata, the economics pin, byte limits, and the one-launch-only flow."""
import pytest
from eth_abi import decode, encode
from eth_utils import keccak

from fakes import decode_tx, topic_addr, tx_hash, word
from wormhole import config as C, launch as L, tx, wallet as W
from wormhole.chain import selector
from wormhole.pons import TOKEN_LAUNCHED

PREVIEW = selector("previewLaunchEconomics(uint256,address)")
FEE_SEL = selector("launchFee()")
LAUNCH_SEL = selector(L.LAUNCH_SIG)
ECON = "0x" + "e1" * 32
FEE = 500_000_000_000_000
TOKEN = "0x" + "70" * 20
CURVE = "0x" + "c0" * 20
GOLDEN_CALLDATA_HASH = "bb9fa58bac0f443d719a2100a3c9627f476184235ad5e3ab7a9daa8c4ea23267"   # keccak of the 1093 calldata bytes


def fixed_params(wallet, **over):
    p = {"name": "Worm", "symbol": "WORM", "logo": "https://example.invalid/logo.png", "description": "a small hooded worm",
         "socials": ("https://x.com/w", "https://t.me/w", "", "https://example.invalid", ""), "creatorFeeRecipient": wallet,
         "creatorTaxBps": 100, "buybackEnabled": False}
    p.update(over)
    return p


def test_calldata_golden_and_round_trip(acct, rpc):
    rpc.eth_calls[PREVIEW] = ECON
    econ = L.economics_pin(rpc)
    assert econ == bytes.fromhex(ECON[2:])
    p = fixed_params(acct.address)
    salt = b"\x5a" * 32
    data = L.calldata(p, econ, salt)
    assert data[:10] == "0xf35abbcf"
    tup, amount, pair = decode([L.TOKEN_PARAMS_T, "uint256", "address"], bytes.fromhex(data[10:]))
    assert pair.lower() == C.USDG and amount == 0
    assert tup[0] == "Worm" and tup[1] == "WORM" and tup[2] == p["logo"] and tup[3] == p["description"]
    assert tuple(tup[4]) == p["socials"]
    assert tup[5].lower() == acct.address.lower() and tup[6] == 100 and tup[7] is False
    assert tup[8] == econ and tup[9] == salt
    assert keccak(hexstr=data).hex() == GOLDEN_CALLDATA_HASH


def test_encode_call_pins_the_preview_and_salts(acct, rpc):
    rpc.eth_calls[PREVIEW] = ECON
    data, econ, salt = L.encode_call(rpc, fixed_params(acct.address), acct.address)
    assert econ == bytes.fromhex(ECON[2:]) and len(salt) == 32 and data[:10] == "0xf35abbcf"
    assert decode([L.TOKEN_PARAMS_T, "uint256", "address"], bytes.fromhex(data[10:]))[0][9] == salt


def test_preview_failure_refuses_unless_unpinned(acct, rpc, capsys):
    with pytest.raises(SystemExit, match="unpinned"):
        L.encode_call(rpc, fixed_params(acct.address), acct.address)
    _, econ, _ = L.encode_call(rpc, fixed_params(acct.address), acct.address, unpinned=True)
    assert econ == b"\x00" * 32
    rpc.eth_calls[PREVIEW] = "0x"                                   # empty answer is a failure too
    with pytest.raises(SystemExit, match="unpinned"):
        L.encode_call(rpc, fixed_params(acct.address), acct.address)


def test_params_limits_count_bytes_not_characters(monkeypatch, acct):
    monkeypatch.setenv("WH_TOKEN_NAME", "é" * 33)                     # 33 characters, 66 bytes
    with pytest.raises(ValueError, match="name"):
        L.params(acct.address)
    monkeypatch.setenv("WH_TOKEN_NAME", "é" * 32)                     # 64 bytes: fine
    assert L.params(acct.address)["name"] == "é" * 32
    monkeypatch.setenv("WH_TOKEN_SYMBOL", "W" * 17)
    with pytest.raises(ValueError, match="symbol"):
        L.params(acct.address)
    monkeypatch.setenv("WH_TOKEN_SYMBOL", "WORM")
    monkeypatch.setenv("WH_TOKEN_X", "x" * 257)
    with pytest.raises(ValueError, match="social"):
        L.params(acct.address)
    monkeypatch.setenv("WH_TOKEN_X", "x" * 256)
    p = L.params(acct.address)
    assert p["creatorFeeRecipient"] == acct.address and p["creatorTaxBps"] == 200 and p["socials"][3] == C.SITE_URL


# ---- main -----------------------------------------------------------------------------

def launched_log(token, curve, deployer):
    return {"address": C.FACTORY, "topics": [TOKEN_LAUNCHED.topic, topic_addr(token), topic_addr(curve), topic_addr(deployer)],
            "data": "0x" + encode(["address", "uint256", "uint256"], [C.USDG, 1, 10 ** 18]).hex(),
            "blockNumber": "0x10", "transactionHash": "0x" + "00" * 32, "logIndex": "0x0"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr(W, "ENV", path)
    return path


@pytest.fixture
def ready(rpc, monkeypatch):
    rpc.eth_calls[PREVIEW] = ECON
    rpc.eth_calls[FEE_SEL] = word(FEE)
    rpc.eth_calls[LAUNCH_SEL] = "0x" + encode(["address", "address"], [TOKEN, CURVE]).hex()   # the dry-run eth_call
    monkeypatch.setattr(L, "Rpc", lambda url: rpc)
    monkeypatch.setenv("WH_TOKEN_PENDING_TX", "")
    monkeypatch.setattr(C, "TOKEN", "")
    return rpc


def test_dry_run_sends_nothing(ready, env, capsys):
    L.main([])
    assert ready.raw == [] and not env.exists()
    out = capsys.readouterr().out
    assert "nothing sent" in out and f"OK: token {TOKEN}" in out
    assert "eth_sendRawTransaction" not in [m for m, _ in ready.calls]


def test_live_launch_pins_pending_then_writes_the_token(ready, env, live, acct, monkeypatch):
    ready.logs = [launched_log(TOKEN, CURVE, acct.address)]
    states = []
    real = W.update_env

    def spy(values=None, remove=(), path=None):
        real(values, remove, path)
        states.append(C.parse_dotenv(env.read_text()))

    monkeypatch.setattr(L, "update_env", spy)
    L.main(["--live"])
    t = decode_tx(ready.raw[0])
    assert t["to"] == C.FACTORY and t["value"] == FEE and t["gas"] == L.GAS_FLOOR       # the floor beats a small estimate
    assert t["data"][:4] == bytes.fromhex(LAUNCH_SEL[2:])
    assert states[0] == {"WH_TOKEN_PENDING_TX": tx_hash(ready.raw[0])}
    assert states[-1] == {"WH_TOKEN": TOKEN}
    assert oct(env.stat().st_mode & 0o777) == "0o600"


def test_live_launch_uses_the_estimate_when_it_is_higher(ready, env, live, acct):
    ready.logs = [launched_log(TOKEN, CURVE, acct.address)]
    ready.estimate = 5_000_000
    L.main(["--live"])
    assert decode_tx(ready.raw[0])["gas"] == 6_500_000


def test_pending_line_blocks_a_second_live_launch(ready, env, live, monkeypatch, capsys):
    monkeypatch.setenv("WH_TOKEN_PENDING_TX", "0x" + "ab" * 32)
    with pytest.raises(SystemExit, match="already in flight"):
        L.main(["--live"])
    assert ready.calls == []
    L.main([])                                                         # a dry run only says so
    assert "already in flight" in capsys.readouterr().out and ready.raw == []


def test_existing_token_blocks_a_second_live_launch(ready, env, live, monkeypatch):
    monkeypatch.setattr(C, "TOKEN", TOKEN)
    with pytest.raises(SystemExit, match="already set"):
        L.main(["--live"])
    assert ready.calls == []


def test_reverted_launch_clears_the_pending_line(ready, env, live):
    ready.status = "0x0"
    with pytest.raises(SystemExit, match="reverted"):
        L.main(["--live"])
    v = C.parse_dotenv(env.read_text())
    assert "WH_TOKEN_PENDING_TX" not in v and "WH_TOKEN" not in v


def test_lost_receipt_keeps_the_pending_line(ready, env, live, monkeypatch):
    ready.receipt_for = lambda h: None
    monkeypatch.setattr(tx, "RECEIPT_WAIT_S", 2)
    with pytest.raises(SystemExit, match="pending line stays"):
        L.main(["--live"])
    assert C.parse_dotenv(env.read_text()) == {"WH_TOKEN_PENDING_TX": tx_hash(ready.raw[0])}


def test_mined_without_the_event_keeps_the_pending_line(ready, env, live):
    with pytest.raises(SystemExit, match="no TokenLaunched"):
        L.main(["--live"])
    assert C.parse_dotenv(env.read_text()) == {"WH_TOKEN_PENDING_TX": tx_hash(ready.raw[0])}


def test_failed_dry_run_refuses_to_send(ready, env, live):
    del ready.eth_calls[LAUNCH_SEL]
    with pytest.raises(SystemExit, match="dry run failed"):
        L.main(["--live"])
    assert ready.raw == [] and not env.exists()


def test_launched_token_reads_the_factory_log(acct):
    rc = {"logs": [launched_log(TOKEN, CURVE, acct.address)]}
    assert L.launched_token(rc) == TOKEN
    assert L.launched_token({"logs": [{**launched_log(TOKEN, CURVE, acct.address), "address": C.USDG}]}) is None
