# Using EchoPath

[Project overview](../README.md) · [Installation](../INSTALL.md) ·
[VM setup](vm_setup.md) · [Windows setup](windows_setup.md)

This guide covers everyday controls, configuration, MCP integration, and
advanced options. Use [INSTALL.md](../INSTALL.md) for prerequisites, agent
registration, updates, and installation repair.

## Control Room

On macOS, open `~/Applications/EchoPath.app`, or the app destination you chose
during installation. For local desktop work, grant **EchoPath** Accessibility
and Screen Recording, quit and reopen it, and keep it running. Local MCP tasks
are submitted to this authorized app; granting permissions only to the coding
agent or terminal does not complete that setup.

On Windows, open **EchoPath** from the Start menu. Its system tray controls
open the browser Control Room. Windows must remain signed in and unlocked.

To start the web Control Room directly, run from the installed source folder:

```bash
.venv/bin/python -m echopath.dashboard --config echopath.local.json
```

On Windows, use `.venv\Scripts\python.exe` in place of `.venv/bin/python`
throughout this guide. The Control Room opens at `http://127.0.0.1:8765`.
Launching the web UI alone does not replace the authorized Mac app for local
macOS desktop control.

In **Start a task**, choose **This computer** or **Virtual machine**, describe
the goal, enable **Allow desktop control**, and start. The **Activity** panel
shows progress; **Recent runs** contains task status, results, and step logs.

### Pause, resume, and stop

- **Pause** requests a checkpoint. Wait for **Paused** before using the target
  desktop yourself; an action or model request already in progress may finish.
- **Resume** takes a fresh observation and discards the previous pending GUI
  plan before continuing toward the same goal.
- **Stop** is cooperative as well. A paused task stops immediately; an active
  task stops after its current action or model request returns.

One run owns each target in this checkout, including while paused. A local run
and a run on a distinct VM endpoint may coexist. Closing the browser does not
stop a task. A paused run needs its worker to remain alive; it cannot be resumed
after that worker exits or the machine restarts.

## Configuration and backends

The installer creates the Git-ignored `echopath.local.json`. Keep
[`echopath.config.json`](../echopath.config.json) as the credential-free public
template. The **Config** window manages desktop and VM settings, model checks,
optional JEV, and recording preferences.

For a manual configuration:

- Use an absolute path to the selected Python interpreter and model bridge.
- Set the bridge's `working_directory` to the source folder.
- Pass an absolute `--config` path when registering a server with another host.
- Use `skills.enabled` to select desktop, command-line, and workspace-file
  capabilities. Desktop opt-in does not disable the other configured skills.

The default backend is the Codex CLI bridge. The agent that calls EchoPath and
the backend EchoPath uses for planning are separate choices.

| Backend | Bridge reference |
| --- | --- |
| Codex CLI | [Existing Codex login and executable overrides](../bridges/codex_cli/README.md) |
| Claude Code | [CLI login or API-key authentication](../bridges/claude_code/README.md) |
| Codex SDK | [Optional Node.js bridge](../bridges/codex_sdk/README.md) |
| Kimi | [Kimi CLI bridge](../bridges/kimi/README.md) |
| Ollama | [Local model server](../bridges/ollama/README.md) |
| OpenCode or another command | [OpenCode](../bridges/opencode/README.md) · [Generic command](../bridges/generic_command/README.md) |

Use the selected backend's existing login where possible. Auth files belong
under the ignored `credentials/` directory, not in the tracked configuration.
See the [bridge contract](../bridges/README.md) and
[auth-file lookup rules](../bridges/_template/README.md).

## MCP integration

For Codex or Claude Code, use the installation helper rather than editing host
configuration by hand:

```bash
python3 packaging/setup.py connect --agent codex
```

Use `--agent claude` or `--agent both` for the other choices. On Windows, run
the helper with `python` instead of `python3`. Restart the selected coding agent
after registration. The bundled usage skill is invoked with `$echopath` in
Codex or `/echopath` in Claude Code.

### Other MCP hosts

EchoPath provides a stdio MCP server. For a host using `mcpServers` JSON, adapt
this example to the absolute paths on your machine:

