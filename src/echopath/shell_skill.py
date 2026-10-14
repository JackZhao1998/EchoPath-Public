from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, MutableMapping, Optional, Union

from .runtime import AgentContext
from .skill import MarkdownSkill


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SKILL_DIRECTORY = ROOT / "skills" / "command_line"


async def command_line_executor(
    payload: Mapping[str, Any],
    context: AgentContext,
) -> Dict[str, Any]:
    commands = list(_coerce_commands(payload))
    results = []
    values: Dict[str, Any] = {}

    for index, command in enumerate(commands, start=1):
        from .run_state import checkpoint
        await checkpoint()
        command_payload = _normalize_command(index, command)
        cwd = _resolve_workspace_path(context.working_directory, command_payload["cwd"])
        completed = await asyncio.to_thread(
            subprocess.run,
            command_payload["command"],
            shell=True,
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=command_payload["timeout_seconds"],
            executable=command_payload["shell"] or None,
        )
        result_key = command_payload["result_key"]
        result = {
            "index": index,
            "command": command_payload["command"],
            "cwd": str(cwd),
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "success": completed.returncode == 0,
        }
        if command_payload["check"] and completed.returncode != 0:
            raise RuntimeError(
                f"Command #{index} failed with exit code {completed.returncode}: "
                f"{command_payload['command']}"
            )
        results.append(result)
        values[result_key] = result

    return {
        "skill": payload.get("selected_skill_name", "command-line"),
        "command_count": len(commands),
        "results": results,
        "values": values,
    }


def build_command_line_skill(
    *,
    skill_directory: Optional[Union[Path, str]] = None,
    name: str = "command-line",
    description: str = "Run shell commands inside the EchoPath workspace.",
) -> MarkdownSkill:
    directory = Path(skill_directory) if skill_directory is not None else DEFAULT_SKILL_DIRECTORY
    return MarkdownSkill.from_directory(
        directory,
        name=name,
        description=description,
        executor=command_line_executor,
    )


def _coerce_commands(payload: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    payload_commands = payload.get("commands")
    if payload_commands is not None:
        if not isinstance(payload_commands, list):
            raise ValueError("command-line skill payload 'commands' must be a list.")
        return payload_commands

    command = payload.get("command")
    if command:
        return [{"command": command}]

    raise ValueError("command-line skill requires a 'commands' list or a top-level 'command'.")


def _normalize_command(index: int, command: Mapping[str, Any]) -> MutableMapping[str, Any]:
    if not isinstance(command, Mapping):
        raise ValueError(f"Command #{index} must be a mapping.")

    normalized = dict(command)
    normalized["command"] = _require_string(command, index, "command")
    normalized["cwd"] = str(command.get("cwd") or ".")
    normalized["shell"] = _resolve_shell(command.get("shell"))
    normalized["timeout_seconds"] = _coerce_timeout(command.get("timeout_seconds", 120))
    normalized["check"] = bool(command.get("check", False))
    normalized["result_key"] = str(command.get("result_key") or f"command_{index}")
    return normalized


def _resolve_shell(requested: Any) -> Optional[str]:
    """Pick the shell to run commands through.

    Returning None lets subprocess use the platform default, which is the only
    correct answer on Windows (COMSPEC / cmd.exe). Hard-coding a POSIX path here
    made every command fail with WinError 3 off macOS.
    """
    candidate = str(requested or "").strip()
    if candidate:
        return candidate
    if os.name == "nt":
        return None
    for option in (os.environ.get("SHELL"), "/bin/zsh", "/bin/bash", "/bin/sh"):
        if option and Path(option).exists():
            return option
    return None


def _coerce_timeout(value: Any) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        timeout = 120.0
    return max(1.0, timeout)


def _require_string(action: Mapping[str, Any], index: int, field_name: str) -> str:
    value = action.get(field_name)
    if value is None or not str(value).strip():
        raise ValueError(f"Command #{index} requires a non-empty '{field_name}'.")
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
