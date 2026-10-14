from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mcp.server import MCPServer

from .contracts import (
    RESOURCE_CONFIG,
    RESOURCE_MEMORY_INDEX,
    RESOURCE_SESSION_DEBUG_TEMPLATE,
    RESOURCE_SESSION_TEMPLATE,
    RESOURCE_SESSIONS,
    RESOURCE_SKILLS,
    SERVER_TITLE,
)
from .service import EchoPathMCPService, default_service


LOCAL_APP_CONTROL_ROOM = 'http://127.0.0.1:8765'


def _local_app_request(path: str, *, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Talk only to the loopback Control Room owned by EchoPath.app."""
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    request = Request(
        LOCAL_APP_CONTROL_ROOM + path,
        data=data,
        headers={'Content-Type': 'application/json'} if data is not None else {},
        method='POST' if data is not None else 'GET',
    )
    try:
        with urlopen(request, timeout=2) as response:
            decoded = json.load(response)
    except (HTTPError, URLError, OSError, ValueError) as error:
        raise RuntimeError(
            'EchoPath.app is not ready for local desktop control. Open it from '
            '/Applications, approve Accessibility and Screen Recording, then reopen it.'
        ) from error
    if not isinstance(decoded, dict):
        raise RuntimeError('EchoPath.app returned an invalid local desktop-control response.')
    return decoded


def _start_local_task_via_app(config_path: Path, task: str, native_handoff: bool = False) -> dict[str, Any]:
    """Submit GUI work to the LaunchServices-started, TCC-authorized app."""
    authorization = _local_app_request('/api/local-desktop-authorization')
    try:
        same_config = Path(str(authorization.get('config_path') or '')).resolve() == config_path.resolve()
    except OSError:
        same_config = False
    if not same_config:
        raise RuntimeError(
            'EchoPath.app is running with a different configuration. Quit it and reopen '
            'the EchoPath.app installed for this workspace before starting a local task.'
        )
    if not bool(authorization.get('app_host')) or not bool(authorization.get('ready')):
        raise RuntimeError(
            str(authorization.get('detail') or '')
            or 'EchoPath.app is not authorized for local desktop control.'
        )
    response = _local_app_request(
        '/api/tasks',
        payload={'task': task, 'desktop': True, 'endpoint': '', 'native_handoff': bool(native_handoff)},
    )
    task_record = response.get('task')
    if not isinstance(task_record, dict) or not task_record.get('id'):
        raise RuntimeError('EchoPath.app did not return a tracked local task.')
    return task_record


def build_mcp_server(
    config_path: Optional[Path] = None,
    *,
    service: Optional[EchoPathMCPService] = None,
) -> MCPServer:
    echopath = service or default_service(config_path)
    mcp = MCPServer(SERVER_TITLE)

    @mcp.tool(name='echopath_list_runs')
    def echopath_list_runs() -> dict[str, Any]:
        """List ongoing, paused and recent runs across CLI, MCP and dashboard workers."""
        from ..run_state import RunRegistry
        return {'runs': RunRegistry(echopath.config_path).list()}

    @mcp.tool(name='echopath_control_run')
    def echopath_control_run(run_id: str, action: str) -> dict[str, Any]:
        """Request pause, resume or stop. Wait for paused before taking over the desktop."""
        from ..run_state import RunRegistry
        registry = RunRegistry(echopath.config_path)
        registry.request(run_id, action)
        return {'runs': registry.list()}

    @mcp.tool(name='echopath_start_task')
    def echopath_start_task(
        task: str,
        target: str = 'vm',
        allow_desktop_control: bool = False,
        vm_endpoint: str = '',
        allow_native_handoff: bool = False,
    ) -> dict[str, Any]:
        """Start a tracked local or VM task and return immediately with its task ID.

        Use echopath_list_runs or echopath_control_run to follow and control it.
        VM desktop control must be explicitly enabled.
        With allow_native_handoff, a replay may stop at status awaiting_native:
        complete the task's native_handoff subgoal on that desktop, then call
        echopath_resume_task with its declared outputs.
        """
        if allow_native_handoff and not allow_desktop_control:
            raise ValueError('allow_native_handoff requires allow_desktop_control.')
        normalized_target = str(target).strip().lower()
        if normalized_target not in ('local', 'vm'):
            raise ValueError("target must be 'local' or 'vm'.")
        if normalized_target == 'local' and allow_desktop_control:
            return {'task': _start_local_task_via_app(echopath.config_path, task, allow_native_handoff)}

        from ..dashboard import TaskRunner
        runner = TaskRunner(echopath.config_path)
        endpoint = ''
        if normalized_target == 'vm':
            endpoint = str(vm_endpoint).strip() or str(runner.vm.get('endpoint', '')).strip()
            if not endpoint:
                raise ValueError('No connected VM bridge is available. Provide vm_endpoint.')
        task_record = runner.start(task, allow_desktop_control, endpoint, bool(allow_native_handoff))
        return {'task': task_record}

    @mcp.tool(name='echopath_resume_task')
    def echopath_resume_task(run_id: str, outputs: Optional[dict[str, str]] = None,
                             cancel: bool = False) -> dict[str, Any]:
        """Answer a tracked task that is awaiting_native: supply its declared outputs, or cancel.

        The worker verifies the live return state before continuing; poll echopath_get_task.
        """
        from ..run_state import RunRegistry
        registry = RunRegistry(echopath.config_path)
        registry.answer_native(run_id, outputs, cancel)
        return {'task': registry.get(run_id)}

    @mcp.tool(name='echopath_list_procedures')
    def echopath_list_procedures() -> dict[str, Any]:
        """List stored procedures (learned paths and replay-library records) with id, store, title and step count.

        Check this before starting a desktop task: a matching active procedure replays
        instead of planning from scratch. Use echopath_get_procedure for its replay phrase.
        """
        from ..dashboard import TaskRunner
        return {'procedures': TaskRunner(echopath.config_path).procedures()}

    @mcp.tool(name='echopath_get_procedure')
    def echopath_get_procedure(store: str, procedure_id: str) -> dict[str, Any]:
        """Return one stored procedure: its replay phrase, parameters and every step.

        Start it with echopath_start_task using the replay phrase as the task,
        changing only the parameter values the new task needs.
        """
        from ..dashboard import TaskRunner
        return {'procedure': TaskRunner(echopath.config_path).procedure(store, procedure_id)}

    @mcp.tool(name='echopath_get_task')
    def echopath_get_task(run_id: str) -> dict[str, Any]:
        """Return a single tracked task, including status, activity and final result."""
        from ..run_state import RunRegistry
        return {'task': RunRegistry(echopath.config_path).get(run_id)}

    @mcp.tool(name="echopath_create_session")
    def echopath_create_session(title: str = "New chat") -> dict[str, Any]:
        """Create an EchoPath chat session and return its session_id."""
        return echopath.create_session(title=title)

    @mcp.tool(name="echopath_run_task")
    def echopath_run_task(
        message: str,
        session_id: Optional[str] = None,
        allow_desktop_control: bool = False,
        allow_native_handoff: bool = False,
    ) -> dict[str, Any]:
        """Send one task to EchoPath with live desktop control disabled by default.

        Enable allow_native_handoff only if your native computer tools control the
        same desktop. On awaiting_native, complete the supplied subgoal and call
        echopath_resume with its token and declared outputs.
        """
        return echopath.run_task(
            message=message,
            session_id=session_id,
            allow_desktop_control=allow_desktop_control,
            allow_native_handoff=allow_native_handoff,
        )

    @mcp.tool(name="echopath_resume")
    def echopath_resume(session_id: str, continuation_token: str,
                        outputs: Optional[dict[str, str]] = None, cancel: bool = False) -> dict[str, Any]:
        """Resume after native computer use. Supply declared outputs; return state is verified.

        Use cancel=true to abandon a pending segment without executing further actions.
        Continuation tokens are single use and expire when this server restarts.
        """
        return echopath.resume(session_id, continuation_token, outputs, cancel)

    @mcp.tool(name="echopath_get_session")
    def echopath_get_session(session_id: str) -> dict[str, Any]:
        """Return an EchoPath session with messages and debug state."""
        return echopath.get_session(session_id)

    @mcp.tool(name="echopath_list_sessions")
    def echopath_list_sessions() -> dict[str, Any]:
        """List in-memory EchoPath sessions for this MCP server process."""
        return echopath.list_sessions()

    @mcp.tool(name="echopath_get_status")
    def echopath_get_status(session_id: str) -> dict[str, Any]:
        """Return the current debug status for an EchoPath session."""
        return echopath.get_status(session_id)

    @mcp.tool(name="echopath_smoke_check")
    def echopath_smoke_check(include_model_bridge: bool = False) -> dict[str, Any]:
        """Run safe EchoPath health checks."""
        return echopath.smoke_check(include_model_bridge=include_model_bridge)

    @mcp.tool(name="echopath_consolidate")
    def echopath_consolidate(
        run_id: str = "",
        task_intent: str = "",
        deprecate_path_id: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        """Consolidate a recorded GUI run into memory, or deprecate a memory."""
        return echopath.consolidate(
            run_id=run_id,
            task_intent=task_intent,
            deprecate_path_id=deprecate_path_id,
            reason=reason,
        )

    @mcp.tool(name="echopath_list_memories")
    def echopath_list_memories(
        include_candidates: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List learned memories from both memory stores."""
        return echopath.list_memories(
            include_candidates=include_candidates,
            limit=limit,
        )

    @mcp.tool(name="echopath_clear_history")
    def echopath_clear_history(confirm: bool = False) -> dict[str, Any]:
        """Clear in-memory sessions and local EchoPath history artifacts."""
        return echopath.clear_history(confirm=confirm)

    @mcp.resource(RESOURCE_CONFIG, mime_type="application/json")
    def echopath_config() -> dict[str, Any]:
        """Credential-free normalized EchoPath configuration."""
        return echopath.config_resource()

    @mcp.resource(RESOURCE_SKILLS, mime_type="application/json")
    def echopath_skills() -> dict[str, Any]:
        """Configured EchoPath skill names and markdown paths."""
        return echopath.skills_resource()

    @mcp.resource(RESOURCE_SESSIONS, mime_type="application/json")
    def echopath_sessions() -> dict[str, Any]:
        """In-memory EchoPath sessions for this MCP process."""
        return echopath.sessions_resource()

    @mcp.resource(RESOURCE_MEMORY_INDEX, mime_type="application/json")
    def echopath_consolidated_memory_index() -> dict[str, Any]:
        """Local consolidated replay memory index, when present."""
        return echopath.consolidated_memory_index_resource()

    @mcp.resource(RESOURCE_SESSION_TEMPLATE, mime_type="application/json")
    def echopath_session(session_id: str) -> dict[str, Any]:
        """Session messages and debug metadata."""
        return echopath.get_session(session_id)

    @mcp.resource(RESOURCE_SESSION_DEBUG_TEMPLATE, mime_type="application/json")
    def echopath_session_debug(session_id: str) -> dict[str, Any]:
        """Current projected run status for a session."""
        return echopath.get_status(session_id)

    @mcp.prompt(name="echopath_plan_task")
    def echopath_plan_task(task: str) -> str:
        """Ask EchoPath to produce a plan without executing actions."""
        return echopath.plan_task_prompt(task)

    @mcp.prompt(name="echopath_visible_desktop_task")
    def echopath_visible_desktop_task(task: str) -> str:
        """Template for explicitly authorized visible desktop work."""
        return echopath.visible_desktop_task_prompt(task)

    @mcp.prompt(name="echopath_replay_diagnostics")
    def echopath_replay_diagnostics(session_id: str) -> str:
        """Inspect replay and session diagnostics for a task."""
        return echopath.replay_diagnostics_prompt(session_id)

    return mcp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run EchoPath as an MCP server.")
    parser.add_argument(
        "--config",
        default=str(Path.cwd() / "echopath.config.json"),
        help="Path to echopath.config.json.",
    )
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=("stdio", "streamable-http", "sse"),
        help="MCP transport. stdio is the default for local hosts.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP/SSE host.")
    parser.add_argument("--port", type=int, default=8000, help="HTTP/SSE port.")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    server = build_mcp_server(Path(args.config))
    if args.transport == "stdio":
        server.run()
    else:
        server.run(transport=args.transport, host=args.host, port=args.port)
    return 0


mcp = build_mcp_server()


if __name__ == "__main__":
    raise SystemExit(main())
