# EchoPath MCP Architecture

This branch exposes EchoPath as a Model Context Protocol server while keeping the
existing EchoPath runtime intact.

## Boundary

EchoPath remains responsible for task routing, planning, grounding, execution,
logging, and replay. MCP is the outer integration boundary for hosts that want to
call EchoPath as a local tool provider.

```text
MCP host
  -> EchoPath MCP server
      -> EchoPathMCPService
          -> EchoPathChatService
              -> OrchestratorAgent
                  -> PlanningAgent
                  -> GUIAgent / ToolAgent
                  -> ExecutionAgent
                  -> desktop / command / file skills
```

## Package Layout

- `echopath.mcp.contracts`: public MCP names, URI constants, and schema metadata.
- `echopath.mcp.service`: EchoPath-facing service layer, safety gates, resources,
  prompts, and health checks.
- `echopath.mcp.server`: official MCP Python SDK adapter and executable entrypoint.

The service layer intentionally has no dependency on the MCP SDK. This keeps the
EchoPath behavior testable without a live MCP host, model bridge, or desktop.

## Safety Model

`echopath_run_task` disables the `pyautogui-desktop` skill unless
`allow_desktop_control=true` is passed for that tool call. When desktop control is
disabled:

- the desktop skill is removed from the per-call skill allowlist,
- the desktop controller is not allocated for the session,
- planner context receives a structured execution constraint explaining that GUI
  actions are not authorized.

Command and workspace-file skills remain governed by EchoPath config and by the
host application's normal tool-approval flow.

## MCP Surface

Tools:

- `echopath_create_session`
- `echopath_run_task`
- `echopath_get_session`
- `echopath_list_sessions`
- `echopath_get_status`
- `echopath_smoke_check`
- `echopath_consolidate`
- `echopath_list_memories`
- `echopath_clear_history`

Resources:

- `echopath://config`
- `echopath://skills`
- `echopath://sessions`
- `echopath://sessions/{session_id}`
- `echopath://sessions/{session_id}/debug`
- `echopath://memory/consolidated/index`

Prompts:

- `echopath_plan_task`
- `echopath_visible_desktop_task`
- `echopath_replay_diagnostics`

## Transport

The default transport is stdio, which matches local MCP hosts. The SDK adapter
also exposes `streamable-http` and `sse` for later deployment experiments.
