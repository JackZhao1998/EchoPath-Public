# Ollama Bridge

This bridge calls a local Ollama server. It does not use cloud credentials.

Set `external_command.command` in `echopath.config.json` to:

```json
["python", "bridges/ollama/run_ollama.py"]
```

Then choose a local model:

```bash
export OLLAMA_MODEL=llama3.1
```

Optional environment variables:

- `OLLAMA_HOST`, default `http://127.0.0.1:11434`
- `OLLAMA_MODEL`
