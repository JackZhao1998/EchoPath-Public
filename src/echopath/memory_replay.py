from __future__ import annotations

import inspect
import json
import sys
import math
import time
from collections.abc import Sequence as SequenceABC
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .gui_wrapper import SUPPORTED_GUI_WRAPPER_ACTIONS
from .agent_utils import extract_json_object
from .memory_segments import SEGMENTS_KEY, native_action_indexes, runtime_segments
from .memory_repo import (
    ACTIVE_MEMORY_STATUS,
    DEFAULT_TASK_INTENT_MIN_SCORE,
    MemoryRepository,
    MemoryRepositoryError,
    score_task_intent_match,
)
from .replay import ReplayMatch, normalize_replay_key
from .router import ModelRequest, ModelRouter
from .visual_grounding import (
    DEFAULT_MAX_SCALE,
    DEFAULT_MIN_SCALE,
    DEFAULT_NARROW_MAX_SCALE,
    DEFAULT_NARROW_MIN_SCALE,
    locate_visual_grounding_target,
)


DEFAULT_MEMORY_REPLAY_MIN_SCORE = 0.75
DEFAULT_MEMORY_REPLAY_INTENT_MIN_SCORE = 0.32
DEFAULT_MEMORY_REPLAY_VISUAL_THRESHOLD = 0.78
DEFAULT_MEMORY_REPLAY_VISUAL_MIN_MARGIN = 0.02

# Half-extent of the first-pass search window, as a fraction of the screen,
# centred on the coordinate the memory recorded. A control rarely moves far
# between record and replay, so searching locally is both faster and less
# likely to lock onto a repeated look-alike elsewhere on screen.
DEFAULT_MEMORY_REPLAY_REAIM_REGION_FRACTION = 0.18
DEFAULT_MEMORY_REPLAY_REAIM_MIN_REGION_WIDTH = 320
DEFAULT_MEMORY_REPLAY_REAIM_MIN_REGION_HEIGHT = 240

# Crop candidates accumulate on a node as it is reused across runs; without a
# cap, re-aiming cost grows linearly with reuse_count.
DEFAULT_MEMORY_REPLAY_MAX_CROP_CANDIDATES = 3
MEMORY_REPLAY_POINTER_ACTIONS = {"click", "move"}
MEMORY_REPLAY_DETERMINISTIC_ANCHOR_ACTIONS = {"hotkey", "paste_text", "type"}
MEMORY_REPLAY_OPTIONAL_STARTUP_CHAIN_ACTIONS = MEMORY_REPLAY_POINTER_ACTIONS | {"wait"}
MEMORY_REPLAY_VISUAL_BINDINGS_KEY = "memory_replay_visual_bindings"
MEMORY_REPLAY_STATE_CONTRACTS_KEY = "memory_replay_state_contracts"


MEMORY_REPLAY_REASONING_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["execute_as_is", "modify_flexible_inputs", "reject"],
        },
        "modifications": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action_index": {"type": "integer", "minimum": 1},
                    "parameter_path": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "replacement_value": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": [
                    "action_index",
                    "parameter_path",
                    "replacement_value",
                    "rationale",
                ],
            },
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string"},
        "blockers": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["decision", "modifications", "confidence", "rationale", "blockers"],
}


class MemoryReplayError(RuntimeError):
    pass


class MemoryReplayReaimingError(MemoryReplayError):
    def __init__(
        self,
        message: str,
        *,
        action_index: Optional[int] = None,
        binding_report: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.action_index = action_index
        self.binding_report = dict(binding_report or {})


@dataclass(frozen=True)
class MemoryReplayPlan:
    path_id: str
    replay_task: Dict[str, Any]
    score: float
    matched_query_key: str
    matched_on: str
    compatibility: Dict[str, Any]
    path_record: Dict[str, Any]
    node_records: List[Dict[str, Any]] = field(default_factory=list)
    replay_reasoning: Dict[str, Any] = field(default_factory=dict)

    @property
    def actions(self) -> List[Dict[str, Any]]:
        payload = self.replay_task.get("payload")
        if not isinstance(payload, Mapping):
            return []
        actions = payload.get("actions")
        return [dict(action) for action in actions if isinstance(action, Mapping)] if isinstance(actions, list) else []

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "used": True,
            "source": "memory_repo",
            "memory_path_id": self.path_id,
            "score": self.score,
            "matched_query_key": self.matched_query_key,
            "matched_on": self.matched_on,
            "task_intent": self.path_record.get("task_intent"),
            "application": self.path_record.get("application"),
            "operation_count": len(self.actions),
            "compatibility": _json_clone(self.compatibility),
            "replay_reasoning": _json_clone(self.replay_reasoning),
        }


@dataclass(frozen=True)
class MemoryReplayExecutionResult:
    success: bool
    path_id: str
    replay_task: Dict[str, Any]
    action_results: List[Dict[str, Any]]
    performed_action_count: int
    error: Optional[str] = None
    failed_action_index: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "path_id": self.path_id,
            "replay_task": _json_clone(self.replay_task),
            "action_results": _json_clone(self.action_results),
            "performed_action_count": self.performed_action_count,
            "error": self.error,
            "failed_action_index": self.failed_action_index,
        }


