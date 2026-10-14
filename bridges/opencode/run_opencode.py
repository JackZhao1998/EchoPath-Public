#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))

from echopath_bridge import (  # noqa: E402
    extract_text,
    fail,
    format_prompt,
    json_object_or_text,
    read_request,
    requested_model,
    run_command,
    split_command,
    timeout_seconds,
    working_directory,
    write_response,
)


def main() -> int:
    request = read_request()
    command = split_command(os.getenv("ECHOPATH_OPENCODE_COMMAND", ""))
    if not command:
        fail("Set ECHOPATH_OPENCODE_COMMAND to the local OpenCode-compatible command.")
    model = requested_model(request, default="opencode")
    prompt = format_prompt(request, image_prefix="Image path")
    completed = run_command(
        command,
        cwd=working_directory(request),
        timeout=timeout_seconds(request),
        stdin=prompt,
    )
    data = json_object_or_text(completed.stdout)
    write_response(
        output_text=extract_text(data),
        model=str(data.get("model") or model),
        response_id=data.get("response_id") or data.get("id"),
        request_id=data.get("request_id"),
        usage=data.get("usage"),
        raw=data,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
