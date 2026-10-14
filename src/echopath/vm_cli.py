"""Command-line VM setup for coding agents; the Config window drives the same VMSetup.

Every command prints one JSON object and exits nonzero on failure. Bridge keys
and SSH private keys are never printed or accepted as arguments.
"""
from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .vm_setup import VMSetup, VMSetupError, missing_permissions

JOBS = ('test', 'install', 'create', 'connect')
# Runs in the guest through Tart's agent: authorize one key and make a 3.10+ python3
# visible to non-interactive SSH (macOS guests ship 3.9; Homebrew is not on that PATH).
GUEST_PREPARE = r'''
set -e
mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
grep -qxF "$1" ~/.ssh/authorized_keys || printf '%s\n' "$1" >> ~/.ssh/authorized_keys
modern() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; }
if ! modern python3; then
  for bin in /opt/homebrew/bin /usr/local/bin; do
    if modern "$bin/python3"; then
      grep -qsF "$bin" ~/.zshenv || printf 'export PATH=%s:$PATH\n' "$bin" >> ~/.zshenv
      break
    fi
  done
fi
'''


def _command(command, *, label, timeout=60, input=None):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, input=input)
    except subprocess.TimeoutExpired:
        raise VMSetupError(f'{label} timed out.') from None
    except OSError:
        raise VMSetupError(f'{label} could not start.') from None
    if result.returncode:
        raise VMSetupError(f'{label} failed: ' + ((result.stderr or result.stdout).strip().splitlines() or ['no output'])[-1][:300])
    return result.stdout


def _tart():
    path = shutil.which('tart') or str(Path.home() / '.local/bin/tart')
    if not Path(path).is_file():
        raise VMSetupError('Tart is not installed. See document/vm_setup.md, "Create a macOS VM with Tart".')
    return path


def _health(vm):
    import httpx
    profile = vm.config()['profile']
    if not profile:
        return None
    try:
        with httpx.Client(timeout=4, follow_redirects=False, trust_env=False) as client:
            return client.get(profile['endpoint'] + '/health').json()
    except Exception:
        return None


def status(vm):
    state = vm.status()
    state['job'] = {k: v for k, v in state.get('job', {}).items() if k in ('running', 'action', 'ok', 'message')}
    health = _health(vm) if state['enabled'] else None
    if isinstance(health, dict):
        state['guest'] = {'permissions': health.get('permissions'), 'python': health.get('python'),
                          'desktop_session': health.get('desktop_session'),
                          'missing_permissions': missing_permissions(health)}
    return state


def run_job(vm, action, timeout):
    vm.start(action)
    deadline, last = time.monotonic() + timeout, None
    while True:
        job = vm.status()['job']
        if job.get('message') != last:
            last = job.get('message')
            print(f'[{action}] {last}', file=sys.stderr, flush=True)
        if not job.get('running'):
            if not job.get('ok'):
                raise VMSetupError(job.get('message') or f'VM {action} failed.')
            return status(vm)
        if time.monotonic() > deadline:
            raise VMSetupError(f'VM {action} is still running; check `status` later.')
        time.sleep(2)


def save(vm, profile):
    if vm.config()['enabled']:
        raise VMSetupError('VM integration is on. Run `disable` before changing the saved connection.')
    vm.save(profile)
    return status(vm)


