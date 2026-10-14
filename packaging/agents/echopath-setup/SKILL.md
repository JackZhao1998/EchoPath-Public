---
name: echopath-setup
description: Install, connect, verify, update, or repair EchoPath for Codex or Claude Code from a Git repository or downloaded source package. Use for EchoPath installation and agent integration, not for running desktop tasks or setting up a VM.
---

# EchoPath setup

Complete a usable local installation: isolated runtime, selected model bridge,
MCP registration, agent skills, the running app, and verified status. The
package contains the installer; do not invent a separate installation procedure
or install only a Python wheel (the runtime also needs the bundled bridges,
skills and config).

Work through the sections in order. Each ends with a check; do not move on
while a check fails, and never report "ready" over a failed one.

## 1. Locate the package and the choices

If this skill has an `origin.json` beside it, read its `package_root` and `config`
for the installed copy. Otherwise the package root is three levels above this
file (`packaging/agents/echopath-setup/SKILL.md`). Read `INSTALL.md` there for
commands, prerequisites, platform differences, and troubleshooting.

This package is the `main` release branch. Use `main` for the macOS app
and `windows-version` for the Windows tray app and managed Windows guests.
For a bare repository URL, select the branch matching the requested host;
preserve an explicitly supplied ref and explain a platform mismatch before
installation. Do not mix branch files or switch a populated installation
with local changes.

For a user-supplied Git URL, clone the requested repository/ref into the
folder the user named, or `~/.echopath/src` when they named none. Preserve a
supplied branch/tag/commit. Never overwrite a populated destination, reset a
checkout, or pull over local changes just to install. If that folder already
contains EchoPath, inspect it and use its setup helper to repair it. For a
downloaded ZIP, extract it first, locate `INSTALL.md`, and use `--destination`
if the source is in Downloads or another temporary location. Do not run the app
permanently from a mounted DMG or an expendable download. The folder is
permanent once installed: the app and the MCP registration point at it, so tell
the user before installing somewhere like Desktop that it must stay there.

Use the coding agent the user named as `--agent codex` or `--agent claude`; use
`both` only when they want both. When unspecified, use the agent conducting this
setup if identifiable; otherwise ask which agent to connect. Use the same
provider for `--backend` unless the user chose another. Existing sign-in is
reused; the agent hosting MCP and the model backend can differ.

## 2. Preflight: interpreter, CLI and login

Fix these before running the installer; each is a common first failure.

