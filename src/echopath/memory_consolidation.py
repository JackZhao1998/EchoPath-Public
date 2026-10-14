from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .router import ModelRequest, ModelRouter
from .agent_utils import extract_json_object
from .memory_segments import SEGMENT_SCHEMA, validate_segments

try:
    from PIL import Image, ImageChops, ImageStat
except ImportError:  # pragma: no cover - Pillow is part of the normal install.
    Image = None  # type: ignore[assignment]
    ImageChops = None  # type: ignore[assignment]
    ImageStat = None  # type: ignore[assignment]


class TrajectoryConsolidationError(RuntimeError):
    pass


_VISUAL_DIFF_SIZE = (160, 90)
_MEAN_PIXEL_DELTA_THRESHOLD = 2.0
_CHANGED_PIXEL_FRACTION_THRESHOLD = 0.04
_PIXEL_CHANGE_THRESHOLD = 16
_INITIAL_DEPENDENCY_ACTIONS = {"click", "press", "hotkey", "scroll", "wait"}
_INITIAL_DEPENDENCY_LOOKBACK_STEP_LIMIT = 4


@dataclass(frozen=True)
class TrajectoryConsolidationResult:
    selected_step_ids: List[str]
    dropped_steps: List[Dict[str, str]]
    flexible_action_inputs: List[Dict[str, Any]]
    reasoning_report: Dict[str, Any]
    model: str
    response_id: Optional[str] = None
    segments: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "selected_step_ids": list(self.selected_step_ids),
            "dropped_steps": [dict(item) for item in self.dropped_steps],
            "flexible_action_inputs": [dict(item) for item in self.flexible_action_inputs],
            "reasoning_report": dict(self.reasoning_report),
            "model": self.model,
            "response_id": self.response_id,
            "segments": self.segments,
        }


TRAJECTORY_CONSOLIDATION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "segments": {"type": "array", "items": SEGMENT_SCHEMA},
        "selected_step_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
        "dropped_steps": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "step_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["step_id", "reason"],
            },
        },
        "flexible_action_inputs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "step_id": {"type": "string"},
                    "parameter_path": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "input_kind": {
                        "type": "string",
                        "enum": [
                            "input_text",
                            "search_query",
                            "form_value",
                            "filename",
                            "url",
                            "other",
                        ],
                    },
                    "description": {"type": "string"},
                    "rebind_when": {"type": "string"},
                    "constraints": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "step_id",
                    "parameter_path",
                    "input_kind",
                    "description",
                    "rebind_when",
                    "constraints",
                ],
            },
        },
        "component_labels": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "step_id": {"type": "string"},
                    "label": {"type": "string"},
                },
                "required": ["step_id", "label"],
            },
        },
        "task_intent": {"type": "string"},
        "query_keys": {"type": "array", "items": {"type": "string"}},
        "preconditions": {"type": "array", "items": {"type": "string"}},
        "effects": {"type": "array", "items": {"type": "string"}},
        "viable": {"type": "boolean"},
        "promotion_recommendation": {
            "type": "string",
            "enum": ["promote", "reject"],
        },
        "blockers": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
    },
    "required": [
        "segments",
        "selected_step_ids",
        "dropped_steps",
        "flexible_action_inputs",
        "component_labels",
        "task_intent",
        "query_keys",
        "preconditions",
        "effects",
        "viable",
        "promotion_recommendation",
        "blockers",
        "rationale",
    ],
}


