#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))

from echopath_bridge import fail, format_prompt, read_request, requested_model, write_response  # noqa: E402


def main() -> int:
    request = read_request()
    model = requested_model(request, default=os.getenv("OLLAMA_MODEL", "llama3.1"))
    host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    prompt = format_prompt(request, image_prefix="Image path")
    messages = []
    instructions = str(request.get("instructions") or "").strip()
    if instructions:
        messages.append({"role": "system", "content": instructions})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    if request.get("temperature") is not None:
        payload["options"] = {"temperature": request.get("temperature")}

    try:
        http_request = urllib.request.Request(
            f"{host}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(http_request, timeout=300) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        fail(f"Ollama bridge request failed: {exc}")

    message = data.get("message") if isinstance(data, dict) else {}
    output_text = ""
    if isinstance(message, dict):
        output_text = str(message.get("content") or "")
    write_response(
        output_text=output_text,
        model=str(data.get("model") or model) if isinstance(data, dict) else model,
        response_id=str(data.get("created_at") or "") if isinstance(data, dict) else None,
        usage={
            "input_tokens": data.get("prompt_eval_count", 0),
            "output_tokens": data.get("eval_count", 0),
            "total_tokens": data.get("prompt_eval_count", 0) + data.get("eval_count", 0),
        }
        if isinstance(data, dict)
        else {},
        raw=data if isinstance(data, dict) else {},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
