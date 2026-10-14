# Install EchoPath with your coding agent

This guide is for the **macOS release branch (`main`)**. For Windows,
use the [other release branch](https://github.com/JackZhao1998/EchoPath-Public/tree/windows-version). Do not mix files from the
two branches in an existing installation.

## With your coding agent

Share this branch or an extracted source folder, then ask:

> Install EchoPath from https://github.com/JackZhao1998/EchoPath-Public.git using branch
> `main` for this coding agent. Read INSTALL.md and follow
> packaging/agents/echopath-setup/SKILL.md from that branch. Use my existing
> model login, include the macOS app, verify setup, and leave VM integration,
> JEV, and recording off unless I ask for them.

For a downloaded folder, supply its absolute path instead of the Git URL.
Choose a permanent location: the app and MCP registration use that folder's
absolute path. The [setup skill](packaging/agents/echopath-setup/SKILL.md) is
included; it does not need a separate installation first.

## Prerequisites and support

- Python 3.10+ with `venv` and pip, available from
  [python.org](https://www.python.org/downloads/) or
  [uv](https://docs.astral.sh/uv/guides/install-python/).
- A local Codex or Claude Code CLI with your normal sign-in. A subscription
  alone does not install the CLI. The model backend and MCP host can differ.
- Network access for dependencies and the optional model check. The source ZIP
  is not an offline Python, model CLI, or VM distribution.
- The optional native app needs macOS 13+, Xcode Command Line Tools
  (`xcode-select --install`), and a Python build providing `libpython`.
  Python 3.12 from python.org or uv is recommended for the app.
- Linux/Windows can use this branch's CLI and web Control Room without `--app`.
  Local Linux control needs a graphical X11 session, Tk, `scrot`, and `xclip`.
  For the Windows tray app and managed Windows guests, select the
  [Windows branch](https://github.com/JackZhao1998/EchoPath-Public/tree/windows-version).

## Install from Git or an extracted folder

Clone into a new permanent folder:

```bash
git clone --branch main --single-branch https://github.com/JackZhao1998/EchoPath-Public.git EchoPath
cd EchoPath
python3 packaging/setup.py install --backend codex --agent codex --app
```

For an existing checkout or extracted source ZIP, run only the installation
command from that folder. The macOS app is placed under `~/Applications`; `--app-destination /Applications` selects a different location.

- Claude Code: use `--backend claude --agent claude`.
- Both MCP hosts: use `--agent both` with one selected backend.
- Runtime only: omit `--app`; `--agent none` skips agent registration.
- Copy an extracted download to an empty permanent folder first:

```bash
python3 packaging/setup.py install --backend codex --agent codex --app --destination "$HOME/.echopath/src"
```

Only the public allowlisted files are copied, including curated example memories.
Local credentials, virtual environments, user-learned memories, and test files
are excluded. The destination must be empty; repair an existing installation
using its own setup helper.

Setup creates a private `.venv`, installs dependencies, writes
`echopath.local.json`, resolves absolute bridge/interpreter paths, verifies a
real MCP stdio connection, and registers EchoPath with the selected hosts.
It installs `echopath-setup`, `echopath-vm-setup`, and `echopath` skills.
Codex skills use `~/.agents/skills`; Claude Code skills use `~/.claude/skills`
or `CLAUDE_CONFIG_DIR`. MCP configuration respects `CODEX_HOME` and
`CLAUDE_CONFIG_DIR`. Other MCP entries and agent settings are retained.

## Start the app and verify

Open `~/Applications/EchoPath.app`. Grant **EchoPath** Accessibility and Screen
Recording in System Settings, quit and reopen it, and keep it running for local
GUI work. MCP submits local desktop tasks through the authorized app's loopback
Control Room. VM-only work does not need host desktop permissions. See the
[VM guide](document/vm_setup.md) for optional guest setup.

**Fully quit and restart your coding agent after setup** so it loads the new
MCP registration. From the installed folder:

```bash
python3 packaging/setup.py doctor
python3 packaging/setup.py doctor --model
```

The first command starts the MCP server, lists its tools, and runs a smoke check
without controlling the desktop or calling a model. The second sends one small
request through your model bridge and uses its quota. Both fail with a nonzero
exit status when verification fails. After restarting the host, call
`echopath_smoke_check` there to verify that it loaded the new server.

For CLI/web-only use, launch the Control Room from the installed folder:

```bash
.venv/bin/python -m echopath.dashboard --config echopath.local.json
```

It listens at `http://127.0.0.1:8765`. Use `$echopath` in Codex or `/echopath` in
Claude Code for a subsequent task. Installation does not grant permission for
arbitrary desktop tasks. VM integration, JEV, and recording start disabled.

## Updates, repair, and conflicts

Pull updates for this branch, stop/reopen EchoPath, and rerun the same setup
command. It reuses the environment and preserves local settings and data.
Do not move the permanent folder without reconnecting its agents and app.

If a different backend or another checkout owns an existing EchoPath entry,
inspect the reported conflict. `--replace-backend` replaces a different backend;
`--replace-agent` replaces conflicting EchoPath registration/skills. The helper
creates backups and preserves unrelated settings. Do not clear whole agent
configuration files or bypass organization/workspace restrictions.

To connect an installed copy without reinstalling:

```bash
python3 packaging/setup.py connect --agent codex
python3 packaging/setup.py connect --agent claude
```

`connect` and `doctor` accept `--config /absolute/path/to/config.json` for custom
configuration. Standard installations use `echopath.local.json`.

## What is included in the download?

The custom source ZIP includes runtime/web UI, model bridges, public config,
skills, platform launcher sources, license, guides, and four curated macOS
Stocks/Weather example records with their small crop assets. It also includes the README branding, demo videos, posters, captions, and companion commands under `document/assets/` and `media/`.

It excludes local credentials, agent logins, installed environments, private
task history/screenshots/recordings, user-learned memories, test suites, and
retired developer/benchmark material.

Build and verify from the source root:

```bash
python3 packaging/release_check.py --output dist
```

This validates the public files, builds the source ZIP, checks file hashes and
extraction, and exercises the `--destination` copy path in a temporary folder.
It does not install the app or publish a release. See [RELEASE.md](RELEASE.md)
for automated and manual release checks. The ZIP contains `PACKAGE.json`, and
the builder writes a SHA-256 checksum beside it. Checksums detect changed or
incomplete downloads; they are not publisher signatures.

GitHub's automatic source ZIP also works when downloaded from this branch.

## Troubleshooting and removal

- Missing CLI/login: install the selected CLI and finish its normal login,
  then rerun `doctor --model`. Keep credentials in the provider's own store.
- An incomplete environment: inspect `.venv` before recreating it. On Debian,
  install the matching `python3-venv` package.
- Missing MCP tools after setup: restart the host and inspect
  `codex mcp get echopath --json` or `claude mcp get echopath`. Project-level or
  organization settings may override the user-level registration.
- Verification failures: inspect the private diagnostic under `local_data/`.
  Redact credentials and private task data before sharing any logs. A previous
  `local_data/installation.json` receipt does not prove current health.
- Mac launcher cannot find `libpython`: use Python 3.12 from python.org/uv,
  recreate only the affected installation's environment, and rebuild the app.

To disconnect, use `codex mcp remove echopath` or
`claude mcp remove --scope user echopath` for the chosen hosts. Check each
installed skill's `origin.json` before removing EchoPath skill folders. Stop
EchoPath before removing its app/source/venv; back up local configuration,
credentials, memories, and recordings you want to keep. Disconnecting does
not delete a VM or its files.
