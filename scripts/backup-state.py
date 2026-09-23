"""Offline backup: stop WORM first. Does not load application code, keys or configuration."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('source', type=Path)
parser.add_argument('destination', type=Path)
parser.add_argument('--service-stopped', action='store_true', required=True,
                    help='attest all writers/signers using this state have been stopped')
args = parser.parse_args()
args.source = args.source.resolve()
args.destination = args.destination.resolve()
if args.destination == args.source or args.source in args.destination.parents:
    raise SystemExit('Keep backups outside the active state directory; do not fill its volume with snapshots.')
if not (args.source / 'wormhole.db').is_file():
    raise SystemExit('source application database missing')
parent = args.destination.parent
while not parent.exists():
    parent = parent.parent
required = 16 * 1024 * 1024 + 2 * sum(p.stat().st_size for p in args.source.iterdir() if p.is_file())
if shutil.disk_usage(parent).free < required:
    raise SystemExit('destination needs more free space for a verified backup')
os.umask(0o077)
args.destination.mkdir(mode=0o700, parents=True, exist_ok=False)
manifest = {'version': 1, 'files': {}}
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
    saved = args.destination / name
    with saved.open('rb') as f:
        digest = hashlib.file_digest(f, 'sha256').hexdigest()
    manifest['files'][name] = {'bytes': saved.stat().st_size, 'sha256': digest}
(args.destination / 'payments.paused').touch(mode=0o600)
(args.destination / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
print('Backup verified. Store encrypted; restore starts with payments paused.')
