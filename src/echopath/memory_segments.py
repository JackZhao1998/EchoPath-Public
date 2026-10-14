"""Semantic segment contracts shared by consolidation, storage and replay."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence


SEGMENTS_KEY = "memory_segments"
CHECKPOINT_KEY = "memory_segment_checkpoint"
STRING_LIST = {"type": "array", "items": {"type": "string"}}
SEGMENT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "segment_id": {"type": "string"},
        "source_step_ids": STRING_LIST,
        "goal": {"type": "string"},
        "execution_policy": {"type": "string", "enum": ["replay", "native", "conditional"]},
        "replay_conditions": STRING_LIST,
        "completion_condition": {"type": "string"},
        "inputs": STRING_LIST,
        "outputs": STRING_LIST,
        "output_bindings": {
            "type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "output": {"type": "string"},
                    "step_id": {"type": "string"},
                    "parameter_path": STRING_LIST,
                },
                "required": ["output", "step_id", "parameter_path"],
            },
        },
        "rationale": {"type": "string"},
    },
    "required": ["segment_id", "source_step_ids", "goal", "execution_policy",
                 "replay_conditions", "completion_condition", "inputs", "outputs",
                 "output_bindings", "rationale"],
}


def validate_segments(value: Any, selected_step_ids: Sequence[str],
                      flexible_inputs: Sequence[Mapping[str, Any]] = ()) -> list[dict]:
    """Require an ordered, complete partition; old memories may omit segments."""
    if value is None or value == []:
        return []
    if not isinstance(value, list):
        raise ValueError("segments must be a list")
    positions = {step: index + 1 for index, step in enumerate(selected_step_ids)}
    flexible = {(x["step_id"], tuple(x["parameter_path"])) for x in flexible_inputs}
    result, covered, ids, available, bound = [], [], set(), {"request"}, set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError("Each segment must be an object")
        segment = deepcopy(dict(raw))
        for key in ("segment_id", "goal", "completion_condition", "rationale"):
            if not isinstance(segment.get(key), str) or not segment[key].strip():
                raise ValueError(f"Segment requires nonempty {key}")
        identity = segment["segment_id"]
        if identity in ids:
            raise ValueError("Duplicate segment_id")
        ids.add(identity)
        policy = segment.get("execution_policy")
        if policy not in {"replay", "native", "conditional"}:
            raise ValueError("Invalid segment execution_policy")
        for key in ("source_step_ids", "replay_conditions", "inputs", "outputs"):
            items = segment.get(key)
            if not isinstance(items, list) or any(not isinstance(x, str) or not x.strip() for x in items):
                raise ValueError(f"Segment {key} must contain nonempty strings")
            if len(items) != len(set(items)):
                raise ValueError(f"Duplicate segment {key}")
        steps = segment["source_step_ids"]
        if not steps or any(step not in positions for step in steps):
            raise ValueError("Segment references missing or unselected steps")
        indexes = [positions[step] for step in steps]
        if indexes != list(range(indexes[0], indexes[-1] + 1)):
            raise ValueError("Segment steps must be contiguous and ordered")
        if policy == "conditional" and not segment["replay_conditions"]:
            raise ValueError("Conditional segments require explicit replay_conditions")
        if policy != "conditional" and segment["replay_conditions"]:
            raise ValueError("Replay conditions require conditional execution")
        if set(segment["inputs"]) - available:
            raise ValueError("Segment inputs must be request or earlier segment outputs")
        if set(segment["outputs"]) & available:
            raise ValueError("Segment outputs must have unique names")
        # Replay cannot produce new semantic values; a live decision must supply them.
        if policy != "native" and segment["outputs"]:
            raise ValueError("Segments producing outputs must use native execution")
        bindings = segment.get("output_bindings")
        if not isinstance(bindings, list):
            raise ValueError("Segment output_bindings must be a list")
        for binding in bindings:
            if not isinstance(binding, Mapping) or binding.get("output") not in segment["outputs"]:
                raise ValueError("Output binding references an undeclared output")
            path = binding.get("parameter_path")
            if path != ["text"]:
                raise ValueError("Output bindings currently support only text fields")
            target = (binding.get("step_id"), tuple(path))
            if target not in flexible or positions.get(target[0], 0) <= indexes[-1]:
                raise ValueError("Outputs may bind only later declared flexible inputs")
            if target in bound:
                raise ValueError("Multiple outputs bind the same action field")
            bound.add(target)
            binding["action_index"] = positions[target[0]]
        segment["start_action_index"], segment["end_action_index"] = indexes[0], indexes[-1]
        available.update(segment["outputs"])
        covered.extend(steps)
        result.append(segment)
    if covered != list(selected_step_ids):
        raise ValueError("Segments must cover every selected step exactly once in order")
    return result


def runtime_segments(payload: Mapping[str, Any]) -> list[dict]:
    """Validate stored bounds as well as semantic contracts before any action."""
    segments = payload.get(SEGMENTS_KEY, [])
    if segments is None or segments == []:
        return []
    if not isinstance(segments, list) or any(not isinstance(s, Mapping) for s in segments):
        raise ValueError("Invalid stored memory segments")
    if any(not isinstance(s.get("source_step_ids"), list)
           or any(not isinstance(step, str) for step in s["source_step_ids"]) for s in segments):
        raise ValueError("Invalid stored segment step IDs")
    steps = [step for s in segments for step in s.get("source_step_ids", [])]
    actions = payload.get("actions") or []
    if len(steps) != len(actions) or len(set(steps)) != len(steps):
        raise ValueError("Memory segment coverage does not match stored actions")
    validated = validate_segments(segments, steps, payload.get("memory_flexible_action_inputs") or [])
    for old, new in zip(segments, validated):
        for key in ("start_action_index", "end_action_index"):
            if old.get(key) != new[key]:
                raise ValueError("Memory segment action bounds are stale")
        for old_binding, binding in zip(old["output_bindings"], new["output_bindings"]):
            if old_binding.get("action_index") != binding["action_index"]:
                raise ValueError("Memory segment output binding index is stale")
        for binding in new["output_bindings"]:
            if actions[binding["action_index"] - 1].get("action") not in {"type", "paste_text"}:
                raise ValueError("Output binding target must be a text-entry action")
    return validated


def native_action_indexes(segments: Sequence[Mapping[str, Any]]) -> set[int]:
    """Actions whose missing replay evidence can be handled by the native host."""
    return {index for s in segments if s.get("execution_policy") in {"native", "conditional"}
            for index in range(s["start_action_index"], s["end_action_index"] + 1)}


def segment_payload(payload: Mapping[str, Any], segment: Mapping[str, Any]) -> dict:
    """Slice a replay segment, preserving binding alignment and recovery boundaries."""
    result = deepcopy(dict(payload))
    start, end = segment["start_action_index"], segment["end_action_index"]
    for key in (SEGMENTS_KEY, CHECKPOINT_KEY, "native_segment_result"):
        result.pop(key, None)
    result["actions"] = result["actions"][start - 1:end]
    for action in result["actions"]:
        # A recorded outcome may mention a previous request's item or value.
        action.pop("expected_outcome", None)
    for key in ("memory_replay_visual_bindings", "memory_replay_state_contracts"):
        result[key] = [{**item, "action_index": item["action_index"] - start + 1}
                       for item in result.get(key, []) if start <= item["action_index"] <= end]
    # Always reacquire targets after a native agent may have changed the UI.
    result["memory_replay_coordinate_copy_first"] = False
    result["memory_replay_repair_rewind_enabled"] = False
    result["memory_replay_optional_prerequisite_skip"] = False
    result["capture_after_action"] = True
    result.setdefault("verify_after_action", True)
    result["verify_goal_completion"] = True
    result["step_verification_failure_policy"] = "block_on_blocked"
    result["observation_task_id"] = f"{payload.get('observation_task_id', 'replay')}-{segment['segment_id']}"
    return result
