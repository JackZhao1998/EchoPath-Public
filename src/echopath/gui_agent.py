from __future__ import annotations

import asyncio
import inspect
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .agent import BaseAgent
from .agent_utils import extract_json_object, optional_float, optional_int, required_int
from .execution_agent import (
    ExecutionAgent,
    build_execution_failure_output,
    build_execution_success_output,
    coerce_task_record,
)
from .models import AgentTask
from .memory_segments import runtime_segments
from .segment_execution import run_segmented_replay
from .memory_replay import (
    MemoryReplayReaimingError,
    bind_memory_replay_visual_action,
    memory_replay_action_is_optional,
    memory_replay_optional_chain_resume_index,
    memory_replay_state_contract_for_action,
    memory_replay_visual_binding_for_action,
    memory_replay_visual_reaim_required,
)
from .planning_agent import (
    DESKTOP_GROUNDABLE_ACTIONS,
    PlanningAgent,
    summarize_desktop_action,
)
from .router import ModelRequest
from .runtime import AgentContext
from .visual_grounding import (
    locate_visual_grounding_target,
    visual_grounding_allows_model_fallback,
)
from .replay_decision import choose_replay_target, decision_model_settings

DEFAULT_DOUBLE_CLICK_INTERVAL_SECONDS = 0.15
DOWNLOAD_TASK_TERMS = (
    "download",
    "downloaded",
    "downloads",
    "save",
    "saved",
    "pdf",
    ".pdf",
)
PARTIAL_DOWNLOAD_SUFFIXES = (
    ".crdownload",
    ".download",
    ".part",
    ".tmp",
)


