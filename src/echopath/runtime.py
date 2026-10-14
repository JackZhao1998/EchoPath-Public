from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Protocol, Tuple, runtime_checkable

from .models import AgentEvent, AgentTask, EventLevel, generate_id
from .run_state import checkpoint
from .router import ModelRequest, ModelResponse, ModelRouter
from .skill import SelectedSkill, SkillNotEquippedError, resolve_skill_selection


@runtime_checkable
class Skill(Protocol):
    name: str
    description: str

    async def run(self, payload: Mapping[str, Any], context: "AgentContext") -> Any:
        ...


@runtime_checkable
class MCPServer(Protocol):
    name: str

    async def call(self, method: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        ...


EventHandler = Callable[[AgentEvent], Optional[Awaitable[None]]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: Dict[str, List[EventHandler]] = {}

    def subscribe(self, event_name: str, handler: EventHandler) -> None:
        self._handlers.setdefault(event_name, []).append(handler)

    async def publish(self, event: AgentEvent) -> None:
        await checkpoint()
        handlers = self._handlers.get("*", []) + self._handlers.get(event.name, [])
        for handler in handlers:
            maybe_awaitable = handler(event)
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable


class CapabilityRegistry:
    def __init__(self) -> None:
        self._model_routers: Dict[str, ModelRouter] = {}
        self._skills: Dict[str, Skill] = {}
        self._mcp_servers: Dict[str, MCPServer] = {}
        self._services: Dict[str, Any] = {}

    def _register(
        self,
        bucket: Dict[str, Any],
        name: str,
        item: Any,
        kind: str,
        replace: bool = False,
    ) -> None:
        if not replace and name in bucket:
            raise ValueError(f"{kind} '{name}' is already registered.")
        bucket[name] = item

    def register_skill(self, skill: Skill, replace: bool = False) -> None:
        self._register(self._skills, skill.name, skill, "Skill", replace=replace)

    def register_model_router(self, router: ModelRouter, replace: bool = False) -> None:
        self._register(self._model_routers, router.name, router, "Model router", replace=replace)

    def register_mcp_server(self, server: MCPServer, replace: bool = False) -> None:
        self._register(self._mcp_servers, server.name, server, "MCP server", replace=replace)

    def register_service(self, name: str, service: Any, replace: bool = False) -> None:
        self._register(self._services, name, service, "Service", replace=replace)

    def get_skill(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def select_skill(self, name: str) -> Optional[SelectedSkill]:
        skill = self.get_skill(name)
        if skill is None:
            return None
        return resolve_skill_selection(skill)

    def get_model_router(self, name: str = "default") -> Optional[ModelRouter]:
        return self._model_routers.get(name)

    def get_mcp_server(self, name: str) -> Optional[MCPServer]:
        return self._mcp_servers.get(name)

    def get_service(self, name: str) -> Any:
        return self._services.get(name)

    def list_skills(self) -> List[str]:
        return sorted(self._skills)

    def list_model_routers(self) -> List[str]:
        return sorted(self._model_routers)

    def list_mcp_servers(self) -> List[str]:
        return sorted(self._mcp_servers)

    def list_services(self) -> List[str]:
        return sorted(self._services)


@dataclass
class AgentContext:
    task: AgentTask
    registry: CapabilityRegistry
    event_bus: EventBus
    working_directory: Path
    equipped_skill_names: Optional[Tuple[str, ...]] = None
    run_id: str = field(default_factory=lambda: generate_id("run"))
    state: Dict[str, Any] = field(default_factory=dict)
    events: List[AgentEvent] = field(default_factory=list)
    selected_skill: Optional[SelectedSkill] = None

    async def emit(
        self,
        name: str,
        message: str,
        payload: Optional[Mapping[str, Any]] = None,
        level: EventLevel = EventLevel.INFO,
    ) -> AgentEvent:
        event = AgentEvent(
            name=name,
            message=message,
            payload=dict(payload or {}),
            run_id=self.run_id,
            level=level,
        )
        self.events.append(event)
        await self.event_bus.publish(event)
        return event

    async def use_skill(self, name: str, payload: Optional[Mapping[str, Any]] = None) -> Any:
        selected_skill = await self.select_skill(name)
        skill = self.registry.get_skill(name)
        if skill is None:
            raise KeyError(f"Skill '{name}' is not registered.")
        await self.emit(
            "agent.skill.requested",
            f"Using skill '{name}'.",
            {"skill": name},
        )
        enriched_payload = dict(payload or {})
        enriched_payload.setdefault("selected_skill_name", selected_skill.name)
        enriched_payload.setdefault("selected_skill_description", selected_skill.description)
        enriched_payload.setdefault("selected_skill_markdown", selected_skill.markdown)
        return await skill.run(enriched_payload, self)

    async def select_skill(self, name: str) -> SelectedSkill:
        self._assert_skill_is_equipped(name)
        selected_skill = self.registry.select_skill(name)
        if selected_skill is None:
            raise KeyError(f"Skill '{name}' is not registered.")
        self.selected_skill = selected_skill
        self.state["selected_skill"] = selected_skill.to_state()
        await self.emit(
            "agent.skill.selected",
            f"Selected skill '{name}'.",
            {
                "skill": name,
                "markdown_path": (
                    str(selected_skill.markdown_path) if selected_skill.markdown_path else None
                ),
            },
        )
        return selected_skill

    def list_available_skills(self) -> List[str]:
        if self.equipped_skill_names is None:
            return self.registry.list_skills()
        return [
            name for name in self.equipped_skill_names if self.registry.get_skill(name) is not None
        ]

    def get_selected_skill(self) -> Optional[SelectedSkill]:
        return self.selected_skill

    def fork(
        self,
        *,
        task: AgentTask,
        share_state: bool = True,
        share_events: bool = True,
    ) -> "AgentContext":
        return AgentContext(
            task=task,
            registry=self.registry,
            event_bus=self.event_bus,
            working_directory=self.working_directory,
            equipped_skill_names=self.equipped_skill_names,
            run_id=self.run_id,
            state=self.state if share_state else dict(self.state),
            events=self.events if share_events else list(self.events),
            selected_skill=self.selected_skill,
        )

    def get_selected_skill_markdown(self) -> str:
        if self.selected_skill is None:
            return ""
        return self.selected_skill.markdown

    async def call_model(
        self,
        model_request: ModelRequest,
        router_name: str = "default",
    ) -> ModelResponse:
        router = self.registry.get_model_router(router_name)
        if router is None:
            raise KeyError(f"Model router '{router_name}' is not registered.")
        await self.emit(
            "agent.model.requested",
            f"Calling model router '{router_name}'.",
            {"router": router_name, "model": model_request.model},
        )
        response = await router.generate(model_request)
        await self.emit(
            "agent.model.completed",
            f"Model router '{router_name}' completed.",
            {
                "router": router_name,
                "model": response.model,
                "request_id": response.request_id,
            },
        )
        return response

    async def call_mcp(
        self,
        name: str,
        method: str,
        params: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        server = self.registry.get_mcp_server(name)
        if server is None:
            raise KeyError(f"MCP server '{name}' is not registered.")
        await self.emit(
            "agent.mcp.requested",
            f"Calling MCP server '{name}'.",
            {"server": name, "method": method},
        )
        return await server.call(method, params)

    def get_service(self, name: str) -> Any:
        return self.registry.get_service(name)

    def _assert_skill_is_equipped(self, name: str) -> None:
        if self.equipped_skill_names is None:
            return
        if name not in self.equipped_skill_names:
            equipped = ", ".join(self.equipped_skill_names) or "<none>"
            raise SkillNotEquippedError(
                f"Skill '{name}' is not equipped for this agent. Equipped skills: {equipped}."
            )
