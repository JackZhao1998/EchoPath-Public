# Bridge Template

A starting point for wiring a new model backend into EchoPath, and the reference
for how auth files work.

`_template` is a scaffold, not a runnable backend. Nothing enumerates this
folder: a bridge is used only when `echopath.config.json` names its path, so
copying the template has no effect until you point the config at the copy.

## Create a bridge

```bash
cp -r bridges/_template bridges/my_backend
mv bridges/my_backend/run_template_bridge.py bridges/my_backend/run_my_backend.py
```

Then edit the five numbered sections in the copied file: identity, credentials,
command, response parsing, response. Point `echopath.config.json` at it:

```json
{
  "external_command": {
    "command": ["python", "bridges/my_backend/run_my_backend.py"]
  }
}
```

Exercise it without starting the MCP server:

```bash
echo '{"model":"default","input":"say hi"}' | python bridges/my_backend/run_my_backend.py
```

## The contract

One JSON request on stdin, one JSON response on stdout. Both shapes are
documented in [`bridges/README.md`](../README.md). Non-zero exit means the turn
failed, and stderr becomes the reason EchoPath reports — so write diagnostics
there, never on stdout, which carries the response.

## Auth files

A bridge inherits the environment EchoPath was launched with. That is enough
when the backend already has a local login, but an MCP server started by Claude
Desktop or another host inherits that host's environment, which usually carries
no API keys. An auth file closes that gap without putting credentials in
`echopath.config.json`.

`load_auth_env("<bridge>")` reads the first file it finds:

1. `$ECHOPATH_<BRIDGE>_AUTH_FILE`, then `$ECHOPATH_AUTH_FILE`
2. `credentials/<bridge>.json`, then `credentials/<bridge>.env`, relative to the
   repository root and then the working directory

Both formats are accepted:

```json
{ "env": { "ANTHROPIC_API_KEY": "sk-ant-..." } }
```

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

Rules worth knowing:

- **Variables already present in the environment win.** The file fills in what
  is missing rather than overriding a key the operator exported deliberately.
- **A missing default file is not an error**, so a bridge that relies on an
  ambient login still works with no file present. A path named explicitly via
  `ECHOPATH_<BRIDGE>_AUTH_FILE` that does not exist *is* an error.
- **Values are never echoed.** Parse failures name the file, not its contents.
- `credentials/` is listed in `.gitignore`. Keep it that way; do not commit an
  auth file, and do not add credentials to this folder or to
  `echopath.config.json`.

Declare what the backend needs in `REQUIRED_AUTH_VARS` so the bridge fails with
a readable message naming the missing variable, instead of letting the backend
fail with a cryptic one.

## Conventions

- **Stay dependency-free** where you can. Every Python bridge except the Codex
  SDK shim uses only the standard library, which keeps `pip install -e .` enough
  to run them.
- **Pin a concrete model.** Map EchoPath's `default` sentinel to a real model id
  rather than deferring to the backend's own default, which varies per machine.
- **Keep the backend read-only.** EchoPath owns the desktop, shell, and file
  action boundary. The backend should return planning, grounding, and
  verification output rather than acting on its own.
- **Read tunables from the environment**, namespaced `ECHOPATH_<BRIDGE>_*`, so
  operators can adjust a bridge without editing it.
- **Check for in-band errors.** A backend that exits 0 while reporting a failure
  in its payload otherwise looks like a successful empty turn. See
  `parse_cli_json` in `bridges/claude_code/run_claude_code.py`.
- **Call executables by name.** `run_command` resolves the name through
  `shutil.which`, which is what makes `claude.cmd` and friends work on Windows;
  passing a bare name to `subprocess` directly does not.
