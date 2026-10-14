from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import time
from collections.abc import Sequence as SequenceABC
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple, Union

from .models import generate_id


GUI_WRAPPER_SCHEMA_VERSION = 1
DEFAULT_GUI_RUN_ROOT = Path("local_data/gui_runs")
SUPPORTED_GUI_WRAPPER_ACTIONS = {
    "activate_app",
    "click",
    "move",
    "paste_text",
    "press",
    "hotkey",
    "scroll",
    "type",
    "wait",
}


class GUIWrapperError(RuntimeError):
    pass


@dataclass
class GUIRunCaptureWriter:
    root_directory: Path = DEFAULT_GUI_RUN_ROOT
    run_id: str = field(default_factory=lambda: generate_id("gui_run"))
    source_agent: str = "coding-agent"
    task_goal: str = ""
    controller_name: str = "desktop"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.root_directory = Path(self.root_directory).resolve()
        self.run_directory = self.root_directory / self.run_id
        self.screenshot_directory = self.run_directory / "screenshots"
        self.manifest_path = self.run_directory / "manifest.json"
        self.steps_path = self.run_directory / "steps.jsonl"
        self.observations_path = self.run_directory / "observations.jsonl"
        self.events_path = self.run_directory / "events.jsonl"
        self.run_directory.mkdir(parents=True, exist_ok=True)
        self.screenshot_directory.mkdir(parents=True, exist_ok=True)
        self.step_count = 0
        self.observation_count = 0
        self.event_count = 0
        self.created_at = _utc_iso()
        self.write_manifest(status="running")

    def next_screenshot_path(self, *, step_id: Optional[str], phase: str) -> Path:
        stem = str(phase or "observe").strip().replace("/", "_")
        if step_id:
            filename = f"{step_id}-{stem}.png"
        else:
            filename = f"observation-{self.observation_count + 1:04d}-{stem}.png"
        return self.screenshot_directory / filename

    def append_step(self, record: Mapping[str, Any]) -> None:
        self.step_count += 1
        self._append_jsonl(self.steps_path, record)

    def append_observation(self, record: Mapping[str, Any]) -> None:
        self.observation_count += 1
        self._append_jsonl(self.observations_path, record)

    def append_event(self, record: Mapping[str, Any]) -> None:
        self.event_count += 1
        self._append_jsonl(self.events_path, record)

    def write_manifest(
        self,
        *,
        status: str,
        error: Optional[str] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "record_type": "gui_run_manifest",
            "schema_version": GUI_WRAPPER_SCHEMA_VERSION,
            "run_id": self.run_id,
            "status": str(status),
            "source_agent": str(self.source_agent),
            "task_goal": str(self.task_goal),
            "controller_name": str(self.controller_name),
            "created_at": self.created_at,
            "updated_at": _utc_iso(),
            "root_directory": str(self.root_directory),
            "run_directory": str(self.run_directory),
            "screenshot_directory": str(self.screenshot_directory),
            "steps_path": str(self.steps_path),
            "observations_path": str(self.observations_path),
            "events_path": str(self.events_path),
            "step_count": int(self.step_count),
            "observation_count": int(self.observation_count),
            "event_count": int(self.event_count),
            "metadata": _json_safe(self.metadata),
        }
        if error:
            payload["error"] = str(error)
        if extra:
            payload.update(_json_safe(dict(extra)))
        self._write_json(self.manifest_path, payload)
        return payload

    def finish(
        self,
        *,
        status: str = "completed",
        error: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = {
            "record_type": "gui_run_finished",
            "schema_version": GUI_WRAPPER_SCHEMA_VERSION,
            "run_id": self.run_id,
            "status": str(status),
            "finished_at": _utc_iso(),
            "error": str(error) if error else None,
            "metadata": _json_safe(dict(metadata or {})),
        }
        self.append_event(payload)
        return self.write_manifest(
            status=status,
            error=error,
            extra={"finished_at": payload["finished_at"]},
        )

    def _append_jsonl(self, path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(_json_safe(payload), ensure_ascii=False, sort_keys=True) + "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def _write_json(self, path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(_json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True)
        with path.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


@dataclass
class GUIRunWrapper:
    controller: Any
    writer: GUIRunCaptureWriter
    name: str = "gui-wrapper"
    settle_seconds: float = 0.1
    observation_region: Optional[Sequence[int]] = None
    source_agent: str = "coding-agent"
    overlay: Any = None

    def __post_init__(self) -> None:
        controller_name = str(getattr(self.controller, "name", "") or "desktop")
        if not self.writer.controller_name or self.writer.controller_name == "desktop":
            self.writer.controller_name = controller_name
            self.writer.write_manifest(status="running")

    def __getattr__(self, name: str) -> Any:
        """Delegate unknown attributes to the wrapped controller.

        The wrapper mirrors the recorded action surface explicitly; anything
        else -- `menu_click`, provider-specific helpers, plain attributes --
        passes through so registering the wrapper in place of the controller
        is behaviour-preserving. `__getattr__` is only consulted for names not
        found normally, so recorded actions are never shadowed.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        # During dataclass construction `controller` may not exist yet; using
        # __dict__ avoids recursing back into __getattr__.
        controller = self.__dict__.get("controller")
        if controller is None:
            raise AttributeError(name)
        return getattr(controller, name)

    async def observe(
        self,
        *,
        path: Optional[Union[str, Path]] = None,
        region: Optional[Sequence[int]] = None,
    ) -> Dict[str, Any]:
        if path is None:
            path = self.writer.next_screenshot_path(step_id=None, phase="observe")
        observation = await self._observe_raw(path=path, region=region)
        observation_record = self._build_observation_record(
            observation,
            phase="observe",
            step_id=None,
            source=self._source_payload({"operation": "observe"}),
        )
        self.writer.append_observation(observation_record)
        return {**observation, "capture": observation_record}

    async def act(
        self,
        action: Mapping[str, Any],
        *,
        source: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        action_name, parameters = _normalize_wrapper_action(action)
        result, step_record = await self._record_and_execute_action(
            action_name,
            parameters,
            source=source,
        )
        return {
            "status": step_record["status"],
            "action": dict(step_record["action"]),
            "result": _json_safe(result),
            "step": step_record,
            "run_id": self.writer.run_id,
            "step_id": step_record["step_id"],
        }

    async def click(
        self,
        *,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
        clicks: int = 1,
        interval: float = 0.0,
    ) -> Dict[str, Any]:
        result, _step = await self._record_and_execute_action(
            "click",
            {
                "x": x,
                "y": y,
                "button": str(button),
                "clicks": int(clicks),
                "interval": float(interval),
            },
        )
        return result

    async def move_to(
        self,
        x: int,
        y: int,
        *,
        duration: float = 0.0,
    ) -> Dict[str, Any]:
        result, _step = await self._record_and_execute_action(
            "move",
            {"x": int(x), "y": int(y), "duration": float(duration)},
        )
        return result

    async def type_text(
        self,
        text: str,
        *,
        interval: float = 0.0,
    ) -> Dict[str, Any]:
        result, _step = await self._record_and_execute_action(
            "type",
            {"text": str(text), "interval": float(interval)},
        )
        return result

    async def paste_text(self, text: str) -> Dict[str, Any]:
        result, _step = await self._record_and_execute_action(
            "paste_text",
            {"text": str(text)},
        )
        return result

    async def press(
        self,
        key: str,
        *,
        presses: int = 1,
        interval: float = 0.0,
    ) -> Dict[str, Any]:
        result, _step = await self._record_and_execute_action(
            "press",
            {"key": str(key), "presses": int(presses), "interval": float(interval)},
        )
        return result

    async def hotkey(
        self,
        keys: Sequence[str],
        *,
        interval: float = 0.0,
    ) -> Dict[str, Any]:
        result, _step = await self._record_and_execute_action(
            "hotkey",
            {"keys": [str(key) for key in keys], "interval": float(interval)},
        )
        return result

    async def activate_app(self, app_name: str) -> Dict[str, Any]:
        result, _step = await self._record_and_execute_action(
            "activate_app",
            {"app_name": str(app_name)},
        )
        return result

    async def scroll(
        self,
        clicks: int,
        *,
        x: Optional[int] = None,
        y: Optional[int] = None,
    ) -> Dict[str, Any]:
        result, _step = await self._record_and_execute_action(
            "scroll",
            {"clicks": int(clicks), "x": x, "y": y},
        )
        return result

    async def wait(self, seconds: float) -> Dict[str, Any]:
        result, _step = await self._record_and_execute_action(
            "wait",
            {"seconds": float(seconds)},
        )
        return result

    async def screen_size(self) -> Dict[str, int]:
        return await self._call_controller("screen_size")

    async def position(self) -> Dict[str, int]:
        return await self._call_controller("position")

    def finish(
        self,
        *,
        status: str = "completed",
        error: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self.writer.finish(status=status, error=error, metadata=metadata)

    async def _record_and_execute_action(
        self,
        action_name: str,
        parameters: Mapping[str, Any],
        *,
        source: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if action_name not in SUPPORTED_GUI_WRAPPER_ACTIONS:
            raise GUIWrapperError(f"Unsupported GUI wrapper action '{action_name}'.")

        if self.overlay is not None:
            self.overlay.update(f"Action {self.writer.step_count + 1}: {action_name.replace('_', ' ')}")

        sequence = self.writer.step_count + 1
        step_id = f"step-{sequence:04d}"
        requested_at = _utc_iso()
        before_observation: Optional[Dict[str, Any]] = None
        after_observation: Optional[Dict[str, Any]] = None
        executor_result: Dict[str, Any] = {}
        error_text: Optional[str] = None
        started_at = ""
        finished_at = ""
        duration_seconds = 0.0
        monotonic_started: Optional[float] = None

        try:
            before_observation = await self._capture_step_observation(
                step_id=step_id,
                phase="before",
                source=self._source_payload(source),
            )
            started_at = _utc_iso()
            monotonic_started = time.monotonic()
            executor_result = await self._execute_action(action_name, parameters)
            if self.settle_seconds > 0:
                await asyncio.sleep(float(self.settle_seconds))
            finished_at = _utc_iso()
            duration_seconds = time.monotonic() - monotonic_started
        except Exception as error:
            if not started_at:
                started_at = _utc_iso()
                duration_seconds = 0.0
            else:
                duration_seconds = (
                    max(time.monotonic() - monotonic_started, 0.0)
                    if monotonic_started is not None
                    else max(duration_seconds, 0.0)
                )
            finished_at = _utc_iso()
            error_text = str(error)
        finally:
            try:
                after_observation = await self._capture_step_observation(
                    step_id=step_id,
                    phase="after",
                    source=self._source_payload(source),
                )
            except Exception as observe_error:
                if error_text:
                    error_text = f"{error_text}; after-observation failed: {observe_error}"
                else:
                    error_text = f"after-observation failed: {observe_error}"

        step_record = {
            "record_type": "gui_action_step",
            "schema_version": GUI_WRAPPER_SCHEMA_VERSION,
            "run_id": self.writer.run_id,
            "step_id": step_id,
            "sequence": sequence,
            "source": _json_safe(self._source_payload(source)),
            "requested_at": requested_at,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": float(duration_seconds),
            "status": "error" if error_text else "ok",
            "action": {
                "name": action_name,
                "parameters": _json_safe(dict(parameters)),
            },
            "coordinate_context": await self._build_coordinate_context(
                before_observation=before_observation,
                after_observation=after_observation,
            ),
            "before_observation": before_observation,
            "after_observation": after_observation,
            "executor_result": _json_safe(executor_result),
            "error": error_text,
        }
        self.writer.append_step(step_record)
        self.writer.write_manifest(status="running")
        if error_text:
            raise GUIWrapperError(error_text)
        return executor_result, step_record

    async def _execute_action(self, action_name: str, parameters: Mapping[str, Any]) -> Dict[str, Any]:
        if action_name == "click":
            return await self._call_controller(
                "click",
                x=parameters.get("x"),
                y=parameters.get("y"),
                button=str(parameters.get("button", "left")),
                clicks=int(parameters.get("clicks", 1)),
                interval=float(parameters.get("interval", 0.0)),
            )
        if action_name == "move":
            return await self._call_controller(
                "move_to",
                int(parameters["x"]),
                int(parameters["y"]),
                duration=float(parameters.get("duration", 0.0)),
            )
        if action_name == "type":
            return await self._call_controller(
                "type_text",
                str(parameters.get("text", "")),
                interval=float(parameters.get("interval", 0.0)),
            )
        if action_name == "paste_text":
            return await self._call_controller("paste_text", str(parameters.get("text", "")))
        if action_name == "press":
            return await self._call_controller(
                "press",
                str(parameters["key"]),
                presses=int(parameters.get("presses", 1)),
                interval=float(parameters.get("interval", 0.0)),
            )
        if action_name == "hotkey":
            return await self._call_controller(
                "hotkey",
                list(parameters.get("keys", [])),
                interval=float(parameters.get("interval", 0.0)),
            )
        if action_name == "activate_app":
            return await self._call_controller(
                "activate_app",
                str(parameters["app_name"]),
            )
        if action_name == "scroll":
            return await self._call_controller(
                "scroll",
                int(parameters["clicks"]),
                x=parameters.get("x"),
                y=parameters.get("y"),
            )
        if action_name == "wait":
            seconds = float(parameters.get("seconds", 0.0))
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if self.overlay is not None:
                    self.overlay.check()
                await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))
            return {"seconds": seconds}
        raise GUIWrapperError(f"Unsupported GUI wrapper action '{action_name}'.")

    async def _capture_step_observation(
        self,
        *,
        step_id: str,
        phase: str,
            source: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        path = self.writer.next_screenshot_path(step_id=step_id, phase=phase)
        observation = await self._observe_raw(path=path, region=self.observation_region)
        return self._build_observation_record(
            observation,
            phase=phase,
            step_id=step_id,
            source=source,
        )

    async def _observe_raw(
        self,
        *,
        path: Optional[Union[str, Path]],
        region: Optional[Sequence[int]],
    ) -> Dict[str, Any]:
        return await self._call_controller("observe", path=path, region=region)

    async def _build_coordinate_context(
        self,
        *,
        before_observation: Optional[Mapping[str, Any]],
        after_observation: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        screen_size = await self._optional_controller_call("screen_size")
        position = await self._optional_controller_call("position")
        observation = before_observation or after_observation or {}
        image_size = _mapping_or_none(observation.get("image_size")) if observation else None
        region = observation.get("region") if isinstance(observation, Mapping) else None
        scale = _infer_scale(image_size=image_size, screen_size=_mapping_or_none(screen_size))
        return {
            "screen_size": _json_safe(screen_size),
            "mouse_position": _json_safe(position),
            "image_size": _json_safe(image_size),
            "region": _json_safe(region),
            "scale_x": scale[0],
            "scale_y": scale[1],
            "coordinate_system": "controller-logical",
        }

    def _build_observation_record(
        self,
        observation: Mapping[str, Any],
        *,
        phase: str,
        step_id: Optional[str],
        source: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        screenshot_path = Path(str(observation.get("path", ""))).expanduser()
        if screenshot_path:
            screenshot_path = screenshot_path.resolve()
        image_size = _mapping_or_none(observation.get("image_size"))
        payload = {
            "record_type": "gui_observation",
            "schema_version": GUI_WRAPPER_SCHEMA_VERSION,
            "run_id": self.writer.run_id,
            "observation_id": generate_id("gui_obs"),
            "step_id": step_id,
            "phase": str(phase),
            "source": _json_safe(dict(source or {"agent": self.source_agent})),
            "captured_at": _utc_iso(),
            "path": str(screenshot_path),
            "mime_type": _mime_type_for_path(screenshot_path),
            "sha256": _sha256_file(screenshot_path) if screenshot_path.exists() else None,
            "file_size_bytes": _file_size(screenshot_path, observation),
            "image_size": _json_safe(image_size),
            "region": _json_safe(observation.get("region")),
            "mouse_position": _json_safe(observation.get("mouse_position")),
            "capture_verified": bool(observation.get("capture_verified", False)),
            "remote_path": observation.get("remote_path"),
            "remote_provider": observation.get("remote_provider"),
            "raw_observation": _json_safe(dict(observation)),
        }
        return payload

    def _source_payload(self, source: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        payload = {"agent": self.source_agent}
        if source:
            payload.update(dict(source))
        return payload

    async def _optional_controller_call(self, method_name: str) -> Optional[Dict[str, Any]]:
        try:
            value = await self._call_controller(method_name)
        except Exception:
            return None
        return dict(value) if isinstance(value, Mapping) else None

    async def _call_controller(self, method_name: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        from .run_state import checkpoint
        await checkpoint()
        method = getattr(self.controller, method_name, None)
        if method is None:
            raise GUIWrapperError(f"Wrapped GUI controller does not support '{method_name}'.")
        if self.overlay is not None:
            self.overlay.check()
        result = method(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, Mapping):
            return dict(result)
        raise GUIWrapperError(f"Wrapped GUI controller method '{method_name}' did not return a mapping.")


def build_gui_run_wrapper(
    controller: Any,
    *,
    root_directory: Union[str, Path] = DEFAULT_GUI_RUN_ROOT,
    run_id: Optional[str] = None,
    task_goal: str = "",
    source_agent: str = "coding-agent",
    settle_seconds: float = 0.1,
    observation_region: Optional[Sequence[int]] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> GUIRunWrapper:
    writer = GUIRunCaptureWriter(
        root_directory=Path(root_directory),
        run_id=run_id or generate_id("gui_run"),
        source_agent=source_agent,
        task_goal=task_goal,
        controller_name=str(getattr(controller, "name", "") or "desktop"),
        metadata=dict(metadata or {}),
    )
    return GUIRunWrapper(
        controller=controller,
        writer=writer,
        name=f"{getattr(controller, 'name', 'desktop')}-gui-wrapper",
        settle_seconds=settle_seconds,
        observation_region=observation_region,
        source_agent=source_agent,
    )


def _normalize_wrapper_action(action: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    if not isinstance(action, Mapping):
        raise GUIWrapperError("GUI wrapper action must be a mapping.")
    action_name = str(action.get("action") or action.get("type") or "").strip().lower()
    if not action_name:
        raise GUIWrapperError("GUI wrapper action is missing an 'action' field.")
    if action_name not in SUPPORTED_GUI_WRAPPER_ACTIONS:
        raise GUIWrapperError(f"Unsupported GUI wrapper action '{action_name}'.")

    if action_name == "click":
        return action_name, {
            "x": _optional_int(action.get("x")),
            "y": _optional_int(action.get("y")),
            "button": str(action.get("button") or "left"),
            "clicks": _optional_int(action.get("clicks"), 1),
            "interval": _optional_float(action.get("interval"), 0.0),
        }
    if action_name == "move":
        return action_name, {
            "x": _require_int(action, "x"),
            "y": _require_int(action, "y"),
            "duration": _optional_float(action.get("duration"), 0.0),
        }
    if action_name == "type":
        return action_name, {
            "text": _require_string(action, "text"),
            "interval": _optional_float(action.get("interval"), 0.0),
        }
    if action_name == "paste_text":
        return action_name, {"text": _require_string(action, "text")}
    if action_name == "press":
        return action_name, {
            "key": _require_string(action, "key"),
            "presses": _optional_int(action.get("presses"), 1),
            "interval": _optional_float(action.get("interval"), 0.0),
        }
    if action_name == "hotkey":
        keys = action.get("keys")
        if not isinstance(keys, SequenceABC) or isinstance(keys, (str, bytes)):
            raise GUIWrapperError("GUI hotkey action requires a sequence of keys.")
        normalized_keys = [str(key).strip() for key in keys if str(key).strip()]
        if not normalized_keys:
            raise GUIWrapperError("GUI hotkey action requires at least one key.")
        return action_name, {
            "keys": normalized_keys,
            "interval": _optional_float(action.get("interval"), 0.0),
        }
    if action_name == "activate_app":
        return action_name, {"app_name": _require_string(action, "app_name")}
    if action_name == "scroll":
        return action_name, {
            "clicks": _require_int(action, "clicks"),
            "x": _optional_int(action.get("x")),
            "y": _optional_int(action.get("y")),
        }
    if action_name == "wait":
        return action_name, {"seconds": _require_float(action, "seconds")}
    raise GUIWrapperError(f"Unsupported GUI wrapper action '{action_name}'.")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_size(path: Path, observation: Mapping[str, Any]) -> Optional[int]:
    value = observation.get("file_size_bytes")
    if value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    if str(path) and path.exists():
        return int(path.stat().st_size)
    return None


def _mime_type_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".jpg" or suffix == ".jpeg":
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    return "image/png"


def _mapping_or_none(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, Mapping):
        return dict(value)
    return None


def _infer_scale(
    *,
    image_size: Optional[Mapping[str, Any]],
    screen_size: Optional[Mapping[str, Any]],
) -> Tuple[Optional[float], Optional[float]]:
    if not image_size or not screen_size:
        return None, None
    try:
        image_width = float(image_size["width"])
        image_height = float(image_size["height"])
        screen_width = float(screen_size["width"])
        screen_height = float(screen_size["height"])
    except (KeyError, TypeError, ValueError):
        return None, None
    if screen_width <= 0 or screen_height <= 0:
        return None, None
    return image_width / screen_width, image_height / screen_height


def _require_string(action: Mapping[str, Any], field_name: str) -> str:
    value = str(action.get(field_name, ""))
    if value == "":
        raise GUIWrapperError(f"GUI action requires '{field_name}'.")
    return value


def _require_int(action: Mapping[str, Any], field_name: str) -> int:
    value = action.get(field_name)
    if value is None:
        raise GUIWrapperError(f"GUI action requires '{field_name}'.")
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise GUIWrapperError(f"GUI action field '{field_name}' must be an integer.") from error


def _require_float(action: Mapping[str, Any], field_name: str) -> float:
    value = action.get(field_name)
    if value is None:
        raise GUIWrapperError(f"GUI action requires '{field_name}'.")
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise GUIWrapperError(f"GUI action field '{field_name}' must be a number.") from error


def _optional_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise GUIWrapperError("GUI action integer field is invalid.") from error


def _optional_float(value: Any, default: float) -> float:
    if value is None:
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise GUIWrapperError("GUI action numeric field is invalid.") from error
