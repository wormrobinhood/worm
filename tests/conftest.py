"""Test setup: an isolated environment so no test can touch the real wallet, .env values or data/.

The environment wins over .env in wormhole.config, so these are set before anything imports it."""
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

KEY = "0x" + "11" * 32                                # throwaway key, tests only
os.environ["WH_SECRET"] = KEY
os.environ["WH_DATA_DIR"] = tempfile.mkdtemp(prefix="irl-worm-tests-")
os.environ["WH_LIVE"] = "0"
os.environ["WH_OWNER_WALLET"] = "0x" + "22" * 20
# keys the real .env may carry that would otherwise reach the tests: pinned to harmless values
os.environ["WH_WALLET"] = ""
os.environ["WH_BASE_WALLET"] = ""
os.environ["WH_TOKEN"] = ""
os.environ["WH_TOKEN_PENDING_TX"] = ""
os.environ["WH_VOICE_MODEL"] = "stub"
os.environ["WH_TOPUP_ALWAYS"] = "0"

import pytest  # noqa: E402


@pytest.fixture
def db(tmp_path):
    from wormhole.db import DB
    return DB(tmp_path / "test.db")


@pytest.fixture
def acct():
    """The throwaway signer: its address is C.WALLET in the tests."""
    from eth_account import Account
    return Account.from_key(KEY)


@pytest.fixture
def rpc():
    from fakes import FakeRpc
    return FakeRpc()


@pytest.fixture
def live(monkeypatch):
    """Arm signing in this test process only, and make the waits instant."""
    from wormhole import config as C, tx
    monkeypatch.setattr(C, "LIVE", True)
    monkeypatch.setattr(tx, "RECEIPT_POLL_S", 0)
    monkeypatch.setattr(tx, "BROADCAST_RETRY_S", 0)
