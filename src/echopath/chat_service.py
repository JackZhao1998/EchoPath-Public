from __future__ import annotations

import asyncio
import copy
import os
import secrets
import shutil
import sys
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .config import (
    build_model_router_from_global_config,
    is_remote_desktop_provider,
    load_global_model_router_config,
)
from .amazon_vm_service import AmazonVMDesktopController
from .file_skill import build_workspace_files_skill
from .interaction_logger import InteractionLogger
from .models import generate_id
from .models import AgentEvent
from .orchestrator_agent import OrchestratorAgent
from .gui_wrapper import build_gui_run_wrapper
from .desktop_overlay import DesktopOverlay
from .memory_loop import (
    CompositeReplayStore,
    consolidate_completed_run,
    prune_gui_runs,
)
from .memory_repo import MemoryRepository
from .memory_replay import MemoryBackedReplayStore
from .pyautogui_service import PyAutoGUIController
from .pyautogui_skill import build_pyautogui_desktop_skill
from .replay import InteractionReplayStore
from .shell_skill import build_command_line_skill

DEBUG_EVENT_LIMIT = 60
DEBUG_SCREENSHOT_LIMIT = 10
DEBUG_PROGRESS_LIMIT = 16
DEBUG_ACTION_STEP_LIMIT = 8


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _empty_debug_state() -> Dict[str, Any]:
    return {
        "status": "idle",
        "task_id": None,
        "goal": "",
        "started_at": None,
        "finished_at": None,
        "updated_at": None,
        "error": None,
        "latest_event": None,
        "latest_screenshot": None,
        "events": [],
        "screenshots": [],
        "agent_statuses": [],
        "progress_items": [],
        "action_steps": [],
        "current_focus": None,
        "initial_screenshot": None,
        "goal_verification": None,
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _serialize_debug_event(event: AgentEvent) -> Dict[str, Any]:
    return {
        "name": event.name,
        "message": event.message,
        "payload": _json_safe(event.payload),
        "run_id": event.run_id,
        "level": event.level.value if hasattr(event.level, "value") else str(event.level),
        "timestamp": event.timestamp.isoformat(),
    }


def _extract_debug_screenshot(event: AgentEvent) -> Optional[Dict[str, Any]]:
    if event.name != "agent.desktop.observed" or not isinstance(event.payload, Mapping):
        return None
    path = event.payload.get("path")
    if not path:
        return None
    screenshot = {
        "path": str(path),
        "label": str(event.payload.get("label", "")).strip() or None,
        "iteration": _coerce_optional_int(event.payload.get("iteration")),
        "observation_phase": str(event.payload.get("observation_phase", "")).strip() or None,
        "capture_verified": bool(event.payload.get("capture_verified", False)),
        "file_size_bytes": event.payload.get("file_size_bytes"),
        "timestamp": event.timestamp.isoformat(),
    }
    return _json_safe(screenshot)


def _coerce_optional_int(value: Any) -> Optional[int]:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _short_text(value: Any, *, limit: int = 120) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3].rstrip()}..."


def _describe_debug_agent(agent_name: str) -> Dict[str, Any]:
    normalized = str(agent_name or "").strip().lower()
    if normalized.endswith("-image-grounding"):
        return {"key": "grounder", "label": "Grounder", "order": 4}
    if normalized.endswith("-planning"):
        return {"key": "planner", "label": "Planner", "order": 2}
    if normalized.endswith("-gui"):
        return {"key": "gui", "label": "GUI Agent", "order": 3}
    if normalized.endswith("-tool"):
        return {"key": "tools", "label": "Tool Agent", "order": 5}
    if normalized.endswith("-execution"):
        return {"key": "execution", "label": "Execution Agent", "order": 6}
    if normalized.endswith("-short-term-memory"):
        return {"key": "short_term_memory", "label": "Short-Term Memory", "order": 7}
    if normalized.endswith("-long-term-memory"):
        return {"key": "long_term_memory", "label": "Long-Term Memory", "order": 8}
    if normalized.endswith("orchestrator") or normalized == "orchestrator":
        return {"key": "orchestrator", "label": "Orchestrator", "order": 1}
    fallback_label = str(agent_name or "Agent").strip() or "Agent"
    return {"key": normalized or "agent", "label": fallback_label, "order": 99}


def _upsert_agent_status(
    agent_statuses: Dict[str, Dict[str, Any]],
    *,
    agent_name: str,
    status: str,
    updated_at: str,
    detail: Optional[str] = None,
) -> Dict[str, Any]:
    descriptor = _describe_debug_agent(agent_name)
    current = dict(agent_statuses.get(descriptor["key"], {}))
    next_status = {
        "key": descriptor["key"],
        "label": descriptor["label"],
        "agent_name": agent_name,
        "status": status,
        "updated_at": updated_at,
        "order": descriptor["order"],
        "detail": detail if detail is not None else current.get("detail"),
    }
    agent_statuses[descriptor["key"]] = next_status
    return next_status


def _update_agent_detail(
    agent_statuses: Dict[str, Dict[str, Any]],
    agent_key: str,
    *,
    detail: str,
    updated_at: str,
) -> None:
    current = agent_statuses.get(agent_key)
    if current is None:
        return
    current["detail"] = detail
    current["updated_at"] = updated_at


def _format_task_type_label(task_type: Any) -> str:
    normalized = str(task_type or "").strip().replace("_", " ")
    if not normalized:
        return "Task"
    return normalized.capitalize()


def _format_desktop_action(action: Any) -> str:
    if not isinstance(action, Mapping):
        return ""

    action_name = str(action.get("action", "")).strip().lower()
    target_label = (
        str(action.get("target_description") or "").strip()
        or str(action.get("target_text") or "").strip()
    )
    x = _coerce_optional_int(action.get("x"))
    y = _coerce_optional_int(action.get("y"))

    if action_name in {"click_target", "double_click_target"}:
        prefix = "Double-click" if action_name == "double_click_target" else "Click"
        return f"{prefix} {target_label or 'the target'}"
    if action_name == "click":
        clicks = _coerce_optional_int(action.get("clicks")) or 1
        prefix = "Double-click" if clicks == 2 else "Click"
        if clicks > 2:
            prefix = f"Click {clicks} times"
        if x is not None and y is not None:
            return f"{prefix} at ({x}, {y})"
        return prefix
    if action_name in {"move", "move_target"}:
        if target_label:
            return f"Move to {target_label}"
        if x is not None and y is not None:
            return f"Move to ({x}, {y})"
        return "Move the cursor"
    if action_name in {"type", "paste_text", "write"}:
        typed_text = _short_text(action.get("text"), limit=48)
        verb = "Paste" if action_name == "paste_text" else "Type"
        return f"{verb} \"{typed_text}\"" if typed_text else f"{verb} into the focused field"
    if action_name == "press":
        key = str(action.get("key") or "").strip()
        return f"Press {key}" if key else "Press a key"
    if action_name == "hotkey":
        keys = action.get("keys")
        if isinstance(keys, list) and keys:
            return f"Press {' + '.join(str(key) for key in keys)}"
        return "Press a shortcut"
    if action_name == "scroll":
        clicks = _coerce_optional_int(action.get("clicks"))
        if clicks is None:
            return "Scroll"
        direction = "down" if clicks < 0 else "up"
        return f"Scroll {direction}"
    if action_name == "wait":
        seconds = action.get("seconds")
        if seconds is not None:
            return f"Wait {seconds} seconds"
        return "Wait"
    return action_name.replace("_", " ").capitalize() or "Desktop action"


