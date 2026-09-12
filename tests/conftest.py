"""Test setup: an isolated environment so no test can touch the real wallet, .env values or data/.

The environment wins over .env in wormhole.config, so these are set before anything imports it."""
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["WH_SECRET"] = "0x" + "11" * 32          # throwaway key, tests only
os.environ["WH_DATA_DIR"] = tempfile.mkdtemp(prefix="irl-worm-tests-")
os.environ["WH_DEMO_TREASURY"] = "0"
os.environ["WH_LIVE"] = "0"
os.environ["WH_OWNER_WALLET"] = "0x" + "22" * 20

import pytest  # noqa: E402


@pytest.fixture
def db(tmp_path):
    from wormhole.db import DB
    return DB(tmp_path / "test.db")