async def consolidate_wrapper_trajectory(
    router: ModelRouter,
    run_directory: Path,
    *,
    task_intent: str,
    application: str = "",
    model: str = "default",
    reasoning_effort: str = "medium",
    goal_verification: Optional[Mapping[str, Any]] = None,
) -> TrajectoryConsolidationResult:
    run_directory = Path(run_directory).resolve()
    steps = _read_jsonl(run_directory / "steps.jsonl")
    if not steps:
        raise TrajectoryConsolidationError("Wrapper run does not contain action steps.")

    compact_steps = [
        _compact_step(
            step,
            next_step=steps[index + 1] if index + 1 < len(steps) else None,
            run_directory=run_directory,
        )
        for index, step in enumerate(steps)
    ]
    prompt = _build_prompt(
        task_intent=task_intent,
        application=application,
        steps=compact_steps,
        goal_verification=goal_verification,
    )
    response = await router.generate(
        ModelRequest(
            model=model,
            input=prompt,
            reasoning_effort=reasoning_effort,
            max_output_tokens=8000,
            metadata={"operation": "memory_trajectory_consolidation"},
            extra_body={"output_schema": TRAJECTORY_CONSOLIDATION_SCHEMA},
        )
    )
    payload = extract_json_object(response.output_text)
    if payload is None:
        raise TrajectoryConsolidationError(
            "Trajectory consolidator returned invalid JSON."
        )
    if not isinstance(payload, Mapping):
        raise TrajectoryConsolidationError(
            "Trajectory consolidator returned a non-object response."
        )

    selected_step_ids = _validate_selected_steps(payload, steps)
    model_selected_step_ids = list(selected_step_ids)
    selected_step_ids, dependency_guard_added_step_ids = _preserve_initial_dependency_steps(
        selected_step_ids,
        steps,
        run_directory=run_directory,
    )
    dropped_steps = _normalize_dropped_steps(
        payload.get("dropped_steps"),
        steps,
        selected_step_ids,
    )
    flexible_action_inputs = _validate_flexible_action_inputs(
        payload.get("flexible_action_inputs"),
        steps,
        selected_step_ids,
    )
    if not payload.get("segments"):
        raise TrajectoryConsolidationError("Consolidation must provide a nonempty segment partition")
    try:
        segments = validate_segments(payload.get("segments"), model_selected_step_ids, flexible_action_inputs)
        if segments and dependency_guard_added_step_ids:
            # Attach restored prerequisites to the first subgoal. A focus click or
            # hotkey alone has no independently verifiable semantic completion.
            segments[0]["source_step_ids"] = dependency_guard_added_step_ids + segments[0]["source_step_ids"]
            segments = validate_segments(segments, selected_step_ids, flexible_action_inputs)
    except ValueError as error:
        raise TrajectoryConsolidationError(str(error)) from error
    reasoning_report = {
        key: payload.get(key)
        for key in (
            "task_intent",
            "query_keys",
            "preconditions",
            "effects",
            "viable",
            "promotion_recommendation",
            "blockers",
            "rationale",
        )
    }
    reasoning_report["component_labels"] = _normalize_component_labels(
        payload.get("component_labels"),
        selected_step_ids,
    )
    reasoning_report["dependency_guard"] = {
        "added_step_ids": dependency_guard_added_step_ids,
        "policy": (
            "Preserve the nearest successful initial state-preparation action with a "
            "meaningful immediate or delayed visual transition before the first selected "
            "task step, while continuing to drop earlier exploratory prefixes."
        ),
    }
    recommendation = str(reasoning_report.get("promotion_recommendation") or "").strip().lower()
    if recommendation.startswith("promote"):
        reasoning_report["promotion_recommendation"] = "promote"
    elif recommendation.startswith("reject"):
        reasoning_report["promotion_recommendation"] = "reject"
    return TrajectoryConsolidationResult(
        selected_step_ids=selected_step_ids,
        dropped_steps=dropped_steps,
        flexible_action_inputs=flexible_action_inputs,
        reasoning_report=reasoning_report,
        model=response.model,
        response_id=response.response_id,
        segments=segments,
    )


