from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from .router import (
    ClaudeCodeRouter,
    CodexSessionRouter,
    EnvironmentOpenAICredentialsProvider,
    ExternalCommandRouter,
    FileOpenAICredentialsProvider,
    KimiCodeRouter,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    OpenAIResponsesRouter,
    RouterError,
    RouterRequestError,
)


class ConfigError(RuntimeError):
    pass


SUPPORTED_AGENT_SKILLS = (
    "pyautogui-desktop",
    "command-line",
    "workspace-files",
)
SUPPORTED_DESKTOP_PROVIDERS = ("local", "amazon-vm", "local-vm")
REMOTE_DESKTOP_PROVIDERS = frozenset({"amazon-vm", "local-vm"})


def is_remote_desktop_provider(provider: str) -> bool:
    return str(provider or "").strip().lower() in REMOTE_DESKTOP_PROVIDERS


DEFAULT_GLOBAL_MODEL_ROUTER_CONFIG_PAYLOAD = {
    "model_routing": {
        "mode": "external_command",
        "router_name": "default",
        "external_model": "default",
    },
    "external_command": {
        "command": [],
        "working_directory": ".",
        "timeout_seconds": 300,
        "env": {},
    },
    "desktop": {
        "provider": "local",
        "reasoning_effort": "medium",
        "max_iterations": 100,
        "pause_seconds": 0.1,
        "fail_safe": True,
        "screenshot_directory": "local_data/desktop_observations",
        "amazon_vm_endpoint": "",
        "amazon_vm_api_key_env": "ECHOPATH_AMAZON_VM_API_KEY",
        "amazon_vm_timeout_seconds": 30.0,
        "replay_fallback_to_planning": True,
        "replay_verify_goal": True,
        "replay_decision_model": False,
        "replay_decision_model_api_key_env": "OPENROUTER_API_KEY",
        "replay_decision_model_min_confidence": 0.9,
        "vm_record_runs": False,
        "vm_recording_directory": "local_data/recordings",
        "vm_recording_max_seconds": 3600,
        "vm_recording_preset": "Preset1920x1080",
    },
    "skills": {
        "available": list(SUPPORTED_AGENT_SKILLS),
        "enabled": list(SUPPORTED_AGENT_SKILLS),
    },
}


@dataclass(frozen=True)
class OpenAIModelConfig:
    auth_source: str = "environment"
    credentials_path: Path = Path("credentials/openai.json")
    credential_name: str = "openAI"
    fallback_credential_names: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CodexModelConfig:
    working_directory: Path = Path(".")
    timeout_seconds: int = 300
    skip_git_repo_check: bool = True
    sandbox_mode: str = "danger-full-access"
    approval_policy: str = "never"
    network_access_enabled: bool = True
    web_search_mode: str = "disabled"
    additional_directories: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ClaudeCodeConfig:
    executable: str = "claude"
    working_directory: Path = Path(".")
    timeout_seconds: int = 300
    permission_mode: str = "dontAsk"
    tools: Tuple[str, ...] = ("Read",)
    allowed_tools: Tuple[str, ...] = ("Read",)
    additional_directories: Tuple[str, ...] = ()
    max_turns: Optional[int] = 8
    bare: bool = False
    no_session_persistence: bool = True


@dataclass(frozen=True)
class KimiCodeConfig:
    executable: str = "kimi"
    working_directory: Path = Path(".")
    timeout_seconds: int = 300
    additional_directories: Tuple[str, ...] = ()
    agent_file: Optional[Path] = None


@dataclass(frozen=True)
class ExternalCommandConfig:
    command: Tuple[str, ...] = ()
    working_directory: Path = Path(".")
    timeout_seconds: int = 300
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DesktopAutomationConfig:
    provider: str = "local"
    reasoning_effort: str = "medium"
    max_iterations: int = 100
    pause_seconds: float = 0.1
    fail_safe: bool = True
    screenshot_directory: Path = Path("local_data/desktop_observations")
    amazon_vm_endpoint: str = ""
    amazon_vm_api_key_env: str = "ECHOPATH_AMAZON_VM_API_KEY"
    amazon_vm_timeout_seconds: float = 30.0
    # When a learned path's step can't be found on screen, plan the rest live
    # instead of failing the run. False keeps replays strict.
    replay_fallback_to_planning: bool = True
    # One model call after a replay to confirm the goal is on screen; if not, plan the rest live.
    replay_verify_goal: bool = True
    # OCR + Jev decision model before the vision model when a replayed click's
    # saved crop misses. Sends on-screen text to OpenRouter, so it is opt-in.
    replay_decision_model: bool = False
    replay_decision_model_api_key_env: str = "OPENROUTER_API_KEY"
    replay_decision_model_min_confidence: float = 0.9
    # Record the guest screen for each VM run and save it here as <run id>.mp4.
    vm_record_runs: bool = False
    vm_recording_directory: Path = Path("local_data/recordings")
    vm_recording_max_seconds: float = 3600.0
    # avconvert preset for the saved MP4; PresetHighestQuality keeps full Retina size.
    vm_recording_preset: str = "Preset1920x1080"