class DesktopExecutionError(RuntimeError):
    def __init__(self, message: str, *, execution_output: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.execution_output = dict(execution_output)


class ImageGroundingAgent(BaseAgent):
    def __init__(
        self,
        name: str,
        description: str = "",
        *,
        router_name: str = "default",
        **kwargs,
    ) -> None:
        super().__init__(name=name, description=description, **kwargs)
        self.router_name = router_name

    async def execute(self, task: AgentTask, context: AgentContext):
        payload = dict(task.payload)
        observation = payload.get("observation")
        next_step = payload.get("next_step")
        task_payload = payload.get("task_payload")
        if not isinstance(observation, Mapping):
            raise ValueError("Image grounding requires an observation payload.")
        if not isinstance(next_step, Mapping):
            raise ValueError("Image grounding requires a next_step mapping.")
        if not isinstance(task_payload, Mapping):
            raise ValueError("Image grounding requires a task_payload mapping.")

        visual_grounding = locate_visual_grounding_target(
            observation_path=Path(str(observation["path"])),
            next_step=next_step,
            task_payload=task_payload,
            working_directory=self.working_directory,
        )
        if visual_grounding is not None:
            await context.emit(
                "agent.desktop.cv_grounded",
                "Visual memory grounding attempted for the desktop target.",
                {
                    "iteration": max(1, int(payload.get("iteration", 1))),
                    "located": visual_grounding.get("located"),
                    "x": visual_grounding.get("x"),
                    "y": visual_grounding.get("y"),
                    "confidence": visual_grounding.get("confidence"),
                    "reason": visual_grounding.get("reason"),
                    "source": visual_grounding.get("source"),
                    "reference_image_path": visual_grounding.get("reference_image_path"),
                    "bbox": visual_grounding.get("bbox"),
                    "match": visual_grounding.get("match"),
                },
            )
            if visual_grounding.get("located") or not visual_grounding_allows_model_fallback(
                task_payload
            ):
                return visual_grounding

        request = self._build_grounding_request(
            str(payload.get("goal") or task.goal),
            observation,
            next_step,
            iteration=max(1, int(payload.get("iteration", 1))),
            payload=task_payload,
        )
        response = await context.call_model(
            request,
            router_name=str(task_payload.get("grounding_router_name", self.router_name)),
        )
        grounding = parse_grounding_response(response.output_text)
        grounding["source"] = f"model:{task_payload.get('grounding_router_name', self.router_name)}"
        grounding["model"] = response.model
        grounding["response_id"] = response.response_id
        grounding["request_id"] = response.request_id
        await context.emit(
            "agent.desktop.grounded",
            f"Image grounding completed for iteration {payload.get('iteration', 1)}.",
            {
                "iteration": max(1, int(payload.get("iteration", 1))),
                "located": grounding["located"],
                "model": response.model,
                "x": grounding.get("x"),
                "y": grounding.get("y"),
                "confidence": grounding.get("confidence"),
                "reason": grounding.get("reason"),
            },
        )
        return grounding

    def _build_grounding_request(
        self,
        goal: str,
        observation: Mapping[str, Any],
        next_step: Mapping[str, Any],
        *,
        iteration: int,
        payload: Mapping[str, Any],
    ) -> ModelRequest:
        observation_path = Path(str(observation["path"]))
        reference_images = grounding_reference_image_paths_for_step(
            payload,
            next_step,
            self.working_directory,
        )
        prompt_payload = {
            "goal": goal,
            "iteration": iteration,
            "next_step": dict(next_step),
            "observation_path": str(observation_path),
            "observation_metadata": dict(observation),
            "reference_images": [
                {"index": index + 1, "path": str(path)}
                for index, path in enumerate(reference_images)
            ],
        }
        instructions = (
            "You are the EchoPath image grounding agent. Review the screenshot and locate the "
            "exact pixel coordinates for the requested target. Respond with JSON only using this "
            "shape: {\"located\": boolean, \"x\": integer|null, \"y\": integer|null, "
            "\"confidence\": number, \"reason\": string}. Only return coordinates when the target "
            "is clearly visible and you are confident. If the target is missing or ambiguous, "
            "return located=false with x=null and y=null."
        )
        if reference_images:
            instructions = (
                f"{instructions}\n\nReference crop images are attached after the live screenshot. "
                "Use them as visual evidence for the exact memorized UI target."
            )
        if bool(payload.get("grounding_allow_repair_action")):
            instructions = (
                f"{instructions}\n\nThis is a bounded replay-step fallback, not a full task plan. "
                "If the memorized target is visible, return its coordinates and set "
                "target_role=\"memory_target\". If the target is hidden by a transient dialog, "
                "onboarding screen, keyring prompt, or similar blocker, you may instead return one "
                "safe dismiss/continue/cancel/close coordinate that should expose the same target, "
                "and set target_role=\"repair_action\". Do not advance the broader task or invent "
                "multi-step plans."
            )
        grounding_prompt = str(payload.get("grounding_prompt", "")).strip()
        if grounding_prompt:
            instructions = f"{instructions}\n\nAdditional grounding guidance:\n{grounding_prompt}"

        request_input: List[Dict[str, Any]] = [
            {"type": "text", "text": json.dumps(prompt_payload, indent=2)},
            {"type": "local_image", "path": str(observation_path)},
        ]
        request_input.extend(
            {"type": "local_image", "path": str(path)} for path in reference_images
        )
        return ModelRequest(
            model=str(payload.get("grounding_model", payload.get("planner_model", "default"))),
            instructions=instructions,
            input=request_input,
            reasoning_effort=payload.get("grounding_reasoning_effort"),
            temperature=payload.get("grounding_temperature"),
            max_output_tokens=payload.get("grounding_max_output_tokens"),
        )


class GUIAgent(BaseAgent):
    def __init__(
        self,
        name: str,
        description: str = "",
        *,
        router_name: str = "default",
        planning_agent: Optional[PlanningAgent] = None,
        image_grounding_agent: Optional[ImageGroundingAgent] = None,
        execution_agent: Optional[ExecutionAgent] = None,
        desktop_skill_name: str = "pyautogui-desktop",
        default_desktop_reasoning_effort: str = "medium",
        default_desktop_max_iterations: int = 100,
        **kwargs,
    ) -> None:
        super().__init__(name=name, description=description, **kwargs)
        self.router_name = router_name
        self.planning_agent = planning_agent or PlanningAgent(
            name=f"{name}-planner",
            description="Plans the next GUI step.",
            router_name=router_name,
            working_directory=self.working_directory,
            registry=self.registry,
            event_bus=self.event_bus,
            equipped_skills=self.equipped_skill_names,
        )
        self.image_grounding_agent = image_grounding_agent or ImageGroundingAgent(
            name=f"{name}-grounder",
            description="Grounds GUI targets to exact pixels.",
            router_name=router_name,
            working_directory=self.working_directory,
            registry=self.registry,
            event_bus=self.event_bus,
            equipped_skills=self.equipped_skill_names,
        )
        self.execution_agent = execution_agent or ExecutionAgent(
            name=f"{name}-execution",
            description="Executes GUI-domain skills.",
            working_directory=self.working_directory,
            registry=self.registry,
            event_bus=self.event_bus,
            equipped_skills=self.equipped_skill_names,
        )
        self.desktop_skill_name = desktop_skill_name
        self.default_desktop_reasoning_effort = (
            str(default_desktop_reasoning_effort or "medium").strip().lower() or "medium"
        )
        self.default_desktop_max_iterations = max(1, int(default_desktop_max_iterations or 100))

    async def plan(self, task: AgentTask, context: AgentContext):
        return {
            "goal": task.goal,
            "steps": [
                "Inspect the routed GUI task.",
                "Run the desktop GUI workflow.",
                "For desktop work, observe, plan, ground, act, and verify until complete.",
            ],
        }

    async def execute(self, task: AgentTask, context: AgentContext):
        task_record = coerce_task_record(task.goal, task.payload)
        assistant_reply = str(task.payload.get("assistant_reply", "")).strip()
        task_type = task_record["type"]

        if task_type == "desktop_task":
            return await self._execute_desktop_task(task, context, task_record, assistant_reply)

        return build_execution_failure_output(
            assistant_reply=assistant_reply,
            task_record=task_record,
            error=f"GUI agent does not handle task type '{task_type}'.",
        )

    async def _execute_skill_task(
        self,
        task: AgentTask,
        context: AgentContext,
        task_record: Mapping[str, Any],
        assistant_reply: str,
        skill_name: str,
    ) -> Dict[str, Any]:
        execution_task = AgentTask(
            goal=str(task_record.get("goal") or task.goal),
            payload={
                "assistant_reply": assistant_reply,
                "task": dict(task_record),
                "skill_name": skill_name,
                "skill_payload": dict(task_record.get("payload", {})),
            },
            metadata={"task_type": task_record["type"], **dict(task.metadata)},
            task_id=task.task_id,
        )
        result = await self.execution_agent.run(
            execution_task,
            context=context.fork(task=execution_task),
        )
        if result.success and result.output is not None:
            return result.output

        return build_execution_failure_output(
            assistant_reply=assistant_reply,
            task_record=task_record,
            error=result.error or "GUI execution failed.",
        )

    async def _execute_desktop_task(
        self,
        task: AgentTask,
        context: AgentContext,
        task_record: Mapping[str, Any],
        assistant_reply: str,
    ) -> Dict[str, Any]:
        desktop_payload = self._normalize_desktop_payload(task_record)
        constraints = task.payload.get("execution_constraints")
        if isinstance(constraints, Mapping):
            desktop_payload["native_handoff_supported"] = bool(constraints.get("native_handoff_supported"))
        replay_metadata = task.payload.get("replay")
        if isinstance(replay_metadata, Mapping):
            desktop_payload["replay"] = dict(replay_metadata)
        desktop_payload.setdefault(
            "observation_task_id",
            str(task.metadata.get("parent_task_id") or task.task_id),
        )
        goal = str(task_record.get("goal") or "Execute desktop GUI task.")

        try:
            execution_output = await self._run_desktop_task(context, goal, desktop_payload)
        except DesktopExecutionError as error:
            return build_execution_failure_output(
                assistant_reply=assistant_reply,
                task_record=task_record,
                error=str(error),
                execution_output=error.execution_output,
            )
        except Exception as error:
            return build_execution_failure_output(
                assistant_reply=assistant_reply,
                task_record=task_record,
                error=str(error),
            )

        if execution_output.get("status") == "awaiting_native":
            return {
                "assistant_reply": "Replay is paused for the calling agent to complete a native segment.",
                "task": dict(task_record),
                "execution": {"success": False, "status": "awaiting_native", "error": None,
                              "output": execution_output},
            }
        if not bool(execution_output.get("completed")):
            return build_execution_failure_output(
                assistant_reply=assistant_reply,
                task_record=task_record,
                error=str(execution_output.get("halt_reason") or "Desktop task did not complete."),
                execution_output=execution_output,
            )

        return build_execution_success_output(
            assistant_reply=assistant_reply,
            task_record=task_record,
            execution_output=execution_output,
        )

    async def _run_desktop_task(
        self, context: AgentContext, goal: str, payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        from .run_state import current_control, DesktopResumed
        control = current_control.get()
        if control:
            control.desktop_scope = True
        resumed = False
        try:
            while True:
                try:
                    if resumed:
                        fresh = dict(payload)
                        fresh.pop('actions', None)
                        fresh['verify_goal_completion'] = True
                        return await self._run_planned_desktop_task(context, goal, fresh)
                    return await self._run_desktop_task_uninterrupted(context, goal, payload)
                except DesktopResumed:
                    resumed = True
        finally:
            if control:
                control.desktop_scope = False

    async def _run_desktop_task_uninterrupted(
        self,
        context: AgentContext,
        goal: str,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        if runtime_segments(payload):
            return await run_segmented_replay(self, context, goal, payload)
        scripted_actions = payload.get("actions")
        if isinstance(scripted_actions, list):
            try:
                replay_output = await self._run_scripted_desktop_task(
                    context,
                    goal,
                    payload,
                    scripted_actions,
                )
            except MemoryReplayReaimingError as error:
                if not memory_replay_visual_reaim_required(payload):
                    raise
                if memory_replay_enhanced_fallback_enabled(payload):
                    await context.emit(
                        "agent.replay.memory_enhanced_fallback.started",
                        "Replay visual re-aiming failed; piloting the stored memory trajectory from the live screen.",
                        {
                            "action_index": error.action_index,
                            "error": str(error),
                            "binding_report": dict(error.binding_report),
                        },
                    )
                    fallback_payload = build_memory_enhanced_replay_fallback_payload(
                        payload,
                        scripted_actions,
                        reaiming_error=error,
                    )
                    fallback_output = await self._run_planned_desktop_task(
                        context,
                        goal,
                        fallback_payload,
                    )
                    fallback_output["replay_fallback"] = {
                        "attempted": True,
                        "mode": "memory_enhanced_replay",
                        "reason": str(error),
                        "failed_action_index": error.action_index,
                        "binding_report": dict(error.binding_report),
                    }
                    await context.emit(
                        "agent.replay.memory_enhanced_fallback.completed",
                        "Memory-enhanced replay fallback finished.",
                        {
                            "success": bool(fallback_output.get("completed")),
                            "failed_action_index": error.action_index,
                            "halt_reason": fallback_output.get("halt_reason"),
                            "performed_action_count": fallback_output.get(
                                "performed_action_count"
                            ),
                        },
                    )
                    return fallback_output
                if not bool(payload.get("memory_replay_fallback_to_planning", False)):
                    raise
                await context.emit(
                    "agent.replay.visual_reaiming.fallback",
                    "Replay visual re-aiming failed; returning control to normal GUI planning.",
                    {
                        "action_index": error.action_index,
                        "error": str(error),
                        "binding_report": dict(error.binding_report),
                    },
                )
                return await self._plan_after_replay(
                    context, goal, payload, reason=str(error),
                    failed_action_index=error.action_index, binding_report=dict(error.binding_report),
                )
            if (
                not replay_output.get("completed")
                and bool(payload.get("memory_replay_fallback_to_planning", False))
                and bool(payload.get("memory_replay_visual_bindings") is not None)
            ):
                # The stored steps ran, but the goal isn't on screen (a replay can click the
                # right place on the wrong page). Carry on live from here instead of failing.
                reason = str(replay_output.get("halt_reason") or "Replay finished without reaching the goal.")
                await context.emit(
                    "agent.replay.goal_not_met.fallback",
                    "The replayed steps finished but the goal isn't on screen; planning the rest live.",
                    {"reason": reason},
                )
                return await self._plan_after_replay(context, goal, payload, reason=reason)
            return replay_output
        return await self._run_planned_desktop_task(context, goal, payload)

    async def _plan_after_replay(
        self,
        context: AgentContext,
        goal: str,
        payload: Mapping[str, Any],
        *,
        reason: str,
        failed_action_index: Optional[int] = None,
        binding_report: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Drop the stored steps and finish the task with live planning and full verification."""
        fallback_payload = dict(payload)
        for key in (
            "actions",
            "memory_replay_visual_bindings",
            "memory_replay_state_contracts",
            "memory_replay_visual_reaim_required",
            "memory_replay_fallback_to_planning",
            "memory_replay_step_grounding_fallback_enabled",
            "memory_replay_step_grounding_fallback_max_attempts",
            "memory_replay_coordinate_copy_first",
            "memory_replay_verify_state_transitions",
            "memory_replay_optional_prerequisite_skip",
            "memory_replay_repair_rewind_enabled",
            "memory_replay_repair_rewind_max_steps",
        ):
            fallback_payload.pop(key, None)
        fallback_payload["capture_after_action"] = True
        fallback_payload["verify_after_action"] = True
        fallback_payload["verify_goal_completion"] = True
        fallback_context = {
            "attempted": True,
            "reason": reason,
            "failed_action_index": failed_action_index,
            "binding_report": dict(binding_report or {}),
        }
        fallback_payload["replay_task_level_fallback"] = dict(fallback_context)
        fallback_output = await self._run_planned_desktop_task(context, goal, fallback_payload)
        fallback_output["replay_fallback"] = dict(fallback_context)
        return fallback_output

    async def _run_scripted_desktop_task(
        self,
        context: AgentContext,
        goal: str,
        payload: Mapping[str, Any],
        actions: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        capture_after_action = bool(payload.get("capture_after_action", True))
        verify_after_action = bool(payload.get("verify_after_action", True))
        verify_goal_completion = bool(payload.get("verify_goal_completion", True))
        step_verification_failure_policy = str(
            payload.get("step_verification_failure_policy", "block_on_blocked")
        )
        visual_reaim_required = memory_replay_visual_reaim_required(payload)
        step_grounding_fallback_max_attempts = memory_replay_step_grounding_fallback_max_attempts(
            payload
        )
        step_grounding_fallback_attempt_count = 0
        step_grounding_fallback_executed_step_count = 0
        action_step_provenance_records: List[Dict[str, Any]] = []
        executed_step_index = 0
        screen_size = (
            await self._desktop_screen_size(context, payload)
            if visual_reaim_required
            else None
        )
        download_baseline = snapshot_desktop_downloads(goal, payload)
        initial_observation = await self._observe_desktop(context, payload, label="before-1")
        observation = initial_observation
        action_results: List[Mapping[str, Any]] = []
        iterations: List[Dict[str, Any]] = []
        goal_verification: Optional[Dict[str, Any]] = None
        final_observation = initial_observation
        performed_action_count = 0
        completed = False
        halt_reason = ""

        async def execute_scripted_action_step(
            *,
            iteration: int,
            action: Mapping[str, Any],
            stored_action: Optional[Mapping[str, Any]],
            visual_reaiming: Optional[Mapping[str, Any]],
            step_grounding_fallback: Optional[Mapping[str, Any]],
            action_provenance: str,
            verify_step: bool,
        ) -> Optional[str]:
            nonlocal observation
            nonlocal final_observation
            nonlocal performed_action_count
            nonlocal executed_step_index

            executed_step_index += 1
            step_index = executed_step_index
            action_payload = dict(action)
            iteration_record: Dict[str, Any] = {
                "iteration": iteration,
                "step_index": step_index,
                "replay_action_index": iteration,
                "observation": observation,
                "action": action_payload,
                "action_provenance": action_provenance,
            }
            if stored_action is not None:
                iteration_record["stored_action"] = dict(stored_action)
            if visual_reaiming is not None:
                iteration_record["visual_reaiming"] = dict(visual_reaiming)
            if step_grounding_fallback is not None:
                iteration_record["step_grounding_fallback"] = dict(step_grounding_fallback)

            action_result = await context.use_skill(
                self.desktop_skill_name,
                {
                    "service_name": str(payload.get("service_name", "desktop")),
                    "actions": [action_payload],
                },
            )
            iteration_record["action_result"] = action_result
            action_results.append(action_result)
            iteration_performed_actions = count_desktop_control_actions(action_result)
            performed_action_count += iteration_performed_actions
            if capture_after_action:
                observation = await self._observe_desktop(
                    context,
                    payload,
                    label=f"after-{step_index}",
                )
                final_observation = observation
                iteration_record["after_observation"] = observation

            provenance_record = {
                "step_index": step_index,
                "iteration": iteration,
                "replay_action_index": iteration,
                "workflow": "scripted",
                "action_provenance": action_provenance,
                "performed_action_count": iteration_performed_actions,
                "is_step_grounding_fallback": action_provenance
                in {
                    "memory_replay_step_grounding_fallback",
                    "memory_replay_step_grounding_repair",
                },
                "is_task_level_planning_fallback": False,
            }
            if step_grounding_fallback is not None:
                provenance_record["step_grounding_fallback"] = dict(step_grounding_fallback)
            action_step_provenance_records.append(provenance_record)
            iteration_record["action_step_provenance"] = dict(provenance_record)

            await context.emit(
                "agent.desktop.actions.completed",
                f"Completed scripted desktop action for iteration {iteration}.",
                {
                    "iteration": iteration,
                    "step_index": step_index,
                    "replay_action_index": iteration,
                    "workflow": "scripted",
                    "action_count": action_result["action_count"],
                    "performed_action_count": iteration_performed_actions,
                    "action": action_payload,
                    "expected_outcome": action_payload.get("expected_outcome"),
                    "action_provenance": action_provenance,
                    "step_grounding_fallback": dict(step_grounding_fallback)
                    if step_grounding_fallback is not None
                    else None,
                    "visual_reaiming": dict(visual_reaiming)
                    if visual_reaiming is not None
                    else None,
                },
            )

            if verify_step and verify_after_action:
                verification_payload = with_desktop_download_evidence(
                    goal,
                    payload,
                    download_baseline,
                )
                verification = await self._request_desktop_step_verification(
                    context,
                    goal,
                    before_observation=iteration_record["observation"],
                    after_observation=observation,
                    action=action_payload,
                    iteration=iteration,
                    history=iterations,
                    payload=verification_payload,
                )
                iteration_record["verification"] = verification
                await context.emit(
                    "agent.desktop.step.verified",
                    f"Verified scripted desktop action for iteration {iteration}.",
                    {
                        "iteration": iteration,
                        "step_index": step_index,
                        "replay_action_index": iteration,
                        "workflow": "scripted",
                        "action": action_payload,
                        "expected_outcome": action_payload.get("expected_outcome"),
                        "step_succeeded": verification.get("step_succeeded"),
                        "outcome_status": verification.get("outcome_status"),
                        "progress_visible": verification.get("progress_visible"),
                        "assessment": verification.get("assessment"),
                        "blocker": verification.get("blocker"),
                        "action_provenance": action_provenance,
                    },
                )
                if desktop_step_verification_blocks_execution(
                    verification,
                    policy=step_verification_failure_policy,
                ):
                    step_halt_reason = str(
                        verification.get("blocker")
                        or verification.get("assessment")
                        or f"Scripted desktop action {iteration} did not visibly achieve its expected outcome."
                    )
                    iteration_record["halt_reason"] = step_halt_reason
                    iteration_record["failed_stage"] = "verification"
                    iterations.append(iteration_record)
                    return step_halt_reason

            iterations.append(iteration_record)
            return None

        forced_visual_reaim_iterations: set[int] = set()
        repair_rewind_attempts: Dict[int, int] = {}

        async def probe_replay_visual_target(
            action_index: int,
            raw_probe_action: Mapping[str, Any],
            *,
            reason: str,
        ) -> Dict[str, Any]:
            probe_action = enrich_scripted_desktop_action(
                raw_probe_action,
                goal=goal,
                iteration=action_index,
                total_actions=len(actions),
            )
            probe_binding = memory_replay_visual_binding_for_action(payload, action_index)
            probe_action_name = str(probe_action.get("action") or "").strip().lower()
            if probe_action_name not in {"click", "move"} or probe_binding is None:
                return {
                    "checked": False,
                    "visible": None,
                    "action_index": action_index,
                    "reason": "No visual target binding is available for this action.",
                    "probe_reason": reason,
                }
            try:
                _bound_action, report = bind_memory_replay_visual_action(
                    probe_action,
                    probe_binding,
                    observation=observation,
                    screen_size=screen_size,
                    working_directory=self.working_directory,
                )
            except MemoryReplayReaimingError as error:
                return {
                    "checked": True,
                    "visible": False,
                    "action_index": action_index,
                    "error": str(error),
                    "binding_report": dict(error.binding_report),
                    "probe_reason": reason,
                }
            return {
                "checked": True,
                "visible": True,
                "action_index": action_index,
                "binding_report": dict(report),
                "probe_reason": reason,
            }

        async def maybe_skip_optional_replay_action(
            *,
            iteration: int,
            stored_action: Mapping[str, Any],
            reaiming_error: MemoryReplayReaimingError,
            allow_anchor_resume: bool,
            step_grounding_fallback: Optional[Mapping[str, Any]] = None,
        ) -> Optional[int]:
            if not memory_replay_optional_prerequisite_skip_enabled(payload):
                return None
            if not memory_replay_action_is_optional(payload, iteration):
                return None

            def append_skip_record(
                *,
                skip_iteration: int,
                resume_iteration: int,
                skip_reason: str,
                skip_mode: str,
                next_target_probe: Optional[Mapping[str, Any]] = None,
            ) -> None:
                skip_contract = (
                    memory_replay_state_contract_for_action(payload, skip_iteration)
                    or {}
                )
                skip_action = enrich_scripted_desktop_action(
                    actions[skip_iteration - 1],
                    goal=goal,
                    iteration=skip_iteration,
                    total_actions=len(actions),
                )
                skip_record = {
                    "iteration": skip_iteration,
                    "step_index": executed_step_index,
                    "replay_action_index": skip_iteration,
                    "resume_replay_action_index": resume_iteration,
                    "observation": observation,
                    "action": dict(skip_action),
                    "stored_action": dict(skip_action),
                    "action_provenance": "memory_replay_optional_prerequisite_skip",
                    "skipped": True,
                    "skip_mode": skip_mode,
                    "skip_reason": skip_reason,
                    "state_contract": dict(skip_contract),
                }
                if skip_iteration == iteration:
                    skip_record["reaiming_error"] = str(reaiming_error)
                    if step_grounding_fallback is not None:
                        skip_record["step_grounding_fallback"] = dict(
                            step_grounding_fallback
                        )
                if next_target_probe is not None:
                    skip_record["next_target_probe"] = dict(next_target_probe)
                iterations.append(skip_record)

            if iteration < len(actions):
                next_probe = await probe_replay_visual_target(
                    iteration + 1,
                    actions[iteration],
                    reason="optional_prerequisite_skip",
                )
                if bool(next_probe.get("visible")):
                    resume_iteration = iteration + 1
                    forced_visual_reaim_iterations.add(resume_iteration)
                    append_skip_record(
                        skip_iteration=iteration,
                        resume_iteration=resume_iteration,
                        skip_reason=(
                            "Optional guarded prerequisite target is absent, while "
                            "the next memorized target is already visible."
                        ),
                        skip_mode="next_visual_target_visible",
                        next_target_probe=next_probe,
                    )
                    await context.emit(
                        "agent.replay.optional_prerequisite.skipped",
                        "Skipped an optional replay prerequisite because the downstream state is already available.",
                        {
                            "iteration": iteration,
                            "next_iteration": resume_iteration,
                            "resume_iteration": resume_iteration,
                            "skipped_action_indexes": [iteration],
                            "skip_mode": "next_visual_target_visible",
                            "error": str(reaiming_error),
                            "state_contract": dict(
                                memory_replay_state_contract_for_action(
                                    payload,
                                    iteration,
                                )
                                or {}
                            ),
                            "next_target_probe": dict(next_probe),
                        },
                    )
                    return resume_iteration

            if not allow_anchor_resume:
                return None
            resume_iteration = memory_replay_optional_chain_resume_index(
                payload,
                actions,
                start_iteration=iteration,
            )
            if resume_iteration is None:
                return None
            skipped_action_indexes = list(range(iteration, resume_iteration))
            skip_reason = (
                "Optional startup prerequisite target is absent; replay is resuming "
                "at the next deterministic memory anchor."
            )
            for skip_iteration in skipped_action_indexes:
                append_skip_record(
                    skip_iteration=skip_iteration,
                    resume_iteration=resume_iteration,
                    skip_reason=skip_reason,
                    skip_mode="deterministic_anchor_resume",
                )
            await context.emit(
                "agent.replay.optional_prerequisite.skipped",
                "Skipped optional startup replay prerequisites and resumed at the next deterministic memory anchor.",
                {
                    "iteration": iteration,
                    "next_iteration": resume_iteration,
                    "resume_iteration": resume_iteration,
                    "resume_action": summarize_desktop_action(
                        actions[resume_iteration - 1]
                    ),
                    "skipped_action_indexes": skipped_action_indexes,
                    "skip_mode": "deterministic_anchor_resume",
                    "error": str(reaiming_error),
                    "state_contract": dict(
                        memory_replay_state_contract_for_action(payload, iteration)
                        or {}
                    ),
                },
            )
            return resume_iteration

        async def maybe_mark_next_transition(
            *,
            iteration: int,
            action: Mapping[str, Any],
        ) -> None:
            if not memory_replay_verify_state_transitions(payload):
                return
            if iteration >= len(actions):
                return
            next_index = iteration + 1
            probe = await probe_replay_visual_target(
                next_index,
                actions[next_index - 1],
                reason="post_action_transition",
            )
            if not bool(probe.get("checked")):
                return
            transition_record = {
                "action_index": iteration,
                "next_action_index": next_index,
                "current_action": dict(action),
                "next_target_visible": bool(probe.get("visible")),
                "probe": dict(probe),
            }
            if iterations:
                iterations[-1]["state_transition_verification"] = dict(transition_record)
            await context.emit(
                "agent.replay.state_transition.checked",
                "Checked whether the next memorized visual target is available after this replay action.",
                transition_record,
            )
            if not bool(probe.get("visible")):
                forced_visual_reaim_iterations.add(next_index)
                await context.emit(
                    "agent.replay.state_transition.missing",
                    "The next replay target is not visible yet; the next step will use visual re-aiming instead of coordinate-copy.",
                    transition_record,
                )

        iteration = 1
        while iteration <= len(actions):
            raw_action = actions[iteration - 1]
            stored_action = enrich_scripted_desktop_action(
                raw_action,
                goal=goal,
                iteration=iteration,
                total_actions=len(actions),
            )
            rewind_to_iteration: Optional[int] = None
            skip_resume_iteration: Optional[int] = None
            while True:
                action = dict(stored_action)
                visual_reaiming: Optional[Dict[str, Any]] = None
                step_grounding_fallback: Optional[Dict[str, Any]] = None
                action_name = str(action.get("action") or "").strip().lower()
                action_provenance = scripted_action_provenance(
                    payload,
                    action_name=action_name,
                    visual_reaiming=None,
                    step_grounding_fallback=None,
                )
                visual_binding = memory_replay_visual_binding_for_action(payload, iteration)
                if action_name in {"click", "move"} and visual_reaim_required:
                    if visual_binding is None:
                        raise MemoryReplayReaimingError(
                            f"Replay pointer action {iteration} is missing visual target evidence.",
                            action_index=iteration,
                        )
                    if (
                        memory_replay_coordinate_copy_first(payload)
                        and iteration not in forced_visual_reaim_iterations
                        and not memory_replay_action_is_optional(payload, iteration)
                    ):
                        coordinate_copy = build_memory_replay_coordinate_copy_report(
                            action,
                            visual_binding,
                            screen_size=screen_size,
                        )
                        if coordinate_copy.get("status") == "coordinate_copy":
                            visual_reaiming = coordinate_copy
                            await context.emit(
                                "agent.replay.coordinate_copy",
                                "Using the stored controller coordinates for this replay action.",
                                {
                                    "iteration": iteration,
                                    "step_index": executed_step_index + 1,
                                    **dict(coordinate_copy),
                                },
                            )
                            action_provenance = scripted_action_provenance(
                                payload,
                                action_name=action_name,
                                visual_reaiming=visual_reaiming,
                                step_grounding_fallback=None,
                            )
                        else:
                            await context.emit(
                                "agent.replay.coordinate_copy.skipped",
                                "Stored coordinates were unavailable for this replay action; trying visual re-aiming.",
                                {
                                    "iteration": iteration,
                                    "step_index": executed_step_index + 1,
                                    **dict(coordinate_copy),
                                },
                            )
                    if visual_reaiming is None:
                        try:
                            action, visual_reaiming = bind_memory_replay_visual_action(
                                action,
                                visual_binding,
                                observation=observation,
                                screen_size=screen_size,
                                working_directory=self.working_directory,
                            )
                        except MemoryReplayReaimingError as error:
                            await context.emit(
                                "agent.replay.visual_reaiming.rejected",
                                "The memorized UI component could not be safely re-aimed.",
                                {
                                    "iteration": iteration,
                                    "step_index": executed_step_index + 1,
                                    "error": str(error),
                                    "binding_report": dict(error.binding_report),
                                },
                            )
                            resume_iteration = await maybe_skip_optional_replay_action(
                                iteration=iteration,
                                stored_action=stored_action,
                                reaiming_error=error,
                                allow_anchor_resume=False,
                            )
                            if resume_iteration is not None:
                                skip_resume_iteration = resume_iteration
                                break
                            decision_result = await self._request_memory_replay_decision_model(
                                context,
                                goal,
                                payload,
                                observation=observation,
                                stored_action=stored_action,
                                visual_binding=visual_binding,
                                actions=actions,
                                iteration=iteration,
                                reaiming_error=error,
                            )
                            if decision_result is None and (not memory_replay_step_grounding_fallback_enabled(
                                payload
                            ) or step_grounding_fallback_attempt_count >= step_grounding_fallback_max_attempts):
                                if (
                                    memory_replay_step_grounding_fallback_enabled(payload)
                                    and step_grounding_fallback_attempt_count
                                    >= step_grounding_fallback_max_attempts
                                ):
                                    await context.emit(
                                        "agent.replay.step_grounding_fallback.limit_reached",
                                        "Replay step-local grounding fallback reached its per-task limit.",
                                        {
                                            "iteration": iteration,
                                            "max_attempts": step_grounding_fallback_max_attempts,
                                            "attempt_count": step_grounding_fallback_attempt_count,
                                            "error": str(error),
                                        },
                                    )
                                raise

                            if decision_result is not None:
                                fallback_attempt = step_grounding_fallback_attempt_count
                                fallback_result = decision_result
                            else:
                                step_grounding_fallback_attempt_count += 1
                                fallback_attempt = step_grounding_fallback_attempt_count
                                fallback_result = await self._request_memory_replay_step_grounding_fallback(
                                    context,
                                    goal,
                                    payload,
                                    observation=observation,
                                    stored_action=stored_action,
                                    visual_binding=visual_binding,
                                    reaiming_error=error,
                                    iteration=iteration,
                                    fallback_attempt=fallback_attempt,
                                )
                            grounding = fallback_result["grounding"]
                            step_grounding_fallback = dict(fallback_result)
                            if not bool(grounding.get("located")):
                                resume_iteration = await maybe_skip_optional_replay_action(
                                    iteration=iteration,
                                    stored_action=stored_action,
                                    reaiming_error=error,
                                    allow_anchor_resume=True,
                                    step_grounding_fallback=step_grounding_fallback,
                                )
                                if resume_iteration is not None:
                                    skip_resume_iteration = resume_iteration
                                    break
                                report = {
                                    "status": "step_grounding_rejected",
                                    "action_index": iteration,
                                    "reason": grounding.get("reason")
                                    or "Step-local grounding fallback could not locate a safe target.",
                                    "grounding": dict(grounding),
                                    "reaiming_error": str(error),
                                    "fallback_attempt": fallback_attempt,
                                }
                                raise MemoryReplayReaimingError(
                                    str(report["reason"]),
                                    action_index=iteration,
                                    binding_report=report,
                                ) from error

                            fallback_action = materialize_desktop_action(
                                fallback_result["next_step"],
                                grounding,
                            )
                            fallback_action_source = build_scripted_desktop_action_source(
                                payload,
                                iteration=iteration,
                                total_actions=len(actions),
                            )
                            fallback_action_source["step_grounding_fallback"] = dict(
                                step_grounding_fallback
                            )
                            fallback_action_source["visual_reaiming_error"] = str(error)
                            fallback_action["source"] = fallback_action_source
                            fallback_role = grounding_role(grounding)
                            if fallback_role == "repair_action":
                                step_grounding_fallback_executed_step_count += 1
                                repair_halt_reason = await execute_scripted_action_step(
                                    iteration=iteration,
                                    action=fallback_action,
                                    stored_action=stored_action,
                                    visual_reaiming=None,
                                    step_grounding_fallback=step_grounding_fallback,
                                    action_provenance="memory_replay_step_grounding_repair",
                                    verify_step=False,
                                )
                                if repair_halt_reason:
                                    halt_reason = repair_halt_reason
                                    break
                                rewind_candidate = memory_replay_repair_rewind_target(
                                    payload,
                                    actions,
                                    failed_iteration=iteration,
                                    repair_rewind_attempts=repair_rewind_attempts,
                                )
                                if rewind_candidate is not None and rewind_candidate < iteration:
                                    repair_rewind_attempts[iteration] = (
                                        repair_rewind_attempts.get(iteration, 0) + 1
                                    )
                                    forced_visual_reaim_iterations.add(rewind_candidate)
                                    rewind_to_iteration = rewind_candidate
                                    await context.emit(
                                        "agent.replay.repair.rewind",
                                        "A replay repair action completed; rewinding to the nearest parent memory action.",
                                        {
                                            "failed_iteration": iteration,
                                            "rewind_iteration": rewind_candidate,
                                            "attempt_count": repair_rewind_attempts[iteration],
                                        },
                                    )
                                    break
                                continue

                            action = fallback_action
                            step_grounding_fallback_executed_step_count += 1
                            action_provenance = "memory_replay_step_grounding_fallback"
                            break

                        await context.emit(
                            "agent.replay.visual_reaimed",
                            "Matched the memorized UI component in the current desktop observation.",
                            {
                                "iteration": iteration,
                                "step_index": executed_step_index + 1,
                                **dict(visual_reaiming),
                            },
                        )
                        action_provenance = scripted_action_provenance(
                            payload,
                            action_name=action_name,
                            visual_reaiming=visual_reaiming,
                            step_grounding_fallback=None,
                        )
                break

            if halt_reason:
                break
            if rewind_to_iteration is not None:
                iteration = rewind_to_iteration
                continue
            if skip_resume_iteration is not None:
                iteration = skip_resume_iteration
                continue

            action_source = build_scripted_desktop_action_source(
                payload,
                iteration=iteration,
                total_actions=len(actions),
            )
            if visual_reaiming is not None:
                action_source["visual_reaiming"] = dict(visual_reaiming)
            if step_grounding_fallback is not None:
                action_source["step_grounding_fallback"] = dict(step_grounding_fallback)
            if action_source:
                action["source"] = action_source
            step_halt_reason = await execute_scripted_action_step(
                iteration=iteration,
                action=action,
                stored_action=stored_action if visual_reaiming is not None else None,
                visual_reaiming=visual_reaiming,
                step_grounding_fallback=step_grounding_fallback,
                action_provenance=action_provenance,
                verify_step=True,
            )
            if step_halt_reason:
                halt_reason = step_halt_reason
                break
            await maybe_mark_next_transition(iteration=iteration, action=action)
            iteration += 1

        if not halt_reason and iteration > len(actions):
            if verify_goal_completion:
                goal_verification_payload = with_desktop_download_evidence(
                    goal,
                    payload,
                    download_baseline,
                )
                if final_observation is not initial_observation:
                    # Replay skips per-step checks, so one end frame cannot show what changed
                    # (an app that quit looks like an app that was never there).
                    goal_verification_payload = {
                        **goal_verification_payload,
                        "goal_verification_before_observation": dict(initial_observation),
                    }
                goal_verification = await self._request_desktop_goal_verification(
                    context,
                    goal,
                    observation=final_observation,
                    iteration=max(1, len(actions)),
                    history=iterations,
                    payload=goal_verification_payload,
                )
                await context.emit(
                    "agent.desktop.goal.verified",
                    "Verified the final desktop goal state for the scripted task.",
                    {
                        "iteration": max(1, len(actions)),
                        "workflow": "scripted",
                        "goal_completed": goal_verification.get("goal_completed"),
                        "assessment": goal_verification.get("assessment"),
                        "blocker": goal_verification.get("blocker"),
                    },
                )
                completed = bool(goal_verification.get("goal_completed"))
                if not completed:
                    halt_reason = str(
                        goal_verification.get("blocker")
                        or goal_verification.get("assessment")
                        or "Scripted desktop actions finished, but the goal was not visibly complete."
                    )
            else:
                completed = True

        if not actions and verify_goal_completion:
            goal_verification_payload = with_desktop_download_evidence(
                goal,
                payload,
                download_baseline,
            )
            goal_verification = await self._request_desktop_goal_verification(
                context,
                goal,
                observation=final_observation,
                iteration=1,
                history=iterations,
                payload=goal_verification_payload,
            )
            await context.emit(
                "agent.desktop.goal.verified",
                "Verified the final desktop goal state for the scripted task.",
                {
                    "iteration": 1,
                    "workflow": "scripted",
                    "goal_completed": goal_verification.get("goal_completed"),
                    "assessment": goal_verification.get("assessment"),
                    "blocker": goal_verification.get("blocker"),
                },
            )
            completed = bool(goal_verification.get("goal_completed"))
            if not completed:
                halt_reason = str(
                    goal_verification.get("blocker")
                    or goal_verification.get("assessment")
                    or "The scripted desktop task did not visibly satisfy the goal."
                )

        action_result = None
        if action_results:
            action_result = {
                "action_count": len(action_results),
                "results": [
                    dict(result_item)
                    for result in action_results
                    if isinstance(result, Mapping)
                    for result_item in list(result.get("results", []))
                ],
            }

        completed_without_action = completed and performed_action_count == 0
        if completed_without_action:
            await context.emit(
                "agent.desktop.completed_without_action",
                "Desktop task completed without performing any control actions.",
                {"workflow": "scripted"},
            )

        return {
            "goal": goal,
            "workflow": "scripted",
            "completed": completed,
            "halt_reason": halt_reason or None,
            "initial_observation": initial_observation,
            "action_result": action_result,
            "final_observation": final_observation,
            "goal_verification": goal_verification,
            "download_evidence": build_desktop_download_evidence(goal, payload, download_baseline),
            "iterations": iterations,
            "performed_action_count": performed_action_count,
            "action_step_provenance": action_step_provenance_records,
            "step_provenance_summary": summarize_action_step_provenance(
                action_step_provenance_records
            ),
            "memory_replay_step_grounding_fallback": {
                "enabled": memory_replay_step_grounding_fallback_enabled(payload),
                "max_attempts": step_grounding_fallback_max_attempts,
                "attempt_count": step_grounding_fallback_attempt_count,
                "executed_step_count": step_grounding_fallback_executed_step_count,
                "rate": safe_rate(
                    step_grounding_fallback_executed_step_count,
                    performed_action_count,
                ),
            },
            "completed_without_action": completed_without_action,
            "failed_iteration": next(
                (
                    int(item.get("iteration", 0))
                    for item in iterations
                    if isinstance(item, Mapping) and item.get("failed_stage")
                ),
                None,
            ),
            "failed_stage": next(
                (
                    str(item.get("failed_stage"))
                    for item in iterations
                    if isinstance(item, Mapping) and item.get("failed_stage")
                ),
                None,
            ),
            "observation_count": 1
            + sum(1 for item in iterations if isinstance(item, Mapping) and "after_observation" in item),
        }

    async def _request_memory_replay_decision_model(
        self,
        context: AgentContext,
        goal: str,
        payload: Mapping[str, Any],
        *,
        observation: Mapping[str, Any],
        stored_action: Mapping[str, Any],
        visual_binding: Mapping[str, Any],
        actions: Sequence[Mapping[str, Any]],
        iteration: int,
        reaiming_error: MemoryReplayReaimingError,
    ) -> Optional[Dict[str, Any]]:
        """OCR + Jev rung between the saved crop and the vision model; None falls through to vision."""
        settings = decision_model_settings(payload)
        if settings is None:
            return None
        await context.emit(
            "agent.replay.decision_model.started",
            "Choosing the replay target by its on-screen text.",
            {"iteration": iteration, "node_id": visual_binding.get("node_id")},
        )
        report = await asyncio.to_thread(
            choose_replay_target,
            goal=goal,
            observation=observation,
            stored_action=stored_action,
            visual_binding=visual_binding,
            actions=actions,
            iteration=iteration,
            settings=settings,
            working_directory=self.working_directory,
        )
        summary = {key: report.get(key) for key in (
            "located", "choice_text", "confidence", "cost", "ocr_ms", "decision_ms", "elapsed_ms", "reason")}
        await context.emit(
            "agent.replay.decision_model.completed",
            str(report.get("reason") or "Decision model finished."),
            {"iteration": iteration, **summary},
        )
        if not report.get("located"):
            return None
        return {
            "status": "grounded",
            "source": "decision_model",
            "iteration": iteration,
            "fallback_attempt": 0,
            "max_attempts": memory_replay_step_grounding_fallback_max_attempts(payload),
            "node_id": visual_binding.get("node_id"),
            "action_index": visual_binding.get("action_index"),
            "role": "memory_target",
            "grounding": dict(report),
            "next_step": build_memory_replay_step_grounding_next_step(
                stored_action, visual_binding, reaiming_error=reaiming_error, fallback_attempt=0),
            "reaiming_error": str(reaiming_error),
            "binding_report": dict(reaiming_error.binding_report),
        }

    async def _request_memory_replay_step_grounding_fallback(
        self,
        context: AgentContext,
        goal: str,
        payload: Mapping[str, Any],
        *,
        observation: Mapping[str, Any],
        stored_action: Mapping[str, Any],
        visual_binding: Mapping[str, Any],
        reaiming_error: MemoryReplayReaimingError,
        iteration: int,
        fallback_attempt: int,
    ) -> Dict[str, Any]:
        next_step = build_memory_replay_step_grounding_next_step(
            stored_action,
            visual_binding,
            reaiming_error=reaiming_error,
            fallback_attempt=fallback_attempt,
        )
        fallback_payload = dict(payload)
        fallback_payload.pop("visual_grounding_only", None)
        fallback_payload.pop("cv_grounding_only", None)
        fallback_payload["visual_grounding_targets"] = (
            build_memory_replay_visual_grounding_targets(visual_binding)
        )
        fallback_payload["visual_grounding_model_fallback"] = True
        fallback_payload["grounding_allow_repair_action"] = True
        fallback_payload["grounding_prompt"] = build_memory_replay_step_grounding_prompt(
            fallback_payload.get("grounding_prompt"),
            visual_binding,
            reaiming_error=reaiming_error,
            fallback_attempt=fallback_attempt,
        )
        await context.emit(
            "agent.replay.step_grounding_fallback.started",
            "Trying bounded step-local grounding before task-level planning fallback.",
            {
                "iteration": iteration,
                "fallback_attempt": fallback_attempt,
                "max_attempts": memory_replay_step_grounding_fallback_max_attempts(payload),
                "node_id": visual_binding.get("node_id"),
                "reaiming_error": str(reaiming_error),
            },
        )
        grounding = await self._request_grounding(
            context,
            goal,
            observation,
            next_step,
            iteration=iteration,
            payload=fallback_payload,
        )
        result = {
            "status": "grounded" if grounding.get("located") else "rejected",
            "iteration": iteration,
            "fallback_attempt": fallback_attempt,
            "max_attempts": memory_replay_step_grounding_fallback_max_attempts(payload),
            "node_id": visual_binding.get("node_id"),
            "action_index": visual_binding.get("action_index"),
            "role": grounding_role(grounding),
            "grounding": dict(grounding),
            "next_step": next_step,
            "reaiming_error": str(reaiming_error),
            "binding_report": dict(reaiming_error.binding_report),
        }
        await context.emit(
            "agent.replay.step_grounding_fallback.completed",
            "Step-local grounding fallback completed.",
            result,
        )
        return result

    async def _run_planned_desktop_task(
        self,
        context: AgentContext,
        goal: str,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        max_iterations = max(1, int(payload.get("max_iterations", self.default_desktop_max_iterations)))
        capture_after_action = bool(payload.get("capture_after_action", True))
        verify_after_action = bool(payload.get("verify_after_action", True))
        verify_goal_completion = bool(payload.get("verify_goal_completion", True))
        download_baseline = snapshot_desktop_downloads(goal, payload)
        iterations: List[Dict[str, Any]] = []
        observation = await self._observe_desktop(context, payload, label="before-1")
        completed = False
        halt_reason = ""
        performed_action_count = 0
        action_step_provenance_records: List[Dict[str, Any]] = []
        task_level_fallback_context = payload.get("replay_task_level_fallback")
        task_level_fallback_used = isinstance(task_level_fallback_context, Mapping)
        memory_enhanced_fallback_context = payload.get(
            "memory_replay_enhanced_fallback_context"
        )
        memory_enhanced_fallback_used = isinstance(
            memory_enhanced_fallback_context,
            Mapping,
        )
        action_provenance = (
            "memory_enhanced_replay_fallback"
            if memory_enhanced_fallback_used
            else "task_level_planning_fallback"
            if task_level_fallback_used
            else "planned"
        )

        for iteration in range(1, max_iterations + 1):
            iteration_record: Dict[str, Any] = {
                "iteration": iteration,
                "step_index": iteration,
                "observation": observation,
                "action_provenance": action_provenance,
            }
            try:
                plan = await self._request_desktop_plan(
                    context,
                    goal,
                    observation,
                    iteration=iteration,
                    history=iterations,
                    payload=payload,
                )
            except Exception as error:
                iteration_record["halt_reason"] = str(error)
                iteration_record["failed_stage"] = "planning"
                iterations.append(iteration_record)
                raise self._build_desktop_execution_error(
                    goal=goal,
                    observation=observation,
                    iterations=iterations,
                    performed_action_count=performed_action_count,
                    halt_reason=str(error),
                    failed_iteration=iteration,
                    failed_stage="planning",
                ) from error

            iteration_record["plan"] = plan
            next_step = plan.get("next_step")
            if plan.get("completed"):
                if verify_goal_completion:
                    goal_verification_payload = with_desktop_download_evidence(
                        goal,
                        payload,
                        download_baseline,
                    )
                    goal_verification = await self._request_desktop_goal_verification(
                        context,
                        goal,
                        observation=observation,
                        iteration=iteration,
                        history=iterations,
                        payload=goal_verification_payload,
                    )
                    iteration_record["goal_verification"] = goal_verification
                    await context.emit(
                        "agent.desktop.goal.verified",
                        f"Verified the final desktop goal state for iteration {iteration}.",
                        {
                            "iteration": iteration,
                            "goal_completed": goal_verification.get("goal_completed"),
                            "assessment": goal_verification.get("assessment"),
                            "blocker": goal_verification.get("blocker"),
                        },
                    )
                    if bool(goal_verification.get("goal_completed")):
                        completed = True
                    else:
                        halt_reason = str(
                            goal_verification.get("blocker")
                            or goal_verification.get("assessment")
                            or "The planner reported completion, but the goal was not visibly complete."
                        )
                        iteration_record["halt_reason"] = halt_reason
                else:
                    completed = True
                iterations.append(iteration_record)
                break
            if next_step is None:
                halt_reason = str(
                    plan.get("assessment")
                    or "The desktop planner did not provide a next step."
                )
                iteration_record["halt_reason"] = halt_reason
                iterations.append(iteration_record)
                break

            grounded_step = next_step
            if requires_grounding(next_step):
                try:
                    grounding = await self._request_grounding(
                        context,
                        goal,
                        observation,
                        next_step,
                        iteration=iteration,
                        payload=payload,
                    )
                except Exception as error:
                    iteration_record["halt_reason"] = str(error)
                    iteration_record["failed_stage"] = "grounding"
                    iterations.append(iteration_record)
                    raise self._build_desktop_execution_error(
                        goal=goal,
                        observation=observation,
                        iterations=iterations,
                        performed_action_count=performed_action_count,
                        halt_reason=str(error),
                        failed_iteration=iteration,
                        failed_stage="grounding",
                    ) from error

                iteration_record["grounding"] = grounding
                if not grounding.get("located"):
                    halt_reason = str(
                        grounding.get("reason") or "Image grounding could not locate the target."
                    )
                    iteration_record["halt_reason"] = halt_reason
                    iterations.append(iteration_record)
                    break
                grounded_step = materialize_desktop_action(next_step, grounding)

            intent = {
                key: str(next_step.get(key)).strip()
                for key in ("target_description", "expected_outcome")
                if str(next_step.get(key) or "").strip()
            }
            grounded_step = dict(grounded_step)
            grounded_step["source"] = {
                **dict(grounded_step.get("source") or {}), **intent,
                "agent": self.name, "workflow": "observe-plan-ground-act",
                "next_step": dict(next_step), "assessment": str(plan.get("assessment") or ""),
            }
            iteration_record["action"] = grounded_step
            try:
                action_result = await context.use_skill(
                    self.desktop_skill_name,
                    {
                        "service_name": str(payload.get("service_name", "desktop")),
                        "actions": [grounded_step],
                    },
                )
            except Exception as error:
                iteration_record["halt_reason"] = str(error)
                iteration_record["failed_stage"] = "action"
                iterations.append(iteration_record)
                raise self._build_desktop_execution_error(
                    goal=goal,
                    observation=observation,
                    iterations=iterations,
                    performed_action_count=performed_action_count,
                    halt_reason=str(error),
                    failed_iteration=iteration,
                    failed_stage="action",
                ) from error

            iteration_record["action_result"] = action_result
            iteration_performed_actions = count_desktop_control_actions(action_result)
            performed_action_count += iteration_performed_actions
            provenance_record = {
                "step_index": iteration,
                "iteration": iteration,
                "workflow": "observe-plan-ground-act",
                "action_provenance": action_provenance,
                "performed_action_count": iteration_performed_actions,
                "is_step_grounding_fallback": False,
                "is_task_level_planning_fallback": task_level_fallback_used,
                "is_memory_enhanced_replay_fallback": memory_enhanced_fallback_used,
            }
            action_step_provenance_records.append(provenance_record)
            iteration_record["action_step_provenance"] = dict(provenance_record)
            if capture_after_action:
                try:
                    observation = await self._observe_desktop(
                        context,
                        payload,
                        label=f"after-{iteration}",
                    )
                except Exception as error:
                    iteration_record["halt_reason"] = str(error)
                    iteration_record["failed_stage"] = "observation"
                    iterations.append(iteration_record)
                    raise self._build_desktop_execution_error(
                        goal=goal,
                        observation=observation,
                        iterations=iterations,
                        performed_action_count=performed_action_count,
                        halt_reason=str(error),
                        failed_iteration=iteration,
                        failed_stage="observation",
                    ) from error
                iteration_record["after_observation"] = observation

            await context.emit(
                "agent.desktop.actions.completed",
                f"Completed planned desktop action for iteration {iteration}.",
                {
                    "iteration": iteration,
                    "workflow": "observe-plan-ground-act",
                    "action_count": action_result["action_count"],
                    "performed_action_count": iteration_performed_actions,
                    "action": dict(grounded_step),
                    "expected_outcome": grounded_step.get("expected_outcome"),
                    "action_provenance": action_provenance,
                    "step_index": iteration,
                    "task_level_planning_fallback": dict(task_level_fallback_context)
                    if task_level_fallback_used
                    else None,
                    "memory_replay_enhanced_fallback": (
                        summarize_memory_enhanced_fallback_context(
                            memory_enhanced_fallback_context
                        )
                        if memory_enhanced_fallback_used
                        else None
                    ),
                },
            )
            if verify_after_action:
                verification_payload = with_desktop_download_evidence(
                    goal,
                    payload,
                    download_baseline,
                )
                verification = await self._request_desktop_step_verification(
                    context,
                    goal,
                    before_observation=iteration_record["observation"],
                    after_observation=observation,
                    action=grounded_step,
                    iteration=iteration,
                    history=iterations,
                    payload=verification_payload,
                )
                iteration_record["verification"] = verification
                await context.emit(
                    "agent.desktop.step.verified",
                    f"Verified planned desktop action for iteration {iteration}.",
                    {
                        "iteration": iteration,
                        "workflow": "observe-plan-ground-act",
                        "action": dict(grounded_step),
                        "expected_outcome": grounded_step.get("expected_outcome"),
                        "step_succeeded": verification.get("step_succeeded"),
                        "outcome_status": verification.get("outcome_status"),
                        "progress_visible": verification.get("progress_visible"),
                        "assessment": verification.get("assessment"),
                        "blocker": verification.get("blocker"),
                        "action_provenance": action_provenance,
                        "step_index": iteration,
                    },
                )

            iterations.append(iteration_record)
        else:
            halt_reason = "Reached the configured maximum number of desktop iterations."
            await context.emit(
                "agent.desktop.iterations.exhausted",
                halt_reason,
                {"max_iterations": max_iterations},
            )

        completed_without_action = completed and performed_action_count == 0
        if completed_without_action:
            await context.emit(
                "agent.desktop.completed_without_action",
                "Desktop task completed without performing any control actions.",
                {
                    "workflow": "observe-plan-ground-act",
                    "iteration_count": len(iterations),
                },
            )

        return {
            "goal": goal,
            "workflow": "observe-plan-ground-act",
            "completed": completed,
            "halt_reason": halt_reason or None,
            "iteration_count": len(iterations),
            "final_observation": observation,
            "download_evidence": build_desktop_download_evidence(goal, payload, download_baseline),
            "iterations": iterations,
            "performed_action_count": performed_action_count,
            "action_step_provenance": action_step_provenance_records,
            "step_provenance_summary": summarize_action_step_provenance(
                action_step_provenance_records
            ),
            "memory_replay_enhanced_fallback": (
                summarize_memory_enhanced_fallback_context(
                    memory_enhanced_fallback_context
                )
                if memory_enhanced_fallback_used
                else None
            ),
            "completed_without_action": completed_without_action,
            "observation_count": 1
            + sum(1 for item in iterations if isinstance(item, Mapping) and "after_observation" in item),
        }

    async def _desktop_screen_size(
        self,
        context: AgentContext,
        payload: Mapping[str, Any],
    ) -> Optional[Dict[str, Any]]:
        service_name = str(payload.get("service_name", "desktop"))
        controller = context.registry.get_service(service_name)
        screen_size_method = getattr(controller, "screen_size", None) if controller is not None else None
        if not callable(screen_size_method):
            return None
        result = screen_size_method()
        if inspect.isawaitable(result):
            result = await result
        return dict(result) if isinstance(result, Mapping) else None

    async def _observe_desktop(
        self,
        context: AgentContext,
        payload: Mapping[str, Any],
        *,
        label: str,
    ) -> Dict[str, Any]:
        observation_path = self._build_desktop_observation_path(payload, label)
        service_name = str(payload.get("service_name", "desktop"))
        controller = context.registry.get_service(service_name)
        observe_method = getattr(controller, "observe", None) if controller is not None else None

        # Capture directly from the desktop controller when possible so transient UI feedback
        # is not missed while the skill layer emits bookkeeping events.
        if callable(observe_method):
            observation = await observe_method(
                path=str(observation_path),
                region=payload.get("observation_region"),
            )
        else:
            result_key = f"{label.replace('-', '_')}_observation"
            observation_result = await context.use_skill(
                self.desktop_skill_name,
                {
                    "service_name": service_name,
                    "actions": [
                        {
                            "action": "observe",
                            "path": str(observation_path),
                            "region": payload.get("observation_region"),
                            "result_key": result_key,
                        }
                    ],
                },
            )
            observation = observation_result["values"][result_key]

        if not isinstance(observation, Mapping):
            raise TypeError("Desktop observation must return a mapping.")
        observation = dict(observation)
        context.state.setdefault("desktop_observations", []).append(observation)
        label_details = parse_observation_label(label)
        await context.emit(
            "agent.desktop.observed",
            f"Captured desktop observation '{label}'.",
            {
                "label": label,
                "iteration": label_details.get("iteration"),
                "observation_phase": label_details.get("phase"),
                "path": observation["path"],
                "capture_verified": bool(observation.get("capture_verified")),
                "file_size_bytes": observation.get("file_size_bytes"),
            },
        )
        return observation

    async def _request_desktop_plan(
        self,
        context: AgentContext,
        goal: str,
        observation: Mapping[str, Any],
        *,
        iteration: int,
        history: List[Mapping[str, Any]],
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        planning_task = AgentTask(
            goal=f"Plan the next desktop step for iteration {iteration}.",
            payload={
                "planning_mode": "desktop_next_step",
                "goal": goal,
                "observation": dict(observation),
                "iteration": iteration,
                "history": history,
                "task_payload": dict(payload),
            },
            metadata={"iteration": iteration, "task_type": "desktop_task"},
            task_id=context.task.task_id,
        )
        result = await self.planning_agent.run(
            planning_task,
            context=context.fork(task=planning_task),
        )
        if not result.success or not isinstance(result.output, Mapping):
            raise RuntimeError(result.error or "Desktop planning agent failed.")
        return dict(result.output)

    async def _request_desktop_step_verification(
        self,
        context: AgentContext,
        goal: str,
        *,
        before_observation: Mapping[str, Any],
        after_observation: Mapping[str, Any],
        action: Mapping[str, Any],
        iteration: int,
        history: List[Mapping[str, Any]],
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        verification_task = AgentTask(
            goal=f"Verify the desktop step outcome for iteration {iteration}.",
            payload={
                "planning_mode": "desktop_step_verification",
                "goal": goal,
                "before_observation": dict(before_observation),
                "after_observation": dict(after_observation),
                "action": dict(action),
                "iteration": iteration,
                "history": history,
                "task_payload": dict(payload),
            },
            metadata={"iteration": iteration, "task_type": "desktop_task"},
            task_id=context.task.task_id,
        )
        result = await self.planning_agent.run(
            verification_task,
            context=context.fork(task=verification_task),
        )
        if not result.success or not isinstance(result.output, Mapping):
            raise RuntimeError(result.error or "Desktop step verification failed.")
        return dict(result.output)

    async def _request_desktop_goal_verification(
        self,
        context: AgentContext,
        goal: str,
        *,
        observation: Mapping[str, Any],
        iteration: int,
        history: List[Mapping[str, Any]],
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        verification_task = AgentTask(
            goal=f"Verify whether the desktop goal is complete for iteration {iteration}.",
            payload={
                "planning_mode": "desktop_goal_verification",
                "goal": goal,
                "observation": dict(observation),
                "iteration": iteration,
                "history": history,
                "task_payload": dict(payload),
            },
            metadata={"iteration": iteration, "task_type": "desktop_task"},
            task_id=context.task.task_id,
        )
        result = await self.planning_agent.run(
            verification_task,
            context=context.fork(task=verification_task),
        )
        if not result.success or not isinstance(result.output, Mapping):
            raise RuntimeError(result.error or "Desktop goal verification failed.")
        return dict(result.output)

    async def _request_grounding(
        self,
        context: AgentContext,
        goal: str,
        observation: Mapping[str, Any],
        next_step: Mapping[str, Any],
        *,
        iteration: int,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        grounding_task = AgentTask(
            goal=f"Ground the desktop target for iteration {iteration}.",
            payload={
                "goal": goal,
                "observation": dict(observation),
                "next_step": dict(next_step),
                "iteration": iteration,
                "task_payload": dict(payload),
            },
            metadata={"iteration": iteration, "task_type": "desktop_task"},
            task_id=context.task.task_id,
        )
        result = await self.image_grounding_agent.run(
            grounding_task,
            context=context.fork(task=grounding_task),
        )
        if not result.success or not isinstance(result.output, Mapping):
            raise RuntimeError(result.error or "Image grounding agent failed.")
        return dict(result.output)

    def _normalize_desktop_payload(self, task_record: Mapping[str, Any]) -> Dict[str, Any]:
        payload = dict(task_record.get("payload", {}) or {})
        goal = (
            str(task_record.get("goal", "")).strip()
            or str(task_record.get("summary", "")).strip()
            or "Advance the requested desktop GUI task safely."
        )

        if "planner_model" not in payload and "analysis_model" in payload:
            payload["planner_model"] = payload["analysis_model"]
        if "planner_router_name" not in payload and "analysis_router_name" in payload:
            payload["planner_router_name"] = payload["analysis_router_name"]
        if "planner_prompt" not in payload and "analysis_prompt" in payload:
            payload["planner_prompt"] = payload["analysis_prompt"]
        if "planner_action_limit" not in payload and "analysis_action_limit" in payload:
            payload["planner_action_limit"] = payload["analysis_action_limit"]
        if "planner_reasoning_effort" not in payload and "analysis_reasoning_effort" in payload:
            payload["planner_reasoning_effort"] = payload["analysis_reasoning_effort"]

        payload.setdefault("planner_model", "default")
        payload.setdefault("planner_router_name", self.router_name)
        payload.setdefault("planner_prompt", goal)
        payload.setdefault("planner_action_limit", 1)
        payload.setdefault("planner_reasoning_effort", self.default_desktop_reasoning_effort)
        payload.setdefault("grounding_model", payload["planner_model"])
        payload.setdefault("grounding_router_name", payload["planner_router_name"])
        payload.setdefault("grounding_reasoning_effort", self.default_desktop_reasoning_effort)
        payload.setdefault("verification_model", payload["planner_model"])
        payload.setdefault("verification_router_name", payload["planner_router_name"])
        payload.setdefault("verification_reasoning_effort", self.default_desktop_reasoning_effort)
        payload.setdefault("max_iterations", self.default_desktop_max_iterations)
        payload.setdefault("capture_after_action", True)
        payload.setdefault("verify_after_action", True)
        payload.setdefault("verify_goal_completion", True)
        payload.setdefault("step_verification_failure_policy", "block_on_blocked")
        payload.setdefault("observation_directory", "local_data/desktop_observations")
        payload.setdefault("service_name", "desktop")
        if bool(payload.get("verify_after_action")) and not bool(payload.get("capture_after_action", True)):
            raise ValueError("Desktop step verification requires capture_after_action=true.")
        if bool(payload.get("verify_goal_completion")) and not bool(
            payload.get("capture_after_action", True)
        ):
            raise ValueError("Desktop goal verification requires capture_after_action=true.")
        return payload

    def _build_desktop_observation_path(self, payload: Mapping[str, Any], label: str) -> Path:
        directory_name = str(payload.get("observation_directory", "local_data/desktop_observations"))
        observation_directory = self.working_directory / directory_name
        safe_label = re.sub(r"[^a-zA-Z0-9._-]+", "-", label).strip("-") or "observation"
        task_id = str(payload.get("observation_task_id") or "desktop-task")
        safe_task_id = re.sub(r"[^a-zA-Z0-9._-]+", "-", task_id).strip("-") or "desktop-task"
        task_directory = observation_directory / safe_task_id
        task_directory.mkdir(parents=True, exist_ok=True)
        return task_directory / f"{safe_label}.png"

    def _build_desktop_execution_error(
        self,
        *,
        goal: str,
        observation: Mapping[str, Any],
        iterations: Sequence[Mapping[str, Any]],
        performed_action_count: int,
        halt_reason: str,
        failed_iteration: int,
        failed_stage: str,
    ) -> DesktopExecutionError:
        execution_output = {
            "goal": goal,
            "workflow": "observe-plan-ground-act",
            "completed": False,
            "halt_reason": halt_reason,
            "iteration_count": len(iterations),
            "failed_iteration": failed_iteration,
            "failed_stage": failed_stage,
            "final_observation": dict(observation),
            "iterations": [dict(item) for item in iterations],
            "performed_action_count": max(0, int(performed_action_count)),
            "completed_without_action": False,
            "observation_count": 1
            + sum(
                1
                for item in iterations
                if isinstance(item, Mapping) and "after_observation" in item
            ),
        }
        return DesktopExecutionError(halt_reason, execution_output=execution_output)


def desktop_step_verification_blocks_execution(
    verification: Mapping[str, Any],
    *,
    policy: str,
) -> bool:
    normalized_policy = str(policy or "block_on_blocked").strip().lower().replace("-", "_")
    if normalized_policy in {"advisory", "continue", "never", "record_only"}:
        return False
    if normalized_policy in {"strict", "halt", "halt_on_failure", "block_on_failure"}:
        return not bool(verification.get("step_succeeded"))

    outcome_status = str(verification.get("outcome_status", "")).strip().lower()
    if outcome_status in {"succeeded", "progressed", "pending"}:
        return False
    if outcome_status == "blocked":
        return True
    return not bool(verification.get("step_succeeded"))


def safe_rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return float(numerator) / float(denominator)


def memory_replay_step_grounding_fallback_enabled(payload: Mapping[str, Any]) -> bool:
    if payload.get("memory_replay_step_grounding_fallback_enabled") is not None:
        return bool(payload.get("memory_replay_step_grounding_fallback_enabled"))
    return memory_replay_visual_reaim_required(payload)


def memory_replay_verify_state_transitions(payload: Mapping[str, Any]) -> bool:
    if payload.get("memory_replay_verify_state_transitions") is not None:
        return bool(payload.get("memory_replay_verify_state_transitions"))
    return memory_replay_visual_reaim_required(payload)


def memory_replay_optional_prerequisite_skip_enabled(payload: Mapping[str, Any]) -> bool:
    if payload.get("memory_replay_optional_prerequisite_skip") is not None:
        return bool(payload.get("memory_replay_optional_prerequisite_skip"))
    return memory_replay_visual_reaim_required(payload)


def memory_replay_repair_rewind_enabled(payload: Mapping[str, Any]) -> bool:
    if payload.get("memory_replay_repair_rewind_enabled") is not None:
        return bool(payload.get("memory_replay_repair_rewind_enabled"))
    return memory_replay_visual_reaim_required(payload)


def memory_replay_repair_rewind_max_steps(payload: Mapping[str, Any]) -> int:
    raw_value = payload.get("memory_replay_repair_rewind_max_steps", 2)
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return 2
    return max(0, min(value, 5))


def memory_replay_repair_rewind_target(
    payload: Mapping[str, Any],
    actions: Sequence[Mapping[str, Any]],
    *,
    failed_iteration: int,
    repair_rewind_attempts: Mapping[int, int],
) -> Optional[int]:
    if not memory_replay_repair_rewind_enabled(payload):
        return None
    if int(repair_rewind_attempts.get(failed_iteration, 0)) > 0:
        return None
    max_steps = memory_replay_repair_rewind_max_steps(payload)
    if max_steps <= 0 or failed_iteration <= 1:
        return None
    lower_bound = max(1, failed_iteration - max_steps)
    for candidate in range(failed_iteration - 1, lower_bound - 1, -1):
        if candidate < 1 or candidate > len(actions):
            continue
        candidate_action = actions[candidate - 1]
        action_name = str(candidate_action.get("action") or "").strip().lower()
        if action_name in {"click", "move", "hotkey", "press"}:
            return candidate
    return None


def memory_replay_enhanced_fallback_enabled(payload: Mapping[str, Any]) -> bool:
    for key in (
        "memory_replay_enhanced_fallback_enabled",
        "memory_replay_enhanced_fallback",
    ):
        if payload.get(key) is not None:
            return bool(payload.get(key))
    return False


def build_memory_enhanced_replay_fallback_payload(
    payload: Mapping[str, Any],
    actions: Sequence[Mapping[str, Any]],
    *,
    reaiming_error: MemoryReplayReaimingError,
) -> Dict[str, Any]:
    fallback_payload = dict(payload)
    fallback_payload.pop("actions", None)
    fallback_payload["capture_after_action"] = True
    fallback_payload["verify_after_action"] = True
    fallback_payload["verify_goal_completion"] = True
    fallback_payload["memory_replay_enhanced_fallback_context"] = (
        build_memory_enhanced_replay_context(
            payload,
            actions,
            reaiming_error=reaiming_error,
        )
    )
    fallback_payload["visual_grounding_targets"] = (
        build_memory_enhanced_replay_visual_grounding_targets(payload)
    )
    fallback_payload["visual_grounding_model_fallback"] = True
    fallback_payload["visual_grounding_enabled"] = False
    fallback_payload["cv_grounding_enabled"] = False
    fallback_payload.pop("memory_replay_enhanced_fallback_max_iterations", None)
    return fallback_payload


def build_memory_enhanced_replay_context(
    payload: Mapping[str, Any],
    actions: Sequence[Mapping[str, Any]],
    *,
    reaiming_error: MemoryReplayReaimingError,
) -> Dict[str, Any]:
    bindings_by_index = {
        int(binding.get("action_index") or 0): dict(binding)
        for binding in payload.get("memory_replay_visual_bindings", [])
        if isinstance(binding, Mapping)
    }
    contracts_by_index = {
        int(contract.get("action_index") or 0): dict(contract)
        for contract in payload.get("memory_replay_state_contracts", [])
        if isinstance(contract, Mapping)
    }
    failed_action_index = optional_int(reaiming_error.action_index, 1) or 1
    trajectory: List[Dict[str, Any]] = []
    reference_images: List[Dict[str, Any]] = []
    reference_limit = max(
        0,
        optional_int(
            payload.get("memory_replay_enhanced_fallback_reference_image_limit"),
            6,
        )
        or 0,
    )
    seen_reference_paths: set[str] = set()

    for action_index, action in enumerate(actions, start=1):
        binding = bindings_by_index.get(action_index, {})
        contract = contracts_by_index.get(action_index, {})
        node_id = str(binding.get("node_id") or contract.get("node_id") or "")
        action_summary = summarize_desktop_action(
            enrich_scripted_desktop_action(
                action,
                goal=str(payload.get("planner_prompt") or ""),
                iteration=action_index,
                total_actions=len(actions),
            )
        )
        entry: Dict[str, Any] = {
            "action_index": action_index,
            "current_failure": action_index == failed_action_index,
            "action": action_summary,
            "role": str(contract.get("role") or "required_step"),
            "optional": bool(contract.get("optional")),
            "required": not bool(contract.get("optional")),
            "node_id": node_id or None,
            "visual_target_id": node_id or None,
            "visual_binding_available": bool(binding),
        }
        for key in (
            "precondition",
            "postcondition",
            "inferred_preconditions",
            "inferred_postconditions",
        ):
            if contract.get(key):
                entry[key] = contract.get(key)
        trajectory.append(entry)

        if reference_limit and binding and len(reference_images) < reference_limit:
            for path in memory_binding_reference_paths(binding, limit=2):
                if path in seen_reference_paths:
                    continue
                seen_reference_paths.add(path)
                reference_images.append(
                    {
                        "action_index": action_index,
                        "node_id": node_id or None,
                        "path": path,
                    }
                )
                if len(reference_images) >= reference_limit:
                    break
        if reference_limit and len(reference_images) >= reference_limit:
            continue

    replay_metadata = payload.get("replay")
    return {
        "mode": "memory_enhanced_replay_fallback",
        "failed_action_index": failed_action_index,
        "failure_reason": str(reaiming_error),
        "binding_report": dict(reaiming_error.binding_report),
        "source_task_id": replay_metadata.get("source_task_id")
        if isinstance(replay_metadata, Mapping)
        else None,
        "memory_path_id": (
            replay_metadata.get("adaptation", {}).get("memory_path_id")
            if isinstance(replay_metadata.get("adaptation"), Mapping)
            else None
        )
        if isinstance(replay_metadata, Mapping)
        else None,
        "trajectory_action_count": len(actions),
        "trajectory": trajectory,
        "reference_images": reference_images,
        "policy": {
            "primary_directive": "Follow the stored memory trajectory from the current live screen.",
            "allowed_repairs": [
                "dismiss a transient blocker",
                "re-open a parent menu or dialog from an earlier memory action",
                "skip a stale optional prerequisite when the downstream memory target is visible",
                "adapt the next memory action target to the live screen",
            ],
            "not_allowed": [
                "start an unconstrained full task plan",
                "invent unrelated actions when memory steps are still usable",
            ],
        },
    }


def summarize_memory_enhanced_fallback_context(context: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(context, Mapping):
        return None
    return {
        "mode": context.get("mode"),
        "failed_action_index": context.get("failed_action_index"),
        "failure_reason": context.get("failure_reason"),
        "source_task_id": context.get("source_task_id"),
        "memory_path_id": context.get("memory_path_id"),
        "trajectory_action_count": context.get("trajectory_action_count"),
    }


def build_memory_enhanced_replay_visual_grounding_targets(
    payload: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    targets: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for binding in payload.get("memory_replay_visual_bindings", []):
        if not isinstance(binding, Mapping):
            continue
        action_index = int(binding.get("action_index") or 0)
        node_id = str(binding.get("node_id") or f"memory-action-{action_index}")
        for target in build_memory_replay_visual_grounding_targets(binding):
            target = dict(target)
            target["memory_target_id"] = node_id
            target["target_id"] = node_id
            target["label"] = node_id
            target["target_description"] = (
                f"memory replay action {action_index} target {node_id}"
            )
            signature = repr(sorted((str(key), str(value)) for key, value in target.items()))
            if signature in seen:
                continue
            seen.add(signature)
            targets.append(target)
    return targets


def memory_binding_reference_paths(
    binding: Mapping[str, Any],
    *,
    limit: int = 3,
) -> List[str]:
    paths: List[str] = []
    for raw_path in [binding.get("reference_image_path")]:
        path = str(raw_path or "").strip()
        if path and path not in paths:
            paths.append(path)
    candidates = binding.get("reference_candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            path = str(candidate.get("reference_image_path") or "").strip()
            if path and path not in paths:
                paths.append(path)
            if len(paths) >= limit:
                break
    return paths[: max(0, limit)]


def memory_replay_coordinate_copy_first(payload: Mapping[str, Any]) -> bool:
    return bool(payload.get("memory_replay_coordinate_copy_first"))


def build_memory_replay_coordinate_copy_report(
    action: Mapping[str, Any],
    visual_binding: Mapping[str, Any],
    *,
    screen_size: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    action_name = str(action.get("action") or "").strip().lower()
    action_index = optional_int(visual_binding.get("action_index"), 0)
    x = optional_int(action.get("x"), None)
    y = optional_int(action.get("y"), None)
    report: Dict[str, Any] = {
        "status": "coordinate_copy",
        "action_index": action_index,
        "action": action_name,
        "node_id": visual_binding.get("node_id"),
        "original_coordinates": {
            "x": x,
            "y": y,
        },
        "bound_controller_coordinates": {
            "x": x,
            "y": y,
        },
        "screen_size": dict(screen_size) if isinstance(screen_size, Mapping) else {},
        "source": "stored_controller_coordinates",
    }
    if x is None or y is None:
        report["status"] = "not_available"
        report["reason"] = "Stored replay action does not contain controller coordinates."
        return report
    if isinstance(screen_size, Mapping):
        width = optional_int(screen_size.get("width"), None)
        height = optional_int(screen_size.get("height"), None)
        if width is not None and not 0 <= x < width:
            report["status"] = "not_available"
            report["reason"] = "Stored x coordinate is outside the current screen."
            return report
        if height is not None and not 0 <= y < height:
            report["status"] = "not_available"
            report["reason"] = "Stored y coordinate is outside the current screen."
            return report
    return report


def memory_replay_step_grounding_fallback_max_attempts(payload: Mapping[str, Any]) -> int:
    raw_value = payload.get("memory_replay_step_grounding_fallback_max_attempts", 20)
    try:
        return max(0, int(raw_value))
    except (TypeError, ValueError):
        return 20


def scripted_action_provenance(
    payload: Mapping[str, Any],
    *,
    action_name: str,
    visual_reaiming: Optional[Mapping[str, Any]],
    step_grounding_fallback: Optional[Mapping[str, Any]],
) -> str:
    if step_grounding_fallback is not None:
        role = grounding_role(step_grounding_fallback.get("grounding", {}))
        if role == "repair_action":
            return "memory_replay_step_grounding_repair"
        return "memory_replay_step_grounding_fallback"
    if visual_reaiming is not None:
        if str(visual_reaiming.get("status") or "").strip().lower() == "coordinate_copy":
            return "memory_replay_coordinate_copy"
        return "memory_replay_visual_reaimed"
    if isinstance(payload.get("replay"), Mapping):
        return "memory_replay_direct"
    if memory_replay_visual_reaim_required(payload):
        return "memory_replay_direct"
    return "scripted"


def summarize_action_step_provenance(
    records: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    executed_steps = sum(
        1
        for record in records
        if int(record.get("performed_action_count") or 0) > 0
    )
    step_grounding_fallback_steps = sum(
        1
        for record in records
        if bool(record.get("is_step_grounding_fallback"))
        and int(record.get("performed_action_count") or 0) > 0
    )
    task_level_planning_fallback_steps = sum(
        1
        for record in records
        if bool(record.get("is_task_level_planning_fallback"))
        and int(record.get("performed_action_count") or 0) > 0
    )
    memory_enhanced_replay_fallback_steps = sum(
        1
        for record in records
        if bool(record.get("is_memory_enhanced_replay_fallback"))
        and int(record.get("performed_action_count") or 0) > 0
    )
    by_provenance: Dict[str, int] = {}
    for record in records:
        if int(record.get("performed_action_count") or 0) <= 0:
            continue
        provenance = str(record.get("action_provenance") or "unknown")
        by_provenance[provenance] = by_provenance.get(provenance, 0) + 1
    return {
        "executed_step_count": executed_steps,
        "step_grounding_fallback_step_count": step_grounding_fallback_steps,
        "step_grounding_fallback_rate": safe_rate(
            step_grounding_fallback_steps,
            executed_steps,
        ),
        "task_level_planning_fallback_step_count": task_level_planning_fallback_steps,
        "task_level_planning_fallback_rate": safe_rate(
            task_level_planning_fallback_steps,
            executed_steps,
        ),
        "memory_enhanced_replay_fallback_step_count": (
            memory_enhanced_replay_fallback_steps
        ),
        "memory_enhanced_replay_fallback_rate": safe_rate(
            memory_enhanced_replay_fallback_steps,
            executed_steps,
        ),
        "by_action_provenance": by_provenance,
    }


def build_memory_replay_visual_grounding_target(
    visual_binding: Mapping[str, Any],
) -> Dict[str, Any]:
    action_index = int(visual_binding.get("action_index") or 0)
    target_id = str(visual_binding.get("node_id") or f"memory-action-{action_index}")
    target_bbox = visual_binding.get("target_bbox")
    click_ratio = visual_binding.get("click_ratio")
    return {
        "id": target_id,
        "label": target_id,
        "target_id": target_id,
        "memory_target_id": target_id,
        "reference_image_path": str(visual_binding.get("reference_image_path") or ""),
        "target_bbox": dict(target_bbox) if isinstance(target_bbox, Mapping) else {},
        "click_ratio": list(click_ratio) if isinstance(click_ratio, list) else [0.5, 0.5],
        "threshold": optional_float(visual_binding.get("threshold"), 0.78),
        "min_match_margin": optional_float(visual_binding.get("min_match_margin"), 0.02),
        "min_scale": optional_float(visual_binding.get("min_scale"), 0.5),
        "max_scale": optional_float(visual_binding.get("max_scale"), 2.0),
        "scale_step": optional_float(visual_binding.get("scale_step"), 0.05),
        "match_mode": str(visual_binding.get("match_mode") or "gray"),
    }


def build_memory_replay_visual_grounding_targets(
    visual_binding: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    base_target = build_memory_replay_visual_grounding_target(visual_binding)
    candidates = visual_binding.get("reference_candidates")
    if not isinstance(candidates, list) or not candidates:
        return [base_target]

    targets: List[Dict[str, Any]] = []
    base_id = str(base_target.get("id") or base_target.get("label") or "memory-target")
    for index, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, Mapping):
            continue
        reference_path = str(candidate.get("reference_image_path") or "").strip()
        if not reference_path:
            continue
        target = dict(base_target)
        target["id"] = f"{base_id}-candidate-{index}"
        target["label"] = base_id
        target["target_id"] = base_id
        target["memory_target_id"] = base_id
        target["reference_image_path"] = reference_path
        target_bbox = candidate.get("target_bbox")
        target["target_bbox"] = (
            dict(target_bbox) if isinstance(target_bbox, Mapping) else {}
        )
        click_ratio = candidate.get("click_ratio")
        target["click_ratio"] = (
            list(click_ratio) if isinstance(click_ratio, list) else [0.5, 0.5]
        )
        target["crop_variant"] = str(candidate.get("crop_variant") or "")
        targets.append(target)
    return targets or [base_target]


def build_memory_replay_step_grounding_next_step(
    stored_action: Mapping[str, Any],
    visual_binding: Mapping[str, Any],
    *,
    reaiming_error: MemoryReplayReaimingError,
    fallback_attempt: int,
) -> Dict[str, Any]:
    action_name = str(stored_action.get("action") or "").strip().lower()
    if action_name == "move":
        grounding_action = "move_target"
    elif int(stored_action.get("clicks") or 1) > 1:
        grounding_action = "double_click_target"
    else:
        grounding_action = "click_target"
    node_id = str(visual_binding.get("node_id") or f"memory-action-{fallback_attempt}")
    intent = str(stored_action.get("target_description") or "").strip()
    return {
        "action": grounding_action,
        "target_description": (
            f"{intent} (the attached reference crop shows how it looked when recorded; "
            "its text or content may differ now, so choose by this description)"
            if intent
            else "the memorized UI target shown in the attached reference crop for "
            f"replay action {visual_binding.get('action_index') or fallback_attempt}"
        ),
        "visual_target_id": node_id,
        "memory_target_id": node_id,
        "button": str(stored_action.get("button") or "left"),
        "clicks": optional_int(stored_action.get("clicks"), 1),
        "interval": optional_float(stored_action.get("interval"), 0.0),
        "duration": optional_float(stored_action.get("duration"), 0.0),
        "expected_outcome": str(
            stored_action.get("expected_outcome")
            or "The replay target is activated, or a transient blocker is dismissed so the same replay step can be retried."
        ),
        "reaiming_error": str(reaiming_error),
        "fallback_attempt": fallback_attempt,
    }


def build_memory_replay_step_grounding_prompt(
    existing_prompt: Any,
    visual_binding: Mapping[str, Any],
    *,
    reaiming_error: MemoryReplayReaimingError,
    fallback_attempt: int,
) -> str:
    prompt_parts = [
        str(existing_prompt or "").strip(),
        (
            "Replay is the execution priority. This fallback is only for the current "
            f"memory replay action {visual_binding.get('action_index')}; do not solve the "
            "whole task from scratch. Prefer the target described in the next step; the "
            "reference crop shows how it looked when recorded, and content such as titles "
            "or dates may have changed. Never pick a different element only because it "
            "looks like the crop or sits in the same place. If the description names "
            "content from when the step was recorded (a title, a name, a date), find the "
            "element that plays the same role for the current task instead. If a "
            "temporary blocker hides it, choose one safe dismiss/continue/cancel/close "
            "control and label it target_role='repair_action'. Otherwise label the "
            "target hit target_role='memory_target'."
        ),
        f"Visual re-aiming failure: {reaiming_error}",
        f"Fallback attempt: {fallback_attempt}",
    ]
    return "\n\n".join(part for part in prompt_parts if part)


def grounding_role(grounding: Mapping[str, Any]) -> str:
    raw_role = str(
        grounding.get("target_role")
        or grounding.get("grounding_role")
        or grounding.get("role")
        or ""
    ).strip().lower().replace("-", "_")
    if raw_role in {
        "repair",
        "repair_action",
        "blocker_repair",
        "dismiss_blocker",
        "blocker_dismissal",
    }:
        return "repair_action"
    return "memory_target"


def grounding_reference_image_paths(
    payload: Mapping[str, Any],
    working_directory: Path,
) -> List[Path]:
    return grounding_reference_image_paths_for_step(payload, {}, working_directory)


def grounding_reference_image_paths_for_step(
    payload: Mapping[str, Any],
    next_step: Mapping[str, Any],
    working_directory: Path,
) -> List[Path]:
    paths: List[Path] = []
    targets = grounding_targets_for_payload(payload)
    if isinstance(next_step, Mapping) and next_step:
        targets = sorted(
            targets,
            key=lambda target: grounding_target_step_score(target, next_step),
            reverse=True,
        )
    for target in targets:
        for key in ("reference_image_path", "reference_path", "image_path", "path"):
            raw_path = str(target.get(key) or "").strip()
            if not raw_path:
                continue
            path = Path(raw_path)
            if not path.is_absolute():
                path = working_directory / path
            if path.exists() and path.is_file() and path not in paths:
                paths.append(path)
            break
    return paths[:3]


def grounding_target_step_score(
    target: Mapping[str, Any],
    next_step: Mapping[str, Any],
) -> float:
    requested_ids = [
        str(next_step.get(key, "")).strip()
        for key in ("visual_target_id", "memory_target_id", "grounding_target_id", "target_id")
    ]
    requested_ids = [item for item in requested_ids if item]
    target_ids = [
        str(target.get(key, "")).strip()
        for key in ("id", "name", "label", "target_id", "memory_target_id")
    ]
    target_ids = [item for item in target_ids if item]
    if requested_ids and target_ids:
        if any(
            normalize_grounding_score_text(requested)
            == normalize_grounding_score_text(target_id)
            for requested in requested_ids
            for target_id in target_ids
        ):
            return 10.0
        return -10.0

    step_terms = [
        str(next_step.get(key, "")).strip()
        for key in ("target_description", "target_text", "description", "text")
    ]
    target_terms = [
        str(target.get(key, "")).strip()
        for key in ("target_description", "target_text", "description", "label", "name")
    ]
    step_terms = [item for item in step_terms if item]
    target_terms = [item for item in target_terms if item]
    if not step_terms or not target_terms:
        return 0.0

    best = 0.0
    for step_term in step_terms:
        normalized_step = normalize_grounding_score_text(step_term)
        step_tokens = set(normalized_step.split())
        for target_term in target_terms:
            normalized_target = normalize_grounding_score_text(target_term)
            if not normalized_step or not normalized_target:
                continue
            if normalized_step == normalized_target:
                best = max(best, 6.0)
            elif normalized_step in normalized_target or normalized_target in normalized_step:
                best = max(best, 4.0)
            else:
                target_tokens = set(normalized_target.split())
                if step_tokens and target_tokens:
                    best = max(
                        best,
                        len(step_tokens & target_tokens)
                        / max(len(step_tokens), len(target_tokens)),
                    )
    return best


def normalize_grounding_score_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()


def grounding_targets_for_payload(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    targets: List[Dict[str, Any]] = []
    for key in (
        "visual_grounding_targets",
        "cv_grounding_targets",
        "grounding_targets",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            targets.extend(dict(item) for item in value if isinstance(item, Mapping))
        elif isinstance(value, Mapping):
            targets.append(dict(value))
    for key in (
        "visual_grounding_target",
        "cv_grounding_target",
        "grounding_target",
    ):
        value = payload.get(key)
        if isinstance(value, Mapping):
            targets.append(dict(value))
    if any(
        str(payload.get(key) or "").strip()
        for key in ("reference_image_path", "reference_path", "image_path", "path")
    ):
        targets.append(dict(payload))
    return targets


def snapshot_desktop_downloads(
    goal: str,
    payload: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    if not desktop_download_evidence_enabled(goal, payload):
        return None

    directory = desktop_download_directory(payload)
    files: Dict[str, Dict[str, Any]] = {}
    try:
        entries = list(directory.iterdir())
    except OSError:
        return {
            "directory": str(directory),
            "available": False,
            "files": files,
        }

    for path in entries:
        try:
            if not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        files[path.name] = {
            "name": path.name,
            "path": str(path),
            "suffix": path.suffix,
            "size_bytes": stat.st_size,
            "modified_ns": stat.st_mtime_ns,
            "is_partial": path.suffix.lower() in PARTIAL_DOWNLOAD_SUFFIXES,
        }

    return {
        "directory": str(directory),
        "available": True,
        "files": files,
    }


def with_desktop_download_evidence(
    goal: str,
    payload: Mapping[str, Any],
    baseline: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    updated = dict(payload)
    updated.pop("download_evidence", None)
    evidence = build_desktop_download_evidence(goal, payload, baseline)
    if evidence is not None:
        updated["download_evidence"] = evidence
    return updated


def build_desktop_download_evidence(
    goal: str,
    payload: Mapping[str, Any],
    baseline: Optional[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    if baseline is None:
        return None

    current = snapshot_desktop_downloads(goal, {**dict(payload), "download_evidence_enabled": True})
    if current is None:
        return None

    baseline_files = baseline.get("files") if isinstance(baseline, Mapping) else {}
    current_files = current.get("files") if isinstance(current, Mapping) else {}
    if not isinstance(baseline_files, Mapping) or not isinstance(current_files, Mapping):
        return None

    new_or_changed: List[Dict[str, Any]] = []
    partial_files: List[Dict[str, Any]] = []
    for name, metadata in current_files.items():
        if not isinstance(metadata, Mapping):
            continue
        compact_metadata = {
            "name": metadata.get("name"),
            "path": metadata.get("path"),
            "suffix": metadata.get("suffix"),
            "size_bytes": metadata.get("size_bytes"),
            "is_partial": metadata.get("is_partial"),
        }
        if bool(metadata.get("is_partial")):
            partial_files.append(compact_metadata)
            continue
        previous = baseline_files.get(name)
        if not isinstance(previous, Mapping):
            new_or_changed.append(compact_metadata)
            continue
        if (
            metadata.get("size_bytes") != previous.get("size_bytes")
            or metadata.get("modified_ns") != previous.get("modified_ns")
        ):
            new_or_changed.append(compact_metadata)

    new_or_changed.sort(key=lambda item: str(item.get("name") or ""))
    partial_files.sort(key=lambda item: str(item.get("name") or ""))
    return {
        "directory": current.get("directory"),
        "available": bool(current.get("available")),
        "baseline_file_count": len(baseline_files),
        "current_file_count": len(current_files),
        "new_or_changed_completed_files": new_or_changed[:10],
        "partial_files": partial_files[:10],
    }


def desktop_download_evidence_enabled(goal: str, payload: Mapping[str, Any]) -> bool:
    explicit = payload.get("download_evidence_enabled")
    if explicit is not None:
        return bool(explicit)

    text_parts = [
        goal,
        str(payload.get("summary", "")),
        str(payload.get("goal", "")),
        str(payload.get("expected_outcome", "")),
    ]
    try:
        text_parts.append(json.dumps(payload, default=str))
    except (TypeError, ValueError):
        pass
    haystack = " ".join(text_parts).casefold()
    return any(term in haystack for term in DOWNLOAD_TASK_TERMS)


def desktop_download_directory(payload: Mapping[str, Any]) -> Path:
    directory = (
        payload.get("download_directory")
        or payload.get("download_dir")
        or payload.get("downloads_directory")
    )
    if isinstance(directory, (str, Path)) and str(directory).strip():
        return Path(str(directory)).expanduser().resolve()
    return (Path.home() / "Downloads").resolve()


def parse_grounding_response(raw_text: str) -> Dict[str, Any]:
    parsed = extract_json_object(raw_text)
    if not isinstance(parsed, Mapping):
        raise ValueError("Desktop grounding model did not return a valid JSON object.")

    x = parsed.get("x")
    y = parsed.get("y")
    located = bool(parsed.get("located", x is not None and y is not None))
    reason = str(parsed.get("reason") or parsed.get("assessment") or "").strip()
    confidence = optional_float(parsed.get("confidence"), 0.0)
    target_role = str(
        parsed.get("target_role")
        or parsed.get("grounding_role")
        or parsed.get("role")
        or ""
    ).strip()

    if located:
        if x is None or y is None:
            raise ValueError("Grounding response marked located=true but omitted x or y.")
        result = {
            "located": True,
            "x": required_int(parsed, "x"),
            "y": required_int(parsed, "y"),
            "confidence": confidence,
            "reason": reason or "Target located.",
        }
        if target_role:
            result["target_role"] = target_role
        action = str(parsed.get("action") or "").strip()
        if action:
            result["action"] = action
        return result

    result = {
        "located": False,
        "x": None,
        "y": None,
        "confidence": confidence,
        "reason": reason or "Target was not confidently located.",
    }
    if target_role:
        result["target_role"] = target_role
    return result


def requires_grounding(next_step: Mapping[str, Any]) -> bool:
    return str(next_step.get("action", "")).strip().lower() in DESKTOP_GROUNDABLE_ACTIONS


def materialize_desktop_action(
    next_step: Mapping[str, Any],
    grounding: Mapping[str, Any],
) -> Dict[str, Any]:
    action_name = str(next_step.get("action", "")).strip().lower()
    if action_name == "move_target":
        return {
            "action": "move",
            "x": required_int(grounding, "x"),
            "y": required_int(grounding, "y"),
            "duration": optional_float(next_step.get("duration"), 0.0),
            "expected_outcome": str(next_step.get("expected_outcome", "")).strip(),
        }

    return {
        "action": "click",
        "x": required_int(grounding, "x"),
        "y": required_int(grounding, "y"),
        "button": str(next_step.get("button") or "left"),
        "clicks": optional_int(next_step.get("clicks"), 2 if action_name == "double_click_target" else 1),
        "interval": optional_float(
            next_step.get("interval"),
            DEFAULT_DOUBLE_CLICK_INTERVAL_SECONDS if action_name == "double_click_target" else 0.0,
        ),
        "expected_outcome": str(next_step.get("expected_outcome", "")).strip(),
    }


def enrich_scripted_desktop_action(
    action: Mapping[str, Any],
    *,
    goal: str,
    iteration: int,
    total_actions: int,
) -> Dict[str, Any]:
    enriched = dict(action)
    expected_outcome = str(action.get("expected_outcome", "")).strip()
    if not expected_outcome:
        if iteration == total_actions:
            expected_outcome = goal
        else:
            expected_outcome = default_expected_outcome_for_action(action)
    enriched["expected_outcome"] = expected_outcome
    return enriched


def build_scripted_desktop_action_source(
    payload: Mapping[str, Any],
    *,
    iteration: int,
    total_actions: int,
) -> Dict[str, Any]:
    replay = payload.get("replay")
    if not isinstance(replay, Mapping):
        return {}
    adaptation = replay.get("adaptation")
    adaptation_payload = dict(adaptation) if isinstance(adaptation, Mapping) else {}
    return {
        "agent": "memory-replay",
        "workflow": "memory_replay",
        "memory_path_id": adaptation_payload.get("memory_path_id") or replay.get("source_task_id"),
        "compatibility_score": adaptation_payload.get("score"),
        "matched_query_key": adaptation_payload.get("matched_query_key"),
        "replay_action_index": int(iteration),
        "replay_action_count": int(total_actions),
    }


def default_expected_outcome_for_action(action: Mapping[str, Any]) -> str:
    action_name = str(action.get("action", "")).strip().lower()
    if action_name == "click":
        return "The clicked desktop target visibly responds and the UI advances."
    if action_name == "move":
        return "The cursor visibly moves to the requested target."
    if action_name == "type":
        typed_text = str(action.get("text", "")).strip()
        if typed_text:
            return f"The text '{typed_text}' is visibly entered into the focused field."
        return "The requested text is visibly entered into the focused field."
    if action_name == "press":
        key = str(action.get("key", "")).strip()
        if key:
            return f"The UI visibly responds to the {key} key press."
        return "The UI visibly responds to the requested key press."
    if action_name == "hotkey":
        keys = action.get("keys")
        if isinstance(keys, list) and keys:
            return f"The screen visibly responds to the {'+'.join(str(key) for key in keys)} shortcut."
        return "The screen visibly responds to the requested shortcut."
    if action_name == "scroll":
        return "The visible content scrolls in the intended direction."
    if action_name == "wait":
        return "The expected UI state becomes visible after waiting."
    return "The desktop visibly changes in the intended way."


def parse_observation_label(label: str) -> Dict[str, Any]:
    normalized = str(label or "").strip().lower()
    match = re.match(r"^(before|after)-(\d+)$", normalized)
    if not match:
        return {"phase": None, "iteration": None}
    return {
        "phase": match.group(1),
        "iteration": int(match.group(2)),
    }


def count_desktop_control_actions(result: Any) -> int:
    if not isinstance(result, Mapping):
        return 0

    results = result.get("results")
    if isinstance(results, list):
        return sum(
            1
            for item in results
            if isinstance(item, Mapping)
            and str(item.get("action", "")).strip().lower() not in {"observe", "position", "screen_size"}
        )

    raw_action_count = result.get("action_count")
    if raw_action_count is None:
        return 0
    try:
        return max(0, int(raw_action_count))
    except (TypeError, ValueError):
        return 0
