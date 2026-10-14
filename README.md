# EchoPath

EchoPath is a local-first runtime that lets a coding agent do GUI work on your
own desktop and learn from it. It plans a task with the model backend you
already have, drives the screen through PyAutoGUI, records what worked, and
replays learned workflows later with visual re-aiming instead of planning from
scratch.

This repository packages the core runtime and exposes it as a
[Model Context Protocol](https://modelcontextprotocol.io) (MCP) server, so hosts
such as Claude Desktop, Claude Code, Codex, or Cursor can call EchoPath as a
local tool provider.

- **Bring your own backend.** EchoPath never ships credentials. It talks to a
  model through a small "bridge" process that reuses the login you already have
  for Claude Code, Codex, Kimi, Ollama, or any local command.
- **Safe by default.** Every MCP task runs with live desktop control switched
  off unless the caller opts in for that one call. The model backend runs
  read-only; EchoPath owns the desktop, shell, and file action boundary.
- **Learns locally.** Successful GUI runs are consolidated into a replayable
  memory library on disk. Nothing is uploaded anywhere except the prompts and
  screenshots you send to your chosen backend.

## Contents

- [Demo: United Airlines flight search](#demo-united-airlines-flight-search)
- [Requirements](#requirements)
- [Installation](#installation)
- [First-time setup with a coding agent](#first-time-setup-with-a-coding-agent)
- [Choose and authenticate a model bridge](#choose-and-authenticate-a-model-bridge)
- [Per-machine configuration](#per-machine-configuration)
- [Connect an MCP host](#connect-an-mcp-host)
- [Verify the installation](#verify-the-installation)
- [Using the MCP surface](#using-the-mcp-surface)
- [Desktop control and platform notes](#desktop-control-and-platform-notes)
- [Memory and learning](#memory-and-learning)
- [Command-line use](#command-line-use)
- [Security and privacy model](#security-and-privacy-model)
- [Runtime outputs](#runtime-outputs)
- [Project layout](#project-layout)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [License](#license)

## Demo: United Airlines flight search

Watch EchoPath replay a flight search from a plain-language instruction in Codex.

https://github.com/user-attachments/assets/3d2c136a-d8bb-44ba-bb24-907043896253

This example uses **Codex GPT-6 Astra with native computer use** and
**Codex GPT-6 Astra with EchoPath MCP** for the same flight search:

> Open Chrome from the Windows desktop, go to United Airlines, and search for
> a one-way flight from Washington Dulles (IAD) to San Francisco (SFO) on
> November 10, 2026, for 2 adults and 1 child aged 2–17 in Business or First.
> Stop at the loaded flight listings.


| Flight search run | Execution time | Recorded tokens | API token estimate |
| --- | ---: | ---: | ---: |
| Native computer use | 380.1 s | 6,093,237 | $8.37 |
| EchoPath first pass | 712.3 s | 1,246,999 | $5.66 |
| EchoPath learned replay | 64.9 s | 18,181 | $0.19 |

These are individual observations recorded on September 5, 2026 (UTC). Estimates use [standard GPT-6 Astra pricing](https://developers.openai.com/api/docs/models/gpt-6-astra)

## Requirements

| Requirement | Notes |
| --- | --- |
| Python 3.10 or newer | Tested on 3.13. |
| One model backend | An authenticated `codex` or `claude` CLI, a `kimi` CLI, a local Ollama server, or any local command that accepts a prompt on stdin. See [Choose and authenticate a model bridge](#choose-and-authenticate-a-model-bridge). |
| An MCP host | Claude Desktop, Claude Code, Codex CLI, Cursor, or any client that launches stdio MCP servers. Optional if you only use the `echopath` CLI. |
| Desktop permissions | macOS asks for Accessibility and Screen Recording permission the first time EchoPath controls the screen. Windows needs nothing extra. |
| Node.js | Only if you use the Claude Code bridge (the CLI is an npm package) or the Codex SDK bridge. |

## Installation

### Let your coding agent install it

Give Codex or Claude Code this repository link (or an extracted source folder)
and ask it to read [INSTALL.md](INSTALL.md) and follow the bundled
[echopath-setup skill](packaging/agents/echopath-setup/SKILL.md). The agent installs
the runtime, selects your model bridge, connects MCP and the usage skills, and
verifies the setup using your existing login.

> Install EchoPath from https://github.com/JackZhao1998/EchoPath for this coding
> agent. Follow INSTALL.md and packaging/agents/echopath-setup/SKILL.md. Use my
> existing model login and leave VM integration off unless I ask for it.

The same workflow works from a downloaded folder. See [INSTALL.md](INSTALL.md)
for commands, supported platforms, conflicts, and verification. To prepare a
source installation ZIP with the guides and skills, run
`python3 packaging/build_package.py`; the output goes to `dist/` and excludes
credentials, local configuration and runtime data.

### First-time setup with a coding agent

Use this short path when you want a coding agent to prepare the host, the
optional JEV/OpenRouter helper, and an isolated macOS desktop. Keep the cloned
folder in a permanent location: the installed app and MCP registration use its
absolute path.

1. Ask Codex or Claude Code to install EchoPath with the prompt above. For a
   Mac that will control its own desktop, ask it to include the native app. The
   installer creates a private `.venv`, a gitignored `echopath.local.json`,
   connects the MCP server, and verifies the model bridge with the login you
   already use. Restart the coding agent completely after registration so it
   discovers the new MCP tools.
2. Open **EchoPath.app**. When macOS asks, approve **Accessibility** and
   **Screen Recording** for **EchoPath**—not for the coding agent or terminal.
   Quit and reopen the app after granting either permission, then keep it
   running for local desktop tasks. If no prompt appears, check **System
   Settings → Privacy & Security**; macOS may already have a grant for the app.
3. To use the optional JEV replay helper, open the local Control Room's
   **Config** page, save and verify an OpenRouter token, then enable JEV. The
   token is stored with private permissions in the ignored
   `credentials/api_keys.yaml` file (or supplied as `OPENROUTER_API_KEY` in the
   app's environment); it is never returned to the browser. JEV is off by
   default and sends on-screen text to OpenRouter, so enable it only when that
   is appropriate for the task.
4. For an isolated macOS desktop, ask the coding agent to use
   `echopath-vm-setup` to create a Tart guest. This needs an Apple-silicon Mac,
   roughly a 25–30 GB download, 50 GB of disk, and 8 GB of memory while the VM
   runs. After your explicit approval for those resources, it installs Tart,
   clones and starts an `echopath-` VM, links it with `vm tart-link`, and runs
   `vm install` to add the guest bridge. The full command sequence is in the
   [Tart guide](document/vm_setup.md#create-a-macos-vm-with-tart).
5. In the VM window, grant the guest bridge's Python runtime **Accessibility**
   and **Screen Recording** permissions separately from the host app's grants.
   Run `vm restart-bridge`, inspect `vm status`, and capture a screenshot until
   the guest reports both permissions and **Desktop ready**. The bridge is then
   connected through an SSH tunnel; model credentials, history, and learned
   procedures remain on the host.

The setup agent should never ask for or accept passwords, API keys, or SSH key
contents in chat. A VM is optional: leave VM integration off if local desktop
control is sufficient. See [INSTALL.md](INSTALL.md) for the complete host setup
and [Optional VM setup](document/vm_setup.md) for connection, bridge, and
troubleshooting details.

### Mac app

On macOS, the easiest route is the menu-bar app. Open
`EchoPath-Installer.dmg`, right-click **Install EchoPath** → **Open**, and follow
the Terminal window. From a checkout you can run the same installer directly:

```bash
packaging/macos/install.sh
```

It sets up Python with [uv](https://docs.astral.sh/uv), installs EchoPath,
creates `echopath.local.json` from the template if you don't have one, and
builds `EchoPath.app` into `/Applications`. The app is a small native launcher
compiled on your Mac that runs the checkout's virtualenv, so macOS lists
**EchoPath** by name under Accessibility and Screen Recording rather than your
terminal. Code changes apply the next time you open the app. Rebuild with
`python packaging/macos/make_app.py` after moving the checkout; an unchanged
build is skipped so your permission grants survive. Build the DMG with
`packaging/macos/build_dmg.sh`.

The CLI and MCP server below work the same way with or without the app.

### From source

Clone the repository and install it into a dedicated virtual environment. Use a
dedicated environment rather than a system or Anaconda base interpreter; see
[Troubleshooting](#troubleshooting) for why.

macOS / Linux:

```bash
git clone https://github.com/JackZhao1998/EchoPath.git
cd EchoPath
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .
```

Windows (PowerShell):

```powershell
git clone https://github.com/JackZhao1998/EchoPath.git
cd EchoPath
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e .
```

The install adds these console scripts to the environment:

| Command | Purpose |
| --- | --- |
| `echopath-mcp` | Runs the MCP server (stdio by default). |
| `echopath` | Sends one task to the runtime from a terminal, without an MCP host. |
| `echopath-dashboard` | Opens the local Control Room (and optionally the Mac menu bar). |
| `echopath-vm-agent` | Runs a desktop bridge inside a prepared guest. |

Confirm the package imports:

```bash
python -c "import echopath; print('EchoPath import OK')"
```

This check needs no credentials, network access, or desktop permissions.

## Choose and authenticate a model bridge

EchoPath calls its model backend through an **external command bridge**: a
small script that reads one JSON request on stdin and writes one JSON response
on stdout. Bridges reuse whatever login the backend already has on this
machine. The full request and response contract is in
[`bridges/README.md`](bridges/README.md).

Pick exactly one bridge and point `external_command.command` at it. The
tracked [`echopath.config.json`](echopath.config.json) defaults to the
dependency-free Codex CLI bridge.

| Bridge | Backend | How it authenticates | Extra setup |
| --- | --- | --- | --- |
| `bridges/codex_cli/` | `codex` CLI | The CLI's own login state (`codex login`). | Install the Codex CLI. |
| `bridges/claude_code/` | `claude` CLI | Claude Code subscription login, **or** `ANTHROPIC_API_KEY` from an auth file. | `npm install -g @anthropic-ai/claude-code` |
| `bridges/codex_sdk/` | `@openai/codex-sdk` | Local Codex auth. | `cd bridges/codex_sdk && npm install` |
| `bridges/kimi/` | `kimi` CLI | The CLI's own login state. | Install Kimi Code. |
| `bridges/ollama/` | Local Ollama server | None. Everything stays on the machine. | Install Ollama and pull a model. |
| `bridges/opencode/` | Any OpenCode-style command | That tool's own local auth. | Set `ECHOPATH_OPENCODE_COMMAND`. |
| `bridges/generic_command/` | Any command that reads a prompt on stdin | That command's own environment. | Set `ECHOPATH_GENERIC_COMMAND`. |
| `bridges/_template/` | Scaffold for a new backend | Auth-file loader included. | Copy and edit; see its README. |

Every bridge has its own README with the flags it sets and the environment
overrides it accepts.

### How credentials reach a bridge

The MCP server inherits the environment of the host that launched it. A
desktop app such as Claude Desktop typically carries no API keys and a much
shorter `PATH` than your shell, so `export ANTHROPIC_API_KEY=...` in a terminal
does not reach a server the app started. EchoPath offers three ways to close
that gap, in order of preference:

1. **Use the backend's existing login.** The Codex, Claude Code, and Kimi CLIs
   store their own login state after you run them once interactively. Bridges
   pick that up with no further configuration. This is the recommended path.
2. **Put a key in an auth file under `credentials/`.** The folder is
   gitignored. Name the file after the bridge folder, for example
   `credentials/claude_code.json`. JSON and `KEY=value` formats are both
   accepted:

   ```json
   { "env": { "ANTHROPIC_API_KEY": "sk-ant-..." } }
   ```

   The Claude Code bridge and the bridge template read auth files through
   `load_auth_env()`. The lookup order is `ECHOPATH_<BRIDGE>_AUTH_FILE`, then
   `ECHOPATH_AUTH_FILE`, then `credentials/<bridge>.json`, then
   `credentials/<bridge>.env`. A variable already present in the environment
   always wins over the file. Full rules:
   [`bridges/_template/README.md`](bridges/_template/README.md).
3. **Set variables in the config's `env` block.** Anything under
   `external_command.env` is added to the bridge's environment. Use this for
   non-secret tunables such as `OLLAMA_MODEL` or a `PATH` entry. Do not put
   secrets here if the config file is tracked.

Never commit a real credential. `credentials/*`, `.env*`, and
`echopath.local.json` are ignored by Git for this reason.

### Claude Code bridge

Install the CLI and point the config at the bridge:

```bash
npm install -g @anthropic-ai/claude-code
```

```json
{
  "external_command": {
    "command": ["python", "bridges/claude_code/run_claude_code.py"]
  }
}
```

Then authenticate one of two ways:

| Credential | Bridge mode | Setup |
| --- | --- | --- |
| Claude Code subscription | standard | Run `claude` once interactively and log in. Nothing else is needed. |
| `ANTHROPIC_API_KEY` | `--bare` | Create `credentials/claude_code.json` as shown above. |

With an API key present, the bridge switches to `--bare`, which skips hook,
plugin, MCP, and `CLAUDE.md` discovery so the run is reproducible and the
repository being worked on cannot steer the backend. The bridge pins the model
to `claude-opus-5` and runs read-only with only the `Read` tool allowed, so
the backend can look at screenshots but cannot act. Details and every override:
[`bridges/claude_code/README.md`](bridges/claude_code/README.md).

### Codex CLI bridge

Install the Codex CLI, run `codex login` once, and keep the default config.
The bridge runs Codex with `--sandbox read-only` and approval policy `never`.
Override the executable or sandbox through `ECHOPATH_CODEX_*` variables listed
in [`bridges/codex_cli/README.md`](bridges/codex_cli/README.md).

### Ollama bridge

No credentials are involved. Start Ollama, pull a model, and set the model name
in the config's `env` block so it reaches the bridge when an MCP host launches
the server:

```json
{
  "external_command": {
    "command": ["python", "bridges/ollama/run_ollama.py"],
    "env": { "OLLAMA_MODEL": "llama3.1", "OLLAMA_HOST": "http://127.0.0.1:11434" }
  }
}
```

### Kimi, OpenCode, and generic commands

These bridges defer entirely to the backend's own local auth. Set the command
in `external_command.command` and pass any required variables through the
`env` block; see the README in each bridge folder.

### Other routing modes

`model_routing.mode` also accepts `api`, `codex`, `claude`, `kimi`, and
`hybrid`, which use in-process routers instead of a bridge. The `api` mode
reads `OPENAI_API_KEY` from the environment by default. Bridges are the
recommended path for MCP use because they keep credentials out of the server
process. See [`document/model_routing.md`](document/model_routing.md) and
[`document/authentication.md`](document/authentication.md).

## Per-machine configuration

Keep the tracked `echopath.config.json` public and credential-free. For a real
installation, copy it to `echopath.local.json`, which is gitignored, and edit
the copy:

```bash
cp echopath.config.json echopath.local.json
```

Two rules make an MCP-launched server reliable:

- **Use absolute paths.** The host's `PATH` may not include your virtual
  environment or the npm global bin directory. Point `command[0]` at the
  interpreter inside `.venv` and pass an absolute `--config` when registering
  the server.
- **Set `working_directory`** to the repository root so bridge paths resolve.

Example for Windows:

```json
{
  "model_routing": {
    "mode": "external_command",
    "router_name": "default",
    "external_model": "default"
  },
  "external_command": {
    "command": [
      "C:\\Users\\<you>\\EchoPath\\.venv\\Scripts\\python.exe",
      "C:\\Users\\<you>\\EchoPath\\bridges\\claude_code\\run_claude_code.py"
    ],
    "working_directory": "C:\\Users\\<you>\\EchoPath",
    "timeout_seconds": 300,
    "env": {}
  },
  "desktop": {
    "provider": "local",
    "reasoning_effort": "medium",
    "max_iterations": 60,
    "pause_seconds": 0.1,
    "fail_safe": true,
    "screenshot_directory": "local_data/desktop_observations"
  },
  "skills": {
    "available": ["pyautogui-desktop", "command-line", "workspace-files"],
    "enabled": ["pyautogui-desktop", "command-line", "workspace-files"]
  }
}
```

On macOS or Linux, use `/Users/<you>/EchoPath/.venv/bin/python` and forward
slashes.

The `skills.enabled` list controls which capabilities the runtime may use at
all. Remove `command-line` or `workspace-files` if the host should never be
able to run shell commands or touch files through EchoPath.

## Connect an MCP host

The server speaks stdio by default. Register it with the absolute path to the
`echopath-mcp` script inside your virtual environment and the absolute path to
your local config.

### Claude Code and Codex, with the EchoPath skills

From an installed folder, register the selected host (user scope) and install
the setup and desktop-task skills:

```bash
python3 packaging/setup.py connect --agent codex
# Or: --agent claude / --agent both
```

In a new session, use `$echopath` in Codex or `/echopath` in Claude Code and
describe a desktop task. The skill has
the agent use EchoPath instead of its own computer-use tools: it checks
`echopath_list_procedures` for a stored procedure to replay, starts a tracked
run with `echopath_start_task` (asking before it takes over this Mac), follows
it with `echopath_get_task`, and can pause or stop it. Runs appear in the
Control Room and the menu bar like any other. Identical registrations are reused;
conflicting entries are reported and preserved. See [INSTALL.md](INSTALL.md) for
reviewed replacement and repair. The existing `packaging/agents/connect.sh`
entry point delegates to the same helper.

### Claude Desktop

Edit the Claude Desktop config file:

| OS | Path |
| --- | --- |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |

```json
{
  "mcpServers": {
    "echopath": {
      "command": "/Users/<you>/EchoPath/.venv/bin/echopath-mcp",
      "args": ["--config", "/Users/<you>/EchoPath/echopath.local.json"]
    }
  }
}
```

On Windows the command is
`C:\\Users\\<you>\\EchoPath\\.venv\\Scripts\\echopath-mcp.exe`. Restart Claude
Desktop after saving. The server appears under the name `echopath`.

### Claude Code

```bash
claude mcp add echopath -- /Users/<you>/EchoPath/.venv/bin/echopath-mcp --config /Users/<you>/EchoPath/echopath.local.json
```

Add `--scope user` to make it available in every project. Run `claude mcp list`
to confirm it connected. Restart Claude Code after changing an existing MCP
configuration.

### Codex CLI

Add the server to `~/.codex/config.toml`:

```toml
[mcp_servers.echopath]
command = "/Users/<you>/EchoPath/.venv/bin/echopath-mcp"
args = ["--config", "/Users/<you>/EchoPath/echopath.local.json"]
```

Newer Codex releases also accept `codex mcp add echopath -- <command> <args>`.
Restart Codex after adding or changing the server. For asynchronous tracked
work, call `echopath_start_task` with `target: "vm"` and
`allow_desktop_control: true`, then poll `echopath_get_task` or use
`echopath_control_run` to pause, resume, or stop it. The Control Room and
menu-bar tracker show the same task ID and progress.

### Cursor and other hosts

Most hosts accept the same `mcpServers` JSON shape as Claude Desktop. For
Cursor, put it in `.cursor/mcp.json` in the project or `~/.cursor/mcp.json`
globally.

### HTTP transports

For hosts that connect over HTTP rather than launching a process, the server
also offers `streamable-http` and `sse`:

```bash
echopath-mcp --config echopath.local.json --transport streamable-http --host 127.0.0.1 --port 8000
```

These transports carry no authentication of their own. Keep them bound to
`127.0.0.1` or place them behind something that does.

## Verify the installation

From the host, call `echopath_smoke_check` with default arguments. It confirms
the package imports and the config parses, and it reports the routing mode,
enabled skills, and desktop provider. It does not contact the model backend.

Then call `echopath_smoke_check` with `include_model_bridge=true`. This sends
one trivial prompt through the configured bridge and reports
`model_bridge_ok: true` when the backend answered. A failure includes the
bridge's stderr, which names the missing login, executable, or auth file.

Finally, run a task with desktop control still off:

```text
echopath_run_task(message="List the files in this workspace")
```

## Using the MCP surface

### Tools

| Tool | What it does |
| --- | --- |
| `echopath_create_session` | Creates a chat session and returns its `session_id`. |
| `echopath_run_task` | Sends one task. `allow_desktop_control` defaults to `false`; set it to `true` for a call that may observe and act on the screen. Pass `session_id` to continue a conversation. |
| `echopath_resume` | Resumes a native segment using its session ID, single-use continuation token and declared outputs, or cancels it with `cancel=true`. |
| `echopath_get_session` | Returns a session's messages and debug state. |
| `echopath_get_status` | Returns the projected run status for a session. Useful for polling a long GUI task. |
| `echopath_list_sessions` | Lists sessions held in this server process. |
| `echopath_list_runs` | Lists ongoing, paused and recent runs across workers in this checkout. |
| `echopath_control_run` | Requests `pause`, `resume`, or `stop` for a run ID. |
| `echopath_smoke_check` | Health checks; calls the bridge only when `include_model_bridge=true`. |
| `echopath_consolidate` | Consolidates a recorded GUI run into memory, or deprecates a memory path. |
| `echopath_list_memories` | Lists learned memories from both stores. |
| `echopath_clear_history` | Clears sessions and local history artifacts. Requires `confirm=true`. |

### Resources and prompts

Resources: `echopath://config`, `echopath://skills`, `echopath://sessions`,
`echopath://sessions/{session_id}`, `echopath://sessions/{session_id}/debug`,
and `echopath://memory/consolidated/index`. The config resource is normalized
and credential-free.

Prompts: `echopath_plan_task` plans without acting,
`echopath_visible_desktop_task` frames an explicitly authorized desktop task,
and `echopath_replay_diagnostics` inspects replay decisions for a session.

Architecture and the safety gate are described in
[`document/mcp_architecture.md`](document/mcp_architecture.md).

## Desktop control and platform notes

On Windows, local MCP desktop runs show a small always-on-top status box at
the top center of the primary screen. It displays the current action, elapsed
time, and a **Stop** button without taking keyboard focus. Stop prevents further
desktop actions; an action or model request already in progress may finish
before the run returns. The box stays visible during screenshots and pointer
actions, with status updates applied in place to avoid flashing between steps.
It can appear in captured images and covers the small area beneath it. It closes
when execution finishes or fails, before memory consolidation starts.
Remote desktop runs and other platforms do not show this local Windows box.
The overlay uses Python's bundled Tkinter; if it is unavailable, desktop runs
continue without the box.

When `allow_desktop_control=true`, EchoPath takes screenshots, plans the next
step with the model backend, grounds click targets visually, and acts through
PyAutoGUI in a loop until the task completes or `desktop.max_iterations` is
reached. Watch the screen while it runs. PyAutoGUI's fail-safe is on by
default: slam the mouse into a screen corner to abort.

- **macOS** is the primary target. Grant Accessibility and Screen Recording
  permission to the terminal or app that launches the server. The bundled
  example memories use macOS Stocks, Clock, and Weather.
- **Windows** works for screenshot, click, type, scroll, and keyboard actions.
  Install with `--app` for system tray controls and the browser Control Room.
  A managed Windows guest can run in a visible VirtualBox window; see the
  [Windows setup guide](document/windows_setup.md).
  A few helpers such as menu-bar clicks and app activation by name are
  currently macOS-only and report a clear error elsewhere.
- **Remote desktops.** `desktop.provider` also accepts `amazon-vm` and
  `local-vm`, which drive a desktop through the HTTP agent in
  `echopath.amazon_vm_agent`. That agent is off by default and requires you to
  set `desktop.amazon_vm_endpoint` and a shared key in the variable named by
  `desktop.amazon_vm_api_key_env`.

Web tasks are performed through the visible desktop browser. The runtime does
not bundle Selenium or selector-based automation.

## Memory and learning

Consolidation can mark semantic segments for `replay`, `native`, or `conditional`
execution. A host with computer tools on the same desktop opts in with
`allow_native_handoff=true` on `echopath_run_task`. Replay returns
`status: "awaiting_native"` at a native boundary; the host completes that subgoal
and calls `echopath_resume`. EchoPath verifies the live return state before
continuing. Existing memories remain compatible. See
[segmented memory replay](document/native_segments.md) for contracts, lifecycle,
recording limits and the Amazon paper towel / bottled water test scenario.

Successful GUI runs are written to `local_data/gui_runs/` and consolidated
automatically into a memory library under `local_data/memory_repo/` using the
configured bridge. Consolidation keeps the essential steps, marks task-specific
typed values as flexible inputs, and cuts small component crops from the run's
own screenshots. A later request that matches replays with visual re-aiming and
rebound values instead of planning from scratch.

- Set `ECHOPATH_AUTO_CONSOLIDATE=0` to record runs without learning from them.
- If a replayed step can't be found on screen (the page changed, or a stored
  crop no longer matches), the run drops the replay and plans the rest of the
  task live, with step and goal verification switched back on. Set
  `desktop.replay_fallback_to_planning` to `false` to fail such runs instead.
- Optionally, a cheaper step can run before that vision fallback. With
  `desktop.replay_decision_model` set to `true` (macOS only), EchoPath reads the
  screen's text with local OCR and asks the Jev decision model on OpenRouter
  which text the step meant, clicking only when Jev's confidence is at least
  `replay_decision_model_min_confidence` (0.9). It helps with links, labels and
  fields, not icon-only buttons. It needs an OpenRouter key in
  `OPENROUTER_API_KEY` (or the variable named by
  `replay_decision_model_api_key_env`), and it sends the on-screen text to
  OpenRouter, so it is off by default.
- `echopath_consolidate` re-runs consolidation for a run id or deprecates a
  learned path; `echopath_list_memories` shows what has been learned.
- `memory/consolidated/` holds curated, tracked example memories with the crops
  they need. See [`memory/consolidated/README.md`](memory/consolidated/README.md)
  and the prompts in
  [`examples/local_gui_memory_pack/TASKS.md`](examples/local_gui_memory_pack/TASKS.md).

## Command-line use

The `echopath` script runs one task without an MCP host, using the same config:

```bash
echopath --config echopath.local.json "List the files in this workspace"
```

Add `--progress` to see each planning and execution update while the task is
running, or `--json` to print the full runtime response. Press `Ctrl+C` to
stop a task cleanly. The CLI runs with the full
skill set from the config, including desktop control if `pyautogui-desktop` is
enabled, so review the config before pointing it at a GUI task.

### Local Control Room

For visible browser and desktop work, start the local dashboard instead of
waiting on a silent terminal:

```bash
echopath-dashboard --config echopath.local.json --tray
```

It opens `http://127.0.0.1:8765` and adds **EchoPath** to the macOS menu bar or
Windows system tray. Install the native dependencies with
`pip install -e ".[macos]"` or `pip install -e ".[windows]"` respectively.
Omit `--tray` on Linux, or add `--no-open` to keep the browser closed at
startup. The Control Room binds only to loopback.

- **Desktops** shows one card per target (this computer and the connected VM) with
  its active run, controls, and an activity log. A desktop that already has an
  active or paused run can't be picked for a new task.
- **Open VM** opens a separate window with a live view of the guest desktop.
  Choose **Control VM** to click, double-click, right-click, drag, scroll and
  type into the guest. **Send text** pastes text, and **Send key** handles
  shortcuts that the host browser might intercept. Manual input works while
  the VM is idle or its task is fully paused; use **Pause task** in the viewer
  to take over a running task. Resuming waits for any input in progress, then
  observes the changed desktop again. **Release control**, pausing the view,
  or switching tabs turns off input. Full screen and view pause/resume remain
  available independently of task execution. Dragging requires the current
  EchoPath guest bridge. The viewer shows task progress, including when it is
  waiting for the model, and keeps the last result visible after the VM resets.
- **Config** opens a separate settings window. Save and verify an OpenRouter
  token, remove it, or switch the JEV replay helper on and off. JEV starts off
  and can only be enabled after token validation; a failed token check or
  removing the token disables it. Tokens stay in the ignored
  `credentials/api_keys.yaml` with private file permissions and are never
  returned to the browser. An environment-provided token takes precedence.
  **Test Codex** and **Test Claude Code** send a small request through the
  bundled CLI bridges using the existing login and report whether a reply
  comes back. These tests use model quota. JEV switch changes apply to new tasks.
- **Virtual machine** in Config is optional and starts off for new installs.
  Save an SSH connection to an existing guest, create a Linux desktop on macOS
  with Lima, create a Windows desktop on Windows with VirtualBox, or use an
  existing bridge URL. Config can install the guest bridge,
  manage the SSH tunnel, and check that the desktop is ready. See the
  [VM setup guide](document/vm_setup.md).
- **Task recording** in the task composer, menu-bar popover, or Config enables
  or disables video capture for new VM tasks. The preference is saved locally;
  changing it does not interrupt a recording already in progress. Videos are
  saved on the Mac under `local_data/recordings/` and can be played or downloaded
  from a run's details. Recording uses `screencapture` on macOS guests or FFmpeg
  with X11 and the `libx264` encoder on Linux guests. Only the guest desktop is
  captured; task history and learned procedures are independent of this switch.
  Video recording currently requires a macOS host; Windows guests are not yet
  supported by the recorder.
- **Automatic VM reset**, when configured, restores a clean snapshot after
  each VM task, including failures and stops. The desktop stays reserved until
  restoration and the authenticated desktop check finish. Recordings and task
  history on the host survive; the viewer reconnects automatically.
- **Stored procedures** lists everything EchoPath has learned: paths in the
  memory repository and records in the consolidated replay library. Open one
  to see its parameters, replay phrase, and every stored step with its expected
  outcome. **Use as task** copies the replay phrase into the task box; review
  it and start the task yourself, since desktop control is chosen per task.
- The menu-bar icon shows elapsed time while a run is working, and **Paused**,
  **Done**, or **Failed** when that needs your attention. Clicking it opens a
  small popover with the same controls, a quick task box, and your latest
  stored procedures.

- **Ongoing & paused** lists live workers, including CLI and MCP runs using
  this checkout. **All recent runs** includes completed, stopped, and failed
  runs, retained across dashboard restarts.
- **Pause** changes the state to **Pausing** until the current action or
  model call reaches a checkpoint. Only **Paused** means the desktop has
  been released. An in-flight model call can take up to its configured timeout.
- **Resume** keeps the task goal but takes a fresh observation and plans again;
  old screen coordinates and pending replay actions are discarded.
- **Stop** is cooperative too; a paused run stops at once, while an in-flight
  action/model call is allowed to return. Paused workers must stay alive:
  this is not restoration after a machine reboot or worker crash.
- Opening the menu-bar popover requests a pause for local work. Status,
  Pause/Resume, and Stop are available there without switching browser windows.
- One run at a time owns each target in this checkout, including while paused.
  A local run and a run on a distinct VM endpoint may coexist. Multiple aliases
  for one VM should not be used as separate targets.

Closing the browser or quitting the menu-bar app leaves workers running. Reopen
the Control Room to reconnect. Stop workers before terminating the terminal
that owns a directly launched CLI run.

### Task control API and MCP

The loopback-only Control Room also exposes a task API. It uses the same
tracker shown in the browser and macOS menu-bar app, so a task started by Codex
appears alongside dashboard and CLI runs.

```text
POST /api/tasks                         create a task; returns its task record and ID
GET  /api/tasks                         list tracked tasks
GET  /api/tasks/{task_id}               inspect status, activity, and final result
POST /api/tasks/{task_id}/pause         request a cooperative pause
POST /api/tasks/{task_id}/resume        resume from a fresh observation
POST /api/tasks/{task_id}/stop          request a cooperative stop
```

Create bodies contain `task`, `desktop`, and `endpoint`. Set `endpoint` to the
VM bridge URL to target the guest. The API binds to `127.0.0.1` only.

Codex MCP clients can call `echopath_start_task` for the same asynchronous
workflow, then use `echopath_get_task`, `echopath_list_runs`, and
`echopath_control_run`. `echopath_start_task` defaults to the VM target but
requires `allow_desktop_control: true` for any desktop automation.

EchoPath masks its menu-bar item and its own popover window before saving local
screenshots. Masking retains screenshot coordinates; the covered pixels are
unavailable to the model. It does not hide arbitrary browser windows or affect
third-party screen recorders. A VM capture contains only the guest desktop,
so the host's entire Control Room and menu bar remain outside that capture.

### Run on a virtual machine

Open **Config → Virtual machine → Set up or edit a VM connection**. Choose:

- **Connect an existing VM with SSH:** save a host or SSH alias, test access,
  then install the bridge or connect to one that is already running.
- **Create a Linux VM on this Mac:** install Lima, save the VM settings, then
  choose **Create VM & connect**. EchoPath prepares an Ubuntu 24.04 desktop
  and browser. This first creation option requires macOS.
- **Create a Windows VM with VirtualBox:** on a Windows host, install VirtualBox,
  choose an official Windows x64 ISO and save the VM settings, then choose
  **Create VM & connect**. EchoPath installs Windows, Guest Additions, Python
  and an interactive desktop bridge. **Open VM window** lets you watch and
  interact with the console. Pause an active EchoPath task before taking over.
  See the [Windows setup guide](document/windows_setup.md).
- **Connect an existing desktop bridge:** save its URL and, when needed, its
  key under Advanced connection settings.

The connection shows **Ready** only after checking desktop access and a valid
screenshot, including key authentication when the bridge requires it. Choose
**Open desktop** to view it, then select **Virtual
machine** in the Control Room to run a task. The saved endpoint is selected
automatically. The SSH tunnel survives closing the Config window; turning the
integration off closes EchoPath's connection and preserves the VM and its files.

Model login, task history, and learned procedures stay on the host. Screenshots,
keyboard, and pointer actions go to the guest, and host shell/filesystem skills
are excluded from VM runs. Full prerequisites, guest permissions, service
commands and troubleshooting are in the [VM setup guide](document/vm_setup.md).

The CLI can use the saved connection's endpoint (default SSH tunnel port 8791):

```bash
echopath --config echopath.local.json --vm-endpoint http://127.0.0.1:8791 --progress "Open the browser and go to example.com"
```

### Restore a clean VM after every task

On macOS, a Lima VZ instance with one raw disk can use an offline APFS baseline.
Prepare the guest with the apps and files each task should start with, and make
its desktop bridge start automatically at boot. Then capture the baseline:

```bash
python -m echopath.vm_reset --config echopath.local.json capture \
  --instance YOUR_LIMA_INSTANCE --endpoint http://127.0.0.1:8790
```

Capture stops and restarts that instance, saves its disk and EFI state, and
enables automatic restoration for that exact endpoint. It refuses to replace
an existing baseline. Snapshots use copy-on-write clones on the same APFS
volume; the baseline, machine-specific policy and reset journal stay in
Git-ignored `local_data/vm-reset/`.

Every completed, failed or stopped VM task saves its host-side records first,
then restores the baseline while holding the desktop lock. Pausing a task or
waiting for a native continuation does not reset it. A crashed worker is
recovered by the Control Room or before the next task. The UI shows
**Resetting VM** until an authenticated desktop observation succeeds. If reset
fails, another task is blocked and **Retry reset** becomes available.

Only the guest disk and EFI state are restored. Files in host-shared folders,
remote services, task history and recordings on the host are outside the
snapshot. Changed VM configuration or additional disks require a new reviewed
baseline; EchoPath will refuse to restore a mismatched instance. You can inspect
the policy or retry a restore while no task owns the VM:

```bash
python -m echopath.vm_reset --config echopath.local.json status
python -m echopath.vm_reset --config echopath.local.json restore
```

## Security and privacy model

- **What leaves the machine.** Prompts, planning context, and the screenshots
  taken during desktop tasks are handed to the model backend you configured.
  With the Claude Code or Codex bridges that means the respective API. With
  Ollama nothing leaves the machine. Choose a backend accordingly and avoid
  running desktop tasks with sensitive content on screen.
- **Desktop control is opt-in per call.** The MCP server strips the desktop
  skill unless `allow_desktop_control=true` is passed on that specific
  `echopath_run_task` call, and tells the planner that GUI actions are not
  authorized.
- **The backend is read-only.** Bridges run the model backend with its own
  write and shell capabilities disabled. Actions happen only through
  EchoPath's skills, which the config's `skills.enabled` list governs.
- **Credentials stay out of the repository.** Bridges reuse existing logins or
  read gitignored auth files. Auth values are never echoed in errors.
- **HTTP transports are unauthenticated.** Use stdio for local hosts and keep
  HTTP bound to loopback.

To report a security issue, open a private report through the repository's
GitHub security advisories rather than a public issue.

## Runtime outputs

Runs write local artifacts to these locations, all ignored by Git except the
curated examples:

| Path | Contents |
| --- | --- |
| `log/` | Per-run logs and events. |
| `downloads/` | Files the runtime downloads on request. |
| `local_data/desktop_observations/` | Screenshots taken during desktop tasks. |
| `local_data/gui_runs/` | Recorded GUI runs awaiting or after consolidation. |
| `local_data/memory_repo/` | Learned memory library. |
| `local_data/token_usage/` | Token usage logs when the backend reports them. |
| `memory/actions/` | Raw first-run trajectories. |
| `memory/consolidated/` | Curated replay-ready memories. Tracked examples only. |

`echopath_clear_history` removes the local history artifacts; the example
memories are left in place.

## Project layout

| Path | Purpose |
| --- | --- |
| `src/echopath/` | Core runtime: orchestrator, planning, GUI and tool agents, desktop, command, and file skills, model routing, replay, and memory. |
| `src/echopath/mcp/` | MCP contracts, the host-independent service layer, and the SDK server entrypoint. |
| `src/echopath/evaluation/` | Benchmark cases, runners and result summaries. |
| `src/echopath/diagnostics/` | Accessibility, permission and component checks. |
| `src/echopath/tools/` | Developer utilities, including the static-site builder. |
| `bridges/` | Model backend bridges and the template for writing one. |
| `skills/` | Markdown skill instructions loaded by the runtime. |
| `credentials/` | Gitignored home for bridge auth files. |
| `document/` | Agent flow, MCP architecture, model routing, and skill notes. |
| `examples/` | Public GUI task packs. |
| `memory/consolidated/` | Example replay memories and crops. |
| `echopath.config.json` | Tracked, credential-free default config. |

## Development

The former root-level Python scripts now run as package modules. Install the
package into your environment first, then use `python -m` instead of a file path:

```bash
python -m echopath.diagnostics.check_permissions
python -m echopath.diagnostics.ax_probe Chrome
python -m echopath.desktop_status "Running a desktop task"
python -m echopath.evaluation.summarise_pareto local_data/offset/results.jsonl
python -m echopath.tools.build_site /path/to/artifact.html --output /path/to/index.html
```

Desktop diagnostics and benchmarks require macOS and can operate the real
desktop; `--help` only shows usage. For benchmarks, install the optional browser
client with `python -m pip install -e ".[macos,evaluation]"`. See
[AGENT_HANDOFF.md](AGENT_HANDOFF.md) for benchmark commands and prerequisites.

These developer modules resolve bridges, credentials and default output paths
from their source checkout or extracted package, even when launched elsewhere.
Set `ECHOPATH_PROJECT_ROOT` to use another source folder. With a wheel install,
set that variable or run from a source folder; the wheel alone does not contain
the external bridges. Reports and screenshots stay under that folder's ignored
`local_data/`. Explicit input/output arguments are relative to the caller's
working directory. The site builder reads `shots/` beside its input HTML and
defaults to writing `<project>/site/index.html`.

To explore the server with the MCP Inspector, install the SDK's CLI extra and
run the module from the repository root, since the module resolves the default
config relative to the current directory:

```bash
python -m pip install -e ".[mcp-dev]"
mcp dev src/echopath/mcp/server.py
```

To add a backend, copy `bridges/_template/` and follow its README. Bridges
should stay dependency-free where possible, pin a concrete model, keep the
backend read-only, and read tunables from `ECHOPATH_<BRIDGE>_*` variables.

## Troubleshooting

- **The bridge reports that `claude` or `codex` was not found, but it runs in
  a terminal.** The host launched the server with a shorter `PATH`. Set
  `ECHOPATH_CLAUDE_EXECUTABLE` or `ECHOPATH_CODEX_EXECUTABLE` to the absolute
  path, or add the directory to `external_command.env.PATH` in
  `echopath.local.json`.
- **`echopath.config.json` not found.** The server looks in the current
  directory by default, and a host's working directory is unpredictable. Always
  pass `--config` with an absolute path.
- **The host times out on a long desktop task.** Some hosts cap a single tool
  call at around a minute. The run continues inside the server; poll
  `echopath_get_status` or read `echopath://sessions/{session_id}/debug`, and
  check the latest screenshot under `local_data/desktop_observations/`.
- **Windows: `python3` is not recognized.** Use `python` or `py -3`, and
  activate the environment with `.\.venv\Scripts\Activate.ps1`. Bridges resolve
  `claude.cmd` and friends through `shutil.which`, so no wrapper is needed.
- **macOS: screenshots are black or clicks do nothing.** Grant Screen
  Recording and Accessibility permission to the process that launched the
  server, then restart the host.

## License

EchoPath is released under the MIT License. See [LICENSE](LICENSE).
