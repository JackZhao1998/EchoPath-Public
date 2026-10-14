# Generic Command Bridge

Use this bridge when a local model command can accept a prompt on stdin. The command may print plain text or the EchoPath bridge JSON response shape.

Set `external_command.command` in `echopath.config.json` to:

```json
["python", "bridges/generic_command/run_command_bridge.py"]
```

Then set:

```bash
export ECHOPATH_GENERIC_COMMAND='your-model-command --flag'
```

Credentials and auth belong to that command's normal local environment.
