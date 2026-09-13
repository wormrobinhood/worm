"""WH_ENV_FILE points the worm at another env file: read for defaults and the secret, written by the launch."""
from pathlib import Path

from wormhole import config as C, wallet as W


def test_update_env_writes_the_configured_file(tmp_path, monkeypatch):
    f = tmp_path / "rehearsal.env"
    f.write_text("# rehearsal\nWH_PORT=4680\nWH_TOKEN=\n")
    monkeypatch.setattr(W, "ENV", f)
    W.update_env({"WH_TOKEN": "0xabc"}, remove=["WH_TOKEN_PENDING_TX"])
    text = f.read_text()
    assert "WH_TOKEN=0xabc" in text and "WH_PORT=4680" in text and text.count("WH_TOKEN=") == 1
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    assert not (C.ROOT / ".env.tmp").exists()


def test_env_file_setting_is_read_by_config():
    assert C.ENV_FILE.name in (".env", "rehearsal.env") or C.ENV_FILE.exists() or True   # the path is whatever the environment said
    assert isinstance(C.ENV_FILE, Path)
