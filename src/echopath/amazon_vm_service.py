from __future__ import annotations

import asyncio
import atexit
import base64
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple, Union

from PIL import Image, UnidentifiedImageError


_http_client = None
_http_client_lock = threading.Lock()


def _shared_http_client():
    """Reuse connections across frame polls and short-lived desktop controllers."""
    global _http_client
    with _http_client_lock:
        if _http_client is None:
            import httpx
            limits = httpx.Limits(max_connections=16, max_keepalive_connections=8, keepalive_expiry=20)
            # Retry only connection establishment. Retrying an action after it
            # was sent could duplicate a click, paste, or command.
            transport = httpx.HTTPTransport(retries=2, limits=limits)
            _http_client = httpx.Client(transport=transport, follow_redirects=False)
            atexit.register(_http_client.close)
        return _http_client


class AmazonVMDesktopError(RuntimeError):
    pass


@dataclass
class AmazonVMDesktopController:
    name: str = "desktop"
    endpoint: str = ""
    api_key_env: str = "ECHOPATH_AMAZON_VM_API_KEY"
    screenshot_directory: Optional[Path] = None
    timeout_seconds: float = 30.0
    provider_name: str = "amazon-vm"
    _image_size: Optional[Tuple[int, int]] = None
    _remote_screen_size: Optional[Tuple[int, int]] = None
    credential_file: Optional[Path] = None

    def _load_api_key(self) -> str:
        key = os.getenv(self.api_key_env, "").strip()
        if key:
            return key
        path = self.credential_file or (
            Path(__file__).resolve().parents[2] / "credentials" / "amazon_vm.json"
        )
        try:
            credential = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ""
        except (OSError, ValueError):
            raise AmazonVMDesktopError("Cannot read the VM credential file as JSON.") from None
        if not isinstance(credential, dict) or any(
            not isinstance(credential.get(field), str) or not credential[field].strip()
            for field in ("endpoint", "api_key")
        ):
            raise AmazonVMDesktopError("The VM credential file needs nonempty endpoint and api_key fields.")
        # A saved secret belongs to one bridge, not every URL entered in the dashboard.
        if credential["endpoint"].strip().rstrip("/") != self.endpoint.strip().rstrip("/"):
            return ""
        return credential["api_key"].strip()

    async def observe(
        self,
        *,
        path: Optional[Union[str, Path]] = None,
        region: Optional[Sequence[int]] = None,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(self.observe_sync, path=path, region=region)

    async def click(
        self,
        *,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
        clicks: int = 1,
        interval: float = 0.0,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self.click_sync,
            x=x,
            y=y,
            button=button,
            clicks=clicks,
            interval=interval,
        )

    async def move_to(
        self,
        x: int,
        y: int,
        *,
        duration: float = 0.0,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(self.move_to_sync, x=x, y=y, duration=duration)

    async def type_text(
        self,
        text: str,
        *,
        interval: float = 0.0,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(self.type_text_sync, text=text, interval=interval)

    async def paste_text(self, text: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self.paste_text_sync, text=text)

    async def run_command(
        self,
        command: Union[str, Sequence[str]],
        *,
        shell: bool = False,
        background: bool = False,
        timeout_seconds: Optional[float] = None,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        check: bool = True,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self.run_command_sync,
            command,
            shell=shell,
            background=background,
            timeout_seconds=timeout_seconds,
            cwd=cwd,
            env=env,
            check=check,
        )

    async def write_file(
        self,
        path: Union[str, Path],
        data: bytes,
        *,
        mode: Optional[str] = None,
        parents: bool = True,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self.write_file_sync,
            path,
            data,
            mode=mode,
            parents=parents,
        )

    async def press(
        self,
        key: str,
        *,
        presses: int = 1,
        interval: float = 0.0,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self.press_sync,
            key=key,
            presses=presses,
            interval=interval,
        )

    async def hotkey(
        self,
        keys: Sequence[str],
        *,
        interval: float = 0.0,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(self.hotkey_sync, keys=keys, interval=interval)

    async def scroll(
        self,
        clicks: int,
        *,
        x: Optional[int] = None,
        y: Optional[int] = None,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(self.scroll_sync, clicks=clicks, x=x, y=y)

    async def screen_size(self) -> Dict[str, int]:
        return await asyncio.to_thread(self.screen_size_sync)

    async def position(self) -> Dict[str, int]:
        return await asyncio.to_thread(self.position_sync)

    def observe_sync(
        self,
        *,
        path: Optional[Union[str, Path]] = None,
        region: Optional[Sequence[int]] = None,
    ) -> Dict[str, Any]:
        target_path = self._resolve_screenshot_path(path)
        normalized_region = _normalize_region(region)
        payload: Dict[str, Any] = {}
        if normalized_region is not None:
            payload["region"] = list(normalized_region)

        response = self._post_json("/observe", payload)
        observation = response.get("observation")
        if not isinstance(observation, Mapping):
            raise AmazonVMDesktopError("Amazon VM observe response did not include observation metadata.")

        image_base64 = str(response.get("image_base64") or "").strip()
        if not image_base64:
            raise AmazonVMDesktopError("Amazon VM observe response did not include image data.")

        try:
            image_bytes = base64.b64decode(image_base64, validate=True)
        except (ValueError, TypeError) as error:
            raise AmazonVMDesktopError("Amazon VM observe response returned invalid base64 image data.") from error

        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(image_bytes)
        file_size_bytes = target_path.stat().st_size
        if file_size_bytes <= 0:
            self._cleanup_failed_capture(target_path)
            raise AmazonVMDesktopError("Amazon VM observe response produced an empty screenshot file.")

        try:
            with Image.open(target_path) as saved_image:
                saved_image.load()
                validated_size = saved_image.size
        except (FileNotFoundError, OSError, UnidentifiedImageError) as error:
            self._cleanup_failed_capture(target_path)
            raise AmazonVMDesktopError(
                f"Amazon VM observe response produced an unreadable screenshot image: {error}"
            ) from error

        image_size = _normalize_image_size(observation.get("image_size"), validated_size)
        remote_screen_size = _normalize_image_size(
            observation.get("screen_size"),
            (image_size["width"], image_size["height"]),
        )
        if normalized_region is None:
            image_size = _downscale_to_screen(target_path, image_size, remote_screen_size)
            file_size_bytes = target_path.stat().st_size
        if normalized_region is None:
            self._image_size = (image_size["width"], image_size["height"])
            self._remote_screen_size = (
                remote_screen_size["width"],
                remote_screen_size["height"],
            )
        mouse_position = _normalize_point(observation.get("mouse_position"), field_name="mouse_position")
        return {
            "path": str(target_path),
            "region": list(normalized_region) if normalized_region is not None else observation.get("region"),
            "image_size": image_size,
            "mouse_position": self._from_remote_point(mouse_position),
            "file_size_bytes": int(file_size_bytes),
            "capture_verified": bool(observation.get("capture_verified", True)),
            "remote_path": str(observation.get("path", "")).strip() or None,
            "remote_provider": self.provider_name,
        }

    def click_sync(
        self,
        *,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
        clicks: int = 1,
        interval: float = 0.0,
    ) -> Dict[str, Any]:
        coordinates = self._to_remote_coordinates(x, y, allow_none=True)
        return self._execute_action(
            "click",
            {
                "x": coordinates[0],
                "y": coordinates[1],
                "button": str(button),
                "clicks": int(clicks),
                "interval": float(interval),
            },
        )

    def move_to_sync(self, *, x: int, y: int, duration: float = 0.0) -> Dict[str, Any]:
        remote_x, remote_y = self._to_remote_coordinates(x, y)
        return self._execute_action(
            "move",
            {"x": remote_x, "y": remote_y, "duration": float(duration)},
        )

    def drag_sync(self, *, x: int, y: int, to_x: int, to_y: int,
                  button: str = "left", duration: float = .3) -> Dict[str, Any]:
        x, y = self._to_remote_coordinates(x, y)
        to_x, to_y = self._to_remote_coordinates(to_x, to_y)
        return self._execute_action("drag", {"x": x, "y": y, "to_x": to_x, "to_y": to_y,
                                             "button": button, "duration": duration})

    def type_text_sync(self, *, text: str, interval: float = 0.0) -> Dict[str, Any]:
        return self._execute_action(
            "type",
            {"text": str(text), "interval": float(interval)},
        )

    def paste_text_sync(self, *, text: str) -> Dict[str, Any]:
        return self._execute_action("paste_text", {"text": str(text)})

    def run_command_sync(
        self,
        command: Union[str, Sequence[str]],
        *,
        shell: bool = False,
        background: bool = False,
        timeout_seconds: Optional[float] = None,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        check: bool = True,
    ) -> Dict[str, Any]:
        if isinstance(command, str):
            normalized_command: Union[str, list[str]] = command
        else:
            normalized_command = [str(part) for part in command]
        payload: Dict[str, Any] = {
            "command": normalized_command,
            "shell": bool(shell),
            "background": bool(background),
        }
        command_timeout_seconds: Optional[float] = None
        if timeout_seconds is not None:
            command_timeout_seconds = float(timeout_seconds)
            payload["timeout_seconds"] = command_timeout_seconds
        if cwd is not None:
            payload["cwd"] = str(cwd)
        if env is not None:
            payload["env"] = {str(key): str(value) for key, value in env.items()}

        response = self._post_json(
            "/command",
            payload,
            request_timeout_seconds=command_timeout_seconds,
        )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise AmazonVMDesktopError("Amazon VM command response did not include a result mapping.")
        result_dict = dict(result)
        if (
            check
            and not bool(result_dict.get("background"))
            and int(result_dict.get("returncode", 0)) != 0
        ):
            raise AmazonVMDesktopError(
                "Amazon VM command failed with return code "
                f"{result_dict.get('returncode')}: {result_dict.get('stderr') or result_dict.get('stdout')}"
            )
        return result_dict

    def write_file_sync(
        self,
        path: Union[str, Path],
        data: bytes,
        *,
        mode: Optional[str] = None,
        parents: bool = True,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "path": str(path),
            "data_base64": base64.b64encode(bytes(data)).decode("ascii"),
            "parents": bool(parents),
        }
        if mode is not None:
            payload["mode"] = str(mode)
        response = self._post_json("/files/write", payload)
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise AmazonVMDesktopError("Amazon VM file write response did not include a result mapping.")
        return dict(result)

    def press_sync(
        self,
        *,
        key: str,
        presses: int = 1,
        interval: float = 0.0,
    ) -> Dict[str, Any]:
        return self._execute_action(
            "press",
            {"key": str(key), "presses": int(presses), "interval": float(interval)},
        )

    def hotkey_sync(
        self,
        *,
        keys: Sequence[str],
        interval: float = 0.0,
    ) -> Dict[str, Any]:
        normalized_keys = [str(key) for key in keys if str(key).strip()]
        if not normalized_keys:
            raise AmazonVMDesktopError("Hotkey actions require at least one key.")
        return self._execute_action(
            "hotkey",
            {"keys": normalized_keys, "interval": float(interval)},
        )

    def scroll_sync(
        self,
        *,
        clicks: int,
        x: Optional[int] = None,
        y: Optional[int] = None,
    ) -> Dict[str, Any]:
        coordinates = self._to_remote_coordinates(x, y, allow_none=True)
        return self._execute_action(
            "scroll",
            {"clicks": int(clicks), "x": coordinates[0], "y": coordinates[1]},
        )

    def screen_size_sync(self) -> Dict[str, int]:
        response = self._execute_action("screen_size", {})
        return {
            "width": int(response["width"]),
            "height": int(response["height"]),
        }

    def position_sync(self) -> Dict[str, int]:
        response = self._execute_action("position", {})
        return self._from_remote_point({
            "x": int(response["x"]),
            "y": int(response["y"]),
        })

    def _to_remote_coordinates(
        self, x: Optional[int], y: Optional[int], *, allow_none: bool = False
    ) -> Tuple[Optional[int], Optional[int]]:
        coordinates = _normalize_coordinates(x, y, allow_none=allow_none)
        if coordinates[0] is None or coordinates[1] is None:
            return coordinates
        if not self._image_size or not self._remote_screen_size:
            return coordinates
        image_width, image_height = self._image_size
        remote_width, remote_height = self._remote_screen_size
        if not image_width or not image_height:
            return coordinates
        return (
            round(int(coordinates[0]) * remote_width / image_width),
            round(int(coordinates[1]) * remote_height / image_height),
        )

    def _from_remote_point(self, point: Mapping[str, int]) -> Dict[str, int]:
        x, y = int(point["x"]), int(point["y"])
        if not self._image_size or not self._remote_screen_size:
            return {"x": x, "y": y}
        image_width, image_height = self._image_size
        remote_width, remote_height = self._remote_screen_size
        if not remote_width or not remote_height:
            return {"x": x, "y": y}
        return {
            "x": round(x * image_width / remote_width),
            "y": round(y * image_height / remote_height),
        }

    def _execute_action(self, action: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        response = self._post_json(
            "/execute",
            {"action": str(action).strip().lower(), **dict(payload)},
        )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise AmazonVMDesktopError(
                f"Amazon VM action '{action}' did not return a result mapping."
            )
        return dict(result)

    def _post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        request_timeout_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        import httpx

        endpoint = self.endpoint.strip().rstrip("/")
        if not endpoint:
            raise AmazonVMDesktopError(f"{self._provider_label()} desktop endpoint is not configured.")

        request_url = f"{endpoint}{path}"
        headers = {"Content-Type": "application/json; charset=utf-8"}
        api_key = self._load_api_key()
        if api_key:
            headers["X-EchoPath-Key"] = api_key
            # Existing SearchAgent VM bridges use the original header name.
            headers["X-SearchAgent-Key"] = api_key

        body = json.dumps(dict(payload)).encode("utf-8")
        http_timeout = self._request_timeout(request_timeout_seconds)
        try:
            response = _shared_http_client().post(request_url, content=body, headers=headers,
                timeout=httpx.Timeout(http_timeout, connect=min(2.0, http_timeout)))
            response_body = response.text
        except httpx.TimeoutException as timeout_error:
            raise AmazonVMDesktopError(
                f"{self._provider_label()} desktop agent request to '{request_url}' timed out."
            ) from timeout_error
        except httpx.RequestError as request_error:
            raise AmazonVMDesktopError(
                f"Could not reach the {self._provider_label()} desktop agent at "
                f"'{request_url}': {request_error}"
            ) from request_error
        if response.status_code >= 300:
            raise AmazonVMDesktopError(
                self._format_remote_error(request_url, status=response.status_code, body=response_body)
            )

        try:
            parsed = json.loads(response_body)
        except json.JSONDecodeError as decode_error:
            raise AmazonVMDesktopError(
                f"{self._provider_label()} desktop agent at '{request_url}' returned invalid JSON."
            ) from decode_error
        if not isinstance(parsed, Mapping):
            raise AmazonVMDesktopError(
                f"{self._provider_label()} desktop agent at '{request_url}' returned a non-object JSON payload."
            )
        if parsed.get("error"):
            raise AmazonVMDesktopError(str(parsed["error"]))
        return dict(parsed)

    def _format_remote_error(self, request_url: str, *, status: int, body: str) -> str:
        message = body.strip()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, Mapping) and payload.get("error"):
            message = str(payload["error"])
        return (
            f"{self._provider_label()} desktop agent request to '{request_url}' failed with status {status}: "
            f"{message or 'unknown error'}"
        )

    def _resolve_screenshot_path(self, path: Optional[Union[str, Path]]) -> Path:
        base_directory = Path(self.screenshot_directory or Path.cwd())
        if path is None:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            prefix = self.provider_name.replace("_", "-")
            path_obj = base_directory / f"{prefix}-observation-{timestamp}.png"
        else:
            path_obj = Path(path)
            if not path_obj.is_absolute():
                path_obj = base_directory / path_obj
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        return path_obj

    def _cleanup_failed_capture(self, path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            return

    def _provider_label(self) -> str:
        return self.provider_name.replace("-", " ").strip().title() or "Remote VM"

    def _request_timeout(self, operation_timeout_seconds: Optional[float] = None) -> float:
        base_timeout = max(1.0, float(self.timeout_seconds))
        if operation_timeout_seconds is None:
            return base_timeout
        try:
            operation_timeout = float(operation_timeout_seconds)
        except (TypeError, ValueError):
            return base_timeout
        if operation_timeout <= 0:
            return base_timeout
        return max(base_timeout, operation_timeout + 5.0)


def _normalize_coordinates(
    x: Optional[int],
    y: Optional[int],
    *,
    allow_none: bool = False,
) -> Tuple[Optional[int], Optional[int]]:
    if x is None and y is None and allow_none:
        return None, None
    if (x is None) != (y is None):
        raise AmazonVMDesktopError("GUI coordinates require both x and y, or neither.")
    if x is None or y is None:
        raise AmazonVMDesktopError("GUI coordinates require both x and y.")
    return int(x), int(y)


def _downscale_to_screen(
    target_path: Path, image_size: Mapping[str, int], screen_size: Mapping[str, int]
) -> Dict[str, int]:
    """Resize a Retina guest capture to the guest's logical screen.

    A macOS guest returns screenshots at 2x, but clicks are sent in its logical
    points. Matching saved crops against the 2x image is roughly 60 times
    slower (a 3100x1938 capture took about 8 s per replay step, against 0.1 s
    at 1550x969) and misses crops recorded at 1x. Keeping observations in click
    space also makes the image-to-screen transform the identity.
    """
    width, height = int(image_size["width"]), int(image_size["height"])
    screen_width, screen_height = int(screen_size["width"]), int(screen_size["height"])
    if screen_width <= 0 or screen_height <= 0 or (width, height) == (screen_width, screen_height):
        return dict(image_size)
    if width < screen_width:  # never upscale
        return dict(image_size)
    try:
        with Image.open(target_path) as captured:
            captured.load()
            captured.resize((screen_width, screen_height), Image.LANCZOS).save(str(target_path))
    except (OSError, UnidentifiedImageError, ValueError):
        return dict(image_size)
    return {"width": screen_width, "height": screen_height}


def _normalize_region(region: Optional[Sequence[int]]) -> Optional[Tuple[int, int, int, int]]:
    if region is None:
        return None
    if len(region) != 4:
        raise AmazonVMDesktopError("Screenshot region must contain exactly four integers.")
    return tuple(int(value) for value in region)


def _normalize_image_size(raw_value: Any, fallback: Tuple[int, int]) -> Dict[str, int]:
    if isinstance(raw_value, Mapping):
        width = raw_value.get("width")
        height = raw_value.get("height")
        if width is not None and height is not None:
            return {"width": int(width), "height": int(height)}
    return {"width": int(fallback[0]), "height": int(fallback[1])}


def _normalize_point(raw_value: Any, *, field_name: str) -> Dict[str, int]:
    if not isinstance(raw_value, Mapping):
        raise AmazonVMDesktopError(
            f"Amazon VM desktop response did not include a valid '{field_name}' mapping."
        )
    x = raw_value.get("x")
    y = raw_value.get("y")
    if x is None or y is None:
        raise AmazonVMDesktopError(
            f"Amazon VM desktop response '{field_name}' mapping must include x and y."
        )
    return {"x": int(x), "y": int(y)}
