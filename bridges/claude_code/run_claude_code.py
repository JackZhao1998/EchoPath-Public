#!/usr/bin/env python3
"""EchoPath bridge for the local `claude` CLI (Claude Code).

Reads one EchoPath JSON request on stdin, runs a single headless `claude -p`
turn, and writes one EchoPath JSON response on stdout.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))

from echopath_bridge import (  # noqa: E402
    collect_local_image_paths,
    env_flag,
    fail,
    format_prompt,
    load_auth_env,
    read_request,
    run_command,
    split_command,
    timeout_seconds,
    working_directory,
    write_response,
)

BRIDGE_NAME = "claude_code"

# Pinned rather than left to the CLI's own default, so EchoPath runs stay
# reproducible across machines whose `claude` default model differs.
DEFAULT_MODEL = "claude-opus-5"

# Values that mean "no explicit model was requested" rather than a model id.
MODEL_SENTINELS = {"claude", "claude-code", "default", "none", "null"}

# EchoPath owns the action boundary: the desktop, shell, and file skills belong
# to the runtime, not to the model backend. The bridge asks for planning and
# grounding text, so only local reads are allowed.
#
# Two layers enforce this, and the order matters. `--permission-mode dontAsk` is
# the real boundary: it denies anything outside the read-only set, including
# tools this list has never heard of. The deny list below is defence in depth --
# bare names drop those tools from the prompt entirely, so the model does not
# plan around capabilities it cannot use.
#
# Do not "simplify" this to `--disallowedTools "*"`. Deny beats allow, so the
# wildcard also removes Read; the model then writes its tool call as prose and
# the bridge returns confident text that never looked at the screenshot.
DEFAULT_ALLOWED_TOOLS = "Read"
DEFAULT_DISALLOWED_TOOLS = (
    "Bash,BashOutput,KillShell,Edit,Write,NotebookEdit,"
    "WebFetch,WebSearch,Task,Agent,Monitor,SlashCommand"
)
DEFAULT_MAX_TURNS = "4"
DEFAULT_PERMISSION_MODE = "dontAsk"


def main() -> int:
    request = read_request()
    load_auth_env(BRIDGE_NAME)

    model = resolve_model(request)
    image_paths = local_image_paths(request.get("input"))
    prompt = build_prompt(request, image_paths)
    cwd = working_directory(request)

    command = build_command(
        model=model,
        image_paths=image_paths,
        output_schema=request.get("output_schema"),
        cwd=cwd,
    )
    completed = run_command(command, cwd=cwd, timeout=timeout_seconds(request), stdin=prompt)
    data = parse_cli_json(completed.stdout)

    write_response(
        output_text=extract_output_text(data),
        model=str(data.get("model") or model),
        response_id=data.get("session_id"),
        request_id=data.get("uuid"),
        usage=normalize_usage(data),
        raw=data,
    )
    return 0


def resolve_model(request: Mapping[str, Any]) -> str:
    """Return a concrete model id, never a sentinel such as `default`.

    Precedence: the request, then the bridge-specific override, then the shared
    one, then the pinned default.
    """
    requested = str(request.get("model") or "").strip()
    if requested and requested.lower() not in MODEL_SENTINELS:
        return requested
    for name in ("ECHOPATH_CLAUDE_MODEL", "ECHOPATH_BRIDGE_MODEL"):
        override = os.getenv(name, "").strip()
        if override and override.lower() not in MODEL_SENTINELS:
            return override
    return DEFAULT_MODEL


def build_prompt(request: Mapping[str, Any], image_paths: list[str]) -> str:
    """Format the EchoPath request, pointing the CLI at any local screenshots.

    The CLI takes a text prompt, so images are passed by path and opened with the
    Read tool rather than inlined as content blocks.
    """
    prompt = format_prompt(request, image_prefix="Local image path")
    if image_paths:
        return (
            "Read each local image path listed below with the Read tool before answering.\n\n"
            + prompt
        )
    return prompt


def use_bare_mode() -> bool:
    """Decide whether to pass `--bare`.

    Bare mode skips discovery of hooks, plugins, MCP servers, and CLAUDE.md, so a
    bridge run cannot be steered by whatever repository it is pointed at. It also
    never reads the Claude Code OAuth login, so defaulting to it is only safe
    once an API key is present.
    """
    override = env_flag("ECHOPATH_CLAUDE_BARE")
    if override is not None:
        return override
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


def build_command(
    *,
    model: str,
    image_paths: list[str],
    output_schema: Any,
    cwd: Path,
) -> list[str]:
    executable = os.getenv("ECHOPATH_CLAUDE_EXECUTABLE", "claude").strip() or "claude"
    command = [executable, "-p", "--output-format", "json", "--model", model]

    if use_bare_mode():
        command.append("--bare")

    permission_mode = os.getenv(
        "ECHOPATH_CLAUDE_PERMISSION_MODE", DEFAULT_PERMISSION_MODE
    ).strip()
    if permission_mode:
        command.extend(["--permission-mode", permission_mode])

    allowed_tools = os.getenv("ECHOPATH_CLAUDE_ALLOWED_TOOLS", DEFAULT_ALLOWED_TOOLS).strip()
    if allowed_tools:
        command.extend(["--allowedTools", allowed_tools])

    disallowed_tools = os.getenv(
        "ECHOPATH_CLAUDE_DISALLOWED_TOOLS", DEFAULT_DISALLOWED_TOOLS
    ).strip()
    if disallowed_tools:
        command.extend(["--disallowedTools", disallowed_tools])

    max_turns = os.getenv("ECHOPATH_CLAUDE_MAX_TURNS", DEFAULT_MAX_TURNS).strip()
    if max_turns:
        command.extend(["--max-turns", max_turns])

    for directory in image_directories(image_paths, cwd):
        command.extend(["--add-dir", directory])

    if isinstance(output_schema, Mapping) and output_schema:
        command.extend(["--json-schema", json.dumps(dict(output_schema), ensure_ascii=False)])

    command.extend(split_command(os.getenv("ECHOPATH_CLAUDE_EXTRA_ARGS", "")))
    return command


def local_image_paths(input_value: Any) -> list[str]:
    """Screenshot paths, whether sent as content blocks or inside chat turns."""
    return collect_local_image_paths(input_value)


def image_directories(image_paths: list[str], cwd: Path) -> list[str]:
    """Directories the CLI needs read access to, beyond the working directory.

    EchoPath writes screenshots under its own runtime folders, which are often
    outside the bridge's working directory; without `--add-dir` the Read tool
    cannot open them.
    """
    resolved_cwd = Path(cwd).expanduser().resolve()
    directories: list[str] = []
    seen: set[Path] = set()
    for image_path in image_paths:
        parent = Path(image_path).expanduser().resolve().parent
        if parent in seen or parent == resolved_cwd or parent.is_relative_to(resolved_cwd):
            continue
        seen.add(parent)
        directories.append(str(parent))
    return directories


def parse_cli_json(stdout: str) -> dict[str, Any]:
    """Parse the `--output-format json` envelope and surface in-run failures.

    A failure inside the run (expired login, denied tool, turn limit) is reported
    on stdout with `is_error` set rather than by a non-zero exit status, so the
    envelope has to be inspected instead of trusted.
    """
    text = str(stdout or "").strip()
    if not text:
        fail("Claude Code returned no output. Check that `claude` is authenticated.")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"result": text}
    if not isinstance(payload, Mapping):
        return {"result": text}

    data = dict(payload)
    if data.get("is_error"):
        detail = str(data.get("result") or data.get("error") or "").strip()
        subtype = str(data.get("subtype") or "").strip()
        fail(f"Claude Code reported an error: {detail or subtype or 'no detail reported'}")
    return data


def extract_output_text(data: Mapping[str, Any]) -> str:
    """Prefer schema-constrained output, then the plain result text."""
    structured = data.get("structured_output")
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)
    for key in ("result", "output_text", "text", "response"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def normalize_usage(data: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten the CLI usage block into EchoPath's token-usage shape."""
    raw_usage = data.get("usage")
    usage: dict[str, Any] = dict(raw_usage) if isinstance(raw_usage, Mapping) else {}
    if "input_tokens" in usage and "output_tokens" in usage:
        usage.setdefault(
            "total_tokens",
            _coerce_int(usage.get("input_tokens")) + _coerce_int(usage.get("output_tokens")),
        )
    for key in ("total_cost_usd", "num_turns", "duration_ms"):
        if data.get(key) is not None:
            usage[key] = data[key]
    return usage


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
