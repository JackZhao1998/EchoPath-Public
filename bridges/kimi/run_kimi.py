#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))

from echopath_bridge import (  # noqa: E402
    format_prompt,
    read_request,
    requested_model,
    run_command,
    timeout_seconds,
    working_directory,
    write_response,
)


def parse_stream_json(stdout: str) -> dict[str, Any]:
    events = []
    for line in str(stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            events.append(dict(value))
    if not events:
        return {"output_text": stdout.strip(), "events": []}
    text_parts = []
    usage = {}
    model = None
    response_id = None
    for event in events:
        model = model or event.get("model") or event.get("model_id")
        response_id = response_id or event.get("session_id") or event.get("response_id")
        if isinstance(event.get("usage"), Mapping):
            usage = dict(event["usage"])
        for key in ("text", "content", "result", "message"):
            value = event.get(key)
            if isinstance(value, str):
                text_parts.append(value)
    return {
        "output_text": "".join(text_parts).strip(),
        "model": model,
        "response_id": response_id,
        "usage": usage,
        "events": events,
    }


def main() -> int:
    request = read_request()
    model = requested_model(request, default="kimi")
    prompt = format_prompt(request, image_prefix="Image reference")
    command = [os.getenv("ECHOPATH_KIMI_EXECUTABLE", "kimi")]
    if model != "kimi":
        command.extend(["-m", model])
    command.extend(["-p", prompt, "--output-format", "stream-json"])

    completed = run_command(
        command,
        cwd=working_directory(request),
        timeout=timeout_seconds(request),
    )
    data = parse_stream_json(completed.stdout)
    write_response(
        output_text=str(data.get("output_text") or ""),
        model=str(data.get("model") or model),
        response_id=data.get("response_id"),
        usage=data.get("usage"),
        raw=data,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