class MemoryReplayRetriever:
    def __init__(self, memory_repository: MemoryRepository) -> None:
        self.memory_repository = memory_repository
        self.last_lookup_summary: Dict[str, Any] = {}

    def retrieve(
        self,
        *,
        goal: str,
        current_task: Optional[Mapping[str, Any]] = None,
        application: str = "",
        query_keys: Optional[Sequence[str]] = None,
        limit: int = 5,
        min_score: float = DEFAULT_MEMORY_REPLAY_MIN_SCORE,
        replay_fallback_to_planning: Optional[bool] = None,
        step_grounding_fallback_enabled: Optional[bool] = None,
        coordinate_copy_first: Optional[bool] = None,
        memory_enhanced_fallback_enabled: Optional[bool] = None,
        require_visual_evidence: bool = True,
    ) -> Optional[MemoryReplayPlan]:
        search_keys = _build_search_keys(goal=goal, current_task=current_task, query_keys=query_keys)
        self.last_lookup_summary = {
            "goal": str(goal or ""),
            "application": str(application or _extract_application(current_task) or ""),
            "search_keys": list(search_keys),
            "min_score": float(min_score),
            "candidate_count": 0,
            "rejected_count": 0,
            "rejections": [],
            "selected": None,
            "status": "not_started",
        }
        if not search_keys:
            self.last_lookup_summary["status"] = "no_search_keys"
            return None

        candidate_matches: Dict[str, Dict[str, Any]] = {}
        candidate_limit = max(int(limit), 25)
        for key in search_keys:
            for candidate in self.memory_repository.query_active_paths(
                key,
                limit=candidate_limit,
                min_intent_score=DEFAULT_TASK_INTENT_MIN_SCORE,
            ):
                path_id = str(candidate.get("path_id") or "").strip()
                if not path_id:
                    continue
                intent_score = float(candidate.get("intent_score") or 0.0)
                previous = candidate_matches.get(path_id)
                if previous is None or intent_score > float(previous.get("intent_score") or 0.0):
                    candidate_matches[path_id] = {
                        "matched_query_key": str(candidate.get("matched_query_key") or key),
                        "matched_on": str(candidate.get("matched_on") or "task_intent"),
                        "intent_score": intent_score,
                        "retrieval": _json_clone(candidate.get("retrieval") or {}),
                    }
        self.last_lookup_summary["candidate_count"] = len(candidate_matches)

        plans: List[MemoryReplayPlan] = []
        for path_id, retrieval_match in candidate_matches.items():
            matched_query_key = str(retrieval_match.get("matched_query_key") or "")
            try:
                path_record = self.memory_repository.load_path(path_id)
            except MemoryRepositoryError:
                self.last_lookup_summary["rejections"].append(
                    {
                        "path_id": path_id,
                        "matched_query_key": matched_query_key,
                        "reason": "path_load_failed",
                    }
                )
                continue
            recorded_platform = str(path_record.get("platform") or "").strip().lower()
            if recorded_platform and recorded_platform != sys.platform:
                # Coordinates and pixel crops are worthless across desktops;
                # skip rather than demote -- the fallback store already turns
                # foreign-platform knowledge into planner guidance.
                self.last_lookup_summary["rejections"].append(
                    {
                        "path_id": path_id,
                        "matched_query_key": matched_query_key,
                        "reason": "platform_mismatch",
                        "recorded_platform": recorded_platform,
                    }
                )
                continue
            node_records = _load_nodes(self.memory_repository, path_record)
            try:
                segments = runtime_segments({
                    "actions": [node["action"] for node in node_records],
                    SEGMENTS_KEY: path_record.get("segments", []),
                    "memory_flexible_action_inputs": path_record.get("flexible_action_inputs") or [],
                })
            except ValueError as error:
                self.last_lookup_summary["rejections"].append({
                    "path_id": path_id, "reason": "invalid_segments", "error": str(error),
                })
                continue
            visual_binding_bundle = build_memory_replay_visual_bindings(
                self.memory_repository,
                path_id=path_id,
                node_records=node_records,
            )
            compatibility = evaluate_memory_replay_compatibility(
                path_record=path_record,
                node_records=node_records,
                search_keys=search_keys,
                matched_query_key=matched_query_key,
                retrieval_intent_score=float(retrieval_match.get("intent_score") or 0.0),
                retrieval_match=retrieval_match,
                application=application or _extract_application(current_task),
                current_task=current_task,
            )
            missing_visual_bindings = list(
                visual_binding_bundle.get("missing_action_indexes") or []
            )
            native_indexes = native_action_indexes(segments)
            missing_visual_bindings = [i for i in missing_visual_bindings if i not in native_indexes]
            pointer_visual_evidence_ok = not missing_visual_bindings or not require_visual_evidence
            compatibility.setdefault("checks", []).append(
                {
                    "name": "pointer_visual_evidence",
                    "passed": pointer_visual_evidence_ok,
                    "required": bool(require_visual_evidence),
                    "pointer_action_count": int(
                        visual_binding_bundle.get("pointer_action_count") or 0
                    ),
                    "binding_count": len(visual_binding_bundle.get("bindings") or []),
                    "missing_action_indexes": missing_visual_bindings,
                }
            )
            compatibility["compatible"] = bool(
                compatibility.get("compatible") and pointer_visual_evidence_ok
            )
            state_contracts = build_memory_replay_state_contracts(
                path_record=path_record,
                node_records=node_records,
                visual_binding_bundle=visual_binding_bundle,
            )
            live_state_check = evaluate_memory_replay_live_start_state(
                current_task=current_task,
                state_contracts=(
                    [c for c in state_contracts if c["action_index"] <= segments[0]["end_action_index"]]
                    if segments and segments[0]["execution_policy"] == "replay"
                    else [] if segments else state_contracts
                ),
                visual_binding_bundle=visual_binding_bundle,
                working_directory=Path.cwd(),
            )
            compatibility.setdefault("checks", []).append(live_state_check)
            compatibility["compatible"] = bool(
                compatibility.get("compatible") and live_state_check.get("passed")
            )
            score = float(compatibility["score"])
            if not compatibility["compatible"]:
                self.last_lookup_summary["rejections"].append(
                    {
                        "path_id": path_id,
                        "matched_query_key": matched_query_key,
                        "reason": "compatibility_failed",
                        "score": score,
                        "failed_checks": [
                            check.get("name")
                            for check in compatibility.get("checks", [])
                            if isinstance(check, Mapping) and not check.get("passed")
                        ],
                    }
                )
                continue
            if score < float(min_score):
                self.last_lookup_summary["rejections"].append(
                    {
                        "path_id": path_id,
                        "matched_query_key": matched_query_key,
                        "reason": "below_min_score",
                        "score": score,
                    }
                )
                continue
            replay_task = self.memory_repository.export_path_to_replay_task(
                path_id,
                goal=goal,
                summary=str(path_record.get("task_intent") or goal),
                verify_after_action=False,
                verify_goal_completion=False,
            )
            replay_task = attach_memory_replay_visual_bindings(
                replay_task,
                visual_binding_bundle,
            )
            replay_task = attach_memory_replay_state_contracts(
                replay_task,
                state_contracts,
            )
            replay_task = materialize_memory_replay_task_for_current_request(
                current_task,
                replay_task,
            )
            replay_task = apply_memory_replay_execution_overrides(
                replay_task,
                fallback_to_planning=replay_fallback_to_planning,
                step_grounding_fallback_enabled=step_grounding_fallback_enabled,
                coordinate_copy_first=coordinate_copy_first,
                memory_enhanced_fallback_enabled=memory_enhanced_fallback_enabled,
            )
            plans.append(
                MemoryReplayPlan(
                    path_id=path_id,
                    replay_task=replay_task,
                    score=score,
                    matched_query_key=matched_query_key,
                    matched_on=str(compatibility.get("matched_on") or "query_key"),
                    compatibility=compatibility,
                    path_record=dict(path_record),
                    node_records=node_records,
                )
            )

        self.last_lookup_summary["rejected_count"] = len(self.last_lookup_summary["rejections"])
        if not plans:
            self.last_lookup_summary["status"] = "no_compatible_match"
            return None
        plans.sort(
            key=lambda plan: (
                plan.score,
                _parse_timestamp(str(plan.path_record.get("promoted_at") or plan.path_record.get("updated_at") or "")),
                plan.path_id,
            ),
            reverse=True,
        )
        selected = plans[0]
        self.last_lookup_summary["status"] = "selected"
        self.last_lookup_summary["selected"] = selected.to_metadata()
        return selected

    async def retrieve_with_reasoner(
        self,
        *,
        goal: str,
        current_task: Optional[Mapping[str, Any]] = None,
        application: str = "",
        query_keys: Optional[Sequence[str]] = None,
        limit: int = 5,
        min_score: float = DEFAULT_MEMORY_REPLAY_MIN_SCORE,
        replay_reasoner: Optional[ModelRouter] = None,
        replay_reasoner_model: str = "default",
        replay_reasoner_reasoning_effort: str = "medium",
        replay_fallback_to_planning: Optional[bool] = None,
        step_grounding_fallback_enabled: Optional[bool] = None,
        coordinate_copy_first: Optional[bool] = None,
        memory_enhanced_fallback_enabled: Optional[bool] = None,
        require_visual_evidence: bool = True,
    ) -> Optional[MemoryReplayPlan]:
        plan = self.retrieve(
            goal=goal,
            current_task=current_task,
            application=application,
            query_keys=query_keys,
            limit=limit,
            min_score=min_score,
            replay_fallback_to_planning=replay_fallback_to_planning,
            step_grounding_fallback_enabled=step_grounding_fallback_enabled,
            coordinate_copy_first=coordinate_copy_first,
            memory_enhanced_fallback_enabled=memory_enhanced_fallback_enabled,
            require_visual_evidence=require_visual_evidence,
        )
        if plan is None:
            return None
        flexible_inputs = _flexible_inputs_for_plan(plan)
        if replay_reasoner is None or (not flexible_inputs and not plan.replay_task.get("payload", {}).get(SEGMENTS_KEY)):
            return plan

        try:
            adapted = await adapt_memory_replay_plan_with_reasoner(
                replay_reasoner,
                plan,
                goal=goal,
                current_task=current_task,
                model=replay_reasoner_model,
                reasoning_effort=replay_reasoner_reasoning_effort,
            )
        except MemoryReplayError as error:
            self.last_lookup_summary["status"] = "replay_reasoner_error"
            self.last_lookup_summary["selected"] = {
                **plan.to_metadata(),
                "replay_reasoning_error": str(error),
            }
            return None
        if adapted is None:
            reasoning = plan.replay_reasoning or {}
            self.last_lookup_summary["status"] = "replay_reasoner_rejected"
            self.last_lookup_summary["selected"] = {
                **plan.to_metadata(),
                "replay_reasoning": _json_clone(reasoning),
            }
            return None
        self.last_lookup_summary["status"] = "selected"
        self.last_lookup_summary["selected"] = adapted.to_metadata()
        return adapted

    def find_latest_successful_match(self, user_query: str) -> Optional[ReplayMatch]:
        plan = self.retrieve(goal=user_query)
        if plan is None:
            return None
        return memory_replay_plan_to_replay_match(plan, self.memory_repository)


class MemoryBackedReplayStore:
    """Adapter that lets the existing orchestrator consume the memory repository as a replay store."""

    def __init__(
        self,
        memory_repository: MemoryRepository,
        *,
        application: str = "",
        min_score: float = DEFAULT_MEMORY_REPLAY_MIN_SCORE,
        replay_reasoner: Optional[ModelRouter] = None,
        replay_reasoner_model: str = "default",
        replay_reasoner_reasoning_effort: str = "medium",
        replay_fallback_to_planning: Optional[bool] = None,
        step_grounding_fallback_enabled: Optional[bool] = None,
        coordinate_copy_first: Optional[bool] = None,
        memory_enhanced_fallback_enabled: Optional[bool] = None,
        require_visual_evidence: bool = True,
    ) -> None:
        self.retriever = MemoryReplayRetriever(memory_repository)
        self.memory_repository = memory_repository
        self.application = str(application or "").strip()
        self.min_score = float(min_score)
        self.replay_reasoner = replay_reasoner
        self.replay_reasoner_model = str(replay_reasoner_model or "default")
        self.replay_reasoner_reasoning_effort = str(replay_reasoner_reasoning_effort or "medium")
        self.replay_fallback_to_planning = replay_fallback_to_planning
        self.step_grounding_fallback_enabled = step_grounding_fallback_enabled
        self.coordinate_copy_first = coordinate_copy_first
        self.memory_enhanced_fallback_enabled = memory_enhanced_fallback_enabled
        self.require_visual_evidence = bool(require_visual_evidence)
        # Settings for the OCR + decision-model rung (replay_decision.py); None leaves it off.
        self.decision_model: Optional[Dict[str, Any]] = None
        # Check the goal after the last replayed step; an unmet goal hands over to live
        # planning (gui_agent._plan_after_replay) instead of reporting success.
        self.verify_goal = True
        self.last_lookup_summary: Dict[str, Any] = {}

    def _with_decision_model(self, match: ReplayMatch) -> ReplayMatch:
        payload = match.replay_task.get("payload") if isinstance(match.replay_task, dict) else None
        if isinstance(payload, dict):
            if self.decision_model:
                payload["memory_replay_decision_model"] = dict(self.decision_model)
            payload["verify_goal_completion"] = bool(self.verify_goal)
        return match

    def find_latest_successful_match(self, user_query: str) -> Optional[ReplayMatch]:
        plan = self.retriever.retrieve(
            goal=user_query,
            application=self.application,
            min_score=self.min_score,
            replay_fallback_to_planning=self.replay_fallback_to_planning,
            step_grounding_fallback_enabled=self.step_grounding_fallback_enabled,
            coordinate_copy_first=self.coordinate_copy_first,
            memory_enhanced_fallback_enabled=self.memory_enhanced_fallback_enabled,
            require_visual_evidence=self.require_visual_evidence,
        )
        self.last_lookup_summary = dict(self.retriever.last_lookup_summary)
        if plan is None:
            return None
        return self._with_decision_model(memory_replay_plan_to_replay_match(plan, self.memory_repository))

    async def find_latest_successful_match_for_task(
        self,
        user_query: str,
        *,
        current_task: Optional[Mapping[str, Any]] = None,
    ) -> Optional[ReplayMatch]:
        plan = await self.retriever.retrieve_with_reasoner(
            goal=user_query,
            current_task=current_task,
            application=self.application,
            min_score=self.min_score,
            replay_reasoner=self.replay_reasoner,
            replay_reasoner_model=self.replay_reasoner_model,
            replay_reasoner_reasoning_effort=self.replay_reasoner_reasoning_effort,
            replay_fallback_to_planning=self.replay_fallback_to_planning,
            step_grounding_fallback_enabled=self.step_grounding_fallback_enabled,
            coordinate_copy_first=self.coordinate_copy_first,
            memory_enhanced_fallback_enabled=self.memory_enhanced_fallback_enabled,
            require_visual_evidence=self.require_visual_evidence,
        )
        self.last_lookup_summary = dict(self.retriever.last_lookup_summary)
        if plan is None:
            return None
        return self._with_decision_model(memory_replay_plan_to_replay_match(plan, self.memory_repository))