def _build_progress_item(
    *,
    event: Mapping[str, Any],
    kind: str,
    status: str,
    title: str,
    detail: str = "",
    agent_label: Optional[str] = None,
    iteration: Optional[int] = None,
) -> Dict[str, Any]:
    return {
        "id": f"{event.get('name', 'event')}:{event.get('timestamp', '')}:{iteration or 'none'}",
        "kind": kind,
        "status": status,
        "title": title,
        "detail": detail,
        "agent_label": agent_label,
        "iteration": iteration,
        "timestamp": event.get("timestamp"),
    }


def _ensure_action_step(
    action_steps: Dict[int, Dict[str, Any]],
    *,
    iteration: Optional[int],
    timestamp: str,
) -> Optional[Dict[str, Any]]:
    if iteration is None:
        return None
    current = action_steps.get(iteration)
    if current is None:
        current = {
            "iteration": iteration,
            "title": f"Action {iteration}",
            "status": "running",
            "workflow": None,
            "action": "",
            "expected_outcome": "",
            "planner_assessment": "",
            "grounding": "",
            "verification": "",
            "blocker": None,
            "screenshot": None,
            "source": "planned",
            "source_label": "Planned",
            "timestamp": timestamp,
            "updated_at": timestamp,
        }
        action_steps[iteration] = current
    else:
        current["updated_at"] = timestamp
    return current


def _set_action_step_source(step: Optional[Dict[str, Any]], *, source: str) -> None:
    if step is None:
        return
    normalized = str(source or "").strip().lower()
    if normalized == "replay":
        step["source"] = "replay"
        step["source_label"] = "Replay"
        step["title"] = f"Replay Step {step.get('iteration')}"
        return
    step.setdefault("source", "planned")
    step.setdefault("source_label", "Planned")


def _seed_replay_preview_step(
    action_steps: Dict[int, Dict[str, Any]],
    *,
    preview: Mapping[str, Any],
    timestamp: str,
) -> None:
    iteration = _coerce_optional_int(preview.get("iteration")) or _coerce_optional_int(preview.get("index"))
    step = _ensure_action_step(action_steps, iteration=iteration, timestamp=timestamp)
    if step is None:
        return
    _set_action_step_source(step, source="replay")
    action_label = _short_text(preview.get("action_label"))
    expected_outcome = _short_text(preview.get("expected_outcome"))
    if action_label:
        step["action"] = action_label
    if expected_outcome:
        step["expected_outcome"] = expected_outcome
    step["status"] = step.get("status") or "running"


