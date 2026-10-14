"""Loopback-only task supervisor shared with the menu-bar app."""
from __future__ import annotations

import argparse
import base64
import binascii
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .consolidated_memory_library import ConsolidatedMemoryLibrary
from .run_state import ACTIVE, RunRegistry, input_lock
from .vm_reset import VMReset
from .settings import Settings


class TaskRunner:
    def __init__(self, config_path: Path):
        self.config_path = Path(config_path).resolve()
        self.registry = RunRegistry(self.config_path)
        self.settings = Settings(self.config_path)
        self.lock = threading.RLock()
        self.workers = {}
        self.launch_errors = []
        self.settings.vm.ensure_supervisor()
        self.vm = self._discover_vm()
        self._procedures = []
        self._procedures_updated = 0.0
        self._frame_lock = threading.Lock()
        self._frame = None
        # Set by the native tray/menu-bar app. On macOS, a process whose
        # executable merely happens to live inside EchoPath.app is not enough
        # for macOS TCC to treat it as that authorized app.
        self._local_app_host = False
        if VMReset.configured(self.config_path):
            threading.Thread(target=self._recover_vm, daemon=True).start()

    def enable_local_app_host(self):
        """Mark this Control Room as the native app entry point."""
        self._local_app_host = True

    def local_desktop_authorization(self):
        """State used by MCP before it submits a local GUI task to this app."""
        if sys.platform == 'win32':
            from .amazon_vm_agent import desktop_session_status
            session = desktop_session_status()
            return {'app_host': self._local_app_host, 'ready': bool(self._local_app_host and session and session['ready']),
                    'detail': 'Windows desktop ready.' if session and session['ready'] else 'Sign in and unlock Windows.',
                    'config_path': str(self.config_path)}
        if not self._local_app_host:
            return {
                'app_host': False,
                'accessibility': False,
                'screen_recording': False,
                'ready': False,
                'detail': 'This Control Room was not started by EchoPath.app.',
                'config_path': str(self.config_path),
            }
        from .macos_permissions import local_desktop_control_status
        permissions = local_desktop_control_status()
        ready = all(permissions.values())
        return {
            'app_host': True,
            **permissions,
            'ready': ready,
            'detail': (
                'EchoPath.app is authorized for local desktop control.'
                if ready
                else 'Enable Accessibility and Screen Recording for EchoPath.app, then reopen it.'
            ),
            'config_path': str(self.config_path),
        }

    def _recover_vm(self, target=None):
        manager = VMReset.configured(self.config_path, target)
        if manager:
            try:
                manager.recover()
            except (OSError, RuntimeError, ValueError):
                pass  # A live worker owns the lock, or the persistent journal reports failure.

    def reset_vm(self, endpoint):
        manager = VMReset.configured(self.config_path, endpoint)
        if manager is None or manager.state()['phase'] != 'error':
            raise ValueError('This VM has no failed reset to retry.')
        threading.Thread(target=self._recover_vm, args=(endpoint,), daemon=True).start()

    def vm_input_status(self, endpoint):
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError('Choose a VM desktop bridge to control.')
        endpoint = endpoint.strip().rstrip('/')
        validate_endpoint(endpoint)
        platform = self.vm.get('platform', '') if self.vm.get('endpoint') == endpoint else ''
        try:
            self.settings.vm.guard(endpoint)
        except ValueError as error:
            return {'allowed': False, 'detail': str(error), 'platform': platform}
        manager = VMReset.configured(self.config_path, endpoint)
        if manager:
            state = manager.state()
            if state['phase'] not in ('ready', 'in_use'):
                return {'allowed': False, 'detail': state['detail'], 'platform': platform}
        history = [r for r in self.registry.list() if r['target'].rstrip('/') == endpoint]
        runs = [r for r in history if r['status'] in ACTIVE]
        if runs:
            run = runs[0]
            allowed = all(r['status'] == 'paused' and r['desired'] == 'paused' for r in runs)
            return {'allowed': allowed, 'run_id': run['id'], 'run_status': run['status'], 'platform': platform,
                    'task': {'title': run['task'], 'status': run['status'], 'detail': run['detail']},
                    'detail': 'Task paused. You can control the VM.' if allowed else 'Pause the task to control the VM.'}
        return {'allowed': True, 'detail': 'Click the desktop to focus keyboard input.',
                'task': ({'title': history[0]['task'], 'status': history[0]['status'], 'detail': history[0]['detail']}
                         if history else None),
                'platform': platform}

    def vm_input(self, endpoint, value):
        from .amazon_vm_service import AmazonVMDesktopController
        from .config import load_global_model_router_config
        from .vm_input import send_input, validate_input
        action = validate_input(value)
        state = self.vm_input_status(endpoint)
        if not state['allowed']:
            raise ValueError(state['detail'])
        endpoint = endpoint.strip().rstrip('/')
        with self.registry.manual_input(endpoint):
            # Reset or task ownership may have changed while waiting for an input.
            state = self.vm_input_status(endpoint)
            if not state['allowed']:
                raise ValueError(state['detail'])
            config = load_global_model_router_config(self.config_path)
            controller = AmazonVMDesktopController(
                endpoint=endpoint, api_key_env=config.desktop.amazon_vm_api_key_env,
                credential_file=self.config_path.parent / 'credentials' / 'amazon_vm.json', timeout_seconds=8,
            )
            send_input(controller, action)
            self._frame = None
        return {'ok': True}

    def vm_frame(self, endpoint):
        """Read the guest screen without acting; share one recent frame across viewers."""
        from PIL import Image
        from .amazon_vm_service import AmazonVMDesktopController
        from .config import load_global_model_router_config

        if not isinstance(endpoint, str):
            raise ValueError('Choose a VM desktop bridge to view.')
        endpoint = endpoint.strip().rstrip('/')
        validate_endpoint(endpoint)
        self.settings.vm.guard(endpoint)
        manager = VMReset.configured(self.config_path, endpoint)
        if manager and manager.state()['phase'] in ('resetting', 'error'):
            raise ValueError(manager.state()['detail'])
        with self._frame_lock:
            if self._frame and self._frame[0] == endpoint and time.monotonic() - self._frame[1] < 0.8:
                return self._frame[2:]
            config = load_global_model_router_config(self.config_path)
            controller = AmazonVMDesktopController(
                endpoint=endpoint, api_key_env=config.desktop.amazon_vm_api_key_env,
                credential_file=self.config_path.parent / 'credentials' / 'amazon_vm.json',
                timeout_seconds=8,
            )
            response = controller._post_json('/observe', {})
            encoded = response.get('image_base64')
            if not isinstance(encoded, str) or not encoded or len(encoded) > 32 * 1024 * 1024:
                raise ValueError('The VM returned no usable desktop image.')
            try:
                frame = base64.b64decode(encoded, validate=True)
                with Image.open(io.BytesIO(frame)) as image:
                    if image.format not in ('PNG', 'JPEG'):
                        raise ValueError('Unsupported VM image format.')
                    content_type = Image.MIME[image.format]
                    image.verify()
            except (binascii.Error, OSError, ValueError):
                raise ValueError('The VM returned an invalid desktop image.') from None
            self._frame = (endpoint, time.monotonic(), frame, content_type)
            return frame, content_type

    def _discover_vm(self):
        """Expose the conventional tunnel only when it is an EchoPath bridge."""
        if self.settings.vm.has_settings():
            state = self.settings.vm.status()
            return {key: state[key] for key in ('endpoint', 'connected', 'platform', 'enabled', 'configured', 'message')}
        endpoint = 'http://127.0.0.1:8790'
        try:
            return {'endpoint': endpoint, 'connected': True, **vm_health(endpoint, self.config_path)}
        except (OSError, ValueError) as error:
            return {'endpoint': endpoint, 'connected': False, 'error': str(error)}

    def snapshot(self):
        with self.lock:
            pending = [target for target, proc in self.workers.items() if proc.poll() is None]
            runs = self.registry.list()
            for run in runs:
                run['recording'] = self.recording(run['id']) is not None
            if self.settings.vm.has_settings():
                self.vm = self._discover_vm()
            vm = dict(self.vm)
            manager = VMReset.configured(self.config_path)
            if manager:
                vm['reset'] = manager.status()
            return {'runs': runs, 'workers': pending, 'launch_errors': self.launch_errors[-5:], 'vm': vm,
                    'procedures': self.procedures(), 'recording_enabled': self.settings.recording_enabled(),
                    'recording_supported': sys.platform == 'darwin'}

    def recording(self, run_id):
        """The run's screen recording (<recording dir>/<run id>.mp4), if one was saved."""
        try:
            from .config import load_global_model_router_config
            from .run_recording import recording_path
            path = recording_path(load_global_model_router_config(self.config_path), self.config_path, str(run_id))
        except Exception:
            path = self.config_path.parent / 'local_data' / 'recordings' / f'{run_id}.mp4'
        return path if path.name == f'{run_id}.mp4' and path.is_file() else None

    def procedures(self):
        if time.time() - self._procedures_updated < 3:
            return list(self._procedures)
        from .mcp.service import EchoPathMCPService
        memory = EchoPathMCPService.from_config_path(self.config_path).list_memories(limit=24)
        records = []
        for source_key in ('memory_repo', 'consolidated_library'):
            for item in memory.get(source_key, []):
                if not isinstance(item, dict):
                    continue
                records.append({
                    'id': str(item.get('path_id') or item.get('memory_id') or ''),
                    'store': str(item.get('store') or source_key),
                    'status': str(item.get('status') or 'active'),
                    'title': str(item.get('task_summary') or item.get('purpose_summary') or item.get('task_intent') or 'Stored procedure'),
                    'steps': int(item.get('step_count') or item.get('action_count') or item.get('operation_count') or 0),
                    'updated_at': str(item.get('updated_at') or item.get('created_at') or ''),
                    'replayable': bool(item.get('replayable', item.get('status') == 'active')),
                })
        self._procedures, self._procedures_updated = records, time.time()
        return list(records)

    def procedure(self, store, procedure_id):
        """Full steps for one stored procedure; record paths and captured output are never returned."""
        root = self.config_path.parent
        if store == 'consolidated-library':
            directory = root / 'memory' / 'consolidated'
            records = ConsolidatedMemoryLibrary(root_directory=directory).iter_records() if (directory / 'records').is_dir() else []
            for record in records:
                if record.get('memory_id') == procedure_id:
                    generalization = record.get('generalization') if isinstance(record.get('generalization'), dict) else {}
                    operations = record.get('consolidated_operations')
                    keys = [str(k) for k in record.get('match_keys') or []]
                    return {
                        'id': procedure_id, 'store': store, 'status': str(record.get('status') or ''),
                        'title': str(generalization.get('name') or record.get('purpose_summary') or procedure_id),
                        'summary': str(record.get('purpose_summary') or ''),
                        'query': keys[0] if keys else str(record.get('replay_key') or ''),
                        'replayable': str(record.get('status')) == 'active' and bool(record.get('replayable')),
                        'platform': str(record.get('platform') or ''), 'tags': [str(t) for t in record.get('tags') or []][:12],
                        'updated_at': str(record.get('updated_at') or ''),
                        'parameters': [{'name': str(p.get('name', '')), 'value': str(p.get('canonical_value', ''))}
                                       for p in generalization.get('parameters', []) if isinstance(p, dict)],
                        'steps': [describe_operation(o) for o in operations if isinstance(o, dict)] if isinstance(operations, list) else [],
                    }
        elif store == 'memory-repo':
            from .memory_repo import MemoryRepository, MemoryRepositoryError
            repository = MemoryRepository(root_directory=root / 'local_data' / 'memory_repo')
            try:
                record = repository.load_path(procedure_id)
            except MemoryRepositoryError:
                raise ValueError('Stored procedure not found.') from None
            steps = []
            for node_id in record.get('node_ids') or []:
                try:
                    node = repository.load_node(node_id)
                except MemoryRepositoryError:
                    continue
                action = node.get('action') if isinstance(node.get('action'), dict) else {}
                step = describe_operation(action)
                step['expected'] = str(node.get('postcondition') or '')[:240] if isinstance(node.get('postcondition'), str) else ''
                steps.append(step)
            keys = [str(k) for k in record.get('query_keys') or []]
            return {
                'id': procedure_id, 'store': store, 'status': str(record.get('status') or ''),
                'title': str(record.get('task_intent') or procedure_id), 'summary': '',
                'query': str(record.get('task_intent') or (keys[0] if keys else '')),
                'replayable': str(record.get('status')) == 'active',
                'platform': str(record.get('platform') or ''), 'tags': [str(record['application'])] if record.get('application') else [],
                'updated_at': str(record.get('updated_at') or ''),
                'parameters': [{'name': str(i.get('input_kind') or i.get('action') or 'input'), 'value': str(i.get('description') or i.get('original_value') or '')}
                               for i in record.get('flexible_action_inputs') or [] if isinstance(i, dict)],
                'steps': steps,
            }
        raise ValueError('Stored procedure not found.')

    def start(self, task, desktop, endpoint='', native_handoff=False):
        if not isinstance(task, str) or not task.strip() or len(task) > 16000:
            raise ValueError('Enter a task of up to 16,000 characters.')
        if not isinstance(desktop, bool) or not isinstance(endpoint, str) or not isinstance(native_handoff, bool):
            raise ValueError('Invalid task options.')
        if native_handoff and not desktop:
            raise ValueError('Allow desktop control to hand native segments to the host.')
        if endpoint:
            validate_endpoint(endpoint)
            self.settings.vm.guard(endpoint)
            if not desktop:
                raise ValueError('Allow desktop control to run a VM task.')
            manager = VMReset.configured(self.config_path, endpoint)
            if manager and manager.state()['phase'] not in ('ready', 'in_use'):
                raise ValueError(manager.state()['detail'])
            self.vm = {'endpoint': endpoint.rstrip('/'), 'connected': True, **vm_health(endpoint, self.config_path)}
        target = endpoint.rstrip('/') or 'local'
        with self.lock, input_lock(self.registry.root, target):
            if endpoint:
                self.settings.vm.guard(endpoint)
            if any(r['target'] == target and r['status'] in ACTIVE for r in self.registry.list()):
                raise ValueError('This desktop already has an active or paused run.')
            if target in self.workers and self.workers[target].poll() is None:
                raise ValueError('A worker is already starting for this desktop.')
            run_id = self.registry.create(task.strip(), target)
            command = [sys.executable, '-m', 'echopath.cli', '--config', str(self.config_path), '--progress-json', '--run-id', run_id]
            if not desktop:
                command.append('--no-desktop-control')
            if native_handoff:
                command.append('--native-handoff')
            if endpoint:
                command.extend(['--vm-endpoint', endpoint])
            else:
                command.append('--local-desktop')
            command.extend(['--', task])
            # File output cannot fill an unread pipe and deadlock the worker.
            log = self.registry.root / ('worker-' + uuid.uuid4().hex + '.log')
            try:
                with log.open('w') as output:
                    options = ({'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                               if os.name == 'nt' else {'start_new_session': True})
                    if os.name == 'nt':
                        options['env'] = {**os.environ, 'PYTHONUTF8': '1'}
                    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=output, **options)
            except OSError as error:
                self.registry.set_error(run_id, f'Could not start worker: {error}')
                raise
            self.workers[target] = proc
            threading.Thread(target=self._reap, args=(target, proc, log), daemon=True).start()
            return self.registry.get(run_id)

    def _reap(self, target, proc, log):
        code = proc.wait()
        self._recover_vm(target)
        with self.lock:
            if self.workers.get(target) is proc:
                del self.workers[target]
            if code not in (0, 130):
                # A worker that registered its run already reports failure there;
                # only a worker that died before that is a launch error.
                if any(r['pid'] == proc.pid for r in self.registry.list()):
                    return
                with log.open('rb') as stream:
                    start = max(0, log.stat().st_size - 3000)
                    stream.seek(start)
                    detail = worker_error_summary(stream.read().decode(errors='replace'), partial=start > 0)
                self.launch_errors.append({'detail': detail, 'time': time.time()})


def worker_error_summary(tail, partial=False):
    """The readable end of a worker log: progress JSON lines dropped, last lines kept."""
    lines = tail.splitlines()[1 if partial else 0:]
    lines = [line for line in lines if line.strip() and not line.lstrip().startswith('{')]
    return '\n'.join(lines[-12:]) or 'The worker exited before starting the task.'


def describe_operation(op):
    """One readable line per stored step; results and captured output stay on disk."""
    action = str(op.get('action') or ('shell' if 'command' in op else 'step'))
    if 'keys' in op and isinstance(op['keys'], list):
        detail = ' + '.join(str(k) for k in op['keys'])
    elif 'key' in op:
        detail = str(op['key'])
    elif 'text' in op:
        detail = '“' + str(op['text']) + '”'
    elif 'x' in op and 'y' in op:
        detail = f"at {op['x']}, {op['y']}" + (f" ×{op['clicks']}" if op.get('clicks', 1) not in (0, 1) else '')
    elif 'seconds' in op:
        detail = f"{op['seconds']} s"
    elif 'command' in op:
        detail = str(op['command'])
    else:
        detail = ', '.join(f'{k}={v}' for k, v in op.items()
                           if k not in ('action', 'expected_outcome') and isinstance(v, (str, int, float, bool)))
    return {'action': action, 'detail': detail[:240], 'expected': str(op.get('expected_outcome') or '')[:240]}


def validate_endpoint(endpoint):
    url = urlsplit(endpoint)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('Use an HTTP(S) VM bridge URL without credentials, query or fragment.')


def vm_health(endpoint, config_path=None):
    import httpx
    from .amazon_vm_service import _shared_http_client

    validate_endpoint(endpoint)
    try:
        with _shared_http_client().stream('GET', endpoint.rstrip('/') + '/health',
                                         timeout=httpx.Timeout(4, connect=2)) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 65536:
                    raise ValueError('The VM health response is too large.')
            value = json.loads(body)
    except httpx.HTTPError as error:
        raise ValueError(f'Could not reach the VM desktop bridge: {error}') from None
    if value.get('service') != 'amazon-vm-desktop-agent' or value.get('status') != 'ok':
        raise ValueError('This URL is not an EchoPath desktop bridge.')
    if config_path is not None and value.get('auth_required'):
        from .amazon_vm_service import AmazonVMDesktopController, AmazonVMDesktopError
        from .config import load_global_model_router_config
        config_path = Path(config_path).resolve()
        config = load_global_model_router_config(config_path)
        controller = AmazonVMDesktopController(
            endpoint=endpoint, api_key_env=config.desktop.amazon_vm_api_key_env,
            credential_file=config_path.parent / 'credentials' / 'amazon_vm.json',
            timeout_seconds=4,
        )
        try:
            controller.screen_size_sync()
        except AmazonVMDesktopError as error:
            raise ValueError(f'VM authentication check failed: {error}') from None
        value['authenticated'] = True
    return value


def build_handler(runner):
    class Handler(BaseHTTPRequestHandler):
        # The live viewer polls continuously. Reuse browser connections instead
        # of consuming a new loopback port for every frame and status check.
        protocol_version = 'HTTP/1.1'

        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, *_):
            pass

        def respond(self, value, code=200, content_type='application/json'):
            body = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            if code >= 400:
                self.send_header('Connection', 'close')
                self.close_connection = True
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' blob:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def send_file(self, path, content_type):
            """Serve a file with byte ranges, which video players need to seek."""
            size = path.stat().st_size
            start, end = 0, size - 1
            match = re.match(r'bytes=(\d*)-(\d*)$', self.headers.get('Range', '') or '')
            if match and (match.group(1) or match.group(2)):
                if match.group(1):
                    start = int(match.group(1))
                    end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
                else:
                    start = max(0, size - int(match.group(2)))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
            self.send_response(206 if match and (match.group(1) or match.group(2)) else 200)
            self.send_header('Content-Type', content_type)
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(end - start + 1))
            if match and (match.group(1) or match.group(2)):
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            with path.open('rb') as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = stream.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def local_request(self):
            allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            host = self.headers.get('Host', '')
            origin = self.headers.get('Origin')
            return host in allowed and (origin is None or origin == 'http://' + host)

        def do_GET(self):
            if not self.local_request():
                return self.respond({'error': 'Local requests only.'}, 403)
            if self.path == '/api/status':
                return self.respond(runner.snapshot())
            if self.path == '/api/local-desktop-authorization':
                return self.respond(runner.local_desktop_authorization())
            if self.path == '/api/config':
                return self.respond(runner.settings.status())
            if self.path == '/api/config/vm':
                return self.respond(runner.settings.vm.status())
            if self.path == '/api/tasks':
                return self.respond({'tasks': runner.registry.list()})
            if self.path == '/api/procedures':
                return self.respond({'procedures': runner.procedures()})
            if self.path.startswith('/api/tasks/'):
                parts = self.path.strip('/').split('/')
                if len(parts) == 3:
                    return self.respond({'task': runner.registry.get(parts[2])})
            if self.path.startswith('/api/runs/') and self.path.endswith('/recording'):
                parts = self.path.strip('/').split('/')
                path = runner.recording(unquote(parts[2])) if len(parts) == 4 else None
                if path is None:
                    return self.respond({'error': 'No recording for this run.'}, 404)
                return self.send_file(path, 'video/mp4')
            if self.path.startswith('/api/runs/') and self.path.endswith('/trace'):
                parts = self.path.strip('/').split('/')
                if len(parts) == 4:
                    try:
                        return self.respond({'trace': runner.registry.trace(unquote(parts[2]))})
                    except ValueError as error:
                        return self.respond({'error': str(error)}, 404)
            if self.path.startswith('/api/procedures/'):
                parts = [unquote(p) for p in self.path.split('?')[0].strip('/').split('/')]
                if len(parts) == 4:
                    try:
                        return self.respond({'procedure': runner.procedure(parts[2], parts[3])})
                    except ValueError as error:
                        return self.respond({'error': str(error)}, 404)
            html, js, css = 'text/html; charset=utf-8', 'text/javascript', 'text/css'
            assets = {'/': ('dashboard.html', html), '/tray': ('tray.html', html), '/common.js': ('common.js', js),
                      '/dashboard.js': ('dashboard.js', js), '/tray.js': ('tray.js', js), '/dashboard.css': ('dashboard.css', css),
                      '/vm': ('vm-viewer.html', html), '/vm-viewer.js': ('vm-viewer.js', js),
                      '/config': ('config.html', html), '/config.js': ('config.js', js)}
            asset_path = urlsplit(self.path).path
            if asset_path in assets:
                name, mime = assets[asset_path]
                return self.respond((Path(__file__).parent / 'web' / name).read_bytes(), content_type=mime)
            self.respond({'error': 'Not found'}, 404)

        def do_POST(self):
            if not self.local_request() or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.respond({'error': 'Local JSON requests only.'}, 403)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 32768:
                    raise ValueError('Invalid request size.')
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError('Expected a JSON object.')
                if self.path == '/api/run':
                    runner.start(payload.get('task'), payload.get('desktop', False), payload.get('endpoint', ''))
                elif self.path == '/api/config/vm/save':
                    return self.respond({'ok': True, 'vm': runner.settings.vm.save(payload.get('profile'), payload.get('key'))})
                elif self.path == '/api/config/vm/enabled':
                    enabled = payload.get('enabled')
                    if not isinstance(enabled, bool):
                        raise ValueError('VM integration must be enabled or disabled.')
                    state = runner.settings.vm.start('connect') if enabled else runner.settings.vm.disable()
                    return self.respond({'ok': True, 'vm': state}, 202 if enabled else 200)
                elif self.path == '/api/config/vm/action':
                    action = payload.get('action')
                    state = (runner.settings.vm.disconnect() if action == 'disconnect' else
                             runner.settings.vm.open_window() if action == 'open-window' else runner.settings.vm.start(action))
                    return self.respond({'ok': True, 'vm': state}, 200 if action in ('disconnect', 'open-window') else 202)
                elif self.path == '/api/config/openrouter/save':
                    return self.respond(runner.settings.save_token(payload.get('token')))
                elif self.path == '/api/config/openrouter/test':
                    return self.respond(runner.settings.test_openrouter())
                elif self.path == '/api/config/openrouter/remove':
                    return self.respond(runner.settings.remove_token())
                elif self.path == '/api/config/jev':
                    return self.respond(runner.settings.set_jev(payload.get('enabled')))
                elif self.path == '/api/config/recording':
                    return self.respond(runner.settings.set_recording(payload.get('enabled')))
                elif self.path == '/api/config/bridge-test':
                    return self.respond(runner.settings.test_bridge(payload.get('bridge')))
                elif self.path == '/api/tasks':
                    task = runner.start(payload.get('task'), payload.get('desktop', False), payload.get('endpoint', ''),
                                        payload.get('native_handoff', False))
                    return self.respond({'task': task}, 202)
                elif self.path.startswith('/api/tasks/'):
                    parts = self.path.strip('/').split('/')
                    if len(parts) != 4 or parts[3] not in ('pause', 'resume', 'stop'):
                        return self.respond({'error': 'Not found'}, 404)
                    runner.registry.request(parts[2], parts[3])
                    return self.respond({'task': runner.registry.get(parts[2])})
                elif self.path == '/api/control':
                    runner.registry.request(payload.get('id'), payload.get('action'))
                elif self.path == '/api/vm-health':
                    runner.settings.vm.guard(str(payload.get('endpoint', '')))
                    result = vm_health(str(payload.get('endpoint', '')), runner.config_path)
                    runner.vm = {'endpoint': str(payload.get('endpoint', '')).rstrip('/'), 'connected': True, **result}
                    return self.respond(runner.vm)
                elif self.path == '/api/vm-frame':
                    frame, content_type = runner.vm_frame(payload.get('endpoint'))
                    return self.respond(frame, content_type=content_type)
                elif self.path == '/api/vm-input-status':
                    return self.respond(runner.vm_input_status(payload.get('endpoint')))
                elif self.path == '/api/vm-input':
                    return self.respond(runner.vm_input(payload.get('endpoint'), payload.get('input')))
                elif self.path == '/api/vm-reset':
                    runner.reset_vm(str(payload.get('endpoint', '')))
                else:
                    return self.respond({'error': 'Not found'}, 404)
                self.respond(runner.snapshot())
            except (ValueError, OSError, RuntimeError) as error:
                self.respond({'error': str(error)}, 409)
    return Handler


