from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Optional


def read_request() -> dict[str, Any]:
    # Decode explicitly rather than relying on the default stream encoding: on
    # Windows that is the ANSI code page, which cannot represent most of what a
    # GUI agent scrapes off the screen.
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace").strip()
    if not raw:
        fail("Expected one EchoPath bridge JSON request on stdin.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"Bridge request was not valid JSON: {exc}")
    if not isinstance(payload, dict):
        fail("Bridge request must be a JSON object.")
    return payload


def write_response(
    *,
    output_text: str,
    model: Optional[str] = None,
    response_id: Optional[str] = None,
    request_id: Optional[str] = None,
    usage: Any = None,
    raw: Any = None,
) -> None:
    payload = {
        "output_text": str(output_text or ""),
        "model": model,
        "response_id": response_id,
        "request_id": request_id,
        "usage": usage if isinstance(usage, Mapping) else {},
        "raw": raw if isinstance(raw, Mapping) else {},
    }
    body = json.dumps(payload, ensure_ascii=False) + "\n"
    sys.stdout.buffer.write(body.encode("utf-8"))
    sys.stdout.buffer.flush()


def fail(message: str, code: int = 1) -> None:
    sys.stderr.buffer.write((str(message).strip() + "\n").encode("utf-8", errors="replace"))
    sys.stderr.buffer.flush()
    raise SystemExit(code)


def requested_model(request: Mapping[str, Any], *, default: str) -> str:
    model = str(request.get("model") or "").strip()
    if model and model not in {"default", "none", "null"}:
        return model
    env_model = os.getenv("ECHOPATH_BRIDGE_MODEL", "").strip()
    return env_model or default


def format_prompt(request: Mapping[str, Any], *, image_prefix: str = "Image path") -> str:
    parts = []
    instructions = str(request.get("instructions") or "").strip()
    if instructions:
        parts.append(instructions)

    output_schema = request.get("output_schema")
    if isinstance(output_schema, Mapping) and output_schema:
        parts.append(
            "Return only valid JSON matching this JSON Schema. Do not wrap it in Markdown.\n"
            + json.dumps(output_schema, ensure_ascii=False, indent=2)
        )

    input_value = request.get("input")
    if isinstance(input_value, str):
        parts.append(input_value)
    elif isinstance(input_value, list):
        input_parts = []
        for item in input_value:
            if not isinstance(item, Mapping):
                if isinstance(item, str) and item.strip():
                    input_parts.append(item)
                continue
            item_type = item.get("type")
            if item_type == "text" and isinstance(item.get("text"), str):
                input_parts.append(item["text"])
            elif item_type == "local_image" and isinstance(item.get("path"), str):
                input_parts.append(f"{image_prefix}: {item['path']}")
            elif "role" in item:
                # EchoPath's agents send chat transcripts, not content blocks.
                # These carry no "type" key, so a type-only reader drops the
                # user's turn and the backend answers as if nothing was asked.
                rendered = format_chat_message(item, image_prefix=image_prefix)
                if rendered:
                    input_parts.append(rendered)
        if input_parts:
            parts.append("\n\n".join(input_parts))
    elif input_value is not None:
        parts.append(json.dumps(input_value, ensure_ascii=False, indent=2))

    return "\n\n".join(part for part in parts if part).strip()


ROLE_LABELS = {"user": "User", "assistant": "Assistant", "system": "System"}


def format_chat_message(item: Mapping[str, Any], *, image_prefix: str = "Image path") -> str:
    """Render one `{"role": ..., "content": ...}` entry as labelled text."""
    role = str(item.get("role", "")).strip().lower() or "user"
    label = ROLE_LABELS.get(role, role.replace("_", " ").title())

    texts: list[str] = []
    content = item.get("content")
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                texts.append(block)
            elif isinstance(block, Mapping):
                if block.get("type") == "local_image" and isinstance(block.get("path"), str):
                    texts.append(f"{image_prefix}: {block['path']}")
                elif isinstance(block.get("text"), str):
                    texts.append(block["text"])
    elif content is not None:
        texts.append(json.dumps(content, ensure_ascii=False))

    body = "\n".join(text for text in texts if str(text).strip()).strip()
    return f"{label}: {body}" if body else ""


def collect_local_image_paths(input_value: Any) -> list[str]:
    """Local image paths from either content-block or chat-transcript input."""
    if not isinstance(input_value, list):
        return []

    paths: list[str] = []

    def take(candidate: Any) -> None:
        if isinstance(candidate, str) and candidate.strip():
            value = candidate.strip()
            if value not in paths:
                paths.append(value)

    for item in input_value:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") == "local_image":
            take(item.get("path"))
            continue
        content = item.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, Mapping) and block.get("type") == "local_image":
                    take(block.get("path"))
    return paths


def working_directory(request: Mapping[str, Any]) -> Path:
    extra_body = request.get("extra_body")
    metadata = request.get("metadata")
    for source in (extra_body, metadata):
        if isinstance(source, Mapping) and source.get("working_directory"):
            return Path(str(source["working_directory"])).expanduser().resolve()
    return Path.cwd()


