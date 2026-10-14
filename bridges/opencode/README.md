# OpenCode Bridge

OpenCode-compatible tools do not all expose the same stable JSON CLI shape. This bridge is a command template: set `ECHOPATH_OPENCODE_COMMAND` to a local command that accepts the prompt on stdin and prints either plain text or the EchoPath bridge JSON response.

Set `external_command.command` in `echopath.config.json` to:

```json
["python", "bridges/opencode/run_opencode.py"]
```

Then set a local command, for example:

```bash
export ECHOPATH_OPENCODE_COMMAND='opencode run'
```

Keep authentication in the local OpenCode environment.