def _derive_debug_projection(debug_state: Mapping[str, Any]) -> Dict[str, Any]:
    snapshot = dict(debug_state or _empty_debug_state())
    raw_events = snapshot.get("events", [])
    events = [item for item in raw_events if isinstance(item, Mapping)]
    agent_statuses: Dict[str, Dict[str, Any]] = {}
    progress_items: List[Dict[str, Any]] = []
    action_steps: Dict[int, Dict[str, Any]] = {}
    initial_screenshot = snapshot.get("initial_screenshot")
    goal_verification = snapshot.get("goal_verification")
    replay_mode_active = False

    for event in events:
        name = str(event.get("name", "")).strip()
        payload = event.get("payload")
        payload_map = payload if isinstance(payload, Mapping) else {}
        timestamp = str(event.get("timestamp") or "")
        iteration = _coerce_optional_int(payload_map.get("iteration"))

        if name == "agent.run.started":
            agent_name = str(payload_map.get("agent", "")).strip()
            if agent_name:
                detail = None
                descriptor = _describe_debug_agent(agent_name)
                if descriptor["key"] == "orchestrator":
                    detail = "Coordinating the run."
                elif descriptor["key"] == "planner":
                    detail = "Choosing the route or next desktop step."
                elif descriptor["key"] == "gui":
                    detail = "Driving the live GUI workflow."
                elif descriptor["key"] == "grounder":
                    detail = "Locating the requested target in the screenshot."
                elif descriptor["key"] == "execution":
                    detail = "Sending actions through the selected skill."
                _upsert_agent_status(
                    agent_statuses,
                    agent_name=agent_name,
                    status="running",
                    updated_at=timestamp,
                    detail=detail,
                )
            continue

        if name == "agent.run.completed":
            agent_name = str(payload_map.get("agent", "")).strip()
            if agent_name:
                _upsert_agent_status(
                    agent_statuses,
                    agent_name=agent_name,
                    status="completed",
                    updated_at=timestamp,
                )
            continue

        if name == "agent.run.failed":
            agent_name = str(payload_map.get("agent", "")).strip()
            error_text = _short_text(payload_map.get("error"))
            if agent_name:
                _upsert_agent_status(
                    agent_statuses,
                    agent_name=agent_name,
                    status="error",
                    updated_at=timestamp,
                    detail=error_text or "The run failed.",
                )
                descriptor = _describe_debug_agent(agent_name)
                progress_items.append(
                    _build_progress_item(
                        event=event,
                        kind="error",
                        status="error",
                        title=f"{descriptor['label']} failed",
                        detail=error_text,
                        agent_label=descriptor["label"],
                    )
                )
            continue

        if name == "agent.planning.route.selected":
            task_type = _format_task_type_label(payload_map.get("task_type"))
            task_summary = _short_text(payload_map.get("task_summary"))
            _update_agent_detail(
                agent_statuses,
                "planner",
                detail=f"Routed this request as {task_type.lower()}.",
                updated_at=timestamp,
            )
            progress_items.append(
                _build_progress_item(
                    event=event,
                    kind="routing",
                    status="completed",
                    title=f"Planner routed the request to {task_type}",
                    detail=task_summary,
                    agent_label="Planner",
                )
            )
            continue

        if name == "agent.replay.used":
            matched_query = _short_text(payload_map.get("matched_user_query"))
            purpose_summary = _short_text(payload_map.get("purpose_summary"))
            matched_on = _short_text(payload_map.get("matched_on"))
            operation_count = _coerce_optional_int(payload_map.get("operation_count"))
            detail = purpose_summary or matched_query
            if operation_count is not None:
                detail = (
                    f"{detail} • replaying {operation_count} stored steps"
                    if detail
                    else f"Replaying {operation_count} stored steps"
                )
            if matched_on:
                detail = f"{detail} • matched on {matched_on.replace('_', ' ')}" if detail else matched_on
            previews = payload_map.get("trajectory_steps_preview")
            if isinstance(previews, list):
                for preview in previews:
                    if isinstance(preview, Mapping):
                        _seed_replay_preview_step(
                            action_steps,
                            preview=preview,
                            timestamp=timestamp,
                        )
            replay_mode_active = True
            progress_items.append(
                _build_progress_item(
                    event=event,
                    kind="replay",
                    status="completed",
                    title="Replay matched a stored action trajectory",
                    detail=detail,
                    agent_label="Orchestrator",
                )
            )
            continue

        if name == "agent.desktop.planned":
            step = _ensure_action_step(action_steps, iteration=iteration, timestamp=timestamp)
            next_step = payload_map.get("next_step")
            assessment = _short_text(payload_map.get("assessment"))
            completed = bool(payload_map.get("completed"))
            action_summary = _format_desktop_action(next_step)
            expected_outcome = (
                str(next_step.get("expected_outcome", "")).strip()
                if isinstance(next_step, Mapping)
                else ""
            )
            if step is not None:
                if replay_mode_active:
                    _set_action_step_source(step, source="replay")
                step["planner_assessment"] = assessment
                step["action"] = action_summary or step.get("action", "")
                step["expected_outcome"] = expected_outcome or step.get("expected_outcome", "")
                step["status"] = "completed" if completed else step.get("status", "running")
            _update_agent_detail(
                agent_statuses,
                "planner",
                detail=assessment or expected_outcome or "Prepared the next desktop step.",
                updated_at=timestamp,
            )
            progress_items.append(
                _build_progress_item(
                    event=event,
                    kind="planning",
                    status="completed" if completed else "running",
                    title=(
                        f"Planner marked iteration {iteration} complete"
                        if completed
                        else f"Planner proposed step {iteration}"
                    ),
                    detail=assessment or expected_outcome or action_summary,
                    agent_label="Planner",
                    iteration=iteration,
                )
            )
            continue

        if name == "agent.desktop.grounded":
            step = _ensure_action_step(action_steps, iteration=iteration, timestamp=timestamp)
            located = bool(payload_map.get("located"))
            x = _coerce_optional_int(payload_map.get("x"))
            y = _coerce_optional_int(payload_map.get("y"))
            reason = _short_text(payload_map.get("reason"))
            detail = reason
            if located and x is not None and y is not None:
                detail = f"Target located at ({x}, {y})"
                if reason:
                    detail = f"{detail} • {reason}"
            if step is not None:
                if replay_mode_active:
                    _set_action_step_source(step, source="replay")
                step["grounding"] = detail
                step["status"] = "running"
            _update_agent_detail(
                agent_statuses,
                "grounder",
                detail=detail or "Grounding finished.",
                updated_at=timestamp,
            )
            progress_items.append(
                _build_progress_item(
                    event=event,
                    kind="grounding",
                    status="completed" if located else "error",
                    title=(
                        f"Grounder found the target for step {iteration}"
                        if located
                        else f"Grounder could not find the target for step {iteration}"
                    ),
                    detail=detail,
                    agent_label="Grounder",
                    iteration=iteration,
                )
            )
            continue

        if name == "agent.desktop.actions.completed":
            step = _ensure_action_step(action_steps, iteration=iteration, timestamp=timestamp)
            action_summary = _format_desktop_action(payload_map.get("action"))
            expected_outcome = _short_text(payload_map.get("expected_outcome"))
            workflow = str(payload_map.get("workflow", "")).strip() or None
            if step is not None:
                if replay_mode_active:
                    _set_action_step_source(step, source="replay")
                step["workflow"] = workflow or step.get("workflow")
                step["action"] = action_summary or step.get("action", "")
                step["expected_outcome"] = expected_outcome or step.get("expected_outcome", "")
                step["status"] = "running"
            _update_agent_detail(
                agent_statuses,
                "gui",
                detail=action_summary or "Sent the next desktop action.",
                updated_at=timestamp,
            )
            progress_items.append(
                _build_progress_item(
                    event=event,
                    kind="action",
                    status="completed",
                    title=f"GUI agent executed step {iteration}",
                    detail=expected_outcome or action_summary,
                    agent_label="GUI Agent",
                    iteration=iteration,
                )
            )
            continue

        if name == "agent.desktop.observed":
            screenshot = {
                "path": str(payload_map.get("path", "")).strip(),
                "label": str(payload_map.get("label", "")).strip() or None,
                "iteration": _coerce_optional_int(payload_map.get("iteration")),
                "observation_phase": str(payload_map.get("observation_phase", "")).strip() or None,
                "capture_verified": bool(payload_map.get("capture_verified", False)),
                "file_size_bytes": payload_map.get("file_size_bytes"),
                "timestamp": timestamp,
            }
            if screenshot["observation_phase"] == "after" and screenshot["iteration"] is not None:
                step = _ensure_action_step(
                    action_steps,
                    iteration=screenshot["iteration"],
                    timestamp=timestamp,
                )
                if step is not None:
                    if replay_mode_active:
                        _set_action_step_source(step, source="replay")
                    step["screenshot"] = screenshot
            elif screenshot["observation_phase"] == "before" and initial_screenshot is None:
                initial_screenshot = screenshot
            continue

        if name == "agent.desktop.step.verified":
            step = _ensure_action_step(action_steps, iteration=iteration, timestamp=timestamp)
            succeeded = bool(payload_map.get("step_succeeded"))
            outcome_status = str(payload_map.get("outcome_status") or "").strip().lower()
            verification_progressed = outcome_status in {"progressed", "pending"}
            assessment = _short_text(payload_map.get("assessment"))
            blocker = _short_text(payload_map.get("blocker"))
            action_summary = _format_desktop_action(payload_map.get("action"))
            if step is not None:
                if replay_mode_active:
                    _set_action_step_source(step, source="replay")
                step["action"] = action_summary or step.get("action", "")
                step["expected_outcome"] = (
                    _short_text(payload_map.get("expected_outcome"))
                    or step.get("expected_outcome", "")
                )
                step["verification"] = assessment
                step["blocker"] = blocker or None
                step["verification_status"] = outcome_status or None
                step["status"] = "completed" if succeeded else "running" if verification_progressed else "error"
            _update_agent_detail(
                agent_statuses,
                "planner",
                detail=assessment or blocker or "Verified the latest step.",
                updated_at=timestamp,
            )
            progress_items.append(
                _build_progress_item(
                    event=event,
                    kind="verification",
                    status="completed" if succeeded else "running" if verification_progressed else "error",
                    title=(
                        f"Planner verified step {iteration}"
                        if succeeded
                        else f"Planner saw progress at step {iteration}"
                        if verification_progressed
                        else f"Planner rejected step {iteration}"
                    ),
                    detail=blocker or assessment,
                    agent_label="Planner",
                    iteration=iteration,
                )
            )
            continue

        if name == "agent.desktop.goal.verified":
            goal_completed = bool(payload_map.get("goal_completed"))
            assessment = _short_text(payload_map.get("assessment"))
            blocker = _short_text(payload_map.get("blocker"))
            workflow = str(payload_map.get("workflow", "")).strip() or None
            goal_verification = {
                "goal_completed": goal_completed,
                "assessment": assessment,
                "blocker": blocker or None,
                "workflow": workflow,
                "iteration": iteration,
                "timestamp": timestamp,
            }
            _update_agent_detail(
                agent_statuses,
                "planner",
                detail=assessment or blocker or "Verified the goal state.",
                updated_at=timestamp,
            )
            progress_items.append(
                _build_progress_item(
                    event=event,
                    kind="goal",
                    status="completed" if goal_completed else "error",
                    title="Goal is visible on screen" if goal_completed else "Goal is still not visible",
                    detail=blocker or assessment,
                    agent_label="Planner",
                    iteration=iteration,
                )
            )
            continue

        if name == "agent.desktop.iterations.exhausted":
            progress_items.append(
                _build_progress_item(
                    event=event,
                    kind="error",
                    status="error",
                    title="Desktop loop hit the iteration limit",
                    detail=_short_text(event.get("message")),
                    agent_label="GUI Agent",
                )
            )
            continue

        if name == "agent.desktop.completed_without_action":
            progress_items.append(
                _build_progress_item(
                    event=event,
                    kind="status",
                    status="completed",
                    title="The desktop task completed without extra input",
                    detail=_short_text(event.get("message")),
                    agent_label="GUI Agent",
                )
            )
            continue

        if name == "agent.memory.update.failed":
            progress_items.append(
                _build_progress_item(
                    event=event,
                    kind="error",
                    status="error",
                    title="A memory update failed",
                    detail=_short_text(payload_map.get("error")),
                    agent_label="Memory",
                )
            )

    status_value = str(snapshot.get("status", "idle")).strip().lower() or "idle"
    if status_value in {"completed", "error"} and "orchestrator" in agent_statuses:
        orchestrator_detail = (
            "The run finished successfully."
            if status_value == "completed"
            else _short_text(snapshot.get("error")) or "The run ended with an error."
        )
        _upsert_agent_status(
            agent_statuses,
            agent_name=str(agent_statuses["orchestrator"].get("agent_name", "echopath-orchestrator")),
            status=status_value,
            updated_at=str(snapshot.get("updated_at") or ""),
            detail=orchestrator_detail,
        )

    action_step_list = [
        dict(item)
        for _, item in sorted(action_steps.items(), key=lambda pair: pair[0])
    ][-DEBUG_ACTION_STEP_LIMIT:]
    progress_list = progress_items[-DEBUG_PROGRESS_LIMIT:]
    agent_status_list = sorted(
        (dict(item) for item in agent_statuses.values()),
        key=lambda item: (item.get("order", 99), item.get("updated_at", "")),
    )
    for item in agent_status_list:
        item.pop("order", None)

    running_agents = [item for item in agent_status_list if item.get("status") == "running"]
    if running_agents:
        active_agent = max(running_agents, key=lambda item: item.get("updated_at") or "")
        current_focus = {
            "title": f"{active_agent.get('label', 'Agent')} is active",
            "detail": active_agent.get("detail") or "Working on the current run.",
            "status": "running",
            "timestamp": active_agent.get("updated_at"),
        }
    elif goal_verification:
        current_focus = {
            "title": (
                "Goal verified on screen"
                if goal_verification.get("goal_completed")
                else "Goal still needs work"
            ),
            "detail": goal_verification.get("blocker")
            or goal_verification.get("assessment")
            or "The planner verified the latest visible state.",
            "status": "completed" if goal_verification.get("goal_completed") else "error",
            "timestamp": goal_verification.get("timestamp"),
        }
    elif action_step_list:
        latest_step = action_step_list[-1]
        current_focus = {
            "title": latest_step.get("title") or "Latest action",
            "detail": latest_step.get("verification")
            or latest_step.get("blocker")
            or latest_step.get("expected_outcome")
            or latest_step.get("action")
            or "Reviewing the latest desktop step.",
            "status": latest_step.get("status") or "running",
            "timestamp": latest_step.get("updated_at") or latest_step.get("timestamp"),
        }
    elif progress_list:
        latest_progress = progress_list[-1]
        current_focus = {
            "title": latest_progress.get("title") or "Latest update",
            "detail": latest_progress.get("detail") or "The orchestrator posted a new update.",
            "status": latest_progress.get("status") or status_value,
            "timestamp": latest_progress.get("timestamp"),
        }
    elif status_value == "running":
        current_focus = {
            "title": "Starting orchestration",
            "detail": "The orchestrator is preparing the run.",
            "status": "running",
            "timestamp": snapshot.get("updated_at"),
        }
    elif status_value == "error":
        current_focus = {
            "title": "Run failed",
            "detail": _short_text(snapshot.get("error")) or "The orchestrator hit an error.",
            "status": "error",
            "timestamp": snapshot.get("updated_at"),
        }
    elif status_value == "completed":
        current_focus = {
            "title": "Run completed",
            "detail": "The orchestrator finished the request.",
            "status": "completed",
            "timestamp": snapshot.get("updated_at"),
        }
    else:
        current_focus = None

    snapshot["agent_statuses"] = agent_status_list
    snapshot["progress_items"] = progress_list
    snapshot["action_steps"] = action_step_list
    snapshot["current_focus"] = current_focus
    snapshot["initial_screenshot"] = initial_screenshot
    snapshot["goal_verification"] = goal_verification
    return snapshot