def timeout_seconds(request: Mapping[str, Any], *, default: int = 300) -> int:
    extra_body = request.get("extra_body")
    raw_value = extra_body.get("timeout_seconds") if isinstance(extra_body, Mapping) else None
    if raw_value is None:
        raw_value = os.getenv("ECHOPATH_BRIDGE_TIMEOUT_SECONDS", str(default))
    try:
        return max(1, int(raw_value))
    except (TypeError, ValueError):
        return default


def split_command(value: str) -> list[str]:
    text = str(value or "").strip()
    return shlex.split(text) if text else []


def run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    stdin: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    if not command:
        fail("Bridge command is empty.")
    command = [resolve_executable(command[0]), *command[1:]]
    environment = dict(os.environ)
    if env:
        environment.update({str(key): str(value) for key, value in env.items()})
    try:
        completed = subprocess.run(
            command,
            input=stdin,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            cwd=str(cwd),
            timeout=timeout,
            check=False,
            env=environment,
        )
    except FileNotFoundError:
        fail(f"Bridge executable was not found: {command[0]}")
    except subprocess.TimeoutExpired:
        fail(f"Bridge command timed out after {timeout} seconds: {' '.join(command)}")

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        details = stderr or stdout or f"exit status {completed.returncode}"
        fail(f"Bridge command failed: {details}")
    return completed


def json_object_or_text(stdout: str) -> dict[str, Any]:
    text = str(stdout or "").strip()
    if not text:
        return {"output_text": ""}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"output_text": text}
    if isinstance(payload, Mapping):
        return dict(payload)
    return {"output_text": text}


def extract_text(data: Mapping[str, Any]) -> str:
    for key in ("output_text", "result", "text", "response", "content"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    structured = data.get("structured_output")
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)
    return ""


def repo_root() -> Path:
    """Repository root, derived from this file's location under bridges/_shared/."""
    return Path(__file__).resolve().parents[2]


def resolve_executable(name: str) -> str:
    """Resolve a command name to a concrete path.

    ``subprocess`` does not consult ``PATHEXT`` on Windows, so a Node-installed
    CLI such as ``claude.cmd`` is invisible to a bare ``claude`` argument.
    ``shutil.which`` does consult it, which keeps bridges portable.
    """
    candidate = str(name or "").strip()
    if not candidate:
        fail("Bridge executable name is empty.")
    if os.sep in candidate or (os.altsep and os.altsep in candidate):
        return str(Path(candidate).expanduser())
    return shutil.which(candidate) or candidate


def env_flag(name: str, default: Optional[bool] = None) -> Optional[bool]:
    """Read a tri-state boolean env var. Unset or ``auto`` returns ``default``."""
    raw = os.getenv(name, "").strip().lower()
    if not raw or raw == "auto":
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def parse_auth_payload(text: str, *, source: str) -> dict[str, str]:
    """Parse an auth file into a flat mapping of environment variables.

    Two shapes are accepted so operators can use whichever they already have:
    JSON (either ``{"env": {...}}`` or a flat object) and ``KEY=value`` lines.
    Values are never echoed; error messages name the file, never its contents.
    """
    body = str(text or "").strip()
    if not body:
        return {}

    if body.startswith("{"):
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            fail(f"Auth file is not valid JSON: {source} ({exc.msg} at line {exc.lineno})")
        if not isinstance(payload, Mapping):
            fail(f"Auth file must contain a JSON object: {source}")
        section = payload.get("env", payload)
        if not isinstance(section, Mapping):
            fail(f"Auth file 'env' must be a JSON object: {source}")
        return {
            str(key): str(value)
            for key, value in section.items()
            if value is not None and not isinstance(value, (Mapping, list))
        }

    variables: dict[str, str] = {}
    for line in body.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        key, _, value = entry.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if not key:
            continue
        variables[key] = value.strip().strip("'\"")
    return variables


def auth_file_candidates(bridge: str) -> list[Path]:
    """Ordered auth-file locations for ``bridge``, most specific first."""
    slug = str(bridge or "").strip()
    explicit = os.getenv(f"ECHOPATH_{slug.upper()}_AUTH_FILE", "").strip() or os.getenv(
        "ECHOPATH_AUTH_FILE", ""
    ).strip()
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    for directory in (repo_root() / "credentials", Path.cwd() / "credentials"):
        for suffix in (".json", ".env"):
            candidate = directory / f"{slug}{suffix}"
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def load_auth_env(bridge: str) -> dict[str, str]:
    """Load credentials for ``bridge`` from the first auth file that exists.

    Existing environment variables win: the file fills in what is missing rather
    than overriding a key the operator exported deliberately. Returns only the
    variables that were actually applied, so callers can log names without values.
    """
    explicit = os.getenv(f"ECHOPATH_{str(bridge).upper()}_AUTH_FILE", "").strip() or os.getenv(
        "ECHOPATH_AUTH_FILE", ""
    ).strip()
    for candidate in auth_file_candidates(bridge):
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError as exc:
            fail(f"Auth file could not be read: {candidate} ({exc.strerror or exc})")
        applied = {}
        for key, value in parse_auth_payload(text, source=str(candidate)).items():
            if not os.getenv(key, "").strip():
                os.environ[key] = value
                applied[key] = value
        return applied
    if explicit:
        fail(f"Auth file was not found: {explicit}")
    return {}
