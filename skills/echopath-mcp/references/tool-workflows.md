# EchoPath MCP Tool Workflows

Use these recipes when the `echopath_*` MCP tools or EchoPath resources are
available in the host.

## Health And Setup

For a lightweight setup check:

```text
echopath_smoke_check(include_model_bridge=false)
```

Confirm `import_ok`, `config_ok`, and the configured `enabled_skills`. This does
not spend a model call.

For installation validation or before a task that depends on a model backend:

```text
echopath_smoke_check(include_model_bridge=true)
```

Report whether `model_bridge_checked` and `model_bridge_ok` are true. If the
bridge fails, use its error text to name the missing executable, login, auth
file, or config issue.

## Running Tasks

Create a named EchoPath session when the user wants a multi-turn EchoPath
conversation or when you need a stable id before a long run:

```text
echopath_create_session(title="<short title>")
```

List current in-memory sessions for this MCP server process:

```text
echopath_list_sessions()
```

Read the full session when a previous tool result was truncated or the user
asks for the exact EchoPath messages:

```text
echopath_get_session(session_id="<session_id>")
```

For ordinary non-GUI work:

```text
echopath_run_task(message="<task>", allow_desktop_control=false)
```

Use a `session_id` when the user is continuing a prior EchoPath session. Omit it
for an isolated task.

For visible desktop work:

```text
echopath_run_task(
  message="<visible desktop task>",
  allow_desktop_control=true
)
```

Only set `allow_desktop_control=true` after the user's request clearly requires
visible GUI observation or actions. If the host exposes prompts, use
`echopath_visible_desktop_task(task=...)` to prepare the message.

Good desktop examples include setting a Clock timer, checking a visible stock
quote, opening Weather for a location, or navigating a local app that the user
can watch.

Avoid desktop execution for credentials, purchases, irreversible deletes,
system security dialogs, or private material that the user has not explicitly
asked EchoPath to view or manipulate.

## Long-Running GUI Tasks

Most GUI runs return only when the task finishes. Wait when the MCP host does
not allow concurrent calls into the same server process.

If concurrent polling is available, poll sparingly:

```text
echopath_get_status(session_id="<session_id>")
```

or read:

```text
echopath://sessions/<session_id>/debug
```

Use status reads to determine whether the run is still active, completed, or
failed. Do not spam status calls while the screen is visibly progressing.

## Result Summary

The response usually contains:

- `session.session_id`
- `session.debug_status`
- assistant message content
- assistant metadata such as `task`, `execution`, `routing`, `replay`, and
  `learning`
- `mcp.allow_desktop_control`
- `mcp.desktop_skill_enabled`
- `mcp.learning` when auto-consolidation reports a result

Summarize the high-signal fields:

```text
MCP result: success/failure
Session: <session_id>
GUI run: <gui_run_id if present>
Replay: used/not used, source task if present
Execution: success/error, completed/halt reason if present
Learning: status/path id/promoted, or error if present
Verification artifact: final useful screenshot path if present
```

Keep raw JSON out of the user-facing answer unless the user asks for it.

## Replay And Memory

List available memories when diagnosing reuse or deciding whether a task should
replay:

```text
echopath_list_memories(include_candidates=false, limit=50)
```

After a completed GUI run, EchoPath may auto-consolidate it. If learning fails
but `execution.success` is true, treat the task as executed successfully and
report the learning failure separately.

To manually consolidate the latest completed GUI run:

```text
echopath_consolidate()
```

To consolidate a specific run:

```text
echopath_consolidate(run_id="<gui_run_id>")
```

To deprecate a bad memory path:

```text
echopath_consolidate(
  deprecate_path_id="<path_id>",
  reason="<short reason>"
)
```

Use `echopath_replay_diagnostics(session_id=...)` or the debug resource when
the user asks why replay was or was not used.

## Clearing History

Only clear history when the user explicitly asks for it:

```text
echopath_clear_history(confirm=true)
```

Never clear sessions or local history as routine cleanup after a test.