def serve_ipv6_loopback(port, handler):
    """Allow localhost browsers to use IPv6 without exposing a network interface."""
    class IPv6Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6
    try:
        server = IPv6Server(('::1', port), handler)
    except OSError:
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description='EchoPath Control Room and desktop tray app.')
    parser.add_argument('--config', default=str(Path.cwd() / 'echopath.local.json'))
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-open', action='store_true')
    parser.add_argument('--tray', action='store_true', help='Show status and controls in the macOS menu bar or Windows system tray.')
    args = parser.parse_args(argv)
    path = Path(args.config).expanduser().resolve()
    if not path.is_file():
        parser.error(f'Config file not found: {path}')
    runner = TaskRunner(path)
    try:
        server = ThreadingHTTPServer(('127.0.0.1', args.port), build_handler(runner))
    except OSError as error:
        parser.error(f'Cannot start Control Room: {error}. An instance may already be running at http://127.0.0.1:{args.port}.')
    ipv6 = serve_ipv6_loopback(server.server_port, build_handler(runner))
    url = f'http://{"localhost" if ipv6 else "127.0.0.1"}:{server.server_port}'
    print(f'Control Room: {url}\nWorkers remain active if this window closes. Reopen to pause, resume or stop them.', flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        if args.tray:
            from .tray import run_tray
            threading.Thread(target=server.serve_forever, daemon=True).start()
            run_tray(runner, url)
            server.shutdown()
        else:
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if ipv6:
            ipv6.shutdown()
            ipv6.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
