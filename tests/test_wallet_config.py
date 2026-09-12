"""The key and .env: parsing rules, the secret kept out of the environment, address checks, wallet new."""
import os

import pytest
from eth_account import Account
from eth_utils import to_checksum_address

from conftest import KEY
from wormhole import config as C, wallet as W

A = "0x" + "aa" * 20
B = "0x" + "bb" * 20


def test_secret_is_not_in_the_environment_after_import():
    assert "WH_SECRET" not in os.environ
    assert C.SECRET == KEY and C.WALLET == Account.from_key(KEY).address.lower()


def test_parse_dotenv_rules():
    text = ('WH_BACKFILL_HOURS=72      # launches kept for creator history\nWH_TOKEN=\nWH_TOKEN=0xabc\n'
            'Q="quoted"\nS=\'single\'\n# WH_X=1\nno equals here\nWH_SECRET=\nWH_SECRET=0xkey\n  SPACED = padded  \n')
    v = C.parse_dotenv(text)
    assert v == {"WH_BACKFILL_HOURS": "72", "WH_TOKEN": "0xabc", "Q": "quoted", "S": "single", "WH_SECRET": "0xkey",
                 "SPACED": "padded"}
    assert float(v["WH_BACKFILL_HOURS"]) == 72.0


def test_load_dotenv_keeps_the_secret_out_of_the_environment(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("WH_SECRET=0xfilekey\nWH_TEST_A=1   # comment\nWH_TEST_B=file\n")
    monkeypatch.delenv("WH_TEST_A", raising=False)
    monkeypatch.setenv("WH_TEST_B", "env")
    assert C._load_dotenv(p) == "0xfilekey"
    assert os.environ["WH_TEST_A"] == "1" and os.environ["WH_TEST_B"] == "env"   # the environment wins
    assert "WH_SECRET" not in os.environ
    monkeypatch.delenv("WH_TEST_A")
    assert C._load_dotenv(tmp_path / "missing") == ""


def test_valid_address():
    assert C.valid_address(A) and C.valid_address(A.upper().replace("0X", "0x")) and C.valid_address(to_checksum_address(C.FACTORY))
    assert not C.valid_address("0x7ed598bcef8bd9edd8c97a195c6d13f40801EC7e")     # mixed case, wrong checksum
    assert not C.valid_address("0x123") and not C.valid_address("") and not C.valid_address(None)


def test_check_addresses():
    C.check_addresses("", "", "")
    C.check_addresses(A, A, B)
    C.check_addresses(to_checksum_address(A), A, "")
    C.check_addresses(A, "", "")                                          # no key: read-only wallet, any address
    with pytest.raises(SystemExit, match="WH_WALLET is not the address of WH_SECRET"):
        C.check_addresses(A, B, "")
    with pytest.raises(SystemExit, match="WH_WALLET is not an address"):
        C.check_addresses("nope", "", "")
    with pytest.raises(SystemExit, match="WH_OWNER_WALLET"):
        C.check_addresses("", "", "0x123")
    with pytest.raises(SystemExit, match="WH_OWNER_WALLET"):
        C.check_addresses("", "", "0x7ed598bcef8bd9edd8c97a195c6d13f40801EC7e")


# ---- wallet new --------------------------------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr(W, "ENV", path)
    monkeypatch.setattr(C, "SECRET", "")
    return path


def test_wallet_new_creates_env_0600_and_prints_only_the_address(env, capsys):
    W.new()
    assert oct(env.stat().st_mode & 0o777) == "0o600"
    key = C.parse_dotenv(env.read_text())["WH_SECRET"]
    assert key.startswith("0x") and len(key) == 66
    out = capsys.readouterr().out
    assert Account.from_key(key).address in out and key not in out and key[2:] not in out
    assert not env.with_name(".env.tmp").exists()
    assert env.read_text().startswith("# IRL Worm secrets")


def test_wallet_new_refuses_an_existing_key(env):
    before = "WH_RPC=x\nWH_SECRET=0x" + "ab" * 32 + "\n"
    env.write_text(before)
    with pytest.raises(SystemExit, match="already in .env"):
        W.new()
    assert env.read_text() == before


def test_wallet_new_refuses_when_config_has_a_secret(env, monkeypatch, capsys):
    monkeypatch.setattr(C, "SECRET", KEY)
    W.new()
    assert "refusing" in capsys.readouterr().out and not env.exists()


def test_wallet_new_replaces_a_blank_line(env):
    env.write_text("# mine\nWH_SECRET=\nWH_RPC=x\n")
    W.new()
    lines = env.read_text().splitlines()
    assert lines[:2] == ["# mine", "WH_RPC=x"] and lines[2].startswith("WH_SECRET=0x") and len(lines) == 3
    assert oct(env.stat().st_mode & 0o777) == "0o600"


def test_update_env_sets_removes_and_keeps_the_rest(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# head\nA=1\nB=2\nB=3\nC=4\n")
    os.chmod(p, 0o644)
    W.update_env({"B": "9", "D": "5"}, remove=["C"], path=p)
    assert p.read_text() == "# head\nA=1\nB=9\nD=5\n" and oct(p.stat().st_mode & 0o777) == "0o600"
    W.update_env(remove=["D", "missing"], path=p)
    assert p.read_text() == "# head\nA=1\nB=9\n"
    q = tmp_path / "new.env"
    W.update_env({"X": "1"}, path=q)
    assert q.read_text() == "# IRL Worm secrets. Never commit this file.\nX=1\n"
