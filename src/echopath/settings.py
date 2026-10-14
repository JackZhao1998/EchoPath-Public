"""Private Control Room settings and bounded, real bridge connection checks."""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .config import load_global_model_router_config, parse_global_model_router_config_payload
from .jev_client import JevError, _load_key, https_context
from .vm_setup import VMSetup, config_lock


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # A credential must only go to the requested OpenRouter URL.


def validate_openrouter_key(key):
    """Validate authentication without making a model request or exposing its response."""
    if not key:
        return {'valid': False, 'message': 'Add an OpenRouter token to enable JEV.'}
    if not re.fullmatch(r'[A-Za-z0-9._-]{20,512}', key):
        return {'valid': False, 'message': 'Enter an OpenRouter API token without spaces or line breaks.'}
    request = Request('https://openrouter.ai/api/v1/key', headers={'Authorization': 'Bearer ' + key})
    try:
        with build_opener(_NoRedirect(), HTTPSHandler(context=https_context())).open(request, timeout=10) as response:
            data = json.loads(response.read(65536)).get('data')
        if not isinstance(data, dict):
            raise ValueError()
        if data.get('is_management_key') or data.get('is_provisioning_key'):
            return {'valid': False, 'message': 'Use an inference API token, not a management token.'}
        remaining = data.get('limit_remaining')
        if isinstance(remaining, (int, float)) and remaining <= 0:
            return {'valid': False, 'message': 'This token has reached its spending limit. Update its limit in OpenRouter.'}
        return {'valid': True, 'message': 'OpenRouter token verified.'}
    except HTTPError as error:
        message = ('OpenRouter rejected this token. Check it or create a new one.' if error.code in (401, 403)
                   else 'OpenRouter could not validate the token right now. Try again shortly.')
    except (URLError, OSError, ValueError, TypeError, AttributeError):
        message = 'Could not reach OpenRouter or verify its response. Check your connection and try again.'
    return {'valid': False, 'message': message}