class MemoryReplayExecutor:
    def __init__(self, gui_wrapper: Any, *, source_agent: str = "memory-replay") -> None:
        self.gui_wrapper = gui_wrapper
        self.source_agent = str(source_agent or "memory-replay")

    async def execute_plan(
        self,
        plan: MemoryReplayPlan,
        *,
        stop_on_error: bool = True,
    ) -> MemoryReplayExecutionResult:
        if plan.replay_task.get("payload", {}).get(SEGMENTS_KEY):
            raise MemoryReplayError("Segmented memories must execute through GUIAgent with native handoff support")
        actions = plan.actions
        action_results: List[Dict[str, Any]] = []
        performed_action_count = 0
        for index, action in enumerate(actions, start=1):
            action_name = str(action.get("action") or "").strip().lower()
            if action_name not in SUPPORTED_GUI_WRAPPER_ACTIONS:
                return MemoryReplayExecutionResult(
                    success=False,
                    path_id=plan.path_id,
                    replay_task=plan.replay_task,
                    action_results=action_results,
                    performed_action_count=performed_action_count,
                    error=f"Unsupported replay action '{action_name}'.",
                    failed_action_index=index,
                )
            execution_action = dict(action)
            reaiming_report: Optional[Dict[str, Any]] = None
            visual_binding = memory_replay_visual_binding_for_action(
                plan.replay_task,
                index,
            )
            visual_reaim_required = memory_replay_visual_reaim_required(plan.replay_task)
            if action_name in MEMORY_REPLAY_POINTER_ACTIONS:
                if visual_binding is None and visual_reaim_required:
                    error = MemoryReplayReaimingError(
                        f"Replay pointer action {index} is missing visual target evidence.",
                        action_index=index,
                    )
                    return MemoryReplayExecutionResult(
                        success=False,
                        path_id=plan.path_id,
                        replay_task=plan.replay_task,
                        action_results=action_results,
                        performed_action_count=performed_action_count,
                        error=str(error),
                        failed_action_index=index,
                    )
                if visual_binding is not None:
                    try:
                        observation = await _observe_gui_wrapper_for_reaiming(self.gui_wrapper)
                        screen_size = await _gui_wrapper_screen_size(self.gui_wrapper)
                        execution_action, reaiming_report = bind_memory_replay_visual_action(
                            action,
                            visual_binding,
                            observation=observation,
                            screen_size=screen_size,
                            working_directory=Path.cwd(),
                        )
                    except MemoryReplayReaimingError as error:
                        action_results.append(
                            {
                                "index": index,
                                "action": _json_clone(action),
                                "status": "reaiming_rejected",
                                "error": str(error),
                                "visual_reaiming": _json_clone(error.binding_report),
                            }
                        )
                        return MemoryReplayExecutionResult(
                            success=False,
                            path_id=plan.path_id,
                            replay_task=plan.replay_task,
                            action_results=action_results,
                            performed_action_count=performed_action_count,
                            error=str(error),
                            failed_action_index=index,
                        )
            try:
                result = await _call_gui_wrapper_action(
                    self.gui_wrapper,
                    execution_action,
                    source={
                        "agent": self.source_agent,
                        "workflow": "memory_replay",
                        "memory_path_id": plan.path_id,
                        "replay_action_index": index,
                        "replay_action_count": len(actions),
                        "visual_reaiming": _json_clone(reaiming_report),
                    },
                )
            except Exception as error:
                action_results.append(
                    {
                        "index": index,
                        "action": _json_clone(execution_action),
                        "stored_action": _json_clone(action),
                        "status": "error",
                        "error": str(error),
                        "visual_reaiming": _json_clone(reaiming_report),
                    }
                )
                if stop_on_error:
                    return MemoryReplayExecutionResult(
                        success=False,
                        path_id=plan.path_id,
                        replay_task=plan.replay_task,
                        action_results=action_results,
                        performed_action_count=performed_action_count,
                        error=str(error),
                        failed_action_index=index,
                    )
                continue

            performed_action_count += 1
            action_results.append(
                {
                    "index": index,
                    "action": _json_clone(execution_action),
                    "stored_action": _json_clone(action),
                    "status": "ok",
                    "result": _json_clone(result),
                    "visual_reaiming": _json_clone(reaiming_report),
                }
            )

        return MemoryReplayExecutionResult(
            success=True,
            path_id=plan.path_id,
            replay_task=plan.replay_task,
            action_results=action_results,
            performed_action_count=performed_action_count,
        )