@dataclass
class ChatMessage:
    role: str
    content: str
    message_id: str = field(default_factory=lambda: generate_id("msg"))
    created_at: datetime = field(default_factory=utc_now)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at.isoformat(),
            "metadata": dict(self.metadata),
        }


@dataclass
class ChatSession:
    session_id: str
    title: str
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    messages: List[ChatMessage] = field(default_factory=list)
    memory_state: Dict[str, Any] = field(default_factory=dict)
    desktop_controller: Any = None
    active_gui_wrapper: Any = None
    pending_native: Optional[Dict[str, Any]] = None
    debug_state: Dict[str, Any] = field(default_factory=_empty_debug_state)
    _lock: threading.RLock = field(
        default_factory=threading.RLock,
        init=False,
        repr=False,
        compare=False,
    )

    def append(self, message: ChatMessage) -> ChatMessage:
        with self._lock:
            self.messages.append(message)
            self.updated_at = message.created_at
            if len(self.messages) == 1 and message.role == "user":
                self.title = _derive_title(message.content)
        return message

    def set_memory_state(self, memory_state: Mapping[str, Any]) -> None:
        with self._lock:
            self.memory_state = copy.deepcopy(dict(memory_state))

    def start_run(self, *, task_id: str, goal: str) -> None:
        timestamp = utc_now()
        with self._lock:
            self.debug_state = _derive_debug_projection({
                **_empty_debug_state(),
                "status": "running",
                "task_id": task_id,
                "goal": goal,
                "started_at": timestamp.isoformat(),
                "updated_at": timestamp.isoformat(),
            })
            self.updated_at = timestamp

    def get_logging_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "session_id": self.session_id,
                "title": self.title,
                "updated_at": self.updated_at.isoformat(),
                "debug_state": copy.deepcopy(self.debug_state),
            }

    def record_event(self, event: AgentEvent) -> None:
        serialized = _serialize_debug_event(event)
        screenshot = _extract_debug_screenshot(event)
        with self._lock:
            debug_state = dict(self.debug_state or _empty_debug_state())
            events = list(debug_state.get("events", []))
            events.append(serialized)
            debug_state["events"] = events[-DEBUG_EVENT_LIMIT:]
            debug_state["latest_event"] = serialized
            debug_state["updated_at"] = serialized["timestamp"]
            if screenshot is not None:
                screenshots = list(debug_state.get("screenshots", []))
                screenshots.append(screenshot)
                debug_state["screenshots"] = screenshots[-DEBUG_SCREENSHOT_LIMIT:]
                debug_state["latest_screenshot"] = screenshot
            self.debug_state = _derive_debug_projection(debug_state)
            self.updated_at = event.timestamp

    def record_result_events(self, events: List[AgentEvent]) -> None:
        existing = {
            (
                item.get("name"),
                item.get("timestamp"),
                item.get("message"),
            )
            for item in self.debug_state.get("events", [])
            if isinstance(item, Mapping)
        }
        for event in events:
            key = (event.name, event.timestamp.isoformat(), event.message)
            if key in existing:
                continue
            self.record_event(event)
            existing.add(key)

    def finish_run(self, *, success: bool, error: Optional[str] = None) -> None:
        timestamp = utc_now()
        with self._lock:
            debug_state = dict(self.debug_state or _empty_debug_state())
            debug_state["status"] = "completed" if success else "error"
            debug_state["error"] = str(error).strip() if error else None
            debug_state["finished_at"] = timestamp.isoformat()
            debug_state["updated_at"] = timestamp.isoformat()
            self.debug_state = _derive_debug_projection(debug_state)
            self.updated_at = timestamp

    def to_summary(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "session_id": self.session_id,
                "title": self.title,
                "created_at": self.created_at.isoformat(),
                "updated_at": self.updated_at.isoformat(),
                "message_count": len(self.messages),
                "debug_status": self.debug_state.get("status", "idle"),
            }

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "session_id": self.session_id,
                "title": self.title,
                "created_at": self.created_at.isoformat(),
                "updated_at": self.updated_at.isoformat(),
                "message_count": len(self.messages),
                "debug_status": self.debug_state.get("status", "idle"),
                "messages": [message.to_dict() for message in self.messages],
                "debug_state": copy.deepcopy(self.debug_state),
            }


