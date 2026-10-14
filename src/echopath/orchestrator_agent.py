from __future__ import annotations

import inspect
from copy import deepcopy
from typing import Any, Dict, Mapping, Optional, Tuple

from .agent import BaseAgent
from .execution_agent import ExecutionAgent, merge_failure_reply
from .gui_agent import GUIAgent, ImageGroundingAgent
from .memory_agent import LongTermMemoryAgent, ShortTermMemoryAgent
from .memory_replay import MEMORY_REPLAY_STATE_CONTRACTS_KEY, MEMORY_REPLAY_VISUAL_BINDINGS_KEY
from .models import AgentTask
from .planning_agent import PlanningAgent, PlanningDecision
from .runtime import AgentContext
from .tool_agent import ToolAgent


class OrchestratorAgent(BaseAgent):
    def __init__(
        self,
        name: str,
        description: str = "",
        *,
        router_name: str = "default",
        desktop_skill_name: str = "pyautogui-desktop",
        command_skill_name: str = "command-line",
        file_skill_name: str = "workspace-files",
        desktop_reasoning_effort: str = "medium",
        desktop_max_iterations: int = 100,
        interaction_replay_store_name: str = "interaction-replay-store",
        planning_agent: Optional[PlanningAgent] = None,
        gui_agent: Optional[GUIAgent] = None,
        tool_agent: Optional[ToolAgent] = None,
        short_term_memory_agent: Optional[ShortTermMemoryAgent] = None,
        long_term_memory_agent: Optional[LongTermMemoryAgent] = None,
        **kwargs,
    ) -> None:
        self._child_agents: Tuple[BaseAgent, ...] = ()
        super().__init__(name=name, description=description, **kwargs)
        self.router_name = router_name
        self.desktop_skill_name = desktop_skill_name
        self.command_skill_name = command_skill_name
        self.file_skill_name = file_skill_name
        self.interaction_replay_store_name = interaction_replay_store_name

        execution_agent = ExecutionAgent(
            name=f"{name}-execution",
            description="Shared execution agent for skill-backed work.",
            working_directory=self.working_directory,
            registry=self.registry,
            event_bus=self.event_bus,
            equipped_skills=self.equipped_skill_names,
        )
        image_grounding_agent = ImageGroundingAgent(
            name=f"{name}-image-grounding",
            description="Grounds GUI targets to exact coordinates.",
            router_name=router_name,
            working_directory=self.working_directory,
            registry=self.registry,
            event_bus=self.event_bus,
            equipped_skills=self.equipped_skill_names,
        )
        self.planning_agent = planning_agent or PlanningAgent(
            name=f"{name}-planning",
            description="Handles task-type routing and execution planning.",
            router_name=router_name,
            working_directory=self.working_directory,
            registry=self.registry,
            event_bus=self.event_bus,
            equipped_skills=self.equipped_skill_names,
        )
        self.gui_agent = gui_agent or GUIAgent(
            name=f"{name}-gui",
            description="Owns visible desktop GUI workflows.",
            router_name=router_name,
            planning_agent=self.planning_agent,
            image_grounding_agent=image_grounding_agent,
            execution_agent=execution_agent,
            desktop_skill_name=desktop_skill_name,
            default_desktop_reasoning_effort=desktop_reasoning_effort,
            default_desktop_max_iterations=desktop_max_iterations,
            working_directory=self.working_directory,
            registry=self.registry,
            event_bus=self.event_bus,
            equipped_skills=self.equipped_skill_names,
        )
        self.tool_agent = tool_agent or ToolAgent(
            name=f"{name}-tool",
            description="Owns command, file, and external tool workflows.",
            execution_agent=execution_agent,
            command_skill_name=command_skill_name,
            file_skill_name=file_skill_name,
            working_directory=self.working_directory,
            registry=self.registry,
            event_bus=self.event_bus,
            equipped_skills=self.equipped_skill_names,
        )
        self.short_term_memory_agent = short_term_memory_agent or ShortTermMemoryAgent(
            name=f"{name}-short-term-memory",
            description="Placeholder short-term memory agent.",
            working_directory=self.working_directory,
            registry=self.registry,
            event_bus=self.event_bus,
            equipped_skills=self.equipped_skill_names,
        )
        self.long_term_memory_agent = long_term_memory_agent or LongTermMemoryAgent(
            name=f"{name}-long-term-memory",
            description="Placeholder long-term memory agent.",
            working_directory=self.working_directory,
            registry=self.registry,
            event_bus=self.event_bus,
            equipped_skills=self.equipped_skill_names,
        )

        self._child_agents = (
            execution_agent,
            image_grounding_agent,
            self.planning_agent,
            self.gui_agent,
            self.tool_agent,
            self.short_term_memory_agent,
            self.long_term_memory_agent,
        )
        self._propagate_equipped_skills()

    def equip_skills(self, skill_names) -> None:
        super().equip_skills(skill_names)
        self._propagate_equipped_skills()

    def allow_all_skills(self) -> None:
        super().allow_all_skills()
        self._propagate_equipped_skills()

    async def plan(self, task: AgentTask, context: AgentContext):
        return {
            "goal": task.goal,
            "steps": [
                "Capture or refresh the short-term memory snapshot.",
                "Ask the planning agent to classify the request into a task type.",
                "Route strictly by task type to the GUI or tool agent.",
                "Refresh placeholder long-term memory after execution.",
            ],
        }

    async def execute(self, task: AgentTask, context: AgentContext):
        continuation = task.payload.get("native_continuation")
        if isinstance(continuation, Mapping):
            final_output = await self._execute_routed_decision(task, context, continuation)
            return await self._finish_replay_execution(task, context, final_output)
        await self._run_memory_agent(
            self.short_term_memory_agent,
            task,
            context,
            phase="intake",
            decision=None,
            execution=None,
        )

        replay_decision = await self._find_replay_decision(task, context)
        if replay_decision is not None:
            await self._run_memory_agent(
                self.short_term_memory_agent,
                task,
                context,
                phase="planned",
                decision=replay_decision["task"],
                execution=None,
            )
            final_output = await self._execute_routed_decision(task, context, replay_decision)
            return await self._finish_replay_execution(task, context, final_output)

        if memory_replay_required(task.payload):
            replay_store = context.get_service(self.interaction_replay_store_name)
            lookup_summary = getattr(replay_store, "last_lookup_summary", None)
            final_output = {
                "assistant_reply": "Memory replay was required, but no compatible stored replay was selected.",
                "task": {
                    "type": "desktop_task",
                    "summary": "Memory replay required",
                    "goal": task.goal,
                    "payload": {},
                },
                "execution": {
                    "success": False,
                    "error": "Memory replay was required, but no compatible stored replay was selected.",
                    "output": {
                        "memory_replay_required": True,
                        "memory_replay_lookup": deepcopy(lookup_summary)
                        if isinstance(lookup_summary, Mapping)
                        else None,
                    },
                },
                "routing": {
                    "task_type": "desktop_task",
                    "agent": None,
                    "memory_replay_required": True,
                },
            }
            await context.emit(
                "agent.replay.required_missing",
                "Memory replay was required, but no compatible stored replay was selected.",
                {
                    "memory_replay_lookup": final_output["execution"]["output"][
                        "memory_replay_lookup"
                    ],
                },
            )
            await self._run_memory_agent(
                self.short_term_memory_agent,
                task,
                context,
                phase="completed",
                decision=final_output.get("task"),
                execution=final_output.get("execution"),
            )
            await self._run_memory_agent(
                self.long_term_memory_agent,
                task,
                context,
                phase="completed",
                decision=final_output.get("task"),
                execution=final_output.get("execution"),
                success=False,
            )
            return final_output

        planning_task = AgentTask(
            goal="Plan the user's request for routing.",
            payload=dict(task.payload),
            metadata={"phase": "routing", **dict(task.metadata)},
            task_id=task.task_id,
        )
        planning_result = await self._run_child_agent(self.planning_agent, planning_task, context)
        if not planning_result.success or not isinstance(planning_result.output, Mapping):
            return {
                "assistant_reply": "I ran into an error while planning the request.",
                "task": {
                    "type": "none",
                    "summary": "",
                    "goal": "",
                    "payload": {},
                },
                "execution": {
                    "success": False,
                    "error": planning_result.error or "Planning failed.",
                    "output": None,
                },
                "routing": {
                    "task_type": "none",
                    "agent": self.planning_agent.name,
                },
            }

        decision = PlanningDecision.from_output(planning_result.output)
        await self._run_memory_agent(
            self.short_term_memory_agent,
            task,
            context,
            phase="planned",
            decision=planning_result.output.get("task"),
            execution=None,
        )

        if decision.task_type == "none":
            final_output = decision.to_output()
            final_output["routing"] = {"task_type": "none", "agent": None}
            await self._run_memory_agent(
                self.short_term_memory_agent,
                task,
                context,
                phase="completed",
                decision=final_output.get("task"),
                execution=final_output.get("execution"),
            )
            await self._run_memory_agent(
                self.long_term_memory_agent,
                task,
                context,
                phase="completed",
                decision=final_output.get("task"),
                execution=final_output.get("execution"),
                success=True,
            )
            return final_output

        final_output = await self._execute_routed_decision(
            task,
            context,
            decision.to_output(),
        )

        await self._run_memory_agent(
            self.short_term_memory_agent,
            task,
            context,
            phase="completed",
            decision=final_output.get("task"),
            execution=final_output.get("execution"),
        )
        await self._run_memory_agent(
            self.long_term_memory_agent,
            task,
            context,
            phase="completed",
            decision=final_output.get("task"),
            execution=final_output.get("execution"),
            success=bool(final_output.get("execution", {}).get("success", False)),
        )
        return final_output

    async def _finish_replay_execution(
        self, task: AgentTask, context: AgentContext, final_output: Dict[str, Any]
    ) -> Dict[str, Any]:
        execution = final_output.get("execution", {})
        if execution.get("status") == "awaiting_native":
            return final_output
        await self._run_memory_agent(
            self.short_term_memory_agent, task, context, phase="completed",
            decision=final_output.get("task"), execution=execution,
        )
        await self._run_memory_agent(
            self.long_term_memory_agent, task, context, phase="completed",
            decision=final_output.get("task"), execution=execution,
            success=bool(execution.get("success", False)),
        )
        return final_output

    async def _find_replay_decision(
        self,
        task: AgentTask,
        context: AgentContext,
    ) -> Optional[Dict[str, Any]]:
        user_query = str(task.payload.get("message", "")).strip()
        if not user_query:
            return None

        replay_store = context.get_service(self.interaction_replay_store_name)
        if replay_store is None or not hasattr(replay_store, "find_latest_successful_match"):
            return None

        task_matcher = getattr(replay_store, "find_latest_successful_match_for_task", None)
        if callable(task_matcher):
            replay_match = task_matcher(user_query, current_task=task.payload)
            if inspect.isawaitable(replay_match):
                replay_match = await replay_match
        else:
            replay_match = replay_store.find_latest_successful_match(user_query)
        if replay_match is None:
            return None

        replay_metadata = replay_match.to_metadata()
        replay_adaptation = replay_metadata.get("adaptation")
        memory_path_id = None
        compatibility_score = None
        if isinstance(replay_adaptation, Mapping):
            memory_path_id = replay_adaptation.get("memory_path_id")
            compatibility_score = replay_adaptation.get("score")
        await context.emit(
            "agent.replay.used",
            "Reusing a stored replayable action trajectory.",
            {
                "source_session_id": replay_metadata.get("source_session_id"),
                "source_task_id": replay_metadata.get("source_task_id"),
                "matched_user_query": replay_metadata.get("matched_user_query"),
                "matched_on": replay_metadata.get("matched_on"),
                "purpose_summary": replay_metadata.get("purpose_summary"),
                "goal_coverage": replay_metadata.get("goal_coverage"),
                "operation_count": replay_metadata.get("operation_count"),
                "trajectory_steps_preview": replay_metadata.get("trajectory_steps_preview"),
                "adaptation": replay_adaptation,
                "memory_path_id": memory_path_id,
                "compatibility_score": compatibility_score,
            },
        )
        return {
            "assistant_reply": "I reused the previously stored action trajectory for this request.",
            "task": _merge_replay_task_for_current_request(
                task.payload,
                replay_match.replay_task,
                user_query=user_query,
                purpose_summary=str(replay_metadata.get("purpose_summary") or ""),
            ),
            "replay": replay_metadata,
        }

    async def _execute_routed_decision(
        self,
        task: AgentTask,
        context: AgentContext,
        decision_output: Mapping[str, Any],
    ) -> Dict[str, Any]:
        decision = PlanningDecision.from_output(decision_output)
        route_agent = self._route_agent_for_task_type(decision.task_type)
        route_payload = {
            "assistant_reply": decision.assistant_reply,
            "task": decision.to_output()["task"],
            "execution_constraints": dict(task.payload.get("execution_constraints") or {}),
        }
        replay_metadata = decision_output.get("replay")
        if isinstance(replay_metadata, Mapping):
            route_payload["replay"] = dict(replay_metadata)

        route_task = AgentTask(
            goal=decision.task_goal or task.goal,
            payload=route_payload,
            metadata={
                "parent_task_id": task.task_id,
                "task_type": decision.task_type,
                **dict(task.metadata),
            },
            task_id=task.task_id,
        )
        route_result = await self._run_child_agent(route_agent, route_task, context)

        if route_result.success and isinstance(route_result.output, Mapping):
            final_output = dict(route_result.output)
        else:
            final_output = decision.to_output()
            error_message = route_result.error or "Execution failed."
            final_output["assistant_reply"] = merge_failure_reply(
                decision.assistant_reply,
                decision.task_summary,
                error_message,
            )
            final_output["execution"] = {
                "success": False,
                "error": error_message,
                "output": None,
            }

        final_output["routing"] = {
            "task_type": decision.task_type,
            "agent": route_agent.name,
        }
        if isinstance(replay_metadata, Mapping):
            final_output["replay"] = dict(replay_metadata)
        return final_output

    async def _run_child_agent(
        self,
        agent: BaseAgent,
        task: AgentTask,
        context: AgentContext,
    ):
        return await agent.run(task, context=context.fork(task=task))

    async def _run_memory_agent(
        self,
        agent: BaseAgent,
        task: AgentTask,
        context: AgentContext,
        *,
        phase: str,
        decision: Optional[Mapping[str, Any]],
        execution: Optional[Mapping[str, Any]],
        success: Optional[bool] = None,
    ) -> None:
        memory_task = AgentTask(
            goal=f"Update {agent.name} for the current orchestration run.",
            payload={
                "phase": phase,
                "message": str(task.payload.get("message") or task.goal),
                "memory": task.payload.get("memory"),
                "decision": dict(decision) if isinstance(decision, Mapping) else None,
                "execution": dict(execution) if isinstance(execution, Mapping) else None,
                "success": success,
            },
            metadata={"task_type": "memory_update", **dict(task.metadata)},
            task_id=task.task_id,
        )
        result = await self._run_child_agent(agent, memory_task, context)
        if result.success:
            return
        await context.emit(
            "agent.memory.update.failed",
            f"Memory agent '{agent.name}' failed to update placeholder memory.",
            {"agent": agent.name, "error": result.error},
        )

    def _route_agent_for_task_type(self, task_type: str) -> BaseAgent:
        if task_type == "desktop_task":
            return self.gui_agent
        if task_type in {"command_task", "file_task"}:
            return self.tool_agent
        return self.planning_agent

    def _propagate_equipped_skills(self) -> None:
        if not self._child_agents:
            return
        if self.equipped_skill_names is None:
            for agent in self._child_agents:
                agent.allow_all_skills()
            return
        for agent in self._child_agents:
            agent.equip_skills(self.equipped_skill_names)


