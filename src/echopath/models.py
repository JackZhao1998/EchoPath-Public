from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional, Tuple
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def generate_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class AgentStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"


class EventLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class AgentTask:
    goal: str
    payload: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    task_id: str = field(default_factory=lambda: generate_id("task"))
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class AgentEvent:
    name: str
    message: str
    payload: Dict[str, Any] = field(default_factory=dict)
    run_id: str = ""
    level: EventLevel = EventLevel.INFO
    timestamp: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class AgentResult:
    agent_name: str
    task_id: str
    success: bool
    output: Any = None
    error: Optional[str] = None
    state: Dict[str, Any] = field(default_factory=dict)
    events: Tuple[AgentEvent, ...] = field(default_factory=tuple)
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime = field(default_factory=utc_now)

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()
