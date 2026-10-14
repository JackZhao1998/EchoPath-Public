# Codex CLI Bridge

This bridge calls the locally installed `codex` CLI. It uses the Codex login state that already exists on the machine and does not store credentials in this repository.

The bridge is dependency-free. It reads one EchoPath JSON request from stdin and writes one EchoPath JSON response to stdout.

Only the final completed assistant message is returned as model output. Progress
messages and other CLI events remain in the raw trace, so they cannot corrupt
the JSON used for planning and desktop actions.

`echopath.config.json` can point at it with:

```json
{
  "external_command": {
    "command": ["python", "bridges/codex_cli/run_codex_cli.py"]
  }
}
```

Useful environment overrides:

- `ECHOPATH_CODEX_EXECUTABLE`: Codex executable name or path, default `codex`.
- `ECHOPATH_CODEX_APPROVAL_POLICY`: Codex approval policy, default `never`.
- `ECHOPATH_CODEX_SANDBOX`: Codex sandbox mode, default `read-only`.
- `ECHOPATH_CODEX_EXTRA_ARGS`: additional shell-split arguments passed before the prompt.

The default sandbox is read-only because EchoPath owns the GUI action boundary. The model backend should return planning, grounding, and verification text or JSON rather than directly changing the desktop.
