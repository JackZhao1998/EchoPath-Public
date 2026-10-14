from __future__ import annotations

from typing import Any, Dict, Mapping

from .agent import BaseAgent
from .agent_utils import summarize_execution_value
from .models import AgentTask
from .runtime import AgentContext

NON_EXECUTING_RESULT_ACTIONS = {"observe"}


def coerce_task_record(default_goal: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
    raw_task = payload.get("task")
    if isinstance(raw_task, Mapping):
        raw_payload = raw_task.get("payload")
        return {
            "type": str(raw_task.get("type", "none")).strip().lower() or "none",
            "summary": str(raw_task.get("summary", "")).strip(),
            "goal": str(raw_task.get("goal", "")).strip() or default_goal,
            "payload": dict(raw_payload) if isinstance(raw_payload, Mapping) else {},
        }

    raw_payload = payload.get("task_payload")
    return {
        "type": str(payload.get("task_type", "none")).strip().lower() or "none",
        "summary": str(payload.get("task_summary", "")).strip(),
        "goal": str(payload.get("task_goal", "")).strip() or default_goal,
        "payload": dict(raw_payload) if isinstance(raw_payload, Mapping) else {},
    }


def is_executed_result_record(record: Any) -> bool:
    if not isinstance(record, Mapping):
        return False

    action_name = str(record.get("action", "")).strip().lower()
    if action_name:
        return action_name not in NON_EXECUTING_RESULT_ACTIONS

    return "command" in record or "path" in record or "selector" in record or "url" in record


def count_performed_actions(execution_output: Any) -> int:
    if not isinstance(execution_output, Mapping):
        return 0

    explicit_count = execution_output.get("performed_action_count")
    if explicit_count is not None:
        try:
            return max(0, int(explicit_count))
        except (TypeError, ValueError):
            pass

    total = 0
    results = execution_output.get("results")
    if isinstance(results, list):
        total += sum(1 for item in results if is_executed_result_record(item))

    action_result = execution_output.get("action_result")
    if isinstance(action_result, Mapping):
        total += count_performed_actions(action_result)

    iterations = execution_output.get("iterations")
    if isinstance(iterations, list):
        for iteration in iterations:
            if not isinstance(iteration, Mapping):
                continue
            nested_action_result = iteration.get("action_result")
            if isinstance(nested_action_result, Mapping):
                total += count_performed_actions(nested_action_result)

    if total > 0:
        return total

    for key in ("command_count", "action_count"):
        raw_value = execution_output.get(key)
        if raw_value is None:
            continue
        try:
            return max(0, int(raw_value))
        except (TypeError, ValueError):
            continue
    return 0


def is_completed_without_action(
    task_record: Mapping[str, Any],
    execution_output: Any,
) -> bool:
    if not isinstance(execution_output, Mapping):
        return False

    explicit = execution_output.get("completed_without_action")
    if explicit is not None:
        return bool(explicit)

    return (
        str(task_record.get("type", "")).strip().lower() == "desktop_task"
        and bool(execution_output.get("completed"))
        and count_performed_actions(execution_output) == 0
    )


def _summary_phrase(value: Any) -> str:
    """A summary rendered mid-sentence, without doubling the caller's period."""
    summary = str(value or "").strip()
    return summary[:-1].rstrip() if summary.endswith(".") else summary


def merge_success_reply(
    base_reply: str,
    task_record: Mapping[str, Any],
    execution_output: Any,
) -> str:
    message = base_reply.strip() or "The task completed successfully."
    summary = _summary_phrase(task_record.get("summary"))
    if not summary:
        return message

    task_type = str(task_record.get("type", "")).strip().lower()
    performed_action_count = count_performed_actions(execution_output)
    detail = ""
    if isinstance(execution_output, Mapping):
        values = execution_output.get("values")
        if isinstance(values, Mapping) and values:
            parts = [
                f"{key}: {summarize_execution_value(value)}"
                for key, value in list(values.items())[:3]
            ]
            detail = " " + " ".join(parts)

    if is_completed_without_action(task_record, execution_output):
        if task_type == "desktop_task":
            suffix = f"Completed without executing any desktop actions: {summary}."
        else:
            suffix = f"Completed without executing any actions: {summary}."
    elif performed_action_count > 0:
        suffix = f"Executed: {summary}."
    else:
        suffix = f"Completed: {summary}."

    return f"{message}\n\n{suffix}{detail}".strip()


def merge_failure_reply(
    base_reply: str,
    task_summary: str,
    error: str,
) -> str:
    message = base_reply.strip() or "I tried to run the task."
    suffix = f"Task failed: {error or 'unknown error'}."
    summary = _summary_phrase(task_summary)
    if summary:
        suffix = f"Attempted: {summary}. {suffix}"
    return f"{message}\n\n{suffix}".strip()


def build_execution_success_output(
    *,
    assistant_reply: str,
    task_record: Mapping[str, Any],
    execution_output: Any,
) -> Dict[str, Any]:
    performed_action_count = count_performed_actions(execution_output)
    completed_without_action = is_completed_without_action(task_record, execution_output)
    return {
        "assistant_reply": merge_success_reply(
            assistant_reply,
            task_record,
            execution_output,
        ),
        "task": {
            "type": str(task_record.get("type", "none")),
            "summary": str(task_record.get("summary", "")),
            "goal": str(task_record.get("goal", "")),
            "payload": dict(task_record.get("payload", {}) or {}),
        },
        "execution": {
            "success": True,
            "error": None,
            "output": execution_output,
            "performed_action_count": performed_action_count,
            "completed_without_action": completed_without_action,
        },
    }


def build_execution_failure_output(
    *,
    assistant_reply: str,
    task_record: Mapping[str, Any],
    error: str,
    execution_output: Any = None,
) -> Dict[str, Any]:
    performed_action_count = count_performed_actions(execution_output)
    completed_without_action = is_completed_without_action(task_record, execution_output)
    return {
        "assistant_reply": merge_failure_reply(
            assistant_reply,
            str(task_record.get("summary", "")),
            error,
        ),
        "task": {
            "type": str(task_record.get("type", "none")),
            "summary": str(task_record.get("summary", "")),
            "goal": str(task_record.get("goal", "")),
            "payload": dict(task_record.get("payload", {}) or {}),
        },
        "execution": {
            "success": False,
            "error": error,
            "output": execution_output,
            "performed_action_count": performed_action_count,
            "completed_without_action": completed_without_action,
        },
    }


class ExecutionAgent(BaseAgent):
    async def execute(self, task: AgentTask, context: AgentContext):
        payload = dict(task.payload)
        task_record = coerce_task_record(task.goal, payload)
        assistant_reply = str(payload.get("assistant_reply", "")).strip()
        skill_name = str(payload.get("skill_name", "")).strip()
        skill_payload = payload.get("skill_payload")
        if not isinstance(skill_payload, Mapping):
            skill_payload = {}

        if not skill_name:
            return build_execution_failure_output(
                assistant_reply=assistant_reply,
                task_record=task_record,
                error="Execution agent requires a skill_name.",
            )

        try:
            execution_output = await context.use_skill(skill_name, dict(skill_payload))
        except Exception as error:
            return build_execution_failure_output(
                assistant_reply=assistant_reply,
                task_record=task_record,
                error=str(error),
            )

        return build_execution_success_output(
            assistant_reply=assistant_reply,
            task_record=task_record,
            execution_output=execution_output,
        )
