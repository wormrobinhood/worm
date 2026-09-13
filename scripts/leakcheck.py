#!/usr/bin/env python3
"""leakcheck: keeps secrets and personal details out of git.

    python3 scripts/leakcheck.py --staged            pre-commit: the files about to be committed
    python3 scripts/leakcheck.py --message <file>    commit-msg: the commit message
    python3 scripts/leakcheck.py --push <remote> <url>   pre-push: every commit about to leave (ref lines on stdin)
    python3 scripts/leakcheck.py --all               tracked and untracked files in the working tree
    python3 scripts/leakcheck.py --rev <rev>         the whole tree at one commit
    python3 scripts/leakcheck.py --history <rev>     every reachable commit and its metadata

Exit status 1 blocks the commit or push. Matches are printed redacted; a secret
value never appears in full. Standard library only, runs on the system python3.

What it refuses
  * files that must never be tracked: .env and variants, databases, logs, key files,
    anything under data/ or .venv/, files over 5 MB
  * every value of the local .env (the real secrets on this machine), searched
    byte for byte in every file, binaries included
  * secret shapes: 64-hex private keys (0x or 0X, also when split by underscores or
    across a line break) unless allow-listed as a public hash, PEM private keys,
    GitHub / OpenAI / Anthropic / Slack / AWS tokens, long values assigned to a
    name that carries a secret (WH_SECRET, signer_pk, WALLET_KEY, api_key, token,
    password, mnemonic, seed phrase, with any prefix or suffix), 43/44-character
    base64 values assigned to such a name, 12-word seed phrases
  * the same shapes inside binary files, read from their printable runs (a key in
    PNG metadata is still a key)
  * personal details: every term in .leakcheck.local (git-ignored, one per line)
    in file contents, commit messages, author names, emails and the push URL,
    plus home-directory paths such as /Users/<name>/ or /home/<name>/
  * a placeholder author email (anything ending in .invalid) at push time
Public hashes that look like keys are listed in scripts/leakcheck_allow.txt.
"""
import os
import re
import subprocess
import sys
from typing import Callable, Dict, List, Optional, Set, Tuple


class ScanError(RuntimeError):
    """A failed read cannot be treated as a clean scan."""


def git(*args: str, inp: Optional[bytes] = None) -> bytes:
    r = subprocess.run(["git", *args], capture_output=True, input=inp, cwd=ROOT)
    if r.returncode:
        # Git errors can contain remote credentials or private file names.
        raise ScanError("git %s failed; scan incomplete (diagnostic output withheld)" % args[0])
    return r.stdout


ROOT = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip() or os.getcwd()
ALLOW_FILE = os.path.join(ROOT, "scripts", "leakcheck_allow.txt")
LOCAL_FILE = os.path.join(ROOT, ".leakcheck.local")
WORDS_FILE = os.path.join(ROOT, "scripts", "bip39_english.txt")
ENV_FILE = os.path.join(ROOT, ".env")
EXAMPLE_FILE = os.path.join(ROOT, ".env.example")
MAX_BYTES = 5 * 1024 * 1024

FORBIDDEN_DIRS = {"data", "rehearsal", ".venv", "venv", "__pycache__", "node_modules", ".claude", ".git"}
FORBIDDEN_SUFFIX = (".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".log", ".pem",
                    ".sqlite-wal", ".sqlite-shm", ".sqlite3-wal", ".sqlite3-shm",
                    ".key", ".p12", ".pfx", ".jks", ".keystore", ".pyc")
