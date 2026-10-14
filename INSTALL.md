# Install EchoPath with your coding agent

Give Codex or Claude Code the repository link or an extracted EchoPath folder.
The agent can read the installation skill directly; you do not need to install
that skill in advance.

**From Git:**

> Install EchoPath from https://github.com/JackZhao1998/EchoPath for this coding
> agent. Read INSTALL.md and follow packaging/agents/echopath-setup/SKILL.md from
> that repository. Use my existing model login, verify the setup, and leave VM
> integration off unless I ask for it.

**From a downloaded folder:**

> Install EchoPath from /absolute/path/to/EchoPath. Read INSTALL.md and follow
> packaging/agents/echopath-setup/SKILL.md inside that folder. Put it in a
> permanent location, connect this coding agent, and verify the setup.

The [setup skill](packaging/agents/echopath-setup/SKILL.md) orchestrates the work.
The helper performs repeatable installation and registration steps. Your agent
handles environment-specific prerequisites and tells you when an interactive
login or OS permission prompt needs your input.

## Prerequisites and support

- Python 3.10+ with `venv` and pip; Python 3.12 is recommended for the Mac app.
  Install Python through [python.org](https://www.python.org/downloads/) or
  [uv](https://docs.astral.sh/uv/guides/install-python/). The installer uses a
  private `.venv` and does not install into the system environment.
- A local Codex or Claude Code CLI and your normal sign-in. Follow the official
  [Codex setup](https://learn.chatgpt.com/docs/cli) or
  [Claude Code setup](https://code.claude.com/docs/en/setup). A chat subscription
  alone does not install the CLI. Setup detects Codex bundled with the Mac
  desktop app when available.
- Network access for Python dependencies and the model check. A source ZIP
  includes the code and setup skill, not an offline Python/CLI/VM distribution.
- macOS: the optional native menu-bar app needs macOS 13+, Xcode Command Line
  Tools (`xcode-select --install`), and a Python build providing `libpython`
  (uv-managed or python.org Python recommended).
- Linux and Windows: install the runtime, MCP server and web Control Room. No
  native menu-bar app is built. Local Linux control needs a graphical X11
  session, Tk, `scrot` and clipboard support (`xclip`). A headless host can
  instead connect to a configured guest desktop. See the [VM guide](document/vm_setup.md).

The agent hosting EchoPath and the model backend are separate choices. For
example, Claude Code can call EchoPath while EchoPath plans with a Codex login.
Use only the selected agents; setup does not connect every detected application.

## Install from a checkout or extracted folder

Choose a permanent location before installing: the app and MCP registration
point to this folder. Do not move/delete it after setup without reconnecting.
The examples below are run in the package root.

```bash
# Codex host and Codex backend
python3 packaging/setup.py install --backend codex --agent codex

# Claude Code host and Claude Code backend
python3 packaging/setup.py install --backend claude --agent claude

# Both hosts, one selected backend
python3 packaging/setup.py install --backend codex --agent both
```

On Windows, use `py -3.12` instead of `python3`; no shell activation is needed.
For a downloaded folder that should be copied to a stable location:

```bash
python3 packaging/setup.py install --backend codex --agent codex --destination "$HOME/.echopath/src"
```

The destination must be empty. Only the public package files are copied; a
source checkout's local credentials, venv, memories and testing files are not
copied. To repair an existing installation, run its own helper in place.

On macOS, add `--app` to build and open the native launcher under `~/Applications`, or
also pass `--app-destination /Applications` if that is your chosen location.
The app asks macOS for Accessibility and Screen Recording when it first opens.
Approve those system-owned prompts, quit and reopen EchoPath, then leave it
running while local tasks run. Codex and Claude Code submit local GUI work to
that authorized menu-bar app rather than directly controlling the desktop.
For runtime-only installation with manual host configuration, use `--agent none`.

Setup creates/reuses `.venv`, installs dependencies, creates
`echopath.local.json`, resolves absolute interpreter/bridge/CLI paths, tests a
real MCP stdio connection, and registers the `echopath` MCP server with the
selected agents. It installs three skills:

| Skill | Purpose |
| --- | --- |
| `echopath-setup` | Install, reconnect, verify, update and repair the local setup. |
| `echopath-vm-setup` | Create or link a VM desktop (macOS with Tart, Linux with Lima, or an existing VM) and verify it. |
| `echopath` | Run and follow desktop tasks through EchoPath. |

Codex uses `~/.agents/skills`; Claude Code uses `~/.claude/skills` (or its
`CLAUDE_CONFIG_DIR`). The setup skill's `origin.json` records the permanent
package path so future repair requests find the correct copy. MCP registration
uses each CLI's supported commands, respecting `CODEX_HOME` or
`CLAUDE_CONFIG_DIR`; it does not append unescaped TOML or replace whole settings
files. These locations follow the official
[Codex skills documentation](https://learn.chatgpt.com/docs/build-skills) and
[Claude Code skills documentation](https://code.claude.com/docs/en/skills).

### Existing installations and conflicts

Rerun the same command to repair an installation, to add `--app` later, or to
update after pulling new code. Existing local configuration and unrelated
settings are retained; skills this installation placed earlier are updated in
place with a backup, and switching between its interpreter and its built app
needs no flag. A standard bridge for the same backend is
updated to this installation's absolute paths. Switching a different/custom
backend requires `--replace-backend`; replacing a conflicting EchoPath MCP entry
or differing installed skill requires `--replace-agent`. Review the conflict
before using these flags. Backups are created beside changed files with private
file permissions. Other MCP servers and agent preferences are retained.

If you already installed EchoPath and only need to connect an agent:

```bash
python3 packaging/setup.py connect --agent codex
python3 packaging/setup.py connect --agent claude
```

`connect` and `doctor` accept `--config /absolute/path/to/config.json` for an
existing custom configuration. The installation command uses
`echopath.local.json` beside its source. The native Mac app also uses that file.

## Verify and start

From the final installed folder:

```bash
python3 packaging/setup.py doctor
python3 packaging/setup.py doctor --model
```

The first check starts an actual MCP stdio server, lists its tools and calls its
smoke check. It does not move the mouse, take screenshots or call a model. The
second adds one small request through the selected model bridge and uses its
quota. Both return a nonzero exit status on failure. A successful import alone
does not establish that the model login, GUI permissions or host reload worked.

Open `~/Applications/EchoPath.app` when built. Otherwise launch the web Control
Room using the installed interpreter:

```bash
.venv/bin/python -m echopath.dashboard --config echopath.local.json
```

Windows:

```powershell
.\.venv\Scripts\python.exe -m echopath.dashboard --config echopath.local.json
```

The Control Room listens on `http://127.0.0.1:8765`. For local macOS desktop
control, approve EchoPath in System Settings → Privacy & Security → Accessibility
and Screen Recording, then quit/reopen it and keep it running. The MCP server
submits local GUI tasks to the app's loopback Control Room, so macOS evaluates
the already-authorized app process rather than Codex's stdio child process.
If the app is not running or either grant is missing, local task start fails
with an actionable authorization message. VM-only use does not need control of
the host's screen. Local Linux/Windows desktop control needs an active user
session; installing the Python package is not a substitute for a desktop.

**Restart Codex or Claude Code completely after installation** before checking
its MCP tools or starting a task. Existing sessions keep their prior MCP
server process and configuration. After restarting, call
`echopath_smoke_check` from that host. Then use `$echopath` in Codex or
`/echopath` in Claude Code for a task. Installing EchoPath does not authorize it
to operate the desktop; each task still needs the user's intended target and
permission to control it.

Fresh installs keep VM integration, JEV and recording off. Configure these later
in **Config**. VM creation and guest installation are covered by the
[VM setup guide](document/vm_setup.md); ask your coding agent to use
`echopath-vm-setup`, or run `.venv/bin/python packaging/setup.py vm -- --help`
for the same steps from a terminal.

## What is included in the download?

The source installation ZIP contains the runtime and web UI, model bridges,
public config template, runtime/agent skills, setup and verification helpers,
Mac launcher sources/icon, license and guides. It includes no credentials,
installed virtual environment, agent login, run history, learned memories,
screenshots, recordings, local machine configuration or testing artifacts.

Build it from a checkout (Git is not required by the builder):

```bash
python3 packaging/build_package.py
```

Output is `dist/EchoPath-<version>-source.zip` plus its SHA-256 checksum. The
archive includes `PACKAGE.json` listing the included file hashes. Checksums
help detect incomplete or changed downloads; they are not a publisher signature.
The allowlist is maintained in `packaging/package-files.json`. Build the ZIP
only after reviewing the included public changes. Distribution is separate from
building; this command does not upload or publish a release.

GitHub's automatic source ZIP also works: extract it and use the same skill.
The Mac DMG is an alternative bootstrap installer; agent-led setup should read
this guide and use the source folder's skill so model/agent choices are explicit.

## Troubleshooting and removal

- **Missing CLI/login:** install the selected CLI and complete its normal login
  flow. Keep credentials in that tool's own store. Rerun `doctor --model`.
- **An existing backend or registration differs:** inspect it and use the
  appropriate replacement flag only when switching that EchoPath installation
  is intended. Preserve backups; do not remove other MCP servers.
- **Different Python/no pip in an old venv:** inspect the environment before
  replacing it. On Debian/Ubuntu, install the matching `python3-venv` package.
- **Mac launcher cannot find libpython:** use a compatible Python 3.12 runtime
  and recreate only the installation's venv after reviewing it. Rebuild the app.
- **MCP works but the host has no tools:** reload the host; inspect
  `codex mcp get echopath --json` or `claude mcp get echopath`. A project-local
  MCP entry or organization policy can override a user entry. Do not silently
  bypass such policy or workspace trust prompts.
- **Moved package:** run setup from its new permanent path, review the old
  paths and use `--replace-backend --replace-agent`. Rebuild the native app too.
- **Verification report:** `local_data/installation.json` records installation
  paths and the initial runtime check. `doctor` prints current results; a prior
  receipt does not prove current health. Detailed runtime logs remain local.
  Failed setup commands/checks report an owner-only diagnostic file in
  `local_data/`; review it locally and redact credentials before sharing it.

To disconnect, use `codex mcp remove echopath` and/or
`claude mcp remove --scope user echopath` for the selected hosts. Remove only the
installed `echopath` and `echopath-setup` skill folders after checking their
`origin.json`. Stop EchoPath before removing its app/source/venv. Review and back
up local configuration, credentials, memories and recordings you want to keep.
Disconnecting EchoPath does not delete a VM or its files.