```json
{
  "mcpServers": {
    "echopath": {
      "command": "/absolute/path/to/EchoPath/.venv/bin/echopath-mcp",
      "args": ["--config", "/absolute/path/to/EchoPath/echopath.local.json"]
    }
  }
}
```

On Windows, the executable is `.venv\Scripts\echopath-mcp.exe` inside the
installation. Follow the host's configuration format, then restart it. For local
macOS work, the authorized EchoPath app must also be running with the same setup.

### Tracked tasks

For GUI work, start an asynchronous tracked task and follow its returned ID:

```text
echopath_start_task(
    task="Open my browser, search for EchoPath on GitHub, and stop at the results.",
    target="local",
    allow_desktop_control=true
)
```

Set `target="vm"` for a connected guest. The tool defaults to `vm`, so specify
the target explicitly. Desktop actions require `allow_desktop_control=true`.
The same task appears in the Control Room.

| Tool | Purpose |
| --- | --- |
| `echopath_get_task` | Read a tracked task's status, activity, and result by `run_id`. |
| `echopath_list_runs` | List ongoing, paused, and recent runs. |
| `echopath_control_run` | Request `pause`, `resume`, or `stop` for a `run_id`. |
| `echopath_list_procedures` | Find an existing workflow before starting new work. |
| `echopath_get_procedure` | Read a procedure's replay phrase, parameters, and steps. |
| `echopath_smoke_check` | Check runtime configuration; optionally test the model bridge. |

The session-oriented interface remains available through `echopath_run_task`,
`echopath_get_session`, and `echopath_get_status`. For the complete tool,
resource, and prompt list, see [MCP architecture](mcp_architecture.md).

### HTTP transports

For a compatible HTTP client:

```bash
.venv/bin/echopath-mcp --config echopath.local.json --transport streamable-http --host 127.0.0.1 --port 8000
```

`sse` is also supported. MCP HTTP transports do not add authentication; keep
them on loopback or provide an authenticated boundary. Use stdio for ordinary
local host integration.

## Workflow memory

Successful GUI runs are recorded under `local_data/gui_runs/` and normally
consolidated through the selected model backend into `local_data/memory_repo/`.
Consolidation uses model quota. It retains useful steps and screen anchors,
and can mark task-specific typed values as parameters.

Open **Stored procedures**, inspect a workflow, and choose **Use as task** to
copy its replay request. Review parameter values, choose the desktop, and
start it. Copying a replay phrase does not start desktop control automatically.

Replay can visually re-aim a stored step. If replay cannot continue, the
default behavior is to fall back to live planning. Set
`desktop.replay_fallback_to_planning=false` to fail instead.

- Set `ECHOPATH_AUTO_CONSOLIDATE=0` to record runs without automatic learning.
- `echopath_consolidate` and `echopath_list_memories` manage learned memories.
- Curated examples live in [`memory/consolidated/`](../memory/consolidated/README.md).
- [Segmented replay](native_segments.md) documents optional native handoff.
  Tracked tasks use `echopath_resume_task`; session calls use `echopath_resume`.
  Native handoff is opt-in and requires host tools on the same target desktop.

### Optional JEV helper

JEV is a macOS-only replay recovery helper. It uses local OCR, sends on-screen
text to OpenRouter, and may incur separate model charges. It starts disabled.
Save and verify an OpenRouter token in **Config** before enabling it. The token
is stored in the ignored `credentials/api_keys.yaml`; an environment-provided
`OPENROUTER_API_KEY` takes precedence. Removing or failing verification of the
token disables the helper.

### Task video recording

Task recording is separate from workflow learning and starts disabled. The
built-in recorder requires a macOS host and a macOS or Linux X11 guest. Windows
guest recording is not supported by this recorder. Videos are saved under
`local_data/recordings/`; only the guest desktop is captured.

## Command-line tasks

The installed CLI runs a task without an MCP host. For a workspace-only task:

```bash
.venv/bin/echopath --config echopath.local.json --local-desktop --no-desktop-control --progress "List the files in this workspace"
```

`--json` prints the full runtime response; `--progress-json` streams structured
progress to stderr. `Ctrl+C` stops a directly launched CLI task.

