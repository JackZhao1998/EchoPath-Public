from __future__ import annotations

import json
from typing import Any, Mapping


def extract_json_object(raw_text: str) -> Any:
    text = str(raw_text or "").strip()
    if not text:
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    snippet = text[start : end + 1]
    try:
        return json.loads(snippet)
    except json.JSONDecodeError:
        return None


def required_int(payload: Mapping[str, Any], field_name: str) -> int:
    value = payload.get(field_name)
    if value is None:
        raise ValueError(f"Expected integer field '{field_name}'.")
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Expected integer field '{field_name}'.") from error


def optional_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Expected an integer-compatible value.") from error


def optional_float(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Expected a float-compatible value.") from error


def summarize_execution_value(value: Any) -> str:
    if isinstance(value, Mapping):
        preferred_keys = ["stdout", "page_title", "saved_pdf", "path", "text"]
        for key in preferred_keys:
            candidate = value.get(key)
            if candidate:
                return truncate_text(str(candidate))
        return truncate_text(json.dumps(dict(value), ensure_ascii=False))

    if isinstance(value, (list, tuple)):
        preview = ", ".join(truncate_text(str(item), limit=60) for item in list(value)[:5])
        return truncate_text(preview, limit=180)

    return truncate_text(str(value))


def truncate_text(value: str, *, limit: int = 180) -> str:
    normalized = " ".join(str(value or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1] + "…"
