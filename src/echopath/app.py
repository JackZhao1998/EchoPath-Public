"""Entry point for EchoPath.app: the Control Room server plus the menu-bar popover."""
from __future__ import annotations

import os
import sys
import webbrowser
from pathlib import Path
from urllib.request import urlopen

PORT = 8765


def alert(title, message):
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
    # The launcher starts in the checkout, so this is the same echopath.local.json the CLI uses.
    config = Path(os.environ.get('ECHOPATH_CONFIG') or Path.cwd() / 'echopath.local.json').expanduser()
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
