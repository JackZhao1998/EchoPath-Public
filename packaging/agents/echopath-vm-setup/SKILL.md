---
name: echopath-vm-setup
description: Create, link, verify, or repair a VM desktop for EchoPath - a Windows VM (VirtualBox), a macOS VM (Tart), a Linux VM (Lima), an existing VM over SSH, or an existing desktop bridge. Use when the user wants EchoPath tasks to run in a VM instead of on their own screen. Not for installing EchoPath itself or for running desktop tasks.
---

# EchoPath VM setup

A VM gives EchoPath a desktop it can control without touching the user's own
screen. The host keeps EchoPath, the model login, history and learned
procedures; the guest runs only a small desktop bridge reached through an SSH
tunnel or a loopback VirtualBox NAT port. Finish with a connection that can see the
guest desktop, not merely one that reports connected.

EchoPath must already be installed (`echopath-setup`). One VM connection is
saved per EchoPath configuration.

## The helper

Read `origin.json` beside this skill for `package_root` and `config`; otherwise
the package root is three levels above this file. Run every command from the
package root with the installed interpreter:

```bash
.venv/bin/python packaging/setup.py vm status
.venv/bin/python packaging/setup.py vm -- --help
```

Each command prints one JSON object with `ok`, and exits nonzero with an `error`
that says what to fix. Add `--config /absolute/config.json` before `vm`'s own
arguments only for a custom configuration. `document/vm_setup.md` describes the
same flow in the Config window and has the troubleshooting table.
On Windows substitute `.venv\Scripts\python.exe` for `.venv/bin/python`.
Read `document/windows_setup.md` for the Windows host and guest workflow.

Start with `status`. If a connection is already saved, say what it is and ask
before replacing it. Saving or installing needs the integration off first
(`vm disable`); stop any active VM task before that.

## Choose the kind of VM

Use what the user asked for. If they did not say, ask once, offering only what
their host supports:

| Choice | Needs | Cost to tell the user first |
| --- | --- | --- |
| New macOS VM (Tart) | Apple silicon Mac | about 25-30 GB download, 50 GB disk reserved, 8 GB memory while running |
| New Linux VM (Lima) | macOS host with Lima | a few GB download, 30 GB disk, 4 GB memory |
| New Windows VM (VirtualBox) | Windows host, VirtualBox 7.2+, Windows x64 ISO | ISO download plus 80 GB expanding disk, 8 GB memory while running |
| Existing VM over SSH | Linux X11 or macOS guest with Python 3.10+ and key login | nothing new |
| Existing desktop bridge | its URL and key | nothing new |

Creating a VM downloads large images and takes disk and memory. State the size
and get a yes before the download; check free disk (`df -h`) and memory first
and say so when the host is short. Installing a VM tool is part of that same
yes. Do not create a second VM to work around a problem with the first.

## New macOS VM with Tart

1. **Install Tart** if `tart --version` fails. Prefer
   `brew install cirruslabs/cli/tart`. If Homebrew is missing or not writable
   by this user, do not use `sudo`: download
   `https://github.com/cirruslabs/tart/releases/latest/download/tart.tar.gz`,
   extract it to `~/.local/opt/tart`, confirm `spctl -a -vv tart.app` reports a
   notarized Developer ID from Cirrus Labs, and link
   `tart.app/Contents/MacOS/tart` to `~/.local/bin/tart`. Tart is free on
   personal machines; organizations should check its license.
2. **Clone an image no newer than the host's macOS** (`sw_vers`): for example
   `ghcr.io/cirruslabs/macos-sonoma-base:latest` on macOS 14,
   `macos-sequoia-base` on 15. Use a name beginning with `echopath-`.

   ```bash
   tart clone ghcr.io/cirruslabs/macos-sonoma-base:latest echopath-macos
   tart set echopath-macos --cpu 4 --memory 8192 --display 1440x900
   ```

   The clone is the long download; run it in the background and report
   progress rather than waiting silently.
3. **Start it detached**, so it outlives this session, and wait for an address:

   ```bash
   mkdir -p ~/.echopath
   nohup tart run echopath-macos > ~/.echopath/tart-echopath-macos.log 2>&1 &
   tart ip echopath-macos --wait 120
   ```

   The VM window must stay open: the guest needs a logged-in desktop.
4. **Link it.** One command authorizes a dedicated SSH key in the guest,
   exposes a Python 3.10+ to SSH, reads the guest's SSH host key through Tart's
   own guest agent and trusts exactly that key, then saves the connection:

   ```bash
   .venv/bin/python packaging/setup.py vm tart-link echopath-macos
   ```

   It never needs the guest password. If it reports that the guest agent or
   Remote Login is unavailable (images not built by Cirrus Labs), fall back to
   the existing-VM steps below.
5. Continue with **Install the bridge and prove the desktop**.

The Cirrus Labs images ship a known default account (`admin` / `admin`). Tell
the user, and recommend changing it if the VM is reachable from anything but
this Mac. Never type that or any password yourself; the user enters it in the
VM window when macOS asks.

## New Linux VM with Lima

On a macOS host with Lima installed (`brew install lima`):

```bash
.venv/bin/python packaging/setup.py vm save-lima --instance echopath-desktop
.venv/bin/python packaging/setup.py vm create
```

`create` downloads Ubuntu, installs an Xfce desktop, browser and the bridge,
and connects. It can take many minutes; its progress is printed as it goes and
`status` shows the result if this session is interrupted.

## New Windows VM with VirtualBox

