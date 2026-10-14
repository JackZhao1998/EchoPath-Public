from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, MutableMapping, Optional, Union

from .runtime import AgentContext
from .skill import MarkdownSkill


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SKILL_DIRECTORY = ROOT / "skills" / "workspace_files"


async def workspace_files_executor(
    payload: Mapping[str, Any],
    context: AgentContext,
) -> Dict[str, Any]:
    actions = list(_coerce_actions(payload))
    results = []
    values: Dict[str, Any] = {}

    for index, action in enumerate(actions, start=1):
        from .run_state import checkpoint
        await checkpoint()
        action_payload = _normalize_action(index, action)
        action_name = action_payload["action"]

        if action_name == "read":
            path = _resolve_workspace_path(context.working_directory, action_payload["path"])
            content = path.read_text(encoding="utf-8")
            results.append(
                {
                    "index": index,
                    "action": "read",
                    "path": str(path),
                    "chars": len(content),
                    "status": "ok",
                }
            )
            values[action_payload["result_key"]] = content
            continue

        if action_name == "write":
            path = _resolve_workspace_path(context.working_directory, action_payload["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(action_payload["content"], encoding="utf-8")
            results.append(
                {"index": index, "action": "write", "path": str(path), "status": "ok"}
            )
            values[action_payload["result_key"]] = str(path)
            continue

        if action_name == "append":
            path = _resolve_workspace_path(context.working_directory, action_payload["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(action_payload["content"])
            results.append(
                {"index": index, "action": "append", "path": str(path), "status": "ok"}
            )
            values[action_payload["result_key"]] = str(path)
            continue

        if action_name == "replace_text":
            path = _resolve_workspace_path(context.working_directory, action_payload["path"])
            original = path.read_text(encoding="utf-8")
            old_text = action_payload["old_text"]
            new_text = action_payload["new_text"]
            replace_all = action_payload["replace_all"]
            if old_text not in original:
                raise ValueError(
                    f"Text to replace was not found in '{path}': {old_text[:80]}"
                )
            updated = (
                original.replace(old_text, new_text)
                if replace_all
                else original.replace(old_text, new_text, 1)
            )
            path.write_text(updated, encoding="utf-8")
            replacements = original.count(old_text) if replace_all else 1
            results.append(
                {
                    "index": index,
                    "action": "replace_text",
                    "path": str(path),
                    "replacements": replacements,
                    "status": "ok",
                }
            )
            values[action_payload["result_key"]] = {
                "path": str(path),
                "replacements": replacements,
            }
            continue

        if action_name == "list_dir":
            path = _resolve_workspace_path(context.working_directory, action_payload["path"])
            if not path.is_dir():
                raise ValueError(f"Directory '{path}' does not exist.")
            children = sorted(item.name for item in path.iterdir())
            results.append(
                {
                    "index": index,
                    "action": "list_dir",
                    "path": str(path),
                    "count": len(children),
                    "status": "ok",
                }
            )
            values[action_payload["result_key"]] = children
            continue

        raise ValueError(f"Unsupported workspace-files action '{action_name}'.")

    return {
        "skill": payload.get("selected_skill_name", "workspace-files"),
        "action_count": len(actions),
        "results": results,
        "values": values,
    }


def build_workspace_files_skill(
    *,
    skill_directory: Optional[Union[Path, str]] = None,
    name: str = "workspace-files",
    description: str = "Read, write, append, and edit files inside the EchoPath workspace.",
) -> MarkdownSkill:
    directory = Path(skill_directory) if skill_directory is not None else DEFAULT_SKILL_DIRECTORY
    return MarkdownSkill.from_directory(
        directory,
        name=name,
        description=description,
        executor=workspace_files_executor,
    )


def _coerce_actions(payload: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    payload_actions = payload.get("actions")
    if payload_actions is not None:
        if not isinstance(payload_actions, list):
            raise ValueError("workspace-files skill payload 'actions' must be a list.")
        return payload_actions
    raise ValueError("workspace-files skill requires an 'actions' list.")


def _normalize_action(index: int, action: Mapping[str, Any]) -> MutableMapping[str, Any]:
    if not isinstance(action, Mapping):
        raise ValueError(f"Workspace action #{index} must be a mapping.")

    action_name = str(action.get("action") or "").strip().lower()
    if not action_name:
        raise ValueError(f"Workspace action #{index} is missing an 'action' field.")

    normalized = dict(action)
    normalized["action"] = action_name
    normalized["result_key"] = str(action.get("result_key") or f"file_{index}")

    if action_name in {"read", "write", "append", "replace_text", "list_dir"}:
        normalized["path"] = _require_string(action, index, "path")

    if action_name in {"write", "append"}:
        normalized["content"] = _require_string(action, index, "content")
        return normalized

    if action_name == "replace_text":
        normalized["old_text"] = _require_string(action, index, "old_text")
        normalized["new_text"] = str(action.get("new_text", ""))
        normalized["replace_all"] = bool(action.get("replace_all", False))
        return normalized

    if action_name in {"read", "list_dir"}:
        return normalized

    return normalized


def _require_string(action: Mapping[str, Any], index: int, field_name: str) -> str:
    value = action.get(field_name)
    if value is None or not str(value).strip():
        raise ValueError(f"Workspace action #{index} requires a non-empty '{field_name}'.")
    return str(value)


def _resolve_workspace_path(root: Path, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = (root / candidate).resolve()
    else:
        candidate = candidate.resolve()

    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        raise ValueError(f"Path '{candidate}' is outside the workspace '{root}'.") from error
    return candidate
