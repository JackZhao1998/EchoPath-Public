from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .execution_agent import count_performed_actions, is_executed_result_record
from .models import AgentEvent, AgentResult, AgentTask
from .replay import build_replay_task, extract_successful_operations, normalize_replay_key


class InteractionLogger:
    def __init__(
        self,
        *,
        root_directory: Path,
        session_id: str,
        replay_store: Any = None,
    ) -> None:
        self.root_directory = Path(root_directory).resolve()
        self.session_id = session_id
        self.replay_store = replay_store
        self.session_directory = self.root_directory / session_id
        self.session_directory.mkdir(parents=True, exist_ok=True)

    def log_interaction(
        self,
        *,
        task: AgentTask,
        result: AgentResult,
        user_query: str,
        assistant_reply: str,
        session_title: str,
    ) -> Dict[str, Any]:
        output = result.output if isinstance(result.output, Mapping) else {}
        screenshots = self._extract_screenshot_report(result, output)
        executed_actions = self._extract_executed_actions(output)
        execution_summary = self._build_execution_summary(output.get("execution"))
        successful_operations = self._extract_successful_operations(output)
        replay_summary = self._build_replay_summary(
            task=task,
            user_query=user_query,
            output=output,
            successful_operations=successful_operations,
        )
        detail_path = self._detail_path(task)
        action_trajectory = self._upsert_action_trajectory(
            task=task,
            user_query=user_query,
            assistant_reply=assistant_reply,
            output=output,
            matched_at=result.finished_at.isoformat(),
            detail_path=detail_path,
        )
        detail_payload = {
            "record_type": "final",
            "session_id": self.session_id,
            "session_title": session_title,
            "task_id": task.task_id,
            "task_goal": task.goal,
            "task_created_at": task.created_at.isoformat(),
            "user_query": user_query,
            "assistant_reply": assistant_reply,
            "task": self._json_safe(output.get("task", {})),
            "routing": self._json_safe(output.get("routing", {})),
            "execution": execution_summary,
            "captured_screenshots": screenshots["captured"],
            "missing_screenshots": screenshots["missing"],
            "screenshot_details": screenshots["details"],
            "executed_actions": executed_actions,
            "executed_action_count": len(executed_actions),
            "successful_operations": successful_operations,
            "successful_operation_count": len(successful_operations),
            "replay": replay_summary,
            "action_trajectory": action_trajectory,
            "events": [self._serialize_event(event) for event in result.events],
            "result": {
                "success": result.success,
                "error": result.error,
                "started_at": result.started_at.isoformat(),
                "finished_at": result.finished_at.isoformat(),
                "duration_seconds": result.duration_seconds,
            },
        }
        self._write_json(detail_path, detail_payload)

        summary_payload = {
            "timestamp": result.finished_at.isoformat(),
            "session_id": self.session_id,
            "task_id": task.task_id,
            "user_query": user_query,
            "assistant_reply": assistant_reply,
            "task_type": output.get("task", {}).get("type") if isinstance(output.get("task"), Mapping) else None,
            "routing_agent": output.get("routing", {}).get("agent") if isinstance(output.get("routing"), Mapping) else None,
            "execution_success": (
                bool(output.get("execution", {}).get("success"))
                if isinstance(output.get("execution"), Mapping)
                else result.success
            ),
            "captured_screenshots": screenshots["captured"],
            "missing_screenshots": screenshots["missing"],
            "executed_actions": executed_actions,
            "executed_action_count": len(executed_actions),
            "successful_operation_count": len(successful_operations),
            "performed_action_count": execution_summary.get("performed_action_count", 0),
            "completed_without_action": execution_summary.get("completed_without_action", False),
            "replay_available": bool(replay_summary.get("available")),
            "replay_used": bool(replay_summary.get("used")),
            "replay_key": replay_summary.get("key"),
            "replay_source_task_id": replay_summary.get("source_task_id"),
            "replay_source_session_id": replay_summary.get("source_session_id"),
            "action_trajectory_path": (
                action_trajectory.get("path") if isinstance(action_trajectory, Mapping) else None
            ),
            "action_trajectory_replayable": (
                bool(action_trajectory.get("replayable"))
                if isinstance(action_trajectory, Mapping)
                else False
            ),
            "detail_path": str(detail_path),
        }
        self._append_jsonl(
            self.session_directory / "interactions.jsonl",
            summary_payload,
        )
        return summary_payload

    def log_partial_interaction(
        self,
        *,
        task: AgentTask,
        user_query: str,
        session_title: str,
        debug_state: Mapping[str, Any],
        latest_event: Optional[AgentEvent] = None,
    ) -> Dict[str, Any]:
        debug_snapshot = self._json_safe(dict(debug_state or {}))
        partial_payload = {
            "record_type": "partial",
            "update_type": "agent_event",
            "session_id": self.session_id,
            "session_title": session_title,
            "task_id": task.task_id,
            "task_goal": task.goal,
            "task_created_at": task.created_at.isoformat(),
            "user_query": user_query,
            "assistant_reply": None,
            "status": str(debug_snapshot.get("status", "running")),
            "event_count": self._count_debug_events(debug_snapshot),
            "captured_screenshots": self._captured_screenshots_from_debug_state(debug_snapshot),
            "latest_event": (
                self._serialize_event(latest_event)
                if latest_event is not None
                else self._json_safe(debug_snapshot.get("latest_event"))
            ),
            "debug_state": debug_snapshot,
            "result": {
                "success": None,
                "error": debug_snapshot.get("error"),
                "finished_at": debug_snapshot.get("finished_at"),
                "updated_at": debug_snapshot.get("updated_at"),
            },
        }
        self._write_json(self._detail_path(task), partial_payload)
        self._append_jsonl(self._partial_history_path(task), partial_payload)
        return partial_payload

    def _build_execution_summary(self, execution: Any) -> Dict[str, Any]:
        if not isinstance(execution, Mapping):
            return {
                "success": False,
                "error": None,
                "output": None,
                "performed_action_count": 0,
                "completed_without_action": False,
            }

        output = execution.get("output")
        summary: Dict[str, Any] = {
            "success": bool(execution.get("success")),
            "error": execution.get("error"),
            "performed_action_count": self._coerce_int(
                execution.get("performed_action_count"),
                default=count_performed_actions(output),
            ),
            "completed_without_action": bool(execution.get("completed_without_action", False)),
        }
        if isinstance(output, Mapping):
            for key in (
                "workflow",
                "completed",
                "halt_reason",
                "action_count",
                "command_count",
                "iteration_count",
                "goal",
                "observation_count",
            ):
                if key in output:
                    summary[key] = self._json_safe(output.get(key))
            if "results" in output and isinstance(output.get("results"), list):
                summary["result_count"] = len(output["results"])
            if "values" in output and isinstance(output.get("values"), Mapping):
                summary["value_keys"] = sorted(str(key) for key in output["values"].keys())
            if "action_result" in output and isinstance(output.get("action_result"), Mapping):
                summary["action_result_count"] = len(output["action_result"].get("results", []))
        else:
            summary["output"] = self._json_safe(output)
        return summary

    def _extract_screenshot_report(
        self,
        result: AgentResult,
        output: Mapping[str, Any],
    ) -> Dict[str, List[Any]]:
        paths: List[str] = []

        state_observations = result.state.get("desktop_observations")
        if isinstance(state_observations, list):
            for observation in state_observations:
                self._append_screenshot_path(paths, observation)

        execution = output.get("execution")
        if isinstance(execution, Mapping):
            execution_output = execution.get("output")
            if isinstance(execution_output, Mapping):
                for key in ("initial_observation", "final_observation"):
                    self._append_screenshot_path(paths, execution_output.get(key))
                iterations = execution_output.get("iterations")
                if isinstance(iterations, list):
                    for iteration in iterations:
                        if not isinstance(iteration, Mapping):
                            continue
                        for key in ("observation", "after_observation"):
                            self._append_screenshot_path(paths, iteration.get(key))

        for event in result.events:
            if event.name == "agent.desktop.observed":
                self._append_screenshot_path(paths, event.payload)

        details = [self._build_screenshot_detail(path) for path in paths]
        return {
            "captured": [detail["path"] for detail in details if detail["exists"]],
            "missing": [detail["path"] for detail in details if not detail["exists"]],
            "details": details,
        }

    def _extract_successful_operations(self, output: Mapping[str, Any]) -> List[Dict[str, Any]]:
        task_record = output.get("task")
        execution = output.get("execution")
        if not isinstance(task_record, Mapping) or not isinstance(execution, Mapping):
            return []
        execution_output = execution.get("output")
        return self._json_safe(
            extract_successful_operations(task_record, execution_output)
        )

    def _build_replay_summary(
        self,
        *,
        task: AgentTask,
        user_query: str,
        output: Mapping[str, Any],
        successful_operations: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        task_record = output.get("task")
        execution = output.get("execution")
        execution_success = bool(execution.get("success")) if isinstance(execution, Mapping) else False
        replay_task = (
            build_replay_task(task_record, successful_operations)
            if isinstance(task_record, Mapping) and execution_success
            else None
        )
        replay_usage = self._extract_replay_usage(task, output)
        return {
            "key": normalize_replay_key(user_query),
            "available": replay_task is not None,
            "task": self._json_safe(replay_task),
            "used": replay_usage["used"],
            "source_session_id": replay_usage.get("source_session_id"),
            "source_task_id": replay_usage.get("source_task_id"),
            "source_detail_path": replay_usage.get("source_detail_path"),
            "trajectory_path": replay_usage.get("trajectory_path"),
            "purpose_summary": replay_usage.get("purpose_summary"),
            "goal_coverage": replay_usage.get("goal_coverage"),
            "matched_on": replay_usage.get("matched_on"),
            "matched_user_query": replay_usage.get("matched_user_query"),
            "matched_at": replay_usage.get("matched_at"),
        }

    def _upsert_action_trajectory(
        self,
        *,
        task: AgentTask,
        user_query: str,
        assistant_reply: str,
        output: Mapping[str, Any],
        matched_at: str,
        detail_path: Path,
    ) -> Optional[Dict[str, Any]]:
        task_record = output.get("task")
        execution = output.get("execution")
        upsert_method = (
            getattr(self.replay_store, "upsert_runtime_trajectory", None)
            if self.replay_store is not None
            else None
        )
        if (
            not isinstance(task_record, Mapping)
            or not isinstance(execution, Mapping)
            or not callable(upsert_method)
        ):
            return None
        summary = upsert_method(
            user_query=user_query,
            task_record=task_record,
            execution=execution,
            session_id=self.session_id,
            task_id=task.task_id,
            matched_at=matched_at,
            assistant_reply=assistant_reply,
            source_detail_path=detail_path,
        )
        return self._json_safe(summary) if isinstance(summary, Mapping) else None

    def _extract_executed_actions(self, output: Mapping[str, Any]) -> List[Dict[str, Any]]:
        execution = output.get("execution")
        if not isinstance(execution, Mapping):
            return []
        execution_output = execution.get("output")
        if not isinstance(execution_output, Mapping):
            return []

        actions: List[Dict[str, Any]] = []
        self._extend_actions_from_mapping(actions, execution_output)
        action_result = execution_output.get("action_result")
        if isinstance(action_result, Mapping):
            self._extend_actions_from_mapping(actions, action_result)

        iterations = execution_output.get("iterations")
        if isinstance(iterations, list):
            for iteration in iterations:
                if not isinstance(iteration, Mapping):
                    continue
                appended_result = False
                action_result = iteration.get("action_result")
                if isinstance(action_result, Mapping):
                    before = len(actions)
                    self._extend_actions_from_mapping(
                        actions,
                        action_result,
                        prefix={"iteration": iteration.get("iteration")},
                    )
                    appended_result = len(actions) > before
                action = iteration.get("action")
                if isinstance(action, Mapping) and not appended_result:
                    normalized = {"iteration": iteration.get("iteration")}
                    normalized.update(self._json_safe(action))
                    if is_executed_result_record(normalized):
                        actions.append(normalized)
        return actions

    def _extend_actions_from_mapping(
        self,
        actions: List[Dict[str, Any]],
        payload: Mapping[str, Any],
        *,
        prefix: Optional[Mapping[str, Any]] = None,
    ) -> None:
        results = payload.get("results")
        if not isinstance(results, list):
            return
        for item in results:
            if not is_executed_result_record(item):
                continue
            normalized: Dict[str, Any] = {}
            if prefix:
                normalized.update(self._json_safe(prefix))
            normalized.update(self._json_safe(item))
            actions.append(normalized)

    def _build_screenshot_detail(self, path: str) -> Dict[str, Any]:
        candidate = Path(path)
        exists = False
        size_bytes = 0
        try:
            exists = candidate.is_file()
            if exists:
                size_bytes = int(candidate.stat().st_size)
        except OSError:
            exists = False
            size_bytes = 0
        return {
            "path": path,
            "exists": exists,
            "size_bytes": size_bytes,
        }

    def _append_screenshot_path(self, paths: List[str], payload: Any) -> None:
        path = None
        if isinstance(payload, Mapping):
            candidate = payload.get("path")
            if candidate:
                path = str(candidate)
        elif payload:
            path = str(payload)

        if path and path not in paths:
            paths.append(path)

    def _extract_replay_usage(self, task: AgentTask, output: Mapping[str, Any]) -> Dict[str, Any]:
        replay = output.get("replay")
        if not isinstance(replay, Mapping):
            replay = task.payload.get("replay")
        if not isinstance(replay, Mapping):
            return {"used": False}
        return {
            "used": bool(replay.get("used")),
            "source_session_id": replay.get("source_session_id"),
            "source_task_id": replay.get("source_task_id"),
            "source_detail_path": replay.get("source_detail_path"),
            "trajectory_path": replay.get("trajectory_path"),
            "purpose_summary": replay.get("purpose_summary"),
            "goal_coverage": replay.get("goal_coverage"),
            "matched_on": replay.get("matched_on"),
            "matched_user_query": replay.get("matched_user_query"),
            "matched_at": replay.get("matched_at"),
        }

    def _serialize_event(self, event: AgentEvent) -> Dict[str, Any]:
        return {
            "name": event.name,
            "message": event.message,
            "payload": self._json_safe(event.payload),
            "run_id": event.run_id,
            "level": event.level.value if hasattr(event.level, "value") else str(event.level),
            "timestamp": event.timestamp.isoformat(),
        }

    def _detail_path(self, task: AgentTask) -> Path:
        return self.session_directory / f"interaction-{task.task_id}.json"

    def _partial_history_path(self, task: AgentTask) -> Path:
        return self.session_directory / f"interaction-{task.task_id}.events.jsonl"

    def _write_json(self, path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self._json_safe(dict(payload)), indent=2) + "\n",
            encoding="utf-8",
        )

    def _append_jsonl(self, path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self._json_safe(dict(payload))) + "\n")

    def _count_debug_events(self, debug_state: Mapping[str, Any]) -> int:
        events = debug_state.get("events")
        if not isinstance(events, list):
            return 0
        return sum(1 for item in events if isinstance(item, Mapping))

    def _captured_screenshots_from_debug_state(self, debug_state: Mapping[str, Any]) -> List[str]:
        screenshots = debug_state.get("screenshots")
        if not isinstance(screenshots, list):
            return []
        paths: List[str] = []
        for item in screenshots:
            if not isinstance(item, Mapping):
                continue
            candidate = item.get("path")
            if candidate:
                path = str(candidate)
                if path not in paths:
                    paths.append(path)
        return paths

    def _coerce_int(self, value: Any, *, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    def _json_safe(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): self._json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._json_safe(item) for item in value]
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)