Use an official Windows x64 ISO. Microsoft's Enterprise evaluation is time
limited to 90 days; it is not a permanent Windows license. Install VirtualBox
from Oracle or `winget install --id Oracle.VirtualBox --exact` if needed.
Check available disk and memory before starting. Then run from PowerShell:

```powershell
.venv\Scripts\python.exe packaging/setup.py vm save-virtualbox --iso 'C:\Images\Windows.iso' --instance echopath-windows
.venv\Scripts\python.exe packaging/setup.py vm create --timeout 3600
```

This creates an owned VM with a visible console, UEFI Secure Boot and TPM,
installs Windows and Guest Additions, installs Python and the guest bridge,
and connects. It exposes guest port 8790 only through host loopback port 8791.
The local account password is generated and saved privately; do not display
it or pass it on command lines. Host folders and clipboards are not shared.
Do not replace or adopt an unrelated existing VM to work around an error.

The bridge runs as an interactive logon task, not a Session 0 service.
Windows must be signed in and unlocked. Use `vm open-window` to show its
console. After a reboot, sign in there, then use `vm connect`. If initial
installation times out, finish any prompts in that same VM and use
`vm install`; do not create another VM. `vm screenshot --out <ignored-path>`
must show the actual desktop before reporting readiness. Use the Control
Room's Pause control before manually taking over an active VM task.

## Existing VM over SSH

The user must already be able to run `ssh <host>` without a password prompt,
with the host key trusted. Do not add `StrictHostKeyChecking=no`, accept an
unverified fingerprint, or ask for a password to get past this; if it fails,
tell the user what to fix in their terminal (see `document/vm_setup.md` for the
Linux packages and macOS Remote Login requirements).

```bash
.venv/bin/python packaging/setup.py vm save-ssh --host my-vm --user me
.venv/bin/python packaging/setup.py vm test
```

`--host` takes a hostname, address or `~/.ssh/config` alias. Add
`--identity-file`, `--port`, and for Linux `--display` / `--xauthority`, only
when the defaults are wrong. `test` checks SSH and guest Python and installs
nothing.

## Existing desktop bridge

```bash
.venv/bin/python packaging/setup.py vm save-direct --endpoint http://127.0.0.1:8791
.venv/bin/python packaging/setup.py vm connect
```

The bridge key is read from the configured environment variable (normally
`ECHOPATH_AMAZON_VM_API_KEY`) or saved by the user in Config. Never ask for it
in chat or pass it on a command line.

## Install the bridge and prove the desktop

```bash
.venv/bin/python packaging/setup.py vm install
```

This uploads only the desktop bridge, installs it as the guest user's login
service or interactive task, opens the connection and connects. Windows
`create` already performs this step. Then verify the desktop in this order:

1. **Permissions (macOS guests).** When the guest bridge lacks **Screen
   Recording** or **Accessibility**, `install` fails naming the missing grants
   and the exact program to enable, and `status` lists them under
   `guest.missing_permissions`. These are the user's to grant, in the VM
   window: System Settings -> Privacy & Security -> each list -> add or enable
   that program (Cmd-Shift-G in the file picker accepts the path). Do not edit
   privacy databases, even where the guest allows it. After the user confirms:

   ```bash
   .venv/bin/python packaging/setup.py vm restart-bridge
   ```

   Repeat until `status` shows `missing_permissions: []`. A macOS guest without
   these still returns screenshots (wallpaper only) and silently drops clicks,
   so never skip this check.
2. **Look at the screen.** Save a capture and read the image yourself:

   ```bash
   .venv/bin/python packaging/setup.py vm screenshot --out /tmp/echopath-vm.png
   ```

   It must show the guest's real desktop: Dock or panel, and any open window.
   Wallpaper and menu bar only means Screen Recording is still missing.
3. **Status.** `connected: true`, `message: "Desktop ready."`, and for macOS
   guests both permissions `true`.

Do not send clicks or keystrokes to the guest as a setup test. A first real
task is the user's choice; offer one small task through the `echopath` skill
(`target: vm`) and run it only when they agree.

## What to tell the user at the end

- What was created or linked: VM name, guest OS, resources, address, and where
  the connection is saved.
- That status, permissions and a screen capture were each verified, or exactly
  which one is still pending and who has to act.
- How to stop and start the VM (`tart stop` / `tart run`, or `limactl stop` /
  `limactl start`), and that it keeps running and using memory until stopped.
- A fresh guest shows first-run dialogs (welcome screens, sign-in prompts) the
  first time each app opens, so the first task in an app is slower and learned
  procedures only replay cleanly afterwards.
- Disk used by the download.

## When it stops working

- **Address changed after a VM restart (Tart):** `vm disable`, then
  `vm tart-link <name>` again, then `vm connect`. A reused address with a new
  host key is replaced only because the key is re-read from inside the guest.
- **VM not running:** start it (`tart run ...` detached, or `limactl start`),
  then `vm connect`.
- **Connected but tasks do nothing or see only wallpaper:** `vm status`; fix
  the reported permissions; `vm restart-bridge`.
- **Bridge missing after a guest rebuild:** `vm disable`, `vm install`.
- **Local tunnel port in use:** `vm disable`, save again with another
  `--local-port`.
- Guest logs: `~/.local/share/echopath-guest/service.log` (macOS) or
  `journalctl --user -u echopath-guest.service` (Linux).
  Windows logs are at `%LOCALAPPDATA%\EchoPath\Guest\service.log` inside the guest.

Never delete a VM, its disk, or the user's `known_hosts` entries for other
hosts to recover. Removal is the user's decision; `document/vm_setup.md` lists
the steps.
