#!/usr/bin/env python3
"""Read-only publication preflight; never uploads, signs a payment, or changes account identity."""
import argparse
import io
import os
import pathlib
import re
import subprocess
import sys
import tarfile

import leakcheck as lc

EXPECTED_REMOTE = "git@github-worm:wormrobinhood/worm.git"
EXPECTED_EMAIL = re.compile(r"(?:\d+\+)?wormrobinhood@users\.noreply\.github\.com", re.I)
PROJECT_NAMES = {"worm", "worm robinhood", "wormrobinhood"}


def check_identity(identity):
    match = re.fullmatch(r"(.+?) <([^<>]+)> \d+ [+-]\d{4}", identity.strip())
    if not match or match[1].lower() not in PROJECT_NAMES or not EXPECTED_EMAIL.fullmatch(match[2]):
        raise lc.ScanError("author/committer must use the dedicated WORM identity and GitHub noreply email")


def check_archive(data, rules):
    findings = {}
    count = 0
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for item in archive:
            path = pathlib.PurePosixPath(item.name)
            if path.is_absolute() or ".." in path.parts or not (item.isdir() or item.isfile()):
                raise lc.ScanError("unsafe archive entry; publication blocked")
            if item.isfile():
                if item.size > lc.MAX_BYTES:
                    raise lc.ScanError("archive file exceeds review limit")
                count += 1
                matches = rules.scan(item.name, archive.extractfile(item).read())
                if matches:
                    findings[item.name] = matches
    if count == 0:
        raise lc.ScanError("empty archive")
    return lc.report(findings, count, "outgoing archive")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--archive", type=pathlib.Path, help="write the verified archive to a NEW private file")
    args = parser.parse_args()
    try:
        if lc.git("remote", "get-url", "--push", "--all", "origin").decode().splitlines() != [EXPECTED_REMOTE]:
            raise lc.ScanError("origin does not use the dedicated WORM SSH destination")
        for field in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
            check_identity(lc.git("var", field).decode())
        auth = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                               "-o", "ConnectTimeout=15", "-T", "git@github-worm"],
                              capture_output=True, text=True, timeout=25)
        greeting = re.search(r"Hi ([^!]+)! You've successfully authenticated", auth.stdout + auth.stderr)
        if not greeting or greeting[1] not in ("wormrobinhood", "wormrobinhood/worm"):
            raise lc.ScanError("SSH authentication did not confirm WORM; account details withheld")
        rev = lc.git("rev-parse", "--verify", args.revision + "^{commit}").decode().strip()
        if lc.main(["leakcheck", "--history", rev]):
            return 1
        archive = lc.git("archive", "--format=tar", rev)
        if check_archive(archive, lc.Rules()):
            return 1
        if args.archive:
            fd = os.open(args.archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as dest:
                dest.write(archive)
        print("Publication preflight passed for " + rev)
        return 0
    except (lc.ScanError, OSError, ValueError, tarfile.TarError, subprocess.TimeoutExpired) as exc:
        detail = str(exc) if isinstance(exc, lc.ScanError) else "unable to verify publication inputs"
        print("Publication BLOCKED: " + detail, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
