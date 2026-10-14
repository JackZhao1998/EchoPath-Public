from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import sys

from echopath.chat_service import EchoPathChatService
from echopath.memory_loop import RUN_MANIFEST_EVALUATOR, consolidate_completed_run
from echopath.consolidated_memory_library import (
    ConsolidatedMemoryLibrary,
    ConsolidatedMemoryLibraryError,
)
from echopath.config import (
    ConfigError,
    build_model_router_from_global_config,
    load_global_model_router_config,
    serialize_global_model_router_config,
)
from echopath.router import ModelRequest

from .contracts import (
    DESKTOP_SKILL_NAME,
    PROMPT_DEFINITIONS,
    RESOURCE_CONFIG,
    RESOURCE_DEFINITIONS,
    RESOURCE_MEMORY_INDEX,
    RESOURCE_SESSION_DEBUG_TEMPLATE,
    RESOURCE_SESSION_TEMPLATE,
    RESOURCE_SESSIONS,
    RESOURCE_SKILLS,
    RESOURCE_TEMPLATE_DEFINITIONS,
    TOOL_DEFINITIONS,
    JSON,
)


class EchoPathMCPError(RuntimeError):
    pass


def package_version() -> str:
    try:
        return metadata.version("echopath-core")
    except metadata.PackageNotFoundError:
        return "0.1.0"


