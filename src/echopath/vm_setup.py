"""Optional VM profiles, bounded setup jobs and a shared SSH tunnel supervisor.

Only explicit setup actions provision guests. The supervisor owns its own SSH
child; it never kills a PID read from disk and survives a closed Config window.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .run_state import ACTIVE, RunRegistry, desktop_lock, input_lock
from .platform_utils import process_alive


class VMSetupError(ValueError):
    pass


def write_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(value if isinstance(value, str) else json.dumps(value, indent=2) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        # Windows readers and sync/indexing software briefly deny replacement.
        # Keep the previous complete file until the atomic swap succeeds.
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if os.name != 'nt' or attempt == 19:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_json(path, default=None):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {} if default is None else default
    except (UnicodeError, json.JSONDecodeError):
        raise VMSetupError(f'Cannot read {Path(path).name}. Restore a valid JSON object before retrying.') from None
    if not isinstance(value, dict):
        raise VMSetupError(f'{Path(path).name} must contain a JSON object.')
    return value


def config_lock(path):
    return desktop_lock(Path(path).parent / 'local_data', 'configuration:' + str(Path(path).resolve()), blocking=True)


def _text(value, name, default=''):
    if value is None:
        return default
    if not isinstance(value, str) or len(value) > 1024 or any(ord(c) < 32 for c in value):
        raise VMSetupError(f'Invalid {name}.')
    return value.strip()


def _integer(value, name, default, low=1, high=65535):
    if value in (None, ''):
        return default
    if isinstance(value, bool) or not re.fullmatch(r'\d+', str(value)) or not low <= int(value) <= high:
        raise VMSetupError(f'{name} must be between {low} and {high}.')
    return int(value)


def normalize_vm(value):
    if value is None:
        return {'enabled': False, 'profile': None}
    if not isinstance(value, dict) or not isinstance(value.get('enabled', False), bool):
        raise VMSetupError('VM settings need an enabled switch and a connection profile.')
    profile = value.get('profile')
    if profile is None:
        if value.get('enabled'):
            raise VMSetupError('Configure a VM before enabling it.')
        return {'enabled': False, 'profile': None}
    if not isinstance(profile, dict):
        raise VMSetupError('Invalid VM connection profile.')
    mode = profile.get('mode', 'ssh')
    if mode not in ('ssh', 'direct', 'lima', 'virtualbox'):
        raise VMSetupError('Choose SSH, an existing bridge, Lima, or VirtualBox.')
    profile_id = _text(profile.get('id'), 'profile id', 'default')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', profile_id):
        raise VMSetupError('Invalid VM profile id.')
    result = {'id': profile_id, 'name': _text(profile.get('name'), 'connection name') or 'My VM', 'mode': mode}
    reconnect = profile.get('auto_reconnect', True)
    if not isinstance(reconnect, bool):
        raise VMSetupError('Automatic reconnection must be enabled or disabled.')
    result['auto_reconnect'] = reconnect
    if mode == 'direct':
        endpoint = _text(profile.get('endpoint'), 'bridge URL').rstrip('/')
        url = urlsplit(endpoint)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
            raise VMSetupError('Enter an HTTP(S) bridge origin without credentials or a path.')
        try:
            url.port
        except ValueError:
            raise VMSetupError('Invalid bridge port.') from None
        result['endpoint'] = endpoint
    else:
        result['local_port'] = _integer(profile.get('local_port'), 'Local port', 8791, 1024)
        result['bridge_port'] = _integer(profile.get('bridge_port'), 'Guest bridge port', 8790, 1024)
        result['endpoint'] = f"http://127.0.0.1:{result['local_port']}"
        result['display'] = _text(profile.get('display'), 'X11 display', ':0') or ':0'
        if not re.fullmatch(r':\d+(?:\.\d+)?', result['display']):
            raise VMSetupError('X11 display must look like :0 or :99.')
        result['xauthority'] = _text(profile.get('xauthority'), 'Xauthority path')
        if mode == 'ssh':
            ssh = profile.get('ssh', {})
            if not isinstance(ssh, dict):
                raise VMSetupError('Invalid SSH settings.')
            host = _text(ssh.get('host'), 'SSH host')
            user = _text(ssh.get('user'), 'SSH username')
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,252}', host):
                raise VMSetupError('Enter a hostname, IP address or SSH alias (without user@).')
            if user and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,63}\$?', user):
                raise VMSetupError('Invalid SSH username.')
            result['ssh'] = {'host': host, 'user': user, 'port': _integer(ssh.get('port'), 'SSH port', None),
                             'identity_file': _text(ssh.get('identity_file'), 'SSH key path')}
        elif mode == 'lima':
            instance = _text(profile.get('instance'), 'VM name', 'echopath-desktop')
            if not re.fullmatch(r'echopath-[a-z0-9][a-z0-9-]{0,39}', instance):
                raise VMSetupError('Use a VM name starting with echopath- and lowercase letters, digits or hyphens.')
            result.update(instance=instance, cpus=_integer(profile.get('cpus'), 'CPU cores', 4, 2, 16),
                          memory_gib=_integer(profile.get('memory_gib'), 'Memory (GiB)', 4, 2, 32),
                          disk_gib=_integer(profile.get('disk_gib'), 'Disk (GiB)', 30, 20, 200), display=':99', xauthority='')
        else:
            instance = _text(profile.get('instance'), 'VM name', 'echopath-windows')
            if not re.fullmatch(r'echopath-[a-zA-Z0-9][a-zA-Z0-9-]{0,39}', instance):
                raise VMSetupError('Use a VM name starting with echopath- and letters, digits or hyphens.')
            result.update(instance=instance, iso_path=_text(profile.get('iso_path'), 'Windows installation ISO'),
                          cpus=_integer(profile.get('cpus'), 'CPU cores', 4, 2, 16),
                          memory_gib=_integer(profile.get('memory_gib'), 'Memory (GiB)', 8, 4, 32),
                          disk_gib=_integer(profile.get('disk_gib'), 'Disk (GiB)', 80, 64, 200))
    return {'enabled': value.get('enabled', False), 'profile': result}


def missing_permissions(health):
    """Names of macOS grants a bridge reports as absent; older bridges report none."""
    permissions = health.get('permissions') if isinstance(health, dict) else None
    if not isinstance(permissions, dict):
        return []
    labels = {'screen_recording': 'Screen Recording', 'accessibility': 'Accessibility'}
    return [label for name, label in labels.items() if permissions.get(name) is False]


def _fingerprint(profile):
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()


def _alive(pid):
    return process_alive(pid)


def _executable(name):
    found = shutil.which(name)
    if not found and sys.platform == 'darwin':
        found = next((str(p) for p in (Path('/opt/homebrew/bin') / name, Path('/usr/local/bin') / name) if p.is_file()), None)
    if not found:
        raise VMSetupError(f'{name} is not installed or cannot be found. Install it, then retry.')
    return found


def _run(command, *, input=None, timeout=30, label='Command'):
    try:
        result = subprocess.run(command, input=input, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout,
                                **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
    except subprocess.TimeoutExpired:
        raise VMSetupError(f'{label} timed out. Check the connection and retry.') from None
    except OSError:
        raise VMSetupError(f'{label} could not start. Check its executable and retry.') from None
    if result.returncode:
        diagnostic = (result.stderr or '').lower()
        if 'host key' in diagnostic or 'authenticity' in diagnostic:
            raise VMSetupError('SSH host identity needs attention. Connect with ssh in Terminal and verify the host fingerprint, then retry.')
        if any(s in diagnostic for s in ('permission denied', 'authentication', 'sign_and_send_pubkey')):
            raise VMSetupError('SSH authentication failed. Sign in with your SSH key or unlock your SSH agent in Terminal, then retry.')
        raise VMSetupError(f'{label} failed. Check SSH access, guest prerequisites and available disk space, then retry.')
    return result.stdout


class VMSetup:
    def __init__(self, config_path):
        self.path = Path(config_path).resolve()
        self.root = self.path.parent / 'local_data' / 'vm-connection'
        self.credentials = self.path.parent / 'credentials' / 'amazon_vm.json'

    def config(self):
        return normalize_vm(read_json(self.path).get('vm'))

    def has_settings(self):
        return 'vm' in read_json(self.path)

    def guard(self, endpoint):
        if not self.has_settings():
            return  # Preserve existing direct-URL installations until configured.
        status = self.status()
        if not status['enabled']:
            raise VMSetupError('VM integration is off. Enable it in Config before starting a VM task.')
        if endpoint.rstrip('/') != status['endpoint']:
            raise VMSetupError('This is not the saved VM connection. Update the VM profile in Config first.')
        if not status['connected']:
            raise VMSetupError('The VM desktop is not ready. Check its connection in Config.')

    def profile(self):
        profile = self.config()['profile']
        if not profile:
            raise VMSetupError('Save a VM connection first.')
        return profile

    def _lock(self):
        return desktop_lock(self.root, 'vm-settings', blocking=True)

    def _write_config(self, value):
        with config_lock(self.path):
            payload = read_json(self.path)
            payload['vm'] = normalize_vm(value)
            write_private(self.path, payload)

    def _idle(self):
        profile = self.config()['profile']
        if profile and any(r['target'].rstrip('/') == profile['endpoint'] and r['status'] in ACTIVE for r in RunRegistry(self.path).list()):
            raise VMSetupError('Stop the active or paused VM task before changing its connection.')

    @contextmanager
    def _editing(self):
        # Serialize with task reservation and manual input, including other MCP workers.
        with self._lock():
            profile = self.config()['profile']
            target = profile['endpoint'] if profile else 'vm-unconfigured'
            with input_lock(self.path.parent / 'local_data', target), desktop_lock(self.path.parent / 'local_data', target):
                self._idle()
                job = read_json(self.root / 'job.json')
                if job.get('running') and _alive(job.get('pid')):
                    raise VMSetupError('A VM setup operation is still running.')
                yield

    def status(self):
        config = self.config()
        profile = config['profile']
        state = read_json(self.root / 'state.json')
        fresh = profile and state.get('fingerprint') == _fingerprint(profile) and _alive(state.get('pid')) and time.time() - state.get('updated', 0) < 30
        desired = read_json(self.root / 'desired.json')
        connected = bool(config['enabled'] and fresh and state.get('connected') and desired.get('connected') is not False)
        job = read_json(self.root / 'job.json')
        if job.get('running') and not _alive(job.get('pid')):
            job = {**job, 'running': False, 'ok': False, 'message': 'Setup was interrupted. Retry the operation.'}
        key = read_json(self.credentials)
        message = state.get('message') if fresh else 'Connection has not been verified.'
        if not config['enabled']:
            message = ('VM integration is off. Test or connect when you are ready.' if profile
                       else 'VM integration is off. Save a connection, then enable it.')
        elif desired.get('connected') is False:
            message = 'Disconnected. Connect when you are ready.'
        return {'configured': bool(profile), **config, 'connected': connected,
                'endpoint': profile['endpoint'] if profile else '', 'platform': state.get('platform', '') if fresh else '',
                'message': message, 'job': job, 'key_saved': bool(profile and key.get('api_key') and key.get('endpoint') == profile['endpoint']),
                'can_create': sys.platform in ('darwin', 'win32'), 'host_platform': sys.platform,
                'managed_created': bool(profile and (self.root / ('managed-' + profile['id'] + '.json')).exists())}

    def save(self, profile, key=None):
        candidate = normalize_vm({'profile': profile})
        if not candidate['profile']:
            raise VMSetupError('Provide a VM connection profile to save.')
        # Validate secrets before mutating configuration. Never return a supplied secret.
        if key is not None and (not isinstance(key, str) or len(key) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in key)):
            raise VMSetupError('Enter a bridge key without spaces or control characters.')
        with self._editing():
            if self.config()['enabled']:
                raise VMSetupError('Turn VM integration off before changing the saved connection.')
            self._write_config(candidate)
            write_private(self.root / 'desired.json', {'connected': False})
            write_private(self.root / 'job.json', {})
            if key:
                write_private(self.credentials, {'endpoint': candidate['profile']['endpoint'], 'api_key': key})
        return self.status()

    def disable(self):
        with self._editing():
            value = self.config()
            self._write_config({**value, 'enabled': False})
            write_private(self.root / 'desired.json', {'connected': False})
        return self.status()

    def disconnect(self):
        with self._editing():
            write_private(self.root / 'desired.json', {'connected': False})
        return self.status()

    def open_window(self):
        profile = self.profile()
        if profile['mode'] != 'virtualbox':
            raise VMSetupError('Open this VM in its provider application.')
        from .vm_virtualbox import VirtualBoxVM
        VirtualBoxVM(self, profile).open()
        return self.status()

    def _spawn(self, action, token=''):
        self.root.mkdir(parents=True, exist_ok=True)
        args = [sys.executable, '-m', 'echopath.vm_setup', '--config', str(self.path), action]
        if token:
            args.extend(['--token', token])
        options = {'start_new_session': True} if os.name != 'nt' else {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
        if os.name == 'nt':
            options['env'] = {**os.environ, 'PYTHONUTF8': '1'}
        with (self.root / 'worker.log').open('ab') as log:
            os.chmod(self.root / 'worker.log', 0o600)
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options)
        # Reap our child without owning its lifetime; other processes read shared status.
        import threading
        threading.Thread(target=process.wait, daemon=True).start()
        return process.pid

    def start(self, action):
        if action not in ('test', 'connect', 'install', 'create'):
            raise VMSetupError('Unknown VM setup action.')
        with self._editing():
            profile = self.profile()
            if action == 'create' and not ((profile['mode'] == 'lima' and sys.platform == 'darwin') or
                                            (profile['mode'] == 'virtualbox' and sys.platform == 'win32')):
                raise VMSetupError('Use Lima on macOS or VirtualBox on Windows to create a desktop VM.')
            if action in ('install', 'create') and self.config()['enabled']:
                raise VMSetupError('Turn VM integration off before preparing the guest.')
            token = uuid.uuid4().hex
            write_private(self.root / 'job.json', {'token': token, 'running': True, 'action': action, 'message': 'Starting…', 'pid': os.getpid()})
            try:
                pid = self._spawn(action, token)
            except OSError:
                write_private(self.root / 'job.json', {'running': False, 'ok': False, 'message': 'Could not start the setup worker.'})
                raise VMSetupError('Could not start the setup worker.') from None
            write_private(self.root / 'job.json', {'token': token, 'running': True, 'action': action, 'message': 'Starting…', 'pid': pid})
        return self.status()

    def _progress(self, message):
        job = read_json(self.root / 'job.json')
        write_private(self.root / 'job.json', {**job, 'message': message})

    def ssh_command(self, profile, *, tunnel=False):
        command = [_executable('ssh'), '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                   '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', '-o', 'ForwardAgent=no',
                   '-o', 'ControlMaster=no', '-o', 'ControlPath=none', '-o', 'RequestTTY=no']
        if profile['mode'] == 'lima':
            marker = read_json(self.root / ('managed-' + profile['id'] + '.json'))
            config_file = marker.get('ssh_config')
            if not config_file or marker.get('instance') != profile['instance']:
                raise VMSetupError('Create this VM first.')
            command.extend(['-F', config_file])
            host = 'lima-' + profile['instance']
        else:
            ssh = profile['ssh']
            host = ssh['host']
            # Existing trust only; first-use confirmation belongs to the user's terminal.
            command.extend(['-o', 'StrictHostKeyChecking=yes'])
            if ssh['port']:
                command.extend(['-p', str(ssh['port'])])
            if ssh['user']:
                command.extend(['-l', ssh['user']])
            if ssh['identity_file']:
                key = Path(ssh['identity_file']).expanduser()
                if not key.is_file():
                    raise VMSetupError('The SSH key file was not found. Choose an existing key or use your SSH agent.')
                command.extend(['-i', str(key)])
        if tunnel:
            command.extend(['-N', '-o', 'ExitOnForwardFailure=yes', '-L',
                            f"127.0.0.1:{profile['local_port']}:127.0.0.1:{profile['bridge_port']}"])
        command.append(host)
        return command

    def inspect(self, profile):
        if profile['mode'] == 'virtualbox':
            from .vm_virtualbox import VirtualBoxVM
            return VirtualBoxVM(self, profile).inspect()
        if profile['mode'] == 'direct':
            return self.probe(profile)
        script = 'import json,os,sys; print(json.dumps({"platform":sys.platform,"python":list(sys.version_info[:2]),"home":os.path.expanduser("~")}))'
        import shlex
        output = _run(self.ssh_command(profile) + ['python3 -c ' + shlex.quote(script)], label='SSH guest check')
        try:
            guest = json.loads(output.strip().splitlines()[-1])
            supported = guest['platform'] in ('linux', 'darwin') and tuple(guest['python']) >= (3, 10)
        except (ValueError, KeyError, IndexError, TypeError):
            raise VMSetupError('The SSH guest check did not return a supported Python environment.') from None
        if not supported:
            raise VMSetupError('Automatic bridge installation needs a Linux/macOS guest with Python 3.10 or newer. Existing Windows bridges can use a direct URL.')
        return guest

    def probe(self, profile, *, desktop=True):
        from .amazon_vm_service import AmazonVMDesktopController
        from .config import load_global_model_router_config
        import httpx
        from PIL import Image
        try:
            with httpx.Client(timeout=4, follow_redirects=False, trust_env=False) as client:
                response = client.get(profile['endpoint'] + '/health')
                response.raise_for_status()
                health = response.json()
            if health.get('service') != 'amazon-vm-desktop-agent' or health.get('status') != 'ok':
                raise VMSetupError('The endpoint is not an EchoPath desktop bridge.')
            missing = missing_permissions(health)
            if isinstance(health.get('desktop_session'), dict) and not health['desktop_session'].get('ready'):
                raise VMSetupError('Sign in and unlock Windows in the VM window, then reconnect.')
            if missing:
                raise VMSetupError(
                    'The guest bridge needs ' + ' and '.join(missing) + ' in the guest\'s System Settings → Privacy & Security'
                    + (f" for {health['python']}" if isinstance(health.get('python'), str) else '')
                    + '. Grant it in the VM window, then restart the guest bridge.')
            config = load_global_model_router_config(self.path)
            controller = AmazonVMDesktopController(endpoint=profile['endpoint'], credential_file=self.credentials,
                                                    api_key_env=config.desktop.amazon_vm_api_key_env, timeout_seconds=8)
            if desktop:
                capture = controller._post_json('/observe', {})
                data = base64.b64decode(capture.get('image_base64', ''), validate=True)
                if not data or len(data) > 24 * 1024 * 1024:
                    raise ValueError()
                with Image.open(io.BytesIO(data)) as screen:
                    if screen.width < 1 or screen.height < 1:
                        raise ValueError()
                    screen.verify()
            else:
                size = controller.screen_size_sync()
                if size['width'] <= 0 or size['height'] <= 0:
                    raise ValueError()
            return {'platform': health.get('platform', ''), 'auth_required': health.get('auth_required', False)}
        except VMSetupError:
            raise
        except Exception:
            raise VMSetupError('Desktop not ready. Check the bridge key, logged-in graphical session and screen permissions; install the guest bridge if it is missing.') from None

    def restart_bridge(self, profile):
        """Restart the installed guest service, e.g. after a macOS permission was granted."""
        if profile['mode'] == 'virtualbox':
            from .vm_virtualbox import VirtualBoxVM
            return VirtualBoxVM(self, profile).restart_bridge()
        if profile['mode'] == 'direct':
            raise VMSetupError('Restart an externally managed bridge on the guest itself.')
        guest = self.inspect(profile)
        command = ('launchctl kickstart -k gui/$(id -u)/org.echopath.guest' if guest['platform'] == 'darwin'
                   else 'systemctl --user restart echopath-guest.service')
        _run(self.ssh_command(profile) + [command], timeout=60, label='Guest bridge restart')

    def ensure_supervisor(self, *, force=False):
        with self._lock():
            config = self.config()
            if not config['enabled'] or not config['profile']:
                return
            desired = read_json(self.root / 'desired.json')
            if not force and (desired.get('connected') is False or not config['profile']['auto_reconnect']):
                return
            if force:
                write_private(self.root / 'desired.json', {'connected': True})
            state = read_json(self.root / 'state.json')
            if state.get('fingerprint') == _fingerprint(config['profile']) and _alive(state.get('pid')) and time.time() - state.get('updated', 0) < 30:
                return
            self._spawn('supervise')

    def _connect(self, profile):
        if profile['mode'] == 'virtualbox':
            from .vm_virtualbox import VirtualBoxVM
            VirtualBoxVM(self, profile).open()
        if profile['mode'] != 'direct':
            self.inspect(profile)
        with config_lock(self.path):
            payload = read_json(self.path)
            payload['vm'] = {**self.config(), 'enabled': True}
            write_private(self.path, payload)
        self._progress('Connecting and checking the guest desktop…')
        self.ensure_supervisor(force=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = self.status()
            if state['connected']:
                return
            time.sleep(2)
            self.ensure_supervisor(force=True)
        raise VMSetupError(self.status()['message'] or 'The VM did not become ready.')

    def _install(self, profile):
        if profile['mode'] == 'virtualbox':
            from .vm_virtualbox import VirtualBoxVM
            return VirtualBoxVM(self, profile).install()
        if profile['mode'] == 'direct':
            raise VMSetupError('Guest installation needs an SSH connection.')
        self.inspect(profile)
        self._progress('Installing the guest bridge. Dependencies may take several minutes…')
        credential = read_json(self.credentials)
        key = credential.get('api_key') if credential.get('endpoint') == profile['endpoint'] else None
        from .config import load_global_model_router_config
        env_name = load_global_model_router_config(self.path).desktop.amazon_vm_api_key_env
        key = os.environ.get(env_name, '').strip() or key or secrets.token_urlsafe(32)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr('echopath_guest/__init__.py', '')
            for name in ('amazon_vm_agent.py', 'pyautogui_service.py'):
                bundle.writestr('echopath_guest/' + name, Path(__file__).with_name(name).read_bytes())
        payload = {'archive': base64.b64encode(archive.getvalue()).decode(), 'key': key, 'port': profile['bridge_port'],
                   'display': profile['display'], 'xauthority': profile['xauthority'], 'managed': profile['mode'] == 'lima'}
        import shlex
        script = Path(__file__).with_name('vm_guest_install.py').read_text()
        _run(self.ssh_command(profile) + ['python3 -c ' + shlex.quote(script)], input=json.dumps(payload), timeout=900, label='Guest bridge installation')
        write_private(self.credentials, {'endpoint': profile['endpoint'], 'api_key': key})

    def _create(self, profile):
        if profile['mode'] == 'virtualbox':
            from .vm_virtualbox import VirtualBoxVM
            return VirtualBoxVM(self, profile).create()
        if sys.platform != 'darwin' or profile['mode'] != 'lima':
            raise VMSetupError('Creating a VM requires macOS and Lima.')
        lima = _executable('limactl')
        marker_path = self.root / ('managed-' + profile['id'] + '.json')
        marker = read_json(marker_path)
        rows = [json.loads(line) for line in _run([lima, 'list', '--json'], label='Lima inventory').splitlines() if line.strip()]
        found = next((r for r in rows if r.get('name') == profile['instance']), None)
        if found and marker.get('instance') != profile['instance']:
            raise VMSetupError('A VM with this name already exists. Choose another name or connect to it through SSH.')
        if not found:
            self._progress('Creating an Ubuntu 24.04 VM. The OS image will be downloaded…')
            _run([lima, '--tty=false', 'create', '--name', profile['instance'], '--vm-type=vz', '--mount-none',
                  '--containerd=none', '--cpus', str(profile['cpus']), '--memory', str(profile['memory_gib']),
                  '--disk', str(profile['disk_gib']), '--set', '.portForwards = [{"guestPortRange": [1, 65535], "ignore": true}]',
                  'template:ubuntu-24.04'], timeout=900, label='Lima VM creation')
            write_private(marker_path, {'instance': profile['instance']})
        self._progress('Starting the VM…')
        _run([lima, '--tty=false', 'start', profile['instance']], timeout=900, label='Lima VM startup')
        ssh_config = _run([lima, 'list', '--format={{.SSHConfigFile}}', profile['instance']], label='Lima SSH configuration').strip()
        if not Path(ssh_config).is_file():
            raise VMSetupError('Lima did not provide its SSH configuration. Update Lima and retry.')
        write_private(marker_path, {'instance': profile['instance'], 'ssh_config': ssh_config})
        self._progress('Preparing the Linux desktop and browser…')
        command = self.ssh_command(profile)
        _run(command + ['sudo -n apt-get update -qq'], timeout=600, label='Guest package index')
        _run(command + ['sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3-venv python3-tk xvfb xauth x11-utils xfce4 xfce4-terminal dbus-x11 epiphany-browser scrot xclip ffmpeg'],
             timeout=1200, label='Guest desktop installation')
        _run(command + ['sudo -n loginctl enable-linger "$(id -un)"'], label='Guest service startup')
        self._install(profile)

    def job(self, action, token):
        # Wait for the parent to publish the worker PID before updating its record.
        with self._lock():
            job = read_json(self.root / 'job.json')
            if job.get('token') != token:
                return
        try:
            profile = self.profile()
            if action == 'test':
                self._progress('Checking VirtualBox guest access…' if profile['mode'] == 'virtualbox' else
                               'Checking SSH access…' if profile['mode'] != 'direct' else 'Checking the desktop bridge…')
                self.inspect(profile)
                message = ('VM and Guest Additions are ready. Install the bridge or connect.' if profile['mode'] == 'virtualbox' else
                           'SSH and Python are ready. Install the guest bridge, or connect if it is already installed.' if profile['mode'] != 'direct' else 'Guest desktop verified.')
            else:
                if action == 'create':
                    self._create(profile)
                elif action == 'install':
                    self._install(profile)
                self._connect(profile)
                message = 'VM connected. The guest desktop is ready.'
            ok = True
        except Exception as error:
            ok = False
            if not isinstance(error, VMSetupError):
                import logging
                logging.exception('VM setup operation failed')
            message = str(error) if isinstance(error, VMSetupError) else 'VM setup failed. Check the saved connection and guest prerequisites, then retry.'
        write_private(self.root / 'job.json', {**read_json(self.root / 'job.json'), 'running': False, 'ok': ok, 'message': message})

    def supervise(self):
        try:
            with desktop_lock(self.root, 'vm-supervisor'):
                self._supervise()
        except RuntimeError:
            return  # Another process already owns this connection.

    def _supervise(self):
        process = None
        profile = self.profile()
        fingerprint = _fingerprint(profile)
        platform = ''
        verified = False
        try:
            while True:
                config = self.config()
                if not config['enabled'] or _fingerprint(config['profile']) != fingerprint or read_json(self.root / 'desired.json').get('connected') is False:
                    return
                connected = False
                message = 'Connecting to the VM…'
                try:
                    if profile['mode'] not in ('direct', 'virtualbox') and (process is None or process.poll() is not None):
                        write_private(self.root / 'state.json', {'fingerprint': fingerprint, 'pid': os.getpid(),
                                      'updated': time.time(), 'connected': False, 'platform': platform, 'message': message})
                        # Never adopt an unrelated listener already using the saved local port.
                        with socket.socket() as listener:
                            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                            listener.bind(('127.0.0.1', profile['local_port']))
                        process = subprocess.Popen(self.ssh_command(profile, tunnel=True), stdin=subprocess.DEVNULL,
                                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        time.sleep(1)
                        if process.poll() is not None:
                            raise VMSetupError('SSH tunnel could not start. Test SSH access and check the local port.')
                        verified = False
                    health = self.probe(profile, desktop=not verified)
                    platform = health.get('platform', '')
                    verified = True
                    connected, message = True, 'Desktop ready.'
                except (OSError, ValueError) as error:
                    verified = False
                    message = str(error) if isinstance(error, VMSetupError) else 'The local tunnel port is unavailable. Disable VM integration and choose another local port.'
                write_private(self.root / 'state.json', {'fingerprint': fingerprint, 'pid': os.getpid(), 'updated': time.time(),
                              'connected': connected, 'platform': platform, 'message': message})
                if not connected and not profile['auto_reconnect']:
                    return
                for _ in range(10):
                    if read_json(self.root / 'desired.json').get('connected') is False:
                        return
                    time.sleep(0.5)
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            write_private(self.root / 'state.json', {'fingerprint': fingerprint, 'pid': 0, 'updated': time.time(),
                          'connected': False, 'message': 'Disconnected.'})


def main():
    parser = argparse.ArgumentParser(description='EchoPath VM connection worker.')
    parser.add_argument('--config', required=True)
    parser.add_argument('action', choices=('supervise', 'test', 'connect', 'install', 'create'))
    parser.add_argument('--token', default='')
    args = parser.parse_args()
    manager = VMSetup(args.config)
    if args.action == 'supervise':
        manager.supervise()
    else:
        manager.job(args.action, args.token)


if __name__ == '__main__':
    # Providers import VMSetupError from this module by its package name.
    # Run the canonical module so its error class also matches in job().
    from echopath.vm_setup import main as worker_main
    worker_main()
