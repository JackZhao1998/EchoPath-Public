#!/usr/bin/env python3
"""Validate the public source package without model calls or desktop actions."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

from build_package import ROOT, build, copy_package, package_files


def require(condition, message):
    if not condition:
        raise ValueError(message)


def anchors(text):
    result, counts = set(), {}
    for match in re.finditer(r'^#{1,6}\s+(.+?)\s*#*$', text, re.M):
        title = re.sub(r'<[^>]+>', '', match[1]).lower()
        slug = re.sub(r'[^\w\-\s]', '', title).replace(' ', '-')
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        result.add(slug + (f'-{count}' if count else ''))
    result.update(re.findall(r'(?:id|name)=[\"\']([^\"\']+)', text))
    return result


def check_links(root, files):
    # Windows runners can expose TEMP through an 8.3 alias; compare resolved paths.
    root = Path(root).resolve()
    checked = 0
    for relative in files:
        if relative.suffix != '.md':
            continue
        source = root / relative
        text = source.read_text(encoding='utf-8')
        # Skip code blocks: example syntax there is not a rendered link.
        text = re.sub(r'```.*?```', '', text, flags=re.S)
        links = re.findall(r'!?\[[^\]\n]*\]\(([^)\n]+)\)|(?:href|src|srcset)="([^"]+)"', text)
        for markdown, html in links:
            raw = (markdown or html).strip().split(' "', 1)[0].strip('<>')
            parsed = urlsplit(raw)
            if parsed.scheme or parsed.netloc:
                continue
            target = (source.parent / unquote(parsed.path)).resolve() if parsed.path else source
            require(target == root or root in target.parents, f'{relative}: link escapes package: {raw}')
            require(target.exists(), f'{relative}: missing link target: {raw}')
            if parsed.fragment and target.suffix == '.md':
                require(unquote(parsed.fragment) in anchors(target.read_text(encoding='utf-8')),
                        f'{relative}: missing heading: {raw}')
            checked += 1
    return checked


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def check_memories(root, files):
    selected = {p.as_posix() for p in files}
    records = sorted(p for p in selected if p.startswith('memory/consolidated/records/') and p.endswith('.json'))
    require(len(records) == 4, 'Release must include the four approved Stocks/Weather example records.')
    catalog = json.loads((root / 'memory/consolidated/index.json').read_text(encoding='utf-8'))
    entries = catalog['memories']
    require(len(entries) == len(records), 'Example catalog has missing or extra records.')
    require(len({m['memory_id'] for m in entries}) == len(entries), 'Duplicate example memory ID.')
    require({f'memory/consolidated/{m["path"]}' for m in entries} == set(records),
            'Example catalog does not match the packaged records.')
    for path in records:
        record = json.loads((root / path).read_text(encoding='utf-8'))
        require(record.get('status') == 'active' and record.get('replayable'), f'Inactive example: {path}')
        entry = next(m for m in entries if m['memory_id'] == record['memory_id'])
        require(entry.get('status') == 'active' and entry.get('replayable'), f'Inactive catalog entry: {path}')
        for value in strings(record):
            if value.startswith('memory/consolidated/crops/'):
                require(value in selected, f'{path}: crop missing from package: {value}')
    return len(records)


def check(root, output):
    root = Path(root).resolve()
    files = package_files(root)
    selected = {p.as_posix() for p in files}
    require(len(selected) == len(files), 'Duplicate package paths.')
    require('AGENT_HANDOFF.md' not in selected, 'Retired developer handoff is packaged.')
    forbidden = ('test/', 'tests/', 'site/', 'local_data/', 'log/', 'outputs/')
    require(not any(p.startswith(forbidden) for p in selected), 'Private/test/developer files are packaged.')
    require(not any('clock-timer-000015-close' in p for p in selected), 'Deprecated Clock assets are packaged.')
    require(not any(p.startswith('credentials/') and p != 'credentials/README.md' for p in selected),
            'Credential files are packaged.')
    require(not any(p.startswith('.') and p != '.gitignore' for p in selected), 'Hidden local files are packaged.')
    old_repo = re.compile(r'https://github\.com/JackZhao1998/EchoPath(?![-\w])')
    python_count = json_count = 0
    for relative in files:
        path = root / relative
        if path.suffix == '.py':
            ast.parse(path.read_text(encoding='utf-8'), filename=str(relative))
            python_count += 1
        elif path.suffix == '.json':
            json.loads(path.read_text(encoding='utf-8'))
            json_count += 1
        if path.suffix in ('.md', '.txt', '.toml', '.srt', '.vtt'):
            require(not old_repo.search(path.read_text(encoding='utf-8')), f'Old repository URL: {relative}')
    # A new public asset must be explicitly approved before a package can omit it.
    for folder in ('media', 'document/assets'):
        for path in (root / folder).rglob('*'):
            if path.is_file():
                require(path.relative_to(root).as_posix() in selected, f'Unapproved or omitted public asset: {path}')
    memory_count = check_memories(root, files)
    archive = build(root, output)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    require(archive.with_suffix('.zip.sha256').read_text().split()[0] == digest, 'ZIP checksum differs.')
    with tempfile.TemporaryDirectory(prefix='echopath-release-check-') as temporary:
        temporary = Path(temporary)
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            prefix = names[0].split('/', 1)[0]
            require(all(not Path(name).is_absolute() and '..' not in Path(name).parts for name in names),
                    'Unsafe archive member path.')
            require(len(names) == len(set(names)), 'Duplicate archive members.')
            manifest = json.loads(bundle.read(prefix + '/PACKAGE.json'))
            require(set(manifest['files']) == selected, 'Archive manifest differs from allowlist.')
            require(set(names) == {prefix + '/' + p for p in selected} | {prefix + '/PACKAGE.json'},
                    'Unexpected or missing archive member.')
            for name, expected in manifest['files'].items():
                require(hashlib.sha256(bundle.read(prefix + '/' + name)).hexdigest() == expected,
                        f'Archive hash mismatch: {name}')
            bundle.extractall(temporary / 'extracted')
        extracted = temporary / 'extracted' / prefix
        require({p.as_posix() for p in package_files(extracted)} == selected,
                'Extracted package cannot reproduce its allowlist.')
        links = check_links(extracted, files)
        destination = copy_package(root, temporary / 'destination')
        actual = {p.relative_to(destination).as_posix() for p in destination.rglob('*') if p.is_file()}
        require(actual == selected, 'Destination copy contains extra or missing files.')
        for relative in files:
            require((destination / relative).read_bytes() == (extracted / relative).read_bytes(),
                    f'Destination/archive bytes differ: {relative}')
    return {'ok': True, 'files': len(files), 'python': python_count, 'json': json_count,
            'local_links': links, 'active_example_records': memory_count,
            'archive': str(archive), 'sha256': digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, help='Keep the source ZIP and checksum in this directory.')
    args = parser.parse_args()
    if args.output:
        result = check(args.root, args.output)
    else:
        with tempfile.TemporaryDirectory(prefix='echopath-source-') as output:
            result = check(args.root, output)
            result['archive'] = '(temporary archive verified and removed)'
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