@dataclass(frozen=True)
class SkillsConfig:
    available: Tuple[str, ...] = SUPPORTED_AGENT_SKILLS
    enabled: Tuple[str, ...] = SUPPORTED_AGENT_SKILLS


@dataclass(frozen=True)
class GlobalModelRouterConfig:
    source_path: Path
    mode: str = "external_command"
    router_name: str = "default"
    external_model: str = "default"
    api_model: str = "gpt-5.5"
    codex_model: str = "gpt-5.5"
    claude_model: str = "sonnet"
    kimi_model: str = "kimi-code/kimi-for-coding"
    openai: OpenAIModelConfig = field(default_factory=OpenAIModelConfig)
    codex: CodexModelConfig = field(default_factory=CodexModelConfig)
    claude: ClaudeCodeConfig = field(default_factory=ClaudeCodeConfig)
    kimi: KimiCodeConfig = field(default_factory=KimiCodeConfig)
    external_command: ExternalCommandConfig = field(default_factory=ExternalCommandConfig)
    desktop: DesktopAutomationConfig = field(default_factory=DesktopAutomationConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    vm: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class ConfiguredModelRouter:
    config: GlobalModelRouterConfig
    api_router: Optional[ModelRouter] = None
    codex_router: Optional[ModelRouter] = None
    claude_router: Optional[ModelRouter] = None
    kimi_router: Optional[ModelRouter] = None
    external_command_router: Optional[ModelRouter] = None
    name: str = "default"

    async def generate(self, model_request: ModelRequest) -> ModelResponse:
        mode = self.config.mode
        if mode == "api":
            return await self._generate_with_api(model_request)
        if mode == "codex":
            return await self._generate_with_codex(model_request)
        if mode == "claude":
            return await self._generate_with_claude(model_request)
        if mode == "kimi":
            return await self._generate_with_kimi(model_request)
        if mode == "external_command":
            return await self._generate_with_external_command(model_request)
        if mode == "hybrid":
            return await self._generate_hybrid(model_request)
        raise ConfigError(f"Unsupported routing mode '{mode}'.")

    async def _generate_with_api(self, model_request: ModelRequest) -> ModelResponse:
        if self.api_router is None:
            raise ConfigError("API mode is enabled, but no API router is configured.")
        return await self.api_router.generate(
            _replace_model(model_request, self.config.api_model)
        )

    async def _generate_with_codex(self, model_request: ModelRequest) -> ModelResponse:
        if self.codex_router is None:
            raise ConfigError("Codex mode is enabled, but no Codex router is configured.")
        return await self.codex_router.generate(
            _replace_model(model_request, self.config.codex_model)
        )

    async def _generate_with_claude(self, model_request: ModelRequest) -> ModelResponse:
        if self.claude_router is None:
            raise ConfigError("Claude mode is enabled, but no Claude Code router is configured.")
        return await self.claude_router.generate(
            _replace_model(model_request, self.config.claude_model)
        )

    async def _generate_with_kimi(self, model_request: ModelRequest) -> ModelResponse:
        if self.kimi_router is None:
            raise ConfigError("Kimi mode is enabled, but no Kimi Code router is configured.")
        return await self.kimi_router.generate(
            _replace_model(model_request, self.config.kimi_model)
        )

    async def _generate_with_external_command(self, model_request: ModelRequest) -> ModelResponse:
        if self.external_command_router is None:
            raise ConfigError(
                "External command mode is enabled, but no external command router is configured."
            )
        return await self.external_command_router.generate(
            _replace_model(model_request, self.config.external_model)
        )

    async def _generate_hybrid(self, model_request: ModelRequest) -> ModelResponse:
        codex_error = None
        try:
            return await self._generate_with_codex(model_request)
        except RouterError as error:
            codex_error = str(error)

        try:
            return await self._generate_with_api(model_request)
        except RouterError as error:
            message = (
                "Hybrid routing failed. "
                f"codex: {codex_error or 'unknown error'} | api: {error}"
            )
            raise RouterRequestError(message) from error


def load_global_model_router_config(
    path: Optional[Path] = None,
) -> GlobalModelRouterConfig:
    config_path = Path(path or "echopath.config.json").resolve()
    raw_payload = read_global_model_router_config_payload(config_path)
    return parse_global_model_router_config_payload(raw_payload, source_path=config_path)


def read_global_model_router_config_payload(
    path: Optional[Path] = None,
) -> dict[str, Any]:
    config_path = Path(path or "echopath.config.json").resolve()
    try:
        raw_payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Global config file was not found at '{config_path}'.") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Global config file '{config_path}' contains invalid JSON.") from exc

    if not isinstance(raw_payload, dict):
        raise ConfigError(f"Global config file '{config_path}' must contain a JSON object.")
    return dict(raw_payload)


