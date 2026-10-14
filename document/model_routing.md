# Model Routing

EchoPath uses the `ModelRouter` interface for planning, image grounding, and verification calls.

The included public config uses `external_command` mode and points at the dependency-free Codex CLI bridge. It relies on the host machine's existing Codex login state and does not store credentials in this folder.

```json
{
  "model_routing": {
    "mode": "external_command",
    "router_name": "default",
    "external_model": "default"
  },
  "external_command": {
    "command": ["python", "bridges/codex_cli/run_codex_cli.py"]
  }
}
```

If the host environment uses a different authenticated backend, point the command at exactly one other bridge under `bridges/`.

Runtime token logs are written under `local_data/token_usage/` when available.
