from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence


CONSOLIDATED_MEMORY_SCHEMA_VERSION = 1
CONSOLIDATED_MEMORY_FILE_PREFIX = "consolidated-"
CONSOLIDATED_MEMORY_INDEX_FILENAME = "index.json"
ARXIV_PAPER_DOWNLOAD_GENERALIZATION_KIND = "arxiv_paper_download"
ARXIV_PAPER_DOWNLOAD_PURPOSE_SUMMARY = (
    "Download paper from arXiv with a given paper URL, arXiv ID, or title"
)
_ARXIV_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?arxiv\.org/(?:pdf|abs)/(?P<id>\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?",
    re.IGNORECASE,
)
_ARXIV_PDF_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?arxiv\.org/pdf/(?P<id>\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?",
    re.IGNORECASE,
)
_ARXIV_ID_PATTERN = re.compile(
    r"(?:arxiv\s*:\s*)?(?P<id>\d{4}\.\d{4,5})(?:v\d+)?",
    re.IGNORECASE,
)
_ARXIV_SEARCH_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?arxiv\.org/search/[^\s\"')<>]+",
    re.IGNORECASE,
)


class ConsolidatedMemoryLibraryError(RuntimeError):
    pass


class ConsolidatedMemoryLibrary:
    """File-backed library for curated replay-ready memories."""

    def __init__(self, *, root_directory: Path) -> None:
        self.root_directory = Path(root_directory).resolve()
        self.records_directory = self.root_directory / "records"
        self.index_path = self.root_directory / CONSOLIDATED_MEMORY_INDEX_FILENAME
        self.records_directory.mkdir(parents=True, exist_ok=True)

    def save_from_trajectory(
        self,
        trajectory: Mapping[str, Any],
        *,
        source_trajectory_path: Optional[Path] = None,
        selected_operations: Optional[Sequence[Mapping[str, Any]]] = None,
        selected_steps: Optional[Sequence[Mapping[str, Any]]] = None,
        consolidation: Optional[Mapping[str, Any]] = None,
        artifact_evidence: Optional[Sequence[Mapping[str, Any]]] = None,
        memory_id: Optional[str] = None,
        status: str = "active",
        tags: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        if not isinstance(trajectory, Mapping):
            raise ConsolidatedMemoryLibraryError("trajectory must be a mapping.")

        operations = _operation_list(
            selected_operations
            if selected_operations is not None
            else trajectory.get("successful_operations")
        )
        if not operations:
            raise ConsolidatedMemoryLibraryError(
                "consolidated memory requires at least one operation."
            )

        task = _mapping_copy(trajectory.get("task"))
        replay_task = _build_consolidated_replay_task(
            replay_task=trajectory.get("replay_task"),
            task=task,
            operations=operations,
        )
        if replay_task is None:
            raise ConsolidatedMemoryLibraryError(
                "consolidated memory requires a replayable task."
            )

        all_steps = _operation_list(trajectory.get("steps"))
        steps = (
            _operation_list(selected_steps)
            if selected_steps is not None
            else _default_selected_steps(all_steps, operations)
        )
        user_query = str(trajectory.get("user_query", "")).strip()
        concrete_purpose_summary = (
            str(trajectory.get("purpose_summary", "")).strip()
            or str(task.get("summary", "")).strip()
            or str(task.get("goal", "")).strip()
            or user_query
        )
        generalization = _infer_generalization(
            task=task,
            replay_task=replay_task,
            operations=operations,
            user_query=user_query,
            purpose_summary=concrete_purpose_summary,
        )
        purpose_summary = (
            str(generalization.get("purpose_summary", "")).strip()
            if generalization is not None
            else ""
        ) or concrete_purpose_summary
        match_keys = _normalized_match_keys(
            [
                user_query,
                concrete_purpose_summary,
                str(trajectory.get("replay_key", "")).strip(),
                *_list_of_strings(trajectory.get("match_keys")),
            ]
        )
        if not match_keys:
            raise ConsolidatedMemoryLibraryError(
                "consolidated memory requires at least one match key."
            )

        now = _utc_now()
        normalized_status = str(status or "active").strip().lower() or "active"
        selected_memory_id = (
            _sanitize_filename_component(memory_id)
            if memory_id
            else _build_memory_id(purpose_summary, trajectory, operations)
        )
        source_path = Path(source_trajectory_path).resolve() if source_trajectory_path else None
        record = {
            "record_type": "consolidated_memory",
            "schema_version": CONSOLIDATED_MEMORY_SCHEMA_VERSION,
            "memory_id": selected_memory_id,
            "created_at": now,
            "updated_at": now,
            "status": normalized_status,
            "replayable": True,
            "replay_key": match_keys[0],
            "match_keys": match_keys,
            "platform": sys.platform,
            "user_query": user_query,
            "purpose_summary": purpose_summary,
            "concrete_purpose_summary": concrete_purpose_summary,
            "goal_coverage": str(trajectory.get("goal_coverage", "full")).strip() or "full",
            "task": _json_clone(task),
            "replay_task": _json_clone(replay_task),
            "consolidated_operations": _json_clone(operations),
            "successful_operations": _json_clone(operations),
            "steps": _json_clone(steps),
            "source": _build_source_record(trajectory, source_path),
            "consolidation": _json_clone(dict(consolidation or {})),
            "generalization": _json_clone(generalization),
            "artifact_evidence": _json_clone(
                [dict(item) for item in artifact_evidence or [] if isinstance(item, Mapping)]
            ),
            "tags": _list_of_strings(tags),
        }

        record_path = self._record_path(selected_memory_id)
        self._write_json(record_path, record)
        self.rebuild_index()
        return self._metadata(record, record_path)

    def load_record(self, memory_id: str) -> Optional[Dict[str, Any]]:
        path = self._record_path(memory_id)
        record = self._read_record(path)
        if record is None:
            return None
        record["path"] = str(path.resolve())
        return record

    def iter_records(self) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        for path in sorted(self.records_directory.glob(f"{CONSOLIDATED_MEMORY_FILE_PREFIX}*.json")):
            record = self._read_record(path)
            if record is None:
                continue
            record["path"] = str(path.resolve())
            records.append(record)
        return records

    def list_metadata(
        self,
        *,
        limit: int = 0,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        normalized_status = str(status or "").strip().lower()
        entries = [
            self._metadata(record, Path(str(record["path"])))
            for record in self.iter_records()
            if not normalized_status
            or str(record.get("status", "")).strip().lower() == normalized_status
        ]
        entries.sort(key=lambda item: _parse_timestamp(str(item.get("updated_at", ""))))
        if limit > 0:
            entries = entries[-limit:]
        return entries

    def load_snapshot(self, *, limit: int = 10) -> Dict[str, Any]:
        memories = self.list_metadata(limit=max(int(limit or 0), 0))
        return {
            "known_consolidated_memories": _json_clone(memories),
            "last_consolidated_memory": _json_clone(memories[-1]) if memories else None,
        }

    def find_latest_successful_match(self, user_query: str) -> Optional[Dict[str, Any]]:
        replay_key = _normalize_replay_key(user_query)
        if not replay_key:
            return None

        best_record: Optional[Dict[str, Any]] = None
        for record in self.iter_records():
            if str(record.get("status", "")).strip().lower() != "active":
                continue
            if not bool(record.get("replayable")):
                continue
            if replay_key not in _normalized_match_keys(record.get("match_keys")):
                continue
            if best_record is None or _parse_timestamp(
                str(record.get("updated_at", ""))
            ) > _parse_timestamp(str(best_record.get("updated_at", ""))):
                best_record = record
        return _json_clone(best_record) if best_record is not None else None

    def list_generalized_records(
        self,
        *,
        kind: Optional[str] = None,
        status: Optional[str] = "active",
    ) -> List[Dict[str, Any]]:
        normalized_kind = str(kind or "").strip()
        normalized_status = str(status or "").strip().lower()
        records: List[Dict[str, Any]] = []
        for record in self.iter_records():
            if normalized_status and str(record.get("status", "")).strip().lower() != normalized_status:
                continue
            if not bool(record.get("replayable")):
                continue
            generalization = _mapping_copy(record.get("generalization"))
            if not generalization:
                continue
            if normalized_kind and str(generalization.get("kind", "")).strip() != normalized_kind:
                continue
            records.append(record)
        records.sort(key=lambda item: _parse_timestamp(str(item.get("updated_at", ""))))
        return _json_clone(records)

    def find_latest_generalized_record(self, *, kind: str) -> Optional[Dict[str, Any]]:
        records = self.list_generalized_records(kind=kind)
        return records[-1] if records else None

    # Curated settings that live in the index rather than in any record. A
    # rebuild has to carry them forward: the packaged index ships them, and
    # regenerating without them silently changes how replay is selected.
    _INDEX_CURATED_KEYS = ("default_replay_mode", "direct_replay_query_prefix")

    def rebuild_index(self) -> Dict[str, Any]:
        existing = self._read_index()
        payload: Dict[str, Any] = {
            "record_type": "consolidated_memory_index",
            "schema_version": CONSOLIDATED_MEMORY_SCHEMA_VERSION,
            "updated_at": _utc_now(),
            "memories": self.list_metadata(),
        }
        for key in self._INDEX_CURATED_KEYS:
            if key in existing:
                payload[key] = _json_clone(existing[key])
        # Absolute paths are deliberately omitted: this file is committed with
        # the packaged examples, and writing machine paths into it leaks the
        # author's home directory.
        self._write_json(self.index_path, payload)
        return payload

    def _read_index(self) -> Dict[str, Any]:
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return dict(payload) if isinstance(payload, Mapping) else {}

    def _record_path(self, memory_id: str) -> Path:
        safe_id = _sanitize_filename_component(memory_id)
        if not safe_id.startswith(CONSOLIDATED_MEMORY_FILE_PREFIX):
            safe_id = f"{CONSOLIDATED_MEMORY_FILE_PREFIX}{safe_id}"
        return self.records_directory / f"{safe_id}.json"

    def _read_record(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, Mapping):
            return None
        if payload.get("record_type") != "consolidated_memory":
            return None
        return dict(payload)

    def _metadata(self, record: Mapping[str, Any], path: Path) -> Dict[str, Any]:
        source = _mapping_copy(record.get("source"))
        task = _mapping_copy(record.get("task"))
        replay_task = _mapping_copy(record.get("replay_task"))
        generalization = _mapping_copy(record.get("generalization"))
        operations = record.get("consolidated_operations")
        steps = record.get("steps")
        return {
            "memory_id": str(record.get("memory_id", "")).strip() or None,
            "path": str(path.resolve()),
            "status": str(record.get("status", "")).strip() or None,
            "replayable": bool(record.get("replayable")),
            "replay_key": str(record.get("replay_key", "")).strip() or None,
            "match_keys": _list_of_strings(record.get("match_keys")),
            "purpose_summary": str(record.get("purpose_summary", "")).strip() or None,
            "concrete_purpose_summary": str(record.get("concrete_purpose_summary", "")).strip()
            or None,
            "generalization_kind": str(generalization.get("kind", "")).strip() or None,
            "generalization_name": str(generalization.get("name", "")).strip() or None,
            "generalization_parameters": _json_clone(generalization.get("parameters", []))
            if isinstance(generalization.get("parameters"), list)
            else [],
            "goal_coverage": str(record.get("goal_coverage", "")).strip() or None,
            "matched_user_query": str(record.get("user_query", "")).strip() or None,
            "created_at": str(record.get("created_at", "")).strip() or None,
            "updated_at": str(record.get("updated_at", "")).strip() or None,
            "task_type": str(replay_task.get("type") or task.get("type") or "").strip()
            or None,
            "task_summary": str(replay_task.get("summary") or task.get("summary") or "").strip()
            or None,
            "source_trajectory_id": str(source.get("trajectory_id", "")).strip() or None,
            "source_trajectory_path": str(source.get("trajectory_path", "")).strip() or None,
            "source_session_id": str(source.get("session_id", "")).strip() or None,
            "source_task_id": str(source.get("task_id", "")).strip() or None,
            "operation_count": len(operations) if isinstance(operations, list) else 0,
            "step_count": len(steps) if isinstance(steps, list) else 0,
            "tags": _list_of_strings(record.get("tags")),
        }

    def _write_json(self, path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )


def _build_consolidated_replay_task(
    *,
    replay_task: Any,
    task: Mapping[str, Any],
    operations: Sequence[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    task_record = _mapping_copy(replay_task)
    if not task_record:
        task_record = _mapping_copy(task)
    task_type = str(task_record.get("type", "")).strip().lower()
    if task_type not in {"desktop_task", "command_task", "file_task"}:
        return None

    payload = _mapping_copy(task_record.get("payload"))
    if task_type == "command_task":
        payload.pop("command", None)
        payload["commands"] = [dict(item) for item in operations]
    else:
        payload["actions"] = [dict(item) for item in operations]
    return {
        "type": task_type,
        "summary": str(task_record.get("summary", "")).strip(),
        "goal": str(task_record.get("goal", "")).strip(),
        "payload": payload,
    }


def _infer_generalization(
    *,
    task: Mapping[str, Any],
    replay_task: Mapping[str, Any],
    operations: Sequence[Mapping[str, Any]],
    user_query: str,
    purpose_summary: str,
) -> Optional[Dict[str, Any]]:
    del task
    return _infer_arxiv_paper_download_generalization(
        replay_task=replay_task,
        operations=operations,
        user_query=user_query,
        purpose_summary=purpose_summary,
    )


def _infer_arxiv_paper_download_generalization(
    *,
    replay_task: Mapping[str, Any],
    operations: Sequence[Mapping[str, Any]],
    user_query: str,
    purpose_summary: str,
) -> Optional[Dict[str, Any]]:
    if str(replay_task.get("type", "")).strip().lower() != "desktop_task":
        return None

    texts = _arxiv_candidate_texts(
        replay_task=replay_task,
        operations=operations,
        user_query=user_query,
        purpose_summary=purpose_summary,
    )
    haystack = "\n".join(texts)
    if "arxiv" not in haystack.casefold() or "download" not in haystack.casefold():
        return None

    arxiv_id = _extract_arxiv_id_from_text(haystack)
    if not arxiv_id:
        return None

    pdf_url = _extract_arxiv_pdf_url(texts, arxiv_id)
    search_url = _extract_arxiv_search_url(texts)
    paper_title = (
        _extract_arxiv_title_from_search_url(search_url)
        or _infer_arxiv_title_from_texts(texts)
        or f"arXiv:{arxiv_id}"
    )
    template_task = _template_arxiv_value(
        _json_clone(dict(replay_task)),
        arxiv_id=arxiv_id,
        paper_title=paper_title,
        search_url=search_url,
        pdf_url=pdf_url,
    )
    template_operations = [
        _template_arxiv_value(
            dict(operation),
            arxiv_id=arxiv_id,
            paper_title=paper_title,
            search_url=search_url,
            pdf_url=pdf_url,
        )
        for operation in operations
    ]
    return {
        "kind": ARXIV_PAPER_DOWNLOAD_GENERALIZATION_KIND,
        "name": "Download paper from arXiv",
        "purpose_summary": ARXIV_PAPER_DOWNLOAD_PURPOSE_SUMMARY,
        "description": (
            "Reusable GUI memory for downloading an arXiv paper PDF after binding "
            "a paper URL, arXiv ID, or title from the current request."
        ),
        "requires_one_of": ["paper_url", "arxiv_id", "paper_title"],
        "parameters": [
            {
                "name": "paper_url",
                "description": "An arXiv abs or PDF URL supplied by the user.",
                "required": False,
            },
            {
                "name": "arxiv_id",
                "description": "An arXiv identifier such as 1706.03762.",
                "required": False,
            },
            {
                "name": "paper_title",
                "description": "A paper title or title-like search phrase.",
                "required": False,
            },
            {
                "name": "pdf_url",
                "description": "The concrete arXiv PDF URL derived for replay.",
                "required": False,
                "derived": True,
            },
            {
                "name": "search_url",
                "description": "The concrete arXiv title-search URL derived for replay.",
                "required": False,
                "derived": True,
            },
        ],
        "intent_keys": [
            "download paper from arxiv",
            "download arxiv paper",
            "download arxiv pdf",
            "download paper from arxiv by url",
            "download paper from arxiv by arxiv id",
            "download paper from arxiv by title",
        ],
        "canonical": {
            "paper_title": paper_title,
            "arxiv_id": arxiv_id,
            "pdf_url": pdf_url,
            "search_url": search_url or None,
        },
        "template": {
            "task": template_task,
            "operations": template_operations,
            "operation_count": len(template_operations),
            "placeholders": [
                "{paper_url}",
                "{paper_title}",
                "{arxiv_id}",
                "{pdf_url}",
                "{search_url}",
            ],
        },
    }


def _arxiv_candidate_texts(
    *,
    replay_task: Mapping[str, Any],
    operations: Sequence[Mapping[str, Any]],
    user_query: str,
    purpose_summary: str,
) -> List[str]:
    texts = [
        str(user_query or "").strip(),
        str(purpose_summary or "").strip(),
        str(replay_task.get("summary", "")).strip(),
        str(replay_task.get("goal", "")).strip(),
    ]
    payload = _mapping_copy(replay_task.get("payload"))
    action_records = _operation_list(payload.get("actions")) + [
        dict(item) for item in operations if isinstance(item, Mapping)
    ]
    for action in action_records:
        for key in ("action", "text", "url", "expected_outcome", "description", "key"):
            value = action.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                texts.extend(str(item).strip() for item in value if str(item or "").strip())
            elif str(value or "").strip():
                texts.append(str(value).strip())
    return [text for text in texts if text]


def _extract_arxiv_id_from_text(value: str) -> str:
    match = _ARXIV_URL_PATTERN.search(str(value or ""))
    if match is not None:
        return match.group("id")
    match = _ARXIV_ID_PATTERN.search(str(value or ""))
    return match.group("id") if match is not None else ""


def _extract_arxiv_pdf_url(texts: Sequence[str], arxiv_id: str) -> str:
    for text in texts:
        match = _ARXIV_PDF_URL_PATTERN.search(str(text or ""))
        if match is not None:
            return match.group(0).rstrip(".,;)")
    return f"https://arxiv.org/pdf/{arxiv_id}.pdf"


def _extract_arxiv_search_url(texts: Sequence[str]) -> str:
    for text in texts:
        match = _ARXIV_SEARCH_URL_PATTERN.search(str(text or ""))
        if match is not None:
            return match.group(0).rstrip(".,;)")
    return ""


def _extract_arxiv_title_from_search_url(search_url: str) -> str:
    if not search_url:
        return ""
    parsed = urllib.parse.urlparse(search_url)
    query_values = urllib.parse.parse_qs(parsed.query).get("query", [])
    if not query_values:
        return ""
    return " ".join(str(query_values[0] or "").replace("+", " ").split())


def _infer_arxiv_title_from_texts(texts: Sequence[str]) -> str:
    patterns = [
        r"search\s+arxiv\s+for\s+[\"'“”‘’]?(?P<title>.+?)[\"'“”‘’]?\s+and\s+download",
        r"download\s+(?:the\s+)?(?P<title>.+?)\s+arxiv\s+pdf",
        r"download\s+(?:the\s+)?(?P<title>.+?)\s+from\s+arxiv",
    ]
    for text in texts:
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match is None:
                continue
            title = match.group("title").strip(" \"'“”‘’.,:;")
            title = re.sub(r"^(?:the|a|an)\s+", "", title, flags=re.IGNORECASE)
            if title and _normalize_replay_key(title) not in {"arxiv", "pdf"}:
                return " ".join(title.split())
    return ""


def _template_arxiv_value(
    value: Any,
    *,
    arxiv_id: str,
    paper_title: str,
    search_url: str,
    pdf_url: str,
) -> Any:
    if isinstance(value, str):
        updated = value
        if search_url:
            updated = updated.replace(search_url, "{search_url}")
        if pdf_url:
            updated = updated.replace(pdf_url, "{pdf_url}")
        if arxiv_id:
            updated = updated.replace(arxiv_id, "{arxiv_id}")
        if paper_title:
            updated = re.sub(
                re.escape(paper_title),
                "{paper_title}",
                updated,
                flags=re.IGNORECASE,
            )
        return updated
    if isinstance(value, list):
        return [
            _template_arxiv_value(
                item,
                arxiv_id=arxiv_id,
                paper_title=paper_title,
                search_url=search_url,
                pdf_url=pdf_url,
            )
            for item in value
        ]
    if isinstance(value, Mapping):
        return {
            key: _template_arxiv_value(
                item,
                arxiv_id=arxiv_id,
                paper_title=paper_title,
                search_url=search_url,
                pdf_url=pdf_url,
            )
            for key, item in value.items()
        }
    return value


def _build_source_record(
    trajectory: Mapping[str, Any],
    source_trajectory_path: Optional[Path],
) -> Dict[str, Any]:
    return {
        "record_type": str(trajectory.get("record_type", "")).strip() or None,
        "trajectory_id": str(trajectory.get("trajectory_id", "")).strip() or None,
        "trajectory_path": str(source_trajectory_path) if source_trajectory_path else None,
        "asset_directory": _optional_text(trajectory.get("asset_directory")),
        "session_id": str(trajectory.get("session_id", "")).strip() or None,
        "task_id": str(trajectory.get("task_id", "")).strip() or None,
        "matched_at": str(trajectory.get("matched_at", "")).strip() or None,
        "source_detail_path": _optional_text(trajectory.get("source_detail_path")),
    }


def _default_selected_steps(
    steps: Sequence[Mapping[str, Any]],
    operations: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    if not steps:
        return []
    if len(steps) == len(operations):
        return [dict(item) for item in steps]
    return []


def _build_memory_id(
    purpose_summary: str,
    trajectory: Mapping[str, Any],
    operations: Sequence[Mapping[str, Any]],
) -> str:
    slug = _slugify(purpose_summary, max_length=56) or "memory"
    digest = hashlib.sha256(
        json.dumps(
            {
                "trajectory_id": trajectory.get("trajectory_id"),
                "user_query": trajectory.get("user_query"),
                "operations": [dict(item) for item in operations],
            },
            ensure_ascii=True,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:12]
    return f"{CONSOLIDATED_MEMORY_FILE_PREFIX}{slug}-{digest}"


def _mapping_copy(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _operation_list(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _list_of_strings(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, Sequence):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


# GUI trajectories are recorded against one desktop's chrome and shortcuts. A
# macOS recording that opens Spotlight with command+space does nothing on
# Windows, so replaying across platforms silently performs the wrong actions
# instead of failing.
_DARWIN_ONLY_ACTIONS = {"activate_app", "menu_click"}
_DARWIN_ONLY_KEYS = {"command", "cmd", "option"}


def record_platform(record: Mapping[str, Any]) -> str:
    """The platform a record was recorded on, inferred when not declared.

    Records written before `platform` existed carry no marker, so fall back to
    the strongest available signal: actions and modifier keys that only exist on
    macOS. Anything still unidentified is treated as portable.
    """
    declared = str(record.get("platform", "")).strip().lower()
    if declared:
        return declared

    for operation in _iter_record_operations(record):
        action = str(operation.get("action", "")).strip().lower()
        if action in _DARWIN_ONLY_ACTIONS:
            return "darwin"
        keys = operation.get("keys")
        if isinstance(keys, Sequence) and not isinstance(keys, (str, bytes)):
            for key in keys:
                if str(key).strip().lower() in _DARWIN_ONLY_KEYS:
                    return "darwin"
    return ""


def record_is_platform_compatible(
    record: Mapping[str, Any], *, platform: Optional[str] = None
) -> bool:
    """True when `record` may be replayed on `platform` (default: this host)."""
    recorded = record_platform(record)
    if not recorded:
        return True
    return recorded == str(platform or sys.platform).strip().lower()


def _iter_record_operations(record: Mapping[str, Any]):
    for field in ("consolidated_operations", "successful_operations", "operations"):
        values = record.get(field)
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
            for operation in values:
                if isinstance(operation, Mapping):
                    yield operation


def _normalized_match_keys(values: Any) -> List[str]:
    keys: List[str] = []
    for value in _list_of_strings(values):
        normalized = _normalize_replay_key(value)
        if normalized and normalized not in keys:
            keys.append(normalized)
    return keys


def _normalize_replay_key(value: Any) -> str:
    return " ".join(str(value or "").strip().split()).casefold()


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _optional_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_timestamp(value: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _slugify(value: str, *, max_length: int = 64) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", str(value or "").strip().lower())
    slug = slug.strip("-")
    if len(slug) <= max_length:
        return slug
    return slug[:max_length].rstrip("-")


def _sanitize_filename_component(value: str) -> str:
    return _slugify(str(value or ""), max_length=120) or "memory"