def write_global_model_router_config_payload(
    payload: Mapping[str, Any],
    path: Optional[Path] = None,
) -> dict[str, Any]:
    config_path = Path(path or "echopath.config.json").resolve()
    parse_global_model_router_config_payload(payload, source_path=config_path)
    normalized_payload = json.loads(json.dumps(payload))
    config_path.write_text(json.dumps(normalized_payload, indent=2) + "\n", encoding="utf-8")
    return normalized_payload


def parse_global_model_router_config_payload(
    raw_payload: Mapping[str, Any],
    *,
    source_path: Optional[Path] = None,
) -> GlobalModelRouterConfig:
    config_path = Path(source_path or "echopath.config.json").resolve()
    if not isinstance(raw_payload, Mapping):
        raise ConfigError(f"Global config file '{config_path}' must contain a JSON object.")

    routing = raw_payload.get("model_routing", {})
    openai = raw_payload.get("openai", {})
    codex = raw_payload.get("codex", {})
    claude = raw_payload.get("claude", {})
    kimi = raw_payload.get("kimi", {})
    external_command = raw_payload.get("external_command", {})
    desktop = raw_payload.get("desktop", {})
    skills = raw_payload.get("skills", {})

    if not isinstance(routing, dict):
        raise ConfigError("'model_routing' must be a JSON object.")
    if not isinstance(openai, dict):
        raise ConfigError("'openai' must be a JSON object.")
    if not isinstance(codex, dict):
        raise ConfigError("'codex' must be a JSON object.")
    if not isinstance(claude, dict):
        raise ConfigError("'claude' must be a JSON object.")
    if not isinstance(kimi, dict):
        raise ConfigError("'kimi' must be a JSON object.")
    if not isinstance(external_command, dict):
        raise ConfigError("'external_command' must be a JSON object.")
    if not isinstance(desktop, dict):
        raise ConfigError("'desktop' must be a JSON object.")
    if not isinstance(skills, dict):
        raise ConfigError("'skills' must be a JSON object.")

    mode = str(routing.get("mode", "external_command")).strip().lower()
    if mode not in {"api", "codex", "claude", "kimi", "hybrid", "external_command"}:
        raise ConfigError(
            "model_routing.mode must be one of: api, codex, claude, kimi, hybrid, "
            "external_command."
        )

    router_name = str(routing.get("router_name", "default")).strip() or "default"
    external_model = str(routing.get("external_model", "default")).strip() or "default"
    api_model = str(routing.get("api_model", "gpt-5.5")).strip() or "gpt-5.5"
    codex_model = str(routing.get("codex_model", "gpt-5.5")).strip() or "gpt-5.5"
    claude_model = str(routing.get("claude_model", "sonnet")).strip() or "sonnet"
    kimi_model = str(routing.get("kimi_model", "kimi-code/kimi-for-coding")).strip() or "kimi-code/kimi-for-coding"

    auth_source = str(openai.get("auth_source", "environment")).strip().lower() or "environment"
    if auth_source not in {"file", "environment"}:
        raise ConfigError("openai.auth_source must be either 'file' or 'environment'.")

    credentials_path = _resolve_path(
        config_path.parent,
        openai.get("credentials_path", "credentials/openai.json"),
    )
    credential_name = str(openai.get("credential_name", "openAI")).strip() or "openAI"
    fallback_credential_names = _coerce_str_tuple(openai.get("fallback_credential_names", ()))

    working_directory = _resolve_path(
        config_path.parent,
        codex.get("working_directory", "."),
    )
    try:
        codex_timeout_seconds = int(codex.get("timeout_seconds", 300))
    except (TypeError, ValueError) as exc:
        raise ConfigError("codex.timeout_seconds must be a whole number.") from exc
    if codex_timeout_seconds <= 0:
        raise ConfigError("codex.timeout_seconds must be greater than 0.")
    additional_directories = tuple(
        str(_resolve_path(config_path.parent, value))
        for value in _coerce_str_tuple(codex.get("additional_directories", ()))
    )

    claude_executable = str(claude.get("executable", "claude")).strip() or "claude"
    claude_working_directory = _resolve_path(
        config_path.parent,
        claude.get("working_directory", "."),
    )
    try:
        claude_timeout_seconds = int(claude.get("timeout_seconds", 300))
    except (TypeError, ValueError) as exc:
        raise ConfigError("claude.timeout_seconds must be a whole number.") from exc
    if claude_timeout_seconds <= 0:
        raise ConfigError("claude.timeout_seconds must be greater than 0.")
    claude_permission_mode = str(claude.get("permission_mode", "dontAsk")).strip() or "dontAsk"
    claude_tools = _coerce_str_tuple(claude.get("tools", ("Read",)))
    claude_allowed_tools = _coerce_str_tuple(claude.get("allowed_tools", ("Read",)))
    claude_additional_directories = tuple(
        str(_resolve_path(config_path.parent, value))
        for value in _coerce_str_tuple(claude.get("additional_directories", ()))
    )
    raw_claude_max_turns = claude.get("max_turns", 8)
    claude_max_turns: Optional[int]
    if raw_claude_max_turns is None:
        claude_max_turns = None
    else:
        try:
            claude_max_turns = int(raw_claude_max_turns)
        except (TypeError, ValueError) as exc:
            raise ConfigError("claude.max_turns must be a whole number or null.") from exc
        if claude_max_turns <= 0:
            raise ConfigError("claude.max_turns must be greater than 0 when set.")

    kimi_executable = str(kimi.get("executable", "kimi")).strip() or "kimi"
    kimi_working_directory = _resolve_path(
        config_path.parent,
        kimi.get("working_directory", "."),
    )
    try:
        kimi_timeout_seconds = int(kimi.get("timeout_seconds", 300))
    except (TypeError, ValueError) as exc:
        raise ConfigError("kimi.timeout_seconds must be a whole number.") from exc
    if kimi_timeout_seconds <= 0:
        raise ConfigError("kimi.timeout_seconds must be greater than 0.")
    kimi_additional_directories = tuple(
        str(_resolve_path(config_path.parent, value))
        for value in _coerce_str_tuple(kimi.get("additional_directories", ()))
    )
    raw_kimi_agent_file = str(kimi.get("agent_file") or "").strip()
    kimi_agent_file = (
        _resolve_path(config_path.parent, raw_kimi_agent_file)
        if raw_kimi_agent_file
        else None
    )

    external_command_value = _coerce_command_tuple(external_command.get("command", ()))
    external_command_working_directory = _resolve_path(
        config_path.parent,
        external_command.get("working_directory", "."),
    )
    try:
        external_command_timeout_seconds = int(external_command.get("timeout_seconds", 300))
    except (TypeError, ValueError) as exc:
        raise ConfigError("external_command.timeout_seconds must be a whole number.") from exc
    if external_command_timeout_seconds <= 0:
        raise ConfigError("external_command.timeout_seconds must be greater than 0.")
    external_command_env = _coerce_str_dict(
        external_command.get("env", {}),
        field_name="external_command.env",
    )

    try:
        desktop_pause_seconds = float(desktop.get("pause_seconds", 0.1))
    except (TypeError, ValueError) as exc:
        raise ConfigError("desktop.pause_seconds must be a number.") from exc
    if desktop_pause_seconds < 0:
        raise ConfigError("desktop.pause_seconds must be greater than or equal to 0.")

    desktop_provider = str(desktop.get("provider", "local")).strip().lower() or "local"
    if desktop_provider not in SUPPORTED_DESKTOP_PROVIDERS:
        raise ConfigError(
            "desktop.provider must be one of: "
            f"{', '.join(SUPPORTED_DESKTOP_PROVIDERS)}."
        )

    desktop_reasoning_effort = _normalize_reasoning_effort(
        desktop.get("reasoning_effort", "medium"),
        field_name="desktop.reasoning_effort",
    )
    try:
        desktop_max_iterations = int(desktop.get("max_iterations", 100))
    except (TypeError, ValueError) as exc:
        raise ConfigError("desktop.max_iterations must be a whole number.") from exc
    if desktop_max_iterations <= 0:
        raise ConfigError("desktop.max_iterations must be greater than 0.")

    amazon_vm_endpoint = str(desktop.get("amazon_vm_endpoint", "")).strip()
    amazon_vm_api_key_env = (
        str(desktop.get("amazon_vm_api_key_env", "ECHOPATH_AMAZON_VM_API_KEY")).strip()
        or "ECHOPATH_AMAZON_VM_API_KEY"
    )
    try:
        amazon_vm_timeout_seconds = float(desktop.get("amazon_vm_timeout_seconds", 30.0))
    except (TypeError, ValueError) as exc:
        raise ConfigError("desktop.amazon_vm_timeout_seconds must be a number.") from exc
    if amazon_vm_timeout_seconds <= 0:
        raise ConfigError("desktop.amazon_vm_timeout_seconds must be greater than 0.")
    replay_fallback_to_planning = desktop.get("replay_fallback_to_planning", True)
    if not isinstance(replay_fallback_to_planning, bool):
        raise ConfigError("desktop.replay_fallback_to_planning must be true or false.")
    replay_verify_goal = desktop.get("replay_verify_goal", True)
    if not isinstance(replay_verify_goal, bool):
        raise ConfigError("desktop.replay_verify_goal must be true or false.")
    replay_decision_model = desktop.get("replay_decision_model", False)
    if not isinstance(replay_decision_model, bool):
        raise ConfigError("desktop.replay_decision_model must be true or false.")
    replay_decision_model_api_key_env = (
        str(desktop.get("replay_decision_model_api_key_env", "OPENROUTER_API_KEY")).strip()
        or "OPENROUTER_API_KEY"
    )
    try:
        replay_decision_model_min_confidence = float(desktop.get("replay_decision_model_min_confidence", 0.9))
    except (TypeError, ValueError) as exc:
        raise ConfigError("desktop.replay_decision_model_min_confidence must be a number.") from exc
    if not 0 < replay_decision_model_min_confidence <= 1:
        raise ConfigError("desktop.replay_decision_model_min_confidence must be between 0 and 1.")
    vm_record_runs = desktop.get("vm_record_runs", False)
    if not isinstance(vm_record_runs, bool):
        raise ConfigError("desktop.vm_record_runs must be true or false.")
    vm_recording_directory = Path(str(desktop.get("vm_recording_directory", "local_data/recordings")).strip() or "local_data/recordings")
    try:
        vm_recording_max_seconds = float(desktop.get("vm_recording_max_seconds", 3600))
    except (TypeError, ValueError) as exc:
        raise ConfigError("desktop.vm_recording_max_seconds must be a number.") from exc
    if vm_recording_max_seconds < 10:
        raise ConfigError("desktop.vm_recording_max_seconds must be at least 10.")
    vm_recording_preset = str(desktop.get("vm_recording_preset", "Preset1920x1080")).strip() or "Preset1920x1080"
    if not vm_recording_preset.replace("_", "").isalnum():
        raise ConfigError("desktop.vm_recording_preset must be an avconvert preset name.")
    if is_remote_desktop_provider(desktop_provider) and not amazon_vm_endpoint:
        raise ConfigError(
            "desktop.amazon_vm_endpoint must be set when desktop.provider is "
            f"'{desktop_provider}'."
        )

    desktop_screenshot_directory = _resolve_path(
        config_path.parent,
        desktop.get("screenshot_directory", "local_data/desktop_observations"),
    )

    available_skills = _normalize_skill_names(
        skills.get("available", SUPPORTED_AGENT_SKILLS),
        field_name="skills.available",
    )
    if not available_skills:
        raise ConfigError("skills.available must include at least one supported skill.")

    enabled_skills = _normalize_skill_names(
        skills.get("enabled", available_skills),
        field_name="skills.enabled",
    )
    missing_enabled_skills = tuple(
        name for name in enabled_skills if name not in available_skills
    )
    if missing_enabled_skills:
        raise ConfigError(
            "skills.enabled must be a subset of skills.available. "
            f"Unexpected: {', '.join(missing_enabled_skills)}."
        )

    from .vm_setup import VMSetupError, normalize_vm
    try:
        vm = normalize_vm(raw_payload.get('vm')) if 'vm' in raw_payload else {}
    except VMSetupError as error:
        raise ConfigError(str(error)) from error
    return GlobalModelRouterConfig(
        vm=vm,
        source_path=config_path,
        mode=mode,
        router_name=router_name,
        external_model=external_model,
        api_model=api_model,
        codex_model=codex_model,
        claude_model=claude_model,
        kimi_model=kimi_model,
        openai=OpenAIModelConfig(
            auth_source=auth_source,
            credentials_path=credentials_path,
            credential_name=credential_name,
            fallback_credential_names=fallback_credential_names,
        ),
        codex=CodexModelConfig(
            working_directory=working_directory,
            timeout_seconds=codex_timeout_seconds,
            skip_git_repo_check=bool(codex.get("skip_git_repo_check", True)),
            sandbox_mode=str(codex.get("sandbox_mode", "danger-full-access")),
            approval_policy=str(codex.get("approval_policy", "never")),
            network_access_enabled=bool(codex.get("network_access_enabled", True)),
            web_search_mode=str(codex.get("web_search_mode", "disabled")),
            additional_directories=additional_directories,
        ),
        claude=ClaudeCodeConfig(
            executable=claude_executable,
            working_directory=claude_working_directory,
            timeout_seconds=claude_timeout_seconds,
            permission_mode=claude_permission_mode,
            tools=claude_tools,
            allowed_tools=claude_allowed_tools,
            additional_directories=claude_additional_directories,
            max_turns=claude_max_turns,
            bare=bool(claude.get("bare", False)),
            no_session_persistence=bool(claude.get("no_session_persistence", True)),
        ),
        kimi=KimiCodeConfig(
            executable=kimi_executable,
            working_directory=kimi_working_directory,
            timeout_seconds=kimi_timeout_seconds,
            additional_directories=kimi_additional_directories,
            agent_file=kimi_agent_file,
        ),
        external_command=ExternalCommandConfig(
            command=external_command_value,
            working_directory=external_command_working_directory,
            timeout_seconds=external_command_timeout_seconds,
            env=external_command_env,
        ),
        desktop=DesktopAutomationConfig(
            provider=desktop_provider,
            reasoning_effort=desktop_reasoning_effort,
            max_iterations=desktop_max_iterations,
            pause_seconds=desktop_pause_seconds,
            fail_safe=bool(desktop.get("fail_safe", True)),
            screenshot_directory=desktop_screenshot_directory,
            amazon_vm_endpoint=amazon_vm_endpoint,
            amazon_vm_api_key_env=amazon_vm_api_key_env,
            amazon_vm_timeout_seconds=amazon_vm_timeout_seconds,
            replay_fallback_to_planning=replay_fallback_to_planning,
            replay_verify_goal=replay_verify_goal,
            replay_decision_model=replay_decision_model,
            replay_decision_model_api_key_env=replay_decision_model_api_key_env,
            replay_decision_model_min_confidence=replay_decision_model_min_confidence,
            vm_record_runs=vm_record_runs,
            vm_recording_directory=vm_recording_directory,
            vm_recording_max_seconds=vm_recording_max_seconds,
            vm_recording_preset=vm_recording_preset,
        ),
        skills=SkillsConfig(
            available=available_skills,
            enabled=enabled_skills,
        ),
    )


