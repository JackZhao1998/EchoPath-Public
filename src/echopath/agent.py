from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .models import AgentResult, AgentStatus, AgentTask, EventLevel, utc_now
from .router import ModelRouter
from .runtime import AgentContext, CapabilityRegistry, EventBus, MCPServer, Skill


class AgentError(RuntimeError):
    pass


class AgentBusyError(AgentError):
    pass


class BaseAgent(ABC):
    def __init__(
        self,
        name: str,
        description: str = "",
        *,
        working_directory: Optional[Path] = None,
        registry: Optional[CapabilityRegistry] = None,
        event_bus: Optional[EventBus] = None,
        equipped_skills: Optional[Iterable[str]] = None,
    ) -> None:
        self.name = name
        self.description = description
        self.working_directory = Path(working_directory or Path.cwd())
        self.registry = registry or CapabilityRegistry()
        self.event_bus = event_bus or EventBus()
        self.status = AgentStatus.IDLE
        self.run_history: List[AgentResult] = []
        self.equipped_skill_names: Optional[Tuple[str, ...]] = None
        if equipped_skills is not None:
            self.equip_skills(equipped_skills)

    def make_context(self, task: AgentTask) -> AgentContext:
        return AgentContext(
            task=task,
            registry=self.registry,
            event_bus=self.event_bus,
            working_directory=self.working_directory,
            equipped_skill_names=self.equipped_skill_names,
        )

    def on(self, event_name: str, handler) -> None:
        self.event_bus.subscribe(event_name, handler)

    def register_skill(self, skill: Skill, replace: bool = False) -> None:
        self.registry.register_skill(skill, replace=replace)

    def equip_skills(self, skill_names: Iterable[str]) -> None:
        self.equipped_skill_names = tuple(dict.fromkeys(skill_names))

    def allow_all_skills(self) -> None:
        self.equipped_skill_names = None

    def list_equipped_skills(self) -> List[str]:
        if self.equipped_skill_names is None:
            return self.registry.list_skills()
        return list(self.equipped_skill_names)

    def register_model_router(self, router: ModelRouter, replace: bool = False) -> None:
        self.registry.register_model_router(router, replace=replace)

    def register_mcp_server(self, server: MCPServer, replace: bool = False) -> None:
        self.registry.register_mcp_server(server, replace=replace)

    def register_service(self, name: str, service: Any, replace: bool = False) -> None:
        self.registry.register_service(name, service, replace=replace)

    async def before_run(self, task: AgentTask, context: AgentContext) -> None:
        return None

    async def plan(self, task: AgentTask, context: AgentContext) -> Optional[Dict[str, Any]]:
        return None

    @abstractmethod
    async def execute(self, task: AgentTask, context: AgentContext) -> Any:
        raise NotImplementedError

    async def after_run(self, task: AgentTask, context: AgentContext, output: Any) -> None:
        return None

    async def on_failure(self, task: AgentTask, context: AgentContext, error: Exception) -> None:
        return None

    async def run(
        self,
        task: AgentTask,
        *,
        context: Optional[AgentContext] = None,
    ) -> AgentResult:
        if self.status == AgentStatus.RUNNING:
            raise AgentBusyError(f"Agent '{self.name}' is already running.")

        runtime = context or self.make_context(task)
        self.status = AgentStatus.RUNNING
        started_at = utc_now()

        try:
            await runtime.emit(
                "agent.run.started",
                f"Agent '{self.name}' started task '{task.goal}'.",
                {"agent": self.name, "task_id": task.task_id},
            )
            await self.before_run(task, runtime)

            plan = await self.plan(task, runtime)
            if plan is not None:
                runtime.state["plan"] = plan
                await runtime.emit(
                    "agent.plan.created",
                    "Execution plan created.",
                    {"plan": plan},
                )

            output = await self.execute(task, runtime)
            await runtime.emit(
                "agent.run.completed",
                f"Agent '{self.name}' completed task '{task.goal}'.",
                {"agent": self.name, "task_id": task.task_id},
            )
            await self.after_run(task, runtime, output)
            result = AgentResult(
                agent_name=self.name,
                task_id=task.task_id,
                success=True,
                output=output,
                state=dict(runtime.state),
                events=tuple(runtime.events),
                started_at=started_at,
                finished_at=utc_now(),
            )
        except Exception as error:  # pragma: no cover - exercised by tests
            await runtime.emit(
                "agent.run.failed",
                f"Agent '{self.name}' failed task '{task.goal}'.",
                {"agent": self.name, "task_id": task.task_id, "error": str(error)},
                level=EventLevel.ERROR,
            )
            await self.on_failure(task, runtime, error)
            result = AgentResult(
                agent_name=self.name,
                task_id=task.task_id,
                success=False,
                error=str(error),
                state=dict(runtime.state),
                events=tuple(runtime.events),
                started_at=started_at,
                finished_at=utc_now(),
            )
        finally:
            self.status = AgentStatus.IDLE

        self.run_history.append(result)
        return result

    def run_sync(
        self,
        task: AgentTask,
        *,
        context: Optional[AgentContext] = None,
    ) -> AgentResult:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.run(task, context=context))
        raise AgentError("run_sync cannot be called from an active event loop.")
