#!/usr/bin/env python3
"""Build EchoPath.app, a menu-bar app that owns its own desktop permissions.

macOS grants Accessibility and Screen Recording to an application. Started from
a shell, EchoPath's grants attach to the terminal, so every script that terminal
runs can drive the Mac. Inside this bundle they attach to EchoPath alone.

The bundle is a thin wrapper: its launcher runs this checkout's virtualenv, so
it stays a few hundred kilobytes and code changes apply on the next launch with
no rebuild. Moving the checkout means rebuilding the app.

    python packaging/macos/make_app.py             build into /Applications
    python packaging/macos/make_app.py --run       build, then launch it
    python packaging/macos/make_app.py --dest DIR  build somewhere else
"""
from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
BUNDLE_ID = 'com.zeroshotmind.echopath'
NAME = 'EchoPath'
LAUNCHER_SRC = HERE / 'launcher' / 'main.c'
ICON_CACHE = HERE / 'EchoPath.icns'
MANIFEST = 'Contents/Resources/build-manifest.json'


def version():
    for line in (PROJECT / 'pyproject.toml').read_text().splitlines():
        if line.startswith('version'):
            return line.split('=', 1)[1].strip().strip('"')
    return '0.0.0'


def venv_layout():
    """Where the interpreter, its stdlib and the installed packages live."""
    venv_python = (PROJECT / '.venv' / 'bin' / 'python').resolve()
    if not venv_python.exists():
        raise SystemExit(f'No virtualenv at {PROJECT / ".venv"}. Run packaging/macos/install.sh first.')
    site = sorted((PROJECT / '.venv' / 'lib').glob('python3.*/site-packages'))
    if not site:
        raise SystemExit('The virtualenv has no site-packages.')
    base = venv_python.parents[1]
    lib = site[0].parent.name
    return {'base_prefix': base, 'libdir': base / 'lib', 'dylib': base / 'lib' / f'lib{lib}.dylib', 'version': lib, 'site': site[0]}


def compile_launcher(target, layout):
    if shutil.which('clang') is None:
        raise SystemExit('clang not found. Install the Xcode Command Line Tools: xcode-select --install')
    if not layout['dylib'].exists():
        raise SystemExit(f'{layout["dylib"]} is missing; use a uv- or python.org-built Python 3.12.')
    command = ['clang', '-O2', '-Wall', '-o', str(target), str(LAUNCHER_SRC),
               f'-DECHOPATH_PROJECT="{PROJECT}"',
               f'-DECHOPATH_PYTHONHOME="{layout["base_prefix"]}"',
               f'-DECHOPATH_SITE="{layout["site"]}"',
               f'-L{layout["libdir"]}', f'-l{layout["version"]}', '-Wl,-rpath,' + str(layout['libdir'])]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise SystemExit('clang failed:\n' + result.stderr.strip()[:1200])


def render_icon(iconset):
    """Draw the EchoPath mark (a dot and two echo arcs) on a dark squircle."""
    from AppKit import NSBezierPath, NSBitmapImageRep, NSColor, NSGraphicsContext, NSPNGFileType, NSCalibratedRGBColorSpace
    from Foundation import NSMakePoint, NSMakeRect

    size = 1024
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
    NSColor.clearColor().set()
    NSBezierPath.fillRect_(NSMakeRect(0, 0, size, size))
    # macOS icon grid: 824 px body inside the 1024 canvas.
    NSColor.colorWithCalibratedRed_green_blue_alpha_(0x17 / 255, 0x18 / 255, 0x1B / 255, 1).set()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(100, 100, 824, 824), 185, 185).fill()
    blue = NSColor.colorWithCalibratedRed_green_blue_alpha_(0x6E / 255, 0x8D / 255, 1, 1)
    white = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.96, 0.96, 0.95, 1)
    cx, cy = 400, 512
    blue.set()
    NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(cx - 62, cy - 62, 124, 124)).fill()
    white.set()
    for radius in (170, 290):
        arc = NSBezierPath.bezierPath()
        arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(NSMakePoint(cx, cy), radius, -48, 48)
        arc.setLineWidth_(54)
        arc.setLineCapStyle_(1)
        arc.stroke()
    NSGraphicsContext.restoreGraphicsState()
    iconset.mkdir(parents=True, exist_ok=True)
    master = iconset / 'master.png'
    rep.representationUsingType_properties_(NSPNGFileType, {}).writeToFile_atomically_(str(master), True)
    for side in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            out = iconset / f'icon_{side}x{side}{"@2x" if scale == 2 else ""}.png'
            subprocess.run(['sips', '-z', str(side * scale), str(side * scale), str(master), '--out', str(out)],
                           check=True, capture_output=True)
    master.unlink()


