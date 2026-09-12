import importlib.util
import pathlib
import string
import struct
import zlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("leakcheck", ROOT / "scripts" / "leakcheck.py")
lc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lc)
RULES = lc.Rules()

SWAP_TOPIC = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"


def kinds(path, data):
    return [f.split(":")[0] for f in RULES.scan(path, data)]


def png_with_text(text):
    """A 1x1 PNG with `text` in a tEXt chunk, built at run time (the sample never sits in this file as bytes)."""
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"tEXt", b"Comment\x00" + text.encode())
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + chunk(b"IEND", b""))


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


def test_catches_wh_prefixed_secret():
    # names with a prefix or suffix around the secret word; the values are assembled at run time
    val = "QmZ9x" + "abcdefghijklmnopqrstuvwxyz0123"
    assert "quoted secret assignment" in kinds("x.py", ("WH_SEC" + "RET = '" + val + "'").encode())
    assert "quoted secret assignment" in kinds("x.py", ("signer_" + "pk = \"" + val + "\"").encode())
    assert "quoted secret assignment" in kinds("cfg.yml", ("WALLET_" + "KEY: '" + val + "'").encode())
    assert "quoted secret assignment" in kinds("x.py", ("seed_" + "phrase = '" + val + "'").encode())
    assert "secret assignment" in kinds("notes.txt", ("PRIVATE_" + "KEY=" + val).encode())        # .env style, no quotes
    assert "secret assignment" in kinds("notes.txt", ("export API_" + "TOKEN = " + val).encode())


def test_catches_uppercase_0x():
    key = "0X" + "ab12" * 16
    found = kinds("x.py", f"k = {key}".encode())
    assert any("private key" in k for k in found)


def test_catches_hex_split_across_lines_or_underscores():
    half = "ab12" * 8
    assert any("split by" in k for k in kinds("notes.md", f"key {half}\n{half} end".encode()))
    grouped = "0x" + "_".join("ab12ab12" for _ in range(8))
    assert any("split by" in k for k in kinds("x.py", f"k = {grouped}".encode()))
    assert kinds("notes.md", f"{half}\n{half[:20]}".encode()) == []            # 52 hex: not a key
    assert kinds("notes.md", f"{half}\n\n{half}".encode()) == []               # a blank line between: two values
    assert kinds("x.py", f"topic = {SWAP_TOPIC[:34]}_{SWAP_TOPIC[34:]}".encode()) == []   # allow-listed once joined


def test_catches_base64_key():
    b64 = (string.ascii_uppercase + string.ascii_lowercase + string.digits)[18:61]
    assert len(b64) == 43
    assert "base64 secret assignment" in kinds("x.py", ("api_" + "key = " + b64 + "=").encode())
    assert "base64 secret assignment" in kinds("cfg.yml", ("access_" + "token: " + b64).encode())
    assert "base64 secret assignment" in kinds("x.py", ("WH_SEC" + "RET=\"" + b64 + "=\"").encode())
    assert "base64 secret assignment" not in kinds("x.py", ("digest = " + b64 + "=").encode())      # no secret in the name
    assert "base64 secret assignment" not in kinds("x.py", ("api_" + "key = " + "a" * 43).encode())  # one character class
    assert "base64 secret assignment" not in kinds("x.py", ("api_" + "key = " + b64 + "xyz").encode())  # not 43/44 long


def test_catches_key_in_png_metadata():
    pem = "-----BEGIN " + "PRIVATE KEY-----"
    png = png_with_text(pem)
    assert b"\0" in png[:8000]                                   # scanned as a binary
    assert any(f.startswith("PEM private key: run") and "binary" in f for f in RULES.scan("logo.png", png))
    key = "0x" + "ab12" * 16
    assert any("private key" in f and "binary" in f for f in RULES.scan("logo.png", png_with_text("signer " + key)))
    assert any("quoted secret assignment" in f for f in RULES.scan("logo.png", png_with_text("wallet_" + "pk='" + "Zx9" * 9 + "'")))
    assert RULES.scan("logo.png", png_with_text("made with love")) == []


def test_no_false_positive_on_prose_and_code():
    clean = [
        b"the token is stored in the secret file, never in git",
        b"token = row['token']",
        b"WH_TOKEN_NAME=IRL Worm   # the token name",
        ("token = '0x" + "ab" * 20 + "'").encode(),                  # a public address assigned to token
        b"WHERE token=? AND graduated=1",
        b"secrets.compare_digest(want.encode(), got.encode())",
        b"pkg==1.0 and package_key_word = 1",
        ("digest = sha256:" + "c3" * 32).encode(),
        b"# WH_RESCAN_TOKEN=        # lets a remote caller use /api/rescan",
        b'headers={"X-Rescan-Token": "t0k3n-for-tests-only"}',
    ]
    for sample in clean:
        assert kinds("x.py", sample) == [], sample