def _build_prompt(
    *,
    task_intent: str,
    application: str,
    steps: Sequence[Mapping[str, Any]],
    goal_verification: Optional[Mapping[str, Any]] = None,
) -> str:
    payload = {
        "task_intent": str(task_intent),
        "application": str(application),
        "steps": list(steps),
        "final_goal_verification": {
            key: goal_verification[key]
            for key in ("goal_completed", "assessment", "blocker", "source")
            if isinstance(goal_verification, Mapping) and key in goal_verification
        },
    }
    return (
        "Consolidate this successful GUI trajectory into a reusable segmented workflow. "
        "Analyze the full trace for semantic decisions before dropping exploratory actions. "
        "Return segments as an ordered, complete partition of selected_step_ids into contiguous "
        "subgoals. Each segment must include segment_id, source_step_ids, goal, execution_policy, "
        "replay_conditions, completion_condition, inputs, outputs, output_bindings and rationale. "
        "Use replay for invariant procedures with declared flexible inputs; native when fresh "
        "judgment, content creation, candidate selection or live information is essential; "
        "conditional when explicit observable replay_conditions decide whether replay applies. "
        "Every segment already receives live completion verification at execution time. "
        "Verification alone does not require native action: keep invariant navigation, form "
        "submission and export procedures replayable when they need no fresh decision. Visual "
        "grounding handles moved controls; conditional policies should express semantic reuse "
        "conditions that can be checked from the live screen, not internal binding checks. "
        "A native segment replaces its recorded steps; retain those steps as example evidence. "
        "Group variable-length decisions through a stable, observable return state. Goals and "
        "completion conditions must refer to the CURRENT request, never hardcode the original "
        "chosen product, document, answer or generated text. Explain uncertainty in rationale. "
        "Generalize request-specific attribute names and units as well as their values: use "
        "the current request's package-size requirements rather than fixing roll count when "
        "another packaged product may be measured in bottles. Apply the same principle to "
        "document topics, entity types and field names. Flexible input constraints must preserve "
        "the field's role, not restrict it to the original query or product category. "
        "Use final_goal_verification, when supplied, as semantic evidence of the recorded end "
        "state; it is evidence from that run, not a guarantee about future executions. "
        "Do not infer semantic intent from pixel changes alone. Prefer conditional with concrete "
        "checks when reuse is uncertain. Inputs are 'request' or named earlier native outputs. "
        "Outputs are globally unique string value names, supported only on native segments. "
        "An output_binding has output, step_id and parameter_path and may target only a later "
        "declared flexible type.text or paste_text.text field. Use empty outputs/output_bindings "
        "where unnecessary. Do not include personal values in generic goals or policies. "
        "Use only evidence present in the trace; do not invent unseen branches or UI states. "
        "Select only actions that are necessary and useful for reproducing the completed "
        "task. Preserve their original order. Drop exploratory clicks, corrections that were "
        "later undone or overwritten, redundant focus changes, no-op moves, unnecessary waits, "
        "and failed actions. Keep required navigation, focus acquisition, edits, save/submit "
        "actions, and waits only when they are necessary for a state transition. Preserve "
        "state-preparation dependencies even when they look unrelated to the final task, including "
        "clicks or keypresses that dismiss onboarding, sign-in prompts, tips, modal dialogs, popups, "
        "permission prompts, blockers, or other first-run UI state before the main task action. "
        "Treat a step as potentially dependent when the screen changes immediately after it, or when "
        "the screen changes between that step's after-screenshot and the next step's before-screenshot. "
        "Do not drop such a step unless a later retained action explicitly reaches the same UI state "
        "without relying on it. Never invent, rewrite, combine, or reorder actions. Return "
        "selected_step_ids as a non-empty subsequence of the supplied successful steps. Mark "
        "flexible_action_inputs for selected type or "
        "paste_text actions whose text/search/form value is task-specific and could be rebound "
        "for a closely related future task. Use parameter_path relative to the replay action, "
        "for example [\"text\"] for paste_text.text or type.text. Do not mark fixed navigation, "
        "save hotkeys, waits, coordinates, or structural actions as flexible unless the task "
        "explicitly makes that parameter the reusable variable. Always include "
        "component_labels: for selected click/move steps whose purpose you can infer from the "
        "action metadata, a short stable UI-component name such as 'Start timer button'; omit "
        "entries you are unsure about rather than guessing, and use an empty array when no "
        "labels can be inferred. Explain every dropped step, and "
        "provide the promotion reasoning fields requested by the schema. Mark viable true only when "
        "the selected action subsequence is complete enough to replay the stated task under the "
        "listed preconditions. Set promotion_recommendation to exactly 'promote' or 'reject'; reject "
        "when the selected steps omit required task effects, contain unsupported/failed actions, or "
        "depend on unstated missing state. Put concrete failure reasons in blockers.\n\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def _compact_step(
    step: Mapping[str, Any],
    *,
    next_step: Optional[Mapping[str, Any]],
    run_directory: Path,
) -> Dict[str, Any]:
    action = step.get("action") if isinstance(step.get("action"), Mapping) else {}
    executor_result = (
        step.get("executor_result")
        if isinstance(step.get("executor_result"), Mapping)
        else {}
    )
    visual_delta = _visual_delta_hint(
        step.get("before_observation"),
        step.get("after_observation"),
        run_directory=run_directory,
    )
    delayed_delta: Dict[str, Any] = {"state_change": "unknown"}
    if next_step is not None:
        delayed_delta = _visual_delta_hint(
            step.get("after_observation"),
            next_step.get("before_observation"),
            run_directory=run_directory,
        )
    return {
        "step_id": str(step.get("step_id") or ""),
        "sequence": step.get("sequence"),
        "status": str(step.get("status") or ""),
        "action": _truncate_value(action),
        "replay_action": _truncate_value(_replay_action_from_step(step)),
        "executor_result": _truncate_value(executor_result),
        "source": _truncate_value(step.get("source") or {}),
        "visual_change": visual_delta.get("state_change", "unknown"),
        "visual_delta": visual_delta,
        "delayed_visual_change_before_next_step": delayed_delta.get(
            "state_change",
            "unknown",
        ),
        "delayed_visual_delta_before_next_step": delayed_delta,
    }


