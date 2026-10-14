from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .pyautogui_service import PyAutoGUIController

RECORDING_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RECORDING_CHUNK_BYTES = 4 * 1024 * 1024


class ScreenRecorder:
    """One guest recording at a time, using screencapture or FFmpeg on Linux X11.

    Interrupting (SIGINT) lets the recorder finalise the movie; a hard kill
    can leave an unusable file, so stop() always interrupts first. A timer stops a
    recording the host never stopped, so a lost host cannot fill the disk.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.lock = threading.Lock()
        self.process: Optional[subprocess.Popen] = None
        self.recording_id: Optional[str] = None
        self.timer: Optional[threading.Timer] = None

    def path(self, recording_id: str) -> Path:
        if not RECORDING_ID.match(str(recording_id or "")):
            raise ValueError("Recording id must be 1-64 letters, digits, '-' or '_'.")
        return self.directory / f"{recording_id}.mov"

    def start(self, recording_id: str, max_seconds: float = 3600.0) -> Dict[str, Any]:
        path = self.path(recording_id)
        if sys.platform == "darwin":
            command = ["screencapture", "-v", "-C", "-k", str(path)]
        elif sys.platform.startswith("linux"):
            ffmpeg = shutil.which("ffmpeg")
            display = os.environ.get("DISPLAY")
            if not ffmpeg or not display:
                raise ValueError("Linux VM recording needs FFmpeg and an X11 display.")
            size = PyAutoGUIController().screen_size_sync()
            command = [ffmpeg, "-y", "-nostdin", "-loglevel", "error", "-f", "x11grab",
                       "-framerate", "15", "-video_size", f"{size['width']}x{size['height']}",
                       "-i", display, "-an", "-c:v", "libx264", "-preset", "ultrafast",
                       "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-pix_fmt", "yuv420p", str(path)]
        else:
            raise ValueError("Screen recording needs a macOS or Linux X11 guest.")
        with self.lock:
            self._stop_locked()
            self.directory.mkdir(parents=True, exist_ok=True)
            path.unlink(missing_ok=True)
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            # Surface missing codecs/display access before reporting recording as active.
            try:
                self.process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                pass
            else:
                self.process = None
                raise ValueError("The guest screen recorder could not start. Check recording permissions and FFmpeg support.")
            self.recording_id = recording_id
            self.timer = threading.Timer(max(10.0, float(max_seconds)), self.stop, args=(recording_id,))
            self.timer.daemon = True
            self.timer.start()
        return {"recording_id": recording_id, "recording": True}

    def stop(self, recording_id: str) -> Dict[str, Any]:
        path = self.path(recording_id)
        with self.lock:
            if self.recording_id == recording_id:
                self._stop_locked()
        if not path.exists() or path.stat().st_size <= 0:
            raise ValueError("No recording was saved for this id.")
        return {"recording_id": recording_id, "size": path.stat().st_size}

    def _stop_locked(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        process, self.process, self.recording_id = self.process, None, None
        if process is None or process.poll() is not None:
            return
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def read(self, recording_id: str, offset: int, length: int) -> Dict[str, Any]:
        path = self.path(recording_id)
        if self.recording_id == recording_id:
            raise ValueError("Stop the recording before reading it.")
        size = path.stat().st_size
        offset = max(0, int(offset))
        with path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(max(1, min(int(length), RECORDING_CHUNK_BYTES)))
        return {"size": size, "offset": offset, "eof": offset + len(data) >= size,
                "data_base64": base64.b64encode(data).decode("ascii")}

    def delete(self, recording_id: str) -> Dict[str, Any]:
        self.path(recording_id).unlink(missing_ok=True)
        return {"recording_id": recording_id, "deleted": True}


class AmazonVMDesktopAgentServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address,
        request_handler_class,
        *,
        controller: PyAutoGUIController,
        api_key: str = "",
        recording_directory: Optional[Path] = None,
    ):
        super().__init__(server_address, request_handler_class)
        self.controller = controller
        self.api_key = api_key.strip()
        self.recorder = ScreenRecorder(Path(recording_directory or "local_data/desktop_agent_recordings").resolve())


def desktop_permissions() -> Optional[Dict[str, bool]]:
    """macOS grants held by this bridge process; None where the OS has no such gate.

    Screen capture without the grant still returns an image (wallpaper only) and
    input without Accessibility is silently dropped, so the host cannot infer either.
    """
    if sys.platform != "darwin":
        return None
    try:
        import ctypes
        import Quartz
        services = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        services.AXIsProcessTrusted.restype = ctypes.c_bool
        return {"accessibility": bool(services.AXIsProcessTrusted()),
                "screen_recording": bool(Quartz.CGPreflightScreenCaptureAccess())}
    except Exception:
        return None


def desktop_session_status():
    """Windows capture/input need the logged-in, unlocked interactive desktop."""
    if sys.platform != 'win32':
        return None
    try:
        import ctypes
        from ctypes import wintypes
        user = ctypes.WinDLL('user32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        session = wintypes.DWORD()
        if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)) or session.value == 0:
            return {'ready': False, 'reason': 'Sign in to Windows; the bridge needs an interactive user session.'}
        user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user.OpenInputDesktop.restype = wintypes.HANDLE
        user.CloseDesktop.argtypes = [wintypes.HANDLE]
        user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        desktop = user.OpenInputDesktop(0, False, 0x101)  # READOBJECTS | SWITCHDESKTOP
        if not desktop:
            return {'ready': False, 'reason': 'Unlock Windows or close the secure desktop prompt in the VM window.'}
        try:
            name, needed = ctypes.create_unicode_buffer(256), wintypes.DWORD()
            ready = bool(user.GetUserObjectInformationW(desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed))) and name.value.casefold() == 'default'
            return {'ready': ready, 'session_id': session.value}
        finally:
            user.CloseDesktop(desktop)
    except Exception:
        return {'ready': False, 'reason': 'The Windows interactive desktop could not be verified.'}


class AmazonVMDesktopAgentRequestHandler(BaseHTTPRequestHandler):
    server: AmazonVMDesktopAgentServer
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(
                {
                    "status": "ok",
                    "service": "amazon-vm-desktop-agent",
                    "auth_required": bool(self.server.api_key),
                    "platform": sys.platform,
                    "permissions": desktop_permissions(),
                    "desktop_session": desktop_session_status(),
                    "python": os.path.realpath(sys.executable),
                }
            )
            return
        self._send_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorize():
            return
        if self.path in ('/observe', '/execute', '/record/start'):
            session = desktop_session_status()
            if session is not None and not session['ready']:
                self._send_json({'error': session.get('reason', 'Sign in and unlock Windows in the VM window.')}, status=HTTPStatus.CONFLICT)
                return
        if self.path == "/observe":
            self._handle_observe()
            return
        if self.path == "/execute":
            self._handle_execute()
            return
        if self.path == "/command":
            self._handle_command()
            return
        if self.path == "/files/write":
            self._handle_file_write()
            return
        if self.path in ("/record/start", "/record/stop", "/record/read", "/record/delete"):
            self._handle_recording(self.path.rsplit("/", 1)[-1])
            return
        self._send_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-EchoPath-Key")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return None

    def _handle_observe(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return
        try:
            observation = self.server.controller.observe_sync(region=payload.get("region"))
            screenshot_bytes = Path(str(observation["path"])).read_bytes()
        except Exception as error:
            self._send_json({"error": str(error)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        screen_size_method = getattr(self.server.controller, "screen_size_sync", None)
        screen_size = screen_size_method() if callable(screen_size_method) else None
        observation_payload = dict(observation)
        if isinstance(screen_size, Mapping):
            observation_payload["screen_size"] = screen_size
        self._send_json(
            {
                # macOS Retina displays commonly expose screenshots in device
                # pixels while PyAutoGUI accepts logical display coordinates.
                "observation": observation_payload,
                "image_base64": base64.b64encode(screenshot_bytes).decode("ascii"),
                "image_media_type": "image/png",
            }
        )

    def _handle_execute(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return
        action_name = str(payload.get("action", "")).strip().lower()
        if not action_name:
            self._send_json(
                {"error": "Request body must include a non-empty 'action' field."},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            result = self._dispatch_action(action_name, payload)
        except ValueError as error:
            self._send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            return
        except Exception as error:
            self._send_json({"error": str(error)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json({"action": action_name, "result": result})

    def _handle_command(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return
        try:
            result = self._run_command(payload)
        except ValueError as error:
            self._send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            return
        except Exception as error:
            self._send_json({"error": str(error)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json({"result": result})

    def _handle_recording(self, action: str) -> None:
        payload = self._read_json_body()
        if payload is None:
            return
        recorder = self.server.recorder
        recording_id = str(payload.get("recording_id", ""))
        try:
            if action == "start":
                result = recorder.start(recording_id, float(payload.get("max_seconds", 3600.0)))
            elif action == "stop":
                result = recorder.stop(recording_id)
            elif action == "read":
                result = recorder.read(recording_id, int(payload.get("offset", 0)),
                                       int(payload.get("length", RECORDING_CHUNK_BYTES)))
            else:
                result = recorder.delete(recording_id)
        except (ValueError, OSError) as error:
            self._send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json(result)

    def _handle_file_write(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return
        try:
            path = Path(str(payload.get("path", "")).strip()).expanduser()
            if not path.is_absolute():
                raise ValueError("File write path must be absolute.")
            raw_data = str(payload.get("data_base64", "")).strip()
            if not raw_data:
                raise ValueError("File write request must include data_base64.")
            file_bytes = base64.b64decode(raw_data, validate=True)
            if bool(payload.get("parents", True)):
                path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(file_bytes)
            mode = payload.get("mode")
            if mode is not None:
                path.chmod(int(str(mode), 8))
        except ValueError as error:
            self._send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            return
        except Exception as error:
            self._send_json({"error": str(error)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json(
            {
                "result": {
                    "path": str(path),
                    "size_bytes": len(file_bytes),
                    "exists": path.exists(),
                }
            }
        )

    def _dispatch_action(self, action_name: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        controller = self.server.controller
        if action_name == "click":
            return controller.click_sync(
                x=payload.get("x"),
                y=payload.get("y"),
                button=str(payload.get("button", "left")),
                clicks=int(payload.get("clicks", 1)),
                interval=float(payload.get("interval", 0.0)),
            )
        if action_name == "move":
            return controller.move_to_sync(
                x=int(payload["x"]),
                y=int(payload["y"]),
                duration=float(payload.get("duration", 0.0)),
            )
        if action_name == "drag":
            return controller.drag_sync(
                x=int(payload['x']), y=int(payload['y']),
                to_x=int(payload['to_x']), to_y=int(payload['to_y']),
                button=str(payload.get('button', 'left')),
                duration=float(payload.get('duration', .3)),
            )
        if action_name == "type":
            return controller.type_text_sync(
                text=str(payload["text"]),
                interval=float(payload.get("interval", 0.0)),
            )
        if action_name == "paste_text":
            return controller.paste_text_sync(text=str(payload["text"]))
        if action_name == "press":
            return controller.press_sync(
                key=str(payload["key"]),
                presses=int(payload.get("presses", 1)),
                interval=float(payload.get("interval", 0.0)),
            )
        if action_name == "hotkey":
            keys = payload.get("keys")
            if not isinstance(keys, list):
                raise ValueError("Hotkey action requires a 'keys' list.")
            return controller.hotkey_sync(
                keys=[str(key) for key in keys],
                interval=float(payload.get("interval", 0.0)),
            )
        if action_name == "scroll":
            return controller.scroll_sync(
                clicks=int(payload["clicks"]),
                x=payload.get("x"),
                y=payload.get("y"),
            )
        if action_name == "position":
            return controller.position_sync()
        if action_name == "screen_size":
            return controller.screen_size_sync()
        if action_name == "wait":
            seconds = float(payload.get("seconds", 0.0))
            time.sleep(max(0.0, seconds))
            return {"seconds": seconds}
        raise ValueError(f"Unsupported action '{action_name}'.")

    def _run_command(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        command = payload.get("command")
        shell = bool(payload.get("shell", False))
        background = bool(payload.get("background", False))
        timeout_seconds = float(payload.get("timeout_seconds", 120.0))
        cwd_value = payload.get("cwd")
        cwd = str(cwd_value) if cwd_value is not None else None
        env_payload = payload.get("env")
        env = os.environ.copy()
        if env_payload is not None:
            if not isinstance(env_payload, Mapping):
                raise ValueError("Command env must be a JSON object when provided.")
            env.update({str(key): str(value) for key, value in env_payload.items()})

        if isinstance(command, list):
            normalized_command: Any = [str(part) for part in command]
            if not normalized_command:
                raise ValueError("Command list must not be empty.")
        elif isinstance(command, str) and command.strip():
            normalized_command = command
        else:
            raise ValueError("Command request must include a non-empty command.")

        if background:
            process = subprocess.Popen(
                normalized_command,
                shell=shell,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return {
                "background": True,
                "pid": process.pid,
                "command": normalized_command,
                "shell": shell,
            }

        process = subprocess.Popen(
            normalized_command,
            shell=shell,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=max(1.0, timeout_seconds))
            returncode = int(process.returncode or 0)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except Exception:
                process.kill()
            try:
                stdout, stderr = process.communicate(timeout=5.0)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
            returncode = 124
            timeout_message = f"Command timed out after {timeout_seconds:.1f} seconds."
            stderr = f"{stderr or ''}\n{timeout_message}".strip()
        return {
            "background": False,
            "returncode": returncode,
            "stdout": (stdout or "")[-20000:],
            "stderr": (stderr or "")[-20000:],
            "command": normalized_command,
            "shell": shell,
            "timed_out": timed_out,
        }

    def _authorize(self) -> bool:
        expected = self.server.api_key
        if not expected:
            return True
        provided = str(self.headers.get("X-EchoPath-Key", "")).strip()
        if provided == expected:
            return True
        self._send_json({"error": "Unauthorized."}, status=HTTPStatus.UNAUTHORIZED)
        return False

    def _read_json_body(self) -> Optional[Dict[str, Any]]:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            content_length = 0
        raw_body = self.rfile.read(max(content_length, 0)).decode("utf-8") if content_length else ""
        try:
            payload = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError as error:
            self._send_json(
                {"error": f"Invalid JSON body: {error.msg}"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return None
        if not isinstance(payload, dict):
            self._send_json(
                {"error": "Request body must be a JSON object."},
                status=HTTPStatus.BAD_REQUEST,
            )
            return None
        return payload

    def _send_json(self, payload: Mapping[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-EchoPath-Key")
        if status >= 400:
            # Early errors can leave an unread request body on this connection.
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the EchoPath desktop bridge inside a local or cloud VM."
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    parser.add_argument("--port", type=int, default=8790, help="Port to bind.")
    parser.add_argument(
        "--screenshot-directory",
        default="local_data/desktop_agent_captures",
        help="Directory for screenshots captured inside the VM.",
    )
    parser.add_argument(
        "--recording-directory",
        default="",
        help="Directory for run recordings (default: next to the screenshot directory).",
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=0.1,
        help="PyAutoGUI pause value inside the VM.",
    )
    parser.add_argument(
        "--fail-safe",
        default="true",
        help="Whether pyautogui fail-safe corner escape is enabled (true/false).",
    )
    parser.add_argument(
        "--api-key-env",
        default="ECHOPATH_AMAZON_VM_API_KEY",
        help="Environment variable that contains the shared API key. Leave unset to disable auth.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    api_key = os.getenv(args.api_key_env, "").strip()
    controller = PyAutoGUIController(
        pause_seconds=float(args.pause_seconds),
        fail_safe=str(args.fail_safe).strip().lower() != "false",
        screenshot_directory=Path(args.screenshot_directory).resolve(),
        mask_host_controls=False,
    )
    server = AmazonVMDesktopAgentServer(
        (args.host, args.port),
        AmazonVMDesktopAgentRequestHandler,
        controller=controller,
        api_key=api_key,
        recording_directory=Path(args.recording_directory).resolve() if args.recording_directory
        else Path(args.screenshot_directory).resolve().parent / "recordings",
    )
    print(
        f"Amazon VM desktop agent running at http://{args.host}:{args.port} "
        f"(auth {'enabled' if api_key else 'disabled'})"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
