"""Freeze the installed, tested runtime dependency closure for Python 3.11 Linux.

Fetch release hashes from PyPI. Does not install or upgrade packages. Run after testing intended
versions in the development environment, then verify requirements.lock in a clean Linux image.
"""
from concurrent.futures import ThreadPoolExecutor
import importlib.metadata as metadata
from pathlib import Path
import requests
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

root = Path(__file__).resolve().parents[1]
env = {**default_environment(), 'sys_platform':'linux', 'platform_system':'Linux', 'os_name':'posix'}
queue = [Requirement(line) for line in (root/'requirements.txt').read_text().splitlines() if line.strip() and not line.startswith('#')]
selected = {}
seen = set()
while queue:
    req = queue.pop()
    name = canonicalize_name(req.name)
    version = metadata.version(name)
    if req.specifier and version not in req.specifier:
        raise RuntimeError(f'installed {name} {version} does not satisfy {req}')
    extras = selected.setdefault(name, set())
    extras.update(req.extras)
    key = (name, tuple(sorted(extras)))
    if key in seen:
        continue
    seen.add(key)
    for text in metadata.requires(name) or []:
        dependency = Requirement(text)
        if dependency.marker is None or any(dependency.marker.evaluate({**env,'extra':extra}) for extra in {'',*extras}):
            queue.append(dependency)

def pinned(name):
    version = metadata.version(name)
    r = requests.get(f'https://pypi.org/pypi/{name}/{version}/json',timeout=30)
    r.raise_for_status()
    files = r.json()['urls']
    hashes = sorted({f['digests']['sha256'] for f in files if not f.get('yanked')})
    if not hashes:
        raise RuntimeError(f'no non-yanked release artifacts for {name}')
    return f'{name}=={version} \\\n' + ' \\\n'.join('    --hash=sha256:'+h for h in hashes)

with ThreadPoolExecutor(max_workers=6) as pool:
    lines = list(pool.map(pinned, sorted(selected)))
(root/'requirements.lock').write_text('# Tested runtime dependency closure; Python 3.11 Linux.\n# Regenerate with scripts/lock-dependencies.py after testing package upgrades.\n\n'+'\n'.join(lines)+'\n')
print(f'Locked {len(lines)} installed runtime dependencies with PyPI artifact hashes.')