def default_global_model_router_config_payload() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_GLOBAL_MODEL_ROUTER_CONFIG_PAYLOAD))


def serialize_global_model_router_config(config: GlobalModelRouterConfig) -> dict[str, Any]:
    config_root = config.source_path.parent
    routing: dict[str, Any] = {
        "mode": config.mode,
        "router_name": config.router_name,
    }
    if config.mode == "external_command":
        routing["external_model"] = config.external_model
    if config.mode in {"api", "hybrid"}:
        routing["api_model"] = config.api_model
    if config.mode in {"codex", "hybrid"}:
        routing["codex_model"] = config.codex_model
    if config.mode == "claude":
        routing["claude_model"] = config.claude_model
    if config.mode == "kimi":
        routing["kimi_model"] = config.kimi_model

    payload: dict[str, Any] = {
        "source_path": str(config.source_path),
        "model_routing": routing,
        "desktop": {
            "provider": config.desktop.provider,
            "reasoning_effort": config.desktop.reasoning_effort,
            "max_iterations": config.desktop.max_iterations,
            "pause_seconds": config.desktop.pause_seconds,
            "fail_safe": config.desktop.fail_safe,
            "screenshot_directory": _format_config_path(
                config_root,
                config.desktop.screenshot_directory,
            ),
            "amazon_vm_endpoint": config.desktop.amazon_vm_endpoint,
            "amazon_vm_api_key_env": config.desktop.amazon_vm_api_key_env,
            "amazon_vm_timeout_seconds": config.desktop.amazon_vm_timeout_seconds,
            "replay_fallback_to_planning": config.desktop.replay_fallback_to_planning,
            "replay_verify_goal": config.desktop.replay_verify_goal,
            "replay_decision_model": config.desktop.replay_decision_model,
            "replay_decision_model_api_key_env": config.desktop.replay_decision_model_api_key_env,
            "replay_decision_model_min_confidence": config.desktop.replay_decision_model_min_confidence,
            "vm_record_runs": config.desktop.vm_record_runs,
            "vm_recording_directory": str(config.desktop.vm_recording_directory),
            "vm_recording_max_seconds": config.desktop.vm_recording_max_seconds,
            "vm_recording_preset": config.desktop.vm_recording_preset,
        },
        "skills": {
            "available": list(config.skills.available),
            "enabled": list(config.skills.enabled),
        },
    }

    if config.vm:
        payload['vm'] = dict(config.vm)
    if config.mode in {"api", "hybrid"}:
        payload["openai"] = {
            "auth_source": config.openai.auth_source,
            "credentials_path": _format_config_path(
                config_root,
                config.openai.credentials_path,
            ),
            "credential_name": config.openai.credential_name,
            "fallback_credential_names": list(config.openai.fallback_credential_names),
        }

    if config.mode in {"codex", "hybrid"}:
        payload["codex"] = {
            "working_directory": _format_config_path(
                config_root,
                config.codex.working_directory,
            ),
            "timeout_seconds": config.codex.timeout_seconds,
            "skip_git_repo_check": config.codex.skip_git_repo_check,
            "sandbox_mode": config.codex.sandbox_mode,
            "approval_policy": config.codex.approval_policy,
            "network_access_enabled": config.codex.network_access_enabled,
            "web_search_mode": config.codex.web_search_mode,
            "additional_directories": list(config.codex.additional_directories),
        }

    if config.mode == "claude":
        payload["claude"] = {
            "executable": config.claude.executable,
            "working_directory": _format_config_path(
                config_root,
                config.claude.working_directory,
            ),
            "timeout_seconds": config.claude.timeout_seconds,
            "permission_mode": config.claude.permission_mode,
            "tools": list(config.claude.tools),
            "allowed_tools": list(config.claude.allowed_tools),
            "additional_directories": list(config.claude.additional_directories),
            "max_turns": config.claude.max_turns,
            "bare": config.claude.bare,
            "no_session_persistence": config.claude.no_session_persistence,
        }

    if config.mode == "kimi":
        payload["kimi"] = {
            "executable": config.kimi.executable,
            "working_directory": _format_config_path(
                config_root,
                config.kimi.working_directory,
            ),
            "timeout_seconds": config.kimi.timeout_seconds,
            "additional_directories": list(config.kimi.additional_directories),
            "agent_file": (
                _format_config_path(config_root, config.kimi.agent_file)
                if config.kimi.agent_file
                else None
            ),
        }

    if config.mode == "external_command":
        payload["external_command"] = {
            "command": list(config.external_command.command),
            "working_directory": _format_config_path(
                config_root,
                config.external_command.working_directory,
            ),
            "timeout_seconds": config.external_command.timeout_seconds,
            "env": dict(config.external_command.env),
        }

    return payload


