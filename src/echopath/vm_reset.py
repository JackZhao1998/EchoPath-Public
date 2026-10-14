"""Restore a configured Lima VZ guest from an offline, copy-on-write baseline.

The policy, baseline and recovery journal live in ignored local_data/vm-reset.
Only the explicitly bound endpoint is reset. Host mounts are never rolled back.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, indent=2) + '\n')
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _clone(source, destination):
    """Never fall back to copying an 80 GB sparse disk on a nearly full host."""
    if sys.platform != 'darwin':
        raise RuntimeError('Lima VZ baselines require macOS and an APFS volume.')
    libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    clone = libc.clonefile
    clone.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int)
    clone.restype = ctypes.c_int
    if clone(os.fsencode(source), os.fsencode(destination), 0):
        code = ctypes.get_errno()
        raise OSError(code, 'Could not clone the VM disk on this filesystem: ' + os.strerror(code))


def _owner_alive(state):
    try:
        pid = int(state.get('pid') or 0)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


class VMReset:
    def __init__(self, config_path, policy):
        self.config_path = Path(config_path).resolve()
        self.root = self.config_path.parent / 'local_data' / 'vm-reset'
        self.policy = policy
        self.endpoint = policy['endpoint'].rstrip('/')
        self.instance = policy['instance']
        self.limactl = policy['limactl']

    @classmethod
    def configured(cls, config_path, target=None):
        if target == 'local':
            return None
        path = Path(config_path).resolve().parent / 'local_data' / 'vm-reset' / 'config.json'
        if not path.exists():
            return None
        try:
            policy = json.loads(path.read_text())
            if (policy['version'] != 1 or not isinstance(policy['endpoint'], str)
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', policy['instance'])
                    or not Path(policy['limactl']).is_absolute()):
                raise ValueError()
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError('The local VM reset configuration is invalid.') from error
        if target is not None and target.rstrip('/') != policy['endpoint'].rstrip('/'):
            return None
        return cls(config_path, policy)

    def state(self):
        path = self.root / 'state.json'
        try:
            value = json.loads(path.read_text())
            if not isinstance(value, dict) or value.get('phase') not in ('ready', 'in_use', 'awaiting_native', 'resetting', 'error'):
                raise ValueError()
            return value
        except (OSError, ValueError):
            return {'phase': 'error', 'detail': 'VM recovery is required before another task.'}

    def status(self):
        return {'endpoint': self.endpoint, 'enabled': True, 'mode': 'snapshot', **self.state()}

    def mark(self, phase, detail, run_id=None):
        _write_json(self.root / 'state.json', {
            'phase': phase, 'detail': detail, 'run_id': run_id,
            'pid': os.getpid(), 'updated': time.time(),
        })

    def _command(self, *args, timeout=120):
        try:
            result = subprocess.run([self.limactl, '--tty=false', *args],
                                    capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f'VM {args[0]} timed out; reset remains blocked.') from error
        if result.returncode:
            # Keep tool logs on the host, not in the public UI or exception text.
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / 'last-command.log').write_text(result.stdout + result.stderr)
            raise RuntimeError(f'VM {args[0]} failed. See local_data/vm-reset/last-command.log.')
        return result.stdout

    def _instance(self):
        rows = [json.loads(line) for line in self._command('list', '--json', self.instance).splitlines() if line.strip()]
        if len(rows) != 1 or rows[0].get('name') != self.instance or rows[0].get('vmType') != 'vz':
            raise RuntimeError('The configured VM is not the expected Lima VZ instance.')
        info = rows[0]
        config = info.get('config', {})
        if config.get('additionalDisks') or config.get('vmOpts', {}).get('vz', {}).get('diskImageFormat', 'raw') != 'raw':
            raise RuntimeError('Snapshot reset supports a single raw guest disk only.')
        return info

    def _manifest(self, info):
        try:
            manifest = json.loads((self.root / 'baseline' / 'manifest.json').read_text())
            directory = Path(info['dir'])
            if (manifest['instance'] != self.instance or manifest['directory'] != str(directory)
                    or manifest['identity'] != hashlib.sha256((directory / 'vz-identifier').read_bytes()).hexdigest()
                    or manifest['config_sha256'] != hashlib.sha256((directory / 'lima.yaml').read_bytes()).hexdigest()
                    or set(manifest['files']) != {'disk', 'vz-efi'}):
                raise ValueError()
            for name, size in manifest['files'].items():
                path = self.root / 'baseline' / name
                if path.is_symlink() or not path.is_file() or path.stat().st_size != size or size <= 0:
                    raise ValueError()
            return manifest
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise RuntimeError('The VM baseline is missing or no longer matches this instance. Reset is blocked.') from error

    def _stop(self):
        info = self._instance()
        if info['status'] == 'Running':
            self._command('stop', self.instance)
            info = self._instance()
        if info['status'] != 'Stopped':
            raise RuntimeError('The VM must be fully stopped before its disk can be restored.')
        return info

    def _wait_ready(self):
        from .amazon_vm_service import AmazonVMDesktopController
        from .config import load_global_model_router_config
        config = load_global_model_router_config(self.config_path)
        controller = AmazonVMDesktopController(
            endpoint=self.endpoint, api_key_env=config.desktop.amazon_vm_api_key_env,
            credential_file=self.config_path.parent / 'credentials' / 'amazon_vm.json', timeout_seconds=3,
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                # An authenticated observation proves the bridge AND desktop are ready.
                response = controller._post_json('/observe', {})
                if response.get('image_base64'):
                    return
            except (OSError, RuntimeError, ValueError):
                pass
            time.sleep(1)
        raise RuntimeError('The VM restarted but its desktop bridge did not become ready.')

    def restore(self, run_id=None):
        """Caller holds this endpoint's desktop lock for the entire restore."""
        from .run_state import input_lock
        with input_lock(self.root.parent, self.endpoint):
            self._restore(run_id)

    def _restore(self, run_id=None):
        self.mark('resetting', 'Restoring the clean VM snapshot…', run_id)
        staged = []
        try:
            info = self._instance()
            manifest = self._manifest(info)  # Validate BEFORE stopping or changing anything.
            directory = Path(info['dir'])
            self._stop()
            # Stage every file first. A failed clone never overwrites the current disk.
            for name in manifest['files']:
                temporary = directory / ('.echopath-' + name + '-' + uuid.uuid4().hex)
                _clone(self.root / 'baseline' / name, temporary)
                staged.append(temporary)
                temporary.chmod(0o600)
            for temporary, name in zip(staged, manifest['files']):
                temporary.replace(directory / name)
            self._command('start', self.instance, '--timeout=120s', timeout=150)
            self._wait_ready()
            self.mark('ready', 'Clean snapshot restored. Ready for the next task.', run_id)
        except Exception as error:
            self.mark('error', str(error), run_id)
            raise
        finally:
            for path in staged:
                path.unlink(missing_ok=True)

    def before_run(self, control):
        try:
            self._manifest(self._instance())
        except Exception as error:
            self.mark('error', str(error), control.id)
            raise
        state = self.state()
        if state['phase'] == 'awaiting_native' and state.get('run_id') != control.id:
            if _owner_alive(state):
                raise RuntimeError('Finish or cancel the pending native continuation before another VM task.')
            self.restore(control.id)
        if state['phase'] not in ('ready', 'awaiting_native'):
            control.update('Recovering the VM before starting this task.')
            self.restore(control.id)
        self.mark('in_use', 'The VM will reset when this task ends.', control.id)

    def after_run(self, control):
        run = control.registry.get(control.id)
        if run['status'] == 'awaiting_native':
            self.mark('awaiting_native', 'Waiting for the task’s native continuation.', control.id)
            return
        with control.registry.connect() as db:
            db.execute("UPDATE runs SET status='resetting', detail='Restoring the clean VM snapshot…', updated=? WHERE id=?", (time.time(), control.id))
        try:
            self.restore(control.id)
            detail = run['detail'] + '\nVM restored to the clean snapshot.'
        except Exception as error:
            detail = run['detail'] + '\nVM reset failed: ' + str(error)
        control.finish(run['status'], detail)

    def recover(self):
        """Recover a dead worker or failed reset without interrupting a live task."""
        from .run_state import desktop_lock
        with desktop_lock(self.root.parent, self.endpoint):
            state = self.state()
            if state['phase'] != 'ready' and not (state['phase'] == 'awaiting_native' and _owner_alive(state)):
                self.restore(state.get('run_id'))

    def capture(self):
        from .run_state import desktop_lock
        from .dashboard import validate_endpoint
        validate_endpoint(self.endpoint)
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        baseline = self.root / 'baseline'
        if baseline.exists() or (self.root / 'config.json').exists():
            raise RuntimeError('A baseline already exists. It will not be overwritten.')
        with desktop_lock(self.root.parent, self.endpoint):
            info = self._stop()
            directory = Path(info['dir'])
            baseline.mkdir(mode=0o700)
            try:
                files = {}
                for name in ('disk', 'vz-efi'):
                    source = directory / name
                    if source.is_symlink() or not source.is_file():
                        raise RuntimeError('Unsupported VM disk layout.')
                    _clone(source, baseline / name)
                    (baseline / name).chmod(0o400)
                    files[name] = source.stat().st_size
                _write_json(baseline / 'manifest.json', {
                    'instance': self.instance, 'directory': str(directory),
                    'identity': hashlib.sha256((directory / 'vz-identifier').read_bytes()).hexdigest(),
                    'config_sha256': hashlib.sha256((directory / 'lima.yaml').read_bytes()).hexdigest(),
                    'files': files, 'created': time.time(),
                })
            finally:
                self._command('start', self.instance, '--timeout=120s', timeout=150)
            self._wait_ready()
            self.mark('ready', 'Clean snapshot saved. Automatic reset is enabled.')
            _write_json(self.root / 'config.json', self.policy)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='echopath.local.json')
    parser.add_argument('action', choices=('capture', 'restore', 'status'))
    parser.add_argument('--instance')
    parser.add_argument('--endpoint')
    args = parser.parse_args(argv)
    if args.action == 'capture':
        executable = shutil.which('limactl')
        if not executable or not args.instance or not args.endpoint:
            parser.error('capture needs limactl, --instance and --endpoint')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', args.instance):
            parser.error('invalid instance name')
        manager = VMReset(args.config, {'version': 1, 'instance': args.instance,
                                       'endpoint': args.endpoint.rstrip('/'), 'limactl': executable})
        manager.capture()
    else:
        manager = VMReset.configured(args.config)
        if not manager:
            parser.error('automatic VM reset has not been configured')
        if args.action == 'restore':
            from .run_state import desktop_lock
            with desktop_lock(manager.root.parent, manager.endpoint):
                manager.restore()
    print(json.dumps(manager.status(), indent=2))


if __name__ == '__main__':
    main()