def evaluate_memory_replay_compatibility(
    *,
    path_record: Mapping[str, Any],
    node_records: Sequence[Mapping[str, Any]],
    search_keys: Sequence[str],
    matched_query_key: str,
    retrieval_intent_score: Optional[float] = None,
    retrieval_match: Optional[Mapping[str, Any]] = None,
    application: str = "",
    current_task: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []
    score = 0.0

    status = str(path_record.get("status") or "").strip()
    status_ok = status == ACTIVE_MEMORY_STATUS
    checks.append({"name": "active_status", "passed": status_ok, "status": status})
    if status_ok:
        score += 0.15

    intent_payload = dict(retrieval_match or {})
    intent_score = _float_or_none(retrieval_intent_score)
    if intent_score is None:
        scored = [
            score_task_intent_match(search_key, path_record)
            for search_key in search_keys
        ]
        scored.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
        if scored:
            intent_payload = scored[0]
            intent_score = float(intent_payload.get("score") or 0.0)
    intent_score = max(0.0, min(float(intent_score or 0.0), 1.0))
    intent_ok = intent_score >= DEFAULT_MEMORY_REPLAY_INTENT_MIN_SCORE
    checks.append(
        {
            "name": "task_intent_match",
            "passed": intent_ok,
            "matched_query_key": str(matched_query_key or "").strip() or None,
            "intent_score": round(intent_score, 4),
            "min_intent_score": DEFAULT_MEMORY_REPLAY_INTENT_MIN_SCORE,
            "matched_terms": list(intent_payload.get("matched_terms") or []),
        }
    )
    if intent_ok:
        score += 0.40 * intent_score

    task_type = _extract_task_type(current_task)
    task_type_ok = task_type in {"", "desktop_task"}
    checks.append({"name": "desktop_task_type", "passed": task_type_ok, "task_type": task_type or None})
    if task_type_ok:
        score += 0.05

    requested_application = normalize_replay_key(application)
    memory_application = normalize_replay_key(str(path_record.get("application") or ""))
    application_ok = not requested_application or not memory_application or requested_application == memory_application
    checks.append(
        {
            "name": "application_match",
            "passed": application_ok,
            "requested_application": requested_application or None,
            "memory_application": memory_application or None,
        }
    )
    if application_ok:
        score += 0.10

    artifact_validation = path_record.get("artifact_validation")
    artifact_ok = _artifact_validation_passed(artifact_validation if isinstance(artifact_validation, Mapping) else {})
    checks.append({"name": "artifact_validation", "passed": artifact_ok})
    if artifact_ok:
        score += 0.15

    reasoning_report = path_record.get("reasoning_report")
    reasoning = reasoning_report if isinstance(reasoning_report, Mapping) else {}
    confidence = _float_or_none(reasoning.get("confidence")) or 0.0
    reasoning_ok = bool(reasoning.get("viable")) and not reasoning.get("blockers")
    checks.append(
        {
            "name": "reasoning_gate",
            "passed": reasoning_ok,
            "confidence": confidence,
        }
    )
    if reasoning_ok:
        score += min(max(confidence, 0.0), 1.0) * 0.10

    unsupported_actions = [
        str(node.get("action", {}).get("action") or "")
        for node in node_records
        if str(node.get("action", {}).get("action") or "") not in SUPPORTED_GUI_WRAPPER_ACTIONS
    ]
    actions_ok = not unsupported_actions and bool(node_records)
    checks.append({"name": "supported_actions", "passed": actions_ok, "unsupported_actions": unsupported_actions})
    if actions_ok:
        score += 0.05

    compatible = all(bool(check["passed"]) for check in checks)
    return {
        "compatible": compatible,
        "score": round(min(score, 1.0), 4),
        "matched_on": "task_intent" if intent_ok else None,
        "checks": checks,
    }


def materialize_memory_replay_task_for_current_request(
    current_task: Optional[Mapping[str, Any]],
    replay_task: Mapping[str, Any],
) -> Dict[str, Any]:
    if not isinstance(current_task, Mapping):
        return _json_clone(replay_task)

    current_desktop_task = _extract_desktop_task(current_task)
    if current_desktop_task is None:
        return _json_clone(replay_task)

    if str(replay_task.get("type") or "").strip().lower() != "desktop_task":
        return _json_clone(replay_task)
    replay_payload = replay_task.get("payload")
    if not isinstance(replay_payload, Mapping):
        return _json_clone(replay_task)
    replay_actions = replay_payload.get("actions")
    if not isinstance(replay_actions, list):
        return _json_clone(replay_task)

    current_payload = current_desktop_task.get("payload")
    if not isinstance(current_payload, Mapping):
        current_payload = {}

    merged_task = _json_clone(current_desktop_task)
    merged_payload = _json_clone(current_payload)
    merged_payload["actions"] = _json_clone(replay_actions)
    for key in (
        "verify_after_action",
        "verify_goal_completion",
        "step_verification_failure_policy",
        MEMORY_REPLAY_VISUAL_BINDINGS_KEY,
        "memory_replay_visual_reaim_required",
        "memory_replay_fallback_to_planning",
        "memory_replay_step_grounding_fallback_enabled",
        "memory_replay_step_grounding_fallback_max_attempts",
        "memory_replay_coordinate_copy_first",
        "memory_replay_verify_state_transitions",
        "memory_replay_optional_prerequisite_skip",
        "memory_replay_repair_rewind_enabled",
        "memory_replay_repair_rewind_max_steps",
        "memory_replay_enhanced_fallback_enabled",
        "memory_replay_enhanced_fallback_reference_image_limit",
        "memory_replay_decision_model",
        MEMORY_REPLAY_STATE_CONTRACTS_KEY,
        SEGMENTS_KEY,
        "memory_flexible_action_inputs",
    ):
        if key in replay_payload:
            merged_payload[key] = _json_clone(replay_payload[key])
    for key in ("capture_after_action", "service_name"):
        if key not in merged_payload and key in replay_payload:
            merged_payload[key] = _json_clone(replay_payload[key])
    merged_task["payload"] = merged_payload
    return merged_task


def apply_memory_replay_execution_overrides(
    replay_task: Mapping[str, Any],
    *,
    fallback_to_planning: Optional[bool] = None,
    step_grounding_fallback_enabled: Optional[bool] = None,
    coordinate_copy_first: Optional[bool] = None,
    memory_enhanced_fallback_enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    updated = _json_clone(replay_task)
    payload = updated.get("payload")
    if not isinstance(payload, dict):
        return updated
    if fallback_to_planning is not None:
        payload["memory_replay_fallback_to_planning"] = bool(fallback_to_planning)
    if step_grounding_fallback_enabled is not None:
        payload["memory_replay_step_grounding_fallback_enabled"] = bool(
            step_grounding_fallback_enabled
        )
    if coordinate_copy_first is not None:
        payload["memory_replay_coordinate_copy_first"] = bool(coordinate_copy_first)
    if memory_enhanced_fallback_enabled is not None:
        payload["memory_replay_enhanced_fallback_enabled"] = bool(
            memory_enhanced_fallback_enabled
        )
    updated["payload"] = payload
    return updated


def build_memory_replay_state_contracts(
    *,
    path_record: Mapping[str, Any],
    node_records: Sequence[Mapping[str, Any]],
    visual_binding_bundle: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    guarded_step_ids = _dependency_guard_added_step_ids(path_record)
    bindings_by_index = {
        int(binding.get("action_index") or 0): dict(binding)
        for binding in (visual_binding_bundle or {}).get("bindings", [])
        if isinstance(binding, Mapping)
    }
    leading_startup_optional_indexes = _leading_startup_optional_action_indexes(
        node_records
    )
    contracts: List[Dict[str, Any]] = []
    for action_index, node in enumerate(node_records, start=1):
        action = node.get("action")
        action_map = dict(action) if isinstance(action, Mapping) else {}
        source_step_ids = _node_source_step_ids(node)
        guard_optional = bool(source_step_ids & guarded_step_ids)
        leading_startup_optional = action_index in leading_startup_optional_indexes
        optional = bool(guard_optional or leading_startup_optional)
        precondition = node.get("precondition")
        postcondition = node.get("postcondition")
        role = "required_step"
        optional_reason = None
        if guard_optional:
            role = "optional_prerequisite"
            optional_reason = "dependency_guard"
        elif leading_startup_optional:
            role = "optional_startup_prerequisite"
            optional_reason = "leading_startup_chain_before_deterministic_anchor"
        contracts.append(
            {
                "action_index": action_index,
                "node_id": str(node.get("node_id") or ""),
                "action": str(action_map.get("action") or "").strip().lower(),
                "role": role,
                "required": not optional,
                "optional": optional,
                "optional_reason": optional_reason,
                "source_step_ids": sorted(source_step_ids),
                "precondition": _json_clone(precondition),
                "postcondition": _json_clone(postcondition),
                "inferred_preconditions": _inferred_action_preconditions(
                    action_map,
                    action_index=action_index,
                ),
                "inferred_postconditions": _inferred_action_postconditions(
                    action_map,
                    action_index=action_index,
                    total_actions=len(node_records),
                    next_binding=bindings_by_index.get(action_index + 1),
                ),
                "visual_binding_available": action_index in bindings_by_index,
            }
        )
    return contracts


def attach_memory_replay_state_contracts(
    replay_task: Mapping[str, Any],
    contracts: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    updated = _json_clone(replay_task)
    payload = updated.get("payload")
    if not isinstance(payload, dict):
        raise MemoryReplayError("Replay task payload is missing while attaching state contracts.")
    payload[MEMORY_REPLAY_STATE_CONTRACTS_KEY] = [
        _json_clone(contract) for contract in contracts if isinstance(contract, Mapping)
    ]
    payload.setdefault("memory_replay_verify_state_transitions", True)
    payload.setdefault("memory_replay_optional_prerequisite_skip", True)
    payload.setdefault("memory_replay_repair_rewind_enabled", True)
    payload.setdefault("memory_replay_repair_rewind_max_steps", 2)
    updated["payload"] = payload
    return updated


def evaluate_memory_replay_live_start_state(
    *,
    current_task: Optional[Mapping[str, Any]],
    state_contracts: Sequence[Mapping[str, Any]],
    visual_binding_bundle: Mapping[str, Any],
    working_directory: Path,
) -> Dict[str, Any]:
    observation = _extract_current_observation(current_task)
    if observation is None:
        return {
            "name": "live_start_state",
            "passed": True,
            "checked": False,
            "status": "deferred_no_live_observation",
            "probe_count": 0,
            "probes": [],
        }

    bindings_by_index = {
        int(binding.get("action_index") or 0): dict(binding)
        for binding in visual_binding_bundle.get("bindings") or []
        if isinstance(binding, Mapping)
    }
    screen_size = _extract_current_screen_size(current_task)
    probes: List[Dict[str, Any]] = []
    for contract in state_contracts:
        if not isinstance(contract, Mapping):
            continue
        action_index = int(contract.get("action_index") or 0)
        action_name = str(contract.get("action") or "").strip().lower()
        if action_name not in MEMORY_REPLAY_POINTER_ACTIONS:
            continue
        binding = bindings_by_index.get(action_index)
        if binding is None:
            continue
        optional = bool(contract.get("optional") or not bool(contract.get("required", True)))
        probe_action = {"action": action_name}
        try:
            _bound_action, report = bind_memory_replay_visual_action(
                probe_action,
                binding,
                observation=observation,
                screen_size=screen_size,
                working_directory=Path(working_directory),
            )
        except MemoryReplayReaimingError as error:
            probe = {
                "action_index": action_index,
                "node_id": contract.get("node_id"),
                "optional": optional,
                "visible": False,
                "error": str(error),
                "binding_report": _json_clone(error.binding_report),
            }
            probes.append(probe)
            if not optional:
                return {
                    "name": "live_start_state",
                    "passed": False,
                    "checked": True,
                    "status": "required_target_missing",
                    "probe_count": len(probes),
                    "probes": probes,
                }
            continue

        probe = {
            "action_index": action_index,
            "node_id": contract.get("node_id"),
            "optional": optional,
            "visible": True,
            "binding_report": _json_clone(report),
        }
        probes.append(probe)
        return {
            "name": "live_start_state",
            "passed": True,
            "checked": True,
            "status": "matched_optional_prerequisite"
            if optional
            else "matched_required_target",
            "probe_count": len(probes),
            "probes": probes,
        }

    return {
        "name": "live_start_state",
        "passed": True,
        "checked": bool(probes),
        "status": "no_required_pointer_target_checked"
        if probes
        else "no_pointer_target_contract",
        "probe_count": len(probes),
        "probes": probes,
    }


async def adapt_memory_replay_plan_with_reasoner(
    replay_reasoner: ModelRouter,
    plan: MemoryReplayPlan,
    *,
    goal: str,
    current_task: Optional[Mapping[str, Any]] = None,
    model: str = "default",
    reasoning_effort: str = "medium",
) -> Optional[MemoryReplayPlan]:
    flexible_inputs = _flexible_inputs_for_plan(plan)
    segments = runtime_segments(plan.replay_task.get("payload") or {})
    if not flexible_inputs and not segments:
        return plan

    prompt = _build_replay_reasoning_prompt(
        plan=plan,
        goal=goal,
        current_task=current_task,
        flexible_inputs=flexible_inputs,
    )
    response = await replay_reasoner.generate(
        ModelRequest(
            model=model,
            input=prompt,
            reasoning_effort=reasoning_effort,
            max_output_tokens=3000,
            metadata={"operation": "memory_replay_flexible_input_reasoning"},
            extra_body={"output_schema": MEMORY_REPLAY_REASONING_SCHEMA},
        )
    )
    payload = extract_json_object(response.output_text)
    if payload is None:
        raise MemoryReplayError("Replay reasoner returned invalid JSON.")
    if not isinstance(payload, Mapping):
        raise MemoryReplayError("Replay reasoner returned a non-object response.")

    decision = str(payload.get("decision") or "").strip().lower()
    reasoning = {
        "decision": decision,
        "modifications": _json_clone(payload.get("modifications") or []),
        "confidence": _float_or_none(payload.get("confidence")),
        "rationale": str(payload.get("rationale") or "").strip(),
        "blockers": [
            str(blocker).strip()
            for blocker in payload.get("blockers") or []
            if str(blocker).strip()
        ],
        "model": response.model,
        "response_id": response.response_id,
    }
    if decision == "execute_as_is":
        if reasoning["blockers"]:
            return None
        return replace(plan, replay_reasoning=reasoning)
    if decision == "reject":
        return None
    if decision != "modify_flexible_inputs":
        raise MemoryReplayError(f"Replay reasoner returned unsupported decision '{decision}'.")

    if reasoning["blockers"]:
        return None
    replay_task = _json_clone(plan.replay_task)
    payload_map = replay_task.get("payload")
    if not isinstance(payload_map, dict):
        raise MemoryReplayError("Replay task payload is missing.")
    actions = payload_map.get("actions")
    if not isinstance(actions, list):
        raise MemoryReplayError("Replay task actions are missing.")

    modifications = _validate_replay_reasoner_modifications(
        payload.get("modifications"),
        flexible_inputs,
    )
    if not modifications:
        raise MemoryReplayError("Replay reasoner chose modification without any valid modifications.")
    for modification in modifications:
        _set_action_value(
            actions,
            int(modification["action_index"]),
            list(modification["parameter_path"]),
            str(modification["replacement_value"]),
        )
    payload_map["actions"] = actions
    payload_map["memory_replay_reasoning"] = reasoning
    replay_task["payload"] = payload_map
    reasoning["modifications"] = _json_clone(modifications)
    return replace(plan, replay_task=replay_task, replay_reasoning=reasoning)


def _build_replay_reasoning_prompt(
    *,
    plan: MemoryReplayPlan,
    goal: str,
    current_task: Optional[Mapping[str, Any]],
    flexible_inputs: Sequence[Mapping[str, Any]],
) -> str:
    payload = {
        "current_goal": str(goal or ""),
        "current_task": _truncate_value(_json_clone(current_task or {})),
        "memory": {
            "path_id": plan.path_id,
            "task_intent": plan.path_record.get("task_intent"),
            "application": plan.path_record.get("application"),
            "matched_query_key": plan.matched_query_key,
            "compatibility": plan.compatibility,
        },
        "actions": _compact_replay_actions(plan.actions),
        "flexible_action_inputs": _truncate_value(_json_clone(list(flexible_inputs))),
        "segments": _json_clone(plan.replay_task.get("payload", {}).get(SEGMENTS_KEY) or []),
    }
    return (
        "Decide whether a stored GUI replay memory can execute the current task. "
        "The procedure is fixed except for the listed flexible_action_inputs. "
        "When segments are present, native segments replace their recorded actions with fresh "
        "decisions for the current request; conditional segments check reuse at runtime. "
        "Differences handled by those segment goals are allowed and do not require rejection. "
        "Assess whether the segment contracts together cover the current task. Do not change "
        "segment policies or treat original native example actions as mandatory choices. "
        "Fields bound by segment output_bindings are filled after native execution; do not "
        "invent those values or reject because those future outputs are not available yet. "
        "Return execute_as_is if the stored action values already satisfy the current task. "
        "Return modify_flexible_inputs if the same GUI procedure applies and only listed "
        "flexible values, such as search contents or input text, need replacement. "
        "Return reject if the current task requires changing any non-flexible action, "
        "different navigation, a different application, unsafe behavior, or information not "
        "available in the current task. Only produce modifications that target one of the "
        "provided flexible action_index + parameter_path pairs. Do not rewrite, add, remove, "
        "or reorder actions.\n\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def _validate_replay_reasoner_modifications(
    value: Any,
    flexible_inputs: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        raise MemoryReplayError("Replay reasoner modifications must be a list.")
    allowed = {
        (
            int(item.get("action_index")),
            tuple(str(part) for part in item.get("parameter_path") or []),
        )
        for item in flexible_inputs
        if item.get("action_index") is not None and isinstance(item.get("parameter_path"), list)
    }
    modifications: List[Dict[str, Any]] = []
    seen = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise MemoryReplayError("Replay reasoner modification entries must be objects.")
        action_index = int(item.get("action_index") or 0)
        parameter_path = [
            str(part).strip()
            for part in item.get("parameter_path") or []
            if str(part).strip()
        ]
        key = (action_index, tuple(parameter_path))
        if key not in allowed:
            raise MemoryReplayError(
                f"Replay reasoner tried to modify non-flexible action field {key!r}."
            )
        if key in seen:
            raise MemoryReplayError(
                f"Replay reasoner returned duplicate modification for {key!r}."
            )
        seen.add(key)
        modifications.append(
            {
                "action_index": action_index,
                "parameter_path": parameter_path,
                "replacement_value": str(item.get("replacement_value") or ""),
                "rationale": str(item.get("rationale") or "").strip(),
            }
        )
    return modifications


def memory_replay_plan_to_replay_match(
    plan: MemoryReplayPlan,
    memory_repository: MemoryRepository,
) -> ReplayMatch:
    path_record = plan.path_record
    path_id = plan.path_id
    path_file = (memory_repository.path_directory / f"{_sanitize_id(path_id)}.json").resolve()
    actions = plan.actions
    trajectory_steps = [
        {
            "node_id": node.get("node_id"),
            "action": _json_clone(node.get("action")),
            "component_signature": node.get("component_signature"),
            "visual_evidence_count": len(node.get("visual_evidence") or []),
        }
        for node in plan.node_records
    ]
    return ReplayMatch(
        session_id="memory_repo",
        task_id=path_id,
        user_query=str(path_record.get("task_intent") or plan.matched_query_key),
        matched_at=str(path_record.get("promoted_at") or path_record.get("updated_at") or _utc_iso()),
        detail_path=path_file,
        replay_task=_json_clone(plan.replay_task),
        successful_operations=_json_clone(actions),
        trajectory_path=path_file,
        trajectory_steps=_json_clone(trajectory_steps),
        trajectory_status=ACTIVE_MEMORY_STATUS,
        purpose_summary=str(path_record.get("task_intent") or ""),
        goal_coverage="full",
        matched_on=plan.matched_on,
        adaptation={
            "type": "memory_repo_replay",
            "memory_path_id": path_id,
            "score": plan.score,
            "matched_query_key": plan.matched_query_key,
            "compatibility": _json_clone(plan.compatibility),
            "flexible_action_inputs": _json_clone(_flexible_inputs_for_plan(plan)),
            "replay_reasoning": _json_clone(plan.replay_reasoning),
            "visual_reaiming": _json_clone(
                _replay_task_payload(plan.replay_task).get(MEMORY_REPLAY_VISUAL_BINDINGS_KEY)
                or []
            ),
        },
    )


def build_memory_replay_visual_bindings(
    memory_repository: MemoryRepository,
    *,
    path_id: str,
    node_records: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    bindings: List[Dict[str, Any]] = []
    missing_action_indexes: List[int] = []
    pointer_action_count = 0

    for action_index, node in enumerate(node_records, start=1):
        action = node.get("action")
        if not isinstance(action, Mapping):
            continue
        action_name = str(action.get("action") or "").strip().lower()
        if action_name not in MEMORY_REPLAY_POINTER_ACTIONS:
            continue
        pointer_action_count += 1

        crop_candidates = _select_component_crop_evidence_candidates(node, path_id=path_id)
        if not crop_candidates:
            missing_action_indexes.append(action_index)
            continue
        crop_evidence = crop_candidates[0]
        reference_path = _resolve_component_crop_path(memory_repository, crop_evidence)
        if reference_path is None:
            missing_action_indexes.append(action_index)
            continue

        primary_candidate = _component_crop_candidate_payload(
            memory_repository,
            crop_evidence,
        )
        if primary_candidate is None:
            missing_action_indexes.append(action_index)
            continue
        width = float(primary_candidate["target_bbox"]["width"])
        height = float(primary_candidate["target_bbox"]["height"])
        reference_candidates = []
        for candidate_evidence in crop_candidates:
            candidate_payload = _component_crop_candidate_payload(
                memory_repository,
                candidate_evidence,
            )
            if candidate_payload is not None:
                reference_candidates.append(candidate_payload)

        bindings.append(
            {
                "action_index": action_index,
                "path_id": str(path_id),
                "node_id": str(node.get("node_id") or ""),
                "action": action_name,
                "reference_image_path": str(primary_candidate["reference_image_path"]),
                "reference_image_sha256": str(crop_evidence.get("sha256") or ""),
                "target_bbox": {
                    "x": 0,
                    "y": 0,
                    "width": int(round(width)),
                    "height": int(round(height)),
                },
                "click_ratio": list(primary_candidate["click_ratio"]),
                "threshold": DEFAULT_MEMORY_REPLAY_VISUAL_THRESHOLD,
                "min_match_margin": DEFAULT_MEMORY_REPLAY_VISUAL_MIN_MARGIN,
                "min_scale": 0.5,
                "max_scale": 2.0,
                "scale_step": 0.05,
                "match_mode": "gray",
                "reference_candidates": reference_candidates,
                "original_coordinates": {
                    "x": _optional_int_value(action.get("x")),
                    "y": _optional_int_value(action.get("y")),
                },
                "source": _json_clone(crop_evidence.get("source") or {}),
            }
        )

    return {
        "enabled": bool(pointer_action_count),
        "pointer_action_count": pointer_action_count,
        "bindings": bindings,
        "missing_action_indexes": missing_action_indexes,
    }


def attach_memory_replay_visual_bindings(
    replay_task: Mapping[str, Any],
    binding_bundle: Mapping[str, Any],
) -> Dict[str, Any]:
    updated = _json_clone(replay_task)
    payload = updated.get("payload")
    if not isinstance(payload, dict):
        raise MemoryReplayError("Replay task payload is missing while attaching visual bindings.")
    pointer_action_count = int(binding_bundle.get("pointer_action_count") or 0)
    payload[MEMORY_REPLAY_VISUAL_BINDINGS_KEY] = _json_clone(
        binding_bundle.get("bindings") or []
    )
    payload["memory_replay_visual_reaim_required"] = pointer_action_count > 0
    payload.setdefault("memory_replay_fallback_to_planning", False)
    updated["payload"] = payload
    return updated


def memory_replay_visual_binding_for_action(
    replay_task_or_payload: Mapping[str, Any],
    action_index: int,
) -> Optional[Dict[str, Any]]:
    payload = _replay_task_payload(replay_task_or_payload)
    bindings = payload.get(MEMORY_REPLAY_VISUAL_BINDINGS_KEY)
    if not isinstance(bindings, list):
        return None
    for binding in bindings:
        if not isinstance(binding, Mapping):
            continue
        if int(binding.get("action_index") or 0) == int(action_index):
            return dict(binding)
    return None


def memory_replay_state_contract_for_action(
    replay_task_or_payload: Mapping[str, Any],
    action_index: int,
) -> Optional[Dict[str, Any]]:
    payload = _replay_task_payload(replay_task_or_payload)
    contracts = payload.get(MEMORY_REPLAY_STATE_CONTRACTS_KEY)
    if not isinstance(contracts, list):
        return None
    for contract in contracts:
        if not isinstance(contract, Mapping):
            continue
        if int(contract.get("action_index") or 0) == int(action_index):
            return dict(contract)
    return None


def memory_replay_action_is_optional(
    replay_task_or_payload: Mapping[str, Any],
    action_index: int,
) -> bool:
    contract = memory_replay_state_contract_for_action(
        replay_task_or_payload,
        action_index,
    )
    if not isinstance(contract, Mapping):
        return False
    return bool(contract.get("optional") or not bool(contract.get("required", True)))


def memory_replay_optional_chain_resume_index(
    replay_task_or_payload: Mapping[str, Any],
    actions: Sequence[Mapping[str, Any]],
    *,
    start_iteration: int,
) -> Optional[int]:
    if start_iteration < 1 or start_iteration > len(actions):
        return None
    if not memory_replay_action_is_optional(replay_task_or_payload, start_iteration):
        return None
    for action_index in range(start_iteration + 1, len(actions) + 1):
        action = actions[action_index - 1]
        action_name = str(action.get("action") or "").strip().lower()
        if action_name in MEMORY_REPLAY_DETERMINISTIC_ANCHOR_ACTIONS:
            return action_index
        if not memory_replay_action_is_optional(replay_task_or_payload, action_index):
            return None
        if action_name not in MEMORY_REPLAY_OPTIONAL_STARTUP_CHAIN_ACTIONS:
            return None
    return None


def memory_replay_visual_reaim_required(replay_task_or_payload: Mapping[str, Any]) -> bool:
    payload = _replay_task_payload(replay_task_or_payload)
    return bool(payload.get("memory_replay_visual_reaim_required"))


def bind_memory_replay_visual_action(
    action: Mapping[str, Any],
    binding: Mapping[str, Any],
    *,
    observation: Mapping[str, Any],
    screen_size: Optional[Mapping[str, Any]],
    working_directory: Path,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    action_name = str(action.get("action") or "").strip().lower()
    action_index = int(binding.get("action_index") or 0) or None
    if action_name not in MEMORY_REPLAY_POINTER_ACTIONS:
        return dict(action), {
            "status": "not_required",
            "action_index": action_index,
            "action": action_name,
        }

    observation_path = str(observation.get("path") or "").strip()
    reference_path = str(binding.get("reference_image_path") or "").strip()
    if not observation_path or not reference_path:
        report = {
            "status": "rejected",
            "action_index": action_index,
            "reason": "Replay re-aiming requires current and reference screenshots.",
        }
        raise MemoryReplayReaimingError(
            report["reason"],
            action_index=action_index,
            binding_report=report,
        )

    target_id = str(binding.get("node_id") or f"memory-action-{action_index or 0}")
    next_step = {
        "action": "move_target" if action_name == "move" else "click_target",
        "target_description": target_id,
        "visual_target_id": target_id,
    }

    search_region = _memory_replay_search_region(
        binding,
        observation=observation,
        screen_size=screen_size,
    )
    passes: List[Dict[str, Any]] = []
    if search_region is not None:
        passes.append(
            {
                "name": "local_narrow",
                "search_region": search_region,
                "min_scale": DEFAULT_NARROW_MIN_SCALE,
                "max_scale": DEFAULT_NARROW_MAX_SCALE,
            }
        )
    passes.append(
        {
            "name": "full_wide",
            "search_region": None,
            "min_scale": None,
            "max_scale": None,
        }
    )

    reaiming_started_at = time.perf_counter()
    pass_reports: List[Dict[str, Any]] = []
    grounding: Any = None
    for pass_spec in passes:
        pass_targets = _visual_targets_for_binding(
            binding,
            target_id=target_id,
            search_region=pass_spec["search_region"],
            min_scale=pass_spec["min_scale"],
            max_scale=pass_spec["max_scale"],
        )
        candidate_grounding = locate_visual_grounding_target(
            observation_path=Path(observation_path),
            next_step=next_step,
            task_payload={
                "visual_grounding_targets": pass_targets,
                "visual_grounding_only": True,
            },
            working_directory=Path(working_directory),
        )
        pass_metrics = (
            candidate_grounding.get("metrics")
            if isinstance(candidate_grounding, Mapping)
            else None
        )
        pass_reports.append(
            {
                "pass": pass_spec["name"],
                "search_region": _json_clone(pass_spec["search_region"]),
                "min_scale": pass_spec["min_scale"],
                "max_scale": pass_spec["max_scale"],
                "located": bool(
                    isinstance(candidate_grounding, Mapping)
                    and candidate_grounding.get("located")
                ),
                "confidence": (
                    candidate_grounding.get("confidence")
                    if isinstance(candidate_grounding, Mapping)
                    else None
                ),
                "metrics": _json_clone(pass_metrics or {}),
            }
        )
        grounding = candidate_grounding
        if isinstance(candidate_grounding, Mapping) and candidate_grounding.get("located"):
            break

    reaiming_metrics = {
        "passes": pass_reports,
        "passes_run": len(pass_reports),
        "elapsed_ms": round((time.perf_counter() - reaiming_started_at) * 1000.0, 2),
    }

    if not isinstance(grounding, Mapping) or not grounding.get("located"):
        reason = (
            str(grounding.get("reason") or "")
            if isinstance(grounding, Mapping)
            else "Visual matcher did not return a result."
        )
        report = {
            "status": "rejected",
            "action_index": action_index,
            "action": action_name,
            "reason": reason or "Memorized UI component could not be located.",
            "grounding": _json_clone(grounding or {}),
            "original_coordinates": _json_clone(binding.get("original_coordinates") or {}),
            "reaiming_metrics": reaiming_metrics,
        }
        raise MemoryReplayReaimingError(
            report["reason"],
            action_index=action_index,
            binding_report=report,
        )

    controller_point, coordinate_report = image_point_to_controller_coordinates(
        x=float(grounding["x"]),
        y=float(grounding["y"]),
        observation=observation,
        screen_size=screen_size,
    )
    bound_action = dict(action)
    bound_action["x"] = controller_point[0]
    bound_action["y"] = controller_point[1]
    report = {
        "status": "reaimed",
        "action_index": action_index,
        "action": action_name,
        "node_id": binding.get("node_id"),
        "reference_image_path": reference_path,
        "original_coordinates": _json_clone(binding.get("original_coordinates") or {}),
        "matched_image_coordinates": {
            "x": int(grounding["x"]),
            "y": int(grounding["y"]),
        },
        "bound_controller_coordinates": {
            "x": controller_point[0],
            "y": controller_point[1],
        },
        "confidence": grounding.get("confidence"),
        "bbox": _json_clone(grounding.get("bbox")),
        "match": _json_clone(grounding.get("match")),
        "reference_candidates": len(pass_targets),
        "coordinate_transform": coordinate_report,
        "reaiming_metrics": reaiming_metrics,
    }
    return bound_action, report


def image_point_to_controller_coordinates(
    *,
    x: float,
    y: float,
    observation: Mapping[str, Any],
    screen_size: Optional[Mapping[str, Any]],
) -> tuple[tuple[int, int], Dict[str, Any]]:
    image_size = observation.get("image_size")
    image_size_map = dict(image_size) if isinstance(image_size, Mapping) else {}
    image_width = _positive_float(image_size_map.get("width"))
    image_height = _positive_float(image_size_map.get("height"))
    region_geometry = _region_geometry(observation.get("region"))

    origin_x = 0.0
    origin_y = 0.0
    logical_width = None
    logical_height = None
    transform_source = "identity"
    if region_geometry is not None:
        origin_x, origin_y, logical_width, logical_height = region_geometry
        transform_source = "observation_region"
    elif isinstance(screen_size, Mapping):
        logical_width = _positive_float(screen_size.get("width"))
        logical_height = _positive_float(screen_size.get("height"))
        transform_source = "screen_size"

    scale_x = (
        image_width / logical_width
        if image_width is not None and logical_width is not None
        else 1.0
    )
    scale_y = (
        image_height / logical_height
        if image_height is not None and logical_height is not None
        else 1.0
    )
    if scale_x <= 0 or scale_y <= 0:
        raise MemoryReplayReaimingError("Replay coordinate transform has an invalid scale.")

    controller_x = int(round(origin_x + float(x) / scale_x))
    controller_y = int(round(origin_y + float(y) / scale_y))
    if isinstance(screen_size, Mapping):
        screen_width = _positive_float(screen_size.get("width"))
        screen_height = _positive_float(screen_size.get("height"))
        if screen_width is not None and not 0 <= controller_x < int(math.ceil(screen_width)):
            raise MemoryReplayReaimingError(
                f"Re-aimed x coordinate {controller_x} is outside the current screen."
            )
        if screen_height is not None and not 0 <= controller_y < int(math.ceil(screen_height)):
            raise MemoryReplayReaimingError(
                f"Re-aimed y coordinate {controller_y} is outside the current screen."
            )

    return (controller_x, controller_y), {
        "source": transform_source,
        "image_size": _json_clone(image_size_map),
        "screen_size": _json_clone(screen_size or {}),
        "region": _json_clone(observation.get("region")),
        "origin_x": origin_x,
        "origin_y": origin_y,
        "scale_x": scale_x,
        "scale_y": scale_y,
    }


async def _observe_gui_wrapper_for_reaiming(gui_wrapper: Any) -> Dict[str, Any]:
    observe = getattr(gui_wrapper, "observe", None)
    if observe is None:
        raise MemoryReplayReaimingError(
            "GUI replay target does not expose an 'observe' method for visual re-aiming."
        )
    result = observe()
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, Mapping):
        raise MemoryReplayReaimingError("GUI replay observation did not return a mapping.")
    return dict(result)


async def _gui_wrapper_screen_size(gui_wrapper: Any) -> Optional[Dict[str, Any]]:
    screen_size = getattr(gui_wrapper, "screen_size", None)
    if screen_size is None:
        return None
    result = screen_size()
    if inspect.isawaitable(result):
        result = await result
    return dict(result) if isinstance(result, Mapping) else None


async def _call_gui_wrapper_action(
    gui_wrapper: Any,
    action: Mapping[str, Any],
    *,
    source: Mapping[str, Any],
) -> Dict[str, Any]:
    act = getattr(gui_wrapper, "act", None)
    if act is None:
        raise MemoryReplayError("GUI replay target does not expose an 'act' method.")
    result = act(action, source=source)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, Mapping):
        return dict(result)
    return {"result": _json_clone(result)}


def _replay_task_payload(replay_task_or_payload: Mapping[str, Any]) -> Dict[str, Any]:
    payload = replay_task_or_payload.get("payload")
    if isinstance(payload, Mapping):
        return dict(payload)
    return dict(replay_task_or_payload)


def _select_component_crop_evidence(
    node: Mapping[str, Any],
    *,
    path_id: str,
) -> Optional[Dict[str, Any]]:
    candidates = _select_component_crop_evidence_candidates(node, path_id=path_id)
    return candidates[0] if candidates else None


def _select_component_crop_evidence_candidates(
    node: Mapping[str, Any],
    *,
    path_id: str,
) -> List[Dict[str, Any]]:
    candidates = []
    for evidence in node.get("visual_evidence") or []:
        if not isinstance(evidence, Mapping):
            continue
        metadata = evidence.get("metadata")
        if not isinstance(metadata, Mapping) or metadata.get("kind") != "component_crop":
            continue
        source = evidence.get("source")
        phase = str(source.get("phase") or "") if isinstance(source, Mapping) else ""
        source_path_id = (
            str(source.get("path_id") or "") if isinstance(source, Mapping) else ""
        )
        image_size = metadata.get("image_size")
        image_size_map = dict(image_size) if isinstance(image_size, Mapping) else {}
        width = _positive_float(image_size_map.get("width")) or 0.0
        height = _positive_float(image_size_map.get("height")) or 0.0
        area = width * height
        crop_variant = str(metadata.get("crop_variant") or "")
        candidates.append(
            (
                0 if source_path_id == str(path_id) else 1,
                0 if phase == "before_observation" else 1,
                0 if crop_variant == "target" else 1,
                area,
                dict(evidence),
            )
        )
    if not candidates:
        return []
    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        evidence = candidate[4]
        signature = str(evidence.get("sha256") or evidence.get("blob_id") or evidence.get("path") or "")
        if signature and signature in seen:
            continue
        if signature:
            seen.add(signature)
        deduped.append(evidence)
        if len(deduped) >= DEFAULT_MEMORY_REPLAY_MAX_CROP_CANDIDATES:
            break
    return deduped


def _component_crop_candidate_payload(
    memory_repository: MemoryRepository,
    evidence: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    reference_path = _resolve_component_crop_path(memory_repository, evidence)
    if reference_path is None:
        return None
    metadata = evidence.get("metadata")
    metadata_map = dict(metadata) if isinstance(metadata, Mapping) else {}
    image_size = metadata_map.get("image_size")
    image_size_map = dict(image_size) if isinstance(image_size, Mapping) else {}
    width = _positive_float(image_size_map.get("width"))
    height = _positive_float(image_size_map.get("height"))
    if width is None or height is None:
        return None
    click_ratio = _component_crop_click_ratio(metadata_map, width=width, height=height)
    return {
        "reference_image_path": str(reference_path),
        "reference_image_sha256": str(evidence.get("sha256") or ""),
        "target_bbox": {
            "x": 0,
            "y": 0,
            "width": int(round(width)),
            "height": int(round(height)),
        },
        "click_ratio": [click_ratio[0], click_ratio[1]],
        "crop_variant": str(metadata_map.get("crop_variant") or "target"),
        "crop_box": _json_clone(metadata_map.get("crop_box") or {}),
    }


def _resolve_component_crop_path(
    memory_repository: MemoryRepository,
    evidence: Mapping[str, Any],
) -> Optional[Path]:
    direct = Path(str(evidence.get("path") or "")).expanduser()
    if str(direct) and direct.is_file():
        return direct.resolve()
    blob_id = str(evidence.get("blob_id") or evidence.get("sha256") or "").strip()
    if not blob_id:
        return None
    relocated = memory_repository.blob_directory / blob_id[:2] / f"{blob_id}.png"
    return relocated.resolve() if relocated.is_file() else None


def _component_crop_click_ratio(
    metadata: Mapping[str, Any],
    *,
    width: float,
    height: float,
) -> tuple[float, float]:
    action_point = metadata.get("action_point")
    crop_box = metadata.get("crop_box")
    if not isinstance(action_point, Mapping) or not isinstance(crop_box, Mapping):
        return 0.5, 0.5
    point_x = _float_or_none(action_point.get("x"))
    point_y = _float_or_none(action_point.get("y"))
    left = _float_or_none(crop_box.get("left"))
    top = _float_or_none(crop_box.get("top"))
    if None in {point_x, point_y, left, top}:
        return 0.5, 0.5
    ratio_x = (float(point_x) - float(left)) / width
    ratio_y = (float(point_y) - float(top)) / height
    return (
        min(max(ratio_x, 0.0), 1.0),
        min(max(ratio_y, 0.0), 1.0),
    )


def _visual_targets_for_binding(
    binding: Mapping[str, Any],
    *,
    target_id: str,
    search_region: Optional[Mapping[str, Any]] = None,
    min_scale: Optional[float] = None,
    max_scale: Optional[float] = None,
) -> List[Dict[str, Any]]:
    base_target = {
        "id": target_id,
        "label": target_id,
        "reference_image_path": str(binding.get("reference_image_path") or ""),
        "target_bbox": _json_clone(binding.get("target_bbox") or {}),
        "click_ratio": _json_clone(binding.get("click_ratio") or [0.5, 0.5]),
        "threshold": float(
            binding.get("threshold") or DEFAULT_MEMORY_REPLAY_VISUAL_THRESHOLD
        ),
        "min_match_margin": float(
            binding.get("min_match_margin")
            if binding.get("min_match_margin") is not None
            else DEFAULT_MEMORY_REPLAY_VISUAL_MIN_MARGIN
        ),
        "min_scale": float(
            min_scale
            if min_scale is not None
            else (binding.get("min_scale") or DEFAULT_MIN_SCALE)
        ),
        "max_scale": float(
            max_scale
            if max_scale is not None
            else (binding.get("max_scale") or DEFAULT_MAX_SCALE)
        ),
        "scale_step": float(binding.get("scale_step") or 0.05),
        "match_mode": str(binding.get("match_mode") or "gray"),
    }
    if search_region:
        base_target["search_region"] = dict(search_region)
    targets: List[Dict[str, Any]] = []
    candidates = binding.get("reference_candidates")
    if isinstance(candidates, list) and candidates:
        for index, candidate in enumerate(candidates, start=1):
            if not isinstance(candidate, Mapping):
                continue
            reference_path = str(candidate.get("reference_image_path") or "").strip()
            if not reference_path:
                continue
            target = dict(base_target)
            target["id"] = f"{target_id}-candidate-{index}"
            target["label"] = target_id
            target["reference_image_path"] = reference_path
            target["target_bbox"] = _json_clone(candidate.get("target_bbox") or {})
            target["click_ratio"] = _json_clone(candidate.get("click_ratio") or [0.5, 0.5])
            target["crop_variant"] = str(candidate.get("crop_variant") or "")
            targets.append(target)
    if not targets:
        targets.append(base_target)
    return targets


def _memory_replay_search_region(
    binding: Mapping[str, Any],
    *,
    observation: Mapping[str, Any],
    screen_size: Optional[Mapping[str, Any]],
) -> Optional[Dict[str, int]]:
    """Search window centred on the coordinate the memory recorded.

    Returns None when the recorded point is unknown or the window would cover
    the whole frame anyway. A wrong window is not a correctness risk: the caller
    always falls back to a full-frame pass when the local one does not locate.
    """

    coordinates = binding.get("original_coordinates")
    if not isinstance(coordinates, Mapping):
        return None
    x = _optional_int_value(coordinates.get("x"))
    y = _optional_int_value(coordinates.get("y"))
    if x is None or y is None:
        return None

    image_size = observation.get("image_size")
    image_map = dict(image_size) if isinstance(image_size, Mapping) else {}
    image_width = _optional_int_value(image_map.get("width")) or 0
    image_height = _optional_int_value(image_map.get("height")) or 0
    if image_width <= 0 or image_height <= 0:
        return None

    # Recorded coordinates are controller-space; the screenshot may be scaled.
    screen_map = dict(screen_size) if isinstance(screen_size, Mapping) else {}
    screen_width = _optional_int_value(screen_map.get("width")) or 0
    screen_height = _optional_int_value(screen_map.get("height")) or 0
    if screen_width > 0 and screen_height > 0:
        x = int(round(x * (image_width / screen_width)))
        y = int(round(y * (image_height / screen_height)))

    half_width = max(
        int(round(image_width * DEFAULT_MEMORY_REPLAY_REAIM_REGION_FRACTION)),
        DEFAULT_MEMORY_REPLAY_REAIM_MIN_REGION_WIDTH,
    )
    half_height = max(
        int(round(image_height * DEFAULT_MEMORY_REPLAY_REAIM_REGION_FRACTION)),
        DEFAULT_MEMORY_REPLAY_REAIM_MIN_REGION_HEIGHT,
    )
    left = max(0, x - half_width)
    top = max(0, y - half_height)
    right = min(image_width, x + half_width)
    bottom = min(image_height, y + half_height)
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        return None
    if width >= image_width and height >= image_height:
        return None
    return {"x": left, "y": top, "width": width, "height": height}


def _dependency_guard_added_step_ids(path_record: Mapping[str, Any]) -> set[str]:
    guard_payloads: List[Any] = []
    consolidation = path_record.get("consolidation")
    if isinstance(consolidation, Mapping):
        guard_payloads.append(consolidation.get("dependency_guard"))
        reasoning_report = consolidation.get("reasoning_report")
        if isinstance(reasoning_report, Mapping):
            guard_payloads.append(reasoning_report.get("dependency_guard"))
    reasoning = path_record.get("reasoning_report")
    if isinstance(reasoning, Mapping):
        guard_payloads.append(reasoning.get("dependency_guard"))
    selected: set[str] = set()
    for guard in guard_payloads:
        if not isinstance(guard, Mapping):
            continue
        for step_id in guard.get("added_step_ids") or []:
            text = str(step_id or "").strip()
            if text:
                selected.add(text)
    return selected


def _node_source_step_ids(node: Mapping[str, Any]) -> set[str]:
    step_ids: set[str] = set()
    for source in node.get("source_steps") or []:
        if not isinstance(source, Mapping):
            continue
        step_id = str(source.get("step_id") or "").strip()
        if step_id:
            step_ids.add(step_id)
    return step_ids


def _leading_startup_optional_action_indexes(
    node_records: Sequence[Mapping[str, Any]],
) -> set[int]:
    anchor_index: Optional[int] = None
    for action_index, node in enumerate(node_records, start=1):
        action = node.get("action")
        action_map = dict(action) if isinstance(action, Mapping) else {}
        action_name = str(action_map.get("action") or "").strip().lower()
        if action_name in MEMORY_REPLAY_DETERMINISTIC_ANCHOR_ACTIONS:
            anchor_index = action_index
            break
    if anchor_index is None or anchor_index <= 1:
        return set()

    optional_indexes: set[int] = set()
    for action_index, node in enumerate(node_records[: anchor_index - 1], start=1):
        action = node.get("action")
        action_map = dict(action) if isinstance(action, Mapping) else {}
        action_name = str(action_map.get("action") or "").strip().lower()
        if action_name not in MEMORY_REPLAY_OPTIONAL_STARTUP_CHAIN_ACTIONS:
            return set()
        optional_indexes.add(action_index)
    return optional_indexes


def _inferred_action_preconditions(
    action: Mapping[str, Any],
    *,
    action_index: int,
) -> List[str]:
    action_name = str(action.get("action") or "").strip().lower()
    if action_name in MEMORY_REPLAY_POINTER_ACTIONS:
        return ["The memorized visual target is visible and not covered by a modal blocker."]
    if action_index > 1:
        return ["The preceding replay action has reached the expected GUI state."]
    return ["The task environment has been prepared for replay."]


def _inferred_action_postconditions(
    action: Mapping[str, Any],
    *,
    action_index: int,
    total_actions: int,
    next_binding: Optional[Mapping[str, Any]],
) -> List[str]:
    action_name = str(action.get("action") or "").strip().lower()
    if next_binding is not None:
        return ["The next memorized visual target becomes visible or remains visible."]
    if action_index >= total_actions:
        return ["The replayed action sequence has advanced the task toward the final goal."]
    if action_name in {"type", "paste_text"}:
        return ["The requested text has been entered into the focused UI element."]
    return ["The screen state visibly advances for the replayed action."]


def _region_geometry(region: Any) -> Optional[tuple[float, float, float, float]]:
    if isinstance(region, Mapping):
        left = _float_or_none(region.get("left", region.get("x")))
        top = _float_or_none(region.get("top", region.get("y")))
        width = _float_or_none(region.get("width"))
        height = _float_or_none(region.get("height"))
        if width is None and region.get("right") is not None and left is not None:
            right = _float_or_none(region.get("right"))
            width = right - left if right is not None else None
        if height is None and region.get("bottom") is not None and top is not None:
            bottom = _float_or_none(region.get("bottom"))
            height = bottom - top if bottom is not None else None
    elif (
        isinstance(region, SequenceABC)
        and not isinstance(region, (str, bytes))
        and len(region) >= 4
    ):
        left = _float_or_none(region[0])
        top = _float_or_none(region[1])
        width = _float_or_none(region[2])
        height = _float_or_none(region[3])
    else:
        return None
    if None in {left, top, width, height} or float(width) <= 0 or float(height) <= 0:
        return None
    return float(left), float(top), float(width), float(height)


def _positive_float(value: Any) -> Optional[float]:
    number = _float_or_none(value)
    return number if number is not None and number > 0 else None


def _optional_int_value(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _build_search_keys(
    *,
    goal: str,
    current_task: Optional[Mapping[str, Any]],
    query_keys: Optional[Sequence[str]],
) -> List[str]:
    raw_keys: List[str] = [str(goal or "")]
    if isinstance(query_keys, SequenceABC) and not isinstance(query_keys, (str, bytes)):
        raw_keys.extend(str(value or "") for value in query_keys)
    if isinstance(current_task, Mapping):
        task = _extract_desktop_task(current_task)
        if isinstance(task, Mapping):
            raw_keys.append(str(task.get("goal") or ""))
            raw_keys.append(str(task.get("summary") or ""))
            payload = task.get("payload")
            if isinstance(payload, Mapping):
                raw_keys.extend(_task_intention_keys_from_payload(payload))
        raw_keys.extend(_task_intention_keys_from_payload(current_task))
    return _normalize_keys(raw_keys)


def _load_nodes(
    memory_repository: MemoryRepository,
    path_record: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    for node_id in path_record.get("node_ids") or []:
        try:
            nodes.append(memory_repository.load_node(str(node_id)))
        except MemoryRepositoryError:
            continue
    return nodes


def _task_identity_keys_from_payload(payload: Mapping[str, Any]) -> List[str]:
    keys: List[str] = []
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping):
        keys.extend(_task_identity_keys_from_payload(metadata))
    for key in ("task_id", "id", "source_task_id", "observation_task_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            keys.append(value)
    custom_benchmark = payload.get("custom_benchmark")
    if isinstance(custom_benchmark, Mapping):
        value = str(custom_benchmark.get("id") or "").strip()
        if value:
            keys.append(value)
    osworld = payload.get("osworld")
    if isinstance(osworld, Mapping):
        domain = str(osworld.get("domain") or "").strip()
        task_id = str(osworld.get("id") or osworld.get("task_id") or "").strip()
        if task_id:
            keys.append(task_id)
        if domain and task_id:
            keys.append(f"{domain} {task_id}")
    return keys


def _task_intention_keys_from_payload(payload: Mapping[str, Any]) -> List[str]:
    keys: List[str] = []
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping):
        keys.extend(_task_intention_keys_from_payload(metadata))
    for key in (
        "instruction",
        "goal",
        "summary",
        "task_goal",
        "task_intent",
        "user_query",
        "request",
        "title",
    ):
        value = str(payload.get(key) or "").strip()
        if value:
            keys.append(value)
    custom_benchmark = payload.get("custom_benchmark")
    if isinstance(custom_benchmark, Mapping):
        for key in ("instruction", "title", "goal", "summary"):
            value = str(custom_benchmark.get(key) or "").strip()
            if value:
                keys.append(value)
    osworld = payload.get("osworld")
    if isinstance(osworld, Mapping):
        instruction = str(osworld.get("instruction") or osworld.get("goal") or "").strip()
        if instruction:
            keys.append(instruction)
    return keys


def _flexible_inputs_for_plan(plan: MemoryReplayPlan) -> List[Dict[str, Any]]:
    payload = _replay_task_payload(plan.replay_task)
    path_inputs = plan.path_record.get("flexible_action_inputs")
    inputs = path_inputs if isinstance(path_inputs, list) else payload.get("memory_flexible_action_inputs", [])
    deferred = {
        (b["action_index"], tuple(b["parameter_path"]))
        for s in payload.get(SEGMENTS_KEY) or [] for b in s.get("output_bindings", [])
    }
    return [dict(item) for item in inputs if isinstance(item, Mapping)
            and (item.get("action_index"), tuple(item.get("parameter_path") or [])) not in deferred]


def _compact_replay_actions(actions: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    compact: List[Dict[str, Any]] = []
    for index, action in enumerate(actions, start=1):
        compact.append(
            {
                "action_index": index,
                "action": str(action.get("action") or ""),
                "parameters": _truncate_value(
                    {
                        str(key): value
                        for key, value in action.items()
                        if key not in {"action", "source"}
                    },
                    max_string=400,
                ),
            }
        )
    return compact


def _set_action_value(
    actions: List[Any],
    action_index: int,
    parameter_path: Sequence[str],
    value: Any,
) -> None:
    if action_index < 1 or action_index > len(actions):
        raise MemoryReplayError(f"Replay action index {action_index} is out of range.")
    action = actions[action_index - 1]
    if not isinstance(action, dict):
        raise MemoryReplayError(f"Replay action {action_index} is not editable.")
    if not parameter_path:
        raise MemoryReplayError("Replay modification parameter_path cannot be empty.")
    current: Any = action
    for key in parameter_path[:-1]:
        if not isinstance(current, dict) or key not in current:
            raise MemoryReplayError(
                f"Replay modification path {list(parameter_path)!r} does not exist."
            )
        current = current[key]
    final_key = str(parameter_path[-1])
    if not isinstance(current, dict) or final_key not in current:
        raise MemoryReplayError(
            f"Replay modification path {list(parameter_path)!r} does not exist."
        )
    current[final_key] = value


def _extract_desktop_task(current_task: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    nested = current_task.get("task")
    if isinstance(nested, Mapping):
        return nested
    if str(current_task.get("type") or "").strip().lower() == "desktop_task":
        return current_task
    return None


def _extract_task_type(current_task: Optional[Mapping[str, Any]]) -> str:
    if not isinstance(current_task, Mapping):
        return ""
    task = _extract_desktop_task(current_task)
    if task is None:
        return str(current_task.get("type") or "").strip().lower()
    return str(task.get("type") or "").strip().lower()


def _extract_application(current_task: Optional[Mapping[str, Any]]) -> str:
    if not isinstance(current_task, Mapping):
        return ""
    task = _extract_desktop_task(current_task)
    sources: List[Any] = [current_task]
    if isinstance(task, Mapping):
        sources.insert(0, task)
        payload = task.get("payload")
        if isinstance(payload, Mapping):
            sources.insert(0, payload)
    for source in sources:
        metadata = source.get("metadata") if isinstance(source, Mapping) else None
        if isinstance(metadata, Mapping):
            application = str(metadata.get("application") or "").strip()
            if application:
                return application
        application = str(source.get("application") or "").strip() if isinstance(source, Mapping) else ""
        if application:
            return application
    return ""


def _extract_current_observation(
    current_task: Optional[Mapping[str, Any]]
) -> Optional[Dict[str, Any]]:
    if not isinstance(current_task, Mapping):
        return None
    task = _extract_desktop_task(current_task)
    sources: List[Any] = [current_task]
    if isinstance(task, Mapping):
        sources.insert(0, task)
        payload = task.get("payload")
        if isinstance(payload, Mapping):
            sources.insert(0, payload)
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for key in (
            "observation",
            "current_observation",
            "initial_observation",
            "live_observation",
        ):
            observation = source.get(key)
            if isinstance(observation, Mapping) and str(observation.get("path") or "").strip():
                return dict(observation)
    return None


def _extract_current_screen_size(
    current_task: Optional[Mapping[str, Any]]
) -> Optional[Dict[str, Any]]:
    if not isinstance(current_task, Mapping):
        return None
    task = _extract_desktop_task(current_task)
    sources: List[Any] = [current_task]
    if isinstance(task, Mapping):
        sources.insert(0, task)
        payload = task.get("payload")
        if isinstance(payload, Mapping):
            sources.insert(0, payload)
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for key in ("screen_size", "current_screen_size"):
            screen_size = source.get(key)
            if isinstance(screen_size, Mapping):
                return dict(screen_size)
    return None


def _artifact_validation_passed(payload: Mapping[str, Any]) -> bool:
    status = str(payload.get("status") or "").strip().lower()
    return bool(payload.get("passed")) or status in {"pass", "passed", "success", "succeeded"}


def _normalize_keys(values: Any) -> List[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, SequenceABC) or isinstance(values, (str, bytes)):
        return []
    result: List[str] = []
    for value in values:
        key = normalize_replay_key(str(value or ""))
        if key and key not in result:
            result.append(key)
    return result


def _parse_timestamp(value: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _float_or_none(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _truncate_value(value: Any, *, max_string: int = 500) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _truncate_value(item, max_string=max_string)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_truncate_value(item, max_string=max_string) for item in value]
    if isinstance(value, str) and len(value) > max_string:
        return value[:max_string] + f"...<{len(value) - max_string} chars omitted>"
    return value


def _sanitize_id(value: str) -> str:
    return "".join(ch for ch in str(value) if ch.isalnum() or ch in {"_", "-"}).strip()


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_clone(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_clone(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_clone(item) for item in value]
    if isinstance(value, tuple):
        return [_json_clone(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
