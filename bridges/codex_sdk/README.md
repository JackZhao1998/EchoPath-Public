# Codex SDK Bridge

Use this bridge only in environments where Codex SDK is available and locally authenticated. The bridge does not contain credentials.

Setup:

```bash
cd bridges/codex_sdk
npm install
```

Then set `external_command.command` in `echopath.config.json` to:

```json
["node", "bridges/codex_sdk/run_codex_sdk.mjs"]
```

If your Codex auth uses a custom setup, configure it in the local environment before running EchoPath.