- **Python.** The helper needs Python 3.10+, and the default `python3` is often
  older (Anaconda base, the macOS system Python). Check candidates and use the
  first that qualifies for every `packaging/setup.py` command:

  ```bash
  for p in python3.13 python3.12 python3.11 python3.10 python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do command -v "$p" >/dev/null && "$p" -c 'import sys; print(sys.executable, sys.version.split()[0])'; done
  ```

  If none qualifies, install one (python.org, `uv python install 3.12`, or the
  platform's package manager) with the user's agreement. Do not install into a
  system or Anaconda base environment; the helper creates its own `.venv`.
- **Backend CLI.** `command -v codex` or `command -v claude` (on macOS, Codex
  may be inside `/Applications/Codex.app`; the helper finds it). If missing,
  install it by its official instructions. If a global `npm install -g` fails
  with a permissions error, do not use `sudo`: install to the user's own prefix
  (`npm install -g --prefix "$HOME/.local" <package>`) and put `~/.local/bin` on
  `PATH` for the installer command. Tell the user if their shell profile needs
  that `PATH` line; ask before editing the profile.
- **Login.** Check it (`claude auth status`, `codex login status`). Signing in is
  the user's own interactive step in their terminal or browser. Never ask for,
  accept, or type passwords, session files, API keys or tokens in this
  conversation or the package. Wait until the status command confirms it.

## 3. Install and connect

1. Explain the selected destination, agent and backend briefly, then run the
   bundled helper with the interpreter from preflight. The user's installation
   request authorizes routine setup; do not ask them to reconfirm each
   dependency or generated config. Respect any actual tool approval boundary.

   ```bash
   python3.12 packaging/setup.py install --backend codex --agent codex
   ```

   Substitute `claude` as selected. For a downloaded folder, add
   `--destination /absolute/permanent/EchoPath`. On macOS, include `--app` unless
   the user wants CLI/web-only operation: local desktop tasks run through that
   app. It builds into `~/Applications` and requires Xcode Command Line Tools
   and a compatible Python (3.12 recommended; if the launcher cannot find
   `libpython`, see `INSTALL.md`). On Windows use `py -3.12`; command arguments
   are otherwise the same.
2. Reruns are safe and are also how to add `--app` later, repair, or update
   after pulling new code: they reuse the venv and preserve VM, recording, keys,
   history and other settings. Switching this same installation between its
   interpreter and its built app is handled without a flag.
3. Resolve reported conflicts narrowly. A different backend, or an `echopath`
   registration or skill that belongs to another checkout, is preserved until
   the corresponding `--replace-backend` or `--replace-agent` is selected.
   Inspect the conflicting entry first. If the user's requested
   switch/reinstallation already covers replacing it, proceed with that flag;
   otherwise ask only about that conflict. The helper creates backups and
   changes only EchoPath's entries. Do not clear another agent's configuration
   or weaken its sandbox/trust settings.
4. From the final installed folder, run:

   ```bash
   python3.12 packaging/setup.py doctor --model
   ```

   This makes one small model request using the selected login/quota. State that
   before running it as part of installation verification. If the user excludes
   model/network checks, omit `--model` and report model verification as pending.
   A failed login/model check is not a completed setup. Help the user finish the
   native login or resolve model access, then retry once after the cause changes.

Check: the helper exited 0 and `doctor` printed `"ok": true` with
`model_bridge_ok: true` (or model verification is explicitly pending).

## 4. Start the app and settle permissions

After every successful local installation, start the user-facing app before
reporting completion. On macOS with `--app`:

```bash
open "$HOME/Applications/EchoPath.app"
```

If `open` fails right after the app was quit (LaunchServices error -600), wait
a few seconds and retry with `open -n`. If no native app was built (including
Linux and Windows), start the web Control Room from the installed folder using
the installed interpreter, as described in `INSTALL.md`, and keep it running.

Guide the user through the OS-owned Accessibility and Screen Recording prompts
when local desktop control is wanted. Do not change privacy databases or claim a
permission is granted without evidence; after approval, EchoPath must be quit
and reopened before its check can pass. Then verify, rather than assume:

```bash
curl -s http://127.0.0.1:8765/api/local-desktop-authorization
```

`"ready": true` means the app holds both grants. Keep the app running while
local tasks run: the MCP server submits them to that authorized app over its
loopback Control Room instead of controlling the desktop from the coding
agent's stdio child process. If macOS instead asks to grant Accessibility to
the coding agent itself (for example "claude"), that is the wrong process: have
the user deny it and route the task through the app. A headless or VM-only
install can finish without granting host desktop control; state that scope.

The app lives in the menu bar (a dot with radio waves). On a notched display
with a full menu bar macOS hides it behind the notch; the Control Room at
`http://127.0.0.1:8765` and its Config page at `/config` are the same controls,
so point the user there instead of hunting for the icon.

Starting the app does not authorize a desktop task and does not replace the
MCP/model verification above.

## 5. Reload the coding agent and verify through it

Require the user to fully quit and restart Codex or Claude Code after
installation or after any rerun that changed the registration. An already-open
host keeps its earlier MCP server process and tool list, and will not see new
tools or options. When the restarted host exposes the tools, call
`echopath_smoke_check(include_model_bridge=false)` once through that host. The
helper's stdio test proves that the server works; it does not prove the current
host session reloaded it. Do not substitute a desktop click test or start a GUI
task during installation verification.

After updating an existing installation's code, also quit and reopen
EchoPath.app: the running app keeps the old code until restarted.

## 6. Optional features, only when asked

VM integration, the JEV replay helper and task recording start off for a fresh
install. Leave them off unless requested; none is a prerequisite.

- **VM desktop.** Use the `echopath-vm-setup` skill. Creating a VM is a
  separate resource choice (large download, disk, memory) that needs its own
  yes.
- **JEV replay helper.** It needs an OpenRouter token and sends on-screen text
  to OpenRouter during replay recovery; say so. The user pastes the token
  themselves in Config (`http://127.0.0.1:8765/config` -> OpenRouter -> save).
  If they paste a token into this conversation, do not enter it for them:
  point them to that field and suggest rotating the token, since it is now in
  the transcript. Afterwards confirm with
  `curl -s http://127.0.0.1:8765/api/config`: `openrouter.valid` must be `true`
  before JEV can be switched on in the same page.

## 7. Report

Report the permanent installation and config paths, the interpreter used, the
selected backend and connected agents, the runtime/MCP/model results, whether
the app is running and authorized, and every user-owned step still pending
(login, permission, host restart). Tell Codex users to invoke `$echopath` and
Claude Code users to invoke `/echopath` for a later desktop task. Never claim
fully ready while a required verification failed or was skipped.