def screenshot(vm, out):
    from .amazon_vm_service import AmazonVMDesktopController
    from .config import load_global_model_router_config
    profile = vm.profile()
    controller = AmazonVMDesktopController(
        endpoint=profile['endpoint'], credential_file=vm.credentials, timeout_seconds=15,
        api_key_env=load_global_model_router_config(vm.path).desktop.amazon_vm_api_key_env)
    capture = controller._post_json('/observe', {})
    out = Path(out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(base64.b64decode(capture['image_base64'], validate=True))
    return {'saved': str(out), 'screen_size': capture.get('observation', {}).get('screen_size')}


def trust_host_key(host, key_line):
    """Record a host key that was read from inside the guest, replacing a stale entry for a reused address."""
    kind, key = key_line.split()[:2]
    known = Path.home() / '.ssh' / 'known_hosts'
    found = subprocess.run(['ssh-keygen', '-F', host], capture_output=True, text=True).stdout
    entries = [line.split() for line in found.splitlines() if line and not line.startswith('#')]
    if any(len(e) >= 3 and e[1] == kind and e[2] == key for e in entries):
        return 'already trusted'
    replaced = bool(entries)
    if replaced:
        _command(['ssh-keygen', '-R', host], label='Removing the stale known_hosts entry')
    known.parent.mkdir(mode=0o700, exist_ok=True)
    with known.open('a') as stream:
        stream.write(f'{host} {kind} {key}\n')
    return 'replaced a stale entry' if replaced else 'added'


def tart_link(vm, name, identity_file, connection_name):
    tart = _tart()
    address = _command([tart, 'ip', name], label='Reading the VM address (is it running?)').strip()
    key = Path(identity_file).expanduser() if identity_file else vm.path.parent / 'credentials' / 'vm_ssh_ed25519'
    if not key.is_file():
        key.parent.mkdir(parents=True, exist_ok=True)
        _command(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'echopath-vm', '-f', str(key)], label='Creating an SSH key')
    public = Path(str(key) + '.pub').read_text().strip()
    guest = lambda *command, label: _command([tart, 'exec', name, *command], label=label)  # noqa: E731
    user = guest('whoami', label='Reaching the guest agent').strip()
    guest('sh', '-c', GUEST_PREPARE, 'sh', public, label='Preparing the guest account')
    host_key = guest('cat', '/etc/ssh/ssh_host_ed25519_key.pub', label='Reading the guest SSH host key (enable Remote Login)').strip()
    trust = trust_host_key(address, host_key)
    profile = {'id': 'default', 'name': connection_name or name, 'mode': 'ssh', 'auto_reconnect': True,
               'ssh': {'host': address, 'user': user, 'identity_file': str(key)}}
    state = save(vm, profile)
    guest_python = vm.inspect(vm.profile())
    return {**state, 'tart': {'vm': name, 'address': address, 'user': user, 'host_key': trust,
                              'identity_file': str(key), 'guest_python': guest_python['python']}}