def bundle_info():
    return {
        'CFBundleName': NAME, 'CFBundleDisplayName': NAME, 'CFBundleIdentifier': BUNDLE_ID,
        'CFBundleExecutable': NAME, 'CFBundleIconFile': NAME, 'CFBundlePackageType': 'APPL',
        'CFBundleShortVersionString': version(), 'CFBundleVersion': version(),
        'CFBundleInfoDictionaryVersion': '6.0', 'LSMinimumSystemVersion': '13.0',
        # Menu-bar only: no Dock icon.
        'LSUIElement': True, 'NSHighResolutionCapable': True,
        'NSAppleEventsUsageDescription': 'EchoPath operates apps on your desktop when you allow desktop control for a task.',
    }


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_manifest(info, layout):
    # Compare inputs, not outputs: the linker stamps a new UUID into every build.
    return {'bundle_id': BUNDLE_ID, 'launcher_source': digest(LAUNCHER_SRC), 'project': str(PROJECT),
            'python_home': str(layout['base_prefix']), 'site': str(layout['site']), 'info_plist': info}


def installed_manifest(app):
    try:
        return json.loads((app / MANIFEST).read_text())
    except (OSError, ValueError):
        return None


def build(dest, force=False):
    """Rebuild only when an input changed.

    macOS keys the Accessibility and Screen Recording grants to the code
    signature, and re-signing even an identical bundle revokes them.
    """
    app = dest / f'{NAME}.app'
    layout = venv_layout()
    info = bundle_info()
    manifest = build_manifest(info, layout)
    if not force and app.exists() and installed_manifest(app) == manifest:
        print('  unchanged: leaving the installed app alone so its permissions survive (--force to rebuild)')
        return app

    staging = dest / f'.{NAME}.app.build'
    shutil.rmtree(staging, ignore_errors=True)
    macos, resources = staging / 'Contents' / 'MacOS', staging / 'Contents' / 'Resources'
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    if not ICON_CACHE.exists() or force:
        print('  drawing icon…')
        iconset = resources / f'{NAME}.iconset'
        render_icon(iconset)
        subprocess.run(['iconutil', '-c', 'icns', str(iconset), '-o', str(ICON_CACHE)], check=True)
        shutil.rmtree(iconset)
    shutil.copy2(ICON_CACHE, resources / f'{NAME}.icns')

    with (staging / 'Contents' / 'Info.plist').open('wb') as handle:
        plistlib.dump(info, handle)
    (staging / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True))

    print('  compiling launcher…')
    compile_launcher(macos / NAME, layout)

    # Ad-hoc signature: without one the grant is keyed to a hash that changes on every build.
    print('  signing (ad-hoc)…')
    subprocess.run(['codesign', '--force', '--deep', '--sign', '-', '--identifier', BUNDLE_ID, str(staging)],
                   check=True, capture_output=True)
    subprocess.run(['codesign', '--verify', str(staging)], check=True, capture_output=True)

    replaced = app.exists()
    if replaced:
        shutil.rmtree(app)
    staging.rename(app)
    if replaced:
        print('  the signature changed, so macOS dropped earlier grants: allow EchoPath again when asked.')
    return app


def main():
    parser = argparse.ArgumentParser(description='Build EchoPath.app')
    parser.add_argument('--dest', default='/Applications', help='where to put the app (default: /Applications)')
    parser.add_argument('--run', action='store_true', help='open the app after building')
    parser.add_argument('--force', action='store_true', help='rebuild and re-sign even if nothing changed (revokes grants)')
    args = parser.parse_args()
    dest = Path(args.dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    print(f'Building {NAME}.app -> {dest}')
    app = build(dest, force=args.force)
    size = sum(f.stat().st_size for f in app.rglob('*') if f.is_file())
    print(f'  built {app} ({size / 1024:.0f} KB)')
    print(f'  logs: ~/.echopath/app.log · runs from {PROJECT}')
    if args.run:
        subprocess.run(['open', str(app)], check=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
