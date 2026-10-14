# Local Credentials

Bridge auth files live here. Everything in this folder except this README is
ignored by Git.

Create one file per bridge, named after the bridge folder:

- `credentials/claude_code.json` for `bridges/claude_code/`
- `credentials/<bridge>.json` or `credentials/<bridge>.env` for any other bridge

JSON and `KEY=value` are both accepted:

```json
{ "env": { "ANTHROPIC_API_KEY": "sk-ant-..." } }
```

Variables already set in the environment take precedence over the file, and a
missing file is not an error unless it was named explicitly through
`ECHOPATH_<BRIDGE>_AUTH_FILE`.

Never commit a real credential. See `bridges/_template/README.md` for the full
lookup order and format rules.

## VM bridge authentication

For the Control Room app, save the VM bridge's shared key in
`credentials/amazon_vm.json` next to the configuration file:

```json
{ "endpoint": "http://127.0.0.1:8790", "api_key": "your-vm-bridge-key" }
```

Restrict the file to your account (`chmod 600 credentials/amazon_vm.json`). It is
Git-ignored. EchoPath uses the key only for that endpoint; the environment
variable named by `desktop.amazon_vm_api_key_env` takes precedence. Both EchoPath
and older SearchAgent bridge authentication headers are supported. The Control
Room checks an authenticated, read-only request before marking a secured bridge
connected. This shared key is separate from the OpenRouter model API key.
