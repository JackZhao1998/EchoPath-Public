# Windows app and VM setup

EchoPath on Windows has a system tray icon and a browser Control Room at
`http://127.0.0.1:8765`. A separate Windows VM runs the applications you automate.
Watch and operate that desktop in its VirtualBox window or EchoPath's browser
viewer. Model logins and task history stay on the host.

## Install the host

Use Windows 11 x64 with hardware virtualization enabled, Python 3.10 or newer,
and your signed-in Codex or Claude Code CLI. Run in the permanent EchoPath folder:

```powershell
python packaging/setup.py install --backend codex --agent codex --app
```

Use `--backend claude --agent claude` for Claude Code. The installer creates a
private `.venv`, installs the Windows tray dependency, connects the selected MCP
host, and creates an **EchoPath** Start menu shortcut. Restart the selected coding
agent after changing its MCP registration. Double-click the tray icon for the
Control Room. Windows may initially put the icon in the tray overflow.

For an existing configured checkout:

```powershell
.venv\Scripts\python.exe -m pip install -e '.[windows]'
.venv\Scripts\python.exe packaging/windows/make_app.py --config echopath.local.json
.venv\Scripts\pythonw.exe -m echopath.app --config echopath.local.json
```

## Create a Windows desktop

Install VirtualBox and download an official Windows x64 ISO:

```powershell
winget install --id Oracle.VirtualBox --exact
```

Use a licensed Windows image or the
[Microsoft Windows 11 Enterprise evaluation](https://www.microsoft.com/en-us/evalcenter/download-windows-11-enterprise).
The evaluation lasts 90 days; use a suitable Windows license/image for continued
operation. EchoPath does not supply an activation key. VM creation installs the
selected image unattended into a newly created virtual disk.

Open **Configuration → Virtual machine → Set up or edit a VM connection**.
Choose **Create a Windows VM with VirtualBox**, enter the ISO's full local path,
and save. Click **Create VM & connect**. Defaults are 4 CPU cores, 8 GiB memory,
and an 80 GiB expanding disk. The guest has UEFI Secure Boot and a virtual TPM.
Setup opens a visible window, creates a local `echopath` account, installs Guest
Additions and Python, and starts the authenticated desktop bridge. The first
Windows installation can take tens of minutes.

The equivalent agent-friendly commands are:

```powershell
.venv\Scripts\python.exe -m echopath.vm_cli --config echopath.local.json save-virtualbox --iso 'C:\Users\you\Downloads\Windows11.iso'
.venv\Scripts\python.exe -m echopath.vm_cli --config echopath.local.json create --timeout 3600
.venv\Scripts\python.exe -m echopath.vm_cli --config echopath.local.json status
.venv\Scripts\python.exe -m echopath.vm_cli --config echopath.local.json open-window
```

If setup is interrupted, retry **Create VM & connect**. If Windows has already
finished installing, use **Install bridge & connect** to repair the bridge.
EchoPath verifies the saved VM UUID and refuses to overwrite an unrelated VM.
Guest Additions must be running for host-side installation and repair.
If Windows is at its desktop but EchoPath reports that Guest Additions are not
ready, use the VM window's **Devices → Insert Guest Additions CD image**, run
the Windows installer from that disc inside the guest, and restart the guest
if requested. Sign in again, then choose **Install bridge & connect**.

## Connect, watch, and take over

The default connection is `http://127.0.0.1:8791`, forwarded by VirtualBox NAT to
guest port `8790`. The host port listens on loopback only. The guest firewall
rule allows the NAT host (`10.0.2.2`); capture and control requests also require
the generated key. Host folders and the host clipboard are not shared. The
guest bridge runs as a scheduled task in the signed-in user's interactive
session. Keep Windows signed in and unlocked; it cannot automate the lock
screen, UAC secure desktop, or another user's session.

1. Choose **Virtual machine** when starting a task in the Control Room, or call
   `echopath_start_task` with `target="vm"` and `allow_desktop_control=true`.
2. Use **Open VM window** to watch the native console, or **Open desktop** for
   EchoPath's browser viewer.
3. Click **Pause** and wait for **Paused** before using the guest mouse or
   keyboard. The browser viewer offers the same cooperative takeover controls.
4. Click **Resume** when finished. EchoPath observes the changed screen and
   replans. **Stop** ends the task; it does not delete the VM.

Turning integration off or disconnecting leaves the guest and its files intact.
Closing the tray controls leaves task workers running; reopen EchoPath to manage
them. Closing/powering off the VM interrupts its connection. Use **Connect** to
start the saved VM again, then sign in to the guest if needed.

The random guest account password and installation receipts are stored privately
under `local_data/vm-connection/virtualbox-<profile-id>/`; the bridge key is in
`credentials/amazon_vm.json`. These locations are Git-ignored. The VM disk lives
under `%LOCALAPPDATA%\EchoPath\VMs`. On the guest, the bridge and logs live under
`%LOCALAPPDATA%\EchoPath\Guest`, with the scheduled task **EchoPath Guest Desktop**.
Automatic sign-in is used for initial installation only. Subsequent sign-in can
use the locally stored password; do not paste it into a coding-agent prompt.

Windows v0.1 uses visual screen control and the primary guest display. macOS
Vision OCR/accessibility helpers, Lima/APFS automatic baseline resets, and the
existing macOS/Linux task-video recorder are not implemented for this Windows
guest. Screenshots, task controls, visual replay, and persistent memory use the
shared runtime.
