---
name: echopath-mcp
description: Use when operating, testing, or troubleshooting the EchoPath MCP server, especially visible desktop tasks, replay diagnostics, memory consolidation, and concise MCP result summaries. Do not use for ordinary EchoPath source-code edits unless the request is about MCP behavior.
metadata:
  short-description: Guide agents through EchoPath MCP tools and desktop safety gates
---

# EchoPath MCP

Use this skill when EchoPath is available through MCP and the user wants an
agent to run, test, inspect, or diagnose EchoPath behavior through that MCP
surface.

## Mental Model

EchoPath MCP is a live capability layer. Let the MCP tools own sessions,
desktop execution, replay, memory, logs, and resources. This skill only guides
when to call those tools, what permissions matter, and how to summarize the
result without dumping raw run JSON.

When EchoPath MCP tools are directly available, use them instead of shelling out
to local scripts. Use a local stdio client only when the task is explicitly an
installation or integration test and the host has not connected the MCP server
as callable tools.

## Default Workflow

1. Start with `echopath_smoke_check(include_model_bridge=false)` for setup,
   health, or first-use requests. Use `include_model_bridge=true` when the user
   is validating installation or a model-backed task.
2. For non-desktop tasks, call `echopath_run_task` with
   `allow_desktop_control=false` unless the user explicitly asks for visible GUI
   work.
3. For visible desktop tasks, require clear user intent before setting
   `allow_desktop_control=true`. If the host exposes MCP prompts, prefer
   `echopath_visible_desktop_task` to frame the task text.
4. For long runs, wait for the tool result. If the host allows concurrent
   polling, use `echopath_get_status` or the session debug resource sparingly.
5. After completion, summarize the user-visible outcome plus `session_id`, GUI
   run id when present, replay use, execution success/error, and learning
   status. Separate post-run learning failures from desktop execution failures.

Read [references/tool-workflows.md](references/tool-workflows.md) when you need
the exact tool recipes, result fields, or memory workflows.

## Native segments

If the host has native computer tools for the same desktop, enable
`allow_native_handoff=true` alongside `allow_desktop_control=true` on a run.
On `status: awaiting_native`, read `native_handoff`, execute only its bounded
subgoal with native tools, and call `echopath_resume` with the returned
`session_id`, `continuation_token`, and exactly the declared string `outputs`.
EchoPath verifies a fresh observation before continuing. A new handoff may request
another segment or correction of an unverified return state; use the new token.
Never restart the original task or replay the segment's recorded example actions
to simulate resume. Tokens are single use and expire with the server process.
Use `cancel=true` on `echopath_resume` to abandon a pending continuation.
Only report completion after `result.output.execution.success` is true.
Native handoff does not extend the user's authorization or the safety boundaries below.

Read [references/troubleshooting.md](references/troubleshooting.md) when a smoke
check fails, a desktop task stalls, permissions look wrong, replay misfires, or
learning/consolidation reports an error.

## Safety Boundaries

- Do not enable desktop control for passwords, payments, security prompts,
  destructive confirmation dialogs, or private content unless the user has
  explicitly asked for that exact visible-desktop operation and the action is
  appropriate to perform.
- Treat `echopath_clear_history(confirm=true)` as destructive. Call it only when
  the user explicitly requests clearing EchoPath history.
- Watch for local screenshots and logs in responses. Share compact file paths
  or links only when they help the user verify the run.
