from __future__ import annotations

import hashlib
import json
import sys
import os
import re
import shutil
from io import BytesIO
from collections.abc import Sequence as SequenceABC
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

try:
    from PIL import Image
except ImportError:  # pragma: no cover - crop evidence is optional when Pillow is absent.
    Image = None  # type: ignore[assignment]

from .gui_wrapper import SUPPORTED_GUI_WRAPPER_ACTIONS
from .memory_segments import SEGMENTS_KEY, validate_segments
from .models import generate_id
from .replay import normalize_replay_key


MEMORY_REPO_SCHEMA_VERSION = 1
DEFAULT_MEMORY_REPO_ROOT = Path("local_data/memory_repo")
ACTIVE_MEMORY_STATUS = "active"
DEFAULT_TASK_INTENT_MIN_SCORE = 0.18
PATH_REF_STATUSES = {
    "candidate",
    "runtime_complete",
    "active",
    "branched",
    "repaired",
    "deprecated",
    "quarantined",
}


class MemoryRepositoryError(RuntimeError):
    pass


class MemoryPromotionGateError(MemoryRepositoryError):
    pass


@dataclass(frozen=True)
class PromotionGateResult:
    passed: bool
    reasons: List[str]
    artifact_validation: Dict[str, Any]
    reasoning_report: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": list(self.reasons),
            "artifact_validation": _json_safe(self.artifact_validation),
            "reasoning_report": _json_safe(self.reasoning_report),
        }


