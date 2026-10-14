# Optional VM setup

EchoPath can use your own desktop or a separate VM desktop. VM integration is
off in the public default configuration. Open **Config → Virtual machine →
Set up or edit a VM connection** to choose a connection. This version saves one
VM profile per EchoPath configuration.

The host runs EchoPath and your authenticated Codex or Claude Code bridge. The
guest runs a small desktop bridge and the applications used by your tasks. Your
model login, model credentials, task history and learned procedures stay on the
host. Screenshots still go to the configured model when a task needs vision.

For a Windows host and a visible Windows guest, follow the
[Windows setup guide](windows_setup.md). The VirtualBox connection uses the same
Control Room, VM viewer, MCP task controls, and learning runtime described here.

## Set up from a terminal or with your coding agent

Every step in this guide is also available without the Config window, which is
what the bundled `echopath-vm-setup` skill uses:

```bash
.venv/bin/python packaging/setup.py vm status
.venv/bin/python packaging/setup.py vm save-ssh --host my-vm --user me
.venv/bin/python packaging/setup.py vm test
.venv/bin/python packaging/setup.py vm install
.venv/bin/python packaging/setup.py vm screenshot --out /tmp/echopath-vm.png
```

Each command prints JSON and exits nonzero on failure. `save-lima` plus `create`
builds the Linux VM described below, `tart-link` prepares a macOS VM made with
Tart, `restart-bridge` restarts the guest service, and `disable` switches the
integration off before a saved connection is changed. Bridge keys are never
accepted on the command line.

## Connect an existing VM through SSH

The VM needs to be running and reachable from the computer running EchoPath.
Use a Linux X11 or macOS guest with Python 3.10+, a graphical user session and a
browser. Automatic installation uses the SSH user's home and service manager;
it does not install an operating system or desktop into an existing VM.

1. Confirm that SSH works in Terminal, for example `ssh my-vm`. Verify the
   host fingerprint through your usual trusted channel when connecting for the
   first time. EchoPath requires an already trusted host and noninteractive
   key authentication. Unlock your SSH agent before using Config.
2. Select **Connect an existing VM with SSH**. Enter a hostname, IP address or
   alias from `~/.ssh/config`. Username, SSH port and private key file are
   optional when already provided by your SSH configuration. Enter a key's
   file path, never its contents or password.
3. Under **Advanced connection settings**, check the guest bridge port and
   Linux display details. The defaults are guest port `8790`, host tunnel port
   `8791`, display `:0`, and the guest user's `~/.Xauthority`. For a Linux login
   session, run `echo "$DISPLAY"` and `echo "$XAUTHORITY"` in its desktop
   terminal to find the actual values. Wayland-only sessions are not supported
   by this installer; use an X11 session.
4. Choose **Save connection**, then **Test SSH**. This checks SSH access and
   Python without installing anything.
5. Choose **Install bridge & connect** to install and enable the guest service.
   If a compatible bridge is already running, enter its key under Advanced,
   save, and choose **Connect** instead.
6. Wait for **Ready**, then choose **Open desktop**. This confirms that the
   authenticated bridge can capture an actual guest screen, beyond opening an
   SSH connection.

The host needs the OpenSSH client. SSH aliases, identity files, agents and
`ProxyJump` can be configured in the user's normal SSH configuration. EchoPath
does not forward the SSH agent to the guest or store SSH passwords.

### Existing Linux prerequisites

Use the same Linux account for SSH and the graphical desktop. The installer
needs Python's `venv` support, Tk, an X11 screenshot program (`scrot`), clipboard
support (`xclip`), and a working user systemd manager. It installs Python
dependencies in its own virtual environment. On Debian/Ubuntu, prepare the
system packages in the guest:

```bash
sudo apt-get install python3 python3-venv python3-tk scrot xclip
```

For optional task recording, also install `ffmpeg` with the `libx264` encoder.
Keep the graphical session logged in. To allow the user service to stay running
after SSH logout, an administrator can enable lingering for that guest account:

```bash
sudo loginctl enable-linger "$USER"
```

Lingering keeps the service manager available; an existing guest still needs
its X11 desktop session. The automatic installer does not run `sudo` on an
existing VM. Prepare these prerequisites yourself before installing the bridge.

