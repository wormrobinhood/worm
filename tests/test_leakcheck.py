import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("leakcheck", ROOT / "scripts" / "leakcheck.py")
lc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lc)
RULES = lc.Rules()

SWAP_TOPIC = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"


def kinds(path, data):
    return [f.split(":")[0] for f in RULES.scan(path, data)]


def test_private_key_shapes_are_blocked():
    key = "0x" + "ab12" * 16
    assert any("private key" in k for k in kinds("x.py", f"k = '{key}'".encode()))
    assert any("private key" in k for k in kinds("x.py", key[2:].upper().encode()))


def test_allow_listed_public_hash_passes():
    assert kinds("x.py", f"topic = {SWAP_TOPIC}".encode()) == []


def test_sha256_pins_pass():
    assert kinds("requirements.txt", ("pkg==1.0 --hash=sha256:" + "c3" * 32).encode()) == []


def test_tokens_paths_and_seeds_are_blocked():
    # the samples are assembled at run time so this file itself passes the guard
    assert kinds("x.py", ("ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456").encode()) == ["GitHub token"]
    assert kinds("x.py", ("sk-" + "ant-api03-abcdefghijklmnopqrstuvwxyz0123").encode()) == ["OpenAI/Anthropic-style key"]
    assert kinds("x.py", ("see /Users" + "/somebody/projects").encode()) == ["home directory path"]
    assert kinds("x.py", ("-----BEGIN " + "PRIVATE KEY-----").encode()) == ["PEM private key"]
    words = " ".join(["abandon", "ability", "able", "about"] * 3).encode()
    assert kinds("notes.md", words) == ["possible seed phrase near line 1"]


def test_forbidden_files_are_blocked_by_name():
    assert "file name that must never be tracked" in RULES.scan(".env", b"") 
    assert "path under a folder that must never be tracked" in RULES.scan("data/wormhole.db", b"")
    assert any("5 MB" in f for f in RULES.scan("big.bin", b"\0" * (5 * 1024 * 1024 + 1)))


def test_placeholder_email_only_blocks_when_leaving():
    assert RULES.scan_identity("author email", "worm@irl-wormhole.invalid", leaving=True)
    assert RULES.scan_identity("git user.email", "worm@irl-wormhole.invalid", leaving=False) == []
