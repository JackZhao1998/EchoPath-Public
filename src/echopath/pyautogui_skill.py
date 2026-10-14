from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Union

from .runtime import AgentContext
from .skill import MarkdownSkill


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SKILL_DIRECTORY = ROOT / "skills" / "pyautogui_desktop"
LEGACY_SKILL_DIRECTORY = ROOT / "skills" / "pyautogui"
WRAPPER_RECORDED_ACTIONS = {
    "activate_app",
    "click",
    "move",
    "type",
    "paste_text",
    "press",
    "hotkey",
    "scroll",
    "wait",
}


async def pyautogui_desktop_executor(
    payload: Mapping[str, Any],
    context: AgentContext,
) -> Dict[str, Any]:
    service_name = str(payload.get("service_name", "desktop"))
    controller = context.get_service(service_name)
    if controller is None:
        raise KeyError(f"Service '{service_name}' is not registered.")

    actions = list(_coerce_actions(payload))
    results = []
    values: Dict[str, Any] = {}

    for index, action in enumerate(actions, start=1):
        from .run_state import checkpoint
        await checkpoint()
        action_payload = _normalize_action(index, action)
        action_name = action_payload["action"]
        wrapper_act = getattr(controller, "act", None)
        if callable(wrapper_act) and action_name in WRAPPER_RECORDED_ACTIONS:
            wrapper_result = wrapper_act(
                action_payload,
                source=_action_source(action_payload, payload),
            )
            if inspect.isawaitable(wrapper_result):
                wrapper_result = await wrapper_result
            results.append(_result_from_wrapper_action(index, action_payload, wrapper_result))
            continue

        if action_name == "observe":
            observation = await controller.observe(
                path=action_payload.get("path"),
                region=action_payload.get("region"),
            )
            result_key = action_payload["result_key"]
            values[result_key] = observation
            results.append(
                {
                    "index": index,
                    "action": "observe",
                    "result_key": result_key,
                    "observation": observation,
                    "status": "ok",
                }
            )
            continue

        if action_name == "move":
            move_result = await controller.move_to(
                action_payload["x"],
                action_payload["y"],
                duration=action_payload["duration"],
            )
            results.append(
                {
                    "index": index,
                    "action": "move",
                    **move_result,
                    "status": "ok",
                }
            )
            continue

        if action_name == "click":
            click_result = await controller.click(
                x=action_payload.get("x"),
                y=action_payload.get("y"),
                button=action_payload["button"],
                clicks=action_payload["clicks"],
                interval=action_payload["interval"],
            )
            results.append(
                {
                    "index": index,
                    "action": "click",
                    **click_result,
                    "status": "ok",
                }
            )
            continue

        if action_name == "type":
            type_result = await controller.type_text(
                action_payload["text"],
                interval=action_payload["interval"],
            )
            results.append(
                {
                    "index": index,
                    "action": "type",
                    **type_result,
                    "status": "ok",
                }
            )
            continue

        if action_name == "paste_text":
            paste_result = await controller.paste_text(action_payload["text"])
            results.append(
                {
                    "index": index,
                    "action": "paste_text",
                    **paste_result,
                    "status": "ok",
                }
            )
            continue

        if action_name == "press":
            press_result = await controller.press(
                action_payload["key"],
                presses=action_payload["presses"],
                interval=action_payload["interval"],
            )
            results.append(
                {
                    "index": index,
                    "action": "press",
                    **press_result,
                    "status": "ok",
                }
            )
            continue

        if action_name == "hotkey":
            hotkey_result = await controller.hotkey(
                action_payload["keys"],
                interval=action_payload["interval"],
            )
            results.append(
                {
                    "index": index,
                    "action": "hotkey",
                    **hotkey_result,
                    "status": "ok",
                }
            )
            continue

        if action_name == "menu_click":
            menu_result = await controller.menu_click(
                process=action_payload["process"],
                menu=action_payload["menu"],
                item=action_payload["item"],
            )
            results.append(
                {
                    "index": index,
                    "action": "menu_click",
                    **menu_result,
                    "status": "ok",
                }
            )
            continue

        if action_name == "activate_app":
            activate_result = await controller.activate_app(action_payload["app_name"])
            results.append(
                {
                    "index": index,
                    "action": "activate_app",
                    **activate_result,
                    "status": "ok",
                }
            )
            continue

        if action_name == "scroll":
            scroll_result = await controller.scroll(
                action_payload["clicks"],
                x=action_payload.get("x"),
                y=action_payload.get("y"),
            )
            results.append(
                {
                    "index": index,
                    "action": "scroll",
                    **scroll_result,
                    "status": "ok",
                }
            )
            continue

        if action_name == "wait":
            seconds = action_payload["seconds"]
            await asyncio.sleep(seconds)
            results.append(
                {
                    "index": index,
                    "action": "wait",
                    "seconds": seconds,
                    "status": "ok",
                }
            )
            continue

        if action_name == "position":
            result_key = action_payload["result_key"]
            position = await controller.position()
            values[result_key] = position
            results.append(
                {
                    "index": index,
                    "action": "position",
                    "result_key": result_key,
                    "position": position,
                    "status": "ok",
                }
            )
            continue

        if action_name == "screen_size":
            result_key = action_payload["result_key"]
            size = await controller.screen_size()
            values[result_key] = size
            results.append(
                {
                    "index": index,
                    "action": "screen_size",
                    "result_key": result_key,
                    "screen_size": size,
                    "status": "ok",
                }
            )
            continue

        raise ValueError(f"Unsupported pyautogui-desktop action '{action_name}'.")

    return {
        "skill": payload.get("selected_skill_name", "pyautogui-desktop"),
        "service": service_name,
        "action_count": len(actions),
        "results": results,
        "values": values,
    }


