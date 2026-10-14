# EchoPath Bridges

EchoPath core calls model backends through a single external-command contract. A bridge reads one JSON request from stdin and writes one JSON response to stdout.

Request shape:

```json
{
  "model": "default",
  "instructions": "system or developer guidance",
  "input": "text, JSON, or [{\"type\":\"text\"}, {\"type\":\"local_image\"}]",
  "metadata": {},
  "reasoning_effort": "medium",
  "temperature": null,
  "max_output_tokens": null,
  "output_schema": {},
  "extra_body": {}
}
```

Response shape:

```json
{
  "output_text": "assistant text or JSON string",
  "model": "resolved-model-name",
  "response_id": null,
  "request_id": null,
  "usage": {}
}
```

A non-zero exit fails the EchoPath turn and stderr becomes the reported reason, so write diagnostics to stderr and keep stdout for the response alone.

## Credentials

Do not place credentials in this folder. A bridge should use the backend's existing local auth, environment variables, or local service configuration.

When the backend needs a key that the host environment does not carry — the common case for an MCP server launched by Claude Desktop — put it in an auth file under `credentials/`, which is gitignored:

```json
{ "env": { "ANTHROPIC_API_KEY": "sk-ant-..." } }
```

`load_auth_env("<bridge>")` reads `credentials/<bridge>.json` or `.env`, or the path in `ECHOPATH_<BRIDGE>_AUTH_FILE`. Variables already set in the environment win. Full rules: [`_template/README.md`](_template/README.md).

Available bridge folders:

- `codex_cli/`: dependency-free shim around an authenticated local `codex` CLI.
- `codex_sdk/`: optional Node bridge for environments with `@openai/codex-sdk`.
- `claude_code/`: Python shim around an authenticated local `claude` CLI, defaulting to Claude Opus 5.
- `kimi/`: Python shim around an authenticated local `kimi` CLI.
- `opencode/`: command-template shim for local OpenCode-style tools.
- `ollama/`: Python shim for a local Ollama server.
- `generic_command/`: generic stdin/stdout bridge for any local model command.
- `_template/`: scaffold and conventions for writing a new bridge.
- `_shared/`: helpers every Python bridge imports.

Set `external_command.command` in `echopath.config.json` to the selected bridge command after local setup.
