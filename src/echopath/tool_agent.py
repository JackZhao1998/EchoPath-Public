from __future__ import annotations

from typing import Optional

from .agent import BaseAgent
from .execution_agent import (
    ExecutionAgent,
    build_execution_failure_output,
    coerce_task_record,
)
from .models import AgentTask
from .runtime import AgentContext


class ToolAgent(BaseAgent):
    def __init__(
        self,
        name: str,
        description: str = "",
        *,
        execution_agent: Optional[ExecutionAgent] = None,
        command_skill_name: str = "command-line",
        file_skill_name: str = "workspace-files",
        **kwargs,
    ) -> None:
        super().__init__(name=name, description=description, **kwargs)
        self.execution_agent = execution_agent or ExecutionAgent(
            name=f"{name}-execution",
            description="Executes tool-domain skills.",
            working_directory=self.working_directory,
            registry=self.registry,
            event_bus=self.event_bus,
            equipped_skills=self.equipped_skill_names,
        )
        self.command_skill_name = command_skill_name
        self.file_skill_name = file_skill_name

    async def plan(self, task: AgentTask, context: AgentContext):
        return {
            "goal": task.goal,
            "steps": [
                "Inspect the routed task record.",
                "Map the task type to the tool domain.",
                "Execute the matching skill or return a clear failure.",
            ],
        }

    async def execute(self, task: AgentTask, context: AgentContext):
        task_record = coerce_task_record(task.goal, task.payload)
        assistant_reply = str(task.payload.get("assistant_reply", "")).strip()
        skill_name = self._skill_name_for_task_type(task_record["type"])
        if skill_name is None:
            return build_execution_failure_output(
                assistant_reply=assistant_reply,
                task_record=task_record,
                error=f"Tool agent does not handle task type '{task_record['type']}'.",
            )

        execution_task = AgentTask(
            goal=task_record["goal"] or task.goal,
            payload={
                "assistant_reply": assistant_reply,
                "task": task_record,
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
            error=result.error or "Tool execution failed.",
        )

    def _skill_name_for_task_type(self, task_type: str) -> Optional[str]:
        if task_type == "command_task":
            return self.command_skill_name
        if task_type == "file_task":
            return self.file_skill_name
        return None
