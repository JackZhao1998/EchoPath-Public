#!/usr/bin/env python3
"""Build a source installation ZIP from a public-file allowlist, without Git."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def package_files(root):
    root = Path(root).resolve()
    spec = json.loads((root / 'packaging/package-files.json').read_text())
    paths = {root / name for name in spec['files']}
    for pattern in spec['patterns']:
        paths.update(root.glob(pattern))
    result = []
    for path in sorted(paths):
        relative = path.relative_to(root)
        if any(part in ('__pycache__', 'node_modules', '.venv', '.git') for part in relative.parts):
            continue
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root and root in parent.parents):
            raise ValueError(f'Package files must not be symlinks: {relative}')
        if not path.is_file():
            raise ValueError(f'Missing package file: {relative}')
        result.append(relative)
    # Refuse a personalized default config; user state belongs outside the bundle.
    config = json.loads((root / 'echopath.config.json').read_text())
    if config.get('vm') != {'enabled': False, 'profile': None} or config.get('external_command', {}).get('env'):
        raise ValueError('The public configuration must have VM setup off and no custom bridge environment.')
    return result


def copy_package(root, destination):
    root, destination = Path(root).resolve(), Path(destination).expanduser().absolute()
    if destination == root:
        return root
    if destination.is_symlink() or (destination.exists() and any(destination.iterdir())):
        raise ValueError('Destination is not empty. Use its existing setup.py to repair it, or choose an empty directory.')
    files = package_files(root)
    destination.mkdir(parents=True, exist_ok=True)
    for relative in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, target)
    return destination.resolve()


def build(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    files = package_files(root)
    match = re.search(r'^version\s*=\s*"([0-9A-Za-z.+-]+)"', (root / 'pyproject.toml').read_text(), re.M)
    if not match:
        raise ValueError('Cannot find package version.')
    folder = 'EchoPath-' + match[1]
    output.mkdir(parents=True, exist_ok=True)
    target = output / (folder + '-source.zip')
    manifest = {'format': 1, 'version': match[1], 'files': {str(p).replace('\\', '/'): hashlib.sha256((root / p).read_bytes()).hexdigest() for p in files}}
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for relative in files:
            archive.write(root / relative, folder + '/' + relative.as_posix())
        archive.writestr(folder + '/PACKAGE.json', json.dumps(manifest, indent=2) + '\n')
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix('.zip.sha256').write_text(digest + '  ' + target.name + '\n')
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    print(build(ROOT, args.output))


if __name__ == '__main__':
    main()
