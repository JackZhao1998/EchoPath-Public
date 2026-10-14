"""Desktop app entry point: the Control Room plus native tray/menu-bar controls."""
from __future__ import annotations

import os
import argparse
import sys
import webbrowser
from pathlib import Path
from urllib.request import urlopen

PORT = 8765


def alert(title, message):
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(message), str(title), 0x10)
        return
    try:
        from AppKit import NSAlert, NSApplication
        NSApplication.sharedApplication()
        dialog = NSAlert.alloc().init()
        dialog.setMessageText_(title)
        dialog.setInformativeText_(message)
        dialog.runModal()
    except Exception:
        print(f'{title}: {message}', file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description='EchoPath desktop app.')
    parser.add_argument('--config', default='')
    args = parser.parse_args(argv)
    # The launcher starts in the checkout, so this is the same echopath.local.json the CLI uses.
    config = Path(args.config or os.environ.get('ECHOPATH_CONFIG') or Path.cwd() / 'echopath.local.json').expanduser()
    url = f'http://localhost:{PORT}'
    try:
        with urlopen(url + '/api/status', timeout=1):
            pass
        # Already running (the app, or a dashboard started from a terminal): show it rather than fail on the port.
        webbrowser.open(url)
        return 0
    except OSError:
        pass
    if not config.is_file():
        alert('EchoPath needs a configuration file',
              f'Create {config} (copy echopath.config.json next to it), then open EchoPath again.')
        return 1
    from .dashboard import main as dashboard
    return dashboard(['--config', str(config), '--port', str(PORT), '--tray', '--no-open'])


if __name__ == '__main__':
    raise SystemExit(main())