class MemoryRepository:
    def __init__(self, root_directory: Path = DEFAULT_MEMORY_REPO_ROOT) -> None:
        self.root_directory = Path(root_directory).resolve()
        self.blob_directory = self.root_directory / "blobs"
        self.node_directory = self.root_directory / "nodes"
        self.path_directory = self.root_directory / "paths"
        self.ref_directory = self.root_directory / "refs"
        self.index_directory = self.root_directory / "index"
        self.log_path = self.root_directory / "logs" / "events.jsonl"
        self._active_path_records_cache: Optional[List[Dict[str, Any]]] = None
        self._ensure_layout()

    def status(self) -> Dict[str, Any]:
        return {
            "root_directory": str(self.root_directory),
            "node_count": len(list(self.node_directory.glob("*.json"))),
            "path_count": len(list(self.path_directory.glob("*.json"))),
            "blob_count": len([path for path in self.blob_directory.glob("*/*") if path.suffix != ".json"]),
            "refs": {
                status: len(list((self.ref_directory / status).glob("*.json")))
                for status in sorted(PATH_REF_STATUSES)
            },
        }

    def add_candidate_from_wrapper_run(
        self,
        run_directory: Path,
        *,
        task_intent: str = "",
        query_keys: Optional[Sequence[str]] = None,
        application: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
        selected_step_ids: Optional[Sequence[str]] = None,
        consolidation: Optional[Mapping[str, Any]] = None,
        platform: str = "",
    ) -> Dict[str, Any]:
        run_directory = Path(run_directory).resolve()
        manifest = _read_json(run_directory / "manifest.json")
        steps = _read_jsonl(run_directory / "steps.jsonl")
        if not steps:
            raise MemoryRepositoryError("Wrapper run does not contain action steps.")

        original_action_count = len(steps)
        if selected_step_ids is not None:
            steps = _select_wrapper_steps(steps, selected_step_ids)

        run_id = str(manifest.get("run_id") or run_directory.name)
        path_id = generate_id("mem_path")
        node_ids: List[str] = []
        step_node_index: List[Dict[str, Any]] = []
        previous_node_id: Optional[str] = None
        for action_index, step in enumerate(steps, start=1):
            node = self._node_from_wrapper_step(
                step,
                run_id=run_id,
                path_id=path_id,
                run_directory=run_directory,
            )
            node_ids.append(node["node_id"])
            step_node_index.append(
                {
                    "step_id": str(step.get("step_id") or ""),
                    "node_id": node["node_id"],
                    "action_index": action_index,
                    "action": str(node.get("action", {}).get("action") or ""),
                }
            )
            if previous_node_id:
                self._link_child(previous_node_id, node["node_id"])
            previous_node_id = node["node_id"]

        # Nodes are content-addressed, so an identical action sequence means an
        # identical node_ids list. Refresh the existing path instead of piling
        # up duplicates every time the same task succeeds again.
        flexible_action_inputs = _normalize_flexible_action_inputs(
            (consolidation or {}).get("flexible_action_inputs"),
            steps=steps, step_node_index=step_node_index,
        )
        try:
            segments = validate_segments((consolidation or {}).get("segments"),
                                         [item["step_id"] for item in step_node_index],
                                         flexible_action_inputs)
        except ValueError as error:
            raise MemoryRepositoryError(str(error)) from error
        for existing in self.list_paths():
            existing_id = str(existing.get("path_id") or "")
            if not existing_id or existing.get("node_ids") != node_ids:
                continue
            existing_record = self.load_path(existing_id)
            # Policy changes must not be hidden by action-only deduplication.
            if (existing_record.get("segments") or []) != segments:
                continue
            existing_record["updated_at"] = _utc_iso()
            source = existing_record.get("source")
            if isinstance(source, dict):
                source["last_run_id"] = run_id
            self._write_json(
                self.path_directory / f"{_sanitize_id(existing_id)}.json",
                existing_record,
            )
            self._append_log(
                {
                    "event": "path_refreshed_from_duplicate_run",
                    "path_id": existing_id,
                    "run_id": run_id,
                }
            )
            return self._path_metadata(existing_record)

        task_goal = str(manifest.get("task_goal") or "").strip()
        keys = _normalize_query_keys(query_keys or [task_intent, task_goal, run_id])
        path_record = {
            "record_type": "memory_branch_path",
            "schema_version": MEMORY_REPO_SCHEMA_VERSION,
            "path_id": path_id,
            "status": "candidate",
            # GUI trajectories are recorded against one desktop's chrome and
            # coordinates; retrieval refuses to replay across platforms.
            "platform": str(platform or sys.platform).strip().lower(),
            "created_at": _utc_iso(),
            "updated_at": _utc_iso(),
            "source": {
                "type": "gui_wrapper_run",
                "run_id": run_id,
                "run_directory": str(run_directory),
                "manifest_path": str((run_directory / "manifest.json").resolve()),
                "steps_path": str((run_directory / "steps.jsonl").resolve()),
            },
            "application": str(application or _manifest_application(manifest)).strip() or None,
            "task_intent": str(task_intent or task_goal).strip(),
            "query_keys": keys,
            "node_ids": node_ids,
            "action_count": len(node_ids),
            "original_action_count": original_action_count,
            "step_node_index": step_node_index,
            "flexible_action_inputs": flexible_action_inputs,
            "segments": segments,
            "consolidation": _json_safe(dict(consolidation or {})) or None,
            "artifact_validation": None,
            "reasoning_report": None,
            "promotion_gate": None,
            "metadata": _json_safe(dict(metadata or {})),
        }
        self._write_path(path_record)
        self._write_ref("candidate", path_record)
        self._append_log(
            {
                "event": "add_candidate_path",
                "path_id": path_id,
                "run_id": run_id,
                "node_count": len(node_ids),
                "original_node_count": original_action_count,
                "consolidated": selected_step_ids is not None,
            }
        )
        return self._path_metadata(path_record)

    def promote_path(
        self,
        path_id: str,
        *,
        artifact_validation: Mapping[str, Any],
        reasoning_report: Mapping[str, Any],
    ) -> Dict[str, Any]:
        path_record = self.load_path(path_id)
        gate = self.evaluate_promotion_gate(
            path_record,
            artifact_validation=artifact_validation,
            reasoning_report=reasoning_report,
        )
        if not gate.passed:
            path_record["promotion_gate"] = gate.to_dict()
            path_record["updated_at"] = _utc_iso()
            self._write_path(path_record)
            self._append_log(
                {
                    "event": "promote_rejected",
                    "path_id": path_id,
                    "reasons": gate.reasons,
                }
            )
            raise MemoryPromotionGateError("; ".join(gate.reasons))

        old_status = str(path_record.get("status") or "candidate")
        path_record["status"] = ACTIVE_MEMORY_STATUS
        path_record["updated_at"] = _utc_iso()
        path_record["promoted_at"] = path_record["updated_at"]
        path_record["artifact_validation"] = _json_safe(dict(artifact_validation))
        path_record["reasoning_report"] = _json_safe(dict(reasoning_report))
        path_record["promotion_gate"] = gate.to_dict()
        path_record["query_keys"] = _normalize_query_keys(
            list(path_record.get("query_keys") or []) + list(reasoning_report.get("query_keys") or [])
        )
        self._write_path(path_record)
        self._remove_ref(old_status, path_id)
        self._write_ref(ACTIVE_MEMORY_STATUS, path_record)
        self._update_query_index(path_record)
        self._append_log(
            {
                "event": "promote_path",
                "path_id": path_id,
                "from_status": old_status,
                "to_status": ACTIVE_MEMORY_STATUS,
            }
        )
        return self._path_metadata(path_record)

    def evaluate_promotion_gate(
        self,
        path_record: Mapping[str, Any],
        *,
        artifact_validation: Mapping[str, Any],
        reasoning_report: Mapping[str, Any],
    ) -> PromotionGateResult:
        reasons: List[str] = []
        if str(path_record.get("status") or "") not in {"candidate", "runtime_complete", "repaired"}:
            reasons.append("path is not in a promotable status")
        if not path_record.get("node_ids"):
            reasons.append("path has no action nodes")
        for node_id in path_record.get("node_ids") or []:
            node = self.load_node(str(node_id))
            if str(node.get("step_status") or "") != "ok":
                reasons.append(f"node {node_id} did not execute successfully")
            if str(node.get("action", {}).get("action") or "") not in SUPPORTED_GUI_WRAPPER_ACTIONS:
                reasons.append(f"node {node_id} uses an unsupported action")

        if not _artifact_validation_passed(artifact_validation):
            reasons.append("artifact validation did not pass")
        if not str(artifact_validation.get("evaluator") or artifact_validation.get("evaluator_id") or "").strip():
            reasons.append("artifact validation must name an evaluator")

        if not bool(reasoning_report.get("viable")):
            reasons.append("reasoning report did not mark the path viable")
        if str(reasoning_report.get("promotion_recommendation") or "").strip().lower() not in {
            "promote",
            "active",
            "use",
        }:
            reasons.append("reasoning report did not recommend promotion")
        if not str(reasoning_report.get("task_intent") or "").strip():
            reasons.append("reasoning report is missing task_intent")
        if not _normalize_query_keys(reasoning_report.get("query_keys") or []):
            reasons.append("reasoning report is missing query_keys")
        if not _list_of_strings(reasoning_report.get("preconditions")):
            reasons.append("reasoning report is missing preconditions")
        if not _list_of_strings(reasoning_report.get("effects")):
            reasons.append("reasoning report is missing effects")
        blockers = reasoning_report.get("blockers")
        if isinstance(blockers, list) and blockers:
            reasons.append("reasoning report contains blockers")

        return PromotionGateResult(
            passed=not reasons,
            reasons=reasons,
            artifact_validation=dict(artifact_validation),
            reasoning_report=dict(reasoning_report),
        )

    def query_active_paths(
        self,
        query: str,
        *,
        limit: int = 5,
        min_intent_score: float = DEFAULT_TASK_INTENT_MIN_SCORE,
    ) -> List[Dict[str, Any]]:
        query_text = str(query or "").strip()
        if not _intent_terms(query_text):
            return []
        scored: List[Dict[str, Any]] = []
        for path_record in self._iter_active_path_records():
            intent_match = score_task_intent_match(query_text, path_record)
            if float(intent_match.get("score") or 0.0) < float(min_intent_score):
                continue
            metadata = self._path_metadata(path_record)
            metadata["intent_score"] = intent_match["score"]
            metadata["matched_query_key"] = query_text
            metadata["matched_on"] = "task_intent"
            metadata["retrieval"] = intent_match
            metadata["promoted_at"] = path_record.get("promoted_at")
            metadata["updated_at"] = path_record.get("updated_at")
            scored.append(metadata)
        scored.sort(
            key=lambda item: (
                float(item.get("intent_score") or 0.0),
                str(item.get("promoted_at") or item.get("updated_at") or ""),
                str(item.get("path_id") or ""),
            ),
            reverse=True,
        )
        return scored[: max(0, int(limit))]

    def query_active_paths_exact(self, query: str, *, limit: int = 5) -> List[Dict[str, Any]]:
        replay_key = normalize_replay_key(query)
        index = self._read_query_index()
        path_ids = list(index.get(replay_key, []))
        results: List[Dict[str, Any]] = []
        for path_id in path_ids:
            try:
                path_record = self.load_path(str(path_id))
            except MemoryRepositoryError:
                continue
            if str(path_record.get("status")) != ACTIVE_MEMORY_STATUS:
                continue
            results.append(self._path_metadata(path_record))
            if len(results) >= int(limit):
                break
        return results

    def diff_paths(self, left_path_id: str, right_path_id: str) -> Dict[str, Any]:
        left = self.load_path(left_path_id)
        right = self.load_path(right_path_id)
        left_nodes = [str(node_id) for node_id in left.get("node_ids") or []]
        right_nodes = [str(node_id) for node_id in right.get("node_ids") or []]
        shared_prefix = 0
        for left_node, right_node in zip(left_nodes, right_nodes):
            if left_node != right_node:
                break
            shared_prefix += 1
        return {
            "left_path_id": left_path_id,
            "right_path_id": right_path_id,
            "shared_prefix_length": shared_prefix,
            "left_divergent_node_ids": left_nodes[shared_prefix:],
            "right_divergent_node_ids": right_nodes[shared_prefix:],
        }

    def export_path_to_replay_task(
        self,
        path_id: str,
        *,
        goal: str,
        summary: str = "",
        verify_after_action: bool = True,
        verify_goal_completion: bool = True,
    ) -> Dict[str, Any]:
        path_record = self.load_path(path_id)
        actions = []
        for node_id in path_record.get("node_ids") or []:
            node = self.load_node(str(node_id))
            action = node.get("action")
            if isinstance(action, Mapping):
                intent = node.get("intent") if isinstance(node.get("intent"), Mapping) else {}
                actions.append({**{k: v for k, v in intent.items() if v}, **dict(action)})
        if not actions:
            raise MemoryRepositoryError("Cannot export a path with no actions.")
        return {
            "type": "desktop_task",
            "summary": str(summary or path_record.get("task_intent") or goal).strip(),
            "goal": str(goal).strip(),
            "payload": {
                "actions": actions,
                SEGMENTS_KEY: _json_safe(path_record.get("segments", [])),
                "memory_flexible_action_inputs": _json_safe(
                    path_record.get("flexible_action_inputs") or []
                ),
                "verify_after_action": bool(verify_after_action),
                "verify_goal_completion": bool(verify_goal_completion),
                "step_verification_failure_policy": "advisory",
            },
        }

    def build_reasoning_prompt(self, path_id: str) -> str:
        path_record = self.load_path(path_id)
        nodes = [self.load_node(str(node_id)) for node_id in path_record.get("node_ids") or []]
        compact_nodes = [
            {
                "node_id": node.get("node_id"),
                "action": node.get("action"),
                "component_signature": node.get("component_signature"),
                "step_status": node.get("step_status"),
            }
            for node in nodes
        ]
        payload = {
            "path_id": path_id,
            "task_intent": path_record.get("task_intent"),
            "query_keys": path_record.get("query_keys"),
            "application": path_record.get("application"),
            "actions": compact_nodes,
        }
        return (
            "Assess whether this GUI memory path should be promoted to active replay memory. "
            "Return JSON with task_intent, query_keys, preconditions, effects, viable, "
            "promotion_recommendation, blockers, and a short rationale. Mark viable true only "
            "when the selected action path is complete enough to replay the stated task under "
            "the listed preconditions. Recommend promotion only when artifact validation and "
            "the selected actions support deterministic replay; otherwise include concrete "
            "blockers.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        )

    def list_paths(
        self,
        *,
        statuses: Optional[Sequence[str]] = None,
        limit: int = 0,
    ) -> List[Dict[str, Any]]:
        """Metadata for stored paths, newest first, optionally filtered by status."""
        wanted = {str(status).strip().lower() for status in statuses or [] if str(status).strip()}
        records: List[Dict[str, Any]] = []
        for path in sorted(self.path_directory.glob("*.json")):
            try:
                payload = _read_json(path)
            except MemoryRepositoryError:
                continue
            if not isinstance(payload, Mapping):
                continue
            if wanted and str(payload.get("status") or "").strip().lower() not in wanted:
                continue
            records.append(self._path_metadata(payload))
        records.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        if limit > 0:
            records = records[:limit]
        return records

    def set_path_status(
        self,
        path_id: str,
        status: str,
        *,
        reason: str = "",
    ) -> Dict[str, Any]:
        """Move a path to another lifecycle status (e.g. deprecate a memory).

        Leaving ``active`` also removes the path from the query index so
        retrieval stops recalling it immediately.
        """
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in PATH_REF_STATUSES:
            raise MemoryRepositoryError(
                f"Unsupported memory path status '{status}'. "
                f"Expected one of: {', '.join(sorted(PATH_REF_STATUSES))}."
            )

        path_record = self.load_path(path_id)
        old_status = str(path_record.get("status") or "")
        path_record["status"] = normalized_status
        path_record["updated_at"] = _utc_iso()
        if reason:
            path_record["status_reason"] = str(reason)

        self._write_json(
            self.path_directory / f"{_sanitize_id(path_id)}.json", path_record
        )
        self._remove_ref(old_status, path_id)
        self._write_ref(normalized_status, path_record)
        if old_status == ACTIVE_MEMORY_STATUS and normalized_status != ACTIVE_MEMORY_STATUS:
            self._remove_from_query_index(path_id)
        self._append_log(
            {
                "event": "path_status_changed",
                "path_id": path_id,
                "from": old_status,
                "to": normalized_status,
                "reason": reason,
            }
        )
        return self._path_metadata(path_record)

    def _remove_from_query_index(self, path_id: str) -> None:
        index = self._read_query_index()
        changed = False
        for key in list(index.keys()):
            values = [value for value in index[key] if value != path_id]
            if len(values) != len(index[key]):
                changed = True
                if values:
                    index[key] = values
                else:
                    del index[key]
        if changed:
            self._write_json(self.index_directory / "query_keys.json", index)

    def load_path(self, path_id: str) -> Dict[str, Any]:
        path = self.path_directory / f"{_sanitize_id(path_id)}.json"
        if not path.exists():
            raise MemoryRepositoryError(f"Memory path '{path_id}' does not exist.")
        payload = _read_json(path)
        if not isinstance(payload, Mapping):
            raise MemoryRepositoryError(f"Memory path '{path_id}' is invalid.")
        return dict(payload)

    def load_node(self, node_id: str) -> Dict[str, Any]:
        path = self.node_directory / f"{_sanitize_id(node_id)}.json"
        if not path.exists():
            raise MemoryRepositoryError(f"Memory node '{node_id}' does not exist.")
        payload = _read_json(path)
        if not isinstance(payload, Mapping):
            raise MemoryRepositoryError(f"Memory node '{node_id}' is invalid.")
        return dict(payload)

    def _node_from_wrapper_step(
        self,
        step: Mapping[str, Any],
        *,
        run_id: str,
        path_id: str,
        run_directory: Path,
    ) -> Dict[str, Any]:
        action = _action_from_step(step)
        action_signature = _action_signature(action)
        component_signature = _component_signature(step, action)
        node_id = _node_id(action_signature, component_signature)
        visual_evidence = self._store_step_visual_evidence(
            step,
            action=action,
            run_id=run_id,
            path_id=path_id,
            run_directory=run_directory,
        )
        node_path = self.node_directory / f"{node_id}.json"
        if node_path.exists():
            node = _read_json(node_path)
        else:
            node = {
                "record_type": "memory_action_node",
                "schema_version": MEMORY_REPO_SCHEMA_VERSION,
                "node_id": node_id,
                "created_at": _utc_iso(),
                "action_signature": action_signature,
                "component_signature": component_signature,
                "action": action,
                "step_status": str(step.get("status") or ""),
                "precondition": _infer_step_precondition(action),
                "postcondition": _infer_step_postcondition(action),
                "visual_evidence": [],
                "source_steps": [],
                "child_node_ids": [],
                "reuse_count": 0,
            }
        node["updated_at"] = _utc_iso()
        # Last write wins: a failing rerun must be able to downgrade a node
        # that once succeeded, or the promotion gate checks stale state.
        node["step_status"] = str(step.get("status") or node.get("step_status") or "")
        if not node.get("precondition"):
            node["precondition"] = _infer_step_precondition(action)
        if not node.get("postcondition"):
            node["postcondition"] = _infer_step_postcondition(action)
        node["reuse_count"] = int(node.get("reuse_count") or 0) + 1
        source = step.get("source") if isinstance(step.get("source"), Mapping) else {}
        intent = {key: str(source.get(key)).strip() for key in ("target_description", "expected_outcome")
                  if str(source.get(key) or "").strip()}
        if intent:
            # What the step was for, as the planner described it: lets a replay find the
            # element by role when its recorded text or picture no longer matches.
            node["intent"] = intent
        node["visual_evidence"] = _merge_unique_dicts(
            list(node.get("visual_evidence") or []),
            visual_evidence,
            key="blob_id",
        )
        node["source_steps"] = _merge_unique_dicts(
            list(node.get("source_steps") or []),
            [
                {
                    "source_key": f"{run_id}:{path_id}:{step.get('step_id') or ''}",
                    "run_id": run_id,
                    "path_id": path_id,
                    "step_id": str(step.get("step_id") or ""),
                    "sequence": step.get("sequence"),
                    "source_run_directory": str(run_directory),
                }
            ],
            key="source_key",
        )
        self._write_json(node_path, node)
        return dict(node)

    def _store_step_visual_evidence(
        self,
        step: Mapping[str, Any],
        *,
        action: Mapping[str, Any],
        run_id: str,
        path_id: str,
        run_directory: Path,
    ) -> List[Dict[str, Any]]:
        evidence: List[Dict[str, Any]] = []
        for phase in ("before_observation", "after_observation"):
            observation = step.get(phase)
            if not isinstance(observation, Mapping):
                continue
            source_path = _resolve_wrapper_evidence_path(
                observation.get("path"),
                run_directory,
            )
            if source_path is None:
                continue
            blob = self._store_blob(
                source_path,
                source={
                    "run_id": run_id,
                    "path_id": path_id,
                    "step_id": str(step.get("step_id") or ""),
                    "phase": phase,
                    "run_directory": str(run_directory),
                },
                metadata={
                    "kind": "full_screenshot",
                    "image_size": observation.get("image_size"),
                    "region": observation.get("region"),
                    "mouse_position": observation.get("mouse_position"),
                    "capture_verified": observation.get("capture_verified"),
                },
            )
            if blob is not None:
                evidence.append(blob)
            for crop in self._store_component_crop_blobs(
                source_path,
                action=action,
                step=step,
                observation=observation,
                source={
                    "run_id": run_id,
                    "path_id": path_id,
                    "step_id": str(step.get("step_id") or ""),
                    "phase": phase,
                    "run_directory": str(run_directory),
                    "derived_from": "component_crop",
                },
            ):
                evidence.append(crop)
        return evidence

    def _store_component_crop_blobs(
        self,
        source_path: Path,
        *,
        action: Mapping[str, Any],
        step: Mapping[str, Any],
        observation: Mapping[str, Any],
        source: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        variants = (
            ("target", 128, 96),
            ("context", 192, 144),
            ("wide_context", 256, 192),
        )
        crops: List[Dict[str, Any]] = []
        for variant, crop_width, crop_height in variants:
            crop = self._store_component_crop_blob(
                source_path,
                action=action,
                step=step,
                observation=observation,
                source=source,
                crop_width=crop_width,
                crop_height=crop_height,
                crop_variant=variant,
            )
            if crop is not None:
                crops.append(crop)
        return crops

    def _store_component_crop_blob(
        self,
        source_path: Path,
        *,
        action: Mapping[str, Any],
        step: Mapping[str, Any],
        observation: Mapping[str, Any],
        source: Mapping[str, Any],
        crop_width: int,
        crop_height: int,
        crop_variant: str,
    ) -> Optional[Dict[str, Any]]:
        if Image is None:
            return None
        point = _action_point_in_image(step=step, action=action, observation=observation)
        if point is None:
            return None
        source_path = source_path.expanduser().resolve()
        if not source_path.exists() or not source_path.is_file():
            return None

        try:
            with Image.open(source_path) as image:
                image_width, image_height = image.size
                center_x, center_y = point
                left = max(0, int(round(center_x - crop_width / 2)))
                top = max(0, int(round(center_y - crop_height / 2)))
                right = min(image_width, left + int(crop_width))
                bottom = min(image_height, top + int(crop_height))
                left = max(0, min(left, max(right - int(crop_width), 0)))
                top = max(0, min(top, max(bottom - int(crop_height), 0)))
                if right <= left or bottom <= top:
                    return None
                crop = image.crop((left, top, right, bottom))
                buffer = BytesIO()
                crop.save(buffer, format="PNG")
        except Exception:
            return None

        data = buffer.getvalue()
        digest = hashlib.sha256(data).hexdigest()
        target_directory = self.blob_directory / digest[:2]
        target_path = target_directory / f"{digest}.png"
        target_directory.mkdir(parents=True, exist_ok=True)
        if not target_path.exists():
            target_path.write_bytes(data)
        metadata = {
            "kind": "component_crop",
            "parent_image_path": str(source_path),
            "parent_image_sha256": _sha256_file(source_path),
            "image_size": {"width": right - left, "height": bottom - top},
            "crop_box": {"left": left, "top": top, "right": right, "bottom": bottom},
            "crop_variant": str(crop_variant),
            "action_point": {"x": int(round(center_x)), "y": int(round(center_y))},
            "action": _json_safe(dict(action)),
        }
        blob = {
            "blob_id": digest,
            "path": str(target_path.resolve()),
            "source_path": str(source_path),
            "size_bytes": int(target_path.stat().st_size),
            "sha256": digest,
            "source": _json_safe(dict(source)),
            "metadata": _json_safe(metadata),
        }
        blob_record_path = target_directory / f"{digest}.json"
        if not blob_record_path.exists():
            self._write_json(blob_record_path, {"record_type": "memory_blob", **blob})
        return blob

    def _store_blob(
        self,
        source_path: Path,
        *,
        source: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> Optional[Dict[str, Any]]:
        source_path = source_path.expanduser().resolve()
        if not source_path.exists() or not source_path.is_file():
            return None
        digest = _sha256_file(source_path)
        suffix = source_path.suffix.lower() or ".bin"
        target_directory = self.blob_directory / digest[:2]
        target_path = target_directory / f"{digest}{suffix}"
        target_directory.mkdir(parents=True, exist_ok=True)
        if not target_path.exists():
            shutil.copy2(source_path, target_path)
        blob = {
            "blob_id": digest,
            "path": str(target_path.resolve()),
            "source_path": str(source_path),
            "size_bytes": int(target_path.stat().st_size),
            "sha256": digest,
            "source": _json_safe(dict(source)),
            "metadata": _json_safe(dict(metadata)),
        }
        blob_record_path = target_directory / f"{digest}.json"
        if not blob_record_path.exists():
            self._write_json(blob_record_path, {"record_type": "memory_blob", **blob})
        return blob

    def _link_child(self, parent_node_id: str, child_node_id: str) -> None:
        parent = self.load_node(parent_node_id)
        children = [str(value) for value in parent.get("child_node_ids") or []]
        if child_node_id not in children:
            children.append(child_node_id)
            parent["child_node_ids"] = children
            parent["updated_at"] = _utc_iso()
            self._write_json(self.node_directory / f"{parent_node_id}.json", parent)

    def _path_metadata(self, path_record: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "path_id": path_record.get("path_id"),
            "status": path_record.get("status"),
            "platform": path_record.get("platform"),
            "created_at": path_record.get("created_at"),
            "promoted_at": path_record.get("promoted_at"),
            "task_intent": path_record.get("task_intent"),
            "query_keys": list(path_record.get("query_keys") or []),
            "application": path_record.get("application"),
            "action_count": path_record.get("action_count"),
            "original_action_count": path_record.get("original_action_count"),
            "flexible_action_inputs": _json_safe(path_record.get("flexible_action_inputs") or []),
            "segments": _json_safe(path_record.get("segments") or []),
            "consolidation": _json_safe(path_record.get("consolidation")),
            "node_ids": list(path_record.get("node_ids") or []),
            "source": _json_safe(path_record.get("source")),
        }

    def _ensure_layout(self) -> None:
        for directory in (
            self.blob_directory,
            self.node_directory,
            self.path_directory,
            self.index_directory,
            self.log_path.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        for status in PATH_REF_STATUSES:
            (self.ref_directory / status).mkdir(parents=True, exist_ok=True)

    def _write_path(self, path_record: Mapping[str, Any]) -> None:
        path_id = _sanitize_id(str(path_record.get("path_id") or ""))
        if not path_id:
            raise MemoryRepositoryError("Path record is missing path_id.")
        self._active_path_records_cache = None
        self._write_json(self.path_directory / f"{path_id}.json", path_record)

    def _write_ref(self, status: str, path_record: Mapping[str, Any]) -> None:
        status = str(status)
        if status not in PATH_REF_STATUSES:
            raise MemoryRepositoryError(f"Unsupported memory ref status '{status}'.")
        path_id = _sanitize_id(str(path_record.get("path_id") or ""))
        payload = {
            "record_type": "memory_ref",
            "schema_version": MEMORY_REPO_SCHEMA_VERSION,
            "status": status,
            "path_id": path_id,
            "path": str((self.path_directory / f"{path_id}.json").resolve()),
            "updated_at": _utc_iso(),
        }
        self._active_path_records_cache = None
        self._write_json(self.ref_directory / status / f"{path_id}.json", payload)

    def _remove_ref(self, status: str, path_id: str) -> None:
        status = str(status)
        if status not in PATH_REF_STATUSES:
            return
        ref_path = self.ref_directory / status / f"{_sanitize_id(path_id)}.json"
        if ref_path.exists():
            self._active_path_records_cache = None
            ref_path.unlink()

    def _read_query_index(self) -> Dict[str, List[str]]:
        path = self.index_directory / "query_keys.json"
        if not path.exists():
            return {}
        payload = _read_json(path)
        if not isinstance(payload, Mapping):
            return {}
        result: Dict[str, List[str]] = {}
        for key, values in payload.items():
            if isinstance(values, list):
                result[str(key)] = [str(value) for value in values]
        return result

    def _update_query_index(self, path_record: Mapping[str, Any]) -> None:
        index = self._read_query_index()
        path_id = str(path_record.get("path_id") or "")
        for key in _normalize_query_keys(path_record.get("query_keys") or []):
            values = [value for value in index.get(key, []) if value != path_id]
            values.insert(0, path_id)
            index[key] = values
        self._write_json(self.index_directory / "query_keys.json", index)

    def _iter_active_path_records(self) -> List[Dict[str, Any]]:
        if self._active_path_records_cache is not None:
            return [dict(record) for record in self._active_path_records_cache]
        records: List[Dict[str, Any]] = []
        for ref_path in sorted((self.ref_directory / ACTIVE_MEMORY_STATUS).glob("*.json")):
            try:
                ref = _read_json(ref_path)
                path_record = self.load_path(str(ref.get("path_id") or ""))
            except MemoryRepositoryError:
                continue
            if str(path_record.get("status") or "") == ACTIVE_MEMORY_STATUS:
                records.append(path_record)
        self._active_path_records_cache = [dict(record) for record in records]
        return records

    def _append_log(self, payload: Mapping[str, Any]) -> None:
        event = {"timestamp": _utc_iso(), **_json_safe(dict(payload))}
        line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def _write_json(self, path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(_json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True)
        with path.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MemoryRepositoryError(f"Could not read JSON file '{path}': {error}") from error
    if not isinstance(payload, Mapping):
        raise MemoryRepositoryError(f"JSON file '{path}' did not contain an object.")
    return dict(payload)


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise MemoryRepositoryError(f"Could not read JSONL file '{path}': {error}") from error
    records: List[Dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise MemoryRepositoryError(f"Invalid JSONL record in '{path}': {error}") from error
        if isinstance(payload, Mapping):
            records.append(dict(payload))
    return records


def _resolve_wrapper_evidence_path(value: Any, run_directory: Path) -> Optional[Path]:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if path.is_file():
        return path.resolve()
    relocated = run_directory / "screenshots" / path.name
    if relocated.is_file():
        return relocated.resolve()
    return None


def _select_wrapper_steps(
    steps: Sequence[Mapping[str, Any]],
    selected_step_ids: Sequence[str],
) -> List[Dict[str, Any]]:
    selected = [str(step_id) for step_id in selected_step_ids]
    if not selected:
        raise MemoryRepositoryError("Consolidated wrapper trajectory selected no steps.")
    if len(selected) != len(set(selected)):
        raise MemoryRepositoryError("Consolidated wrapper trajectory contains duplicate steps.")

    by_id = {
        str(step.get("step_id") or ""): dict(step)
        for step in steps
    }
    unknown = [step_id for step_id in selected if step_id not in by_id]
    if unknown:
        raise MemoryRepositoryError(
            "Consolidated wrapper trajectory selected unknown steps: " + ", ".join(unknown)
        )

    original_order = [str(step.get("step_id") or "") for step in steps]
    positions = {step_id: index for index, step_id in enumerate(original_order)}
    if selected != sorted(selected, key=positions.__getitem__):
        raise MemoryRepositoryError("Consolidated wrapper trajectory reordered steps.")
    return [by_id[step_id] for step_id in selected]


def _normalize_flexible_action_inputs(
    value: Any,
    *,
    steps: Sequence[Mapping[str, Any]],
    step_node_index: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise MemoryRepositoryError("Flexible action inputs must be a list.")

    step_lookup = {
        str(step.get("step_id") or ""): step
        for step in steps
        if str(step.get("step_id") or "")
    }
    node_lookup = {
        str(item.get("step_id") or ""): dict(item)
        for item in step_node_index
        if str(item.get("step_id") or "")
    }
    normalized: List[Dict[str, Any]] = []
    seen = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise MemoryRepositoryError("Flexible action input entries must be objects.")
        step_id = str(item.get("step_id") or "").strip()
        step = step_lookup.get(step_id)
        index_item = node_lookup.get(step_id)
        if step is None or index_item is None:
            raise MemoryRepositoryError(f"Flexible action input references unknown step '{step_id}'.")
        action = _action_from_step(step)
        parameter_path = _normalize_parameter_path(item.get("parameter_path"))
        original_value = _value_at_path(action, parameter_path)
        if original_value is None:
            raise MemoryRepositoryError(
                f"Flexible action input references missing action parameter {parameter_path!r} on {step_id}."
            )
        action_name = str(action.get("action") or "").strip().lower()
        if action_name not in {"type", "paste_text"} or parameter_path != ["text"]:
            raise MemoryRepositoryError(
                "Flexible action inputs currently support only type.text and paste_text.text."
            )
        key = (int(index_item["action_index"]), tuple(parameter_path))
        if key in seen:
            raise MemoryRepositoryError(
                f"Duplicate flexible action input for action {index_item['action_index']}."
            )
        seen.add(key)
        normalized.append(
            {
                "step_id": step_id,
                "node_id": str(index_item.get("node_id") or ""),
                "action_index": int(index_item["action_index"]),
                "action": action_name,
                "parameter_path": parameter_path,
                "input_kind": str(item.get("input_kind") or "input_text").strip() or "input_text",
                "description": str(item.get("description") or "").strip(),
                "rebind_when": str(item.get("rebind_when") or "").strip(),
                "constraints": [
                    str(constraint).strip()
                    for constraint in item.get("constraints") or []
                    if str(constraint).strip()
                ],
                "original_value": _json_safe(original_value),
            }
        )
    return normalized


def _normalize_parameter_path(value: Any) -> List[str]:
    if isinstance(value, str):
        path = [part for part in value.split(".") if part]
    elif isinstance(value, list):
        path = [str(part).strip() for part in value if str(part).strip()]
    else:
        path = []
    if not path:
        raise MemoryRepositoryError("Flexible action input parameter_path cannot be empty.")
    return path


def _value_at_path(payload: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def _action_from_step(step: Mapping[str, Any]) -> Dict[str, Any]:
    action_record = step.get("action")
    if not isinstance(action_record, Mapping):
        raise MemoryRepositoryError("Wrapper step is missing an action record.")
    action_name = str(action_record.get("name") or action_record.get("action") or "").strip()
    if not action_name:
        raise MemoryRepositoryError("Wrapper step action is missing a name.")
    parameters = action_record.get("parameters")
    if not isinstance(parameters, Mapping):
        parameters = {}
    return {"action": action_name, **_json_safe(dict(parameters))}


def _action_signature(action: Mapping[str, Any]) -> str:
    name = str(action.get("action") or "").strip().lower()
    payload = {"action": name}
    for key, value in sorted(action.items()):
        if key == "action":
            continue
        if name in {"type", "paste_text"} and key == "text":
            payload[key] = {"sha256": hashlib.sha256(str(value).encode("utf-8")).hexdigest()}
        else:
            payload[key] = value
    return _stable_digest(payload)


def _component_signature(step: Mapping[str, Any], action: Mapping[str, Any]) -> str:
    name = str(action.get("action") or "").strip().lower()
    if name == "hotkey":
        return "keyboard:" + "+".join(str(key).lower() for key in action.get("keys", []))
    if name in {"type", "paste_text", "press"}:
        return f"focused-control:{name}"
    if name in {"click", "move"}:
        return f"point:{action.get('x')}:{action.get('y')}"
    if name == "scroll":
        return f"scroll-region:{action.get('x')}:{action.get('y')}"
    if name == "wait":
        return "runtime-wait"
    coordinate_context = step.get("coordinate_context")
    if isinstance(coordinate_context, Mapping):
        return _stable_digest(coordinate_context)
    return name


def _infer_step_precondition(action: Mapping[str, Any]) -> str:
    name = str(action.get("action") or "").strip().lower()
    if name in {"click", "move", "scroll"}:
        return "The target region is visible and not hidden by a transient dialog."
    if name in {"type", "paste_text"}:
        return "The intended text field or editable document area has focus."
    if name in {"press", "hotkey"}:
        return "The active application is focused and ready for keyboard input."
    if name == "wait":
        return "A previously requested UI transition is in progress."
    return "The desktop is in the expected state for this replay action."


def _infer_step_postcondition(action: Mapping[str, Any]) -> str:
    name = str(action.get("action") or "").strip().lower()
    if name == "click":
        return "The click changes focus, opens the target UI, or advances the visible task state."
    if name == "move":
        return "The pointer is positioned over the intended target."
    if name in {"type", "paste_text"}:
        return "The requested text is present in the focused field or document."
    if name in {"press", "hotkey"}:
        return "The active application visibly responds to the keyboard input."
    if name == "scroll":
        return "The target view scrolls in the intended direction."
    if name == "wait":
        return "The awaited UI state becomes visible."
    return "The visible desktop state advances as intended."


def _action_point_in_image(
    *,
    step: Mapping[str, Any],
    action: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> Optional[tuple[float, float]]:
    name = str(action.get("action") or "").strip().lower()
    if name not in {"click", "move", "scroll"}:
        return None
    try:
        action_x = float(action["x"])
        action_y = float(action["y"])
    except (KeyError, TypeError, ValueError):
        return None

    coordinate_context = step.get("coordinate_context")
    if not isinstance(coordinate_context, Mapping):
        coordinate_context = {}
    scale_x = _float_or_none(coordinate_context.get("scale_x")) or 1.0
    scale_y = _float_or_none(coordinate_context.get("scale_y")) or 1.0
    region = observation.get("region") or coordinate_context.get("region")
    region_left, region_top = _region_origin(region)
    image_size = observation.get("image_size")
    image_width = None
    image_height = None
    if isinstance(image_size, Mapping):
        image_width = _float_or_none(image_size.get("width"))
        image_height = _float_or_none(image_size.get("height"))

    point_x = (action_x - region_left) * scale_x
    point_y = (action_y - region_top) * scale_y
    if image_width is not None:
        point_x = max(0.0, min(point_x, max(image_width - 1, 0.0)))
    if image_height is not None:
        point_y = max(0.0, min(point_y, max(image_height - 1, 0.0)))
    return point_x, point_y


def _region_origin(region: Any) -> tuple[float, float]:
    if isinstance(region, Mapping):
        left = _float_or_none(region.get("left", region.get("x", 0.0))) or 0.0
        top = _float_or_none(region.get("top", region.get("y", 0.0))) or 0.0
        return left, top
    if isinstance(region, SequenceABC) and not isinstance(region, (str, bytes)) and len(region) >= 2:
        left = _float_or_none(region[0]) or 0.0
        top = _float_or_none(region[1]) or 0.0
        return left, top
    return 0.0, 0.0


def _node_id(action_signature: str, component_signature: str) -> str:
    return "mem_node_" + _stable_digest(
        {"action_signature": action_signature, "component_signature": component_signature}
    )[:24]


def _stable_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_validation_passed(payload: Mapping[str, Any]) -> bool:
    status = str(payload.get("status") or "").strip().lower()
    return bool(payload.get("passed")) or status in {"pass", "passed", "success", "succeeded"}


def _normalize_query_keys(values: Any) -> List[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, SequenceABC):
        return []
    result: List[str] = []
    for value in values:
        normalized = normalize_replay_key(str(value or ""))
        if normalized and normalized not in result:
            result.append(normalized)
    return result


_INTENT_TOKEN_RE = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)?", re.IGNORECASE)
_INTENT_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "been",
    "by",
    "can",
    "could",
    "do",
    "does",
    "for",
    "from",
    "get",
    "have",
    "help",
    "i",
    "in",
    "into",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "please",
    "task",
    "the",
    "this",
    "to",
    "use",
    "using",
    "want",
    "with",
    "you",
}
_INTENT_CANONICAL_TOKENS = {
    "added": "add",
    "adding": "add",
    "append": "add",
    "appended": "add",
    "browser": "chrome",
    "browsing": "browse",
    "calculating": "calculate",
    "calculation": "calculate",
    "closed": "close",
    "closing": "close",
    "computed": "calculate",
    "computing": "calculate",
    "configured": "configure",
    "configuring": "configure",
    "copied": "copy",
    "copying": "copy",
    "created": "create",
    "creating": "create",
    "deleted": "delete",
    "deleting": "delete",
    "duplicate": "copy",
    "duplicated": "copy",
    "edit": "modify",
    "edited": "modify",
    "editing": "modify",
    "erased": "delete",
    "erasing": "delete",
    "filled": "fill",
    "filling": "fill",
    "find": "search",
    "finding": "search",
    "formatted": "format",
    "formatting": "format",
    "insert": "add",
    "inserted": "add",
    "inserting": "add",
    "locate": "search",
    "located": "search",
    "lookup": "search",
    "make": "create",
    "modified": "modify",
    "modifying": "modify",
    "opened": "open",
    "opening": "open",
    "ordered": "sort",
    "ordering": "sort",
    "overwrite": "replace",
    "overwritten": "replace",
    "pasted": "paste",
    "pasting": "paste",
    "populate": "fill",
    "populated": "fill",
    "renamed": "rename",
    "renaming": "rename",
    "replaced": "replace",
    "replacement": "replace",
    "replacing": "replace",
    "saved": "save",
    "saving": "save",
    "searched": "search",
    "searching": "search",
    "set": "configure",
    "setting": "configure",
    "sorted": "sort",
    "sorting": "sort",
    "store": "save",
    "stored": "save",
    "storing": "save",
    "substitute": "replace",
    "substituted": "replace",
    "swapped": "replace",
    "transform": "convert",
    "transformed": "convert",
    "vscode": "code",
    "workbook": "spreadsheet",
    "worksheet": "spreadsheet",
}


def score_task_intent_match(query: str, path_record: Mapping[str, Any]) -> Dict[str, Any]:
    query_text = str(query or "").strip()
    query_terms = _intent_terms(query_text)
    if not query_terms:
        return {
            "score": 0.0,
            "query_terms": [],
            "matched_terms": [],
            "matched_text": None,
        }

    best: Dict[str, Any] = {
        "score": 0.0,
        "query_terms": query_terms,
        "matched_terms": [],
        "matched_text": None,
    }
    for candidate_text in _path_intent_texts(path_record):
        score_payload = _score_intent_texts(query_text, candidate_text)
        if float(score_payload.get("score") or 0.0) > float(best.get("score") or 0.0):
            best = score_payload
    return best


def _path_intent_texts(path_record: Mapping[str, Any]) -> List[str]:
    texts: List[str] = []
    for value in (
        path_record.get("task_intent"),
        path_record.get("application"),
    ):
        text = str(value or "").strip()
        if text:
            texts.append(text)
    for value in path_record.get("query_keys") or []:
        text = str(value or "").strip()
        if text and _intent_terms(text):
            texts.append(text)
    reasoning = path_record.get("reasoning_report")
    if isinstance(reasoning, Mapping):
        for value in (reasoning.get("task_intent"), reasoning.get("rationale")):
            text = str(value or "").strip()
            if text:
                texts.append(text)
        for key in ("preconditions", "effects"):
            values = reasoning.get(key)
            if isinstance(values, SequenceABC) and not isinstance(values, (str, bytes)):
                for value in values:
                    text = str(value or "").strip()
                    if text:
                        texts.append(text)
    combined = " ".join(texts)
    return [combined, *texts] if combined else []


def _score_intent_texts(query: str, candidate: str) -> Dict[str, Any]:
    query_terms = _intent_terms(query)
    candidate_terms = _intent_terms(candidate)
    if not query_terms or not candidate_terms:
        return {
            "score": 0.0,
            "query_terms": query_terms,
            "matched_terms": [],
            "matched_text": candidate,
        }

    query_counts = _term_counts(query_terms)
    candidate_counts = _term_counts(candidate_terms)
    overlap = sum(
        min(query_counts[term], candidate_counts.get(term, 0))
        for term in query_counts
    )
    if overlap <= 0:
        return {
            "score": 0.0,
            "query_terms": query_terms,
            "matched_terms": [],
            "matched_text": candidate,
        }

    query_total = sum(query_counts.values())
    candidate_total = sum(candidate_counts.values())
    coverage = overlap / max(query_total, 1)
    overlap_coefficient = overlap / max(min(query_total, candidate_total), 1)
    precision = overlap / max(candidate_total, 1)
    f1 = (2.0 * coverage * precision / (coverage + precision)) if coverage + precision else 0.0
    query_bigrams = set(_intent_ngrams(query_terms, 2))
    candidate_bigrams = set(_intent_ngrams(candidate_terms, 2))
    bigram_overlap = (
        len(query_bigrams & candidate_bigrams) / max(len(query_bigrams), 1)
        if query_bigrams
        else 0.0
    )
    score = (
        0.45 * coverage
        + 0.30 * overlap_coefficient
        + 0.15 * f1
        + 0.10 * bigram_overlap
    )
    return {
        "score": round(min(max(score, 0.0), 1.0), 4),
        "query_terms": query_terms,
        "candidate_term_count": candidate_total,
        "matched_terms": sorted(set(query_counts) & set(candidate_counts)),
        "matched_text": candidate,
        "coverage": round(coverage, 4),
        "overlap_coefficient": round(overlap_coefficient, 4),
        "bigram_overlap": round(bigram_overlap, 4),
    }


def _intent_terms(text: str) -> List[str]:
    terms: List[str] = []
    normalized = normalize_replay_key(text).replace("-", " ").replace("/", " ")
    for raw_token in _INTENT_TOKEN_RE.findall(normalized):
        token = raw_token.strip("_")
        if not token or _is_identifier_like_intent_token(token):
            continue
        token = _canonical_intent_token(token)
        if token in _INTENT_STOPWORDS or len(token) < 2:
            continue
        terms.append(token)
    return terms


def _canonical_intent_token(token: str) -> str:
    token = str(token or "").casefold()
    if token in _INTENT_CANONICAL_TOKENS:
        return _INTENT_CANONICAL_TOKENS[token]
    if token.endswith("ies") and len(token) > 4:
        token = token[:-3] + "y"
    elif token.endswith("s") and len(token) > 4 and not token.endswith("ss"):
        token = token[:-1]
    return _INTENT_CANONICAL_TOKENS.get(token, token)


def _is_identifier_like_intent_token(token: str) -> bool:
    token = str(token or "").casefold()
    if not token:
        return True
    if len(token) >= 4 and token.isdigit():
        return True
    if len(token) >= 4 and re.fullmatch(r"[0-9a-f]+", token):
        return True
    if len(token) >= 8 and re.search(r"\d", token):
        return True
    return False


def _term_counts(terms: Sequence[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for term in terms:
        counts[term] = counts.get(term, 0) + 1
    return counts


def _intent_ngrams(terms: Sequence[str], size: int) -> List[tuple[str, ...]]:
    if size <= 0 or len(terms) < size:
        return []
    return [tuple(terms[index : index + size]) for index in range(len(terms) - size + 1)]


def _manifest_application(manifest: Mapping[str, Any]) -> str:
    metadata = manifest.get("metadata")
    if isinstance(metadata, Mapping):
        return str(metadata.get("application") or "").strip()
    return ""


def _merge_unique_dicts(
    existing: List[Any],
    new_items: List[Mapping[str, Any]],
    *,
    key: str,
) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = [dict(item) for item in existing if isinstance(item, Mapping)]
    seen = {str(item.get(key) or "") for item in merged}
    for item in new_items:
        item_key = str(item.get(key) or "")
        if item_key and item_key in seen:
            continue
        merged.append(dict(item))
        if item_key:
            seen.add(item_key)
    return merged


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def _sanitize_id(value: str) -> str:
    return "".join(ch for ch in str(value) if ch.isalnum() or ch in {"_", "-"}).strip()


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _float_or_none(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _list_of_strings(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
