# EchoPath MCP Troubleshooting

Use this reference when an EchoPath MCP task fails, stalls, or produces a result
that needs interpretation.

## Smoke Check Failures

- `import_ok=false`: the Python environment cannot import EchoPath or its MCP
  dependencies. Reinstall the package in the environment used by the MCP host.
- `config_ok=false`: inspect the configured `echopath.config.json` or local
  override. Keep public configs credential-free.
- `model_bridge_ok=false`: report the bridge error text. It usually names a
  missing CLI, login state, auth file, environment variable, or timeout.

Run `include_model_bridge=true` only when validating the backend or when a
model-backed task is about to run.

## Desktop Control Is Disabled

If a GUI task returns guidance that desktop control is disabled, the MCP call
was made without `allow_desktop_control=true`. Ask for or confirm explicit
visible-desktop intent, then rerun with desktop control enabled if appropriate.

Do not work around this gate by using another desktop-control mechanism unless
the user explicitly redirects the task.

## macOS Desktop Permissions

Symptoms include black screenshots, no click effect, no keyboard input, or a
run that cannot observe the target app. The process that launches the MCP server
needs macOS Screen Recording and Accessibility permission, then the MCP host
must be restarted.

Name the launching process in the user's setup when known, such as Terminal,
Claude Desktop, Codex, or another MCP host.

## Stalled Or Slow Runs

Fresh GUI tasks can take longer than replayed tasks because EchoPath may need
the model bridge for planning and visual grounding. Check whether a GUI run
directory is being updated before calling it stuck.

Useful signals:

- `local_data/gui_runs/<run_id>/manifest.json` status and `step_count`
- latest screenshot under `local_data/gui_runs/<run_id>/screenshots/`
- `echopath_get_status(session_id=...)` when the host supports polling

If the same step repeats without progress, summarize the current screenshot and
halt reason if present rather than blindly retrying.

## Execution Success Versus Learning Failure

EchoPath can finish the visible desktop task and then fail during post-run
learning or consolidation. Treat these as separate outcomes:

- `execution.success=true` means the task runner completed its desktop work.
- `mcp.learning.status=error` or metadata `learning.status=error` means the
  memory update failed after execution.

Report both. Do not mark the user task failed solely because learning failed.
Use `echopath_consolidate(run_id=...)` only if the user wants to retry memory
consolidation or the current task is specifically about learning.

## Replay Misfires

If replay opens the wrong app, clicks the wrong target, or uses a stale UI:

1. Inspect the session debug resource and the last useful screenshot.
2. Check `replay.used`, `source_task_id`, visual re-aiming status, and any
   adaptation fields.
3. If the memory is bad, deprecate it with
   `echopath_consolidate(deprecate_path_id=..., reason=...)`.
4. Rerun only when the user still wants the task performed and the risk is low.

## Privacy And Public Artifacts

EchoPath writes local observations, screenshots, logs, and memory candidates
under ignored local directories. Do not expose raw screenshots or logs in a
public report unless the user asks and you have inspected them for private
content.