def _write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class Settings:
    def __init__(self, config_path):
        self.path = Path(config_path).resolve()
        self.key_path = self.path.parent / 'credentials' / 'api_keys.yaml'
        self.lock = threading.RLock()
        self.validation = None
        self.bridge_locks = {name: threading.Lock() for name in ('codex', 'claude')}
        self.vm = VMSetup(self.path)

    def _key(self):
        config = load_global_model_router_config(self.path)
        name = config.desktop.replay_decision_model_api_key_env
        value = os.environ.get(name, '').strip()
        if value:
            return value, 'environment', name
        try:
            return _load_key(self.key_path), 'file', name
        except JevError:
            return '', 'none', name

    def _checked(self, key):
        fingerprint = hashlib.sha256(key.encode()).hexdigest()
        if self.validation and self.validation[0] == fingerprint and time.monotonic() - self.validation[1] < 300:
            return self.validation[2]
        return None

    def _verify(self, key):
        result = validate_openrouter_key(key)
        self.validation = (hashlib.sha256(key.encode()).hexdigest(), time.monotonic(), result)
        return result

    def _set_desktop_option(self, name, enabled):
        with config_lock(self.path):
            payload = json.loads(self.path.read_text())
            payload.setdefault('desktop', {})[name] = enabled
            parse_global_model_router_config_payload(payload, source_path=self.path)
            _write_private(self.path, json.dumps(payload, indent=2) + '\n')

    def _set_jev(self, enabled):
        self._set_desktop_option('replay_decision_model', enabled)

    def recording_enabled(self):
        # Atomic config replacement lets status polling read without waiting for token checks.
        return load_global_model_router_config(self.path).desktop.vm_record_runs

    def set_recording(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError('Task recording must be enabled or disabled.')
        with self.lock:
            self._set_desktop_option('vm_record_runs', enabled)
            return {'ok': True, 'recording_enabled': enabled,
                    'message': 'Recording enabled for new VM tasks.' if enabled else 'Recording disabled for new VM tasks.'}

    def status(self):
        with self.lock:
            key, source, env_name = self._key()
            result = self._checked(key)
            enabled = load_global_model_router_config(self.path).desktop.replay_decision_model
            return {
                'openrouter': {'present': bool(key), 'source': source, 'env_name': env_name,
                               'valid': result['valid'] if result else None,
                               'message': result['message'] if result else ('Token saved. Validate it to enable JEV.' if key else 'No OpenRouter token configured.')},
                'jev_enabled': bool(enabled and key and (result is None or result['valid'])),
                'recording_enabled': self.recording_enabled(),
                'vm': self.vm.status(),
            }

    def test_openrouter(self):
        with self.lock:
            key, _, _ = self._key()
            result = self._verify(key)
            if not result['valid']:
                self._set_jev(False)
            return {'ok': result['valid'], 'message': result['message'], 'settings': self.status()}

    def save_token(self, token):
        if not isinstance(token, str):
            raise ValueError('Enter an OpenRouter API token.')
        token = token.strip()
        with self.lock:
            _, source, name = self._key()
            if source == 'environment':
                raise ValueError(f'The active token is managed by {name}. Update that environment variable and restart EchoPath.')
            result = validate_openrouter_key(token)
            if not result['valid']:
                return {'ok': False, 'message': result['message'], 'settings': self.status()}
            original = self.key_path.read_text() if self.key_path.exists() else ''
            # Preserve other providers and comments in the shared credential file.
            lines = [line for line in original.splitlines() if not re.match(r'^\s*openrouter\s*:', line)]
            lines.append('openrouter: ' + token)
            _write_private(self.key_path, '\n'.join(lines) + '\n')
            self.validation = (hashlib.sha256(token.encode()).hexdigest(), time.monotonic(), result)
            return {'ok': True, 'message': 'Token verified and saved.', 'settings': self.status()}

    def remove_token(self):
        with self.lock:
            if self._key()[1] == 'environment':
                raise ValueError('Remove the OpenRouter environment variable and restart EchoPath to clear this token.')
            self._set_jev(False)
            if self.key_path.exists():
                lines = [line for line in self.key_path.read_text().splitlines() if not re.match(r'^\s*openrouter\s*:', line)]
                _write_private(self.key_path, '\n'.join(lines) + '\n')
            self.validation = None
            return {'ok': True, 'message': 'Saved token removed. JEV is disabled.', 'settings': self.status()}

    def set_jev(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError('JEV must be enabled or disabled.')
        with self.lock:
            if enabled:
                key, _, _ = self._key()
                result = self._checked(key) or self._verify(key)
                if not result['valid']:
                    self._set_jev(False)
                    return {'ok': False, 'message': result['message'], 'settings': self.status()}
            self._set_jev(enabled)
            return {'ok': True, 'message': 'JEV enabled for new tasks.' if enabled else 'JEV disabled for new tasks.', 'settings': self.status()}

    def test_bridge(self, name):
        if not isinstance(name, str) or name not in self.bridge_locks:
            raise ValueError('Choose Codex or Claude Code.')
        lock = self.bridge_locks[name]
        if not lock.acquire(blocking=False):
            raise ValueError('This bridge test is already running.')
        try:
            return self._test_bridge(name)
        finally:
            lock.release()

    def _test_bridge(self, name):
        config = load_global_model_router_config(self.path)
        slug = 'codex_cli' if name == 'codex' else 'claude_code'
        script = Path(__file__).resolve().parents[2] / 'bridges' / slug / ('run_' + slug + '.py')
        if not script.is_file():
            return {'ok': False, 'message': 'The bundled bridge script is missing. Reinstall EchoPath from its checkout.'}
        command = [sys.executable, str(script)]
        active = any(Path(part).name == script.name for part in config.external_command.command)
        if active and config.mode == 'external_command':
            command = list(config.external_command.command)
        environment = {**os.environ, **config.external_command.env}
        request = {'model': config.external_model if active else 'default',
                   'instructions': 'Connection check only. Do not use tools, read files, or perform any actions.',
                   'input': 'Reply with exactly ECHOPATH_BRIDGE_OK.',
                   'extra_body': {'working_directory': str(config.external_command.working_directory), 'timeout_seconds': 45}}
        start = time.monotonic()
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding='utf-8', errors='replace', env=environment,
                                       cwd=config.external_command.working_directory, start_new_session=os.name != 'nt')
            try:
                stdout, stderr = process.communicate(json.dumps(request), timeout=50)
            except subprocess.TimeoutExpired:
                if os.name != 'nt':
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.communicate()
                return {'ok': False, 'message': 'Bridge test timed out. Check the CLI login and network connection.'}
        except OSError:
            return {'ok': False, 'message': 'Could not start the bridge. Check its executable and working directory.'}
        try:
            response = json.loads(stdout)
            ok = process.returncode == 0 and response.get('output_text', '').strip() == 'ECHOPATH_BRIDGE_OK'
        except (ValueError, AttributeError):
            ok = False
        if ok:
            return {'ok': True, 'message': f'Connected. Bridge replied successfully in {time.monotonic() - start:.1f}s.'}
        # Never return raw CLI output: it can contain credentials or local files.
        diagnostic = (stderr + '\n' + stdout).lower()
        label = 'codex' if name == 'codex' else 'claude'
        if any(word in diagnostic for word in ('not found', 'no such file', 'enoent')):
            message = f'{label} could not be found. Install the CLI or configure its executable path.'
        elif any(word in diagnostic for word in ('unauthorized', 'authentication', 'not logged', 'login', 'log in', '401')):
            message = f'Authentication failed. Sign in with {label} in Terminal, then test again.'
        elif any(word in diagnostic for word in ('quota', 'credit', 'rate limit', 'usage limit', '429')):
            message = 'The bridge reached the provider, but its usage or rate limit blocked the test.'
        elif 'model' in diagnostic and any(word in diagnostic for word in ('unavailable', 'not exist', 'not support', 'invalid')):
            message = 'The configured model is unavailable. Check the model selected for this bridge.'
        else:
            message = f'The bridge did not return the expected reply. Check {label} login, model access, and connectivity.'
        return {'ok': False, 'message': message}
