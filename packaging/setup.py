#!/usr/bin/env python3
"""Install a checkout/source bundle, connect coding agents, or verify EchoPath.

Uses Python's standard library until the isolated runtime is installed. All
paths passed to coding agents are absolute. No model credentials are copied.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ('echopath', 'echopath-setup', 'echopath-vm-setup')
BACKENDS = {'codex': ('codex_cli', 'run_codex_cli.py', 'ECHOPATH_CODEX_EXECUTABLE'),
            'claude': ('claude_code', 'run_claude_code.py', 'ECHOPATH_CLAUDE_EXECUTABLE')}


class SetupError(ValueError):
    pass


def read_object(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except (UnicodeError, json.JSONDecodeError):
        raise SetupError(f'Invalid JSON in {path}; the existing file was not changed.') from None
    if not isinstance(value, dict):
        raise SetupError(f'{path} must contain a JSON object.')
    return value


def write_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(json.dumps(value, indent=2) + '\n')
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def backup(path):
    path = Path(path)
    if path.is_file():
        target = path.with_name(path.name + '.backup-' + uuid.uuid4().hex[:8] + '.bak')
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(path.read_bytes())
        print(f'Backup: {target}')


def executable(name):
    candidate = shutil.which(name)
    if not candidate and name == 'codex' and sys.platform == 'darwin':
        # Desktop installations can supply a CLI without adding it to PATH.
        candidate = next((str(p) for p in (
            Path('/Applications/Codex.app/Contents/Resources/codex'),
            Path('/Applications/ChatGPT.app/Contents/Resources/codex')) if p.is_file()), None)
    if not candidate:
        raise SetupError(f'{name} is not installed or on PATH. Install/sign in to that coding agent, then rerun setup.')
    # Do not resolve symlinks: Node launchers can rely on their installed name.
    return os.path.abspath(candidate)


def run(command, *, cwd=None, capture=False, timeout=900):
    result = subprocess.run([str(part) for part in command], cwd=cwd, text=True,
                            capture_output=capture, timeout=timeout)
    if result.returncode:
        detail = 'Review its local output and retry.'
        if capture:
            log = Path(cwd or ROOT) / 'local_data' / 'setup-command-error.json'
            write_private(log, {'executable': Path(command[0]).name, 'exit': result.returncode,
                                'stdout': result.stdout, 'stderr': result.stderr})
            detail = f'Private diagnostic: {log}'
        raise SetupError(f'{Path(command[0]).name} failed (exit {result.returncode}). {detail}')
    return result.stdout if capture else ''


def python_path(root):
    return Path(root) / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def configure(root, backend, cli, replace=False):
    config = root / 'echopath.local.json'
    existed = config.exists()
    payload = read_object(config if existed else root / 'echopath.config.json')
    original = json.dumps(payload, sort_keys=True)
    folder, script, env_name = BACKENDS[backend]
    if existed:
        previous = payload.get('external_command', {}).get('command', [])
        expected_script = root / 'bridges' / folder / script
        same = (isinstance(previous, list) and len(previous) == 2
                and isinstance(previous[1], str)
                and (root / previous[1]).resolve() == expected_script.resolve()
                and payload.get('model_routing', {}).get('mode', 'external_command') == 'external_command')
        if not same and not replace:
            raise SetupError('The existing config uses a different/custom backend. It was preserved. Review it, then use --replace-backend to select this backend explicitly.')
    command = payload.setdefault('external_command', {})
    command.update(command=[str(python_path(root)), str(root / 'bridges' / folder / script)], working_directory=str(root))
    command.setdefault('env', {})[env_name] = cli
    routing = payload.setdefault('model_routing', {})
    routing['mode'] = 'external_command'
    if not existed or replace:
        routing['external_model'] = 'default'
    if not existed or original != json.dumps(payload, sort_keys=True):
        backup(config)
        write_private(config, payload)
    return config


def agent_paths(agent):
    if agent == 'codex':
        home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser()
        return home / 'config.toml', Path.home() / '.agents' / 'skills'
    override = os.environ.get('CLAUDE_CONFIG_DIR')
    home = Path(override).expanduser() if override else Path.home() / '.claude'
    return (home / '.claude.json' if override else Path.home() / '.claude.json'), home / 'skills'


def registration(agent, cli, root):
    if agent == 'claude':
        path, _ = agent_paths(agent)
        return read_object(path).get('mcpServers', {}).get('echopath')
    result = subprocess.run([cli, 'mcp', 'get', 'echopath', '--json'], cwd=root, text=True, capture_output=True, timeout=30)
    if result.returncode:
        if 'No MCP server named' in result.stderr or 'No MCP server named' in result.stdout:
            return None
        raise SetupError('Cannot inspect Codex MCP configuration. Run codex mcp get echopath --json and resolve its error before retrying.')
    try:
        data = json.loads(result.stdout)
        transport = data.get('transport', data)
        return {**transport, '_enabled': data.get('enabled', True)}
    except (ValueError, AttributeError):
        raise SetupError('Codex did not return a supported MCP configuration. Update the CLI and retry.') from None


def expected_server(root, config, command=None):
    """Describe the MCP process the selected coding agent must launch.

    On macOS app installs this is the EchoPath launcher, rather than the
    virtualenv interpreter. The coding agent uses it for MCP, while local GUI
    tasks are handed to the already-running EchoPath.app Control Room, the
    process macOS has authorized for Accessibility and Screen Recording.
    """
    return {'command': str(command or python_path(root)),
            'args': ['-m', 'echopath.mcp.server', '--config', str(config)]}


def own_server(current, expected, root):
    """An earlier registration of this same installation (venv interpreter or its built app)."""
    return (isinstance(current, dict) and current.get('args', []) == expected['args'] and not current.get('url')
            and (current.get('command') == str(python_path(root))
                 or str(current.get('command', '')).endswith('/EchoPath.app/Contents/MacOS/EchoPath')))


def same_server(current, expected):
    return (isinstance(current, dict) and current.get('command') == expected['command']
            and current.get('args', []) == expected['args'] and not current.get('url')
            and current.get('_enabled', True))


def skill_updates(root, agent, replace=False):
    _, directory = agent_paths(agent)
    updates = []
    for name in SKILLS:
        source = root / 'packaging' / 'agents' / name
        target = directory / name
        old_origin = read_object(target / 'origin.json')
        if old_origin and old_origin.get('package_root') != str(root) and not replace:
            raise SetupError(f'{target} belongs to another checkout. Review it before using --replace-agent.')
        # A copy this installation placed earlier is updated in place (with a backup);
        # anything else that differs is someone's edit or another package's skill.
        ours = old_origin.get('package_root') == str(root)
        for file in source.rglob('*'):
            if not file.is_file() or '__pycache__' in file.parts:
                continue
            dest = target / file.relative_to(source)
            if dest.is_symlink() or target.is_symlink():
                raise SetupError(f'{target} is symlinked; preserve it and choose how to resolve that skill manually.')
            if dest.exists() and dest.read_bytes() != file.read_bytes() and not ours and not replace:
                raise SetupError(f'An existing skill differs at {dest}. Review it before using --replace-agent.')
            updates.append((file, dest))
    return updates


def connect(root, config, selected, replace=False, server_command=None):
    if selected == 'none':
        return []
    expected = expected_server(root, config, server_command)
    plans = []
    # Inspect every requested target before making any registration changes.
    for agent in ('codex', 'claude') if selected == 'both' else (selected,):
        cli = executable(agent)
        current = registration(agent, cli, root)
        if current is not None and not same_server(current, expected) and not own_server(current, expected, root) and not replace:
            raise SetupError(f'{agent} already has a different echopath MCP server. It was preserved. Review it before using --replace-agent.')
        plans.append((agent, cli, current, skill_updates(root, agent, replace)))
    done = []
    for agent, cli, current, updates in plans:
        config_file, skill_directory = agent_paths(agent)
        if not same_server(current, expected):
            backup(config_file)
            if current is not None:
                remove = [cli, 'mcp', 'remove'] + (['--scope', 'user'] if agent == 'claude' else []) + ['echopath']
                run(remove, cwd=root, capture=True, timeout=30)
            add = [cli, 'mcp', 'add'] + (['--scope', 'user'] if agent == 'claude' else []) + ['echopath', '--', expected['command'], *expected['args']]
            run(add, cwd=root, capture=True, timeout=30)
            if not same_server(registration(agent, cli, root), expected):
                raise SetupError(f'{agent} did not save the expected MCP command. Inspect its echopath registration before continuing.')
        for source, dest in updates:
            if dest.exists() and dest.read_bytes() == source.read_bytes():
                continue
            backup(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
        for name in SKILLS:
            write_private(skill_directory / name / 'origin.json', {'package_root': str(root), 'config': str(config)})
        print(f'{agent}: echopath MCP registered; skills installed in {skill_directory}')
        done.append(agent)
    return done


def verify(root, config, model=False):
    command = [python_path(root), root / 'packaging/verify.py', '--config', config]
    if model:
        command.append('--model')
    run(command, cwd=root, timeout=380 if model else 60)


def control_room_running():
    from urllib.request import urlopen
    try:
        with urlopen('http://127.0.0.1:8765/api/status', timeout=2):
            return True
    except OSError:
        return False


def open_app(app):
    """Open the built app; a launch hiccup must not fail an installation that already succeeded."""
    import time
    if subprocess.run(['open', str(app)], capture_output=True).returncode == 0:
        return
    # LaunchServices can refuse (-600) just after the app quit, or while an instance is running.
    time.sleep(3)
    if control_room_running() or subprocess.run(['open', '-n', str(app)], capture_output=True).returncode == 0:
        return
    print(f'Could not open {app} automatically. Open it from Finder before running a local desktop task.')


def install(args):
    from build_package import copy_package, package_files
    package_files(ROOT)  # Also catches incomplete downloaded folders before mutation.
    cli = executable(args.backend)
    if args.app and sys.platform not in ('darwin', 'win32'):
        raise SetupError('--app requires macOS or Windows. The CLI and web Control Room work on other hosts.')
    root = copy_package(ROOT, args.destination) if args.destination else ROOT
    python = python_path(root)
    if not python.exists():
        if (root / '.venv').exists():
            raise SetupError('An incomplete .venv already exists. Inspect it before removing or repairing it.')
        run([sys.executable, '-m', 'venv', root / '.venv'])
    run([python, '-c', 'import sys; assert sys.prefix != sys.base_prefix, "Expected an isolated virtual environment"'])
    pip = subprocess.run([str(python), '-m', 'pip', '--version'], capture_output=True, timeout=30)
    if pip.returncode:
        run([python, '-m', 'ensurepip', '--upgrade'])
    # Python's ensurepip can seed a version too old to understand the
    # pyproject editable-install protocol used by this package.  Upgrade only
    # the installation's private pip before installing dependencies.
    run([python, '-m', 'pip', 'install', '--upgrade', 'pip'], cwd=root)
    extra = '[macos]' if sys.platform == 'darwin' else '[windows]' if sys.platform == 'win32' else ''
    run([python, '-m', 'pip', 'install', '-e', str(root) + extra], cwd=root)
    config = configure(root, args.backend, cli, args.replace_backend)
    verify(root, config)
    server_command = None
    app_path = None
    if args.app and sys.platform == 'darwin':
        run([python, root / 'packaging/macos/make_app.py', '--dest', args.app_destination], cwd=root)
        server_command = Path(args.app_destination).expanduser() / 'EchoPath.app/Contents/MacOS/EchoPath'
        if not server_command.is_file():
            raise SetupError('EchoPath.app was built without its launcher; inspect the app destination and retry.')
        app_path = Path(args.app_destination).expanduser() / 'EchoPath.app'
    elif args.app:
        run([python, root / 'packaging/windows/make_app.py', '--config', config, '--dest', args.app_destination], cwd=root)
        app_path = Path(args.app_destination).expanduser() / 'EchoPath.lnk'
    connected = connect(root, config, args.agent, args.replace_agent, server_command)
    receipt = {'package_root': str(root), 'config': str(config), 'python': str(python),
               'backend': args.backend, 'agents': connected, 'mcp_verified': True, 'model_verified': False,
               'app': str(app_path) if app_path else None}
    write_private(root / 'local_data/installation.json', receipt)
    print(json.dumps(receipt, indent=2))
    if args.app and sys.platform == 'darwin':
        print('AUTHORIZATION STEP: EchoPath.app is opening to request local-control permissions from macOS.')
        print('Approve Accessibility and Screen Recording for EchoPath, then quit and reopen EchoPath.app.')
        print('Keep EchoPath.app open while local desktop tasks run: your coding agent sends those tasks through this authorized app.')
        open_app(Path(args.app_destination).expanduser() / 'EchoPath.app')
    elif app_path:
        os.startfile(app_path)
    if connected:
        agents = ' and '.join('Codex' if agent == 'codex' else 'Claude Code' for agent in connected)
        print(f'REQUIRED NEXT STEP: fully quit and restart {agents} so it reloads the new EchoPath MCP server.')
        print('Do not run an EchoPath desktop task until that restart' + (' and the macOS authorization step are complete.' if args.app and sys.platform == 'darwin' else ' is complete.'))
    else:
        print('Runtime installed and MCP verified. Run doctor --model to check the login/backend, then reload your coding agent.')
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    installer = commands.add_parser('install', help='Create/reuse .venv, configure a backend, verify and register agents.')
    installer.add_argument('--backend', choices=BACKENDS, required=True)
    installer.add_argument('--agent', choices=('codex', 'claude', 'both', 'none'), required=True)
    installer.add_argument('--destination', type=Path, help='Copy the public package into an empty permanent folder before installing.')
    installer.add_argument('--replace-backend', action='store_true', help='Back up and replace a different existing backend; keep unrelated settings.')
    installer.add_argument('--replace-agent', action='store_true', help='Back up and replace conflicting echopath registrations/skills only.')
    installer.add_argument('--app', action='store_true', help='Build the macOS menu-bar app or create a Windows tray-app shortcut.')
    installer.add_argument('--app-destination', default=str(Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs' if sys.platform == 'win32' else Path.home() / 'Applications'))
    connector = commands.add_parser('connect', help='Register an already installed runtime and its skills.')
    connector.add_argument('--agent', choices=('codex', 'claude', 'both'), required=True)
    connector.add_argument('--config', type=Path, default=ROOT / 'echopath.local.json')
    connector.add_argument('--replace-agent', action='store_true')
    doctor = commands.add_parser('doctor', help='Check MCP; optionally send one model request. No desktop actions.')
    doctor.add_argument('--config', type=Path, default=ROOT / 'echopath.local.json')
    doctor.add_argument('--model', action='store_true')
    vm = commands.add_parser('vm', help='Set up, connect or check a VM desktop; run "vm -- --help" for its commands.')
    vm.add_argument('--config', type=Path, default=ROOT / 'echopath.local.json')
    vm.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if sys.version_info < (3, 10):
            raise SetupError(f'Python 3.10+ is required (this is {sys.version.split()[0]}); Python 3.12 is recommended for the Mac app. '
                             'Run this helper with a newer interpreter, e.g. python3.12 packaging/setup.py ...')
        if args.action == 'install':
            install(args)
        else:
            config = args.config.expanduser().resolve()
            if not python_path(ROOT).is_file() or not config.is_file():
                raise SetupError('Install the runtime and create its local configuration first.')
            if args.action == 'vm':
                arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
                return subprocess.run([str(python_path(ROOT)), '-m', 'echopath.vm_cli', '--config', str(config), *arguments]).returncode
            if args.action == 'connect':
                verify(ROOT, config)
                connect(ROOT, config, args.agent, args.replace_agent)
            else:
                verify(ROOT, config, args.model)
        return 0
    except (SetupError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f'Setup incomplete: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
