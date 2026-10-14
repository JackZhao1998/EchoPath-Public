"""Owned Windows VirtualBox guests with a loopback NAT port and visible console.

VM passwords stay in a private local directory and are passed by file. Only the
small desktop bridge is copied to the guest; model logins remain on the host.
"""
from __future__ import annotations

import base64
import io
import os
import re
import secrets
import shutil
import socket
import subprocess
import time
import zipfile
from pathlib import Path

from .platform_utils import private_directory
from .vm_setup import VMSetupError, read_json, write_private


def executable():
    found = shutil.which('VBoxManage')
    candidate = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Oracle/VirtualBox/VBoxManage.exe'
    if not found and candidate.is_file():
        found = str(candidate)
    if not found:
        raise VMSetupError('Install VirtualBox for Windows, then retry. You can use winget install --id Oracle.VirtualBox --exact.')
    return found


class VirtualBoxVM:
    def __init__(self, manager, profile):
        self.manager, self.profile = manager, profile
        self.vbox = executable()
        self.marker_path = manager.root / ('managed-' + profile['id'] + '.json')
        self.root = manager.root / ('virtualbox-' + profile['id'])

    def command(self, *args, timeout=60):
        try:
            result = subprocess.run([self.vbox, *map(str, args)], capture_output=True, text=True,
                                    encoding='utf-8', errors='replace', timeout=timeout,
                                    **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
        except (OSError, subprocess.TimeoutExpired):
            raise VMSetupError('VirtualBox could not complete the operation. Check the VM window and retry.') from None
        if result.returncode:
            # Never expose unattended output, which can contain the guest password.
            if args[0] == 'guestcontrol' and 'guest execution service is not ready' in result.stderr.lower():
                raise VMSetupError('The VirtualBox guest execution service is not ready. Finish installing Guest Additions and restart Windows in the VM, then sign in and retry.')
            codes = re.findall(r'\b(?:VERR_[A-Z0-9_]+|VBOX_E_[A-Z0-9_]+|E_[A-Z0-9_]+)\b', result.stderr)
            detail = ' (' + ', '.join(dict.fromkeys(codes)) + ')' if codes else ''
            raise VMSetupError('VirtualBox ' + str(args[0]) + ' failed' + detail + '. Check the VM window and VirtualBox logs.')
        return result.stdout

    def owned(self):
        marker = read_json(self.marker_path)
        if marker.get('provider') != 'virtualbox' or marker.get('instance') != self.profile['instance'] or not marker.get('uuid'):
            raise VMSetupError('Create this VM first. EchoPath only changes VirtualBox guests it created.')
        return marker

    def info(self):
        marker = self.owned()
        output = self.command('showvminfo', marker['uuid'], '--machinereadable')
        info = dict(re.findall(r'^([^=\r\n]+)="(.*)"$', output, re.M))
        if info.get('name') != self.profile['instance']:
            raise VMSetupError('The saved VM identity changed. Restore its name or create a new profile.')
        return info

    def open(self):
        marker = self.owned()
        state = self.info().get('VMState')
        if state in ('poweroff', 'saved', 'aborted'):
            self.command('startvm', marker['uuid'], '--type', 'gui', timeout=120)
        elif state == 'paused':
            self.command('controlvm', marker['uuid'], 'resume')
        elif state == 'running':
            # Reuse the existing console. Launching a second VM process can
            # display an error dialog for an already running guest.
            import ctypes
            from ctypes import wintypes
            user = ctypes.WinDLL('user32', use_last_error=True)
            user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user.SetForegroundWindow.argtypes = [wintypes.HWND]
            callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
            def focus(handle, _):
                title = ctypes.create_unicode_buffer(512)
                user.GetWindowTextW(handle, title, len(title))
                if title.value.startswith(self.profile['instance'] + ' [') and 'VirtualBox' in title.value:
                    user.ShowWindow(handle, 9)  # SW_RESTORE
                    user.SetForegroundWindow(handle)
                    return False
                return True
            user.EnumWindows(callback_type(focus), 0)
        else:
            raise VMSetupError('Wait for the VM to finish starting or stopping, then reconnect.')

    def guest_auth(self):
        credential = read_json(self.root / 'account.json')
        if not credential.get('username') or not (self.root / 'password.txt').is_file():
            raise VMSetupError('The local VM account receipt is missing. Restore it before installing the bridge.')
        return ['--username', credential['username'], '--passwordfile', str(self.root / 'password.txt')]

    def guest_run(self, exe, *args, timeout=60):
        return self.command('guestcontrol', self.owned()['uuid'], 'run', *self.guest_auth(),
                            '--exe', exe, '--timeout', str(timeout * 1000), '--wait-stdout', '--wait-stderr',
                            '--', exe, *args, timeout=timeout + 10)

    def inspect(self):
        info = self.info()
        if info.get('VMState') != 'running':
            raise VMSetupError('Open the VM and sign in to Windows first.')
        if not info.get('GuestAdditionsVersion'):
            raise VMSetupError('VirtualBox Guest Additions are not ready. Finish Windows setup and sign in in the VM window. If its desktop is ready, install Guest Additions from the VirtualBox Devices menu, then retry.')
        output = self.guest_run(r'C:\Windows\System32\cmd.exe', '/c', 'ver')
        if 'Windows' not in output:
            raise VMSetupError('Windows or VirtualBox Guest Additions are not ready yet.')
        return {'platform': 'win32'}

    def create(self):
        if os.name != 'nt':
            raise VMSetupError('Managed Windows VM creation requires a Windows host.')
        profile, manager = self.profile, self.manager
        iso = Path(profile['iso_path']).expanduser().resolve()
        if not iso.is_file() or iso.suffix.lower() != '.iso':
            raise VMSetupError('Choose a downloaded Windows x64 installation ISO before creating the VM.')
        private_directory(self.root)
        marker = read_json(self.marker_path)
        if not marker:
            inventory = self.command('list', 'vms')
            if any(name == profile['instance'] for name in re.findall(r'^"(.*)" \{', inventory, re.M)):
                raise VMSetupError('A VM with this name already exists. Choose a new name; EchoPath will not overwrite it.')
            with socket.socket() as listener:
                try:
                    listener.bind(('127.0.0.1', profile['local_port']))
                except OSError:
                    raise VMSetupError('The local connection port is in use. Choose another port before creating the VM.') from None
            base = private_directory(Path(os.environ['LOCALAPPDATA']) / 'EchoPath' / 'VMs')
            manager._progress('Creating a Windows VM with a private local connection…')
            output = self.command('createvm', '--name', profile['instance'], '--ostype', 'Windows11_64',
                                  '--basefolder', base, '--register')
            match = re.search(r'UUID:\s*([0-9a-f-]{36})', output, re.I)
            if not match:
                raise VMSetupError('VirtualBox did not return the new VM identity.')
            marker = {'provider': 'virtualbox', 'instance': profile['instance'], 'uuid': match[1], 'phase': 'created'}
            write_private(self.marker_path, marker)
        self.owned()
        if marker.get('phase') == 'created':
            vm = marker['uuid']
            info = self.info()
            rule = f"echopath,tcp,127.0.0.1,{profile['local_port']},,{profile['bridge_port']}"
            forwarding = [value for key, value in info.items() if key.startswith('Forwarding(')]
            if any(value.startswith('echopath,') and value != rule for value in forwarding):
                self.command('modifyvm', vm, '--natpf1', 'delete', 'echopath')
            self.command('modifyvm', vm, '--cpus', profile['cpus'], '--memory', profile['memory_gib'] * 1024,
                         '--firmware', 'efi', '--tpm-type', '2.0', '--graphicscontroller', 'vboxsvga', '--vram', '128',
                         '--ioapic', 'on', '--nic1', 'nat', '--clipboard-mode', 'disabled', '--draganddrop', 'disabled')
            if rule not in forwarding:
                self.command('modifyvm', vm, '--natpf1', rule)
            self.command('modifynvram', vm, 'inituefivarstore')
            self.command('modifynvram', vm, 'enrollmssignatures')
            self.command('modifynvram', vm, 'enrollorclpk')
            self.command('modifynvram', vm, 'secureboot', '--enable')
            disk = Path(os.environ['LOCALAPPDATA']) / 'EchoPath' / 'VMs' / profile['instance'] / 'desktop.vdi'
            if not disk.exists():
                self.command('createmedium', 'disk', '--filename', disk, '--size', profile['disk_gib'] * 1024, '--format', 'VDI')
            if 'SATA' not in [value for key, value in info.items() if key.startswith('storagecontrollername')]:
                self.command('storagectl', vm, '--name', 'SATA', '--add', 'sata', '--controller', 'IntelAhci')
            self.command('storageattach', vm, '--storagectl', 'SATA', '--port', '0', '--device', '0', '--type', 'hdd', '--medium', disk)
            self.command('storageattach', vm, '--storagectl', 'SATA', '--port', '1', '--device', '0', '--type', 'dvddrive', '--medium', iso)
            marker['phase'] = 'configured'
            write_private(self.marker_path, marker)
        if marker.get('phase') == 'configured':
            vm = marker['uuid']
            if not (self.root / 'password.txt').exists():
                write_private(self.root / 'password.txt', secrets.token_urlsafe(24) + 'aA1!')
                write_private(self.root / 'account.json', {'username': 'echopath'})
            # Use the installed provider's template. A compliant VM has TPM and
            # Secure Boot, so remove the template's hardware-check bypasses.
            template = Path(self.vbox).parent / 'UnattendedTemplates/win_nt6_unattended.xml'
            source = template.read_text(encoding='utf-8')
            source = re.sub(r'<RunAsynchronous>.*?</RunAsynchronous>', '', source, flags=re.S)
            source = source.replace('<AutoLogon>', '<AutoLogon>\n<LogonCount>1</LogonCount>')
            unattended = self.root / 'unattended.xml'
            write_private(unattended, source)
            firewall = ('netsh advfirewall firewall add rule name="EchoPath Guest Desktop" dir=in action=allow '
                        f"protocol=TCP localport={profile['bridge_port']} remoteip=10.0.2.2")
            self.command('unattended', 'install', vm, '--iso', iso, '--user', 'echopath',
                         '--user-password-file', self.root / 'password.txt', '--admin-password-file', self.root / 'password.txt',
                         '--full-user-name', 'EchoPath', '--hostname', 'echopath-guest.local', '--locale', 'en_US', '--language', 'en-US',
                         '--country', 'US', '--time-zone', 'America/New_York', '--install-additions', '--no-install-txs',
                         '--script-template', unattended, '--auxiliary-base-path', str(self.root / 'unattended-'),
                         '--post-install-command', firewall, timeout=120)
            marker['phase'] = 'installing'
            write_private(self.marker_path, marker)
        self.open()
        manager._progress('Windows is installing in the VM window. Waiting for its desktop and Guest Additions…')
        deadline = time.monotonic() + 2700
        last_error = None
        while time.monotonic() < deadline:
            try:
                self.inspect()
                break
            except VMSetupError as error:
                if str(error) != last_error:
                    last_error = str(error)
                    manager._progress(last_error)
                time.sleep(10)
        else:
            raise VMSetupError((last_error or 'Windows setup is still pending.') + ' Then use Install bridge & connect.')
        self.install()

    def install(self):
        self.inspect()
        manager, profile = self.manager, self.profile
        manager._progress('Installing Python and the Windows guest desktop bridge…')
        private_directory(self.root)
        credential = read_json(manager.credentials)
        from .config import load_global_model_router_config
        env = load_global_model_router_config(manager.path).desktop.amazon_vm_api_key_env
        key = os.environ.get(env, '').strip() or (credential.get('api_key') if credential.get('endpoint') == profile['endpoint'] else None) or secrets.token_urlsafe(32)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr('echopath_guest/__init__.py', '')
            for name in ('amazon_vm_agent.py', 'pyautogui_service.py'):
                bundle.writestr('echopath_guest/' + name, Path(__file__).with_name(name).read_bytes())
        write_private(self.root / 'payload.json', {'archive': base64.b64encode(archive.getvalue()).decode(),
                                                  'key': key, 'port': profile['bridge_port']})
        for name in ('vm_guest_install.py', 'windows_guest_bootstrap.ps1'):
            write_private(self.root / name, Path(__file__).with_name(name).read_text(encoding='utf-8'))
        guest = r'C:\Users\echopath\AppData\Local\EchoPathSetup'
        self.command('guestcontrol', self.owned()['uuid'], 'mkdir', *self.guest_auth(), '--parents', guest)
        self.command('guestcontrol', self.owned()['uuid'], 'copyto', *self.guest_auth(), '--target-directory', guest + '\\',
                     *[self.root / name for name in ('payload.json', 'vm_guest_install.py', 'windows_guest_bootstrap.ps1')])
        self.guest_run(r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe', '-NoProfile', '-NonInteractive',
                       '-ExecutionPolicy', 'Bypass', '-File', guest + r'\windows_guest_bootstrap.ps1',
                       '-BundleDirectory', guest, timeout=900)
        private_directory(manager.credentials.parent)
        write_private(manager.credentials, {'endpoint': profile['endpoint'], 'api_key': key})
        marker = self.owned()
        write_private(self.marker_path, {**marker, 'phase': 'ready'})

    def restart_bridge(self):
        self.inspect()
        self.guest_run(r'C:\Windows\System32\schtasks.exe', '/end', '/tn', 'EchoPath Guest Desktop')
        self.guest_run(r'C:\Windows\System32\schtasks.exe', '/run', '/tn', 'EchoPath Guest Desktop')