def _visual_change_hint(step: Mapping[str, Any], *, run_directory: Path) -> str:
    return str(
        _visual_delta_hint(
            step.get("before_observation"),
            step.get("after_observation"),
            run_directory=run_directory,
        ).get("state_change")
        or "unknown"
    )


def _visual_delta_hint(
    before: Any,
    after: Any,
    *,
    run_directory: Path,
) -> Dict[str, Any]:
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return {"state_change": "unknown"}
    before_path = _resolve_observation_path(before.get("path"), run_directory)
    after_path = _resolve_observation_path(after.get("path"), run_directory)
    if before_path is None or after_path is None:
        return {"state_change": "unknown"}
    return _compare_observation_images(before_path, after_path)


def _compare_observation_images(before_path: Path, after_path: Path) -> Dict[str, Any]:
    if Image is None or ImageChops is None or ImageStat is None:
        try:
            changed = before_path.read_bytes() != after_path.read_bytes()
        except OSError:
            return {"state_change": "unknown"}
        return {"state_change": "changed" if changed else "unchanged"}

    try:
        before_image = Image.open(before_path).convert("RGB").resize(_VISUAL_DIFF_SIZE)
        after_image = Image.open(after_path).convert("RGB").resize(_VISUAL_DIFF_SIZE)
    except OSError:
        return {"state_change": "unknown"}

    difference = ImageChops.difference(before_image, after_image).convert("L")
    histogram = difference.histogram()
    pixel_count = _VISUAL_DIFF_SIZE[0] * _VISUAL_DIFF_SIZE[1]
    changed_fraction = sum(histogram[_PIXEL_CHANGE_THRESHOLD:]) / float(pixel_count)
    mean_delta = float(ImageStat.Stat(difference).mean[0])
    state_changed = (
        mean_delta >= _MEAN_PIXEL_DELTA_THRESHOLD
        or changed_fraction >= _CHANGED_PIXEL_FRACTION_THRESHOLD
    )
    return {
        "state_change": "changed" if state_changed else "unchanged",
        "mean_pixel_delta": round(mean_delta, 4),
        "changed_pixel_fraction": round(changed_fraction, 6),
    }


def _resolve_observation_path(value: Any, run_directory: Path) -> Optional[Path]:
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



