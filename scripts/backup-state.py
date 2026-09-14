"""Offline backup: stop WORM first. Does not load application code, keys or configuration."""
import argparse
import os
from pathlib import Path
import sqlite3

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('source', type=Path)
parser.add_argument('destination', type=Path)
parser.add_argument('--service-stopped', action='store_true', required=True,
                    help='attest all writers/signers using this state have been stopped')
args = parser.parse_args()
args.source = args.source.resolve()
args.destination = args.destination.resolve()
os.umask(0o077)
args.destination.mkdir(mode=0o700, parents=True, exist_ok=False)
for name in ('wormhole.db', 'transactions.sqlite3'):
    source = args.source / name
    if not source.exists():
        if name == 'wormhole.db':
            raise SystemExit('source application database missing')
        continue
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src:
        with sqlite3.connect(args.destination / name) as dest:
            src.backup(dest)
            if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise SystemExit('backup integrity check failed')
(args.destination / 'payments.paused').touch(mode=0o600)
print('Backup verified. Store encrypted; restore starts with payments paused.')
