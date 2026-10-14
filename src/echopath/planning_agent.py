from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Sequence

from .agent import BaseAgent
from .agent_utils import extract_json_object, optional_float, optional_int, required_int
from .models import AgentTask
from .router import ModelRequest
from .runtime import AgentContext


DESKTOP_GROUNDABLE_ACTIONS = (
    "click_target",
    "double_click_target",
    "move_target",
)
DESKTOP_DIRECT_ACTIONS = (
    "click",
    "move",
    "type",
    "paste_text",
    "press",
    "hotkey",
    "scroll",
    "wait",
)
SUPPORTED_DESKTOP_PLANNER_ACTIONS = DESKTOP_GROUNDABLE_ACTIONS + DESKTOP_DIRECT_ACTIONS
DESKTOP_HISTORY_LIMIT = 4
DESKTOP_STEP_OUTCOME_STATUSES = {
    "succeeded",
    "progressed",
    "pending",
    "blocked",
}
SUPPORTED_TASK_TYPES = (
    "none",
    "desktop_task",
    "command_task",
    "file_task",
)

TASK_ROUTING_INSTRUCTIONS = """You are the EchoPath planning agent.

You are responsible for:
1. complex task planning,
2. execution planning,
3. sub-agent task routing,
4. execution-plan revision when needed.

Return JSON only with this exact shape:
{
  "assistant_reply": "string",
  "task": {
    "type": "none" | "desktop_task" | "command_task" | "file_task",
    "summary": "short string",
    "goal": "short string",
    "payload": {}
  }
}

Routing rules:
- Choose task.type based on the user's primary requested task domain.
- The orchestrator will route later based only on task.type.
- Use task.type = "none" for normal conversation, planning, product questions, or when the user did not explicitly ask to execute something now.
- Use task.type = "desktop_task" when the user is explicitly asking the agent to use GUI operations on the real device right now, including web browsing in a visible browser window.
- Use task.type = "command_task" only when the user is explicitly asking the agent to run terminal commands right now.
- Use task.type = "file_task" only when the user is explicitly asking the agent to read, write, or edit files right now.
- Never claim you can bypass, solve, or automate CAPTCHA / human verification. For those cases, use task.type = "none" and ask the user to complete the verification manually before continuing.
- Never use desktop_task for passwords, payments, security prompts, or destructive confirmation dialogs.

Payload guidance:
- For desktop_task payloads, prefer high-level guidance because the GUI agent will:
  1. capture the current screen,
  2. request the next GUI step from the planning agent,
  3. ground the exact target when a visual click or move is needed,
  4. execute the step,
  5. observe again to verify progress.
  Example:
  {
    "planner_model": "default",
    "planner_router_name": "default",
    "planner_prompt": "View my desktop and open the AIeMarket folder via GUI operation.",
    "grounding_model": "default",
    "grounding_router_name": "default",
    "capture_after_action": true,
    "observation_directory": "local_data/desktop_observations"
  }
- For browser workflows, still use desktop_task. The browser is just another visible desktop app, so describe the desired navigation, search, and download outcome in planner_prompt instead of using DOM selectors or hidden browser drivers.
- Omit payload.max_iterations for normal desktop tasks unless you intentionally need a non-default cap. This allows the GUI executor to honor the configured desktop max-iterations setting.
- Use explicit desktop actions only when the next steps are fully deterministic and do not depend on async UI readiness, Spotlight, app launches, modal dialogs, or uncertain focus changes.
- For launching or switching apps and windows, prefer the high-level planner loop instead of scripted key sequences.
  {
    "actions": [
      {"action": "hotkey", "keys": ["command", "space"]},
      {"action": "type", "text": "Finder"},
      {"action": "press", "key": "enter"}
    ],
    "capture_after_action": true,
    "observation_directory": "local_data/desktop_observations"
  }
- For command_task payloads, use:
  {
    "commands": [
      {
        "command": "pwd",
        "cwd": ".",
        "timeout_seconds": 120,
        "check": false,
        "result_key": "command_1"
      }
    ]
  }
- For file_task payloads, use:
  {
    "actions": [
      {"action": "read", "path": "README.md", "result_key": "readme"},
      {"action": "write", "path": "notes.txt", "content": "hello", "result_key": "note"},
      {
        "action": "replace_text",
        "path": "src/app.py",
        "old_text": "old",
        "new_text": "new",
        "replace_all": false,
        "result_key": "edit"
      }
    ]
  }
- Keep assistant_reply concise, useful, and user-facing.
- Never include markdown code fences.
"""


