"""Record a VM run's screen and bring it back to this Mac as an MP4.

The guest records with macOS screencapture or Linux FFmpeg (amazon_vm_agent.py's
/record endpoints); when the run ends, however it ends, the movie is copied
over the existing bridge in chunks, converted with the host's avconvert, and
removed from the guest. A recording problem is logged and never fails the run.
"""
from __future__ import annotations

import base64
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional


def recording_directory(config, config_path: Path) -> Path:
    directory = Path(config.desktop.vm_recording_directory)
    return directory if directory.is_absolute() else Path(config_path).resolve().parent / directory


def recording_path(config, config_path: Path, run_id: str) -> Path:
    return recording_directory(config, config_path) / f"{run_id}.mp4"


def _log(message: str) -> None:
    print(f"EchoPath recording: {message}", file=sys.stderr, flush=True)


def _convert(source: Path, target: Path, preset: str = "Preset1920x1080") -> None:
    """MOV from screencapture to an H.264 MP4 every browser plays; 1080p keeps files ~1/3 the Retina size."""
    result = subprocess.run(
        ["avconvert", "--source", str(source), "--output", str(target), "--preset", preset, "--replace"],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        raise RuntimeError((result.stderr or result.stdout or "avconvert failed").strip()[-300:])


def fetch_recording(controller, recording_id: str, target: Path, preset: str = "Preset1920x1080") -> Path:
    """Stop the guest recording, copy it here in chunks, convert, and delete the guest copy."""
    stopped = controller._post_json("/record/stop", {"recording_id": recording_id}, request_timeout_seconds=60)
    size = int(stopped.get("size") or 0)
    target.parent.mkdir(parents=True, exist_ok=True)
    # avconvert picks the format from the extension, so the partial file must end in .mov.
    movie = target.with_name(target.stem + ".download.mov")
    offset = 0
    with movie.open("wb") as stream:
        while True:
            chunk = controller._post_json("/record/read", {"recording_id": recording_id, "offset": offset},
                                          request_timeout_seconds=120)
            data = base64.b64decode(chunk.get("data_base64") or "")
            stream.write(data)
            offset += len(data)
            if chunk.get("eof") or not data:
                break
    if offset != size:
        raise RuntimeError(f"copied {offset} of {size} bytes")
    try:
        _convert(movie, target, preset)
    finally:
        movie.unlink(missing_ok=True)
    controller._post_json("/record/delete", {"recording_id": recording_id})
    return target


@contextmanager
def record_vm_run(config, config_path: Path, run_id: str) -> Iterator[Optional[Path]]:
    """Wrap one VM run; yields the MP4 path it will write, or None when not recording."""
    from .amazon_vm_service import AmazonVMDesktopController
    from .config import is_remote_desktop_provider

    desktop = config.desktop
    if not desktop.vm_record_runs or not is_remote_desktop_provider(desktop.provider) or sys.platform != "darwin":
        yield None
        return
    controller = AmazonVMDesktopController(endpoint=desktop.amazon_vm_endpoint, api_key_env=desktop.amazon_vm_api_key_env,
                                           credential_file=config_path.parent / "credentials" / "amazon_vm.json",
                                           timeout_seconds=desktop.amazon_vm_timeout_seconds)
    target = recording_path(config, config_path, run_id)
    try:
        controller._post_json("/record/start", {"recording_id": run_id, "max_seconds": desktop.vm_recording_max_seconds})
    except Exception as error:  # an older guest agent, or no screen-recording permission
        _log(f"not recording this run ({error}).")
        yield None
        return
    try:
        yield target
    finally:
        try:
            fetch_recording(controller, run_id, target, desktop.vm_recording_preset)
            _log(f"saved {target}")
        except Exception as error:
            _log(f"could not save the recording ({error}).")
