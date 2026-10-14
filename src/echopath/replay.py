from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .consolidated_memory_library import (
    ARXIV_PAPER_DOWNLOAD_GENERALIZATION_KIND,
    ConsolidatedMemoryLibraryError,
    ARXIV_PAPER_DOWNLOAD_PURPOSE_SUMMARY,
    ConsolidatedMemoryLibrary,
    record_is_platform_compatible,
    record_platform,
)


TRAJECTORY_FILE_PREFIX = "trajectory-"
TRAJECTORY_ASSET_DIRECTORY_SUFFIX = "-assets"
TRAJECTORY_SCHEMA_VERSION = 1
TRAJECTORY_STEP_PREVIEW_LIMIT = 8
REPLAYABLE_TASK_TYPES = {
    "command_task",
    "desktop_task",
    "file_task",
}
REPLAYABLE_DESKTOP_ACTIONS = {
    "activate_app",
    "click",
    "hotkey",
    "move",
    "paste_text",
    "press",
    "scroll",
    "type",
    "wait",
}
ARXIV_PDF_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?arxiv\.org/(?:pdf|abs)/(?P<id>\d{4}\.\d{4,5})(?:v\d+)?",
    re.IGNORECASE,
)
ARXIV_ID_PATTERN = re.compile(r"(?:arxiv\s*:\s*)?(?P<id>\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)
ARXIV_SEARCH_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?arxiv\.org/search/[^\s\"')<>]+",
    re.IGNORECASE,
)
CLOCK_TIMER_GENERALIZATION_KIND = "local_gui_clock_timer"
CLOCK_TIMER_DIRECT_REPLAY_PREFIX = "direct replay:"
CLOCK_TIMER_MAX_SECONDS = (23 * 60 * 60) + (59 * 60) + 59
CLOCK_TIMER_CLOSE_BUFFER_SECONDS = 1.5
WEATHER_LOCATION_GENERALIZATION_KIND = "local_gui_weather_location"
WEATHER_LOCATION_DIRECT_REPLAY_PREFIX = "direct replay:"
WEATHER_LOCATION_MAX_LENGTH = 120


def normalize_replay_key(value: str) -> str:
    return " ".join(str(value or "").strip().split()).casefold()


def extract_successful_operations(
    task_record: Mapping[str, Any],
    execution_output: Any,
) -> List[Dict[str, Any]]:
    task_type = str(task_record.get("type", "")).strip().lower()
    payload = _mapping_copy(task_record.get("payload"))

    if task_type == "command_task":
        return _filter_operations_by_results(
            _command_operations_from_payload(payload),
            execution_output,
        )
    if task_type == "file_task":
        return _filter_operations_by_results(
            _actions_from_payload(payload),
            execution_output,
        )
    if task_type == "desktop_task":
        return _desktop_operations_from_execution(payload, execution_output)
    return []


def build_replay_task(
    task_record: Mapping[str, Any],
    successful_operations: Sequence[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    if (task_record.get("payload") or {}).get("memory_segments"):
        return None
    task_type = str(task_record.get("type", "")).strip().lower()
    if task_type not in REPLAYABLE_TASK_TYPES:
        return None

    operations = [dict(item) for item in successful_operations if isinstance(item, Mapping)]
    if not operations:
        return None

    payload = _mapping_copy(task_record.get("payload"))
    if task_type == "command_task":
        payload.pop("command", None)
        payload["commands"] = operations
    else:
        payload["actions"] = operations
        if task_type == "desktop_task":
            payload.setdefault("verify_after_action", True)
            payload.setdefault("verify_goal_completion", True)
            payload["step_verification_failure_policy"] = "advisory"

    return {
        "type": task_type,
        "summary": str(task_record.get("summary", "")).strip(),
        "goal": str(task_record.get("goal", "")).strip(),
        "payload": payload,
    }


def extract_successful_trajectory_steps(
    task_record: Mapping[str, Any],
    execution_output: Any,
) -> List[Dict[str, Any]]:
    task_type = str(task_record.get("type", "")).strip().lower()
    payload = _mapping_copy(task_record.get("payload"))

    if task_type == "command_task":
        return _steps_from_result_list(
            _command_operations_from_payload(payload),
            execution_output,
        )
    if task_type == "file_task":
        return _steps_from_result_list(
            _actions_from_payload(payload),
            execution_output,
        )
    if task_type == "desktop_task":
        return _desktop_steps_from_execution(payload, execution_output)
    return []


def extract_clean_replay_trajectory_segment(
    task_record: Mapping[str, Any],
    execution_output: Any,
    *,
    execution_succeeded: bool = False,
) -> Dict[str, List[Dict[str, Any]]]:
    task_type = str(task_record.get("type", "")).strip().lower()
    payload = _mapping_copy(task_record.get("payload"))

    if task_type == "command_task":
        steps = _consecutive_steps_from_result_list(
            _command_operations_from_payload(payload),
            execution_output,
        )
    elif task_type == "file_task":
        steps = _consecutive_steps_from_result_list(
            _actions_from_payload(payload),
            execution_output,
        )
    elif task_type == "desktop_task":
        if execution_succeeded:
            steps = _completed_desktop_steps_from_execution(payload, execution_output)
        else:
            steps = _consecutive_desktop_steps_from_execution(payload, execution_output)
    else:
        steps = []

    operations = [
        dict(step.get("action"))
        for step in steps
        if isinstance(step, Mapping) and isinstance(step.get("action"), Mapping)
    ]
    return {"operations": operations, "steps": steps}


def build_action_trajectory(
    *,
    user_query: str,
    task_record: Mapping[str, Any],
    execution: Mapping[str, Any],
    session_id: str = "",
    task_id: str = "",
    matched_at: Optional[str] = None,
    assistant_reply: str = "",
    source_detail_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if (task_record.get("payload") or {}).get("memory_segments"):
        return None
    execution_output = execution.get("output")
    execution_success = bool(execution.get("success"))
    cleaned_segment = extract_clean_replay_trajectory_segment(
        task_record,
        execution_output,
        execution_succeeded=execution_success,
    )
    successful_operations = cleaned_segment["operations"]
    successful_steps = cleaned_segment["steps"]
    if not successful_operations or not successful_steps:
        return None

    replay_task = build_replay_task(task_record, successful_operations)
    goal_coverage = "full" if execution_success else "partial"
    purpose_summary = _build_trajectory_purpose_summary(
        user_query,
        task_record,
        successful_steps,
        goal_coverage=goal_coverage,
    )
    match_keys = _build_trajectory_match_keys(
        user_query=user_query,
        purpose_summary=purpose_summary,
        goal_coverage=goal_coverage,
    )
    trajectory_id = _build_trajectory_id(session_id=session_id, task_id=task_id)
    return {
        "record_type": "action_trajectory",
        "schema_version": TRAJECTORY_SCHEMA_VERSION,
        "trajectory_id": trajectory_id,
        "replay_key": normalize_replay_key(user_query),
        "user_query": str(user_query or "").strip(),
        "matched_at": matched_at or datetime.now(timezone.utc).isoformat(),
        "assistant_reply": str(assistant_reply or "").strip(),
        "session_id": str(session_id or "").strip(),
        "task_id": str(task_id or "").strip(),
        "task": _json_clone(task_record),
        "execution": {
            "success": execution_success,
            "error": str(execution.get("error") or "").strip() or None,
            "performed_action_count": _coerce_int(
                execution.get("performed_action_count"),
                default=len(successful_operations),
            ),
            "completed_without_action": bool(execution.get("completed_without_action", False)),
        },
        "status": "completed" if execution_success else "partial",
        "goal_coverage": goal_coverage,
        "purpose_summary": purpose_summary,
        "match_keys": match_keys,
        "replayable": bool(replay_task is not None and match_keys),
        "replay_task": _json_clone(replay_task) if replay_task is not None else None,
        "successful_operations": _json_clone(successful_operations),
        "steps": _json_clone(successful_steps),
        "source_detail_path": str(source_detail_path) if source_detail_path else None,
    }


def build_action_trajectory_from_logged_detail(
    detail_payload: Mapping[str, Any],
    *,
    detail_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    task_record = detail_payload.get("task")
    if not isinstance(task_record, Mapping):
        return None

    if (task_record.get("payload") or {}).get("memory_segments"):
        return None

    execution_success = _logged_execution_succeeded(detail_payload)
    cleaned_segment = _clean_replay_segment_from_logged_detail(
        task_record,
        detail_payload,
        execution_succeeded=execution_success,
    )
    operations = cleaned_segment["operations"]
    step_records = cleaned_segment["steps"]
    if not operations or not step_records:
        return None

    replay_task = build_replay_task(task_record, operations)
    replay_payload = detail_payload.get("replay")
    if replay_task is None and isinstance(replay_payload, Mapping):
        maybe_replay_task = replay_payload.get("task")
        if isinstance(maybe_replay_task, Mapping):
            replay_task = _json_clone(maybe_replay_task)

    execution = detail_payload.get("execution")
    execution_summary = dict(execution) if isinstance(execution, Mapping) else {}
    session_id = str(detail_payload.get("session_id", "")).strip()
    task_id = str(detail_payload.get("task_id", "")).strip()
    user_query = str(detail_payload.get("user_query", "")).strip()
    goal_coverage = "full" if execution_success else "partial"
    purpose_summary = _build_trajectory_purpose_summary(
        user_query,
        task_record,
        step_records,
        goal_coverage=goal_coverage,
    )
    match_keys = _build_trajectory_match_keys(
        user_query=user_query,
        purpose_summary=purpose_summary,
        goal_coverage=goal_coverage,
    )
    trajectory_id = _build_trajectory_id(session_id=session_id, task_id=task_id)
    return {
        "record_type": "action_trajectory",
        "schema_version": TRAJECTORY_SCHEMA_VERSION,
        "trajectory_id": trajectory_id,
        "replay_key": normalize_replay_key(user_query),
        "user_query": user_query,
        "matched_at": _extract_finished_at(detail_payload),
        "assistant_reply": str(detail_payload.get("assistant_reply", "")).strip(),
        "session_id": session_id,
        "task_id": task_id,
        "task": _json_clone(task_record),
        "execution": {
            "success": execution_success,
            "error": str(execution_summary.get("error") or "").strip() or None,
            "performed_action_count": _coerce_int(
                execution_summary.get("performed_action_count"),
                default=len(operations),
            ),
            "completed_without_action": bool(execution_summary.get("completed_without_action", False)),
        },
        "status": "completed" if execution_success else "partial",
        "goal_coverage": goal_coverage,
        "purpose_summary": purpose_summary,
        "match_keys": match_keys,
        "replayable": bool(replay_task is not None and match_keys),
        "replay_task": _json_clone(replay_task) if replay_task is not None else None,
        "successful_operations": _json_clone(operations),
        "steps": _json_clone(step_records),
        "source_detail_path": str(detail_path.resolve()) if detail_path is not None else None,
    }


def derive_replay_task_from_logged_detail(
    detail_payload: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    replay = detail_payload.get("replay")
    if isinstance(replay, Mapping):
        replay_task = replay.get("task")
        if isinstance(replay_task, Mapping):
            return _json_clone(replay_task)

    task_record = detail_payload.get("task")
    if not isinstance(task_record, Mapping):
        return None
    if not _logged_execution_succeeded(detail_payload):
        return None

    successful_operations = detail_payload.get("successful_operations")
    if isinstance(successful_operations, list):
        operations = [dict(item) for item in successful_operations if isinstance(item, Mapping)]
    else:
        operations = _derive_operations_from_logged_task(task_record)
    return build_replay_task(task_record, operations)


@dataclass(frozen=True)
class ReplayMatch:
    session_id: str
    task_id: str
    user_query: str
    matched_at: str
    detail_path: Path
    replay_task: Dict[str, Any]
    successful_operations: List[Dict[str, Any]]
    trajectory_path: Optional[Path] = None
    trajectory_steps: List[Dict[str, Any]] = field(default_factory=list)
    trajectory_status: str = ""
    purpose_summary: str = ""
    goal_coverage: str = ""
    matched_on: str = ""
    adaptation: Dict[str, Any] = field(default_factory=dict)

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "used": True,
            "source_session_id": self.session_id,
            "source_task_id": self.task_id,
            "source_detail_path": str(self.detail_path),
            "trajectory_path": str(self.trajectory_path) if self.trajectory_path else None,
            "trajectory_status": self.trajectory_status or None,
            "trajectory_step_count": len(self.trajectory_steps),
            "trajectory_steps_preview": _trajectory_step_previews(self.trajectory_steps),
            "matched_user_query": self.user_query,
            "matched_at": self.matched_at,
            "matched_on": self.matched_on or None,
            "operation_count": len(self.successful_operations),
            "task_type": str(self.replay_task.get("type", "")).strip() or None,
            "task_summary": str(self.replay_task.get("summary", "")).strip() or None,
            "purpose_summary": self.purpose_summary or None,
            "goal_coverage": self.goal_coverage or None,
            "adaptation": _json_clone(self.adaptation) if self.adaptation else None,
        }



MEMORY_REPLAY_VISUAL_BINDINGS_KEY = "memory_replay_visual_bindings"
MEMORY_REPLAY_STATE_CONTRACTS_KEY = "memory_replay_state_contracts"



def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}



def _operation_items(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _auto_memory_id(trajectory: Mapping[str, Any]) -> str:
    """Stable id for an auto-consolidated memory: one per query per platform."""
    replay_key = normalize_replay_key(str(trajectory.get("user_query", "")))
    digest = hashlib.sha1(replay_key.encode("utf-8")).hexdigest()[:12]
    platform = re.sub(r"[^a-z0-9]+", "", str(sys.platform).lower()) or "unknown"
    return f"auto-{platform}-{digest}"



def _is_ungated_desktop_replay(replay_task: Any) -> bool:
    """True when a raw runtime record would blind-replay GUI actions.

    Raw trajectories and session logs carry no visual bindings and no quality
    gate, so exact-query matching replayed whatever click sequence last
    "succeeded" -- including clicks on windows that merely happened to be
    open. Desktop replay is reserved for curated consolidated memories and
    the model-gated memory repository; raw desktop records only feed those.
    """
    if not isinstance(replay_task, Mapping):
        return False
    return str(replay_task.get("type") or "").strip().lower() == "desktop_task"


def _demote_match_for_incompatible_platform(
    record: Mapping[str, Any],
    match: Optional["ReplayMatch"],
) -> Optional["ReplayMatch"]:
    """Keep a foreign-platform memory's knowledge, drop its recorded actions.

    A GUI trajectory recorded on another desktop cannot be replayed here: its
    shortcuts and screen coordinates belong to that machine. Replaying a macOS
    Clock memory on Windows pressed command+space at a Spotlight that does not
    exist. The generalization is still valid, though -- the task kind and the
    parameter the caller asked for -- so hand the planner the adapted goal and
    let it drive this platform's UI instead of discarding the memory outright.
    """
    if match is None or record_is_platform_compatible(record):
        return match

    replay_task = _json_clone(match.replay_task)
    payload = replay_task.get("payload")
    if not isinstance(payload, dict):
        return None

    for key in (
        "actions",
        MEMORY_REPLAY_VISUAL_BINDINGS_KEY,
        MEMORY_REPLAY_STATE_CONTRACTS_KEY,
        "memory_replay_visual_reaim_required",
        "memory_replay_coordinate_copy_first",
        "replay_mode",
    ):
        payload.pop(key, None)
    payload["memory_replay_required"] = False
    payload["memory_replay_fallback_to_planning"] = True
    # The recorded payload disabled per-step and goal verification because
    # replay does its own probes. A demoted match runs the live planner
    # instead, and an unverified planned run cannot be trusted -- or learned
    # from -- so restore full verification (mirrors the task-level fallback).
    payload["verify_after_action"] = True
    payload["verify_goal_completion"] = True

    prompt = (
        str(match.purpose_summary or "").strip()
        or str(replay_task.get("goal") or "").strip()
        or str(replay_task.get("summary") or "").strip()
    )
    if prompt:
        payload["planner_prompt"] = prompt
    replay_task["payload"] = payload

    recorded_on = record_platform(record) or "another platform"
    return replace(
        match,
        replay_task=replay_task,
        successful_operations=[],
        trajectory_steps=[],
        matched_on=f"{match.matched_on or 'consolidated_memory'}:replanned_for_platform",
        adaptation={
            **dict(match.adaptation or {}),
            "recorded_platform": recorded_on,
            "actions_dropped": True,
            "reason": "recorded on a different platform; actions replanned locally",
        },
    )


class InteractionReplayStore:
    def __init__(
        self,
        *,
        root_directory: Path,
        trajectory_directory: Optional[Path] = None,
        consolidated_memory_directory: Optional[Path] = None,
    ) -> None:
        self.root_directory = Path(root_directory).resolve()
        self.trajectory_directory = Path(
            trajectory_directory or (self.root_directory.parent / "memory" / "actions")
        ).resolve()
        self.auto_consolidate = _env_flag("ECHOPATH_AUTO_CONSOLIDATE", True)
        self.consolidated_memory_library = ConsolidatedMemoryLibrary(
            root_directory=Path(
                consolidated_memory_directory
                or (self.root_directory.parent / "memory" / "consolidated")
            )
        )
        self.trajectory_directory.mkdir(parents=True, exist_ok=True)
        self._logs_backfilled = False

    def backfill_from_logs(self) -> int:
        if self._logs_backfilled:
            return 0

        self._materialize_existing_trajectory_assets()

        written = 0
        if self.root_directory.exists():
            for detail_path in sorted(self.root_directory.glob("session_*/interaction-*.json")):
                try:
                    payload = json.loads(detail_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, Mapping):
                    continue
                if payload.get("record_type") != "final":
                    continue
                if self.upsert_logged_trajectory(payload, detail_path=detail_path) is not None:
                    written += 1

        self._logs_backfilled = True
        return written

    def upsert_runtime_trajectory(
        self,
        *,
        user_query: str,
        task_record: Mapping[str, Any],
        execution: Mapping[str, Any],
        session_id: str = "",
        task_id: str = "",
        matched_at: Optional[str] = None,
        assistant_reply: str = "",
        source_detail_path: Optional[Path] = None,
    ) -> Optional[Dict[str, Any]]:
        trajectory = build_action_trajectory(
            user_query=user_query,
            task_record=task_record,
            execution=execution,
            session_id=session_id,
            task_id=task_id,
            matched_at=matched_at,
            assistant_reply=assistant_reply,
            source_detail_path=str(source_detail_path.resolve()) if source_detail_path else None,
        )
        if trajectory is None:
            return None
        trajectory_path = self._trajectory_path(session_id=session_id, task_id=task_id)
        stored_trajectory = self._prepare_trajectory_for_storage(trajectory, trajectory_path)
        self._write_json(trajectory_path, stored_trajectory)
        self.consolidate_trajectory(stored_trajectory, trajectory_path)
        return self._trajectory_metadata(stored_trajectory, trajectory_path)

    def upsert_logged_trajectory(
        self,
        detail_payload: Mapping[str, Any],
        *,
        detail_path: Optional[Path] = None,
    ) -> Optional[Dict[str, Any]]:
        trajectory = build_action_trajectory_from_logged_detail(
            detail_payload,
            detail_path=detail_path,
        )
        if trajectory is None:
            return None
        trajectory_path = self._trajectory_path(
            session_id=str(trajectory.get("session_id", "")),
            task_id=str(trajectory.get("task_id", "")),
        )
        stored_trajectory = self._prepare_trajectory_for_storage(trajectory, trajectory_path)
        self._write_json(trajectory_path, stored_trajectory)
        # Deliberately not consolidated: this is a catch-up scan over historical
        # logs, not a run that just succeeded. Learning here would re-import old
        # trajectories every time a store is constructed.
        return self._trajectory_metadata(stored_trajectory, trajectory_path)

    def consolidate_trajectory(
        self,
        trajectory: Mapping[str, Any],
        trajectory_path: Optional[Path] = None,
    ) -> Optional[Dict[str, Any]]:
        """Promote a successful run into a reusable consolidated memory.

        Without this, a trajectory is only ever recalled by its own normalized
        query, and nothing a machine learns locally becomes a first-class memory
        -- the only generalized records were the ones shipped with the package.
        The memory id is derived from the query and the platform, so repeating a
        task refreshes one record instead of accumulating near-duplicates, and a
        macOS and a Windows recording of the same request coexist.
        """
        if not self.auto_consolidate:
            return None
        if str(trajectory.get("status", "")).strip().lower() != "completed":
            return None
        if not bool(trajectory.get("replayable")):
            return None
        if not _operation_items(trajectory.get("successful_operations")):
            return None
        task_type = str((trajectory.get("task") or {}).get("type") or "").strip().lower()
        if task_type == "desktop_task":
            # GUI learning belongs to the model-gated memory-repo loop, which
            # judges whether a trajectory actually demonstrates the task before
            # promoting it. Memorizing GUI runs here, ungated, stored blind
            # click sequences (e.g. clicks on a window that happened to be
            # open) that exact-query matching then replayed on later requests.
            # This library keeps only deterministic command/file runs.
            return None

        try:
            return self.consolidated_memory_library.save_from_trajectory(
                trajectory,
                source_trajectory_path=trajectory_path,
                memory_id=_auto_memory_id(trajectory),
                tags=["auto-consolidated"],
            )
        except (ConsolidatedMemoryLibraryError, OSError, ValueError):
            # Learning is best-effort: never fail a completed run because its
            # memory could not be written.
            return None

    def find_latest_successful_match(self, user_query: str) -> Optional[ReplayMatch]:
        replay_key = normalize_replay_key(user_query)
        if not replay_key:
            return None

        self.backfill_from_logs()

        consolidated_match = self._find_latest_consolidated_memory_match(user_query)
        if consolidated_match is not None:
            return consolidated_match

        best_match: Optional[ReplayMatch] = None
        for trajectory_path in sorted(self.trajectory_directory.glob(f"{TRAJECTORY_FILE_PREFIX}*.json")):
            match = self._load_trajectory_match(trajectory_path, replay_key)
            if match is None:
                continue
            if best_match is None or _parse_timestamp(match.matched_at) > _parse_timestamp(
                best_match.matched_at
            ):
                best_match = match

        if best_match is not None:
            return best_match

        best_match = self._find_latest_adaptive_consolidated_arxiv_match(user_query)
        if best_match is not None:
            return best_match

        best_match = self._find_latest_adaptive_consolidated_clock_timer_match(user_query)
        if best_match is not None:
            return best_match

        best_match = self._find_latest_adaptive_consolidated_weather_location_match(user_query)
        if best_match is not None:
            return best_match

        best_match = self._find_latest_adaptive_arxiv_match(user_query)
        if best_match is not None:
            return best_match

        if not self.root_directory.exists():
            return None

        for detail_path in sorted(self.root_directory.glob("session_*/interaction-*.json")):
            match = self._load_match(detail_path, replay_key)
            if match is None:
                continue
            if best_match is None or _parse_timestamp(match.matched_at) > _parse_timestamp(
                best_match.matched_at
                ):
                best_match = match
        return best_match

    def load_short_term_memory_snapshot(self, *, limit: int = 10) -> Dict[str, Any]:
        normalized_limit = max(_coerce_int(limit, default=10), 0)
        self.backfill_from_logs()
        self._materialize_existing_trajectory_assets()

        summaries: List[Dict[str, Any]] = []
        for trajectory_path in sorted(self.trajectory_directory.glob(f"{TRAJECTORY_FILE_PREFIX}*.json")):
            try:
                payload = json.loads(trajectory_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping):
                continue
            if payload.get("record_type") != "action_trajectory":
                continue
            summaries.append(self._trajectory_metadata(payload, trajectory_path))

        summaries.sort(
            key=lambda item: _parse_timestamp(str(item.get("matched_at", "")).strip())
        )
        if normalized_limit:
            summaries = summaries[-normalized_limit:]
        else:
            summaries = []

        return {
            "known_action_trajectories": [_json_clone(item) for item in summaries],
            "last_recorded_action_trajectory": (
                _json_clone(summaries[-1]) if summaries else None
            ),
            **self.consolidated_memory_library.load_snapshot(limit=normalized_limit),
        }

    def _find_latest_consolidated_memory_match(self, user_query: str) -> Optional[ReplayMatch]:
        record = self.consolidated_memory_library.find_latest_successful_match(user_query)
        if record is None:
            return None
        return _demote_match_for_incompatible_platform(
            record, _replay_match_from_consolidated_memory_record(record)
        )

    def _find_latest_adaptive_consolidated_arxiv_match(
        self,
        user_query: str,
    ) -> Optional[ReplayMatch]:
        records = self.consolidated_memory_library.list_generalized_records(
            kind=ARXIV_PAPER_DOWNLOAD_GENERALIZATION_KIND,
        )
        if not records:
            return None

        request = _resolve_arxiv_replay_request_from_consolidated_records(
            user_query,
            records,
        )
        if request is None:
            request = _resolve_arxiv_replay_request(user_query)
        if request is None:
            return None

        for record in reversed(records):
            match = _demote_match_for_incompatible_platform(
                record,
                _adaptive_arxiv_match_from_consolidated_memory_record(record, request),
            )
            if match is not None:
                return match
        return None

    def _find_latest_adaptive_consolidated_clock_timer_match(
        self,
        user_query: str,
    ) -> Optional[ReplayMatch]:
        request = _resolve_clock_timer_replay_request(user_query)
        if request is None:
            return None

        records = self.consolidated_memory_library.list_generalized_records(
            kind=CLOCK_TIMER_GENERALIZATION_KIND,
        )
        if not records:
            return None

        requested_direct_replay = bool(request.get("direct_replay"))
        for record in reversed(records):
            replay_task = _mapping_copy(record.get("replay_task"))
            payload = _mapping_copy(replay_task.get("payload"))
            is_direct_record = (
                str(payload.get("replay_mode", "")).strip().lower() == "direct_replay"
                or str(record.get("memory_id", "")).startswith("local-demo-direct-")
            )
            if is_direct_record != requested_direct_replay:
                continue
            match = _demote_match_for_incompatible_platform(
                record,
                _adaptive_clock_timer_match_from_consolidated_memory_record(
                    record,
                    request,
                ),
            )
            if match is not None:
                return match
        return None

    def _find_latest_adaptive_consolidated_weather_location_match(
        self,
        user_query: str,
    ) -> Optional[ReplayMatch]:
        request = _resolve_weather_location_replay_request(user_query)
        if request is None:
            return None

        records = self.consolidated_memory_library.list_generalized_records(
            kind=WEATHER_LOCATION_GENERALIZATION_KIND,
        )
        if not records:
            return None

        requested_direct_replay = bool(request.get("direct_replay"))
        for record in reversed(records):
            replay_task = _mapping_copy(record.get("replay_task"))
            payload = _mapping_copy(replay_task.get("payload"))
            is_direct_record = (
                str(payload.get("replay_mode", "")).strip().lower() == "direct_replay"
                or str(record.get("memory_id", "")).startswith("local-demo-direct-")
            )
            if is_direct_record != requested_direct_replay:
                continue
            match = _demote_match_for_incompatible_platform(
                record,
                _adaptive_weather_location_match_from_consolidated_memory_record(
                    record,
                    request,
                ),
            )
            if match is not None:
                return match
        return None

    def _load_trajectory_match(
        self,
        trajectory_path: Path,
        replay_key: str,
    ) -> Optional[ReplayMatch]:
        try:
            payload = json.loads(trajectory_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, Mapping):
            return None
        if payload.get("record_type") != "action_trajectory":
            return None
        if _is_ungated_desktop_replay(payload.get("replay_task")):
            return None
        if not bool(payload.get("replayable")):
            return None
        match_keys = payload.get("match_keys")
        normalized_match_keys = [
            normalize_replay_key(item)
            for item in match_keys
            if str(item or "").strip()
        ] if isinstance(match_keys, list) else []
        if not normalized_match_keys:
            normalized_match_keys = _build_trajectory_match_keys(
                user_query=str(payload.get("user_query", "")).strip(),
                purpose_summary=str(payload.get("purpose_summary", "")).strip(),
                goal_coverage=str(payload.get("goal_coverage", "")).strip() or "full",
            )
        if replay_key not in normalized_match_keys:
            return None

        replay_task = payload.get("replay_task")
        if not isinstance(replay_task, Mapping):
            return None

        successful_operations = payload.get("successful_operations")
        if isinstance(successful_operations, list):
            operations = [dict(item) for item in successful_operations if isinstance(item, Mapping)]
        else:
            operations = _derive_operations_from_logged_task(replay_task)

        steps = payload.get("steps")
        trajectory_steps = [dict(item) for item in steps if isinstance(item, Mapping)] if isinstance(steps, list) else []
        source_detail_path = payload.get("source_detail_path")
        purpose_summary = str(payload.get("purpose_summary", "")).strip()
        matched_on = "purpose_summary"
        if replay_key == normalize_replay_key(str(payload.get("user_query", ""))):
            matched_on = "user_query"
        detail_path = (
            Path(str(source_detail_path)).resolve()
            if str(source_detail_path or "").strip()
            else trajectory_path.resolve()
        )
        return ReplayMatch(
            session_id=str(payload.get("session_id", "")).strip(),
            task_id=str(payload.get("task_id", "")).strip(),
            user_query=str(payload.get("user_query", "")).strip(),
            matched_at=str(payload.get("matched_at", "")).strip() or datetime.now(timezone.utc).isoformat(),
            detail_path=detail_path,
            replay_task=_json_clone(replay_task),
            successful_operations=_json_clone(operations),
            trajectory_path=trajectory_path.resolve(),
            trajectory_steps=_json_clone(trajectory_steps),
            trajectory_status=str(payload.get("status", "")).strip(),
            purpose_summary=purpose_summary,
            goal_coverage=str(payload.get("goal_coverage", "")).strip(),
            matched_on=matched_on,
        )

    def _find_latest_adaptive_arxiv_match(self, user_query: str) -> Optional[ReplayMatch]:
        request = _resolve_arxiv_replay_request(user_query)
        if request is None:
            return None

        best_match: Optional[ReplayMatch] = None
        for trajectory_path in sorted(self.trajectory_directory.glob(f"{TRAJECTORY_FILE_PREFIX}*.json")):
            match = self._load_adaptive_arxiv_trajectory_match(trajectory_path, request)
            if match is None:
                continue
            if best_match is None or _parse_timestamp(match.matched_at) > _parse_timestamp(
                best_match.matched_at
            ):
                best_match = match
        return best_match

    def _load_adaptive_arxiv_trajectory_match(
        self,
        trajectory_path: Path,
        request: Mapping[str, Any],
    ) -> Optional[ReplayMatch]:
        try:
            payload = json.loads(trajectory_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, Mapping):
            return None
        if payload.get("record_type") != "action_trajectory":
            return None
        if not bool(payload.get("replayable")):
            return None
        if str(payload.get("goal_coverage", "")).strip().lower() != "full":
            return None
        if str(payload.get("status", "")).strip().lower() != "completed":
            return None

        replay_task = payload.get("replay_task")
        if not isinstance(replay_task, Mapping):
            return None
        if str(replay_task.get("type", "")).strip().lower() != "desktop_task":
            return None

        arxiv_template = _detect_arxiv_pdf_replay_template(replay_task)
        if arxiv_template is None:
            return None

        requested_id = str(request.get("arxiv_id", "")).strip()
        if not requested_id:
            return None
        old_id = str(arxiv_template.get("arxiv_id", "")).strip()
        if requested_id == old_id:
            return None

        requested_title = str(request.get("title", "")).strip() or f"arXiv:{requested_id}"
        adapted_replay_task = _adapt_arxiv_replay_task(
            replay_task,
            old_id=old_id,
            new_id=requested_id,
            old_title=str(arxiv_template.get("title", "")).strip(),
            new_title=requested_title,
        )
        actions = _actions_from_payload(_mapping_copy(adapted_replay_task.get("payload")))
        if not actions:
            return None

        steps = payload.get("steps")
        trajectory_steps = (
            _adapt_arxiv_steps(
                [dict(item) for item in steps if isinstance(item, Mapping)],
                old_id=old_id,
                new_id=requested_id,
                old_title=str(arxiv_template.get("title", "")).strip(),
                new_title=requested_title,
            )
            if isinstance(steps, list)
            else []
        )
        source_detail_path = payload.get("source_detail_path")
        detail_path = (
            Path(str(source_detail_path)).resolve()
            if str(source_detail_path or "").strip()
            else trajectory_path.resolve()
        )
        purpose_summary = f"Download {requested_title} from arXiv via adapted compact PDF route"
        return ReplayMatch(
            session_id=str(payload.get("session_id", "")).strip(),
            task_id=str(payload.get("task_id", "")).strip(),
            user_query=str(request.get("user_query", "")).strip(),
            matched_at=str(payload.get("matched_at", "")).strip() or datetime.now(timezone.utc).isoformat(),
            detail_path=detail_path,
            replay_task=_json_clone(adapted_replay_task),
            successful_operations=_json_clone(actions),
            trajectory_path=trajectory_path.resolve(),
            trajectory_steps=_json_clone(trajectory_steps),
            trajectory_status=str(payload.get("status", "")).strip(),
            purpose_summary=purpose_summary,
            goal_coverage=str(payload.get("goal_coverage", "")).strip(),
            matched_on="adaptive_arxiv_pdf",
            adaptation={
                "type": "arxiv_pdf_url_substitution",
                "source_arxiv_id": old_id,
                "target_arxiv_id": requested_id,
                "target_title": requested_title,
                "resolver": request.get("resolver"),
            },
        )

    def _load_match(self, detail_path: Path, replay_key: str) -> Optional[ReplayMatch]:
        try:
            payload = json.loads(detail_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, Mapping):
            return None
        if normalize_replay_key(str(payload.get("user_query", ""))) != replay_key:
            return None

        replay_task = derive_replay_task_from_logged_detail(payload)
        if replay_task is None:
            return None
        if _is_ungated_desktop_replay(replay_task):
            return None

        successful_operations = payload.get("successful_operations")
        if isinstance(successful_operations, list):
            operations = [dict(item) for item in successful_operations if isinstance(item, Mapping)]
        else:
            operations = _derive_operations_from_logged_task(replay_task)

        return ReplayMatch(
            session_id=str(payload.get("session_id", "")).strip(),
            task_id=str(payload.get("task_id", "")).strip(),
            user_query=str(payload.get("user_query", "")).strip(),
            matched_at=_extract_finished_at(payload),
            detail_path=detail_path.resolve(),
            replay_task=_json_clone(replay_task),
            successful_operations=_json_clone(operations),
        )

    def _trajectory_path(self, *, session_id: str, task_id: str) -> Path:
        safe_session_id = _sanitize_filename_component(session_id or "session_unknown")
        safe_task_id = _sanitize_filename_component(task_id or "task_unknown")
        return self.trajectory_directory / (
            f"{TRAJECTORY_FILE_PREFIX}{safe_session_id}-{safe_task_id}.json"
        )

    def _trajectory_asset_directory(self, trajectory_path: Path) -> Path:
        return trajectory_path.parent / f"{trajectory_path.stem}{TRAJECTORY_ASSET_DIRECTORY_SUFFIX}"

    def _prepare_trajectory_for_storage(
        self,
        trajectory: Mapping[str, Any],
        trajectory_path: Path,
    ) -> Dict[str, Any]:
        stored_trajectory = _json_clone(dict(trajectory))
        if stored_trajectory.get("record_type") != "action_trajectory":
            return stored_trajectory

        asset_directory, copied_count = self._materialize_trajectory_screenshots(
            stored_trajectory,
            trajectory_path,
        )
        stored_trajectory["asset_directory"] = (
            str(asset_directory.resolve()) if asset_directory is not None else None
        )
        stored_trajectory["stored_screenshot_count"] = copied_count
        return stored_trajectory

    def _materialize_existing_trajectory_assets(self) -> None:
        if not self.trajectory_directory.exists():
            return

        for trajectory_path in sorted(self.trajectory_directory.glob(f"{TRAJECTORY_FILE_PREFIX}*.json")):
            try:
                payload = json.loads(trajectory_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping):
                continue
            stored_trajectory = self._prepare_trajectory_for_storage(payload, trajectory_path)
            self._write_json(trajectory_path, stored_trajectory)

    def _materialize_trajectory_screenshots(
        self,
        trajectory: Dict[str, Any],
        trajectory_path: Path,
    ) -> tuple[Optional[Path], int]:
        steps = trajectory.get("steps")
        if not isinstance(steps, list):
            return None, 0

        asset_directory = self._trajectory_asset_directory(trajectory_path)
        copied_paths: Dict[str, Path] = {}
        stored_paths: List[str] = []
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, Mapping):
                continue
            normalized_step = dict(step)
            for phase in ("before_observation", "after_observation"):
                observation = normalized_step.get(phase)
                if not isinstance(observation, Mapping):
                    continue
                materialized = self._materialize_observation_snapshot(
                    observation,
                    asset_directory=asset_directory,
                    step_index=index,
                    phase=phase,
                    copied_paths=copied_paths,
                )
                normalized_step[phase] = materialized
                stored_path = str(materialized.get("path", "")).strip()
                if (
                    stored_path
                    and _path_within_directory(_resolve_path_soft(stored_path), asset_directory)
                    and stored_path not in stored_paths
                ):
                    stored_paths.append(stored_path)
            steps[index - 1] = normalized_step
        return (
            asset_directory if stored_paths else None,
            len(stored_paths),
        )

    def _materialize_observation_snapshot(
        self,
        observation: Mapping[str, Any],
        *,
        asset_directory: Path,
        step_index: int,
        phase: str,
        copied_paths: Dict[str, Path],
    ) -> Dict[str, Any]:
        snapshot = _json_clone(dict(observation))
        path = str(snapshot.get("path", "")).strip()
        if not path:
            return snapshot

        resolved_current_path = _resolve_path_soft(path)
        if _path_within_directory(resolved_current_path, asset_directory) and resolved_current_path.exists():
            return snapshot

        copied_path = self._copy_trajectory_screenshot(
            path,
            asset_directory=asset_directory,
            step_index=step_index,
            phase=phase,
            copied_paths=copied_paths,
        )
        if copied_path is None:
            return snapshot

        snapshot["source_path"] = path
        snapshot["path"] = str(copied_path.resolve())
        snapshot["exists"] = copied_path.exists()
        try:
            snapshot["size_bytes"] = copied_path.stat().st_size
        except OSError:
            pass
        return snapshot

    def _copy_trajectory_screenshot(
        self,
        source_path: str,
        *,
        asset_directory: Path,
        step_index: int,
        phase: str,
        copied_paths: Dict[str, Path],
    ) -> Optional[Path]:
        normalized_source = _resolve_path_soft(source_path)
        if _path_within_directory(normalized_source, asset_directory) and normalized_source.exists():
            return normalized_source

        source_key = str(normalized_source)
        existing_target = copied_paths.get(source_key)
        if existing_target is not None and existing_target.exists():
            return existing_target

        if not normalized_source.exists() or not normalized_source.is_file():
            return None

        suffix = normalized_source.suffix or ".png"
        phase_label = "before" if phase == "before_observation" else "after"
        target_path = asset_directory / f"step-{step_index:03d}-{phase_label}{suffix}"
        asset_directory.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(normalized_source, target_path)
        except OSError:
            # A screenshot locked by another process (e.g. a sync client still
            # holding a freshly written PNG on Windows) must not fail trajectory
            # persistence -- or retrieval, which backfills on lookup. Keep the
            # original path; a later upsert retries the copy.
            return None
        copied_paths[source_key] = target_path.resolve()
        return copied_paths[source_key]

    def _trajectory_metadata(
        self,
        trajectory: Mapping[str, Any],
        trajectory_path: Path,
    ) -> Dict[str, Any]:
        successful_operations = trajectory.get("successful_operations")
        steps = trajectory.get("steps")
        task_record = trajectory.get("task")
        return {
            "trajectory_id": str(trajectory.get("trajectory_id", "")).strip() or None,
            "path": str(trajectory_path.resolve()),
            "asset_directory": _optional_text(trajectory.get("asset_directory")),
            "replay_key": str(trajectory.get("replay_key", "")).strip() or None,
            "replayable": bool(trajectory.get("replayable")),
            "status": str(trajectory.get("status", "")).strip() or None,
            "goal_coverage": str(trajectory.get("goal_coverage", "")).strip() or None,
            "purpose_summary": str(trajectory.get("purpose_summary", "")).strip() or None,
            "source_session_id": str(trajectory.get("session_id", "")).strip() or None,
            "source_task_id": str(trajectory.get("task_id", "")).strip() or None,
            "matched_user_query": str(trajectory.get("user_query", "")).strip() or None,
            "matched_at": str(trajectory.get("matched_at", "")).strip() or None,
            "trajectory_steps_preview": (
                _trajectory_step_previews(steps)
                if isinstance(steps, list)
                else []
            ),
            "task_type": (
                str(task_record.get("type", "")).strip()
                if isinstance(task_record, Mapping)
                else None
            ),
            "task_summary": (
                str(task_record.get("summary", "")).strip()
                if isinstance(task_record, Mapping)
                else None
            ),
            "stored_screenshot_count": _coerce_int(
                trajectory.get("stored_screenshot_count"),
                default=0,
            ),
            "successful_operation_count": len(successful_operations)
            if isinstance(successful_operations, list)
            else 0,
            "step_count": len(steps) if isinstance(steps, list) else 0,
        }

    def _write_json(self, path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )


def _replay_match_from_consolidated_memory_record(
    record: Mapping[str, Any],
) -> Optional[ReplayMatch]:
    replay_task = record.get("replay_task")
    if not isinstance(replay_task, Mapping):
        return None
    operations = record.get("consolidated_operations")
    if not isinstance(operations, list):
        operations = record.get("successful_operations")
    successful_operations = [
        dict(item) for item in operations if isinstance(item, Mapping)
    ] if isinstance(operations, list) else []
    if not successful_operations:
        return None

    source = _mapping_copy(record.get("source"))
    steps = record.get("steps")
    trajectory_steps = [
        dict(item) for item in steps if isinstance(item, Mapping)
    ] if isinstance(steps, list) else []
    record_path_text = str(record.get("path") or "").strip()
    if not record_path_text:
        return None
    record_path = Path(record_path_text).expanduser()
    source_detail_path = str(source.get("source_detail_path") or "").strip()
    detail_path = (
        Path(source_detail_path).expanduser().resolve()
        if source_detail_path
        else record_path.resolve()
    )
    memory_id = str(record.get("memory_id", "")).strip()
    source_task_id = str(source.get("task_id") or memory_id).strip()
    return ReplayMatch(
        session_id=str(source.get("session_id", "")).strip(),
        task_id=source_task_id,
        user_query=str(record.get("user_query", "")).strip(),
        matched_at=str(record.get("updated_at") or record.get("created_at") or "").strip()
        or datetime.now(timezone.utc).isoformat(),
        detail_path=detail_path,
        replay_task=_json_clone(dict(replay_task)),
        successful_operations=_json_clone(successful_operations),
        trajectory_path=record_path.resolve(),
        trajectory_steps=_json_clone(trajectory_steps),
        trajectory_status=str(record.get("status", "")).strip(),
        purpose_summary=str(record.get("purpose_summary", "")).strip(),
        goal_coverage=str(record.get("goal_coverage", "full")).strip() or "full",
        matched_on="consolidated_memory",
        adaptation={
            "type": "consolidated_memory",
            "memory_id": memory_id,
            "source_trajectory_path": source.get("trajectory_path"),
        },
    )


def _adaptive_arxiv_match_from_consolidated_memory_record(
    record: Mapping[str, Any],
    request: Mapping[str, Any],
) -> Optional[ReplayMatch]:
    replay_task = record.get("replay_task")
    if not isinstance(replay_task, Mapping):
        return None
    if str(replay_task.get("type", "")).strip().lower() != "desktop_task":
        return None

    arxiv_template = _detect_arxiv_pdf_replay_template(replay_task)
    generalization = _mapping_copy(record.get("generalization"))
    canonical = _mapping_copy(generalization.get("canonical"))
    old_id = str((arxiv_template or {}).get("arxiv_id") or canonical.get("arxiv_id") or "").strip()
    requested_id = str(request.get("arxiv_id", "")).strip()
    if not old_id or not requested_id:
        return None

    old_title = str(
        (arxiv_template or {}).get("title")
        or canonical.get("paper_title")
        or ""
    ).strip()
    requested_title = str(request.get("title", "")).strip() or f"arXiv:{requested_id}"
    adapted_replay_task = _adapt_arxiv_replay_task(
        replay_task,
        old_id=old_id,
        new_id=requested_id,
        old_title=old_title,
        new_title=requested_title,
    )
    actions = _actions_from_payload(_mapping_copy(adapted_replay_task.get("payload")))
    if not actions:
        return None

    steps = record.get("steps")
    trajectory_steps = (
        _adapt_arxiv_steps(
            [dict(item) for item in steps if isinstance(item, Mapping)],
            old_id=old_id,
            new_id=requested_id,
            old_title=old_title,
            new_title=requested_title,
        )
        if isinstance(steps, list)
        else []
    )
    record_path_text = str(record.get("path") or "").strip()
    if not record_path_text:
        return None
    record_path = Path(record_path_text).expanduser().resolve()
    source = _mapping_copy(record.get("source"))
    source_detail_path = str(source.get("source_detail_path") or "").strip()
    detail_path = (
        Path(source_detail_path).expanduser().resolve()
        if source_detail_path
        else record_path
    )
    memory_id = str(record.get("memory_id", "")).strip()
    return ReplayMatch(
        session_id=str(source.get("session_id", "")).strip(),
        task_id=str(source.get("task_id") or memory_id).strip(),
        user_query=str(request.get("user_query", "")).strip(),
        matched_at=str(record.get("updated_at") or record.get("created_at") or "").strip()
        or datetime.now(timezone.utc).isoformat(),
        detail_path=detail_path,
        replay_task=_json_clone(adapted_replay_task),
        successful_operations=_json_clone(actions),
        trajectory_path=record_path,
        trajectory_steps=_json_clone(trajectory_steps),
        trajectory_status=str(record.get("status", "")).strip(),
        purpose_summary=ARXIV_PAPER_DOWNLOAD_PURPOSE_SUMMARY,
        goal_coverage=str(record.get("goal_coverage", "full")).strip() or "full",
        matched_on=f"consolidated_memory:{ARXIV_PAPER_DOWNLOAD_GENERALIZATION_KIND}",
        adaptation={
            "type": "consolidated_memory_arxiv_binding",
            "memory_id": memory_id,
            "source_trajectory_path": source.get("trajectory_path"),
            "source_arxiv_id": old_id,
            "source_title": old_title or None,
            "target_arxiv_id": requested_id,
            "target_title": requested_title,
            "resolver": request.get("resolver"),
        },
    )


def _adaptive_clock_timer_match_from_consolidated_memory_record(
    record: Mapping[str, Any],
    request: Mapping[str, Any],
) -> Optional[ReplayMatch]:
    replay_task = record.get("replay_task")
    if not isinstance(replay_task, Mapping):
        return None
    if str(replay_task.get("type", "")).strip().lower() != "desktop_task":
        return None

    adapted_replay_task = _adapt_clock_timer_replay_task(replay_task, request)
    actions = _actions_from_payload(_mapping_copy(adapted_replay_task.get("payload")))
    if not actions:
        return None

    steps = record.get("steps")
    trajectory_steps = (
        _adapt_clock_timer_steps(
            [dict(item) for item in steps if isinstance(item, Mapping)],
            request,
        )
        if isinstance(steps, list)
        else []
    )
    record_path_text = str(record.get("path") or "").strip()
    if not record_path_text:
        return None
    record_path = Path(record_path_text).expanduser().resolve()
    source = _mapping_copy(record.get("source"))
    source_detail_path = str(source.get("source_detail_path") or "").strip()
    detail_path = (
        Path(source_detail_path).expanduser().resolve()
        if source_detail_path
        else record_path
    )
    memory_id = str(record.get("memory_id", "")).strip()
    display_duration = str(request.get("timer_display") or "").strip()
    return ReplayMatch(
        session_id=str(source.get("session_id", "")).strip(),
        task_id=str(source.get("task_id") or memory_id).strip(),
        user_query=str(request.get("user_query", "")).strip(),
        matched_at=str(record.get("updated_at") or record.get("created_at") or "").strip()
        or datetime.now(timezone.utc).isoformat(),
        detail_path=detail_path,
        replay_task=_json_clone(adapted_replay_task),
        successful_operations=_json_clone(actions),
        trajectory_path=record_path,
        trajectory_steps=_json_clone(trajectory_steps),
        trajectory_status=str(record.get("status", "")).strip(),
        purpose_summary=(
            f"Set a Clock timer for {display_duration} and close Clock after it completes."
        ),
        goal_coverage=str(record.get("goal_coverage", "full")).strip() or "full",
        matched_on=f"consolidated_memory:{CLOCK_TIMER_GENERALIZATION_KIND}",
        adaptation={
            "type": "consolidated_memory_clock_timer_binding",
            "memory_id": memory_id,
            "timer_hours": int(request.get("hours") or 0),
            "timer_minutes": int(request.get("minutes") or 0),
            "timer_seconds": int(request.get("seconds") or 0),
            "timer_total_seconds": int(request.get("total_seconds") or 0),
            "timer_display": display_duration,
            "timer_completion_wait_seconds": float(
                request.get("timer_completion_wait_seconds") or 0.0
            ),
            "source_trajectory_path": source.get("trajectory_path"),
        },
    )


def _adapt_clock_timer_replay_task(
    replay_task: Mapping[str, Any],
    request: Mapping[str, Any],
) -> Dict[str, Any]:
    adapted = _json_clone(dict(replay_task))
    payload = _mapping_copy(adapted.get("payload"))
    payload["actions"] = _replace_spotlight_launcher([
        _adapt_clock_timer_action(action, request)
        for action in _actions_from_payload(payload)
    ])
    payload["timer_hours"] = int(request.get("hours") or 0)
    payload["timer_minutes"] = int(request.get("minutes") or 0)
    payload["timer_seconds"] = int(request.get("seconds") or 0)
    payload["timer_total_seconds"] = int(request.get("total_seconds") or 0)
    payload["timer_display"] = str(request.get("timer_display") or "")
    payload["timer_completion_wait_seconds"] = float(
        request.get("timer_completion_wait_seconds") or 0.0
    )
    adapted["goal"] = str(request.get("stripped_user_query") or request.get("user_query") or "")
    adapted["payload"] = payload
    return adapted


def _adapt_clock_timer_steps(
    steps: Sequence[Mapping[str, Any]],
    request: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    adapted_steps: List[Dict[str, Any]] = []
    for step in steps:
        adapted_step = _json_clone(dict(step))
        action = adapted_step.get("action")
        if isinstance(action, Mapping):
            adapted_step["action"] = _adapt_clock_timer_action(action, request)
        adapted_steps.append(adapted_step)
    # The public Clock example originally launched through Spotlight.  That
    # shortcut is global, can be reassigned, and may be consumed by the host
    # app before it reaches macOS.  Keep old memories useful while replacing
    # their launcher with the native macOS app activation primitive.
    if adapted_steps:
        first = adapted_steps[0].get("action")
        if _is_spotlight_launcher(first):
            replacement = _json_clone(adapted_steps[0])
            replacement["action"] = {
                "action": "activate_app",
                "app_name": "Clock",
                "expected_outcome": "Clock opens or comes to the front.",
            }
            return [replacement, *adapted_steps[5:]]
    return adapted_steps


def _is_spotlight_launcher(action: Any) -> bool:
    if not isinstance(action, Mapping):
        return False
    if str(action.get("action") or "").strip().lower() != "hotkey":
        return False
    keys = action.get("keys")
    if not isinstance(keys, Sequence) or isinstance(keys, (str, bytes)):
        return False
    return {str(key).strip().lower() for key in keys} == {"command", "space"}


def _replace_spotlight_launcher(actions: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Upgrade legacy Clock replays without rewriting user memory files."""
    copied = [_json_clone(dict(action)) for action in actions]
    if not copied or not _is_spotlight_launcher(copied[0]):
        return copied
    return [{
        "action": "activate_app",
        "app_name": "Clock",
        "expected_outcome": "Clock opens or comes to the front.",
    }, *copied[5:]]


def _adapt_clock_timer_action(
    action: Mapping[str, Any],
    request: Mapping[str, Any],
) -> Dict[str, Any]:
    adapted = _json_clone(dict(action))
    dynamic_input = _mapping_copy(adapted.get("dynamic_input"))
    input_name = str(dynamic_input.get("name") or "").strip()
    if input_name == "timer_hours":
        adapted["text"] = str(request.get("input_hours") or "00")
    elif input_name == "timer_minutes":
        adapted["text"] = str(request.get("input_minutes") or "00")
    elif input_name == "timer_seconds":
        adapted["text"] = str(request.get("input_seconds") or "00")

    dynamic_wait = _mapping_copy(adapted.get("dynamic_wait"))
    wait_name = str(dynamic_wait.get("name") or "").strip()
    if wait_name == "timer_completion_wait_seconds":
        adapted["seconds"] = float(request.get("timer_completion_wait_seconds") or 0.0)
    return adapted


def _adaptive_weather_location_match_from_consolidated_memory_record(
    record: Mapping[str, Any],
    request: Mapping[str, Any],
) -> Optional[ReplayMatch]:
    replay_task = record.get("replay_task")
    if not isinstance(replay_task, Mapping):
        return None
    if str(replay_task.get("type", "")).strip().lower() != "desktop_task":
        return None

    adapted_replay_task = _adapt_weather_location_replay_task(replay_task, request)
    actions = _actions_from_payload(_mapping_copy(adapted_replay_task.get("payload")))
    if not actions:
        return None

    steps = record.get("steps")
    trajectory_steps = (
        _adapt_weather_location_steps(
            [dict(item) for item in steps if isinstance(item, Mapping)],
            request,
        )
        if isinstance(steps, list)
        else []
    )
    record_path_text = str(record.get("path") or "").strip()
    if not record_path_text:
        return None
    record_path = Path(record_path_text).expanduser().resolve()
    source = _mapping_copy(record.get("source"))
    source_detail_path = str(source.get("source_detail_path") or "").strip()
    detail_path = (
        Path(source_detail_path).expanduser().resolve()
        if source_detail_path
        else record_path
    )
    memory_id = str(record.get("memory_id", "")).strip()
    weather_location = str(request.get("weather_location") or "").strip()
    return ReplayMatch(
        session_id=str(source.get("session_id", "")).strip(),
        task_id=str(source.get("task_id") or memory_id).strip(),
        user_query=str(request.get("user_query", "")).strip(),
        matched_at=str(record.get("updated_at") or record.get("created_at") or "").strip()
        or datetime.now(timezone.utc).isoformat(),
        detail_path=detail_path,
        replay_task=_json_clone(adapted_replay_task),
        successful_operations=_json_clone(actions),
        trajectory_path=record_path,
        trajectory_steps=_json_clone(trajectory_steps),
        trajectory_status=str(record.get("status", "")).strip(),
        purpose_summary=f"Show the Weather forecast for {weather_location} and close Weather.",
        goal_coverage=str(record.get("goal_coverage", "full")).strip() or "full",
        matched_on=f"consolidated_memory:{WEATHER_LOCATION_GENERALIZATION_KIND}",
        adaptation={
            "type": "consolidated_memory_weather_location_binding",
            "memory_id": memory_id,
            "weather_location": weather_location,
            "source_trajectory_path": source.get("trajectory_path"),
        },
    )


def _adapt_weather_location_replay_task(
    replay_task: Mapping[str, Any],
    request: Mapping[str, Any],
) -> Dict[str, Any]:
    adapted = _json_clone(dict(replay_task))
    payload = _mapping_copy(adapted.get("payload"))
    payload["actions"] = [
        _adapt_weather_location_action(action, request)
        for action in _actions_from_payload(payload)
    ]
    payload["weather_location"] = str(request.get("weather_location") or "")
    adapted["goal"] = str(request.get("stripped_user_query") or request.get("user_query") or "")
    adapted["summary"] = (
        f"Show Weather forecast for {payload['weather_location']} and close Weather"
    )
    adapted["payload"] = payload
    return adapted


def _adapt_weather_location_steps(
    steps: Sequence[Mapping[str, Any]],
    request: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    adapted_steps: List[Dict[str, Any]] = []
    for step in steps:
        adapted_step = _json_clone(dict(step))
        action = adapted_step.get("action")
        if isinstance(action, Mapping):
            adapted_step["action"] = _adapt_weather_location_action(action, request)
        adapted_steps.append(adapted_step)
    return adapted_steps


def _adapt_weather_location_action(
    action: Mapping[str, Any],
    request: Mapping[str, Any],
) -> Dict[str, Any]:
    adapted = _json_clone(dict(action))
    dynamic_input = _mapping_copy(adapted.get("dynamic_input"))
    input_name = str(dynamic_input.get("name") or "").strip()
    if input_name == "weather_location":
        adapted["text"] = str(request.get("weather_location") or "")
    return adapted


def _resolve_arxiv_replay_request_from_consolidated_records(
    user_query: str,
    records: Sequence[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    query = str(user_query or "").strip()
    if not query:
        return None
    normalized = normalize_replay_key(query)
    if "arxiv" not in normalized or "download" not in normalized:
        return None

    explicit_id = _extract_arxiv_id(query)
    title_candidate = _extract_arxiv_title_candidate(query)
    if explicit_id:
        return {
            "user_query": query,
            "arxiv_id": explicit_id,
            "title": title_candidate or f"arXiv:{explicit_id}",
            "resolver": "query_arxiv_id",
        }

    if not title_candidate:
        return None
    normalized_title = normalize_replay_key(title_candidate)
    for record in reversed(list(records)):
        generalization = _mapping_copy(record.get("generalization"))
        canonical = _mapping_copy(generalization.get("canonical"))
        canonical_title = str(canonical.get("paper_title") or "").strip()
        canonical_id = str(canonical.get("arxiv_id") or "").strip()
        if not canonical_title or not canonical_id:
            continue
        if normalize_replay_key(canonical_title) != normalized_title:
            continue
        return {
            "user_query": query,
            "arxiv_id": canonical_id,
            "title": canonical_title,
            "resolver": "consolidated_memory_canonical_title",
        }
    return None


def _resolve_clock_timer_replay_request(user_query: str) -> Optional[Dict[str, Any]]:
    query = str(user_query or "").strip()
    if not query:
        return None

    stripped_query = query
    normalized = normalize_replay_key(stripped_query)
    direct_replay = False
    if normalized.startswith(CLOCK_TIMER_DIRECT_REPLAY_PREFIX):
        direct_replay = True
        stripped_query = stripped_query.split(":", 1)[1].strip()
        normalized = normalize_replay_key(stripped_query)

    if "clock" not in normalized or "timer" not in normalized:
        return None

    duration = _parse_clock_timer_duration(stripped_query)
    if duration is None:
        return None
    hours, minutes, seconds, total_seconds = duration
    if total_seconds <= 0 or total_seconds > CLOCK_TIMER_MAX_SECONDS:
        return None

    wait_seconds = round(float(total_seconds) + CLOCK_TIMER_CLOSE_BUFFER_SECONDS, 3)
    timer_display = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return {
        "user_query": query,
        "stripped_user_query": stripped_query,
        "direct_replay": direct_replay,
        "hours": hours,
        "minutes": minutes,
        "seconds": seconds,
        "total_seconds": total_seconds,
        "input_hours": f"{hours:02d}",
        "input_minutes": f"{minutes:02d}",
        "input_seconds": f"{seconds:02d}",
        "timer_display": timer_display,
        "timer_completion_wait_seconds": wait_seconds,
    }


def _parse_clock_timer_duration(text: str) -> Optional[tuple[int, int, int, int]]:
    query = str(text or "")
    full_match = re.search(
        r"(?<!\d)(?P<h>\d{1,2})\s*:\s*(?P<m>\d{1,2})\s*:\s*(?P<s>\d{1,2})(?!\d)",
        query,
    )
    if full_match:
        hours = int(full_match.group("h"))
        minutes = int(full_match.group("m"))
        seconds = int(full_match.group("s"))
        if hours <= 23 and minutes <= 59 and seconds <= 59:
            return hours, minutes, seconds, (hours * 3600) + (minutes * 60) + seconds
        return None

    short_match = re.search(
        r"(?<!\d)(?P<m>\d{1,2})\s*:\s*(?P<s>\d{1,2})(?!\d)",
        query,
    )
    if short_match:
        minutes = int(short_match.group("m"))
        seconds = int(short_match.group("s"))
        if minutes <= 59 and seconds <= 59:
            total = (minutes * 60) + seconds
            return 0, minutes, seconds, total
        return None

    hour_value = _first_clock_timer_unit_value(
        query,
        r"(?:hours|hour|hrs|hr|h)",
    )
    minute_value = _first_clock_timer_unit_value(
        query,
        r"(?:minutes|minute|mins|min|m)",
    )
    second_value = _first_clock_timer_unit_value(
        query,
        r"(?:seconds|second|secs|sec|s)",
    )
    if hour_value is None and minute_value is None and second_value is None:
        return None

    total_seconds = (
        int(hour_value or 0) * 3600
        + int(minute_value or 0) * 60
        + int(second_value or 0)
    )
    if total_seconds <= 0 or total_seconds > CLOCK_TIMER_MAX_SECONDS:
        return None
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return hours, minutes, seconds, total_seconds


def _first_clock_timer_unit_value(text: str, unit_pattern: str) -> Optional[int]:
    match = re.search(
        rf"(?<!\d)(?P<value>\d+)\s*-?\s*{unit_pattern}\b",
        str(text or ""),
        re.IGNORECASE,
    )
    if match is None:
        return None
    return int(match.group("value"))


def _resolve_weather_location_replay_request(user_query: str) -> Optional[Dict[str, Any]]:
    query = str(user_query or "").strip()
    if not query:
        return None

    stripped_query = query
    normalized = normalize_replay_key(stripped_query)
    direct_replay = False
    if normalized.startswith(WEATHER_LOCATION_DIRECT_REPLAY_PREFIX):
        direct_replay = True
        stripped_query = stripped_query.split(":", 1)[1].strip()
        normalized = normalize_replay_key(stripped_query)

    if "weather" not in normalized:
        return None

    location = _parse_weather_location(stripped_query)
    if location is None:
        return None

    return {
        "user_query": query,
        "stripped_user_query": stripped_query,
        "direct_replay": direct_replay,
        "weather_location": location,
    }


def _parse_weather_location(text: str) -> Optional[str]:
    query = " ".join(str(text or "").strip().split())
    if not query:
        return None

    stop_clause = (
        r"(?=(?:,\s*|\s+)"
        r"(?:show|open|then|and\s+show|and\s+then|and\s+close|close)\b|[.!?]?$)"
    )
    patterns = [
        rf"\bsearch\s+(?:for\s+)?(?P<location>.+?){stop_clause}",
        rf"\b(?:weather|forecast)\s+(?:for|in)\s+(?P<location>.+?){stop_clause}",
    ]
    for pattern in patterns:
        match = re.search(pattern, query, re.IGNORECASE)
        if match is None:
            continue
        location = _clean_weather_location(match.group("location"))
        if location is not None:
            return location
    return None


def _clean_weather_location(value: str) -> Optional[str]:
    location = str(value or "").strip()
    location = location.strip(" \t\r\n\"'`")
    location = re.sub(r"[.!?;:]+$", "", location).strip()
    if not location or len(location) > WEATHER_LOCATION_MAX_LENGTH:
        return None
    if normalize_replay_key(location) in {"weather", "forecast", "the forecast"}:
        return None
    return location


def _mapping_copy(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _command_operations_from_payload(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    commands = payload.get("commands")
    if isinstance(commands, list):
        return [dict(item) for item in commands if isinstance(item, Mapping)]

    command = str(payload.get("command", "")).strip()
    if not command:
        return []
    return [{"command": command}]


def _actions_from_payload(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    actions = payload.get("actions")
    if not isinstance(actions, list):
        return []
    return [dict(item) for item in actions if isinstance(item, Mapping)]


def _filter_operations_by_results(
    operations: Sequence[Mapping[str, Any]],
    execution_output: Any,
) -> List[Dict[str, Any]]:
    if not isinstance(execution_output, Mapping):
        return []
    results = execution_output.get("results")
    if not isinstance(results, list):
        return []

    successful: List[Dict[str, Any]] = []
    for operation, result in zip(operations, results):
        if not isinstance(operation, Mapping) or not isinstance(result, Mapping):
            continue
        if _result_succeeded(result):
            successful.append(dict(operation))
    return successful


def _desktop_operations_from_execution(
    payload: Mapping[str, Any],
    execution_output: Any,
) -> List[Dict[str, Any]]:
    if not isinstance(execution_output, Mapping):
        return []

    iterations = execution_output.get("iterations")
    if isinstance(iterations, list):
        successful = []
        for iteration in iterations:
            if not isinstance(iteration, Mapping):
                continue
            action = iteration.get("action")
            if not isinstance(action, Mapping) or not _is_replayable_desktop_action(action):
                continue
            action_result = iteration.get("action_result")
            if not isinstance(action_result, Mapping):
                continue
            results = action_result.get("results")
            if not isinstance(results, list):
                continue
            verification = iteration.get("verification")
            if (
                isinstance(verification, Mapping)
                and verification.get("step_succeeded") is not None
                and not bool(verification.get("step_succeeded"))
            ):
                continue
            if any(isinstance(result, Mapping) and _result_succeeded(result) for result in results):
                successful.append(dict(action))
        return successful

    scripted_actions = _actions_from_payload(payload)
    if not scripted_actions:
        return []

    action_result = execution_output.get("action_result")
    if not isinstance(action_result, Mapping):
        return []
    results = action_result.get("results")
    if not isinstance(results, list):
        return []

    successful: List[Dict[str, Any]] = []
    for action, result in zip(scripted_actions, results):
        if not _is_replayable_desktop_action(action):
            continue
        if isinstance(result, Mapping) and _result_succeeded(result):
            successful.append(dict(action))
    return successful


def _steps_from_result_list(
    operations: Sequence[Mapping[str, Any]],
    execution_output: Any,
) -> List[Dict[str, Any]]:
    if not isinstance(execution_output, Mapping):
        return []
    results = execution_output.get("results")
    if not isinstance(results, list):
        return []

    steps: List[Dict[str, Any]] = []
    for index, (operation, result) in enumerate(zip(operations, results), start=1):
        if not isinstance(operation, Mapping) or not isinstance(result, Mapping):
            continue
        if not _result_succeeded(result):
            continue
        steps.append(
            {
                "index": index,
                "action": dict(operation),
                "result": dict(result),
                "successful": True,
            }
        )
    return steps


def _consecutive_steps_from_result_list(
    operations: Sequence[Mapping[str, Any]],
    execution_output: Any,
) -> List[Dict[str, Any]]:
    if not isinstance(execution_output, Mapping):
        return []
    results = execution_output.get("results")
    if not isinstance(results, list):
        return []

    steps: List[Dict[str, Any]] = []
    for index, (operation, result) in enumerate(zip(operations, results), start=1):
        if not isinstance(operation, Mapping) or not isinstance(result, Mapping):
            break
        if not _result_succeeded(result):
            break
        steps.append(
            {
                "index": index,
                "action": dict(operation),
                "result": dict(result),
                "successful": True,
            }
        )
    return steps


def _desktop_steps_from_execution(
    payload: Mapping[str, Any],
    execution_output: Any,
) -> List[Dict[str, Any]]:
    if not isinstance(execution_output, Mapping):
        return []

    iterations = execution_output.get("iterations")
    if not isinstance(iterations, list):
        scripted_actions = _actions_from_payload(payload)
        action_result = execution_output.get("action_result")
        results = action_result.get("results") if isinstance(action_result, Mapping) else None
        if not scripted_actions or not isinstance(results, list):
            return []

        initial_observation = _observation_snapshot(execution_output.get("initial_observation"))
        final_observation = _observation_snapshot(execution_output.get("final_observation"))
        successful_steps: List[Dict[str, Any]] = []
        for index, (action, result) in enumerate(zip(scripted_actions, results), start=1):
            if not isinstance(action, Mapping) or not _is_replayable_desktop_action(action):
                continue
            if not isinstance(result, Mapping) or not _result_succeeded(result):
                continue
            step: Dict[str, Any] = {
                "index": len(successful_steps) + 1,
                "iteration": index,
                "action": dict(action),
                "successful": True,
                "result": dict(result),
            }
            if initial_observation is not None and not successful_steps:
                step["before_observation"] = dict(initial_observation)
            if final_observation is not None and index == len(scripted_actions):
                step["after_observation"] = dict(final_observation)
            successful_steps.append(step)
        return successful_steps

    steps: List[Dict[str, Any]] = []
    for iteration in iterations:
        if not isinstance(iteration, Mapping):
            continue
        action = iteration.get("action")
        if not isinstance(action, Mapping) or not _is_replayable_desktop_action(action):
            continue
        action_result = iteration.get("action_result")
        if not isinstance(action_result, Mapping):
            continue
        results = action_result.get("results")
        if not isinstance(results, list):
            continue
        if not any(isinstance(result, Mapping) and _result_succeeded(result) for result in results):
            continue
        verification = iteration.get("verification")
        if (
            isinstance(verification, Mapping)
            and verification.get("step_succeeded") is not None
            and not bool(verification.get("step_succeeded"))
        ):
            continue
        step: Dict[str, Any] = {
            "index": len(steps) + 1,
            "iteration": _coerce_int(iteration.get("iteration"), default=len(steps) + 1),
            "action": dict(action),
            "successful": True,
            "before_observation": _observation_snapshot(iteration.get("observation")),
            "after_observation": _observation_snapshot(iteration.get("after_observation")),
        }
        result = _first_successful_result(results)
        if result is not None:
            step["result"] = result
        if isinstance(verification, Mapping):
            step["verification"] = _json_clone(dict(verification))
        steps.append(step)
    return steps


def _completed_desktop_steps_from_execution(
    payload: Mapping[str, Any],
    execution_output: Any,
) -> List[Dict[str, Any]]:
    if not isinstance(execution_output, Mapping):
        return []

    iterations = execution_output.get("iterations")
    if not isinstance(iterations, list):
        return _desktop_steps_from_execution(payload, execution_output)

    steps: List[Dict[str, Any]] = []
    for iteration in iterations:
        if not isinstance(iteration, Mapping):
            continue
        action = iteration.get("action")
        if not isinstance(action, Mapping) or not _is_replayable_desktop_action(action):
            continue
        action_result = iteration.get("action_result")
        if not isinstance(action_result, Mapping):
            continue
        results = action_result.get("results")
        if not isinstance(results, list):
            continue
        if not any(isinstance(result, Mapping) and _result_succeeded(result) for result in results):
            continue
        step: Dict[str, Any] = {
            "index": len(steps) + 1,
            "iteration": _coerce_int(iteration.get("iteration"), default=len(steps) + 1),
            "action": dict(action),
            "successful": True,
            "before_observation": _observation_snapshot(iteration.get("observation")),
            "after_observation": _observation_snapshot(iteration.get("after_observation")),
        }
        result = _first_successful_result(results)
        if result is not None:
            step["result"] = result
        expected_outcome = str(
            iteration.get("expected_outcome")
            or action.get("expected_outcome")
            or ""
        ).strip()
        if expected_outcome:
            step["expected_outcome"] = expected_outcome
        verification = iteration.get("verification")
        if isinstance(verification, Mapping):
            step["verification"] = _json_clone(dict(verification))
        steps.append(step)
    return steps


def _consecutive_desktop_steps_from_execution(
    payload: Mapping[str, Any],
    execution_output: Any,
) -> List[Dict[str, Any]]:
    if not isinstance(execution_output, Mapping):
        return []

    iterations = execution_output.get("iterations")
    if not isinstance(iterations, list):
        scripted_actions = _actions_from_payload(payload)
        action_result = execution_output.get("action_result")
        results = action_result.get("results") if isinstance(action_result, Mapping) else None
        if not scripted_actions or not isinstance(results, list):
            return []

        initial_observation = _observation_snapshot(execution_output.get("initial_observation"))
        final_observation = _observation_snapshot(execution_output.get("final_observation"))
        cleaned_steps: List[Dict[str, Any]] = []
        for index, (action, result) in enumerate(zip(scripted_actions, results), start=1):
            if not isinstance(action, Mapping) or not _is_replayable_desktop_action(action):
                break
            if not isinstance(result, Mapping) or not _result_succeeded(result):
                break
            step: Dict[str, Any] = {
                "index": len(cleaned_steps) + 1,
                "iteration": index,
                "action": dict(action),
                "successful": True,
                "result": dict(result),
            }
            if initial_observation is not None and not cleaned_steps:
                step["before_observation"] = dict(initial_observation)
            if final_observation is not None and index == len(scripted_actions):
                step["after_observation"] = dict(final_observation)
            cleaned_steps.append(step)
        return cleaned_steps

    cleaned_steps: List[Dict[str, Any]] = []
    for iteration in iterations:
        if not isinstance(iteration, Mapping):
            if cleaned_steps:
                break
            continue
        action = iteration.get("action")
        if not isinstance(action, Mapping):
            continue
        if not _is_replayable_desktop_action(action):
            break
        action_result = iteration.get("action_result")
        if not isinstance(action_result, Mapping):
            break
        results = action_result.get("results")
        if not isinstance(results, list):
            break
        if not any(isinstance(result, Mapping) and _result_succeeded(result) for result in results):
            break
        verification = iteration.get("verification")
        if (
            isinstance(verification, Mapping)
            and verification.get("step_succeeded") is not None
            and not bool(verification.get("step_succeeded"))
        ):
            break
        step: Dict[str, Any] = {
            "index": len(cleaned_steps) + 1,
            "iteration": _coerce_int(iteration.get("iteration"), default=len(cleaned_steps) + 1),
            "action": dict(action),
            "successful": True,
            "before_observation": _observation_snapshot(iteration.get("observation")),
            "after_observation": _observation_snapshot(iteration.get("after_observation")),
        }
        result = _first_successful_result(results)
        if result is not None:
            step["result"] = result
        if isinstance(verification, Mapping):
            step["verification"] = _json_clone(dict(verification))
        cleaned_steps.append(step)
    return cleaned_steps


def _result_succeeded(result: Mapping[str, Any]) -> bool:
    status = str(result.get("status", "")).strip().lower()
    if status:
        return status == "ok"

    success = result.get("success")
    if success is not None:
        return bool(success)

    exit_code = result.get("exit_code")
    if exit_code is not None:
        try:
            return int(exit_code) == 0
        except (TypeError, ValueError):
            return False
    return False


def _is_replayable_desktop_action(action: Mapping[str, Any]) -> bool:
    action_name = str(action.get("action", "")).strip().lower()
    return action_name in REPLAYABLE_DESKTOP_ACTIONS


def _logged_execution_succeeded(detail_payload: Mapping[str, Any]) -> bool:
    execution = detail_payload.get("execution")
    if isinstance(execution, Mapping):
        return bool(execution.get("success"))
    result = detail_payload.get("result")
    if isinstance(result, Mapping):
        return bool(result.get("success"))
    return False


def _derive_operations_from_logged_task(task_record: Mapping[str, Any]) -> List[Dict[str, Any]]:
    task_type = str(task_record.get("type", "")).strip().lower()
    payload = _mapping_copy(task_record.get("payload"))
    if task_type == "command_task":
        return _command_operations_from_payload(payload)
    if task_type == "file_task":
        return _actions_from_payload(payload)
    if task_type == "desktop_task":
        return [action for action in _actions_from_payload(payload) if _is_replayable_desktop_action(action)]
    return []


def _extract_finished_at(detail_payload: Mapping[str, Any]) -> str:
    result = detail_payload.get("result")
    if isinstance(result, Mapping):
        finished_at = str(result.get("finished_at", "")).strip()
        if finished_at:
            return finished_at
    return datetime.now(timezone.utc).isoformat()


def _parse_timestamp(value: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _coerce_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _resolve_path_soft(value: str) -> Path:
    return Path(str(value or "")).expanduser().resolve()


def _path_within_directory(candidate: Path, directory: Path) -> bool:
    try:
        candidate.relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def _first_successful_result(results: Sequence[Any]) -> Optional[Dict[str, Any]]:
    for result in results:
        if isinstance(result, Mapping) and _result_succeeded(result):
            return dict(result)
    return None


def _observation_snapshot(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, Mapping):
        return None
    snapshot = _json_clone(dict(value))
    path = str(snapshot.get("path", "")).strip()
    snapshot["path"] = path or None
    return snapshot


def _build_steps_from_logged_detail(
    task_record: Mapping[str, Any],
    detail_payload: Mapping[str, Any],
    successful_operations: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    task_type = str(task_record.get("type", "")).strip().lower()
    operations = [dict(item) for item in successful_operations if isinstance(item, Mapping)]
    if not operations:
        return []
    if task_type != "desktop_task":
        return [
            {
                "index": index,
                "action": dict(operation),
                "successful": True,
            }
            for index, operation in enumerate(operations, start=1)
        ]

    before_observation, after_observations = _logged_observation_index(detail_payload)
    steps: List[Dict[str, Any]] = []
    previous_observation = before_observation
    for index, operation in enumerate(operations, start=1):
        step: Dict[str, Any] = {
            "index": index,
            "iteration": index,
            "action": dict(operation),
            "successful": True,
        }
        if previous_observation is not None:
            step["before_observation"] = dict(previous_observation)
        current_after = after_observations.get(index)
        if current_after is not None:
            step["after_observation"] = dict(current_after)
            previous_observation = current_after
        steps.append(step)
    return steps


def _clean_replay_segment_from_logged_detail(
    task_record: Mapping[str, Any],
    detail_payload: Mapping[str, Any],
    *,
    execution_succeeded: bool = False,
) -> Dict[str, List[Dict[str, Any]]]:
    task_type = str(task_record.get("type", "")).strip().lower()
    if task_type == "desktop_task":
        if execution_succeeded:
            steps = _completed_desktop_steps_from_logged_events(detail_payload)
        else:
            steps = _consecutive_desktop_steps_from_logged_events(detail_payload)
        operations = [
            dict(step.get("action"))
            for step in steps
            if isinstance(step, Mapping) and isinstance(step.get("action"), Mapping)
        ]
        return {"operations": operations, "steps": steps}

    operations = _derive_operations_from_logged_task(task_record)
    executed_actions = detail_payload.get("executed_actions")
    if not isinstance(executed_actions, list):
        return {"operations": [], "steps": []}

    cleaned_operations: List[Dict[str, Any]] = []
    steps: List[Dict[str, Any]] = []
    for index, (operation, executed) in enumerate(zip(operations, executed_actions), start=1):
        if not isinstance(operation, Mapping) or not isinstance(executed, Mapping):
            break
        if not _result_succeeded(executed):
            break
        cleaned_operation = dict(operation)
        cleaned_operations.append(cleaned_operation)
        steps.append(
            {
                "index": index,
                "action": cleaned_operation,
                "result": dict(executed),
                "successful": True,
            }
        )
    return {"operations": cleaned_operations, "steps": steps}


def _consecutive_desktop_steps_from_logged_events(
    detail_payload: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    events = detail_payload.get("events")
    if not isinstance(events, list):
        return []

    iteration_records: Dict[int, Dict[str, Any]] = {}
    initial_observation = _logged_observation_index(detail_payload)[0]
    latest_after_observation = initial_observation
    for event in events:
        if not isinstance(event, Mapping):
            continue
        name = str(event.get("name", "")).strip()
        payload = event.get("payload")
        payload_map = payload if isinstance(payload, Mapping) else {}
        iteration = _coerce_optional_int_logged(payload_map.get("iteration"))
        step_index = _coerce_optional_int_logged(payload_map.get("step_index"))
        record_key = step_index if step_index is not None else iteration

        if name == "agent.desktop.observed":
            observation_phase = str(payload_map.get("observation_phase", "")).strip().lower()
            observation = _observation_snapshot(payload_map)
            if observation_phase == "before" and initial_observation is None:
                initial_observation = observation
            elif observation_phase == "after" and iteration is not None and observation is not None:
                record = iteration_records.setdefault(iteration, {"iteration": iteration})
                if record.get("before_observation") is None and latest_after_observation is not None:
                    record["before_observation"] = dict(latest_after_observation)
                record["after_observation"] = observation
                latest_after_observation = observation
            continue

        if record_key is None:
            continue
        record = iteration_records.setdefault(record_key, {"iteration": iteration or record_key})
        if step_index is not None:
            record["step_index"] = step_index
        if iteration is not None:
            record["replay_action_index"] = iteration
        if name == "agent.desktop.actions.completed":
            action = payload_map.get("action")
            if isinstance(action, Mapping):
                record["action"] = dict(action)
                action_provenance = str(payload_map.get("action_provenance") or "").strip()
                if action_provenance:
                    record["action_provenance"] = action_provenance
                step_grounding_fallback = payload_map.get("step_grounding_fallback")
                if isinstance(step_grounding_fallback, Mapping):
                    record["step_grounding_fallback"] = _json_clone(
                        dict(step_grounding_fallback)
                    )
                if record.get("before_observation") is None and latest_after_observation is not None:
                    record["before_observation"] = dict(latest_after_observation)
                expected_outcome = str(payload_map.get("expected_outcome", "")).strip()
                if expected_outcome:
                    record["expected_outcome"] = expected_outcome
        elif name == "agent.desktop.step.verified":
            record["verification"] = _json_clone(dict(payload_map))

    cleaned_steps: List[Dict[str, Any]] = []
    previous_observation = initial_observation
    for iteration in sorted(iteration_records):
        record = iteration_records[iteration]
        action = record.get("action")
        if not isinstance(action, Mapping):
            if cleaned_steps:
                break
            continue
        if not _is_replayable_desktop_action(action):
            break
        verification = record.get("verification")
        if isinstance(verification, Mapping) and verification.get("step_succeeded") is not None:
            if not bool(verification.get("step_succeeded")):
                break
        step: Dict[str, Any] = {
            "index": len(cleaned_steps) + 1,
            "iteration": int(
                record.get("replay_action_index") or record.get("iteration") or iteration
            ),
            "action": dict(action),
            "successful": True,
        }
        if record.get("step_index") is not None:
            step["step_index"] = record.get("step_index")
        if record.get("replay_action_index") is not None:
            step["replay_action_index"] = record.get("replay_action_index")
        if record.get("action_provenance"):
            step["action_provenance"] = record.get("action_provenance")
        if isinstance(record.get("step_grounding_fallback"), Mapping):
            step["step_grounding_fallback"] = dict(record["step_grounding_fallback"])
        before_observation = record.get("before_observation") or previous_observation
        if isinstance(before_observation, Mapping):
            step["before_observation"] = dict(before_observation)
        after_observation = record.get("after_observation")
        if isinstance(after_observation, Mapping):
            step["after_observation"] = dict(after_observation)
            previous_observation = dict(after_observation)
        expected_outcome = str(record.get("expected_outcome", "")).strip()
        if expected_outcome:
            step["expected_outcome"] = expected_outcome
        if isinstance(verification, Mapping):
            step["verification"] = dict(verification)
        cleaned_steps.append(step)
    return cleaned_steps


def _completed_desktop_steps_from_logged_events(
    detail_payload: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    events = detail_payload.get("events")
    if not isinstance(events, list):
        return []

    iteration_records: Dict[int, Dict[str, Any]] = {}
    initial_observation = _logged_observation_index(detail_payload)[0]
    latest_after_observation = initial_observation
    for event in events:
        if not isinstance(event, Mapping):
            continue
        name = str(event.get("name", "")).strip()
        payload = event.get("payload")
        payload_map = payload if isinstance(payload, Mapping) else {}
        iteration = _coerce_optional_int_logged(payload_map.get("iteration"))
        step_index = _coerce_optional_int_logged(payload_map.get("step_index"))
        record_key = step_index if step_index is not None else iteration

        if name == "agent.desktop.observed":
            observation_phase = str(payload_map.get("observation_phase", "")).strip().lower()
            observation = _observation_snapshot(payload_map)
            if observation_phase == "before" and initial_observation is None:
                initial_observation = observation
            elif observation_phase == "after" and iteration is not None and observation is not None:
                record = iteration_records.setdefault(iteration, {"iteration": iteration})
                if record.get("before_observation") is None and latest_after_observation is not None:
                    record["before_observation"] = dict(latest_after_observation)
                record["after_observation"] = observation
                latest_after_observation = observation
            continue

        if record_key is None:
            continue
        record = iteration_records.setdefault(record_key, {"iteration": iteration or record_key})
        if step_index is not None:
            record["step_index"] = step_index
        if iteration is not None:
            record["replay_action_index"] = iteration
        if name == "agent.desktop.actions.completed":
            action = payload_map.get("action")
            if isinstance(action, Mapping):
                record["action"] = dict(action)
                action_provenance = str(payload_map.get("action_provenance") or "").strip()
                if action_provenance:
                    record["action_provenance"] = action_provenance
                step_grounding_fallback = payload_map.get("step_grounding_fallback")
                if isinstance(step_grounding_fallback, Mapping):
                    record["step_grounding_fallback"] = _json_clone(
                        dict(step_grounding_fallback)
                    )
                if record.get("before_observation") is None and latest_after_observation is not None:
                    record["before_observation"] = dict(latest_after_observation)
                expected_outcome = str(
                    payload_map.get("expected_outcome")
                    or action.get("expected_outcome")
                    or ""
                ).strip()
                if expected_outcome:
                    record["expected_outcome"] = expected_outcome
        elif name == "agent.desktop.step.verified":
            record["verification"] = _json_clone(dict(payload_map))

    completed_steps: List[Dict[str, Any]] = []
    previous_observation = initial_observation
    for iteration in sorted(iteration_records):
        record = iteration_records[iteration]
        action = record.get("action")
        if not isinstance(action, Mapping) or not _is_replayable_desktop_action(action):
            continue
        step: Dict[str, Any] = {
            "index": len(completed_steps) + 1,
            "iteration": int(
                record.get("replay_action_index") or record.get("iteration") or iteration
            ),
            "action": dict(action),
            "successful": True,
        }
        if record.get("step_index") is not None:
            step["step_index"] = record.get("step_index")
        if record.get("replay_action_index") is not None:
            step["replay_action_index"] = record.get("replay_action_index")
        if record.get("action_provenance"):
            step["action_provenance"] = record.get("action_provenance")
        if isinstance(record.get("step_grounding_fallback"), Mapping):
            step["step_grounding_fallback"] = dict(record["step_grounding_fallback"])
        before_observation = record.get("before_observation") or previous_observation
        if isinstance(before_observation, Mapping):
            step["before_observation"] = dict(before_observation)
        after_observation = record.get("after_observation")
        if isinstance(after_observation, Mapping):
            step["after_observation"] = dict(after_observation)
            previous_observation = dict(after_observation)
        expected_outcome = str(record.get("expected_outcome", "")).strip()
        if expected_outcome:
            step["expected_outcome"] = expected_outcome
        verification = record.get("verification")
        if isinstance(verification, Mapping):
            step["verification"] = dict(verification)
        completed_steps.append(step)
    return completed_steps


def _logged_observation_index(
    detail_payload: Mapping[str, Any],
) -> tuple[Optional[Dict[str, Any]], Dict[int, Dict[str, Any]]]:
    screenshot_details = detail_payload.get("screenshot_details")
    normalized_entries: List[Dict[str, Any]] = []
    if isinstance(screenshot_details, list):
        for entry in screenshot_details:
            if not isinstance(entry, Mapping):
                continue
            path = str(entry.get("path", "")).strip()
            if not path:
                continue
            normalized = _json_clone(dict(entry))
            normalized["path"] = path
            normalized_entries.append(normalized)
    else:
        captured = detail_payload.get("captured_screenshots")
        if isinstance(captured, list):
            for path in captured:
                normalized_path = str(path or "").strip()
                if normalized_path:
                    normalized_entries.append({"path": normalized_path, "exists": True})

    before_observation: Optional[Dict[str, Any]] = None
    after_observations: Dict[int, Dict[str, Any]] = {}
    for entry in normalized_entries:
        path = str(entry.get("path", "")).strip()
        if not path:
            continue
        index = _extract_observation_index(path)
        if index is None:
            continue
        if Path(path).name.lower().startswith("before-"):
            if before_observation is None:
                before_observation = entry
            continue
        after_observations[index] = entry
    return before_observation, after_observations


def _extract_observation_index(path: str) -> Optional[int]:
    match = re.search(r"(before|after)-(\d+)\.", Path(path).name.lower())
    if match is None:
        return None
    return _coerce_int(match.group(2), default=0) or None


def _resolve_arxiv_replay_request(user_query: str) -> Optional[Dict[str, Any]]:
    query = str(user_query or "").strip()
    if not query:
        return None
    normalized = normalize_replay_key(query)
    if "arxiv" not in normalized or "download" not in normalized:
        return None

    explicit_id = _extract_arxiv_id(query)
    title_candidate = _extract_arxiv_title_candidate(query)
    if explicit_id:
        return {
            "user_query": query,
            "arxiv_id": explicit_id,
            "title": title_candidate or f"arXiv:{explicit_id}",
            "resolver": "query_arxiv_id",
        }

    if not title_candidate:
        return None

    resolved = _resolve_arxiv_id_from_title(title_candidate)
    if resolved is None:
        return None
    return {
        "user_query": query,
        "arxiv_id": resolved["arxiv_id"],
        "title": resolved.get("title") or title_candidate,
        "resolver": "arxiv_atom_title_search",
    }


def _extract_arxiv_id(value: str) -> str:
    match = ARXIV_ID_PATTERN.search(str(value or ""))
    return match.group("id") if match is not None else ""


def _extract_arxiv_title_candidate(user_query: str) -> str:
    candidate = str(user_query or "").strip()
    explicit_title = _extract_explicit_arxiv_title_candidate(candidate)
    if explicit_title:
        return explicit_title

    candidate = re.sub(r"https?://\S+", " ", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\barxiv\s*:\s*\d{4}\.\d{4,5}(?:v\d+)?\b", " ", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\b\d{4}\.\d{4,5}(?:v\d+)?\b", " ", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\bdownload(?:ing)?\b", " ", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\b(?:the|a|an)\b", " ", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\bpaper\b", " ", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\bfrom\s+arxiv\b.*", " ", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\b(?:from|at|url|link)\b", " ", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\busing\b.*", " ", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\bvia\b.*", " ", candidate, flags=re.IGNORECASE)
    candidate = candidate.strip(" \"'“”‘’.,:;")
    candidate = " ".join(candidate.split())
    if not candidate or normalize_replay_key(candidate) in {"arxiv", "pdf"}:
        return ""
    return candidate


def _extract_explicit_arxiv_title_candidate(user_query: str) -> str:
    patterns = [
        r"\b(?:title|titled|called|named)\s*[:=]?\s*[\"'“”‘’]?(?P<title>.+?)[\"'“”‘’]?$",
        r"\bfor\s+[\"'“”‘’](?P<title>.+?)[\"'“”‘’]",
    ]
    for pattern in patterns:
        match = re.search(pattern, str(user_query or ""), flags=re.IGNORECASE)
        if match is None:
            continue
        title = _clean_arxiv_title_candidate(match.group("title"))
        if title:
            return title
    return ""


def _clean_arxiv_title_candidate(title: str) -> str:
    cleaned = str(title or "").strip(" \"'“”‘’.,:;")
    cleaned = re.sub(r"\s+and\s+download\b.*", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+(?:from|on)\s+arxiv\b.*", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+using\b.*", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+via\b.*", " ", cleaned, flags=re.IGNORECASE)
    cleaned = " ".join(cleaned.strip(" \"'“”‘’.,:;").split())
    if normalize_replay_key(cleaned) in {"arxiv", "pdf", "paper"}:
        return ""
    return cleaned


def _resolve_arxiv_id_from_title(title: str) -> Optional[Dict[str, str]]:
    normalized_title = str(title or "").strip()
    if not normalized_title:
        return None
    query = urllib.parse.urlencode(
        {
            "search_query": f'ti:"{normalized_title}"',
            "start": "0",
            "max_results": "1",
        }
    )
    url = f"https://export.arxiv.org/api/query?{query}"
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            payload = response.read()
    except OSError:
        return None

    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return None
    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    entry = root.find("atom:entry", namespace)
    if entry is None:
        return None
    entry_id = (entry.findtext("atom:id", default="", namespaces=namespace) or "").strip()
    arxiv_match = ARXIV_PDF_URL_PATTERN.search(entry_id.replace("/abs/", "/pdf/"))
    if arxiv_match is None:
        return None
    entry_title = " ".join(
        (entry.findtext("atom:title", default="", namespaces=namespace) or "").split()
    )
    return {
        "arxiv_id": arxiv_match.group("id"),
        "title": entry_title or normalized_title,
    }


def _detect_arxiv_pdf_replay_template(replay_task: Mapping[str, Any]) -> Optional[Dict[str, str]]:
    haystack_parts = [
        str(replay_task.get("summary", "")),
        str(replay_task.get("goal", "")),
    ]
    payload = replay_task.get("payload")
    payload_map = payload if isinstance(payload, Mapping) else {}
    actions = _actions_from_payload(payload_map)
    for action in actions:
        if isinstance(action, Mapping):
            haystack_parts.extend(
                str(action.get(key, ""))
                for key in ("text", "url", "expected_outcome")
            )
    haystack = "\n".join(haystack_parts)
    if "arxiv" not in haystack.casefold() or "download" not in haystack.casefold():
        return None
    match = ARXIV_PDF_URL_PATTERN.search(haystack)
    if match is None:
        return None
    return {
        "arxiv_id": match.group("id"),
        "title": _infer_arxiv_template_title(replay_task),
    }


def _infer_arxiv_template_title(replay_task: Mapping[str, Any]) -> str:
    for value in (replay_task.get("summary"), replay_task.get("goal")):
        text = str(value or "").strip()
        match = re.search(r"Download\s+(.+?)\s+from\s+arXiv", text, flags=re.IGNORECASE)
        if match is not None:
            return match.group(1).strip()
    return ""


def _adapt_arxiv_replay_task(
    replay_task: Mapping[str, Any],
    *,
    old_id: str,
    new_id: str,
    old_title: str,
    new_title: str,
) -> Dict[str, Any]:
    adapted = _replace_arxiv_template_values(
        _json_clone(dict(replay_task)),
        old_id=old_id,
        new_id=new_id,
        old_title=old_title,
        new_title=new_title,
    )
    if not isinstance(adapted, dict):
        return _json_clone(dict(replay_task))
    adapted["summary"] = f"Download {new_title} from arXiv via adapted compact PDF route"
    adapted["goal"] = f"Use the local Chrome GUI to download the PDF of arXiv paper {new_id}, {new_title}."
    payload = adapted.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    actions = payload.get("actions")
    if isinstance(actions, list):
        for action in actions:
            if not isinstance(action, dict):
                continue
            if str(action.get("action", "")).strip().lower() in {"paste_text", "type"}:
                text = str(action.get("text", "")).strip()
                if ARXIV_SEARCH_URL_PATTERN.search(text):
                    action["text"] = _build_arxiv_title_search_url(new_title)
                elif ARXIV_PDF_URL_PATTERN.search(text):
                    action["text"] = f"https://arxiv.org/pdf/{new_id}.pdf"
    payload["actions"] = actions if isinstance(actions, list) else []
    payload["download_evidence_enabled"] = True
    payload.setdefault("step_verification_failure_policy", "advisory")
    adapted["payload"] = payload
    return adapted


def _adapt_arxiv_steps(
    steps: Sequence[Mapping[str, Any]],
    *,
    old_id: str,
    new_id: str,
    old_title: str,
    new_title: str,
) -> List[Dict[str, Any]]:
    adapted_steps = _replace_arxiv_template_values(
        [dict(step) for step in steps],
        old_id=old_id,
        new_id=new_id,
        old_title=old_title,
        new_title=new_title,
    )
    return [dict(step) for step in adapted_steps if isinstance(step, Mapping)] if isinstance(adapted_steps, list) else []


def _replace_arxiv_template_values(
    value: Any,
    *,
    old_id: str,
    new_id: str,
    old_title: str,
    new_title: str,
) -> Any:
    if isinstance(value, str):
        updated = value
        updated = ARXIV_SEARCH_URL_PATTERN.sub(
            lambda _: _build_arxiv_title_search_url(new_title),
            updated,
        )
        if old_id:
            updated = updated.replace(old_id, new_id)
        if old_title:
            updated = updated.replace(old_title, new_title)
            updated = updated.replace(old_title.lower(), new_title)
        return updated
    if isinstance(value, list):
        return [
            _replace_arxiv_template_values(
                item,
                old_id=old_id,
                new_id=new_id,
                old_title=old_title,
                new_title=new_title,
            )
            for item in value
        ]
    if isinstance(value, Mapping):
        return {
            key: _replace_arxiv_template_values(
                item,
                old_id=old_id,
                new_id=new_id,
                old_title=old_title,
                new_title=new_title,
            )
            for key, item in value.items()
        }
    return value


def _build_arxiv_title_search_url(title: str) -> str:
    query = urllib.parse.urlencode(
        {
            "query": str(title or "").strip(),
            "searchtype": "title",
            "abstracts": "show",
            "order": "-announced_date_first",
            "size": "50",
        }
    )
    return f"https://arxiv.org/search/?{query}"


def _build_trajectory_purpose_summary(
    user_query: str,
    task_record: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]],
    *,
    goal_coverage: str,
) -> str:
    base_summary = (
        str(task_record.get("summary", "")).strip()
        or str(task_record.get("goal", "")).strip()
        or str(user_query or "").strip()
    )
    last_step = next(
        (
            step
            for step in reversed(list(steps))
            if isinstance(step, Mapping)
        ),
        None,
    )
    if goal_coverage == "partial" and isinstance(last_step, Mapping):
        expected_outcome = str(
            last_step.get("expected_outcome")
            or (last_step.get("verification", {}) or {}).get("assessment")
            or ""
        ).strip()
        if expected_outcome:
            return expected_outcome
        action_label = _format_trajectory_action(last_step.get("action"))
        if action_label:
            return action_label
    if base_summary:
        return base_summary
    if isinstance(last_step, Mapping):
        return _format_trajectory_action(last_step.get("action"))
    return ""


def _build_trajectory_match_keys(
    *,
    user_query: str,
    purpose_summary: str,
    goal_coverage: str,
) -> List[str]:
    keys: List[str] = []
    normalized_purpose = normalize_replay_key(purpose_summary)
    normalized_query = normalize_replay_key(user_query)
    if goal_coverage == "full" and normalized_query:
        keys.append(normalized_query)
    if normalized_purpose and normalized_purpose not in keys:
        keys.append(normalized_purpose)
    return keys


def _trajectory_step_previews(steps: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    previews: List[Dict[str, Any]] = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, Mapping):
            continue
        action = step.get("action")
        preview: Dict[str, Any] = {
            "index": _coerce_int(step.get("index"), default=index),
            "iteration": _coerce_int(step.get("iteration"), default=index),
            "action": _json_clone(dict(action)) if isinstance(action, Mapping) else None,
            "action_label": _format_trajectory_action(action),
            "expected_outcome": str(step.get("expected_outcome", "")).strip() or None,
            "reference_screenshot_path": (
                str((step.get("after_observation") or {}).get("path", "")).strip() or None
                if isinstance(step.get("after_observation"), Mapping)
                else None
            ),
        }
        previews.append(preview)
        if len(previews) >= TRAJECTORY_STEP_PREVIEW_LIMIT:
            break
    return previews


def _format_trajectory_action(action: Any) -> str:
    if not isinstance(action, Mapping):
        return ""

    command = str(action.get("command", "")).strip()
    if command:
        return f"Run {command}"

    action_name = str(action.get("action", "")).strip().lower()
    if action_name == "activate_app":
        app_name = str(action.get("app_name", "")).strip()
        return f"Open {app_name}" if app_name else "Open an app"
    if action_name == "open":
        url = str(action.get("url", "")).strip()
        return f"Open {url}" if url else "Open a page"
    if action_name == "click":
        x = _coerce_int(action.get("x"), default=0)
        y = _coerce_int(action.get("y"), default=0)
        if "x" in action and "y" in action:
            return f"Click at ({x}, {y})"
        return "Click"
    if action_name == "type":
        text = str(action.get("text", "")).strip()
        return f'Type "{text}"' if text else "Type text"
    if action_name == "press":
        key = str(action.get("key", "")).strip()
        return f"Press {key}" if key else "Press a key"
    if action_name == "hotkey":
        keys = action.get("keys")
        if isinstance(keys, list) and keys:
            return f"Press {' + '.join(str(key) for key in keys)}"
        return "Press a shortcut"
    if action_name == "wait":
        seconds = action.get("seconds")
        return f"Wait {seconds} seconds" if seconds is not None else "Wait"
    if action_name == "scroll":
        clicks = _coerce_optional_int_logged(action.get("clicks"))
        if clicks is None:
            return "Scroll"
        return "Scroll down" if clicks < 0 else "Scroll up"
    path = str(action.get("path", "")).strip()
    if path:
        return f"Use {path}"
    selector = str(action.get("selector", "")).strip()
    if selector:
        return f"Use selector {selector}"
    url = str(action.get("url", "")).strip()
    if url:
        return f"Open {url}"
    return action_name.replace("_", " ").capitalize() or "Action"


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_optional_int_logged(value: Any) -> Optional[int]:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _build_trajectory_id(*, session_id: str, task_id: str) -> str:
    normalized_session = str(session_id or "").strip() or "session_unknown"
    normalized_task = str(task_id or "").strip() or "task_unknown"
    return f"{normalized_session}:{normalized_task}"


def _sanitize_filename_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip())
    return normalized.strip("-") or "unknown"
