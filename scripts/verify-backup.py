"""Verify an offline WORM backup after transfer. Never loads application configuration or signs."""
import argparse
import hashlib
import json
import sqlite3

from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('backup', type=Path)
args = parser.parse_args()
root = args.backup.resolve()


def require(condition):
    if not condition:
        raise ValueError('invalid backup')


try:
    manifest = json.loads((root / 'manifest.json').read_text())
    files = manifest['files']
    require(manifest['version'] == 1 and 'wormhole.db' in files)
    require(set(files) <= {'wormhole.db', 'transactions.sqlite3'})
    require((root / 'payments.paused').is_file())
    require((root / 'transactions.sqlite3').exists() == ('transactions.sqlite3' in files))
    for name, expected in files.items():
        path = root / name
        require(not path.is_symlink() and path.is_file())
        require(not (root / (name + '-wal')).exists())  # finalized offline backup, not a running database
        require(path.stat().st_size == expected['bytes'])
        with path.open('rb') as stream:
            require(hashlib.file_digest(stream, 'sha256').hexdigest() == expected['sha256'])
        with sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True) as conn:
            require(conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok')
    print('Backup checksums and database integrity verified. Keep payments paused until reconciliation.')
except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
    raise SystemExit('Backup verification failed. Do not restore or remove the source; inspect privately.')