def _describe_current_request(
    task: Dict[str, Any],
    *,
    user_query: str = "",
    purpose_summary: str = "",
) -> Dict[str, Any]:
    """Restate a replayed task in terms of the request that triggered it.

    A stored trajectory carries the summary and goal of the run it was recorded
    from. Reusing those verbatim makes EchoPath report the remembered task
    instead of the one it just performed -- a 10-second timer request replayed
    from a 15-second memory was reported as "Start a 15-second Clock timer and
    close Clock after it finishes". The actions adapt to the new request, so the
    description has to as well.
    """
    summary = str(user_query or "").strip() or str(purpose_summary or "").strip()
    if summary:
        task["summary"] = summary
    goal = str(user_query or "").strip()
    if goal:
        task["goal"] = goal
    return task


def _merge_replay_task_for_current_request(
    current_payload: Mapping[str, Any],
    replay_task: Mapping[str, Any],
    *,
    user_query: str = "",
    purpose_summary: str = "",
) -> Dict[str, Any]:
    """Preserve current-run desktop payload fields while reusing replay actions."""

    def _replay_task_only() -> Dict[str, Any]:
        return _describe_current_request(
            deepcopy(dict(replay_task)),
            user_query=user_query,
            purpose_summary=purpose_summary,
        )

    current_task = current_payload.get("task")
    if not isinstance(current_task, Mapping):
        return _replay_task_only()

    current_type = str(current_task.get("type", "")).strip().lower()
    replay_type = str(replay_task.get("type", "")).strip().lower()
    if current_type != "desktop_task" or replay_type != "desktop_task":
        return _replay_task_only()

    current_task_payload = current_task.get("payload")
    replay_payload = replay_task.get("payload")
    if not isinstance(current_task_payload, Mapping) or not isinstance(replay_payload, Mapping):
        return _replay_task_only()

    replay_actions = replay_payload.get("actions")
    if not isinstance(replay_actions, list):
        return _replay_task_only()

    merged_task = deepcopy(dict(current_task))
    merged_payload = deepcopy(dict(current_task_payload))
    merged_payload["actions"] = deepcopy(replay_actions)

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
        "memory_replay_required",
        MEMORY_REPLAY_STATE_CONTRACTS_KEY,
        "memory_segments",
        "memory_flexible_action_inputs",
    ):
        if key in replay_payload:
            merged_payload[key] = deepcopy(replay_payload[key])

    for key in ("capture_after_action", "service_name"):
        if key not in merged_payload and key in replay_payload:
            merged_payload[key] = deepcopy(replay_payload[key])

    merged_task["payload"] = merged_payload
    return merged_task


def memory_replay_required(payload: Mapping[str, Any]) -> bool:
    if bool(payload.get("memory_replay_required")):
        return True
    task = payload.get("task")
    if not isinstance(task, Mapping):
        return False
    task_payload = task.get("payload")
    return bool(
        isinstance(task_payload, Mapping)
        and task_payload.get("memory_replay_required")
    )
