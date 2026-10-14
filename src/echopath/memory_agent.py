from __future__ import annotations

from typing import Any, Dict, List, Mapping

from .agent import BaseAgent
from .models import AgentTask
from .runtime import AgentContext


def _initialize_memory_state(seed: Any) -> Dict[str, Any]:
    memory: Dict[str, Any] = {}
    if isinstance(seed, Mapping):
        short_term = seed.get("short_term")
        long_term = seed.get("long_term")
        if isinstance(short_term, Mapping):
            memory["short_term"] = dict(short_term)
        if isinstance(long_term, Mapping):
            memory["long_term"] = dict(long_term)
    return memory


def _ensure_memory_state(context: AgentContext, payload: Mapping[str, Any]) -> Dict[str, Any]:
    existing = context.state.get("memory")
    if isinstance(existing, dict):
        return existing
    if isinstance(existing, Mapping):
        memory = dict(existing)
        context.state["memory"] = memory
        return memory
    memory = _initialize_memory_state(payload.get("memory"))
    context.state["memory"] = memory
    return memory


def _append_unique(items: List[Any], value: Any, *, limit: int) -> List[Any]:
    normalized = list(items)
    if value in normalized:
        normalized.remove(value)
    normalized.append(value)
    return normalized[-limit:]


def _sync_short_term_trajectories_from_store(
    short_term: Dict[str, Any],
    replay_store: Any,
    *,
    limit: int = 10,
) -> bool:
    load_snapshot = (
        getattr(replay_store, "load_short_term_memory_snapshot", None)
        if replay_store is not None
        else None
    )
    if not callable(load_snapshot):
        return False

    snapshot = load_snapshot(limit=limit)
    if not isinstance(snapshot, Mapping):
        return False

    known_trajectories = snapshot.get("known_action_trajectories")
    if isinstance(known_trajectories, list):
        short_term["known_action_trajectories"] = [
            dict(item) for item in known_trajectories if isinstance(item, Mapping)
        ]
    else:
        short_term["known_action_trajectories"] = []

    last_recorded = snapshot.get("last_recorded_action_trajectory")
    short_term["last_recorded_action_trajectory"] = (
        dict(last_recorded) if isinstance(last_recorded, Mapping) else None
    )
    return True


class ShortTermMemoryAgent(BaseAgent):
    async def execute(self, task: AgentTask, context: AgentContext):
        payload = dict(task.payload)
        memory = _ensure_memory_state(context, payload)
        short_term = memory.setdefault(
            "short_term",
            {
                "high_level_todo_tasks": [],
                "execution_level_memory": [],
                "known_action_trajectories": [],
                "matched_action_trajectory": None,
                "last_recorded_action_trajectory": None,
                "placeholder": False,
            },
        )
        short_term.setdefault("known_action_trajectories", [])
        short_term.setdefault("matched_action_trajectory", None)
        short_term.setdefault("last_recorded_action_trajectory", None)

        latest_message = str(payload.get("message") or task.goal).strip()
        if latest_message:
            todos = list(short_term.get("high_level_todo_tasks", []))
            short_term["high_level_todo_tasks"] = _append_unique(
                todos,
                latest_message,
                limit=10,
            )

        replay_store = context.get_service("interaction-replay-store")
        synced_from_store = _sync_short_term_trajectories_from_store(
            short_term,
            replay_store,
            limit=10,
        )
        replay_match = None
        find_match = (
            getattr(replay_store, "find_latest_successful_match", None)
            if replay_store is not None
            else None
        )
        if latest_message and callable(find_match):
            replay_match = find_match(latest_message)
        short_term["matched_action_trajectory"] = (
            replay_match.to_metadata() if replay_match is not None else None
        )

        decision = payload.get("decision")
        if isinstance(decision, Mapping):
            execution_notes = list(short_term.get("execution_level_memory", []))
            execution_notes.append(
                {
                    "phase": str(payload.get("phase", "update")).strip() or "update",
                    "task_type": str(
                        decision.get("type") or decision.get("task_type") or "none"
                    ).strip()
                    or "none",
                    "summary": str(decision.get("summary", "")).strip(),
                }
            )
            short_term["execution_level_memory"] = execution_notes[-20:]

        execution = payload.get("execution")
        if isinstance(execution, Mapping):
            short_term["last_execution"] = {
                "success": bool(execution.get("success")),
                "error": str(execution.get("error") or "").strip() or None,
            }

        upsert_trajectory = (
            getattr(replay_store, "upsert_runtime_trajectory", None)
            if replay_store is not None
            else None
        )
        trajectory_summary = None
        if (
            str(payload.get("phase", "update")).strip() == "completed"
            and isinstance(decision, Mapping)
            and isinstance(execution, Mapping)
            and callable(upsert_trajectory)
        ):
            trajectory_summary = upsert_trajectory(
                user_query=latest_message,
                task_record=decision,
                execution=execution,
                session_id=str(task.metadata.get("session_id", "")).strip(),
                task_id=task.task_id,
                assistant_reply=str(payload.get("assistant_reply", "")).strip(),
            )
            if _sync_short_term_trajectories_from_store(short_term, replay_store, limit=10):
                synced_from_store = True
            else:
                short_term["last_recorded_action_trajectory"] = (
                    dict(trajectory_summary) if isinstance(trajectory_summary, Mapping) else None
                )
            if isinstance(trajectory_summary, Mapping) and not synced_from_store:
                known_trajectories = list(short_term.get("known_action_trajectories", []))
                short_term["known_action_trajectories"] = _append_unique(
                    known_trajectories,
                    dict(trajectory_summary),
                    limit=10,
                )
                refreshed_match = find_match(latest_message) if latest_message and callable(find_match) else None
                if refreshed_match is not None and short_term.get("matched_action_trajectory") is None:
                    short_term["matched_action_trajectory"] = refreshed_match.to_metadata()

        short_term["last_updated_task_id"] = task.task_id
        short_term["placeholder"] = False
        return dict(short_term)


class LongTermMemoryAgent(BaseAgent):
    async def execute(self, task: AgentTask, context: AgentContext):
        payload = dict(task.payload)
        memory = _ensure_memory_state(context, payload)
        long_term = memory.setdefault(
            "long_term",
            {
                "specific_task_patterns": [],
                "replicable_skills": [],
                "placeholder_candidates": [],
                "placeholder": True,
            },
        )

        decision = payload.get("decision")
        if isinstance(decision, Mapping):
            task_type = str(decision.get("type") or decision.get("task_type") or "none").strip()
            summary = str(decision.get("summary", "")).strip()
            if task_type != "none" and summary:
                candidates = list(long_term.get("placeholder_candidates", []))
                candidates.append(
                    {
                        "task_type": task_type,
                        "summary": summary,
                        "success": bool(payload.get("success", False)),
                    }
                )
                long_term["placeholder_candidates"] = candidates[-20:]

        long_term.setdefault("specific_task_patterns", [])
        long_term.setdefault("replicable_skills", [])
        long_term["placeholder"] = True
        return dict(long_term)