def build_model_router_from_global_config(
    config_or_path: Optional[GlobalModelRouterConfig | Path] = None,
) -> ConfiguredModelRouter:
    config = (
        config_or_path
        if isinstance(config_or_path, GlobalModelRouterConfig)
        else load_global_model_router_config(config_or_path)
    )

    api_router: Optional[ModelRouter] = None
    codex_router: Optional[ModelRouter] = None
    claude_router: Optional[ModelRouter] = None
    kimi_router: Optional[ModelRouter] = None
    external_command_router: Optional[ModelRouter] = None

    if config.mode in {"api", "hybrid"}:
        credentials_provider = _build_openai_credentials_provider(config.openai)
        fallback_credentials_providers = ()
        if config.openai.auth_source == "file":
            fallback_credentials_providers = tuple(
                FileOpenAICredentialsProvider(
                    path=config.openai.credentials_path,
                    credential_name=name,
                )
                for name in config.openai.fallback_credential_names
            )
        api_router = OpenAIResponsesRouter(
            name=f"{config.router_name}-api",
            credentials_provider=credentials_provider,
            fallback_credentials_providers=fallback_credentials_providers,
        )

    if config.mode in {"codex", "hybrid"}:
        codex_router = CodexSessionRouter(
            name=f"{config.router_name}-codex",
            working_directory=config.codex.working_directory,
            timeout_seconds=config.codex.timeout_seconds,
            skip_git_repo_check=config.codex.skip_git_repo_check,
            sandbox_mode=config.codex.sandbox_mode,
            approval_policy=config.codex.approval_policy,
            network_access_enabled=config.codex.network_access_enabled,
            web_search_mode=config.codex.web_search_mode,
            additional_directories=config.codex.additional_directories,
        )

    if config.mode == "claude":
        claude_router = ClaudeCodeRouter(
            name=f"{config.router_name}-claude",
            executable=config.claude.executable,
            working_directory=config.claude.working_directory,
            timeout_seconds=config.claude.timeout_seconds,
            permission_mode=config.claude.permission_mode,
            tools=config.claude.tools,
            allowed_tools=config.claude.allowed_tools,
            additional_directories=config.claude.additional_directories,
            max_turns=config.claude.max_turns,
            bare=config.claude.bare,
            no_session_persistence=config.claude.no_session_persistence,
        )

    if config.mode == "kimi":
        kimi_router = KimiCodeRouter(
            name=f"{config.router_name}-kimi",
            executable=config.kimi.executable,
            working_directory=config.kimi.working_directory,
            timeout_seconds=config.kimi.timeout_seconds,
            additional_directories=config.kimi.additional_directories,
            agent_file=config.kimi.agent_file,
        )

    if config.mode == "external_command":
        external_command_router = ExternalCommandRouter(
            name=f"{config.router_name}-external-command",
            command=config.external_command.command,
            working_directory=config.external_command.working_directory,
            timeout_seconds=config.external_command.timeout_seconds,
            model=config.external_model,
            env=dict(config.external_command.env),
        )

    return ConfiguredModelRouter(
        name=config.router_name,
        config=config,
        api_router=api_router,
        codex_router=codex_router,
        claude_router=claude_router,
        kimi_router=kimi_router,
        external_command_router=external_command_router,
    )


