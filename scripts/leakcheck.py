#!/usr/bin/env python3
"""leakcheck: keeps secrets and personal details out of git.

    python3 scripts/leakcheck.py --staged            pre-commit: the files about to be committed
    python3 scripts/leakcheck.py --message <file>    commit-msg: the commit message
    python3 scripts/leakcheck.py --push <remote> <url>   pre-push: every commit about to leave (ref lines on stdin)
    python3 scripts/leakcheck.py --all               tracked and untracked files in the working tree
    python3 scripts/leakcheck.py --rev <rev>         the whole tree at one commit

Exit status 1 blocks the commit or push. Matches are printed redacted; a secret
value never appears in full. Standard library only, runs on the system python3.

What it refuses
  * files that must never be tracked: .env and variants, databases, logs, key files,
    anything under data/ or .venv/, files over 5 MB
  * every value of the local .env (the real secrets on this machine), searched
    byte for byte in every file, binaries included
  * secret shapes: 64-hex private keys unless allow-listed as a public hash,
    PEM private keys, GitHub / OpenAI / Anthropic / Slack / AWS tokens,
    quoted long values assigned to key/secret/password names, 12-word seed phrases
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
from typing import Dict, List, Optional, Set, Tuple


def git(*args: str, inp: Optional[bytes] = None) -> bytes:
    r = subprocess.run(["git", *args], capture_output=True, input=inp, cwd=ROOT)
    return r.stdout


ROOT = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip() or os.getcwd()
ALLOW_FILE = os.path.join(ROOT, "scripts", "leakcheck_allow.txt")
LOCAL_FILE = os.path.join(ROOT, ".leakcheck.local")
WORDS_FILE = os.path.join(ROOT, "scripts", "bip39_english.txt")
ENV_FILE = os.path.join(ROOT, ".env")
EXAMPLE_FILE = os.path.join(ROOT, ".env.example")
MAX_BYTES = 5 * 1024 * 1024

FORBIDDEN_DIRS = {"data", ".venv", "venv", "__pycache__", "node_modules", ".claude", ".git"}
FORBIDDEN_SUFFIX = (".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".log", ".pem",
                    ".key", ".p12", ".pfx", ".jks", ".keystore", ".pyc")
FORBIDDEN_NAMES = {".env", ".leakcheck.local", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"}

TEXT_RULES = [
    ("PEM private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("OpenAI/Anthropic-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("Slack token", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("home directory path", re.compile(r"(?:/Users/|/home/|C:\\\\Users\\\\)(?!Shared\b)[A-Za-z0-9._-]+")),
    ("quoted secret assignment", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|private[_-]?key|passw(?:or)?d|mnemonic|seed[_-]?phrase)\b"
        r"\s*[:=]\s*[\"'](?!0x[0-9a-fA-F]{40}[\"'])[A-Za-z0-9+/=_.\-]{24,}[\"']")),
]
HEX64 = re.compile(r"(?<![0-9a-zA-Z])(?:0x)?([0-9a-fA-F]{64})(?![0-9a-zA-Z])")
SHA_PREFIX = re.compile(r"sha(?:256|512)[:=]\s*$")
WORD = re.compile(r"[a-z]+")
SEED_RUN = 12


def read_lines(path: str) -> List[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    except OSError:
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
            return out  # binary: only the exact searches above apply
        text = data.decode("utf-8", errors="ignore")
        for name, rx in TEXT_RULES:
            for m in rx.finditer(text):
                out.append("%s: line %d, %s\u2026 (%d chars)" % (name, text.count("\n", 0, m.start()) + 1, m.group(0)[:6], len(m.group(0))))
        for m in HEX64.finditer(text):
            if m.group(1).lower() in self.allow or SHA_PREFIX.search(text[max(0, m.start() - 12):m.start()]):
                continue
            if len(set(m.group(1).lower())) <= 2:
                continue  # 0x000…/0xfff… style constants
            out.append("64-hex value that may be a private key: line %d, %s\u2026 (add to scripts/leakcheck_allow.txt if it is a public hash)"
                       % (text.count("\n", 0, m.start()) + 1, m.group(0)[:8]))
        if self.words and not path.endswith("bip39_english.txt"):
            run = 0
            for m in WORD.finditer(text.lower()):
                run = run + 1 if m.group() in self.words else 0
                if run >= SEED_RUN:
                    out.append("possible seed phrase near line %d" % (text.count("\n", 0, m.start()) + 1))
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
        if typ == b"blob":
            files.append((path.decode("utf-8", "replace"), sha.decode()))
    return files


def main(argv: List[str]) -> int:
    rules = Rules()
    findings: Dict[str, List[str]] = {}
    scanned = 0

    def add(where: str, items: List[str]) -> None:
        if items:
            findings.setdefault(where, []).extend(items)

    mode = argv[1] if len(argv) > 1 else "--all"

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

    if mode == "--push":
        remote = argv[2] if len(argv) > 2 else ""
        url = argv[3] if len(argv) > 3 else ""
        add("push url", rules.scan_identity("push url", url))
        commits: List[str] = []
        for line in sys.stdin.read().splitlines():
            p = line.split()
            if len(p) != 4 or set(p[1]) == {"0"}:
                continue
            rng = [p[1], "--not", "--remotes"] if set(p[3]) == {"0"} else ["%s..%s" % (p[3], p[1])]
            commits += git("rev-list", *rng).decode().split()
        seen: Dict[str, List[str]] = {}
        for c in commits:
            an, ae, cn, ce = git("log", "-1", "--format=%an%n%ae%n%cn%n%ce", c).decode().split("\n")[:4]
            for w, v in (("author name", an), ("author email", ae), ("committer name", cn), ("committer email", ce)):
                add("commit %s %s" % (c[:10], w), rules.scan_identity(w, v))
            add("commit %s message" % c[:10], rules.scan("COMMIT_MSG", git("log", "-1", "--format=%B", c)))
            for path, sha in blobs_at(c):
                if sha not in seen:
                    seen[sha] = rules.scan(path, git("cat-file", "blob", sha))
                    scanned += 1
                add("commit %s %s" % (c[:10], path), seen[sha])
        return report(findings, scanned, "%d commit(s) to %s" % (len(commits), remote or "remote"))

    if mode == "--rev":
        for path, sha in blobs_at(argv[2]):
            add(path, rules.scan(path, git("cat-file", "blob", sha)))
            scanned += 1
        return report(findings, scanned, "tree at %s" % argv[2])

    # --all: tracked plus untracked-but-not-ignored working files
    names = git("ls-files", "-z").split(b"\0") + git("ls-files", "-z", "-o", "--exclude-standard").split(b"\0")
    for n in sorted({x for x in names if x}):
        path = n.decode("utf-8", "replace")
        full = os.path.join(ROOT, path)
        if os.path.isfile(full):
            with open(full, "rb") as f:
                add(path, rules.scan(path, f.read()))
            scanned += 1
    return report(findings, scanned, "working tree")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