FORBIDDEN_NAMES = {".env", ".leakcheck.local", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"}

# A name that carries a secret, with any prefix or suffix: WH_SECRET, signer_pk, WALLET_KEY, PRIVATE_KEY,
# api_key2, SEED_PHRASE. A bare "key" does not count (dict keys, cache keys); "_key" ending a compound name does.
SECRET_NAME = (r"(?<![A-Za-z0-9])[A-Za-z0-9_.\-]*?(?:api[_-]?key|secret|private[_-]?key|passw(?:or)?d|mnemonic|"
               r"seed[_-]?phrase|token|(?<![A-Za-z])pk(?![A-Za-z])|(?<=[A-Za-z0-9])[_-]key(?![A-Za-z]))[A-Za-z0-9_.\-]*")
LONG_VALUE = r"[A-Za-z0-9+/=_.\-]{24,}"
NOT_ADDRESS = r"(?!0x[0-9a-fA-F]{40}(?![A-Za-z0-9]))"      # a 40-hex address assigned to "token" is public


def _mixed(value: str) -> bool:
    """A random 32-byte key in base64 has lower case, upper case and digits; a long identifier rarely does."""
    return (any(c.islower() for c in value) and any(c.isupper() for c in value) and any(c.isdigit() for c in value)
            and len(set(value)) >= 12)


TEXT_RULES: List[Tuple[str, "re.Pattern[str]", Optional[Callable[["re.Match[str]"], bool]]]] = [
    ("PEM private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), None),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), None),
    ("OpenAI/Anthropic-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), None),
    ("Slack token", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}"), None),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), None),
    ("home directory path", re.compile(r"(?:/Users/|/home/|C:\\\\Users\\\\)(?!Shared\b)[A-Za-z0-9._-]+"), None),
    ("quoted secret assignment", re.compile(
        r"(?i)" + SECRET_NAME + r"\s*[:=]\s*[\"']" + NOT_ADDRESS + LONG_VALUE + r"[\"']"), None),
    ("secret assignment", re.compile(      # a whole KEY=value line, .env style, no quotes needed
        r"(?im)^[ \t]*(?:export[ \t]+)?" + SECRET_NAME + r"[ \t]*=[ \t]*" + NOT_ADDRESS + LONG_VALUE + r"[ \t]*$"), None),
    ("base64 secret assignment", re.compile(
        r"(?i)" + SECRET_NAME + r"\s*[:=]\s*[\"']?(?P<v>[A-Za-z0-9+/_\-]{43}=?)(?![A-Za-z0-9+/=_\-])"),
     lambda m: _mixed(m.group("v"))),
]
HEX64 = re.compile(r"(?<![0-9a-zA-Z])(?:0[xX])?([0-9a-fA-F]{64})(?![0-9a-zA-Z])")
# hex groups joined by single underscores (0xdead_beef_…) or by one line break with nothing else between them
HEX_SPLIT = re.compile(r"(?<![0-9a-zA-Z])(?:0[xX])?[0-9a-fA-F]{2,}(?:(?:_|\r?\n)[0-9a-fA-F]{2,})+(?![0-9a-zA-Z_])")
SHA_PREFIX = re.compile(r"sha(?:256|512)[:=]\s*$")
PRINTABLE_RUN = re.compile(rb"[\x20-\x7e\t]{20,}")
WORD = re.compile(r"[a-z]+")
SEED_RUN = 12


def read_lines(path: str) -> List[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    except FileNotFoundError:
        return []


def env_values(path: str) -> Dict[str, str]:
    out = {}
    for ln in read_lines(path):
        if "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        v = v.split(" #", 1)[0].strip().strip("\"'")
        if v:
            out[k.strip()] = v
    return out


class Rules:
    def __init__(self) -> None:
        self.allow: Set[str] = {ln.lower().replace("0x", "") for ln in read_lines(ALLOW_FILE) if re.fullmatch(r"(0x)?[0-9a-fA-F]{64}", ln)}
        self.terms: List[str] = [t.lower() for t in read_lines(LOCAL_FILE)]
        self.words: Set[str] = set(read_lines(WORDS_FILE))
        public = set(env_values(EXAMPLE_FILE).values())
        self.secrets: Dict[str, bytes] = {}
        for k, v in env_values(ENV_FILE).items():
            if len(v) >= 12 and v not in public:
                v = v.lower()
                self.secrets[k] = (v[2:] if v.startswith("0x") else v).encode()

    def scan(self, path: str, data: bytes) -> List[str]:
        """Return a list of redacted findings for one file."""
        out: List[str] = []
        base = os.path.basename(path)
        parts = path.split("/")
        if any(p in FORBIDDEN_DIRS for p in parts[:-1]):
            out.append("path under a folder that must never be tracked")
        if base in FORBIDDEN_NAMES or (base.startswith(".env") and base != ".env.example") or base.endswith(FORBIDDEN_SUFFIX):
            out.append("file name that must never be tracked")
        if len(data) > MAX_BYTES:
            out.append("file over 5 MB (database or dump?)")
        low = data.lower()
        for k, v in self.secrets.items():
            if v in low:
                out.append("value of %s from .env" % k)
        for t in self.terms:
            if t.encode() in low:
                out.append("personal term '%s\u2026'" % t[:2])
        if b"\0" in data[:8000]:
            # binary: the exact searches above, then the text rules over its printable runs
            runs = [m.group().decode("ascii") for m in PRINTABLE_RUN.finditer(data)]
            out.extend(self._scan_text("\n".join(runs), path, "run %d in binary"))
            return out
        out.extend(self._scan_text(data.decode("utf-8", errors="ignore"), path, "line %d"))
        return out

    def _hex_public(self, hexval: str, text: str, start: int) -> bool:
        """An allow-listed public hash, a sha256/sha512 pin, or a 0x000…/0xfff… style constant."""
        return (hexval.lower() in self.allow or bool(SHA_PREFIX.search(text[max(0, start - 12):start]))
                or len(set(hexval.lower())) <= 2)

    def _scan_text(self, text: str, path: str, where: str) -> List[str]:
        out: List[str] = []

        def loc(pos: int) -> str:
            return where % (text.count("\n", 0, pos) + 1)

        for name, rx, check in TEXT_RULES:
            for m in rx.finditer(text):
                if check and not check(m):
                    continue
                out.append("%s: %s, %s\u2026 (%d chars)" % (name, loc(m.start()), m.group(0)[:6], len(m.group(0))))
        for m in HEX64.finditer(text):
            if self._hex_public(m.group(1), text, m.start()):
                continue
            out.append("64-hex value that may be a private key: %s, %s\u2026 (add to scripts/leakcheck_allow.txt if it is a public hash)"
                       % (loc(m.start()), m.group(0)[:8]))
        for m in HEX_SPLIT.finditer(text):
            joined = re.sub(r"[_\r\n]", "", m.group(0))
            joined = joined[2:] if joined[:2].lower() == "0x" else joined
            if len(joined) != 64 or self._hex_public(joined, text, m.start()):
                continue
            out.append("64-hex value split by underscores or a line break that may be a private key: %s, %s\u2026"
                       " (add the joined value to scripts/leakcheck_allow.txt if it is a public hash)" % (loc(m.start()), m.group(0)[:8]))
        if self.words and not path.endswith("bip39_english.txt"):
            run = 0
            for m in WORD.finditer(text.lower()):
                run = run + 1 if m.group() in self.words else 0
                if run >= SEED_RUN:
                    out.append("possible seed phrase near %s" % loc(m.start()))
                    break
        return out

    def scan_identity(self, what: str, value: str, leaving: bool = True) -> List[str]:
        low = value.lower()
        out = [("%s contains personal term '%s\u2026'" % (what, t[:2])) for t in self.terms if t in low]
        if leaving and what.endswith("email") and low.strip().endswith(".invalid"):
            out.append("%s is still the placeholder; set the GitHub no-reply address first" % what)
        return out


def report(findings: Dict[str, List[str]], scanned: int, what: str) -> int:
    if not findings:
        print("leakcheck: %s clean (%d items)" % (what, scanned))
        return 0
    print("leakcheck: BLOCKED %s\n" % what)
    for where, items in findings.items():
        for it in items:
            print("  %s\n      %s" % (where, it))
    print("\nFix the items above (or allow-list a public hash), then retry.")
    return 1


def blobs_at(rev: str) -> List[Tuple[str, str]]:
    files = []
    for entry in git("ls-tree", "-r", "-z", rev).split(b"\0"):
        if not entry:
            continue
        meta, path = entry.split(b"\t", 1)
        mode, typ, sha = meta.split()
        if mode == b"120000" or typ != b"blob":
            raise ScanError("symlinks and submodules need separate review; scan blocked")
        if typ == b"blob":
            files.append((path.decode("utf-8", "replace"), sha.decode()))
    return files


def scan_main(argv: List[str]) -> int:
    git("rev-parse", "--show-toplevel")
    rules = Rules()
    findings: Dict[str, List[str]] = {}
    scanned = 0

    def add(where: str, items: List[str]) -> None:
        if items:
            findings.setdefault(where, []).extend(items)

    mode = argv[1] if len(argv) > 1 else "--all"
    counts = {"--all": (1, 2), "--staged": (2,), "--message": (3,), "--rev": (3,),
              "--history": (3,), "--push": (4,)}
    if mode not in counts or len(argv) not in counts[mode]:
        raise ScanError("invalid scan arguments")

    if mode == "--staged":
        for w, key in (("git user.name", "user.name"), ("git user.email", "user.email")):
            add(w, rules.scan_identity(w, git("config", key).decode().strip(), leaving=False))
        for n in git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split(b"\0"):
            if n:
                path = n.decode("utf-8", "replace")
                add(path, rules.scan(path, git("show", ":" + path)))
                scanned += 1
        return report(findings, scanned, "staged files")

    if mode == "--message":
        with open(argv[2], "rb") as f:
            msg = f.read()
        add("commit message", rules.scan("COMMIT_MSG", msg))
        return report(findings, 1, "commit message")

    if mode in ("--push", "--history"):
        remote = argv[2] if mode == "--push" else "history"
        commits: List[str] = []
        if mode == "--history":
            rev = git("rev-parse", "--verify", argv[2] + "^{commit}").decode().strip()
            commits = git("rev-list", rev).decode().split()
        else:
            add("push url", rules.scan_identity("push url", argv[3]))
            for line in sys.stdin.read().splitlines():
                p = line.split()
                if len(p) != 4 or not all(re.fullmatch(r"[0-9a-f]{40,64}", p[i]) for i in (1, 3)):
                    raise ScanError("invalid pre-push input")
                if set(p[1]) == {"0"}:
                    continue   # deletion carries no new objects
                git("rev-parse", "--verify", p[1] + "^{commit}")
                # A new remote branch may expose any ancestor, including ones present on other remotes.
                rng = [p[1]] if set(p[3]) == {"0"} else ["%s..%s" % (p[3], p[1])]
                commits += git("rev-list", *rng).decode().split()
        seen: Dict[Tuple[str, str], List[str]] = {}
        for c in dict.fromkeys(commits):
            an, ae, cn, ce = git("log", "-1", "--format=%an%n%ae%n%cn%n%ce", c).decode().split("\n")[:4]
            for w, v in (("author name", an), ("author email", ae), ("committer name", cn), ("committer email", ce)):
                add("commit %s %s" % (c[:10], w), rules.scan_identity(w, v))
            add("commit %s message" % c[:10], rules.scan("COMMIT_MSG", git("log", "-1", "--format=%B", c)))
            for path, sha in blobs_at(c):
                key = (path, sha)   # file-name rules must run even when identical bytes were already scanned
                if key not in seen:
                    seen[key] = rules.scan(path, git("cat-file", "blob", sha))
                    scanned += 1
                add("commit %s %s" % (c[:10], path), seen[key])
        return report(findings, scanned, "%d commit(s) to %s" % (len(commits), remote or "remote"))

    if mode == "--rev":
        rev = git("rev-parse", "--verify", argv[2] + "^{commit}").decode().strip()
        for path, sha in blobs_at(rev):
            add(path, rules.scan(path, git("cat-file", "blob", sha)))
            scanned += 1
        return report(findings, scanned, "tree at %s" % argv[2])

    # --all: tracked plus untracked-but-not-ignored working files
    names = git("ls-files", "-z").split(b"\0") + git("ls-files", "-z", "-o", "--exclude-standard").split(b"\0")
    for n in sorted({x for x in names if x}):
        path = n.decode("utf-8", "replace")
        full = os.path.join(ROOT, path)
        if os.path.islink(full):
            raise ScanError("symlinks need separate review; scan blocked")
        if os.path.isfile(full):
            with open(full, "rb") as f:
                add(path, rules.scan(path, f.read()))
            scanned += 1
    return report(findings, scanned, "working tree")


def main(argv: List[str]) -> int:
    try:
        return scan_main(argv)
    except (ScanError, OSError, ValueError) as exc:
        detail = str(exc) if isinstance(exc, ScanError) else "unable to read scan inputs"
        print("leakcheck: BLOCKED: " + detail, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