@dataclass(frozen=True)
class PlanningDecision:
    assistant_reply: str
    task_type: str = "none"
    task_summary: str = ""
    task_goal: str = ""
    task_payload: Optional[Dict[str, Any]] = None
    raw_response: str = ""

    def to_output(self) -> Dict[str, Any]:
        return {
            "assistant_reply": self.assistant_reply,
            "task": {
                "type": self.task_type,
                "summary": self.task_summary,
                "goal": self.task_goal,
                "payload": dict(self.task_payload or {}),
            },
            "raw_response": self.raw_response,
        }

    @classmethod
    def from_output(cls, output: Mapping[str, Any]) -> "PlanningDecision":
        if not isinstance(output, Mapping):
            return cls(assistant_reply="I’m ready to help.")
        return parse_planning_response(json.dumps(dict(output)))


class PlanningAgent(BaseAgent):
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

    async def plan(self, task: AgentTask, context: AgentContext):
        return {
            "goal": task.goal,
            "steps": [
                "Review the conversation context and current memory.",
                "Classify the request into a task type for routing.",
                "Produce or revise the next execution step when GUI planning is needed.",
            ],
        }

    async def execute(self, task: AgentTask, context: AgentContext):
        planning_mode = str(task.payload.get("planning_mode", "route_task")).strip().lower()
        if planning_mode == "desktop_next_step":
            return await self._execute_desktop_next_step(task, context)
        if planning_mode == "desktop_step_verification":
            return await self._execute_desktop_step_verification(task, context)
        if planning_mode == "desktop_goal_verification":
            return await self._execute_desktop_goal_verification(task, context)

        decision = await self.decide_task(task, context)
        context.state.setdefault("planning", {})["route_decision"] = decision.to_output()["task"]
        await context.emit(
            "agent.planning.route.selected",
            "Planning agent selected a task type.",
            {"task_type": decision.task_type, "task_summary": decision.task_summary},
        )
        return decision.to_output()

    async def decide_task(self, task: AgentTask, context: AgentContext) -> PlanningDecision:
        direct_decision = decision_from_task_payload(task.payload)
        if direct_decision is not None:
            return direct_decision

        messages = list(task.payload.get("messages", []))
        latest_message = str(task.payload.get("message", "")).strip()
        memory = task.payload.get("memory")
        execution_constraints = task.payload.get("execution_constraints")
        model_input = self._build_model_input(
            messages=messages,
            latest_message=latest_message,
            memory=memory if isinstance(memory, Mapping) else None,
            execution_constraints=(
                execution_constraints if isinstance(execution_constraints, Mapping) else None
            ),
        )

        response = await context.call_model(
            ModelRequest(
                model="default",
                input=model_input,
                instructions=TASK_ROUTING_INSTRUCTIONS,
                reasoning_effort="medium",
                max_output_tokens=900,
            ),
            router_name=str(task.payload.get("router_name", self.router_name)),
        )
        return parse_planning_response(response.output_text)

    async def plan_desktop_next_step(
        self,
        context: AgentContext,
        goal: str,
        observation: Mapping[str, Any],
        *,
        iteration: int,
        history: List[Mapping[str, Any]],
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        planner_request = self._build_desktop_planner_request(
            goal,
            observation,
            iteration=iteration,
            history=history,
            payload=payload,
        )
        response = await context.call_model(
            planner_request,
            router_name=str(payload.get("planner_router_name", self.router_name)),
        )
        plan = parse_desktop_plan_response(response.output_text)
        plan["source"] = f"model:{payload.get('planner_router_name', self.router_name)}"
        plan["model"] = response.model
        plan["response_id"] = response.response_id
        plan["request_id"] = response.request_id
        await context.emit(
            "agent.desktop.planned",
            f"Planning agent completed the desktop plan for iteration {iteration}.",
            {
                "iteration": iteration,
                "model": response.model,
                "completed": bool(plan.get("completed")),
                "assessment": plan.get("assessment"),
                "next_step": plan.get("next_step"),
            },
        )
        return plan

    async def verify_desktop_step(
        self,
        context: AgentContext,
        goal: str,
        before_observation: Mapping[str, Any],
        after_observation: Mapping[str, Any],
        *,
        iteration: int,
        action: Mapping[str, Any],
        history: List[Mapping[str, Any]],
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        verifier_request = self._build_desktop_step_verifier_request(
            goal,
            before_observation,
            after_observation,
            iteration=iteration,
            action=action,
            history=history,
            payload=payload,
        )
        router_name = str(
            payload.get(
                "verification_router_name",
                payload.get("planner_router_name", self.router_name),
            )
        )
        response = await context.call_model(
            verifier_request,
            router_name=router_name,
        )
        verification = parse_desktop_step_verification_response(response.output_text)
        verification["source"] = f"model:{router_name}"
        verification["model"] = response.model
        verification["response_id"] = response.response_id
        verification["request_id"] = response.request_id
        return verification

    async def verify_desktop_goal(
        self,
        context: AgentContext,
        goal: str,
        observation: Mapping[str, Any],
        *,
        iteration: int,
        history: List[Mapping[str, Any]],
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        verifier_request = self._build_desktop_goal_verifier_request(
            goal,
            observation,
            iteration=iteration,
            history=history,
            payload=payload,
        )
        router_name = str(
            payload.get(
                "verification_router_name",
                payload.get("planner_router_name", self.router_name),
            )
        )
        response = await context.call_model(
            verifier_request,
            router_name=router_name,
        )
        verification = parse_desktop_goal_verification_response(response.output_text)
        verification["source"] = f"model:{router_name}"
        verification["model"] = response.model
        verification["response_id"] = response.response_id
        verification["request_id"] = response.request_id
        return verification

    async def _execute_desktop_next_step(
        self,
        task: AgentTask,
        context: AgentContext,
    ) -> Dict[str, Any]:
        observation = task.payload.get("observation")
        payload = task.payload.get("task_payload")
        if not isinstance(observation, Mapping):
            raise ValueError("Desktop planning requires an observation payload.")
        if not isinstance(payload, Mapping):
            raise ValueError("Desktop planning requires a task_payload mapping.")

        history = task.payload.get("history")
        if not isinstance(history, list):
            history = []

        return await self.plan_desktop_next_step(
            context,
            str(task.payload.get("goal") or task.goal),
            observation,
            iteration=max(1, int(task.payload.get("iteration", 1))),
            history=history,
            payload=payload,
        )

    async def _execute_desktop_step_verification(
        self,
        task: AgentTask,
        context: AgentContext,
    ) -> Dict[str, Any]:
        before_observation = task.payload.get("before_observation")
        after_observation = task.payload.get("after_observation")
        action = task.payload.get("action")
        payload = task.payload.get("task_payload")
        if not isinstance(before_observation, Mapping):
            raise ValueError("Desktop step verification requires a before_observation mapping.")
        if not isinstance(after_observation, Mapping):
            raise ValueError("Desktop step verification requires an after_observation mapping.")
        if not isinstance(action, Mapping):
            raise ValueError("Desktop step verification requires an action mapping.")
        if not isinstance(payload, Mapping):
            raise ValueError("Desktop step verification requires a task_payload mapping.")

        history = task.payload.get("history")
        if not isinstance(history, list):
            history = []

        return await self.verify_desktop_step(
            context,
            str(task.payload.get("goal") or task.goal),
            before_observation,
            after_observation,
            iteration=max(1, int(task.payload.get("iteration", 1))),
            action=action,
            history=history,
            payload=payload,
        )

    async def _execute_desktop_goal_verification(
        self,
        task: AgentTask,
        context: AgentContext,
    ) -> Dict[str, Any]:
        observation = task.payload.get("observation")
        payload = task.payload.get("task_payload")
        if not isinstance(observation, Mapping):
            raise ValueError("Desktop goal verification requires an observation mapping.")
        if not isinstance(payload, Mapping):
            raise ValueError("Desktop goal verification requires a task_payload mapping.")

        history = task.payload.get("history")
        if not isinstance(history, list):
            history = []

        return await self.verify_desktop_goal(
            context,
            str(task.payload.get("goal") or task.goal),
            observation,
            iteration=max(1, int(task.payload.get("iteration", 1))),
            history=history,
            payload=payload,
        )

    def _build_model_input(
        self,
        *,
        messages: List[Mapping[str, Any]],
        latest_message: str,
        memory: Optional[Mapping[str, Any]],
        execution_constraints: Optional[Mapping[str, Any]] = None,
    ) -> List[Dict[str, str]]:
        history = [
            {
                "role": "system",
                "content": (
                    "Supported task types: none, desktop_task, command_task, file_task. "
                    "Route browser workflows as visible desktop GUI tasks."
                ),
            }
        ]

        if memory:
            history.append(
                {
                    "role": "system",
                    "content": f"Current memory snapshot: {json.dumps(dict(memory), ensure_ascii=False)}",
                }
            )

        if execution_constraints:
            history.append(
                {
                    "role": "system",
                    "content": (
                        "Execution constraints for this run: "
                        f"{json.dumps(dict(execution_constraints), ensure_ascii=False)}"
                    ),
                }
            )

        for message in messages[-10:]:
            role = str(message.get("role", "user")).strip().lower()
            if role not in {"user", "assistant", "system"}:
                role = "user"
            content = str(message.get("content", "")).strip()
            if content:
                history.append({"role": role, "content": content})

        if latest_message and not any(item["role"] == "user" for item in history[1:]):
            history.append({"role": "user", "content": latest_message})

        return history

    def _build_desktop_planner_request(
        self,
        goal: str,
        observation: Mapping[str, Any],
        *,
        iteration: int,
        history: List[Mapping[str, Any]],
        payload: Mapping[str, Any],
    ) -> ModelRequest:
        observation_path = Path(str(observation["path"]))
        history_summary = [
            summarize_desktop_iteration(item)
            for item in history[-DESKTOP_HISTORY_LIMIT:]
        ]
        prompt_payload = {
            "goal": goal,
            "iteration": iteration,
            "supported_actions": list(SUPPORTED_DESKTOP_PLANNER_ACTIONS),
            "history": history_summary,
            "observation_path": str(observation_path),
            "observation_metadata": dict(observation),
            "task_payload_hints": {
                "observation_region": payload.get("observation_region"),
            },
        }
        memory_enhanced_context = payload.get("memory_replay_enhanced_fallback_context")
        if isinstance(memory_enhanced_context, Mapping):
            prompt_payload["memory_replay_enhanced_fallback"] = dict(
                memory_enhanced_context
            )
        instructions = (
            "You are the EchoPath planning agent handling desktop execution planning. "
            "Review the screenshot and decide the single best next step to advance the task. "
            "Respond with JSON only using this shape: "
            "{\"assessment\": string, \"completed\": boolean, \"next_step\": object|null}. "
            "Use exactly one next_step at a time. Allowed next_step.action values are: "
            f"{', '.join(SUPPORTED_DESKTOP_PLANNER_ACTIONS)}. "
            "Every next_step must include an expected_outcome string describing what must be "
            "visibly true in the very next screenshot if the step succeeds. "
            "Use *_target actions when the exact pixel must be chosen from the image later by an "
            "image-grounding agent. For click_target, double_click_target, or move_target include "
            "a clear target_description and optionally target_text. For click or move without "
            "grounding, include integer x and y coordinates directly. For hotkey, include keys. "
            "For press, include key. For type or paste_text, include text. Prefer paste_text "
            "over type for exact text, multi-line text, structured data, JSON, formulas, or "
            "any full-file replacement, because it avoids partial keyboard entry mistakes. "
            "For scroll, include clicks. For wait, include seconds. Only return completed=true when the requested end state is "
            "directly visible in the current screenshot. Do not infer completion from Dock "
            "indicators, menu bar state, background app status, or assumed focus. For app, "
            "window, or browser launch tasks, completed=true requires the requested app or "
            "window to be visibly open and ready for interaction in the screenshot. If the task "
            "is unsafe to continue, return completed=true with next_step=null and explain why in "
            "assessment."
        )
        if isinstance(memory_enhanced_context, Mapping):
            instructions = (
                f"{instructions}\n\nMemory-enhanced replay fallback guidance:\n"
                "You are not doing unconstrained full planning. A deterministic memory replay "
                "failed on the recorded action index shown in the payload, and your job is to "
                "pilot the same stored trajectory from the live screenshot. Use the trajectory "
                "as the primary plan. Prefer executing the current or next memory action, "
                "adapted to the live UI. You may perform one safe repair action to dismiss a "
                "transient blocker, re-open an earlier parent menu or dialog from the memory "
                "trajectory, or skip a stale optional prerequisite when a downstream memory "
                "target is already visible. Do not invent unrelated steps while a memory step "
                "or repair can advance the replay. When selecting a remembered visual target, "
                "include its memory_target_id or visual_target_id from the trajectory so the "
                "grounding layer can choose the matching reference crop. For remembered text, "
                "hotkey, press, scroll, or wait steps, preserve the stored parameters unless "
                "the live state makes a small adaptation clearly necessary. Mark completed=true "
                "only when the original user goal is visibly complete."
            )
            reference_images = memory_replay_enhanced_reference_image_paths(
                payload,
                self.working_directory,
            )
            if reference_images:
                prompt_payload["memory_reference_images"] = [
                    {"index": index + 1, "path": str(path)}
                    for index, path in enumerate(reference_images)
                ]
                instructions = (
                    f"{instructions}\n\nMemory reference crop images are attached after "
                    "the live screenshot. Use them to identify remembered replay targets."
                )
        else:
            reference_images = []
        planner_prompt = str(payload.get("planner_prompt", "")).strip()
        if planner_prompt:
            instructions = f"{instructions}\n\nTask-specific guidance:\n{planner_prompt}"

        request_input: List[Dict[str, Any]] = [
            {"type": "text", "text": json.dumps(prompt_payload, indent=2)},
            {"type": "local_image", "path": str(observation_path)},
        ]
        request_input.extend(
            {"type": "local_image", "path": str(path)} for path in reference_images
        )
        return ModelRequest(
            model=str(payload.get("planner_model", "default")),
            instructions=instructions,
            input=request_input,
            reasoning_effort=payload.get("planner_reasoning_effort"),
            temperature=payload.get("planner_temperature"),
            max_output_tokens=payload.get("planner_max_output_tokens"),
        )

    def _build_desktop_step_verifier_request(
        self,
        goal: str,
        before_observation: Mapping[str, Any],
        after_observation: Mapping[str, Any],
        *,
        iteration: int,
        action: Mapping[str, Any],
        history: List[Mapping[str, Any]],
        payload: Mapping[str, Any],
    ) -> ModelRequest:
        before_path = Path(str(before_observation["path"]))
        after_path = Path(str(after_observation["path"]))
        history_summary = [
            summarize_desktop_iteration(item)
            for item in history[-DESKTOP_HISTORY_LIMIT:]
        ]
        prompt_payload = {
            "goal": goal,
            "iteration": iteration,
            "action": summarize_desktop_action(action),
            "history": history_summary,
            "before_observation_path": str(before_path),
            "before_observation_metadata": dict(before_observation),
            "after_observation_path": str(after_path),
            "after_observation_metadata": dict(after_observation),
            "download_evidence": payload.get("download_evidence"),
        }
        instructions = (
            "You are the EchoPath desktop step verification agent. Compare the before and after "
            "screenshots and determine whether the executed step achieved, advanced, or blocked its expected "
            "outcome. Respond with JSON only using this shape: "
            "{\"assessment\": string, \"step_succeeded\": boolean, "
            "\"outcome_status\": \"succeeded\"|\"progressed\"|\"pending\"|\"blocked\", "
            "\"blocker\": string|null}. "
            "Use outcome_status=succeeded and step_succeeded=true only when the action's expected "
            "outcome is directly visible in the after screenshot or proven by provided non-visual "
            "evidence. Use outcome_status=progressed when the exact expected outcome is not fully "
            "visible yet, but the UI visibly advanced toward the goal, such as a URL changing to a "
            "PDF route, a loading viewer appearing, a Save/Download dialog opening, or a provided "
            "download_evidence entry showing a new completed file. Use outcome_status=pending when "
            "the operation appears to be settling and there is no clear blocker yet. Use "
            "outcome_status=blocked when the screen is unchanged, the wrong window prevents the next "
            "safe step, a permission/error dialog blocks progress, or there is direct evidence the "
            "action failed. Do not infer success from Dock indicators, app running status, menu bar "
            "state, or assumed keyboard focus. For exact text, formula, or structured-data edits, "
            "do not mark the step succeeded merely because a requested substring is visible; check "
            "for obvious extra or missing characters, duplicate braces, syntax errors, stale text, "
            "or unsaved dirty indicators in the visible editor."
        )
        verification_prompt = str(payload.get("verification_prompt", "")).strip()
        if verification_prompt:
            instructions = f"{instructions}\n\nTask-specific verification guidance:\n{verification_prompt}"

        return ModelRequest(
            model=str(payload.get("verification_model", payload.get("planner_model", "default"))),
            instructions=instructions,
            input=[
                {"type": "text", "text": json.dumps(prompt_payload, indent=2)},
                {"type": "local_image", "path": str(before_path)},
                {"type": "local_image", "path": str(after_path)},
            ],
            reasoning_effort=payload.get("verification_reasoning_effort"),
            temperature=payload.get("verification_temperature"),
            max_output_tokens=payload.get("verification_max_output_tokens"),
        )

    def _build_desktop_goal_verifier_request(
        self,
        goal: str,
        observation: Mapping[str, Any],
        *,
        iteration: int,
        history: List[Mapping[str, Any]],
        payload: Mapping[str, Any],
    ) -> ModelRequest:
        observation_path = Path(str(observation["path"]))
        history_summary = [
            summarize_desktop_iteration(item)
            for item in history[-DESKTOP_HISTORY_LIMIT:]
        ]
        prompt_payload = {
            "goal": goal,
            "iteration": iteration,
            "history": history_summary,
            "observation_path": str(observation_path),
            "observation_metadata": dict(observation),
            "download_evidence": payload.get("download_evidence"),
        }
        instructions = (
            "You are the EchoPath desktop goal verification agent. Review the screenshot and "
            "determine whether the user's requested desktop goal is visibly complete. Respond with "
            "JSON only using this shape: "
            "{\"assessment\": string, \"goal_completed\": boolean, \"blocker\": string|null}. "
            "Mark goal_completed=true only when the requested end state is directly visible and "
            "ready for interaction in the screenshot, or when provided non-visual evidence such as "
            "download_evidence proves that the requested downloaded file exists and is complete. "
            "Do not infer completion from Dock "
            "indicators, background app status, menu bar state, or assumed focus. For app, "
            "window, or browser launch tasks, the requested app or window must be visibly open in "
            "the screenshot. For exact text, formula, or structured-data edits, do not mark the "
            "goal complete merely because a requested substring is visible; reject obvious extra "
            "or missing characters, duplicate braces, syntax errors, stale text, or unsaved dirty "
            "indicators in the visible editor. If the goal is not visibly complete, set goal_completed=false and "
            "describe the blocker or missing evidence."
        )
        verification_prompt = str(payload.get("verification_prompt", "")).strip()
        if verification_prompt:
            instructions = f"{instructions}\n\nTask-specific verification guidance:\n{verification_prompt}"

        model_input = [
            {"type": "text", "text": json.dumps(prompt_payload, indent=2)},
            {"type": "local_image", "path": str(observation_path)},
        ]
        before = payload.get("goal_verification_before_observation")
        if isinstance(before, Mapping) and before.get("path") and str(before["path"]) != str(observation_path):
            prompt_payload["before_observation_path"] = str(before["path"])
            model_input[0] = {"type": "text", "text": json.dumps(prompt_payload, indent=2)}
            model_input.append({"type": "local_image", "path": str(before["path"])})
            instructions += (
                "\n\nTwo screenshots are attached. The FIRST is the current screen, to be judged. "
                "The SECOND was captured immediately before the actions in history ran; use it only "
                "to understand what changed."
            )

        return ModelRequest(
            model=str(payload.get("verification_model", payload.get("planner_model", "default"))),
            instructions=instructions,
            input=model_input,
            reasoning_effort=payload.get("verification_reasoning_effort"),
            temperature=payload.get("verification_temperature"),
            max_output_tokens=payload.get("verification_max_output_tokens"),
        )


def decision_from_task_payload(payload: Mapping[str, Any]) -> Optional[PlanningDecision]:
    raw_task = payload.get("task")
    if isinstance(raw_task, Mapping):
        parsed = {
            "assistant_reply": payload.get("assistant_reply", ""),
            "task": raw_task,
        }
        return parse_planning_response(json.dumps(parsed))

    task_type = str(payload.get("task_type", "")).strip()
    if not task_type:
        return None

    parsed = {
        "assistant_reply": payload.get("assistant_reply", ""),
        "task": {
            "type": task_type,
            "summary": payload.get("task_summary", ""),
            "goal": payload.get("task_goal", ""),
            "payload": payload.get("task_payload", {}),
        },
    }
    return parse_planning_response(json.dumps(parsed))


def memory_replay_enhanced_reference_image_paths(
    payload: Mapping[str, Any],
    working_directory: Path,
) -> List[Path]:
    context = payload.get("memory_replay_enhanced_fallback_context")
    if not isinstance(context, Mapping):
        return []
    limit = optional_int(
        payload.get("memory_replay_enhanced_fallback_reference_image_limit"),
        6,
    )
    limit = max(0, limit or 0)
    images = context.get("reference_images")
    if not isinstance(images, list) or limit <= 0:
        return []

    paths: List[Path] = []
    for item in images:
        if not isinstance(item, Mapping):
            continue
        raw_path = str(item.get("path") or item.get("reference_image_path") or "").strip()
        if not raw_path:
            continue
        path = Path(raw_path)
        if not path.is_absolute():
            path = working_directory / path
        if path.exists() and path.is_file() and path not in paths:
            paths.append(path)
        if len(paths) >= limit:
            break
    return paths


def parse_planning_response(raw_text: str) -> PlanningDecision:
    parsed = extract_json_object(raw_text)
    if not isinstance(parsed, Mapping):
        return PlanningDecision(
            assistant_reply=raw_text.strip() or "I’m ready to help.",
            raw_response=raw_text,
        )

    task = parsed.get("task")
    if not isinstance(task, Mapping):
        task = {}

    task_payload = task.get("payload")
    if not isinstance(task_payload, Mapping):
        task_payload = {}

    task_type = str(task.get("type", "none")).strip().lower() or "none"
    if task_type not in SUPPORTED_TASK_TYPES:
        task_type = "none"

    return PlanningDecision(
        assistant_reply=str(parsed.get("assistant_reply", "")).strip() or "I’m ready to help.",
        task_type=task_type,
        task_summary=str(task.get("summary", "")).strip(),
        task_goal=str(task.get("goal", "")).strip(),
        task_payload=dict(task_payload),
        raw_response=raw_text,
    )


def parse_desktop_plan_response(raw_text: str) -> Dict[str, Any]:
    parsed = extract_json_object(raw_text)
    if not isinstance(parsed, Mapping):
        raise ValueError("Desktop planner did not return a valid JSON object.")

    next_step = parsed.get("next_step")
    if next_step is None:
        next_actions = parsed.get("next_actions")
        if isinstance(next_actions, list) and next_actions:
            next_step = next_actions[0]

    if next_step is not None and not isinstance(next_step, Mapping):
        raise ValueError("Desktop planner field 'next_step' must be an object or null.")

    completed = bool(parsed.get("completed", next_step is None))
    assessment = str(parsed.get("assessment", "")).strip() or "No assessment provided."
    if completed:
        # A planner saying "done -- and optionally you could also X" is a
        # benign shape. Completion wins; failing an already-finished task on
        # this contradiction (or on a malformed trailing step) threw away
        # otherwise successful runs, so the extra step is not even normalized.
        if next_step is not None:
            assessment = (
                f"{assessment} (Planner also proposed a further step after "
                "reporting completion; it was ignored in favour of the "
                "completed state.)"
            )
        return {"assessment": assessment, "completed": True, "next_step": None}

    normalized_step = normalize_desktop_next_step(next_step) if next_step is not None else None
    return {
        "assessment": assessment,
        "completed": completed,
        "next_step": normalized_step,
    }


def parse_desktop_step_verification_response(raw_text: str) -> Dict[str, Any]:
    parsed = extract_json_object(raw_text)
    if not isinstance(parsed, Mapping):
        raise ValueError("Desktop step verifier did not return a valid JSON object.")

    blocker = parsed.get("blocker")
    blocker_text = str(blocker).strip() if blocker is not None else ""
    step_succeeded = bool(parsed.get("step_succeeded"))
    outcome_status = str(parsed.get("outcome_status") or parsed.get("status") or "").strip().lower()
    if outcome_status not in DESKTOP_STEP_OUTCOME_STATUSES:
        outcome_status = "succeeded" if step_succeeded else "blocked"
    if outcome_status == "succeeded":
        step_succeeded = True
    return {
        "assessment": str(parsed.get("assessment", "")).strip() or "No assessment provided.",
        "step_succeeded": step_succeeded,
        "outcome_status": outcome_status,
        "progress_visible": outcome_status in {"succeeded", "progressed"},
        "blocker": blocker_text or None,
    }


def parse_desktop_goal_verification_response(raw_text: str) -> Dict[str, Any]:
    parsed = extract_json_object(raw_text)
    if not isinstance(parsed, Mapping):
        raise ValueError("Desktop goal verifier did not return a valid JSON object.")

    blocker = parsed.get("blocker")
    blocker_text = str(blocker).strip() if blocker is not None else ""
    return {
        "assessment": str(parsed.get("assessment", "")).strip() or "No assessment provided.",
        "goal_completed": parsed.get("goal_completed") is True,
        "blocker": blocker_text or None,
    }


def normalize_desktop_next_step(next_step: Mapping[str, Any]) -> Dict[str, Any]:
    normalized: MutableMapping[str, Any] = dict(next_step)
    action_name = str(next_step.get("action") or next_step.get("type") or "").strip().lower()
    if not action_name:
        raise ValueError("Desktop planner next_step is missing an 'action' field.")

    if action_name == "double_click":
        action_name = "double_click_target" if has_target_fields(next_step) else "click"
        normalized.setdefault("clicks", 2)
    if action_name == "click" and has_target_fields(next_step) and not has_xy(next_step):
        action_name = "click_target"
    if action_name == "move" and has_target_fields(next_step) and not has_xy(next_step):
        action_name = "move_target"

    normalized["action"] = action_name
    expected_outcome = str(next_step.get("expected_outcome", "")).strip()
    if not expected_outcome:
        raise ValueError("Desktop planner next_step requires a non-empty expected_outcome.")
    normalized["expected_outcome"] = expected_outcome

    if action_name in {"click_target", "double_click_target", "move_target"}:
        target_description = str(next_step.get("target_description", "")).strip()
        target_text = str(next_step.get("target_text", "")).strip()
        if not target_description and not target_text:
            raise ValueError(
                f"Desktop planner target action '{action_name}' requires target_description or target_text."
            )
        normalized["target_description"] = target_description
        if target_text:
            normalized["target_text"] = target_text
        normalized["button"] = str(next_step.get("button") or "left")
        if action_name == "double_click_target":
            normalized["clicks"] = 2
            normalized["interval"] = optional_float(next_step.get("interval"), 0.15)
        else:
            normalized["clicks"] = optional_int(next_step.get("clicks"), 1)
            normalized["interval"] = optional_float(next_step.get("interval"), 0.0)
        return dict(normalized)

    if action_name == "click":
        normalized["x"] = required_int(next_step, "x")
        normalized["y"] = required_int(next_step, "y")
        normalized["button"] = str(next_step.get("button") or "left")
        normalized["clicks"] = optional_int(next_step.get("clicks"), 1)
        normalized["interval"] = optional_float(next_step.get("interval"), 0.0)
        return dict(normalized)

    if action_name == "move":
        normalized["x"] = required_int(next_step, "x")
        normalized["y"] = required_int(next_step, "y")
        normalized["duration"] = optional_float(next_step.get("duration"), 0.0)
        return dict(normalized)

    if action_name == "type":
        text = str(next_step.get("text", "")).strip()
        if not text:
            raise ValueError("Desktop planner type step requires non-empty text.")
        normalized["text"] = text
        normalized["interval"] = optional_float(next_step.get("interval"), 0.0)
        return dict(normalized)

    if action_name == "paste_text":
        text = str(next_step.get("text", ""))
        if not text:
            raise ValueError("Desktop planner paste_text step requires non-empty text.")
        normalized["text"] = text
        return dict(normalized)

    if action_name == "press":
        key = str(next_step.get("key", "")).strip()
        if not key:
            raise ValueError("Desktop planner press step requires a key.")
        normalized["key"] = key
        normalized["presses"] = optional_int(next_step.get("presses"), 1)
        normalized["interval"] = optional_float(next_step.get("interval"), 0.0)
        return dict(normalized)

    if action_name == "hotkey":
        keys = next_step.get("keys")
        if not isinstance(keys, list) or not keys:
            raise ValueError("Desktop planner hotkey step requires a non-empty keys list.")
        normalized["keys"] = [str(key) for key in keys if str(key).strip()]
        if not normalized["keys"]:
            raise ValueError("Desktop planner hotkey step requires at least one key.")
        normalized["interval"] = optional_float(next_step.get("interval"), 0.0)
        return dict(normalized)

    if action_name == "scroll":
        normalized["clicks"] = required_int(next_step, "clicks")
        if has_xy(next_step):
            normalized["x"] = required_int(next_step, "x")
            normalized["y"] = required_int(next_step, "y")
        return dict(normalized)

    if action_name == "wait":
        normalized["seconds"] = optional_float(next_step.get("seconds"), 1.0)
        return dict(normalized)

    raise ValueError(f"Unsupported desktop planner action '{action_name}'.")


def has_target_fields(payload: Mapping[str, Any]) -> bool:
    return bool(
        str(payload.get("target_description", "")).strip()
        or str(payload.get("target_text", "")).strip()
    )


def has_xy(payload: Mapping[str, Any]) -> bool:
    return payload.get("x") is not None and payload.get("y") is not None


def summarize_desktop_iteration(item: Mapping[str, Any]) -> Dict[str, Any]:
    summary = {
        "iteration": item.get("iteration"),
        "assessment": item.get("plan", {}).get("assessment")
        if isinstance(item.get("plan"), Mapping)
        else None,
        "completed": item.get("plan", {}).get("completed")
        if isinstance(item.get("plan"), Mapping)
        else None,
        "next_step": summarize_desktop_action(item.get("plan", {}).get("next_step"))
        if isinstance(item.get("plan"), Mapping)
        else None,
        "grounding": summarize_grounding_result(item.get("grounding")),
        "action": summarize_desktop_action(item.get("action")),
        "verification": summarize_desktop_step_verification(item.get("verification")),
        "goal_verification": summarize_desktop_goal_verification(item.get("goal_verification")),
        "failed_stage": item.get("failed_stage"),
        "halt_reason": item.get("halt_reason"),
    }
    return summary


def summarize_desktop_action(action: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(action, Mapping):
        return None

    summary: Dict[str, Any] = {
        "action": action.get("action"),
    }
    for key in (
        "target_description",
        "target_text",
        "x",
        "y",
        "button",
        "clicks",
        "interval",
        "text",
        "key",
        "keys",
        "seconds",
        "duration",
        "expected_outcome",
    ):
        if key in action:
            summary[key] = action.get(key)
    return summary


def summarize_grounding_result(grounding: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(grounding, Mapping):
        return None

    summary: Dict[str, Any] = {
        "located": grounding.get("located"),
    }
    for key in ("x", "y", "confidence", "reason"):
        if key in grounding:
            summary[key] = grounding.get(key)
    return summary


def summarize_desktop_step_verification(verification: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(verification, Mapping):
        return None

    summary: Dict[str, Any] = {
        "step_succeeded": verification.get("step_succeeded"),
    }
    for key in ("outcome_status", "progress_visible", "assessment", "blocker"):
        if key in verification:
            summary[key] = verification.get(key)
    return summary


def summarize_desktop_goal_verification(verification: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(verification, Mapping):
        return None

    summary: Dict[str, Any] = {
        "goal_completed": verification.get("goal_completed"),
    }
    for key in ("assessment", "blocker"):
        if key in verification:
            summary[key] = verification.get(key)
    return summary
