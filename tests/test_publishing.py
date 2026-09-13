"""Publication checks use disposable repositories and harmless data; nothing is pushed."""
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import leakcheck as lc

spec = importlib.util.spec_from_file_location("publish_check", ROOT / "scripts" / "publish-check.py")
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)


@pytest.fixture
def repository(tmp_path, monkeypatch):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="Worm", GIT_COMMITTER_NAME="Worm",
               GIT_AUTHOR_EMAIL="wormrobinhood@users.noreply.github.com",
               GIT_COMMITTER_EMAIL="wormrobinhood@users.noreply.github.com")
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, env=env, stderr=subprocess.DEVNULL).decode().strip()
    git("init", "-b", "main")
    (tmp_path / "README.md").write_text("Harmless fixture\n")
    git("add", ".")
    git("commit", "-m", "Fixture base")
    monkeypatch.setattr(lc, "ROOT", str(tmp_path))
    return tmp_path, git


def test_missing_revision_fails_closed(repository):
    assert lc.main(["scan", "--rev", "nonexistent-audit-reference"]) == 1
    assert lc.main(["scan", "--history", "nonexistent-audit-reference"]) == 1


def test_git_failure_is_not_an_empty_success(repository, monkeypatch):
    monkeypatch.setattr(lc, "ROOT", str(repository[0] / "missing"))
    assert lc.main(["scan", "--all"]) == 1


def test_renamed_blob_keeps_historical_path_checks(repository, monkeypatch):
    root, git = repository
    base = git("rev-parse", "HEAD")
    (root / "data").mkdir()
    (root / "data" / "fixture.txt").write_text("Fixture text\n")
    git("add", ".")
    git("commit", "-m", "Fixture forbidden path")
    git("mv", "data/fixture.txt", "safe.txt")
    git("commit", "-m", "Fixture renamed")
    head = git("rev-parse", "HEAD")
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"refs/heads/main {head} refs/heads/main {base}\n"))
    assert lc.main(["scan", "--push", "origin", pc.EXPECTED_REMOTE]) == 1
    assert lc.main(["scan", "--history", "HEAD"]) == 1


def test_malformed_push_and_unavailable_old_tip_block(repository, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("invalid input\n"))
    assert lc.main(["scan", "--push", "origin", pc.EXPECTED_REMOTE]) == 1
    head = repository[1]("rev-parse", "HEAD")
    absent = "ab" * 20
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"refs/heads/main {head} refs/heads/main {absent}\n"))
    assert lc.main(["scan", "--push", "origin", pc.EXPECTED_REMOTE]) == 1


def test_clean_history_passes_and_new_branch_checks_ancestors(repository, monkeypatch):
    head = repository[1]("rev-parse", "HEAD")
    zero = "0" * 40
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"refs/heads/main {head} refs/heads/new {zero}\n"))
    assert lc.main(["scan", "--push", "origin", pc.EXPECTED_REMOTE]) == 0
    assert lc.main(["scan", "--history", "HEAD"]) == 0


def test_unreadable_deny_list_does_not_silently_disappear(monkeypatch, tmp_path):
    monkeypatch.setattr(lc, "LOCAL_FILE", str(tmp_path))
    assert lc.main(["scan", "--all"]) == 1


def test_rehearsal_and_sqlite_sidecars_are_forbidden():
    for name in ("rehearsal/notes.txt", "transactions.sqlite3-wal", "transactions.sqlite3-shm"):
        assert lc.Rules().scan(name, b"fixture")


def archive(name, *, symlink=False):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as tar:
        item = tarfile.TarInfo(name)
        if symlink:
            item.type = tarfile.SYMTYPE
            item.linkname = "private"
            tar.addfile(item)
        else:
            body = b"Harmless fixture"
            item.size = len(body)
            tar.addfile(item, io.BytesIO(body))
    return data.getvalue()


def test_outgoing_archive_blocks_ignored_files_and_links():
    assert pc.check_archive(archive("README.md"), lc.Rules()) == 0
    assert pc.check_archive(archive("nested/.env"), lc.Rules()) == 1
    for name, link in [("../escape", False), ("link", True)]:
        with pytest.raises(lc.ScanError):
            pc.check_archive(archive(name, symlink=link), lc.Rules())


def test_effective_commit_identity_must_be_dedicated():
    pc.check_identity("Worm <wormrobinhood@users.noreply.github.com> 1 +0000")
    pc.check_identity("Worm <123+wormrobinhood@users.noreply.github.com> 1 +0000")
    for identity in ("Worm <someone@example.invalid> 1 +0000", "Someone <wormrobinhood@users.noreply.github.com> 1 +0000"):
        with pytest.raises(lc.ScanError):
            pc.check_identity(identity)