def _normalize_component_labels(
    value: Any,
    selected_step_ids: Sequence[str],
) -> List[Dict[str, str]]:
    """Keep only well-formed labels for steps that were actually selected."""
    if not isinstance(value, list):
        return []
    selected = set(selected_step_ids)
    labels: List[Dict[str, str]] = []
    seen: set = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        step_id = str(item.get("step_id") or "").strip()
        label = str(item.get("label") or "").strip()
        if not step_id or not label or step_id not in selected or step_id in seen:
            continue
        seen.add(step_id)
        labels.append({"step_id": step_id, "label": label})
    return labels

def _validate_selected_steps(
    payload: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]],
) -> List[str]:
    raw_selected = payload.get("selected_step_ids")
    if not isinstance(raw_selected, list) or not raw_selected:
        raise TrajectoryConsolidationError("Consolidated trajectory selected no steps.")
    selected = [str(step_id) for step_id in raw_selected]
    if len(selected) != len(set(selected)):
        raise TrajectoryConsolidationError("Consolidated trajectory contains duplicate steps.")

    positions = {
        str(step.get("step_id") or ""): index
        for index, step in enumerate(steps)
    }
    unknown = [step_id for step_id in selected if step_id not in positions]
    if unknown:
        raise TrajectoryConsolidationError(
            "Consolidated trajectory selected unknown steps: " + ", ".join(unknown)
        )
    if selected != sorted(selected, key=positions.__getitem__):
        raise TrajectoryConsolidationError("Consolidated trajectory reordered steps.")

    by_id = {str(step.get("step_id") or ""): step for step in steps}
    failed = [
        step_id
        for step_id in selected
        if str(by_id[step_id].get("status") or "") != "ok"
    ]
    if failed:
        raise TrajectoryConsolidationError(
            "Consolidated trajectory selected failed steps: " + ", ".join(failed)
        )
    return selected


def _preserve_initial_dependency_steps(
    selected_step_ids: Sequence[str],
    steps: Sequence[Mapping[str, Any]],
    *,
    run_directory: Path,
) -> tuple[List[str], List[str]]:
    selected = [str(step_id) for step_id in selected_step_ids]
    positions = {
        str(step.get("step_id") or ""): index
        for index, step in enumerate(steps)
    }
    first_selected_index = positions[selected[0]]
    if first_selected_index <= 0:
        return selected, []

    skipped_noop_count = 0
    guarded: List[str] = []
    for index in range(first_selected_index - 1, -1, -1):
        step = steps[index]
        step_id = str(step.get("step_id") or "")
        if not step_id:
            continue
        if str(step.get("status") or "") != "ok":
            break
        if not _is_initial_dependency_action(step):
            break
        next_step = steps[index + 1] if index + 1 < len(steps) else None
        if _has_meaningful_initial_state_transition(
            step,
            next_step=next_step,
            run_directory=run_directory,
        ):
            guarded.append(step_id)
            break
        skipped_noop_count += 1
        if skipped_noop_count >= _INITIAL_DEPENDENCY_LOOKBACK_STEP_LIMIT:
            break

    if not guarded:
        return selected, []
    guarded.reverse()
    return guarded + selected, guarded


def _is_initial_dependency_action(step: Mapping[str, Any]) -> bool:
    action_name = str(_replay_action_from_step(step).get("action") or "").strip().lower()
    return action_name in _INITIAL_DEPENDENCY_ACTIONS


def _has_meaningful_initial_state_transition(
    step: Mapping[str, Any],
    *,
    next_step: Optional[Mapping[str, Any]],
    run_directory: Path,
) -> bool:
    immediate = _visual_delta_hint(
        step.get("before_observation"),
        step.get("after_observation"),
        run_directory=run_directory,
    )
    if immediate.get("state_change") == "changed":
        return True
    if next_step is None:
        return False
    delayed = _visual_delta_hint(
        step.get("after_observation"),
        next_step.get("before_observation"),
        run_directory=run_directory,
    )
    return delayed.get("state_change") == "changed"