@dataclass
class EchoPathMCPService:
    config_path: Path
    chat_service: EchoPathChatService

    @classmethod
    def from_config_path(
        cls,
        config_path: Path,
        *,
        chat_service_factory: Callable[..., EchoPathChatService] = EchoPathChatService,
    ) -> "EchoPathMCPService":
        resolved_config_path = Path(config_path).expanduser().resolve()
        root = resolved_config_path.parent
        return cls(
            config_path=resolved_config_path,
            chat_service=chat_service_factory(
                config_path=resolved_config_path,
                workspace_directory=root,
                download_directory=root / "downloads",
                log_directory=root / "log",
            ),
        )

    def tool_definitions(self) -> list[JSON]:
        return copy.deepcopy(TOOL_DEFINITIONS)

    def resource_definitions(self) -> list[JSON]:
        resources = copy.deepcopy(RESOURCE_DEFINITIONS)
        for session in self.chat_service.list_sessions():
            session_id = str(session.get("session_id", ""))
            if not session_id:
                continue
            resources.append(
                {
                    "uri": f"echopath://sessions/{session_id}",
                    "name": f"EchoPath session {session_id}",
                    "description": "Session messages and debug metadata.",
                    "mimeType": "application/json",
                }
            )
            resources.append(
                {
                    "uri": f"echopath://sessions/{session_id}/debug",
                    "name": f"EchoPath session {session_id} debug state",
                    "description": "Current projected run status for this session.",
                    "mimeType": "application/json",
                }
            )
        return resources

    def resource_template_definitions(self) -> list[JSON]:
        return copy.deepcopy(RESOURCE_TEMPLATE_DEFINITIONS)

    def prompt_definitions(self) -> list[JSON]:
        return copy.deepcopy(PROMPT_DEFINITIONS)

    def create_session(self, title: str = "New chat") -> JSON:
        return self.chat_service.create_session(title=title or "New chat")

    def list_sessions(self) -> JSON:
        return {"sessions": self.chat_service.list_sessions()}

    def get_session(self, session_id: str) -> JSON:
        session = self.chat_service.get_session(require_text(session_id, "session_id"))
        if session is None:
            raise EchoPathMCPError(f"EchoPath session '{session_id}' was not found.")
        return session

    def get_status(self, session_id: str) -> JSON:
        session = self.get_session(session_id)
        return {
            "session_id": session_id,
            "debug_status": session.get("debug_status"),
            "debug_state": session.get("debug_state", {}),
        }

    def run_task(
        self,
        message: str,
        session_id: Optional[str] = None,
        allow_desktop_control: bool = False,
        allow_native_handoff: bool = False,
    ) -> JSON:
        normalized_message = require_text(message, "message")
        normalized_session_id = optional_text(session_id, "session_id")
        config = load_global_model_router_config(self.config_path)
        enabled_skills = list(config.skills.enabled)

        execution_constraints: JSON = {
            "source": "mcp",
            "desktop_control_allowed": bool(allow_desktop_control),
            "native_handoff_supported": bool(allow_native_handoff and allow_desktop_control),
        }
        if not allow_desktop_control:
            enabled_skills = [name for name in enabled_skills if name != DESKTOP_SKILL_NAME]
            execution_constraints["desktop_control_guidance"] = (
                "Live desktop control is disabled for this MCP tool call. "
                "If the user request requires GUI actions, do not attempt the task; "
                "explain that allow_desktop_control must be true."
            )
        execution_constraints["enabled_skills"] = list(enabled_skills)

        response = self.chat_service.send_message(
            message=normalized_message,
            session_id=normalized_session_id,
            enabled_skills=enabled_skills,
            execution_constraints=execution_constraints,
        )
        response["mcp"] = {
            "allow_desktop_control": bool(allow_desktop_control),
            "allow_native_handoff": bool(allow_native_handoff),
            "desktop_skill_enabled": DESKTOP_SKILL_NAME in enabled_skills,
        }
        result = response.get("result")
        output = result.get("output") if isinstance(result, Mapping) else None
        learning = output.get("learning") if isinstance(output, Mapping) else None
        if isinstance(learning, Mapping):
            response["mcp"]["learning"] = {
                "status": learning.get("status"),
                "path_id": learning.get("path_id"),
                "promoted": bool(learning.get("promoted")),
            }
        return response

    def resume(self, session_id: str, continuation_token: str,
               outputs: Optional[JSON] = None, cancel: bool = False) -> JSON:
        """Resume the frozen replay after native work, or release it without acting."""
        return self.chat_service.resume_native(
            session_id=require_text(session_id, "session_id"),
            continuation_token=require_text(continuation_token, "continuation_token"),
            outputs={} if outputs is None else outputs, cancel=cancel,
        )

    def clear_history(self, confirm: bool = False) -> JSON:
        if not confirm:
            raise EchoPathMCPError("Set confirm=true to clear EchoPath history.")
        return self.chat_service.clear_history()

    def consolidate(
        self,
        *,
        run_id: str = "",
        task_intent: str = "",
        deprecate_path_id: str = "",
        reason: str = "",
    ) -> JSON:
        """Consolidate a recorded GUI run into memory, or deprecate a memory path."""
        repository = self.chat_service.memory_repository

        if str(deprecate_path_id or "").strip():
            metadata = repository.set_path_status(
                str(deprecate_path_id).strip(),
                "deprecated",
                reason=str(reason or "").strip() or "deprecated via echopath_consolidate",
            )
            return {"action": "deprecated", "path": metadata}

        run_directory = self._resolve_gui_run_directory(str(run_id or "").strip())
        if run_directory is None:
            raise EchoPathMCPError(
                "No completed GUI run was found to consolidate. "
                "Run a desktop task first, or pass an explicit run_id."
            )
        manifest = read_json_file(run_directory / "manifest.json")
        resolved_intent = (
            str(task_intent or "").strip()
            or str(manifest.get("task_goal") or "").strip()
            or run_directory.name
        )
        # A re-run has no live goal verification; the manifest's completion
        # status is the weaker evaluator, and the response names it.
        artifact_validation = {
            "passed": str(manifest.get("status") or "").strip().lower() == "completed",
            "evaluator": RUN_MANIFEST_EVALUATOR,
            "assessment": f"run manifest status: {manifest.get('status')}",
            "verified_at": manifest.get("finished_at") or manifest.get("updated_at"),
            "source": "gui_run_manifest",
        }
        config = load_global_model_router_config(self.config_path)
        router = build_model_router_from_global_config(config)
        summary = run_sync_awaitable(
            consolidate_completed_run(
                router,
                run_directory,
                task_intent=resolved_intent,
                memory_repository=repository,
                artifact_validation=artifact_validation,
                platform=sys.platform,
            )
        )
        return {"action": "consolidated", "evaluator": RUN_MANIFEST_EVALUATOR, **summary}

    def list_memories(
        self,
        *,
        include_candidates: bool = False,
        limit: int = 50,
    ) -> JSON:
        """Merged view of learned memory paths and consolidated replay records."""
        normalized_limit = max(1, int(limit or 50))
        statuses = None if include_candidates else ["active"]
        repo_entries = [
            {"store": "memory-repo", **entry}
            for entry in self.chat_service.memory_repository.list_paths(
                statuses=statuses, limit=normalized_limit
            )
        ]

        library_entries: list[JSON] = []
        index = self.consolidated_memory_index_resource()
        for item in list(index.get("memories") or [])[:normalized_limit]:
            if isinstance(item, Mapping):
                library_entries.append({"store": "consolidated-library", **dict(item)})

        return {
            "memory_repo": repo_entries,
            "consolidated_library": library_entries,
            "counts": {
                "memory_repo": len(repo_entries),
                "consolidated_library": len(library_entries),
            },
        }

    def _resolve_gui_run_directory(self, run_id: str) -> Optional[Path]:
        root = self.config_path.parent / "local_data" / "gui_runs"
        if run_id:
            candidate = root / run_id
            return candidate if (candidate / "manifest.json").is_file() else None
        best: Optional[Path] = None
        best_stamp = ""
        if not root.is_dir():
            return None
        for run_dir in root.iterdir():
            manifest_path = run_dir / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = read_json_file(manifest_path)
            except EchoPathMCPError:
                continue
            if str(manifest.get("status") or "").strip().lower() != "completed":
                continue
            stamp = str(manifest.get("updated_at") or "")
            if stamp > best_stamp:
                best_stamp = stamp
                best = run_dir
        return best

    def smoke_check(self, include_model_bridge: bool = False) -> JSON:
        checks: JSON = {
            "import_ok": True,
            "config_ok": False,
            "model_bridge_checked": False,
            "model_bridge_ok": None,
        }
        try:
            config = load_global_model_router_config(self.config_path)
            checks["config_ok"] = True
            checks["config"] = {
                "mode": config.mode,
                "router_name": config.router_name,
                "enabled_skills": list(config.skills.enabled),
                "desktop_provider": config.desktop.provider,
            }
        except ConfigError as error:
            checks["config_error"] = str(error)
            return checks

        if include_model_bridge:
            checks["model_bridge_checked"] = True
            try:
                router = build_model_router_from_global_config(config)
                response = run_sync_awaitable(
                    router.generate(
                        ModelRequest(
                            model="default",
                            input="Return the exact text: EchoPath MCP smoke OK",
                            instructions="Return plain text only.",
                            max_output_tokens=32,
                        )
                    )
                )
                checks["model_bridge_ok"] = "EchoPath MCP smoke OK" in response.output_text
                checks["model"] = response.model
            except Exception as error:
                checks["model_bridge_ok"] = False
                checks["model_bridge_error"] = str(error)

        return checks

    def config_resource(self) -> JSON:
        config = load_global_model_router_config(self.config_path)
        return serialize_global_model_router_config(config)

    def skills_resource(self) -> JSON:
        config = load_global_model_router_config(self.config_path)
        root = self.config_path.parent
        skills = []
        for skill_name in config.skills.available:
            path = skill_markdown_path(root, skill_name)
            skills.append(
                {
                    "name": skill_name,
                    "enabled": skill_name in config.skills.enabled,
                    "markdown_path": str(path),
                    "markdown_exists": path.exists(),
                }
            )
        return {"skills": skills}

    def sessions_resource(self) -> JSON:
        return self.list_sessions()

    def consolidated_memory_index_resource(self) -> JSON:
        directory = self.config_path.parent / "memory" / "consolidated"
        path = directory / "index.json"
        if not path.exists() and (directory / "records").is_dir():
            # The index is derived from the records, and the runtime rewrites it
            # whenever it learns something, so it is not tracked. Rebuild it
            # rather than reporting an empty library on a fresh checkout.
            try:
                ConsolidatedMemoryLibrary(root_directory=directory).rebuild_index()
            except (ConsolidatedMemoryLibraryError, OSError):
                pass
        if not path.exists():
            return {"records": [], "missing": str(path)}
        return read_json_file(path)

    def read_resource(self, uri: str) -> JSON:
        normalized_uri = require_text(uri, "uri")
        if normalized_uri == RESOURCE_CONFIG:
            return self.config_resource()
        if normalized_uri == RESOURCE_SKILLS:
            return self.skills_resource()
        if normalized_uri == RESOURCE_SESSIONS:
            return self.sessions_resource()
        if normalized_uri == RESOURCE_MEMORY_INDEX:
            return self.consolidated_memory_index_resource()

        session_prefix = "echopath://sessions/"
        if normalized_uri.startswith(session_prefix):
            remainder = normalized_uri[len(session_prefix) :]
            if remainder.endswith("/debug"):
                return self.get_status(remainder[: -len("/debug")])
            return self.get_session(remainder)

        raise EchoPathMCPError(f"Unknown EchoPath MCP resource '{uri}'.")

    def plan_task_prompt(self, task: str) -> str:
        return (
            "Plan this EchoPath task without executing file, command, or desktop actions:\n"
            f"{require_text(task, 'task')}"
        )

    def visible_desktop_task_prompt(self, task: str) -> str:
        return (
            "Run this through EchoPath only if live desktop control is explicitly allowed "
            "on echopath_run_task:\n"
            f"{require_text(task, 'task')}"
        )

    def replay_diagnostics_prompt(self, session_id: str) -> str:
        normalized_session_id = require_text(session_id, "session_id")
        return (
            "Inspect this EchoPath session debug resource and summarize replay, action, "
            "and verification diagnostics: "
            f"echopath://sessions/{normalized_session_id}/debug"
        )


def skill_markdown_path(root: Path, skill_name: str) -> Path:
    if skill_name == "command-line":
        return root / "skills" / "command_line" / "SKILL.md"
    return root / "skills" / skill_name.replace("-", "_") / "SKILL.md"


def read_json_file(path: Path) -> JSON:
    import json

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise EchoPathMCPError(f"Resource file was not found: {path}") from error
    except json.JSONDecodeError as error:
        raise EchoPathMCPError(f"Resource file contains invalid JSON: {path}") from error
    if not isinstance(payload, dict):
        raise EchoPathMCPError(f"Resource file must contain a JSON object: {path}")
    return payload


def require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EchoPathMCPError(f"'{field_name}' must be a non-empty string.")
    return value.strip()


def optional_text(value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise EchoPathMCPError(f"'{field_name}' must be a string when provided.")
    normalized = value.strip()
    return normalized or None


def run_sync_awaitable(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    raise EchoPathMCPError("Cannot run synchronous MCP service call from an active event loop.")


def default_service(config_path: Optional[Path] = None) -> EchoPathMCPService:
    return EchoPathMCPService.from_config_path(Path(config_path or Path.cwd() / "echopath.config.json"))