### Existing macOS prerequisites

Enable **Remote Login** for the guest account, install Python 3.10+ with venv
support, and keep that account logged into the graphical desktop. Grant the
guest Python runtime **Accessibility** and **Screen Recording** permissions in
System Settings. The installed executable is under
`~/.local/share/echopath-guest/venv/bin/python`. Restart the guest service after
changing permissions. These are guest permissions, independent of the host's
EchoPath permissions.

The bridge reports both grants to the host. While one is missing, connecting
fails with a message naming it and the program to enable, instead of showing a
desktop that returns wallpaper-only screenshots or ignores input. Grant it in
the guest, restart the guest service (`vm restart-bridge`), and connect again.

## Create a macOS VM with Tart

EchoPath does not create macOS guests itself, but a VM made with
[Tart](https://tart.run) on an Apple silicon Mac links in one step. Expect a
25-30 GB image download, 50 GB of reserved disk and 8 GB of memory while it runs.

```bash
brew install cirruslabs/cli/tart
tart clone ghcr.io/cirruslabs/macos-sonoma-base:latest echopath-macos
tart set echopath-macos --cpu 4 --memory 8192 --display 1440x900
nohup tart run echopath-macos > ~/.echopath/tart-echopath-macos.log 2>&1 &
tart ip echopath-macos --wait 120
.venv/bin/python packaging/setup.py vm tart-link echopath-macos
.venv/bin/python packaging/setup.py vm install
```

Choose an image no newer than the host's macOS version. `tart-link` uses Tart's
guest agent to authorize a dedicated key (`credentials/vm_ssh_ed25519`), make a
Python 3.10+ visible to SSH, and read the guest's SSH host key from inside the
guest before trusting it, so no password is typed and no fingerprint is
accepted blindly. Then grant Screen Recording and Accessibility in the VM
window as described above. The Cirrus Labs images use the default account
`admin` / `admin`; change it if the VM is reachable from other machines. If the
VM's address changes after a restart, switch the integration off and run
`tart-link` again. Without Homebrew, install Tart from its GitHub release into
your home folder; Tart's license applies to organizational use.

## Create a Linux VM on macOS

The initial managed creation option uses Lima and Apple's Virtualization
framework. Install a current Lima release on the host (`brew install lima`),
then:

1. Choose **Create a Linux VM on this Mac**.
2. Enter a VM name beginning with `echopath-`. The defaults are
   `echopath-desktop`, 4 CPU cores, 4 GiB memory and 30 GiB disk capacity. Leave
   enough host memory and disk space for the guest and downloaded images.
3. **Save connection**, then choose **Create VM & connect**. This downloads the
   Ubuntu 24.04 image from Lima's installed template, starts the VM, installs
   Xfce, a browser, screenshot/clipboard tools and FFmpeg, and starts an X11
   desktop on `:99` with the guest bridge.
4. Wait for **Ready**, then **Open desktop**.

Creation and package downloads can take several minutes. Progress continues
when the Config window closes; reopen it to see the result. Host directories
are not mounted in the managed guest. The bridge listens on guest loopback and
is reached through EchoPath's SSH tunnel. The guest still has network access.

EchoPath will not reuse or overwrite an existing Lima VM with the requested
name unless that VM was created by the same saved EchoPath profile. Choose
another name or use the existing-VM SSH option. A failed preparation can be
retried with **Prepare VM & connect** while the integration is off.

The VM is persistent. Disconnecting or switching the integration off does not
stop or delete it, and setup does not enable automatic snapshot resets. Manage
its power separately with Lima, for example:

```bash
limactl stop echopath-desktop
limactl start echopath-desktop
```

Start a powered-off VM before reconnecting. See the shared usage guide's
[reset instructions](https://github.com/JackZhao1998/EchoPath-Public/blob/main/document/usage.md#automatic-vm-reset) if you
want a reviewed baseline restored after tasks. These Lima/Tart creation paths require a macOS host. For managed Windows guest
creation and installation on a Windows host, use the [Windows guide](windows_setup.md).

## Use an existing desktop bridge

Select **Connect an existing desktop bridge**, enter its HTTP(S) origin, and
enter its bridge key under Advanced. Use **Save connection → Test desktop →
Connect**. This mode works with an existing tunnel or an authenticated HTTPS
bridge, including manually prepared Windows guests. EchoPath monitors the
desktop but does not manage the external tunnel or install guest software in
this mode.

For manual bridge installation from an EchoPath checkout, set a strong
`ECHOPATH_AMAZON_VM_API_KEY` in the guest environment, then run:

```bash
python -m echopath.amazon_vm_agent --host 127.0.0.1 --port 8790
```

Forward the bridge from the host, leaving this separate tunnel running:

```bash
ssh -N -L 8791:127.0.0.1:8790 your-user@your-vm-host
```

Save `http://127.0.0.1:8791` in Config and supply the same key. A direct bridge
URL does not create SSH encryption by itself; use loopback through a tunnel or
HTTPS for a remote connection.

Older configurations without a `vm` section retain the existing direct-URL
behavior until a profile is saved. Once a profile is saved, VM task launches,
viewer input and connections use that profile and respect the integration
switch. Local desktop tasks remain available independently.

## Connection lifecycle and saved files

**Connect** enables the integration. **Disconnect** closes EchoPath's tunnel
and pauses reconnection while retaining the enabled preference. The **Use a
virtual machine** switch turns the integration off. Switch it off before
editing the saved profile. Stop active or paused VM tasks before disconnecting,
changing or preparing the guest.

The detached connection worker survives closing Config and the Control Room.
With automatic reconnection enabled it retries a lost connection and can be
started again when EchoPath opens. After a host restart, open EchoPath and
unlock any required SSH key. Explicitly disconnecting prevents automatic
reconnection until **Connect** is chosen again.

| Location | Contents |
| --- | --- |
| Active EchoPath config, `vm` section | Enable switch, profile, SSH alias/path, ports and VM options; no key contents. |
| `credentials/amazon_vm.json`, beside the config | Endpoint and bridge key, with owner-only file permissions. Never returned by Config's API. |
| `local_data/vm-connection/`, beside the config | Shared job and connection status, logs, and markers for VMs created by this profile. |
| Guest `~/.local/share/echopath-guest/` | Minimal bridge code, virtual environment, private connection settings and captures. |

The generated bridge key travels to the guest over SSH standard input. Only the
desktop bridge is uploaded; Codex/Claude, the full host configuration, memories
and model credentials are not copied. If the host environment provides the
configured bridge-key variable (normally `ECHOPATH_AMAZON_VM_API_KEY`), it takes
precedence over the saved key. Installation uses that value too.

On Linux the service is `echopath-guest.service` under the guest user's systemd
directory. On macOS it is the guest user's `org.echopath.guest` LaunchAgent.

## Troubleshooting

| Message or symptom | Next step |
| --- | --- |
| SSH host identity needs attention | Connect in Terminal and verify the fingerprint. Do not bypass host-key checks. |
| SSH authentication failed | Verify the same alias/user/key in Terminal and unlock the SSH agent available to EchoPath. |
| `limactl` missing | Install Lima on the host; restart EchoPath if its environment changed. |
| SSH works, desktop not ready | Check guest Python, bridge key, X11 display/Xauthority or macOS permissions. Confirm that the graphical session is running. |
| Local tunnel port unavailable | Turn integration off and choose an unused local tunnel port. EchoPath does not adopt or stop an unrelated process using that port. |
| Setup interrupted | Reopen Config and retry the operation. A failed creation preserves its VM for inspection or retry. |
| Cannot change connection | Stop the active/paused VM task, or wait for a running setup operation to finish. |

Linux guest service diagnostics:

```bash
systemctl --user status echopath-guest.service
journalctl --user -u echopath-guest.service -n 80
systemctl --user restart echopath-guest.service
```

For a macOS guest, inspect `~/.local/share/echopath-guest/service.log` and restart
with `launchctl kickstart -k gui/$(id -u)/org.echopath.guest`.

To remove the guest bridge independently of EchoPath, disable its user service
first (`systemctl --user disable --now echopath-guest.service` on Linux, or
`launchctl bootout gui/$(id -u)/org.echopath.guest` on macOS), then remove its
service definition and `~/.local/share/echopath-guest/` after reviewing any
captures you want to keep. VM deletion is managed separately through Lima or
your VM provider.
