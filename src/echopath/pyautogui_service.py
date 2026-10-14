from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, Union

from PIL import Image, UnidentifiedImageError


PyAutoGUIBackendFactory = Callable[[], Any]


class PyAutoGUIError(RuntimeError):
    pass


@dataclass
class PyAutoGUIController:
    name: str = "desktop"
    pause_seconds: float = 0.1
    fail_safe: bool = True
    screenshot_directory: Optional[Path] = None
    backend_factory: Optional[PyAutoGUIBackendFactory] = None
    mask_host_controls: bool = True
    _backend: Any = field(default=None, init=False, repr=False)

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

    async def menu_click(self, *, process: str, menu: str, item: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self.menu_click_sync, process=process, menu=menu, item=item)

    async def activate_app(self, app_name: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self.activate_app_sync, app_name=app_name)

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
        backend = self._get_backend()
        target_path = self._resolve_screenshot_path(path)
        normalized_region = _normalize_region(region)

        try:
            image = backend.screenshot(region=normalized_region)
            if sys.platform == 'darwin' and self.mask_host_controls:
                from .tray import mask_controls
                image = mask_controls(image, normalized_region, backend.size())
            # Only persist the masked version; coordinates and dimensions stay intact.
            image.save(target_path)
        except Exception as error:
            self._cleanup_failed_capture(target_path)
            raise PyAutoGUIError(f"Desktop screenshot capture failed: {error}") from error

        if image is not None and not target_path.exists():
            save_method = getattr(image, "save", None)
            if callable(save_method):
                try:
                    save_method(str(target_path))
                except Exception:
                    pass

        if not target_path.exists():
            raise PyAutoGUIError(
                "pyautogui returned a screenshot response, but no screenshot file was written."
            )

        file_size_bytes = target_path.stat().st_size
        if file_size_bytes <= 0:
            self._cleanup_failed_capture(target_path)
            raise PyAutoGUIError("Desktop screenshot capture produced an empty file.")

        try:
            with Image.open(target_path) as saved_image:
                saved_image.load()
                validated_size = saved_image.size
        except (FileNotFoundError, OSError, UnidentifiedImageError) as error:
            self._cleanup_failed_capture(target_path)
            raise PyAutoGUIError(
                f"Desktop screenshot capture produced an unreadable image: {error}"
            ) from error

        size = getattr(image, "size", None)
        if not size or len(size) != 2:
            size = validated_size

        size, capture_scale = self._normalize_capture_to_logical_space(
            target_path,
            captured_size=size,
            region=normalized_region,
        )
        file_size_bytes = target_path.stat().st_size

        mouse_position = self.position_sync()
        return {
            "path": str(target_path),
            "region": list(normalized_region) if normalized_region is not None else None,
            "image_size": {"width": int(size[0]), "height": int(size[1])},
            "capture_scale": capture_scale,
            "mouse_position": mouse_position,
            "file_size_bytes": int(file_size_bytes),
            "capture_verified": True,
        }

    def _normalize_capture_to_logical_space(
        self,
        target_path: Path,
        *,
        captured_size: Sequence[int],
        region: Optional[Sequence[int]],
    ) -> Tuple[Tuple[int, int], float]:
        """Resize a HiDPI capture down to the coordinate space clicks use.

        pyautogui reports the pointer and accepts click targets in logical
        points, but a screenshot on a Retina display comes back in physical
        pixels. Visual grounding reads targets straight off the image, so a
        capture left at 2x sends every grounded click to roughly double its
        intended position, which walks the pointer off-screen and trips the
        fail-safe. Saved crops are matched against this image too, so keeping
        it in logical space is what lets 1x reference crops match at all.
        """
        captured_width = int(captured_size[0])
        captured_height = int(captured_size[1])

        if region is not None and len(region) == 4:
            expected_width = int(region[2])
            expected_height = int(region[3])
        else:
            screen = self.screen_size_sync()
            expected_width = int(screen["width"])
            expected_height = int(screen["height"])

        if expected_width <= 0 or expected_height <= 0:
            return (captured_width, captured_height), 1.0
        if (captured_width, captured_height) == (expected_width, expected_height):
            return (captured_width, captured_height), 1.0

        try:
            with Image.open(target_path) as captured_image:
                captured_image.load()
                captured_image.resize(
                    (expected_width, expected_height),
                    Image.LANCZOS,
                ).save(str(target_path))
        except (OSError, UnidentifiedImageError, ValueError):
            return (captured_width, captured_height), 1.0

        return (expected_width, expected_height), captured_width / expected_width

    def click_sync(
        self,
        *,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
        clicks: int = 1,
        interval: float = 0.0,
    ) -> Dict[str, Any]:
        backend = self._get_backend()
        coordinates = _normalize_coordinates(x, y)
        backend.click(
            x=coordinates[0],
            y=coordinates[1],
            button=str(button),
            clicks=int(clicks),
            interval=float(interval),
        )
        return {
            "x": coordinates[0],
            "y": coordinates[1],
            "button": str(button),
            "clicks": int(clicks),
            "interval": float(interval),
        }

    def move_to_sync(self, *, x: int, y: int, duration: float = 0.0) -> Dict[str, Any]:
        backend = self._get_backend()
        backend.moveTo(int(x), int(y), duration=float(duration))
        return {"x": int(x), "y": int(y), "duration": float(duration)}

    def type_text_sync(self, *, text: str, interval: float = 0.0) -> Dict[str, Any]:
        backend = self._get_backend()
        backend.write(str(text), interval=float(interval))
        return {"text": str(text), "interval": float(interval)}

    def drag_sync(self, *, x: int, y: int, to_x: int, to_y: int,
                  button: str = "left", duration: float = .3) -> Dict[str, Any]:
        if button not in ('left', 'middle', 'right') or not 0.1 <= duration <= 2:
            raise ValueError('Invalid drag options.')
        backend = self._get_backend()
        backend.moveTo(int(x), int(y))
        try:
            backend.dragTo(int(to_x), int(to_y), duration=float(duration), button=button)
        finally:
            # Release even if the user hits a fail-safe corner during a drag.
            failsafe = backend.FAILSAFE
            try:
                backend.FAILSAFE = False
                backend.mouseUp(button=button)
            finally:
                backend.FAILSAFE = failsafe
        return {'x': int(to_x), 'y': int(to_y), 'button': button}

    def paste_text_sync(self, *, text: str) -> Dict[str, Any]:
        backend = self._get_backend()
        text_value = str(text)
        if sys.platform == "darwin":
            subprocess.run(
                ["pbcopy"],
                input=text_value,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
            )
            keys = ["command", "v"]
        else:
            try:
                import pyperclip  # type: ignore
            except Exception as error:
                raise PyAutoGUIError(
                    "Clipboard paste requires pyperclip outside macOS."
                ) from error
            pyperclip.copy(text_value)
            keys, paste_target = (["ctrl", "v"], "windows") if sys.platform == "win32" else _linux_clipboard_paste_hotkey()
        backend.hotkey(*keys)
        result = {"text_length": len(text_value), "keys": keys}
        if sys.platform != "darwin":
            result["paste_target"] = paste_target
        return result

    def press_sync(
        self,
        *,
        key: str,
        presses: int = 1,
        interval: float = 0.0,
    ) -> Dict[str, Any]:
        backend = self._get_backend()
        normalized_key = _normalize_key_name(key)
        backend.press(normalized_key, presses=int(presses), interval=float(interval))
        return {"key": normalized_key, "presses": int(presses), "interval": float(interval)}

    def hotkey_sync(
        self,
        *,
        keys: Sequence[str],
        interval: float = 0.0,
    ) -> Dict[str, Any]:
        normalized_keys = [_normalize_key_name(key) for key in keys if str(key).strip()]
        if not normalized_keys:
            raise PyAutoGUIError("Hotkey actions require at least one key.")
        backend = self._get_backend()
        backend.hotkey(*normalized_keys, interval=float(interval))
        return {"keys": normalized_keys, "interval": float(interval)}

    def menu_click_sync(self, *, process: str, menu: str, item: str) -> Dict[str, Any]:
        if sys.platform != "darwin":
            raise PyAutoGUIError("menu_click is currently implemented only for macOS.")
        script = f"""
tell application "System Events"
  tell process {_applescript_quote(process)}
    tell menu bar item {_applescript_quote(menu)} of menu bar 1
      click
      delay 0.1
      click menu item {_applescript_quote(item)} of menu 1
    end tell
  end tell
end tell
"""
        subprocess.run(
            ["osascript"],
            input=script,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return {"process": str(process), "menu": str(menu), "item": str(item)}

    def activate_app_sync(self, *, app_name: str) -> Dict[str, Any]:
        if sys.platform != "darwin":
            raise PyAutoGUIError("activate_app is currently implemented only for macOS.")
        script = f'tell application {_applescript_quote(app_name)} to activate\n'
        subprocess.run(
            ["osascript"],
            input=script,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return {"app_name": str(app_name)}

    def scroll_sync(
        self,
        *,
        clicks: int,
        x: Optional[int] = None,
        y: Optional[int] = None,
    ) -> Dict[str, Any]:
        backend = self._get_backend()
        coordinates = _normalize_coordinates(x, y, allow_none=True)
        backend.scroll(int(clicks), x=coordinates[0], y=coordinates[1])
        return {"clicks": int(clicks), "x": coordinates[0], "y": coordinates[1]}

    def screen_size_sync(self) -> Dict[str, int]:
        backend = self._get_backend()
        width, height = backend.size()
        return {"width": int(width), "height": int(height)}

    def position_sync(self) -> Dict[str, int]:
        backend = self._get_backend()
        position = backend.position()
        if hasattr(position, "x") and hasattr(position, "y"):
            x_value = position.x
            y_value = position.y
        else:
            x_value, y_value = position
        return {"x": int(x_value), "y": int(y_value)}

    def _get_backend(self) -> Any:
        if self._backend is None:
            backend = self.backend_factory() if self.backend_factory is not None else _import_pyautogui()
            if hasattr(backend, "FAILSAFE"):
                backend.FAILSAFE = self.fail_safe
            if hasattr(backend, "PAUSE"):
                backend.PAUSE = self.pause_seconds
            _route_macos_events_to_session_tap()
            self._backend = backend
        return self._backend

    def _resolve_screenshot_path(self, path: Optional[Union[str, Path]]) -> Path:
        base_directory = Path(self.screenshot_directory or Path.cwd())
        if path is None:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            path_obj = base_directory / f"desktop-observation-{timestamp}.png"
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


def _route_macos_events_to_session_tap() -> bool:
    """Post synthetic events at the session tap rather than the HID tap.

    pyautogui posts every mouse and key event through ``kCGHIDEventTap``, which
    injects at the driver level. Recent macOS releases ignore those posts from
    an ordinary process even when Accessibility is granted: the cursor still
    moves, because moving it does not require delivery, while clicks and
    keystrokes reach no application at all. Nothing raises, so the failure looks
    like an app that will not take focus.

    ``kCGSessionEventTap`` injects into the login session instead and carries no
    such restriction, which is the right level for UI automation anyway. Set
    ECHOPATH_MACOS_HID_TAP=1 to keep pyautogui's original behaviour.
    """
    if sys.platform != "darwin":
        return False
    if os.environ.get("ECHOPATH_MACOS_HID_TAP") == "1":
        return False

    try:
        import Quartz
    except ImportError:
        return False

    if getattr(Quartz.CGEventPost, "_echopath_session_tap", False):
        return True

    original_post = Quartz.CGEventPost
    hid_tap = Quartz.kCGHIDEventTap
    session_tap = Quartz.kCGSessionEventTap

    def post_to_session_tap(tap, event):
        return original_post(session_tap if tap == hid_tap else tap, event)

    post_to_session_tap._echopath_session_tap = True
    Quartz.CGEventPost = post_to_session_tap
    return True


def _import_pyautogui():
    if sys.platform == 'win32':
        # Set awareness before PyAutoGUI's import-time legacy DPI call, so
        # screenshots and pointer coordinates use the same physical pixels.
        import ctypes
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except (AttributeError, OSError):
                pass
    try:
        import pyautogui
    except ImportError as error:
        raise PyAutoGUIError(
            "pyautogui is required for desktop GUI control. Install project dependencies first."
        ) from error
    return pyautogui


def _applescript_quote(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _normalize_coordinates(
    x: Optional[int],
    y: Optional[int],
    *,
    allow_none: bool = False,
) -> Tuple[Optional[int], Optional[int]]:
    if x is None and y is None and allow_none:
        return None, None
    if (x is None) != (y is None):
        raise PyAutoGUIError("GUI coordinates require both x and y, or neither.")
    if x is None or y is None:
        raise PyAutoGUIError("GUI coordinates require both x and y.")
    return int(x), int(y)


def _normalize_key_name(key: str) -> str:
    normalized = str(key).strip().lower()
    aliases = {
        "cmd": "command",
        "control": "ctrl",
        "escape": "esc",
        "return": "enter",
        "del": "delete",
    }
    return aliases.get(normalized, normalized)


_TERMINAL_WINDOW_HINTS = (
    "terminal",
    "gnome-terminal",
    "gnome-terminal-server",
    "org.gnome.terminal",
    "xterm",
    "xfce4-terminal",
    "lxterminal",
    "konsole",
    "tilix",
    "alacritty",
    "kitty",
    "wezterm",
)


def _linux_clipboard_paste_hotkey() -> tuple[list[str], str]:
    override = os.getenv("SEARCHAGENT_LINUX_PASTE_HOTKEY", "").strip()
    if override:
        normalized = override.replace("+", " ").replace(",", " ")
        keys = [_normalize_key_name(part) for part in normalized.split() if part.strip()]
        if keys:
            return keys, "override"

    identity = _active_x11_window_identity().lower()
    if any(hint in identity for hint in _TERMINAL_WINDOW_HINTS):
        return ["ctrl", "shift", "v"], "terminal"
    return ["ctrl", "v"], "default"


def _active_x11_window_identity() -> str:
    window_id = _run_xdotool(["getactivewindow"])
    if not window_id:
        return ""
    values = []
    # xdotool has getwindowname but no getwindowclassname command. Terminal
    # titles often contain only a user/host/path, so read the X11 class directly.
    try:
        result = subprocess.run(['xprop', '-id', window_id, 'WM_CLASS'],
                                capture_output=True, text=True, timeout=1, check=False)
        if result.returncode == 0:
            values.append(result.stdout)
    except (OSError, subprocess.TimeoutExpired):
        pass
    name = _run_xdotool(['getwindowname', window_id])
    if name:
        values.append(name)
    return " ".join(values)


def _run_xdotool(args: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            ["xdotool", *[str(arg) for arg in args]],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=1.0,
            check=False,
        )
    except Exception:
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def _normalize_region(region: Optional[Sequence[int]]) -> Optional[Tuple[int, int, int, int]]:
    if region is None:
        return None
    if len(region) != 4:
        raise PyAutoGUIError("Screenshot region must contain exactly four integers.")
    return tuple(int(value) for value in region)
