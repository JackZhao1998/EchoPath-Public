# Kimi Bridge

This bridge calls the locally installed `kimi` CLI. It expects Kimi Code to already be authenticated in the local environment.

Set `external_command.command` in `echopath.config.json` to:

```json
["python", "bridges/kimi/run_kimi.py"]
```

Optional environment variables:

- `ECHOPATH_KIMI_EXECUTABLE`
- `ECHOPATH_BRIDGE_MODEL`
