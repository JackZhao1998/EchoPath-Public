from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .chat_service import EchoPathChatService
from .config import load_global_model_router_config
from .models import AgentEvent
from .run_state import RunRegistry, RunStopped

# How long a tracked worker keeps a native segment open for its host before cancelling it.
NATIVE_WAIT_SECONDS = 1800


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send one task to the EchoPath core runtime."
    )
    parser.add_argument(
        "message",
        nargs="*",
        help="Task text. If omitted, EchoPath reads the task from stdin.",
    )
    parser.add_argument(
        "--config",
        default=str(Path.cwd() / "echopath.config.json"),
        help="Path to the EchoPath JSON config.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the complete runtime response as JSON.",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Stream concise task progress to stderr while the task runs.",
    )
    parser.add_argument(
        "--progress-json",
        action="store_true",
        help="Stream newline-delimited progress events as JSON to stderr.",
    )
    parser.add_argument(
        "--no-desktop-control",
        action="store_true",
        help="Prevent this task from controlling the visible desktop.",
    )
    parser.add_argument('--vm-endpoint', help='Control a VM bridge URL instead of this desktop.')
    parser.add_argument('--local-desktop', action='store_true', help='Use this computer even when the config names a VM.')
    parser.add_argument('--run-id', help='Reserved Control Room task ID. Internal use.')
    parser.add_argument('--native-handoff', action='store_true',
                        help='With --run-id: yield native replay segments to the host that started this task. Internal use.')
    return parser


def _progress_event(event: AgentEvent) -> dict[str, str]:
    timestamp = event.timestamp.astimezone(timezone.utc).strftime("%H:%M:%S")
    name = str(event.name or "update").replace("agent.", "").replace(".", " ")
    detail = str(event.message or "Working").strip()
    return {"timestamp": timestamp, "event": name, "detail": detail}


def _await_native(service: EchoPathChatService, registry: RunRegistry, run_id: str,
                  response: Mapping[str, Any], wait_seconds: float = NATIVE_WAIT_SECONDS) -> dict[str, Any]:
    """Publish the pending native segment, wait for the host's answer, then resume the replay."""
    handoff = dict(response["native_handoff"])
    token, session_id = handoff.pop("continuation_token"), handoff.pop("session_id")
    required = set(handoff.get("required_outputs") or [])
    deadline = time.monotonic() + wait_seconds
    while True:
        registry.offer_native(run_id, handoff)
        answer = None
        while answer is None:
            if time.monotonic() > deadline:
                answer = {"outputs": {}, "cancel": True}
                break
            time.sleep(.5)
            answer = registry.take_native_answer(run_id)
        outputs = answer["outputs"]
        if not answer["cancel"] and (set(outputs) != required or any(not str(v).strip() for v in outputs.values())):
            # Wrong outputs leave the segment pending; say why and keep waiting.
            handoff["reason"] = f"Outputs must be exactly these nonempty strings: {sorted(required)}"
            continue
        return service.resume_native(session_id=session_id, continuation_token=token,
                                     outputs=outputs, cancel=bool(answer["cancel"]))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    message = " ".join(args.message).strip()
    if not message and not sys.stdin.isatty():
        message = sys.stdin.read().strip()
    if not message:
        parser.error("provide a task message or pipe one on stdin")

    config_path = Path(args.config).expanduser().resolve()
    root = config_path.parent
    service = EchoPathChatService(
        config_path=config_path,
        download_directory=root / "downloads",
        log_directory=root / "log",
        vm_endpoint='' if args.local_desktop else args.vm_endpoint,
        run_id=args.run_id,
    )
    def report_progress(event: AgentEvent) -> None:
        progress = _progress_event(event)
        if args.progress_json:
            print(json.dumps(progress), file=sys.stderr, flush=True)
        elif args.progress:
            print(
                f"[{progress['timestamp']}] {progress['detail']}",
                file=sys.stderr,
                flush=True,
            )

    enabled_skills = None
    execution_constraints = None
    if args.no_desktop_control:
        config = load_global_model_router_config(config_path)
        enabled_skills = [
            name for name in config.skills.enabled if name != "pyautogui-desktop"
        ]
        execution_constraints = {
            "source": "cli",
            "desktop_control_allowed": False,
            "desktop_control_guidance": "Live desktop control is disabled for this CLI task.",
            "enabled_skills": enabled_skills,
        }
    elif args.native_handoff and args.run_id:
        execution_constraints = {
            "source": "cli",
            "desktop_control_allowed": True,
            "native_handoff_supported": True,
        }

    try:
        response = service.send_message(
            message=message,
            enabled_skills=enabled_skills,
            execution_constraints=execution_constraints,
            event_callback=report_progress if (args.progress or args.progress_json) else None,
        )
        while response.get("status") == "awaiting_native" and args.run_id:
            response = _await_native(service, RunRegistry(config_path), args.run_id, response)
    except (KeyboardInterrupt, RunStopped):
        print('EchoPath task stopped.', file=sys.stderr)
        return 130
    except Exception as error:
        print(f'EchoPath: {error}', file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(response, indent=2, default=str))

    result = response.get("result", {})
    if not isinstance(result, Mapping) or not result.get("success"):
        error = result.get("error") if isinstance(result, Mapping) else "unknown error"
        print(f"EchoPath run failed: {error}")
        return 1

    output = result.get("output")
    assistant_reply = ""
    execution: Any = None
    if isinstance(output, Mapping):
        assistant_reply = str(output.get("assistant_reply") or "").strip()
        execution = output.get("execution")
    if not args.json:
        print(assistant_reply or "Done.")

    if isinstance(execution, Mapping) and not bool(execution.get("success", True)):
        error = str(execution.get("error") or "").strip()
        if error:
            print(f"Execution note: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
