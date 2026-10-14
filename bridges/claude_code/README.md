# Claude Code Bridge

Runs one headless `claude -p` turn per EchoPath model request, using the
locally installed `claude` CLI. Defaults to **Claude Opus 5**.

Point `external_command.command` in `echopath.config.json` at it:

```json
{
  "external_command": {
    "command": ["python", "bridges/claude_code/run_claude_code.py"]
  }
}
```

## Install and authenticate

The bridge needs the Claude Code CLI on `PATH`:

```bash
npm install -g @anthropic-ai/claude-code
```

There are two ways to authenticate, and they select different modes:

| Credential | Mode | Setup |
| --- | --- | --- |
| Claude Code subscription login | standard | `claude` once, interactively, then nothing else |
| `ANTHROPIC_API_KEY` | `--bare` | put the key in `credentials/claude_code.json` |

`--bare` skips discovery of hooks, plugins, MCP servers, `CLAUDE.md`, and custom
commands, which makes a run reproducible and stops a repository the bridge is
pointed at from steering the model backend. It also never reads the Claude Code
OAuth login, so the bridge only turns it on when an API key is present.
`ECHOPATH_CLAUDE_BARE=1` or `=0` forces the choice either way.

For the API-key path, create `credentials/claude_code.json` (already gitignored):

```json
{ "env": { "ANTHROPIC_API_KEY": "sk-ant-..." } }
```

An `ANTHROPIC_API_KEY` already set in the environment takes precedence over the
file. See [`bridges/_template/README.md`](../_template/README.md) for the full
auth-file lookup order.

## Defaults

| Setting | Default | Why |
| --- | --- | --- |
| Model | `claude-opus-5` | Pinned so runs do not vary with each machine's `claude` default |
| Permission mode | `dontAsk` | A headless turn has nobody to answer a prompt |
| Allowed tools | `Read` | Lets the model open the screenshots EchoPath passes by path |
| Disallowed tools | `Bash,BashOutput,KillShell,Edit,Write,NotebookEdit,WebFetch,WebSearch,Task,Agent,Monitor,SlashCommand` | Defence in depth; also keeps unusable tools out of the prompt |
| Max turns | `4` | Enough to read a screenshot and answer; bounded against loops |

The model backend is deliberately read-only. EchoPath's own desktop, command,
and workspace-file skills perform actions; the backend supplies planning,
grounding, and verification.

Two layers enforce that, and it is worth knowing which does the work.
`--permission-mode dontAsk` is the real boundary: it denies anything outside the
read-only set, including tools the deny list has never heard of. The deny list
is defence in depth — bare tool names drop those tools from the prompt, so the
model does not plan around capabilities it cannot use.

Verified against the live CLI: a request to run a shell command is refused and
writes no file, and the same holds when the request names a specific tool such
as `Monitor` to get around it.

Do not collapse the deny list to `--disallowedTools "*"`. Deny beats allow, so
the wildcard also removes `Read` — the model then emits its tool call as prose
instead of executing it, and the bridge returns confident text that never
looked at the screenshot.

Screenshots arrive as `local_image` entries and are passed by path, with
`--add-dir` added for any directory outside the working directory so the Read
tool can open them.

When EchoPath sets an `output_schema`, the bridge passes it to `--json-schema`
and returns the CLI's `structured_output` field as the response text.

## Environment overrides

- `ECHOPATH_CLAUDE_EXECUTABLE`: executable name or path, default `claude`.
- `ECHOPATH_CLAUDE_MODEL`: fallback model when the request does not name one.
- `ECHOPATH_CLAUDE_BARE`: `1`, `0`, or `auto` (default).
- `ECHOPATH_CLAUDE_PERMISSION_MODE`: default `dontAsk`.
- `ECHOPATH_CLAUDE_ALLOWED_TOOLS`: default `Read`; empty string omits the flag.
- `ECHOPATH_CLAUDE_DISALLOWED_TOOLS`: default as tabled above.
- `ECHOPATH_CLAUDE_MAX_TURNS`: default `4`.
- `ECHOPATH_CLAUDE_EXTRA_ARGS`: extra shell-split flags appended to the command.
- `ECHOPATH_CLAUDE_CODE_AUTH_FILE`: auth file path, overriding the default lookup.

## Notes

- A failure inside a run — expired login, denied tool, exhausted turns — is
  reported by the CLI on stdout with `is_error` set, not by a non-zero exit
  status. The bridge inspects the envelope and fails the turn with the CLI's own
  message rather than returning an empty response.
- The executable is resolved through `shutil.which`, so an npm-installed
  `claude.cmd` works on Windows. Passing a bare name to `subprocess` does not.
- An MCP server inherits its host's `PATH`, which may not include the npm global
  bin directory even though your shell's does. If the bridge reports that
  `claude` was not found while it runs fine in a terminal, set
  `ECHOPATH_CLAUDE_EXECUTABLE` to the absolute path, or add the directory to the
  `env` block of `external_command` in `echopath.config.json`.

## Test without the CLI

`tests/test_claude_code_bridge.py` runs the bridge end to end against a stub
`claude` placed on `PATH`, so the request framing, flag construction, error
handling, and response shape are all covered with no network access, no
credentials, and no CLI installed:

```bash
python -m unittest tests.test_claude_code_bridge
```
