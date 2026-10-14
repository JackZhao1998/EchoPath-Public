"""Guest-side installer, sent over authenticated SSH with its payload on stdin.

Runs only on an explicit Install/Create action. No model CLI or host credentials
are copied. The uploaded desktop bridge is from the installed host version.
"""
from __future__ import annotations

import base64
import io
import json
import os
import plistlib
import subprocess
import sys
import zipfile
from pathlib import Path


def install():
    payload = json.load(sys.stdin)
    if sys.platform not in ('linux', 'darwin') or sys.version_info < (3, 10):
        raise RuntimeError('Linux/macOS with Python 3.10 or newer is required.')
    root = Path.home() / '.local' / 'share' / 'echopath-guest'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(payload['archive']))) as archive:
        expected = {'echopath_guest/__init__.py', 'echopath_guest/amazon_vm_agent.py', 'echopath_guest/pyautogui_service.py'}
        if set(archive.namelist()) != expected:
            raise RuntimeError('Unexpected guest bundle contents.')
        archive.extractall(root)
    python = root / 'venv' / 'bin' / 'python'
    if not python.exists():
        subprocess.run([sys.executable, '-m', 'venv', str(root / 'venv')], check=True)
    dependencies = ['Pillow>=10,<13', 'pyautogui==0.9.54', 'pyperclip>=1.8,<2']
    if sys.platform == 'darwin':
        dependencies.append('pyobjc-framework-Quartz>=10')
    subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', *dependencies], check=True)
    config = root / 'connection.json'
    config.write_text(json.dumps({k: payload[k] for k in ('key', 'port', 'display', 'xauthority', 'managed')}))
    config.chmod(0o600)
    # A Python launcher avoids putting credentials in process arguments or unit files.
    launcher = root / 'start.py'
    launcher.write_text('''import json, os, subprocess, sys, time
from pathlib import Path
root = Path(__file__).parent
config = json.loads((root / 'connection.json').read_text())
os.chdir(root)
os.environ['ECHOPATH_AMAZON_VM_API_KEY'] = config['key']
if sys.platform.startswith('linux'):
    os.environ['DISPLAY'] = config['display']
    os.environ['XDG_SESSION_TYPE'] = 'x11'
    os.environ['XAUTHORITY'] = os.path.expanduser(config['xauthority']) if config['xauthority'] else str(Path.home() / '.Xauthority')
    if config['managed']:
        auth = Path(os.environ['XAUTHORITY'])
        auth.touch(mode=0o600, exist_ok=True)
        auth.chmod(0o600)
        import secrets
        subprocess.run(['xauth', '-f', str(auth), 'add', config['display'], '.', secrets.token_hex(16)], check=True)
        screen = subprocess.Popen(['Xvfb', config['display'], '-screen', '0', '1600x900x24', '-auth', str(auth), '-nolisten', 'tcp', '-noreset'])
        for _ in range(30):
            if subprocess.run(['xdpyinfo'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('The guest display did not start.')
        os.environ['XDG_CURRENT_DESKTOP'] = 'XFCE'
        desktop = subprocess.Popen(['dbus-run-session', '--', 'xfce4-session'])
os.execv(sys.executable, [sys.executable, '-m', 'echopath_guest.amazon_vm_agent', '--host', '127.0.0.1', '--port', str(config['port']), '--screenshot-directory', str(root / 'captures')])
''')
    launcher.chmod(0o700)
    if sys.platform == 'linux':
        unit_dir = Path.home() / '.config' / 'systemd' / 'user'
        unit_dir.mkdir(parents=True, exist_ok=True)
        # systemd command strings use double quotes; escape specifiers and quotes.
        def unit_quote(value):
            return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
        (unit_dir / 'echopath-guest.service').write_text(
            '[Unit]\nDescription=EchoPath guest desktop bridge\n'
            '[Service]\nType=simple\nExecStart=' + unit_quote(python) + ' ' + unit_quote(launcher) + '\n'
            'Restart=on-failure\nRestartSec=5\nKillMode=control-group\n'
            '[Install]\nWantedBy=default.target\n')
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', '--user', 'enable', 'echopath-guest.service'], check=True)
        subprocess.run(['systemctl', '--user', 'restart', 'echopath-guest.service'], check=True)
    else:
        agents = Path.home() / 'Library' / 'LaunchAgents'
        agents.mkdir(parents=True, exist_ok=True)
        target = agents / 'org.echopath.guest.plist'
        domain = f'gui/{os.getuid()}'
        subprocess.run(['launchctl', 'bootout', domain + '/org.echopath.guest'], capture_output=True)
        with target.open('wb') as stream:
            plistlib.dump({'Label': 'org.echopath.guest', 'ProgramArguments': [str(python), str(launcher)],
                          'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 5,
                          'StandardOutPath': str(root / 'service.log'), 'StandardErrorPath': str(root / 'service.log')}, stream)
        subprocess.run(['launchctl', 'bootstrap', domain, str(target)], check=True)
    print('Guest bridge installed.')


if __name__ == '__main__':
    install()
