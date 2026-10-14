from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Mapping, Optional, Union


if TYPE_CHECKING:
    from .runtime import AgentContext


class SkillError(RuntimeError):
    pass


class SkillNotEquippedError(SkillError):
    pass


SkillExecutor = Callable[[Mapping[str, Any], "AgentContext"], Any]


@dataclass(frozen=True)
class SelectedSkill:
    name: str
    description: str
    markdown: str = ""
    markdown_path: Optional[Path] = None

    def to_state(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "markdown": self.markdown,
            "markdown_path": str(self.markdown_path) if self.markdown_path else None,
        }

    def to_prompt_context(self) -> str:
        if self.markdown:
            return f"# Skill: {self.name}\n\n{self.markdown}".strip()
        return f"# Skill: {self.name}\n\n{self.description}".strip()


@dataclass
class MarkdownSkill:
    name: str
    description: str
    markdown_path: Path
    executor: Optional[SkillExecutor] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_directory(
        cls,
        directory: Union[str, Path],
        *,
        name: str,
        description: str,
        executor: Optional[SkillExecutor] = None,
        filename: Optional[str] = None,
    ) -> "MarkdownSkill":
        directory_path = Path(directory)
        if filename is None:
            for candidate in ("SKILL.md", "README.md"):
                candidate_path = directory_path / candidate
                if candidate_path.is_file():
                    filename = candidate
                    break
        if filename is None:
            raise FileNotFoundError(
                f"No skill markdown file found in '{directory_path}'. Expected SKILL.md or README.md."
            )
        return cls(
            name=name,
            description=description,
            markdown_path=directory_path / filename,
            executor=executor,
        )

    def load_markdown(self) -> str:
        return self.markdown_path.read_text(encoding="utf-8")

    async def run(self, payload: Mapping[str, Any], context: "AgentContext") -> Any:
        enriched_payload = dict(payload)
        enriched_payload.setdefault("selected_skill_name", self.name)
        enriched_payload.setdefault("selected_skill_description", self.description)
        enriched_payload.setdefault("selected_skill_markdown", self.load_markdown())

        if self.executor is None:
            return {
                "skill": self.name,
                "status": "selected",
                "message": "Skill markdown was loaded, but no executor is attached yet.",
                "metadata": dict(self.metadata),
            }

        result = self.executor(enriched_payload, context)
        if inspect.isawaitable(result):
            return await result
        return result


def resolve_skill_selection(skill: Any) -> SelectedSkill:
    name = getattr(skill, "name")
    description = getattr(skill, "description", "")

    markdown_path = _resolve_markdown_path(skill)
    if markdown_path is not None:
        markdown = markdown_path.read_text(encoding="utf-8")
        return SelectedSkill(
            name=name,
            description=description,
            markdown=markdown,
            markdown_path=markdown_path,
        )

    markdown = getattr(skill, "markdown", "")
    if not isinstance(markdown, str):
        markdown = ""

    return SelectedSkill(
        name=name,
        description=description,
        markdown=markdown,
    )


def _resolve_markdown_path(skill: Any) -> Optional[Path]:
    explicit_markdown_path = getattr(skill, "markdown_path", None)
    if explicit_markdown_path:
        markdown_path = Path(explicit_markdown_path)
        return markdown_path

    skill_directory = getattr(skill, "skill_directory", None)
    if skill_directory:
        directory_path = Path(skill_directory)
        for candidate in ("SKILL.md", "README.md"):
            candidate_path = directory_path / candidate
            if candidate_path.is_file():
                return candidate_path

    return None