The CLI permits the configured desktop skill by default; use
`--no-desktop-control` when it is not needed. `--local-desktop` explicitly selects
this computer, and `--vm-endpoint` selects a guest bridge. Prefer the authorized
Mac app and tracked MCP tasks for local macOS GUI work.

## Automatic VM reset

This advanced option supports a macOS host with a Lima VZ guest, one raw disk,
and an APFS baseline. It is not a general reset mechanism for Tart or VirtualBox.
See [VM setup](vm_setup.md) for the desktop connection itself.

Prepare the guest's apps, files, and bridge autostart, then capture a baseline:

```bash
.venv/bin/python -m echopath.vm_reset --config echopath.local.json capture --instance YOUR_LIMA_INSTANCE --endpoint http://127.0.0.1:8791
```

Capture stops and restarts that VM, saves disk and EFI state, and enables
automatic restoration for the selected endpoint. It refuses to overwrite an
existing baseline. Use the endpoint of your actual saved connection.

After a completed, failed, or stopped task, restoration holds the target lock
until an authenticated desktop check succeeds. Pausing does not reset the VM.
The baseline excludes host history, recordings, shared folders, and external
services. Configuration or disk changes require a new compatible baseline.

```bash
.venv/bin/python -m echopath.vm_reset --config echopath.local.json status
.venv/bin/python -m echopath.vm_reset --config echopath.local.json restore
```

The policy and reset journal are stored under the ignored
`local_data/vm-reset/`. Use **Retry reset** after resolving a failed restoration.

## Local data and privacy

| Location | Contents |
| --- | --- |
| `echopath.local.json` | Machine-specific configuration. |
| `credentials/` | Local backend and optional helper credentials. |
| `log/` | Runtime logs and events. |
| `downloads/` | Files downloaded during tasks. |
| `local_data/desktop_observations/` | Task screenshots. |
| `local_data/gui_runs/` | Recorded GUI trajectories for learning. |
| `local_data/memory_repo/` | Learned workflows. |
| `local_data/token_usage/` | Usage logs when supplied by the backend. |
| `local_data/recordings/` | Optional task videos. |

These locations are ignored by Git. Curated example memories are tracked
separately under `memory/consolidated/`.

Local storage does not imply offline model processing: prompts, planning
context, and screenshots go to the configured backend. Optional JEV sends text
to OpenRouter. The Codex and Claude Code bridges request read-only backend
operation; desktop, shell, and file actions are governed by EchoPath's configured
skills. Custom bridges are user-provided commands with their own capabilities.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| EchoPath tools are missing after setup | Fully restart the coding agent and run the installation helper's `doctor` check. |
| Local macOS task cannot start or screenshots are black | Open EchoPath.app, check its Accessibility and Screen Recording grants, then quit and reopen it. |
| Backend CLI works in a terminal but not from MCP | Check executable paths and `external_command.env.PATH`; desktop hosts may inherit a shorter environment. |
| Configuration cannot be found | Register the server with an absolute `--config` path. |
| A long task outlasts the host's tool timeout | Use `echopath_start_task` and poll `echopath_get_task` instead of holding a synchronous call open. |
| Status stays at Pausing | Wait for the active action or model request to reach a checkpoint. |
| A Windows VM is unavailable | Check the signed-in guest session, Guest Additions, and bridge connection using the [Windows guide](windows_setup.md). |

For setup repair, model checks, and removal, see [INSTALL.md](../INSTALL.md).

## Development

Core code is under [`src/echopath/`](../src/echopath/). MCP integration is under
[`src/echopath/mcp/`](../src/echopath/mcp/). The regression suites in `test/` and
`tests/` are local development files and are excluded from this release branch.

From a development checkout containing the local suite, run the tests with its
private interpreter:

```bash
.venv/bin/python -m unittest discover -s test
```

These regression tests use mocks rather than model calls or live desktop
actions; some platform-specific cases are skipped on other operating systems.
Developer diagnostics and evaluation modules are separate and can operate a
real desktop. New backends can start from the [bridge template](../bridges/_template/README.md).

To inspect MCP schemas interactively, install the optional inspector dependency
into the same environment and launch from the repository root:

```bash
.venv/bin/python -m pip install -e '.[mcp-dev]'
.venv/bin/mcp dev src/echopath/mcp/server.py
```