class EchoPathChatService:
    def __init__(
        self,
        *,
        config_path: Path,
        download_directory: Optional[Path] = None,
        log_directory: Optional[Path] = None,
        workspace_directory: Optional[Path] = None,
        vm_endpoint: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        self.vm_endpoint = vm_endpoint
        self.run_id = run_id
        # Anchor the workspace to the config file, not the process working
        # directory. An MCP host launches the server from wherever it happens to
        # live -- Claude Desktop uses its own install folder -- so a cwd default
        # points every skill at the host's directory instead of the project.
        self.workspace_directory = Path(
            workspace_directory or self.config_path.parent
        ).expanduser().resolve()
        self.download_directory = Path(
            download_directory or (self.config_path.parent / "downloads")
        ).resolve()
        self.log_directory = Path(log_directory or (self.config_path.parent / "log")).resolve()
        self.replay_store = InteractionReplayStore(root_directory=self.log_directory)
        self.replay_store.backfill_from_logs()
        self.gui_run_directory = self.config_path.parent / "local_data" / "gui_runs"
        self.memory_repository = MemoryRepository(
            root_directory=self.config_path.parent / "local_data" / "memory_repo"
        )
        self.memory_replay_store = MemoryBackedReplayStore(
            memory_repository=self.memory_repository,
            replay_reasoner_model="default",
        )
        self.composite_replay_store = CompositeReplayStore(
            self.memory_replay_store,
            self.replay_store,
        )
        self._sessions: Dict[str, ChatSession] = {}
        self._execution_lock = threading.Lock()

    def list_sessions(self) -> List[Dict[str, Any]]:
        sessions = sorted(
            self._sessions.values(),
            key=lambda session: session.updated_at,
            reverse=True,
        )
        return [session.to_summary() for session in sessions]

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        return session.to_dict()

    def create_session(self, *, title: str = "New chat") -> Dict[str, Any]:
        session = ChatSession(session_id=generate_id("session"), title=title)
        self._sessions[session.session_id] = session
        return session.to_dict()

    def clear_history(self) -> Dict[str, Any]:
        sessions = list(self._sessions.values())
        cleared_session_count = len(sessions)
        for session in sessions:
            self._dispose_session_resources(session)
        self._sessions.clear()

        config = load_global_model_router_config(self.config_path)
        screenshot_roots = {
            config.desktop.screenshot_directory.resolve(),
            (self.config_path.parent / "local_data" / "desktop_observations").resolve(),
        }

        deleted_log_directories = self._remove_history_artifacts(
            self.log_directory,
            prefixes=("session_",),
            delete_files=False,
        )
        deleted_screenshot_directories: List[str] = []
        for root in sorted(screenshot_roots, key=str):
            deleted_screenshot_directories.extend(
                self._remove_history_artifacts(
                    root,
                    prefixes=("session_", "task_"),
                    delete_files=True,
                )
            )
        deleted_memory_action_files = self._remove_history_artifacts(
            self.replay_store.trajectory_directory,
            prefixes=("trajectory-",),
            delete_files=True,
        )

        return {
            "cleared": True,
            "cleared_session_count": cleared_session_count,
            "deleted_log_directory_count": len(deleted_log_directories),
            "deleted_screenshot_directory_count": len(deleted_screenshot_directories),
            "deleted_memory_action_count": len(deleted_memory_action_files),
            "deleted_log_directories": deleted_log_directories,
            "deleted_screenshot_directories": deleted_screenshot_directories,
            "deleted_memory_action_files": deleted_memory_action_files,
        }

    def send_message(
        self, *, message: str, session_id: Optional[str] = None,
        enabled_skills: Optional[Sequence[str]] = None,
        execution_constraints: Optional[Mapping[str, Any]] = None,
        event_callback: Optional[Callable[[AgentEvent], None]] = None,
    ) -> Dict[str, Any]:
        from .run_state import RunStopped
        if not self._execution_lock.acquire(blocking=False):
            raise RuntimeError("An EchoPath execution is already running")
        try:
            if any(s.pending_native for s in self._sessions.values()):
                raise RuntimeError("Resume or cancel the pending native segment before starting another task")
            return self._controlled_send_message(
                message=message, session_id=session_id, enabled_skills=enabled_skills,
                execution_constraints=execution_constraints, event_callback=event_callback,
            )
        except RunStopped:
            return {'result': {'success': False, 'error': 'Stopped by you.', 'output': None}}
        finally:
            self._execution_lock.release()

    def _controlled_send_message(
        self, *, message: str, **kwargs,
    ) -> Dict[str, Any]:
        from .run_state import RunControl, RunRegistry
        config = self._runtime_config()
        if is_remote_desktop_provider(config.desktop.provider):
            # A VM task must not fall through to host shell/filesystem skills.
            kwargs['enabled_skills'] = [name for name in (kwargs.get('enabled_skills') if kwargs.get('enabled_skills') is not None else config.skills.enabled) if name == 'pyautogui-desktop']
            constraints = dict(kwargs.get('execution_constraints') or {})
            constraints['desktop_target'] = 'Remote VM only. All screenshots and actions refer to the guest desktop, never the host.'
            kwargs['execution_constraints'] = constraints
        target = config.desktop.amazon_vm_endpoint if is_remote_desktop_provider(config.desktop.provider) else 'local'
        from .run_recording import record_vm_run
        with RunControl(RunRegistry(self.config_path), message, target, run_id=self.run_id) as control:
            with record_vm_run(config, self.config_path, control.id):
                response = self._send_message(message=message, **kwargs)
            self._finish_controlled_response(response, control)
            return response

    def _finish_controlled_response(self, response, control):
        if response.get('status') == 'awaiting_native':
            token = response['native_handoff']['continuation_token']
            for session in self._sessions.values():
                if session.pending_native and session.pending_native['token'] == token:
                    session.pending_native['run_id'] = control.id
            control.finish('awaiting_native', 'Waiting for the native continuation.')
            return
        result = response.get('result', {})
        output = result.get('output') or {}
        execution = output.get('execution') or {}
        success = bool(result.get('success')) and execution.get('success', True)
        control.finish('completed' if success else 'error', str(execution.get('error') or result.get('error') or output.get('assistant_reply') or 'Task completed.'))

    def resume_native(self, *, session_id: str, continuation_token: str,
                      outputs: Mapping[str, str], cancel: bool = False) -> Dict[str, Any]:
        if not self._execution_lock.acquire(blocking=False):
            raise RuntimeError("An EchoPath execution is already running")
        try:
            session = self._resolve_session(session_id)
            pending = session.pending_native
            if not pending or not secrets.compare_digest(pending["token"], continuation_token):
                raise ValueError("Unknown, expired or already consumed continuation token")
            if not cancel:
                required = pending["handoff"]["required_outputs"]
                if (not isinstance(outputs, Mapping) or set(outputs) != set(required)
                        or any(not isinstance(v, str) or not v.strip() or len(v) > 16000 for v in outputs.values())):
                    raise ValueError("Outputs must match the pending segment's declared nonempty string values")
            def resume():
                # Consume only after obtaining the desktop; a busy VM can be retried.
                session.pending_native = None
                if cancel:
                    session.finish_run(success=False, error="Native continuation cancelled")
                    return {"status": "cancelled", "session_id": session_id}
                continuation = copy.deepcopy(pending["decision"])
                continuation["assistant_reply"] = "Continued the stored workflow after native execution."
                continuation["task"]["payload"].pop("observation_task_id", None)
                continuation["task"]["payload"]["native_segment_result"] = {
                    "segment_id": pending["handoff"]["segment_id"], "outputs": dict(outputs),
                }
                return self._send_message(message=pending["request"], session_id=session_id,
                                          enabled_skills=pending["enabled_skills"],
                                          execution_constraints=pending["constraints"], continuation=continuation)
            if not pending.get('run_id'):
                return resume()
            from .run_state import RunControl, RunRegistry
            from .run_recording import record_vm_run
            config = self._runtime_config()
            target = config.desktop.amazon_vm_endpoint if is_remote_desktop_provider(config.desktop.provider) else 'local'
            with RunControl(RunRegistry(self.config_path), pending['request'], target, run_id=pending['run_id']) as control:
                with record_vm_run(config, self.config_path, control.id):
                    response = resume()
                if cancel:
                    control.finish('stopped', 'Native continuation cancelled.')
                else:
                    self._finish_controlled_response(response, control)
                return response
        finally:
            self._execution_lock.release()

    def _send_message(
        self,
        *,
        message: str,
        session_id: Optional[str] = None,
        enabled_skills: Optional[Sequence[str]] = None,
        execution_constraints: Optional[Mapping[str, Any]] = None,
        event_callback: Optional[Callable[[AgentEvent], None]] = None,
        continuation: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        normalized_message = str(message or "").strip()
        if not normalized_message:
            raise ValueError("Message must not be empty.")

        session = self._resolve_session(session_id)
        if continuation is None:
            session.append(ChatMessage(role="user", content=normalized_message))

        agent = self._build_orchestrator_agent(
            session,
            enabled_skills=enabled_skills,
            task_goal=normalized_message,
        )
        task = _build_chat_task(
            router_name=agent.router_name,
            latest_message=normalized_message,
            session=session,
            execution_constraints=execution_constraints,
        )
        if continuation is not None:
            task.payload["native_continuation"] = copy.deepcopy(dict(continuation))
        session.start_run(task_id=task.task_id, goal=normalized_message)
        register_event_handler = getattr(agent, "on", None)
        if callable(register_event_handler):
            register_event_handler(
                "*",
                lambda event,
                current_session=session,
                current_task=task,
                current_user_query=normalized_message: self._handle_agent_event(
                    current_session,
                    current_task,
                    current_user_query,
                    event,
                    event_callback=event_callback,
                ),
            )
        wrapper = session.active_gui_wrapper
        overlay = None
        if (wrapper is not None and sys.platform == "win32"
                and isinstance(wrapper.controller, PyAutoGUIController)):
            overlay = DesktopOverlay()
            wrapper.overlay = overlay
        try:
            if overlay is not None:
                overlay.start()
            result = agent.run_sync(task=task)
            if overlay is not None:
                overlay.check()
        except BaseException as error:
            self._finalize_gui_wrapper(session, status="failed", error=str(error))
            session.finish_run(success=False, error=str(error))
            raise
        finally:
            if overlay is not None:
                overlay.close()
        session.record_result_events(list(result.events))

        if not result.success:
            self._finalize_gui_wrapper(session, status="failed", error=result.error)
            session.finish_run(success=False, error=result.error)
            assistant_content = (
                f"I ran into an error while processing your request: {result.error}"
            )
            assistant_message = ChatMessage(
                role="assistant",
                content=assistant_content,
                metadata={"error": result.error or "unknown"},
            )
            session.append(assistant_message)
            self._log_interaction(
                session=session,
                task=task,
                result=result,
                user_query=normalized_message,
                assistant_reply=assistant_content,
            )
            return {
                "session": session.to_dict(),
                "result": {
                    "success": False,
                    "error": result.error,
                    "output": None,
                },
            }

        output = result.output or {}
        memory_state = result.state.get("memory")
        if isinstance(memory_state, dict):
            session.set_memory_state(memory_state)
        execution = output.get("execution")
        execution_output = execution.get("output") if isinstance(execution, Mapping) else None
        segmented = bool((output.get("task") or {}).get("payload", {}).get("memory_segments"))
        if segmented and session.active_gui_wrapper is not None:
            writer = session.active_gui_wrapper.writer
            writer.metadata = {**writer.metadata, "segmented_replay": True}
        if isinstance(execution, Mapping) and execution.get("status") == "awaiting_native":
            checkpoint_payload = execution_output.pop("_checkpoint_payload")
            token = secrets.token_urlsafe(32)
            handoff = execution_output["native_handoff"]
            decision = {"assistant_reply": output["assistant_reply"],
                        "task": {**output["task"], "payload": checkpoint_payload},
                        "replay": output.get("replay", {})}
            session.pending_native = {"token": token, "decision": decision,
                                      "handoff": copy.deepcopy(handoff), "request": normalized_message,
                                      "enabled_skills": enabled_skills,
                                      "constraints": dict(execution_constraints or {})}
            handoff["continuation_token"] = token
            handoff["session_id"] = session.session_id
            self._finalize_gui_wrapper(session, status="awaiting_native")
            with session._lock:
                session.debug_state["status"] = "awaiting_native"
            session.append(ChatMessage(role="assistant", content=output["assistant_reply"],
                                       metadata={"status": "awaiting_native", "segment_id": handoff["segment_id"]}))
            return {"status": "awaiting_native", "native_handoff": handoff,
                    "session": session.to_dict(),
                    "result": {"success": True, "status": "awaiting_native", "error": None, "output": output}}
        execution_success = bool(execution.get("success")) if isinstance(execution, Mapping) else True
        execution_error = (
            str(execution.get("error") or "").strip() or None
            if isinstance(execution, Mapping)
            else None
        )
        if execution_success and session.active_gui_wrapper is not None:
            verification = _extract_goal_verification(output, session)
            if verification:
                writer = session.active_gui_wrapper.writer
                writer.metadata = {**writer.metadata, "goal_verification": dict(verification)}
        run_directory = self._finalize_gui_wrapper(
            session,
            status="completed" if execution_success else "failed",
            error=execution_error,
        )
        session.finish_run(success=execution_success, error=execution_error)
        if segmented:
            output["learning"] = {"status": "skipped", "reason": "Preserving segment contracts; native actions are external"}
        if execution_success and run_directory is not None and not segmented:
            learning = self._consolidate_gui_run(
                session=session,
                run_directory=run_directory,
                output=output,
                user_query=normalized_message,
            )
            if learning is not None:
                output["learning"] = learning
        assistant_content = str(output.get("assistant_reply", "")).strip() or "Done."
        assistant_message = ChatMessage(
            role="assistant",
            content=assistant_content,
            metadata={
                "task": output.get("task", {}),
                "execution": output.get("execution"),
            },
        )
        session.append(assistant_message)
        self._log_interaction(
            session=session,
            task=task,
            result=result,
            user_query=normalized_message,
            assistant_reply=assistant_content,
        )

        return {
            "session": session.to_dict(),
            "result": {
                "success": True,
                "error": None,
                "output": output,
            },
        }

    def _resolve_session(self, session_id: Optional[str]) -> ChatSession:
        if session_id:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(f"Chat session '{session_id}' was not found.")
            return session

        session = ChatSession(session_id=generate_id("session"), title="New chat")
        self._sessions[session.session_id] = session
        return session

    def _runtime_config(self):
        config = load_global_model_router_config(self.config_path)
        if self.vm_endpoint:
            from urllib.parse import urlsplit
            url = urlsplit(self.vm_endpoint)
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError('VM endpoint must be an HTTP(S) URL without credentials, query or fragment.')
            config = replace(config, desktop=replace(config.desktop, provider='local-vm', amazon_vm_endpoint=self.vm_endpoint.rstrip('/')))
        elif self.vm_endpoint == '':
            config = replace(config, desktop=replace(config.desktop, provider='local', amazon_vm_endpoint=''))
        return config

    def _build_orchestrator_agent(
        self,
        session: ChatSession,
        *,
        enabled_skills: Optional[Sequence[str]] = None,
        task_goal: str = "",
    ) -> OrchestratorAgent:
        config = self._runtime_config()
        router = build_model_router_from_global_config(config)
        resolved_enabled_skills = _resolve_enabled_skills(
            configured_enabled_skills=config.skills.enabled,
            requested_enabled_skills=enabled_skills,
        )
        agent = OrchestratorAgent(
            name="echopath-orchestrator",
            description="Primary orchestrator agent for the EchoPath product chat surface.",
            working_directory=self.workspace_directory,
            router_name=router.name,
            equipped_skills=resolved_enabled_skills,
            desktop_reasoning_effort=config.desktop.reasoning_effort,
            desktop_max_iterations=config.desktop.max_iterations,
        )
        agent.register_model_router(router, replace=True)
        # Learned memories rebind flexible inputs through the same bridge.
        self.memory_replay_store.replay_reasoner = router
        self.memory_replay_store.replay_fallback_to_planning = (
            config.desktop.replay_fallback_to_planning
        )
        self.memory_replay_store.verify_goal = config.desktop.replay_verify_goal
        self.memory_replay_store.decision_model = (
            {
                "enabled": True,
                "api_key_env": config.desktop.replay_decision_model_api_key_env,
                "credentials_path": str(config.source_path.parent / "credentials" / "api_keys.yaml"),
                "min_confidence": config.desktop.replay_decision_model_min_confidence,
            }
            if config.desktop.replay_decision_model
            else None
        )
        agent.register_service(
            "interaction-replay-store", self.composite_replay_store, replace=True
        )
        if "command-line" in resolved_enabled_skills:
            agent.register_skill(build_command_line_skill(), replace=True)
        if "pyautogui-desktop" in resolved_enabled_skills:
            agent.register_skill(build_pyautogui_desktop_skill(), replace=True)
        if "workspace-files" in resolved_enabled_skills:
            agent.register_skill(build_workspace_files_skill(), replace=True)
        if "pyautogui-desktop" in resolved_enabled_skills:
            if session.desktop_controller is None:
                session.desktop_controller = self._build_desktop_controller(config, session)
            # Register a fresh per-message recording wrapper instead of the bare
            # controller: the desktop skill routes actions through wrapper.act,
            # which records steps.jsonl plus before/after screenshots -- the raw
            # material consolidation turns into replayable memories.
            session.active_gui_wrapper = build_gui_run_wrapper(
                session.desktop_controller,
                root_directory=self.gui_run_directory,
                task_goal=task_goal,
                source_agent="echopath-mcp",
                metadata={
                    "session_id": session.session_id,
                    "platform": sys.platform,
                },
            )
            agent.register_service("desktop", session.active_gui_wrapper, replace=True)
        return agent

    def _log_interaction(
        self,
        *,
        session: ChatSession,
        task,
        result,
        user_query: str,
        assistant_reply: str,
    ) -> None:
        logger = InteractionLogger(
            root_directory=self.log_directory,
            session_id=session.session_id,
            replay_store=self.replay_store,
        )
        logger.log_interaction(
            task=task,
            result=result,
            user_query=user_query,
            assistant_reply=assistant_reply,
            session_title=session.title,
        )

    def _finalize_gui_wrapper(
        self,
        session: ChatSession,
        *,
        status: str,
        error: Optional[str] = None,
    ) -> Optional[Path]:
        """Close this message's recording wrapper and return its run directory.

        Runs that performed no GUI actions leave nothing worth keeping, so
        their empty directories are deleted. Never raises: recording hygiene
        must not affect the chat result.
        """
        wrapper = session.active_gui_wrapper
        session.active_gui_wrapper = None
        if wrapper is None:
            return None
        try:
            run_directory = Path(wrapper.writer.run_directory)
            step_count = int(wrapper.writer.step_count)
            wrapper.finish(status=status, error=error)
            if step_count <= 0:
                shutil.rmtree(run_directory, ignore_errors=True)
                return None
            return run_directory if status == "completed" else None
        except Exception:
            return None

    def _consolidate_gui_run(
        self,
        *,
        session: ChatSession,
        run_directory: Path,
        output: Mapping[str, Any],
        user_query: str,
    ) -> Optional[Dict[str, Any]]:
        """Best-effort learning: consolidate a successful GUI run into memory.

        The whole body is guarded -- learning must never fail the run that
        produced it. Returns a compact summary for the response, or None when
        learning is disabled or not applicable.
        """
        if not _auto_consolidate_enabled():
            return {"status": "skipped", "reason": "ECHOPATH_AUTO_CONSOLIDATE disabled"}
        try:
            goal_verification = _extract_goal_verification(output, session)
            self._record_learning_event(
                session,
                "chat.memory.consolidation.started",
                "Consolidating the completed GUI run into memory.",
                {"run_directory": str(run_directory)},
            )
            config = load_global_model_router_config(self.config_path)
            router = build_model_router_from_global_config(config)
            summary = _run_sync_awaitable(
                consolidate_completed_run(
                    router,
                    run_directory,
                    task_intent=user_query,
                    memory_repository=self.memory_repository,
                    goal_verification=goal_verification,
                    metadata={"session_id": session.session_id},
                    platform=sys.platform,
                )
            )
            if summary.get("status") == "error":
                self._record_learning_event(
                    session,
                    "chat.memory.consolidation.failed",
                    "Memory consolidation failed.",
                    {"error": summary.get("error")},
                )
            else:
                self._record_learning_event(
                    session,
                    "chat.memory.consolidation.completed",
                    "Memory consolidation finished.",
                    {
                        "path_id": summary.get("path_id"),
                        "status": summary.get("status"),
                    },
                )
                if summary.get("promoted"):
                    self._record_learning_event(
                        session,
                        "chat.memory.promotion.completed",
                        "Consolidated memory was promoted for replay.",
                        {"path_id": summary.get("path_id")},
                    )
                elif summary.get("gate_reasons"):
                    self._record_learning_event(
                        session,
                        "chat.memory.promotion.rejected",
                        "Consolidated memory stayed a candidate.",
                        {"gate_reasons": summary.get("gate_reasons")},
                    )
            try:
                prune_gui_runs(self.gui_run_directory)
            except Exception:
                pass
            return summary
        except Exception as error:  # noqa: BLE001 - learning is best-effort
            try:
                self._record_learning_event(
                    session,
                    "chat.memory.consolidation.failed",
                    "Memory consolidation failed.",
                    {"error": str(error)},
                )
            except Exception:
                pass
            return {"status": "error", "error": str(error), "promoted": False}

    def _record_learning_event(
        self,
        session: ChatSession,
        name: str,
        message: str,
        payload: Mapping[str, Any],
    ) -> None:
        session.record_event(
            AgentEvent(name=name, message=message, payload=dict(payload))
        )

    def _handle_agent_event(
        self,
        session: ChatSession,
        task,
        user_query: str,
        event: AgentEvent,
        event_callback: Optional[Callable[[AgentEvent], None]] = None,
    ) -> None:
        session.record_event(event)
        from .run_state import current_control
        control = current_control.get()
        if control:
            control.update(event.message, event, steps=session.debug_state.get("action_steps"))
        if event_callback is not None:
            try:
                event_callback(event)
            except Exception:
                # Progress observers must never interrupt the task they observe.
                pass
        try:
            snapshot = session.get_logging_snapshot()
            logger = InteractionLogger(
                root_directory=self.log_directory,
                session_id=session.session_id,
                replay_store=self.replay_store,
            )
            logger.log_partial_interaction(
                task=task,
                user_query=user_query,
                session_title=str(snapshot.get("title", session.title)),
                debug_state=dict(snapshot.get("debug_state", {})),
                latest_event=event,
            )
        except Exception:
            pass

    def _build_desktop_controller(self, config, session: ChatSession):
        screenshot_directory = config.desktop.screenshot_directory / session.session_id
        if is_remote_desktop_provider(config.desktop.provider):
            return AmazonVMDesktopController(
                endpoint=config.desktop.amazon_vm_endpoint,
                api_key_env=config.desktop.amazon_vm_api_key_env,
                credential_file=self.config_path.parent / "credentials" / "amazon_vm.json",
                timeout_seconds=config.desktop.amazon_vm_timeout_seconds,
                screenshot_directory=screenshot_directory,
                provider_name=config.desktop.provider,
            )
        return PyAutoGUIController(
            pause_seconds=config.desktop.pause_seconds,
            fail_safe=config.desktop.fail_safe,
            screenshot_directory=screenshot_directory,
        )

    def _dispose_session_resources(self, session: ChatSession) -> None:
        session.desktop_controller = None

    def _remove_history_artifacts(
        self,
        root: Path,
        *,
        prefixes: tuple[str, ...],
        delete_files: bool,
    ) -> List[str]:
        directory = Path(root).resolve()
        if not directory.exists() or not directory.is_dir():
            return []

        deleted: List[str] = []
        for candidate in sorted(directory.iterdir()):
            if not any(candidate.name.startswith(prefix) for prefix in prefixes):
                continue
            resolved = candidate.resolve()
            try:
                if candidate.is_dir():
                    shutil.rmtree(candidate)
                elif delete_files and candidate.exists():
                    candidate.unlink()
                else:
                    continue
            except FileNotFoundError:
                continue
            deleted.append(str(resolved))
        return deleted


def _build_chat_task(
    *,
    router_name: str,
    latest_message: str,
    session: ChatSession,
    execution_constraints: Optional[Mapping[str, Any]] = None,
):
    from .models import AgentTask

    return AgentTask(
        goal="Respond to the user in the product chat.",
        payload={
            "router_name": router_name,
            "message": latest_message,
            "messages": [message.to_dict() for message in session.messages],
            "memory": copy.deepcopy(session.memory_state),
            "execution_constraints": (
                dict(execution_constraints)
                if isinstance(execution_constraints, Mapping)
                else {}
            ),
        },
        metadata={"session_id": session.session_id},
    )


def _resolve_enabled_skills(
    *,
    configured_enabled_skills: Sequence[str],
    requested_enabled_skills: Optional[Sequence[str]],
) -> List[str]:
    configured = list(dict.fromkeys(str(name) for name in configured_enabled_skills))
    if requested_enabled_skills is None:
        return configured

    configured_set = set(configured)
    requested = list(dict.fromkeys(str(name) for name in requested_enabled_skills))
    unknown = [name for name in requested if name not in configured_set]
    if unknown:
        raise ValueError(
            "Requested enabled skills must be a subset of the configured enabled skills. "
            f"Unexpected: {', '.join(unknown)}."
        )
    return requested


def _derive_title(message: str) -> str:
    normalized = " ".join(str(message or "").strip().split())
    if not normalized:
        return "New chat"
    return normalized[:60] + ("…" if len(normalized) > 60 else "")


def _auto_consolidate_enabled() -> bool:
    raw = os.getenv("ECHOPATH_AUTO_CONSOLIDATE", "").strip().lower()
    if not raw:
        return True
    return raw in {"1", "true", "yes", "on"}


def _run_sync_awaitable(awaitable: Any) -> Any:
    """Run a coroutine from the sync chat path (no event loop is active here)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    raise RuntimeError(
        "EchoPath chat consolidation must not run inside an active event loop."
    )


def _extract_goal_verification(
    output: Mapping[str, Any],
    session: "ChatSession",
) -> Optional[Dict[str, Any]]:
    """Find the run's goal verification wherever this execution mode put it.

    Scripted replays report it at execution.output.goal_verification; planned
    runs attach it to the completing iteration; the session debug snapshot
    mirrors whichever one fired.
    """
    execution = output.get("execution")
    execution_output = (
        execution.get("output") if isinstance(execution, Mapping) else None
    )
    if isinstance(execution_output, Mapping):
        verification = execution_output.get("goal_verification")
        if isinstance(verification, Mapping):
            return dict(verification)
        iterations = execution_output.get("iterations")
        if isinstance(iterations, list):
            for iteration in reversed(iterations):
                if isinstance(iteration, Mapping) and isinstance(
                    iteration.get("goal_verification"), Mapping
                ):
                    return dict(iteration["goal_verification"])
    snapshot = session.debug_state.get("goal_verification")
    if isinstance(snapshot, Mapping):
        return dict(snapshot)
    return None
