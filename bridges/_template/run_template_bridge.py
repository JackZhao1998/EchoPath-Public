#!/usr/bin/env python3
"""Starting point for a new EchoPath model bridge.

Copy this folder to `bridges/<your_backend>/`, rename the file, then work
through the five numbered sections below. Everything else is already wired up:
stdin/stdout framing, timeouts, working directory, auth-file loading, and
Windows-safe executable resolution all come from `bridges/_shared`.

The contract is one JSON request on stdin and one JSON response on stdout.
Anything written to stderr is treated as diagnostics; a non-zero exit code
fails the EchoPath turn with that stderr as the reason.

Run it standalone while developing:

    echo '{"model":"default","input":"say hi"}' \
      | python bridges/_template/run_template_bridge.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))

from echopath_bridge import (  # noqa: E402
    extract_text,
    fail,
    format_prompt,
    json_object_or_text,
    load_auth_env,
    read_request,
    requested_model,
    run_command,
    split_command,
    timeout_seconds,
    working_directory,
    write_response,
)

# ---------------------------------------------------------------------------
# 1. Identity
# ---------------------------------------------------------------------------
# BRIDGE_NAME selects the auth file and the env-var namespace. With the value
# below, EchoPath reads `credentials/template.json` (or `.env`) and honours
# ECHOPATH_TEMPLATE_AUTH_FILE. Rename it to match your backend.
BRIDGE_NAME = "template"
ENV_PREFIX = f"ECHOPATH_{BRIDGE_NAME.upper()}"

# Pin a concrete model rather than deferring to the backend's own default, so
# runs stay reproducible across machines.
DEFAULT_MODEL = "your-backend-default-model"

# Environment variables the backend needs before it can authenticate. The
# bridge fails early with a readable message instead of letting the CLI fail
# with a cryptic one. Leave empty if the backend uses an ambient local login.
REQUIRED_AUTH_VARS: tuple[str, ...] = ()


def main() -> int:
    request = read_request()

    # ------------------------------------------------------------------
    # 2. Credentials
    # ------------------------------------------------------------------
    # Loads `credentials/<BRIDGE_NAME>.json` into the environment if present.
    # Variables already set in the environment are left alone, so an exported
    # key still wins over the file. Secrets are never echoed or logged.
    load_auth_env(BRIDGE_NAME)
    require_auth()

    model = resolve_model(request)
    prompt = format_prompt(request, image_prefix="Image path")

    # ------------------------------------------------------------------
    # 3. Command
    # ------------------------------------------------------------------
    command = build_command(model=model, request=request)

    completed = run_command(
        command,
        cwd=working_directory(request),
        timeout=timeout_seconds(request),
        stdin=prompt,  # drop this and put the prompt in argv if the CLI wants it there
    )

    # ------------------------------------------------------------------
    # 4. Response parsing
    # ------------------------------------------------------------------
    data = parse_output(completed.stdout)

    # ------------------------------------------------------------------
    # 5. Response
    # ------------------------------------------------------------------
    write_response(
        output_text=extract_text(data),
        model=str(data.get("model") or model),
        response_id=data.get("response_id") or data.get("id"),
        request_id=data.get("request_id"),
        usage=data.get("usage"),
        raw=data,
    )
    return 0


def require_auth() -> None:
    """Fail early, and by name, when a credential is missing."""
    missing = [name for name in REQUIRED_AUTH_VARS if not os.getenv(name, "").strip()]
    if missing:
        fail(
            f"{BRIDGE_NAME} bridge is missing {', '.join(missing)}. "
            f"Set them in credentials/{BRIDGE_NAME}.json, or export them, or point "
            f"{ENV_PREFIX}_AUTH_FILE at another file."
        )


def resolve_model(request: Mapping[str, Any]) -> str:
    """Turn EchoPath's routing sentinels into a concrete model id."""
    model = requested_model(request, default=DEFAULT_MODEL).strip()
    if model.lower() in {"default", "none", "null", BRIDGE_NAME}:
        return DEFAULT_MODEL
    return model


def build_command(*, model: str, request: Mapping[str, Any]) -> list[str]:
    """Assemble the backend invocation.

    Read every tunable from the environment so operators can adjust a bridge
    without editing the file, and keep the backend read-only: EchoPath owns the
    desktop, shell, and file action boundary, so the model backend should return
    planning, grounding, and verification output rather than acting on its own.
    """
    executable = os.getenv(f"{ENV_PREFIX}_EXECUTABLE", "your-backend-cli").strip()
    command = [executable, "--model", model]

    # Ask for machine-readable output when the backend supports it; parse_output
    # below already tolerates plain text as a fallback.
    command.extend(["--format", "json"])

    # Structured output: EchoPath sets `output_schema` when it needs JSON back.
    output_schema = request.get("output_schema")
    if isinstance(output_schema, Mapping) and output_schema:
        command.extend(["--schema", json.dumps(dict(output_schema), ensure_ascii=False)])

    # Escape hatch for flags this bridge does not model explicitly.
    command.extend(split_command(os.getenv(f"{ENV_PREFIX}_EXTRA_ARGS", "")))
    return command


def parse_output(stdout: str) -> dict[str, Any]:
    """Normalise backend stdout into a mapping.

    `json_object_or_text` accepts a JSON object or falls back to `{"output_text": ...}`
    for plain text. Replace it if your backend streams JSONL — see
    `bridges/codex_cli/run_codex_cli.py` for a line-by-line event reducer.

    Check for in-band failures here: a backend that exits 0 while reporting an
    error in its payload will otherwise look like a successful empty turn.
    """
    data = json_object_or_text(stdout)
    if data.get("is_error") or data.get("error"):
        detail = data.get("error") or data.get("result") or "no detail reported"
        fail(f"{BRIDGE_NAME} backend reported an error: {detail}")
    return data


if __name__ == "__main__":
    raise SystemExit(main())