def _normalize_dropped_steps(
    value: Any,
    steps: Sequence[Mapping[str, Any]],
    selected_step_ids: Sequence[str],
) -> List[Dict[str, str]]:
    supplied: Dict[str, str] = {}
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, Mapping):
                continue
            step_id = str(item.get("step_id") or "")
            reason = str(item.get("reason") or "").strip()
            if step_id and reason:
                supplied[step_id] = reason
    selected = set(selected_step_ids)
    return [
        {
            "step_id": str(step.get("step_id") or ""),
            "reason": supplied.get(
                str(step.get("step_id") or ""),
                "not selected by consolidator",
            ),
        }
        for step in steps
        if str(step.get("step_id") or "") not in selected
    ]


def _validate_flexible_action_inputs(
    value: Any,
    steps: Sequence[Mapping[str, Any]],
    selected_step_ids: Sequence[str],
) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TrajectoryConsolidationError("flexible_action_inputs must be a list.")

    selected = set(str(step_id) for step_id in selected_step_ids)
    by_id = {
        str(step.get("step_id") or ""): step
        for step in steps
        if str(step.get("step_id") or "")
    }
    flexible_inputs: List[Dict[str, Any]] = []
    seen = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise TrajectoryConsolidationError("flexible_action_inputs entries must be objects.")
        step_id = str(item.get("step_id") or "").strip()
        if step_id not in selected:
            raise TrajectoryConsolidationError(
                f"Flexible input references unselected step '{step_id}'."
            )
        step = by_id.get(step_id)
        if step is None:
            raise TrajectoryConsolidationError(
                f"Flexible input references unknown step '{step_id}'."
            )
        parameter_path = _normalize_parameter_path(item.get("parameter_path"))
        replay_action = _replay_action_from_step(step)
        if _value_at_path(replay_action, parameter_path) is None:
            raise TrajectoryConsolidationError(
                f"Flexible input references missing action parameter {parameter_path!r} on {step_id}."
            )
        action_name = str(replay_action.get("action") or "").strip().lower()
        if action_name not in {"type", "paste_text"} or parameter_path != ["text"]:
            raise TrajectoryConsolidationError(
                "Flexible inputs currently support only type.text and paste_text.text."
            )
        key = (step_id, tuple(parameter_path))
        if key in seen:
            raise TrajectoryConsolidationError(
                f"Duplicate flexible input for step '{step_id}' and path {parameter_path!r}."
            )
        seen.add(key)
        flexible_inputs.append(
            {
                "step_id": step_id,
                "parameter_path": parameter_path,
                "input_kind": str(item.get("input_kind") or "input_text").strip() or "input_text",
                "description": str(item.get("description") or "").strip(),
                "rebind_when": str(item.get("rebind_when") or "").strip(),
                "constraints": [
                    str(constraint).strip()
                    for constraint in item.get("constraints") or []
                    if str(constraint).strip()
                ],
            }
        )
    return flexible_inputs


def _replay_action_from_step(step: Mapping[str, Any]) -> Dict[str, Any]:
    action_record = step.get("action")
    if not isinstance(action_record, Mapping):
        return {}
    action_name = str(action_record.get("name") or action_record.get("action") or "").strip()
    parameters = action_record.get("parameters")
    if not isinstance(parameters, Mapping):
        parameters = {}
    return {"action": action_name, **dict(parameters)}


def _normalize_parameter_path(value: Any) -> List[str]:
    if isinstance(value, str):
        path = [part for part in value.split(".") if part]
    elif isinstance(value, list):
        path = [str(part).strip() for part in value if str(part).strip()]
    else:
        path = []
    if not path:
        raise TrajectoryConsolidationError("Flexible input parameter_path cannot be empty.")
    return path


def _value_at_path(payload: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise TrajectoryConsolidationError(
            f"Could not read trajectory '{path}': {error}"
        ) from error
    records: List[Dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise TrajectoryConsolidationError(
                f"Invalid trajectory record in '{path}'."
            ) from error
        if isinstance(payload, Mapping):
            records.append(dict(payload))
    return records