def _build_openai_credentials_provider(config: OpenAIModelConfig):
    if config.auth_source == "environment":
        return EnvironmentOpenAICredentialsProvider()
    return FileOpenAICredentialsProvider(
        path=config.credentials_path,
        credential_name=config.credential_name,
    )


def _coerce_command_tuple(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        return tuple(shlex.split(stripped)) if stripped else ()
    if isinstance(value, (list, tuple)):
        command = []
        for item in value:
            text = str(item).strip()
            if text:
                command.append(text)
        return tuple(command)
    text = str(value).strip()
    return tuple(shlex.split(text)) if text else ()


def _coerce_str_dict(value: Any, *, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{field_name} must be a JSON object.")
    return {
        str(key): str(item)
        for key, item in value.items()
        if item is not None
    }


def _normalize_skill_names(value: Any, *, field_name: str) -> Tuple[str, ...]:
    names = _coerce_str_tuple(value)
    if not names:
        return ()

    normalized = []
    seen = set()
    unsupported = []
    for name in names:
        candidate = str(name).strip()
        if not candidate:
            continue
        if candidate not in SUPPORTED_AGENT_SKILLS:
            unsupported.append(candidate)
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)

    if unsupported:
        raise ConfigError(
            f"{field_name} contains unsupported skills: {', '.join(unsupported)}."
        )

    return tuple(normalized)


def _replace_model(model_request: ModelRequest, resolved_model: str) -> ModelRequest:
    return ModelRequest(
        model=resolved_model or model_request.model,
        input=model_request.input,
        instructions=model_request.instructions,
        metadata=dict(model_request.metadata),
        reasoning_effort=model_request.reasoning_effort,
        temperature=model_request.temperature,
        max_output_tokens=model_request.max_output_tokens,
        extra_body=dict(model_request.extra_body),
    )


def _resolve_path(base_directory: Path, value: object) -> Path:
    path = Path(str(value or "."))
    if path.is_absolute():
        return path
    return (base_directory / path).resolve()


def _format_config_path(base_directory: Path, path: Path) -> str:
    resolved_path = Path(path).expanduser().resolve()
    resolved_base = base_directory.expanduser().resolve()
    try:
        relative_path = resolved_path.relative_to(resolved_base)
    except ValueError:
        return str(resolved_path)
    text = relative_path.as_posix()
    return text or "."


def _coerce_str_tuple(value: object) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        return (stripped,) if stripped else ()
    if isinstance(value, (list, tuple)):
        values = []
        for item in value:
            text = str(item).strip()
            if text:
                values.append(text)
        return tuple(values)
    text = str(value).strip()
    return (text,) if text else ()


def _normalize_reasoning_effort(value: object, *, field_name: str) -> str:
    normalized = str(value or "low").strip().lower() or "low"
    if normalized in {"minimum", "minimal"}:
        normalized = "low"
    if normalized not in {"none", "low", "medium", "high", "xhigh"}:
        raise ConfigError(
            f"{field_name} must be one of: none, low, medium, high, xhigh."
        )
    return normalized