def build_pyautogui_desktop_skill(
    *,
    skill_directory: Optional[Union[Path, str]] = None,
    name: str = "pyautogui-desktop",
    description: str = "Use pyautogui to observe the desktop and perform GUI actions.",
) -> MarkdownSkill:
    if skill_directory is not None:
        directory = Path(skill_directory)
    elif DEFAULT_SKILL_DIRECTORY.exists():
        directory = DEFAULT_SKILL_DIRECTORY
    else:
        directory = LEGACY_SKILL_DIRECTORY
    return MarkdownSkill.from_directory(
        directory,
        name=name,
        description=description,
        executor=pyautogui_desktop_executor,
    )


def _coerce_actions(payload: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    payload_actions = payload.get("actions")
    if payload_actions is not None:
        if not isinstance(payload_actions, list):
            raise ValueError("pyautogui-desktop skill payload 'actions' must be a list.")
        return payload_actions
    raise ValueError("pyautogui-desktop skill requires an 'actions' list.")


def _normalize_action(index: int, action: Mapping[str, Any]) -> MutableMapping[str, Any]:
    if not isinstance(action, Mapping):
        raise ValueError(f"GUI action #{index} must be a mapping.")

    action_name = str(action.get("action") or action.get("type") or "").strip().lower()
    if not action_name:
        raise ValueError(f"GUI action #{index} is missing an 'action' field.")

    normalized = dict(action)
    normalized["action"] = action_name

    if action_name == "observe":
        normalized["path"] = _optional_string(action.get("path"))
        normalized["region"] = _optional_region(action.get("region"))
        normalized["result_key"] = str(action.get("result_key") or f"observation_{index}")
        return normalized

    if action_name == "move":
        normalized["x"] = _require_int(action, index, "x")
        normalized["y"] = _require_int(action, index, "y")
        normalized["duration"] = _optional_float(action.get("duration"), 0.0)
        return normalized

    if action_name == "click":
        x_value = action.get("x")
        y_value = action.get("y")
        if x_value is None and y_value is None:
            normalized["x"] = None
            normalized["y"] = None
        else:
            normalized["x"] = _require_int(action, index, "x")
            normalized["y"] = _require_int(action, index, "y")
        normalized["button"] = str(action.get("button") or "left")
        normalized["clicks"] = _optional_int(action.get("clicks"), 1)
        normalized["interval"] = _optional_float(action.get("interval"), 0.0)
        return normalized

    if action_name == "type":
        normalized["text"] = _require_string(action, index, "text")
        normalized["interval"] = _optional_float(action.get("interval"), 0.0)
        return normalized

    if action_name == "paste_text":
        normalized["text"] = _require_string(action, index, "text")
        return normalized

    if action_name == "press":
        normalized["key"] = _require_string(action, index, "key")
        normalized["presses"] = _optional_int(action.get("presses"), 1)
        normalized["interval"] = _optional_float(action.get("interval"), 0.0)
        return normalized

    if action_name == "hotkey":
        normalized["keys"] = _require_keys(action, index)
        normalized["interval"] = _optional_float(action.get("interval"), 0.0)
        return normalized

    if action_name == "menu_click":
        normalized["process"] = _require_string(action, index, "process")
        normalized["menu"] = _require_string(action, index, "menu")
        normalized["item"] = _require_string(action, index, "item")
        return normalized

    if action_name == "activate_app":
        normalized["app_name"] = _require_string(action, index, "app_name")
        return normalized

    if action_name == "scroll":
        normalized["clicks"] = _require_int(action, index, "clicks")
        x_value = action.get("x")
        y_value = action.get("y")
        if x_value is None and y_value is None:
            normalized["x"] = None
            normalized["y"] = None
        else:
            normalized["x"] = _require_int(action, index, "x")
            normalized["y"] = _require_int(action, index, "y")
        return normalized

    if action_name == "wait":
        normalized["seconds"] = _require_float(action, index, "seconds")
        return normalized

    if action_name in {"position", "screen_size"}:
        normalized["result_key"] = str(action.get("result_key") or f"{action_name}_{index}")
        return normalized

    return normalized


def _action_source(
    action: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> Dict[str, Any]:
    source = action.get("source")
    result = dict(source) if isinstance(source, Mapping) else {}
    result.setdefault("agent", str(payload.get("source_agent") or "pyautogui-desktop"))
    result.setdefault("skill", str(payload.get("selected_skill_name") or "pyautogui-desktop"))
    return result


def _result_from_wrapper_action(
    index: int,
    action: Mapping[str, Any],
    wrapper_result: Any,
) -> Dict[str, Any]:
    result = dict(wrapper_result) if isinstance(wrapper_result, Mapping) else {"result": wrapper_result}
    executor_result = result.get("result")
    record: Dict[str, Any] = {
        "index": index,
        "action": str(action.get("action") or ""),
        "status": str(result.get("status") or "ok"),
        "step_id": result.get("step_id"),
        "run_id": result.get("run_id"),
        "wrapper_result": result,
    }
    if isinstance(executor_result, Mapping):
        record.update(dict(executor_result))
    return record


def _require_string(action: Mapping[str, Any], index: int, field_name: str) -> str:
    value = action.get(field_name)
    if value is None or not str(value).strip():
        raise ValueError(f"GUI action #{index} requires a non-empty '{field_name}'.")
    return str(value)


def _require_int(action: Mapping[str, Any], index: int, field_name: str) -> int:
    value = action.get(field_name)
    if value is None:
        raise ValueError(f"GUI action #{index} requires '{field_name}'.")
    return int(value)


def _optional_int(value: Any, default: int) -> int:
    if value is None:
        return default
    return int(value)


def _require_float(action: Mapping[str, Any], index: int, field_name: str) -> float:
    value = action.get(field_name)
    if value is None:
        raise ValueError(f"GUI action #{index} requires '{field_name}'.")
    return float(value)


def _optional_float(value: Any, default: float) -> float:
    if value is None:
        return default
    return float(value)


def _optional_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_region(value: Any) -> Optional[List[int]]:
    if value is None:
        return None
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("GUI observe actions expect 'region' to be a four-item sequence.")
    if len(value) != 4:
        raise ValueError("GUI observe actions expect 'region' to contain four items.")
    return [int(item) for item in value]


def _require_keys(action: Mapping[str, Any], index: int) -> List[str]:
    keys = action.get("keys")
    if not isinstance(keys, list) or not keys:
        raise ValueError(f"GUI action #{index} requires a non-empty 'keys' list.")
    return [str(key) for key in keys if str(key).strip()]
