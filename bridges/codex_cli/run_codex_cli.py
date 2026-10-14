#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))

from echopath_bridge import (  # noqa: E402
    fail,
    format_prompt,
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
    model = requested_model(request, default="codex")
    prompt = format_prompt(request, image_prefix="Image path")
    image_paths = local_image_paths(request.get("input"))
    schema_path = write_schema_file(request.get("output_schema"))
    try:
        command = build_command(model=model, image_paths=image_paths, schema_path=schema_path)
        completed = run_command(
            command,
            cwd=working_directory(request),
            timeout=timeout_seconds(request),
            stdin=prompt,
        )
    finally:
        if schema_path is not None:
            schema_path.unlink(missing_ok=True)

    data = parse_codex_jsonl(completed.stdout)
    write_response(
        output_text=str(data.get("output_text") or ""),
        model=str(data.get("model") or model),
        response_id=data.get("response_id"),
        request_id=data.get("request_id"),
        usage=data.get("usage"),
        raw=data,
    )
    return 0


def build_command(
    *,
    model: str,
    image_paths: list[str],
    schema_path: Path | None,
) -> list[str]:
    executable = os.getenv("ECHOPATH_CODEX_EXECUTABLE", "codex")
    approval_policy = os.getenv("ECHOPATH_CODEX_APPROVAL_POLICY", "never").strip()
    sandbox = os.getenv("ECHOPATH_CODEX_SANDBOX", "read-only").strip()

    command = [executable]
    if approval_policy:
        command.extend(["--ask-for-approval", approval_policy])
    command.append("exec")
    command.extend(["--ephemeral", "--skip-git-repo-check", "--json"])
    if sandbox:
        command.extend(["--sandbox", sandbox])
    if model and model not in {"codex", "default", "none", "null"}:
        command.extend(["--model", model])
    for image_path in image_paths:
        command.extend(["--image", image_path])
    if schema_path is not None:
        command.extend(["--output-schema", str(schema_path)])
    command.extend(split_command(os.getenv("ECHOPATH_CODEX_EXTRA_ARGS", "")))
    command.append("-")
    return command


def local_image_paths(input_value: Any) -> list[str]:
    if not isinstance(input_value, list):
        return []
    paths = []
    for item in input_value:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") != "local_image":
            continue
        path = item.get("path")
        if isinstance(path, str) and path.strip():
            paths.append(path.strip())
    return paths


def write_schema_file(schema: Any) -> Path | None:
    if not isinstance(schema, Mapping) or not schema:
        return None
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="echopath-codex-schema-",
        suffix=".json",
        delete=False,
    )
    with handle:
        json.dump(schema, handle)
    return Path(handle.name)


def parse_codex_jsonl(stdout: str) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    completed_messages: list[str] = []
    final_messages: list[str] = []
    fallback_text: list[str] = []
    usage: dict[str, Any] = {}
    response_id = None
    model = None

    for line in str(stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping):
            continue
        event = dict(event)
        events.append(event)
        if event.get("type") == "turn.failed":
            fail("Codex CLI did not complete the model response.")
        model = model or event.get("model")
        if event.get("type") == "thread.started":
            response_id = response_id or event.get("thread_id")
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), Mapping):
            usage = dict(event["usage"])
        item = event.get("item")
        if isinstance(item, Mapping):
            model = model or item.get("model")
            if (event.get("type") == "item.completed" and item.get("type") == "agent_message"
                    and isinstance(item.get("text"), str) and item["text"].strip()):
                completed_messages.append(item["text"])
                if item.get("phase") == "final_answer":
                    final_messages.append(item["text"])
        for key in ("output_text", "text", "result", "response", "content"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                fallback_text.append(value)

    if not events:
        fail("Codex CLI returned no JSON events.")
    # Progress/commentary is a separate message, not part of the final model
    # response. Concatenating them can turn valid planning JSON into two objects,
    # which EchoPath cannot route to the desktop executor.
    responses = final_messages or completed_messages or fallback_text
    if not responses:
        fail("Codex CLI returned no completed model response.")

    return {
        "output_text": responses[-1].strip(),
        "model": model,
        "response_id": response_id,
        "request_id": None,
        "usage": usage,
        "events": events,
    }


if __name__ == "__main__":
    raise SystemExit(main())