def build_parser():
    parser = argparse.ArgumentParser(prog='echopath-vm', description=__doc__.splitlines()[0])
    parser.add_argument('--config', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status', help='Saved connection, connection state and guest permissions.')
    ssh = commands.add_parser('save-ssh', help='Save an existing VM reached over SSH.')
    ssh.add_argument('--host', required=True, help='Hostname, IP address or ~/.ssh/config alias.')
    ssh.add_argument('--user', default='')
    ssh.add_argument('--port', type=int)
    ssh.add_argument('--identity-file', default='', help='Path to a private key; never its contents.')
    ssh.add_argument('--display', default=':0', help='Linux X11 display.')
    ssh.add_argument('--xauthority', default='')
    lima = commands.add_parser('save-lima', help='Save a new Linux desktop VM to create with Lima (macOS hosts).')
    lima.add_argument('--instance', default='echopath-desktop')
    lima.add_argument('--cpus', type=int, default=4)
    lima.add_argument('--memory-gib', type=int, default=4)
    lima.add_argument('--disk-gib', type=int, default=30)
    vbox = commands.add_parser('save-virtualbox', help='Save a Windows desktop VM to create with VirtualBox (Windows hosts).')
    vbox.add_argument('--instance', default='echopath-windows')
    vbox.add_argument('--iso', required=True, help='Path to an official Windows x64 installation ISO.')
    vbox.add_argument('--cpus', type=int, default=4)
    vbox.add_argument('--memory-gib', type=int, default=8)
    vbox.add_argument('--disk-gib', type=int, default=80)
    direct = commands.add_parser('save-direct', help='Save an existing desktop bridge URL; its key comes from the configured environment variable.')
    direct.add_argument('--endpoint', required=True)
    for saver in (ssh, lima, direct, vbox):
        saver.add_argument('--name', default='', help='Connection name shown in Config.')
        saver.add_argument('--local-port', type=int, default=8791)
        saver.add_argument('--bridge-port', type=int, default=8790)
    tart = commands.add_parser('tart-link', help='Prepare a running Tart VM for SSH (key, host key, Python path) and save it.')
    tart.add_argument('vm')
    tart.add_argument('--identity-file', default='', help='Existing passphrase-free key; default creates credentials/vm_ssh_ed25519.')
    tart.add_argument('--name', default='')
    for action, text in (('test', 'Check SSH and guest Python; installs nothing.'),
                         ('install', 'Install the guest bridge, then connect.'),
                         ('create', 'Create the saved managed VM, then connect.'),
                         ('connect', 'Enable the integration and connect.')):
        commands.add_parser(action, help=text).add_argument('--timeout', type=int, default=3600 if action == 'create' else 900)
    commands.add_parser('disconnect', help='Close the tunnel; keep the saved connection.')
    commands.add_parser('disable', help='Turn VM integration off (required before saving or installing).')
    commands.add_parser('restart-bridge', help='Restart the guest service, e.g. after granting macOS permissions.')
    commands.add_parser('open-window', help='Open the visible window of the saved VirtualBox VM.')
    shot = commands.add_parser('screenshot', help='Save one guest screen capture for inspection.')
    shot.add_argument('--out', required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    vm = VMSetup(Path(args.config).expanduser().resolve())
    try:
        if args.command == 'status':
            result = status(vm)
        elif args.command == 'save-ssh':
            result = save(vm, {'id': 'default', 'name': args.name, 'mode': 'ssh', 'auto_reconnect': True,
                               'local_port': args.local_port, 'bridge_port': args.bridge_port,
                               'display': args.display, 'xauthority': args.xauthority,
                               'ssh': {'host': args.host, 'user': args.user, 'port': args.port,
                                       'identity_file': args.identity_file}})
        elif args.command == 'save-lima':
            result = save(vm, {'id': 'default', 'name': args.name or args.instance, 'mode': 'lima', 'auto_reconnect': True,
                               'local_port': args.local_port, 'bridge_port': args.bridge_port, 'instance': args.instance,
                               'cpus': args.cpus, 'memory_gib': args.memory_gib, 'disk_gib': args.disk_gib})
        elif args.command == 'save-direct':
            result = save(vm, {'id': 'default', 'name': args.name, 'mode': 'direct', 'auto_reconnect': True,
                               'endpoint': args.endpoint})
        elif args.command == 'save-virtualbox':
            result = save(vm, {'id': 'default', 'name': args.name or args.instance, 'mode': 'virtualbox', 'auto_reconnect': True,
                               'local_port': args.local_port, 'bridge_port': args.bridge_port, 'instance': args.instance,
                               'iso_path': args.iso, 'cpus': args.cpus, 'memory_gib': args.memory_gib, 'disk_gib': args.disk_gib})
        elif args.command == 'tart-link':
            result = tart_link(vm, args.vm, args.identity_file, args.name)
        elif args.command in JOBS:
            result = run_job(vm, args.command, args.timeout)
        elif args.command == 'disconnect':
            vm.disconnect()
            result = status(vm)
        elif args.command == 'disable':
            vm.disable()
            result = status(vm)
        elif args.command == 'restart-bridge':
            vm.restart_bridge(vm.profile())
            time.sleep(4)
            result = status(vm)
        elif args.command == 'open-window':
            if vm.profile()['mode'] != 'virtualbox':
                raise VMSetupError('Open the guest console in your VM provider. This command opens managed VirtualBox guests.')
            from .vm_virtualbox import VirtualBoxVM
            VirtualBoxVM(vm, vm.profile()).open()
            result = status(vm)
        else:
            result = screenshot(vm, args.out)
    except (VMSetupError, ValueError, OSError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        return 1
    print(json.dumps({'ok': True, **result}, indent=2, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
