from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Protocol, Tuple, runtime_checkable
from urllib import error, request

from .usage import TokenUsageRecord, TokenUsageTracker


class RouterError(RuntimeError):
    pass


class RouterAuthenticationError(RouterError):
    pass


class RouterRequestError(RouterError):
    pass


@dataclass(frozen=True)
class OpenAIAuthBundle:
    token: str
    source: str
    organization: Optional[str] = None
    project: Optional[str] = None

    def to_headers(self) -> Dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if self.organization:
            headers["OpenAI-Organization"] = self.organization
        if self.project:
            headers["OpenAI-Project"] = self.project
        return headers


@runtime_checkable
class OpenAICredentialsProvider(Protocol):
    def load(self) -> OpenAIAuthBundle:
        ...


@dataclass
class EnvironmentOpenAICredentialsProvider:
    api_key_env: str = "OPENAI_API_KEY"
    oauth_token_env: str = "OPENAI_OAUTH_ACCESS_TOKEN"
    organization_env: str = "OPENAI_ORGANIZATION"
    project_env: str = "OPENAI_PROJECT"
    prefer_oauth_token: bool = False

    def load(self) -> OpenAIAuthBundle:
        api_key = os.getenv(self.api_key_env)
        oauth_token = os.getenv(self.oauth_token_env)

        if self.prefer_oauth_token and oauth_token:
            return self._bundle(oauth_token, self.oauth_token_env)
        if api_key:
            return self._bundle(api_key, self.api_key_env)
        if oauth_token:
            return self._bundle(oauth_token, self.oauth_token_env)

        raise RouterAuthenticationError(
            "No OpenAI credentials found. Set OPENAI_API_KEY for the documented API flow, "
            "or supply OPENAI_OAUTH_ACCESS_TOKEN from a separate OAuth bridge."
        )

    def _bundle(self, token: str, source: str) -> OpenAIAuthBundle:
        return OpenAIAuthBundle(
            token=token,
            source=source,
            organization=os.getenv(self.organization_env),
            project=os.getenv(self.project_env),
        )


@dataclass
class FileOpenAICredentialsProvider:
    path: Path
    credential_name: str = "openAI"

    def load(self) -> OpenAIAuthBundle:
        try:
            contents = self.path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise RouterAuthenticationError(
                f"OpenAI credentials file was not found at '{self.path}'."
            ) from exc

        try:
            payload = json.loads(contents)
        except json.JSONDecodeError as exc:
            raise RouterAuthenticationError(
                f"OpenAI credentials file '{self.path}' contains invalid JSON."
            ) from exc

        if not isinstance(payload, Mapping):
            raise RouterAuthenticationError(
                f"OpenAI credentials file '{self.path}' must contain a JSON object."
            )

        raw_profile = payload.get(self.credential_name)
        if raw_profile is None:
            raise RouterAuthenticationError(
                f"Credential profile '{self.credential_name}' was not found in '{self.path}'."
            )

        return self._parse_profile(raw_profile)

    def _parse_profile(self, raw_profile: Any) -> OpenAIAuthBundle:
        if isinstance(raw_profile, str):
            return OpenAIAuthBundle(
                token=raw_profile,
                source=f"file:{self.credential_name}:api_key",
            )

        if not isinstance(raw_profile, Mapping):
            raise RouterAuthenticationError(
                f"Credential profile '{self.credential_name}' must be a string or object."
            )

        auth_type = str(raw_profile.get("auth_type") or raw_profile.get("type") or "api_key")
        token = raw_profile.get("token")
        if token is None and auth_type == "api_key":
            token = raw_profile.get("api_key")
        if token is None and auth_type in {"oauth", "oauth_token"}:
            token = raw_profile.get("oauth_token")

        if not token or not isinstance(token, str):
            raise RouterAuthenticationError(
                f"Credential profile '{self.credential_name}' is missing a token value."
            )

        normalized_type = "oauth_token" if auth_type in {"oauth", "oauth_token"} else "api_key"
        organization = raw_profile.get("organization")
        project = raw_profile.get("project")
        return OpenAIAuthBundle(
            token=token,
            source=f"file:{self.credential_name}:{normalized_type}",
            organization=organization if isinstance(organization, str) else None,
            project=project if isinstance(project, str) else None,
        )


@dataclass
class CodexAuthOpenAICredentialsProvider:
    path: Path = field(default_factory=lambda: Path.home() / ".codex" / "auth.json")
    token_path: tuple[str, ...] = ("tokens", "access_token")

    def load(self) -> OpenAIAuthBundle:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RouterAuthenticationError(
                f"Codex auth file was not found at '{self.path}'."
            ) from exc
        except json.JSONDecodeError as exc:
            raise RouterAuthenticationError(
                f"Codex auth file '{self.path}' contains invalid JSON."
            ) from exc

        current: Any = payload
        for key in self.token_path:
            if not isinstance(current, Mapping) or key not in current:
                dotted_path = ".".join(self.token_path)
                raise RouterAuthenticationError(
                    f"Could not find '{dotted_path}' inside '{self.path}'."
                )
            current = current[key]

        if not isinstance(current, str) or not current.strip():
            dotted_path = ".".join(self.token_path)
            raise RouterAuthenticationError(
                f"Token at '{dotted_path}' inside '{self.path}' is empty."
            )

        return OpenAIAuthBundle(
            token=current.strip(),
            source=f"codex_auth:{'.'.join(self.token_path)}",
        )


@dataclass(frozen=True)
class ModelRequest:
    model: str
    input: Any
    instructions: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    reasoning_effort: Optional[str] = None
    temperature: Optional[float] = None
    max_output_tokens: Optional[int] = None
    extra_body: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResponse:
    model: str
    output_text: str
    raw: Dict[str, Any]
    response_id: Optional[str] = None
    request_id: Optional[str] = None


@runtime_checkable
class ModelRouter(Protocol):
    name: str

    async def generate(self, model_request: ModelRequest) -> ModelResponse:
        ...


@dataclass
class ExternalCommandRouter:
    name: str = "external-command"
    command: Tuple[str, ...] = field(default_factory=tuple)
    working_directory: Optional[Path] = None
    timeout_seconds: int = 300
    model: str = "default"
    env: Dict[str, str] = field(default_factory=dict)
    usage_tracker: TokenUsageTracker = field(default_factory=TokenUsageTracker)

    async def generate(self, model_request: ModelRequest) -> ModelResponse:
        return await asyncio.to_thread(self._generate_sync, model_request)

    def _generate_sync(self, model_request: ModelRequest) -> ModelResponse:
        started_at = time.perf_counter()
        credential_source = self._resolve_credential_source()
        command = self._resolve_command(model_request)

        if not command:
            message = (
                "External command routing requires a model bridge command. "
                "Set external_command.command in echopath.config.json or export "
                "ECHOPATH_MODEL_BRIDGE."
            )
            self._track_request(
                model_request=model_request,
                credential_source=credential_source,
                success=False,
                duration_ms=_duration_ms(started_at),
                error=message,
            )
            raise RouterRequestError(message)

        if not _command_head_exists(command[0]):
            message = f"External model bridge executable was not found: {command[0]}"
            self._track_request(
                model_request=model_request,
                credential_source=credential_source,
                success=False,
                duration_ms=_duration_ms(started_at),
                error=message,
            )
            raise RouterRequestError(message)

        try:
            completed = subprocess.run(
                command,
                input=json.dumps(self._build_payload(model_request), ensure_ascii=False),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                cwd=str(self._resolve_working_directory(model_request)),
                timeout=self._resolve_timeout(model_request),
                check=False,
                env=self._build_environment(model_request),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
        except subprocess.TimeoutExpired as exc:
            message = f"External model bridge timed out after {self._resolve_timeout(model_request)} seconds."
            self._track_request(
                model_request=model_request,
                credential_source=credential_source,
                success=False,
                duration_ms=_duration_ms(started_at),
                error=message,
            )
            raise RouterRequestError(message) from exc
        except OSError as exc:
            message = f"External model bridge failed to start: {exc}"
            self._track_request(
                model_request=model_request,
                credential_source=credential_source,
                success=False,
                duration_ms=_duration_ms(started_at),
                error=message,
            )
            raise RouterRequestError(message) from exc

        if completed.returncode != 0:
            message = _format_completed_process_failure(
                completed,
                fallback=f"External model bridge exited with status {completed.returncode}.",
            )
            self._track_request(
                model_request=model_request,
                credential_source=credential_source,
                success=False,
                duration_ms=_duration_ms(started_at),
                error=message,
            )
            raise RouterRequestError(message)

        data = _parse_external_bridge_stdout(completed.stdout)
        response = ModelResponse(
            model=str(data.get("model") or model_request.model or self.model),
            output_text=_extract_external_output_text(data),
            raw=dict(data),
            response_id=_coerce_optional_str(data.get("response_id") or data.get("id")),
            request_id=_coerce_optional_str(data.get("request_id")),
        )
        self._track_request(
            model_request=model_request,
            credential_source=credential_source,
            success=True,
            duration_ms=_duration_ms(started_at),
            request_id=response.request_id,
            response_id=response.response_id,
            usage_payload=_normalize_cli_usage_payload(data.get("usage")),
            resolved_model=response.model,
        )
        return response

    def _build_payload(self, model_request: ModelRequest) -> Dict[str, Any]:
        extra_body = dict(model_request.extra_body)
        output_schema = extra_body.pop("output_schema", None)
        payload: Dict[str, Any] = {
            "model": model_request.model or self.model,
            "input": model_request.input,
            "instructions": model_request.instructions,
            "metadata": dict(model_request.metadata),
            "reasoning_effort": model_request.reasoning_effort,
            "temperature": model_request.temperature,
            "max_output_tokens": model_request.max_output_tokens,
            "extra_body": extra_body,
        }
        if isinstance(output_schema, Mapping):
            payload["output_schema"] = dict(output_schema)
        return payload

    def _resolve_command(self, model_request: ModelRequest) -> list[str]:
        extra_body = dict(model_request.extra_body)
        raw_command = (
            extra_body.get("external_command")
            or extra_body.get("command")
            or self.command
            or os.getenv("ECHOPATH_MODEL_BRIDGE", "")
        )
        command = _coerce_command(raw_command)
        if not command:
            return []
        return _resolve_command_head(command, self._resolve_working_directory(model_request))

    def _resolve_working_directory(self, model_request: ModelRequest) -> Path:
        extra_body = dict(model_request.extra_body)
        metadata = dict(model_request.metadata)
        working_directory = extra_body.get(
            "working_directory",
            metadata.get("working_directory", self.working_directory or Path.cwd()),
        )
        return Path(str(working_directory)).expanduser().resolve()

    def _resolve_timeout(self, model_request: ModelRequest) -> int:
        raw_timeout = model_request.extra_body.get("timeout_seconds", self.timeout_seconds)
        try:
            timeout_seconds = int(raw_timeout)
        except (TypeError, ValueError):
            timeout_seconds = self.timeout_seconds
        return max(1, timeout_seconds)

    def _build_environment(self, model_request: ModelRequest) -> Dict[str, str]:
        environment = dict(os.environ)
        environment.update({str(key): str(value) for key, value in self.env.items()})
        extra_env = model_request.extra_body.get("env")
        if isinstance(extra_env, Mapping):
            environment.update(
                {
                    str(key): str(value)
                    for key, value in extra_env.items()
                    if value is not None
                }
            )
        return environment

    def _resolve_credential_source(self) -> str:
        source = os.getenv("ECHOPATH_MODEL_BRIDGE_NAME", "").strip()
        return f"external_command:{source}" if source else "external_command"

    def _track_request(
        self,
        *,
        model_request: ModelRequest,
        credential_source: str,
        success: bool,
        duration_ms: int,
        request_id: Optional[str] = None,
        response_id: Optional[str] = None,
        usage_payload: Optional[Mapping[str, Any]] = None,
        resolved_model: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        if self.usage_tracker is None:
            return
        record = TokenUsageRecord.create(
            model=resolved_model or model_request.model or self.model,
            router_name=self.name,
            credential_source=credential_source,
            success=success,
            duration_ms=duration_ms,
            request_id=request_id,
            response_id=response_id,
            error=error,
            usage_payload=usage_payload,
        )
        self.usage_tracker.record(record)


@dataclass
class OpenAIResponsesRouter:
    credentials_provider: OpenAICredentialsProvider = field(
        default_factory=EnvironmentOpenAICredentialsProvider
    )
    fallback_credentials_providers: Tuple[OpenAICredentialsProvider, ...] = field(
        default_factory=tuple
    )
    name: str = "openai"
    endpoint: str = "https://api.openai.com/v1/responses"
    timeout_seconds: int = 60
    usage_tracker: TokenUsageTracker = field(default_factory=TokenUsageTracker)
    max_retries: int = 2
    retry_backoff_seconds: float = 1.0

    async def generate(self, model_request: ModelRequest) -> ModelResponse:
        return await asyncio.to_thread(self._generate_sync, model_request)

    def _generate_sync(self, model_request: ModelRequest) -> ModelResponse:
        authentication_errors = []
        request_errors = []
        providers = (self.credentials_provider, *self.fallback_credentials_providers)

        for index, provider in enumerate(providers):
            try:
                credentials = provider.load()
            except RouterAuthenticationError as exc:
                authentication_errors.append(str(exc))
                continue

            credential_source = credentials.source
            last_error: Optional[BaseException] = None
            data: Optional[Dict[str, Any]] = None
            request_id: Optional[str] = None
            for attempt_index in range(max(0, int(self.max_retries)) + 1):
                attempt_started_at = time.perf_counter()
                try:
                    data, request_id = self._send_request(model_request, credentials)
                    break
                except error.HTTPError as exc:
                    last_error = exc
                    message = self._format_http_error(exc)
                    self._track_request(
                        model_request=model_request,
                        credential_source=credential_source,
                        success=False,
                        duration_ms=_duration_ms(attempt_started_at),
                        error=message,
                    )
                    if self._should_retry_same_provider(exc.code, attempt_index):
                        time.sleep(self._retry_delay(attempt_index))
                        continue
                    request_errors.append(f"{credential_source}: {message}")
                    if self._should_retry_with_fallback(exc.code, index, len(providers)):
                        break
                    raise RouterRequestError(self._combine_attempt_errors(request_errors)) from exc
                except error.URLError as exc:
                    last_error = exc
                    message = f"OpenAI request failed: {exc.reason}"
                    self._track_request(
                        model_request=model_request,
                        credential_source=credential_source,
                        success=False,
                        duration_ms=_duration_ms(attempt_started_at),
                        error=message,
                    )
                    if self._should_retry_same_provider(None, attempt_index):
                        time.sleep(self._retry_delay(attempt_index))
                        continue
                    request_errors.append(f"{credential_source}: {message}")
                    raise RouterRequestError(message) from exc
                except Exception as exc:
                    self._track_request(
                        model_request=model_request,
                        credential_source=credential_source,
                        success=False,
                        duration_ms=_duration_ms(attempt_started_at),
                        error=str(exc),
                    )
                    raise
            else:
                if last_error is not None:
                    raise RouterRequestError(str(last_error)) from last_error
                raise RouterRequestError("OpenAI request failed before a response was returned.")

            if data is None:
                continue

            response = ModelResponse(
                model=str(data.get("model") or model_request.model),
                output_text=self._extract_output_text(data),
                raw=data,
                response_id=data.get("id"),
                request_id=request_id,
            )
            self._track_request(
                model_request=model_request,
                credential_source=credential_source,
                success=True,
                duration_ms=_duration_ms(attempt_started_at),
                request_id=request_id,
                response_id=response.response_id,
                usage_payload=data.get("usage"),
                resolved_model=response.model,
            )
            return response

        if request_errors:
            raise RouterRequestError(self._combine_attempt_errors(request_errors))
        if authentication_errors:
            raise RouterAuthenticationError("; ".join(authentication_errors))
        raise RouterAuthenticationError("No OpenAI credentials providers could be loaded.")

    def _send_request(
        self,
        model_request: ModelRequest,
        credentials: OpenAIAuthBundle,
    ) -> tuple[Dict[str, Any], Optional[str]]:
        payload = self._build_payload(model_request)
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            **credentials.to_headers(),
        }
        http_request = request.Request(
            self.endpoint,
            data=body,
            headers=headers,
            method="POST",
        )
        with request.urlopen(http_request, timeout=self.timeout_seconds) as http_response:
            response_body = http_response.read().decode("utf-8")
            data = json.loads(response_body)
            request_id = http_response.headers.get("x-request-id")
        return data, request_id

    def _build_payload(self, model_request: ModelRequest) -> Dict[str, Any]:
        extra_body = dict(model_request.extra_body)
        output_schema = extra_body.pop("output_schema", None)
        payload: Dict[str, Any] = {
            "model": model_request.model,
            "input": _normalize_openai_input(model_request.input),
        }
        if model_request.instructions:
            payload["instructions"] = model_request.instructions
        if model_request.metadata:
            payload["metadata"] = model_request.metadata
        if model_request.reasoning_effort:
            payload["reasoning"] = {"effort": model_request.reasoning_effort}
        if model_request.temperature is not None:
            payload["temperature"] = model_request.temperature
        if model_request.max_output_tokens is not None:
            payload["max_output_tokens"] = model_request.max_output_tokens
        payload.update(extra_body)
        if isinstance(output_schema, Mapping):
            payload["text"] = _merge_openai_output_schema(
                payload.get("text"),
                output_schema,
                model_request=model_request,
            )
        return payload

    def _extract_output_text(self, data: Mapping[str, Any]) -> str:
        output_text = data.get("output_text")
        if isinstance(output_text, str):
            return output_text

        chunks = []
        for item in data.get("output", []):
            if not isinstance(item, Mapping):
                continue
            for content in item.get("content", []):
                if not isinstance(content, Mapping):
                    continue
                text = content.get("text")
                if isinstance(text, str) and content.get("type") in {"output_text", "text"}:
                    chunks.append(text)

        return "".join(chunks).strip()

    def _format_http_error(self, exc: error.HTTPError) -> str:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return f"OpenAI request failed with status {exc.code}."

        message = payload
        if isinstance(payload, Mapping):
            error_block = payload.get("error")
            if isinstance(error_block, Mapping):
                message = error_block.get("message", payload)

        return f"OpenAI request failed with status {exc.code}: {message}"

    def _should_retry_with_fallback(
        self,
        status_code: int,
        attempt_index: int,
        total_attempts: int,
    ) -> bool:
        has_fallback_remaining = attempt_index < total_attempts - 1
        return has_fallback_remaining and status_code in {401, 403}

    def _should_retry_same_provider(self, status_code: Optional[int], attempt_index: int) -> bool:
        if attempt_index >= max(0, int(self.max_retries)):
            return False
        if status_code is None:
            return True
        return int(status_code) in {408, 409, 429, 500, 502, 503, 504}

    def _retry_delay(self, attempt_index: int) -> float:
        return max(0.0, float(self.retry_backoff_seconds)) * (2 ** max(0, attempt_index))

    def _combine_attempt_errors(self, request_errors: list[str]) -> str:
        if len(request_errors) == 1:
            return request_errors[0]
        return "OpenAI request failed for all configured credentials: " + " | ".join(request_errors)

    def _track_request(
        self,
        *,
        model_request: ModelRequest,
        credential_source: str,
        success: bool,
        duration_ms: int,
        request_id: Optional[str] = None,
        response_id: Optional[str] = None,
        usage_payload: Optional[Mapping[str, Any]] = None,
        resolved_model: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        if self.usage_tracker is None:
            return
        record = TokenUsageRecord.create(
            model=resolved_model or model_request.model,
            router_name=self.name,
            credential_source=credential_source,
            success=success,
            duration_ms=duration_ms,
            request_id=request_id,
            response_id=response_id,
            error=error,
            usage_payload=usage_payload,
        )
        self.usage_tracker.record(record)


@dataclass
class CodexSessionRouter:
    name: str = "codex"
    node_executable: str = "node"
    bridge_script: Optional[Path] = None
    working_directory: Optional[Path] = None
    timeout_seconds: int = 300
    skip_git_repo_check: bool = True
    sandbox_mode: str = "danger-full-access"
    approval_policy: str = "never"
    network_access_enabled: bool = True
    web_search_mode: str = "disabled"
    additional_directories: Tuple[str, ...] = field(default_factory=tuple)
    usage_tracker: TokenUsageTracker = field(default_factory=TokenUsageTracker)

    async def generate(self, model_request: ModelRequest) -> ModelResponse:
        return await asyncio.to_thread(self._generate_sync, model_request)

    def _generate_sync(self, model_request: ModelRequest) -> ModelResponse:
        credential_source = self._resolve_credential_source(model_request)
        payload = self._build_bridge_payload(model_request)
        bridge_script = self._resolve_bridge_script()
        started_at = time.perf_counter()

        node_path = shutil.which(self.node_executable)
        if node_path is None:
            message = f"Node executable '{self.node_executable}' was not found on PATH."
            self._track_request(
                model_request=model_request,
                credential_source=credential_source,
                success=False,
                duration_ms=_duration_ms(started_at),
                error=message,
            )
            raise RouterRequestError(message)

        if bridge_script is None:
            message = (
                "Codex session routing requires an adapter script. "
                "Set ECHOPATH_CODEX_ADAPTER_SCRIPT or pass bridge_script explicitly."
            )
            self._track_request(
                model_request=model_request,
                credential_source=credential_source,
                success=False,
                duration_ms=_duration_ms(started_at),
                error=message,
            )
            raise RouterRequestError(message)

        if not bridge_script.is_file():
            message = f"Codex adapter script was not found at '{bridge_script}'."
            self._track_request(
                model_request=model_request,
                credential_source=credential_source,
                success=False,
                duration_ms=_duration_ms(started_at),
                error=message,
            )
            raise RouterRequestError(message)

        try:
            completed = subprocess.run(
                [node_path, str(bridge_script)],
                input=json.dumps(payload),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                cwd=str(bridge_script.parent),
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            message = f"Codex adapter timed out after {self.timeout_seconds} seconds."
            self._track_request(
                model_request=model_request,
                credential_source=credential_source,
                success=False,
                duration_ms=_duration_ms(started_at),
                error=message,
            )
            raise RouterRequestError(message) from exc
        except OSError as exc:
            message = f"Codex adapter failed to start: {exc}"
            self._track_request(
                model_request=model_request,
                credential_source=credential_source,
                success=False,
                duration_ms=_duration_ms(started_at),
                error=message,
            )
            raise RouterRequestError(message) from exc

        if completed.returncode != 0:
            message = self._format_bridge_failure(completed)
            self._track_request(
                model_request=model_request,
                credential_source=credential_source,
                success=False,
                duration_ms=_duration_ms(started_at),
                error=message,
            )
            raise RouterRequestError(message)

        try:
            data = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            message = "Codex adapter returned invalid JSON."
            self._track_request(
                model_request=model_request,
                credential_source=credential_source,
                success=False,
                duration_ms=_duration_ms(started_at),
                error=message,
            )
            raise RouterRequestError(message) from exc

        if not isinstance(data, Mapping):
            message = "Codex adapter returned an invalid response payload."
            self._track_request(
                model_request=model_request,
                credential_source=credential_source,
                success=False,
                duration_ms=_duration_ms(started_at),
                error=message,
            )
            raise RouterRequestError(message)

        response = ModelResponse(
            model=str(data.get("model") or model_request.model or "codex"),
            output_text=str(data.get("output_text") or ""),
            raw=dict(data),
            response_id=_coerce_optional_str(data.get("response_id") or data.get("thread_id")),
            request_id=_coerce_optional_str(data.get("request_id")),
        )
        self._track_request(
            model_request=model_request,
            credential_source=credential_source,
            success=True,
            duration_ms=_duration_ms(started_at),
            request_id=response.request_id,
            response_id=response.response_id,
            usage_payload=self._build_usage_payload(data.get("usage")),
            resolved_model=response.model,
        )
        return response

    def _build_bridge_payload(self, model_request: ModelRequest) -> Dict[str, Any]:
        extra_body = dict(model_request.extra_body)
        metadata = dict(model_request.metadata)
        working_directory = extra_body.pop(
            "working_directory",
            metadata.get("working_directory", self.working_directory or Path.cwd()),
        )
        additional_directories = _coerce_path_list(
            extra_body.pop(
                "additional_directories",
                metadata.get("additional_directories", self.additional_directories),
            )
        )
        payload: Dict[str, Any] = {
            "input": self._build_codex_input(model_request),
            "working_directory": str(Path(working_directory)),
            "skip_git_repo_check": bool(
                extra_body.pop("skip_git_repo_check", self.skip_git_repo_check)
            ),
            "sandbox_mode": extra_body.pop("sandbox_mode", self.sandbox_mode),
            "approval_policy": extra_body.pop("approval_policy", self.approval_policy),
            "network_access_enabled": bool(
                extra_body.pop("network_access_enabled", self.network_access_enabled)
            ),
            "web_search_mode": extra_body.pop("web_search_mode", self.web_search_mode),
            "additional_directories": additional_directories,
        }

        model = extra_body.pop("codex_model", model_request.model)
        if model and model not in {"default", "codex", "codex-default"}:
            payload["model"] = model

        thread_id = extra_body.pop("thread_id", metadata.get("thread_id"))
        if isinstance(thread_id, str) and thread_id:
            payload["thread_id"] = thread_id

        api_key = extra_body.pop("api_key", None)
        if isinstance(api_key, str) and api_key:
            payload["api_key"] = api_key

        base_url = extra_body.pop("base_url", None)
        if isinstance(base_url, str) and base_url:
            payload["base_url"] = base_url

        config = extra_body.pop("config", None)
        if isinstance(config, Mapping):
            payload["config"] = dict(config)

        env = extra_body.pop("env", None)
        if isinstance(env, Mapping):
            payload["env"] = {
                str(key): str(value)
                for key, value in env.items()
                if value is not None
            }

        output_schema = extra_body.pop("output_schema", None)
        if isinstance(output_schema, Mapping):
            payload["output_schema"] = dict(output_schema)

        reasoning_effort = extra_body.pop(
            "model_reasoning_effort",
            model_request.reasoning_effort,
        )
        normalized_reasoning_effort = _normalize_codex_reasoning_effort(reasoning_effort)
        if normalized_reasoning_effort:
            payload["model_reasoning_effort"] = normalized_reasoning_effort

        web_search_enabled = extra_body.pop("web_search_enabled", None)
        if isinstance(web_search_enabled, bool):
            payload["web_search_enabled"] = web_search_enabled

        return payload

    def _build_codex_input(self, model_request: ModelRequest) -> Any:
        if _looks_like_codex_input(model_request.input):
            items = list(model_request.input)
            if model_request.instructions:
                return [{"type": "text", "text": model_request.instructions}, *items]
            return items

        prompt_parts = []
        if model_request.instructions:
            prompt_parts.append(model_request.instructions.strip())
        if isinstance(model_request.input, str):
            prompt_parts.append(model_request.input)
        else:
            prompt_parts.append(json.dumps(model_request.input, ensure_ascii=False, indent=2))
        return "\n\n".join(part for part in prompt_parts if part).strip()

    def _build_usage_payload(self, usage: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(usage, Mapping):
            return None
        input_tokens = _coerce_int(usage.get("input_tokens"))
        output_tokens = _coerce_int(usage.get("output_tokens"))
        cached_tokens = _coerce_int(usage.get("cached_input_tokens"))
        reasoning_tokens = _coerce_int(usage.get("reasoning_output_tokens"))
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "input_tokens_details": {"cached_tokens": cached_tokens},
            "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
        }

    def _resolve_bridge_script(self) -> Optional[Path]:
        if self.bridge_script is not None:
            return Path(self.bridge_script).expanduser().resolve()
        env_path = os.getenv("ECHOPATH_CODEX_ADAPTER_SCRIPT")
        if isinstance(env_path, str) and env_path.strip():
            return Path(env_path).expanduser().resolve()
        return None

    def _resolve_credential_source(self, model_request: ModelRequest) -> str:
        api_key = model_request.extra_body.get("api_key")
        if isinstance(api_key, str) and api_key:
            return "codex:api_key"

        auth_path = Path.home() / ".codex" / "auth.json"
        try:
            payload = json.loads(auth_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return "codex:session"

        if payload.get("OPENAI_API_KEY"):
            return "codex:api_key"

        auth_mode = payload.get("auth_mode")
        if isinstance(auth_mode, str) and auth_mode:
            return f"codex:{auth_mode}"
        return "codex:session"

    def _format_bridge_failure(self, completed: subprocess.CompletedProcess) -> str:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        if stderr:
            return stderr
        if stdout:
            return stdout
        return f"Codex adapter exited with status {completed.returncode}."

    def _track_request(
        self,
        *,
        model_request: ModelRequest,
        credential_source: str,
        success: bool,
        duration_ms: int,
        request_id: Optional[str] = None,
        response_id: Optional[str] = None,
        usage_payload: Optional[Mapping[str, Any]] = None,
        resolved_model: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        if self.usage_tracker is None:
            return
        record = TokenUsageRecord.create(
            model=resolved_model or model_request.model or "codex",
            router_name=self.name,
            credential_source=credential_source,
            success=success,
            duration_ms=duration_ms,
            request_id=request_id,
            response_id=response_id,
            error=error,
            usage_payload=usage_payload,
        )
        self.usage_tracker.record(record)


@dataclass
class ClaudeCodeRouter:
    name: str = "claude"
    executable: str = "claude"
    working_directory: Optional[Path] = None
    timeout_seconds: int = 300
    permission_mode: str = "dontAsk"
    tools: Tuple[str, ...] = ("Read",)
    allowed_tools: Tuple[str, ...] = ("Read",)
    additional_directories: Tuple[str, ...] = field(default_factory=tuple)
    max_turns: Optional[int] = 3
    bare: bool = False
    no_session_persistence: bool = True
    usage_tracker: TokenUsageTracker = field(default_factory=TokenUsageTracker)

    async def generate(self, model_request: ModelRequest) -> ModelResponse:
        return await asyncio.to_thread(self._generate_sync, model_request)

    def _generate_sync(self, model_request: ModelRequest) -> ModelResponse:
        credential_source = self._resolve_credential_source()
        command = self._build_command(model_request)
        prompt = self._build_prompt(model_request)
        working_directory = self._resolve_working_directory(model_request)
        started_at = time.perf_counter()

        executable_path = shutil.which(self.executable)
        if executable_path is None:
            message = f"Claude Code executable '{self.executable}' was not found on PATH."
            self._track_request(
                model_request=model_request,
                credential_source=credential_source,
                success=False,
                duration_ms=_duration_ms(started_at),
                error=message,
            )
            raise RouterRequestError(message)

        command[0] = executable_path

        try:
            completed = subprocess.run(
                command,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                cwd=str(working_directory),
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            message = f"Claude Code timed out after {self.timeout_seconds} seconds."
            self._track_request(
                model_request=model_request,
                credential_source=credential_source,
                success=False,
                duration_ms=_duration_ms(started_at),
                error=message,
            )
            raise RouterRequestError(message) from exc
        except OSError as exc:
            message = f"Claude Code failed to start: {exc}"
            self._track_request(
                model_request=model_request,
                credential_source=credential_source,
                success=False,
                duration_ms=_duration_ms(started_at),
                error=message,
            )
            raise RouterRequestError(message) from exc

        if completed.returncode != 0:
            message = self._format_cli_failure(completed)
            self._track_request(
                model_request=model_request,
                credential_source=credential_source,
                success=False,
                duration_ms=_duration_ms(started_at),
                error=message,
            )
            raise RouterRequestError(message)

        try:
            data = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            message = "Claude Code returned invalid JSON."
            self._track_request(
                model_request=model_request,
                credential_source=credential_source,
                success=False,
                duration_ms=_duration_ms(started_at),
                error=message,
            )
            raise RouterRequestError(message) from exc

        if not isinstance(data, Mapping):
            message = "Claude Code returned an invalid response payload."
            self._track_request(
                model_request=model_request,
                credential_source=credential_source,
                success=False,
                duration_ms=_duration_ms(started_at),
                error=message,
            )
            raise RouterRequestError(message)

        response = ModelResponse(
            model=str(data.get("model") or model_request.model or "claude"),
            output_text=self._extract_output_text(data),
            raw=dict(data),
            response_id=_coerce_optional_str(data.get("session_id") or data.get("response_id")),
            request_id=_coerce_optional_str(data.get("request_id") or data.get("uuid")),
        )
        self._track_request(
            model_request=model_request,
            credential_source=credential_source,
            success=True,
            duration_ms=_duration_ms(started_at),
            request_id=response.request_id,
            response_id=response.response_id,
            usage_payload=self._build_usage_payload(data.get("usage")),
            resolved_model=response.model,
        )
        return response

    def _build_command(self, model_request: ModelRequest) -> list[str]:
        extra_body = dict(model_request.extra_body)
        command = [self.executable, "-p", self._build_prompt(model_request), "--output-format", "json"]

        model = extra_body.pop("claude_model", model_request.model)
        if isinstance(model, str) and model.strip() and model not in {"default", "claude", "claude-default"}:
            command.extend(["--model", model.strip()])

        max_turns = extra_body.pop("max_turns", self.max_turns)
        if max_turns is not None:
            command.extend(["--max-turns", str(int(max_turns))])

        permission_mode = str(extra_body.pop("permission_mode", self.permission_mode) or "").strip()
        if permission_mode:
            command.extend(["--permission-mode", permission_mode])

        tools = _coerce_str_tuple(extra_body.pop("tools", self.tools))
        if tools:
            command.extend(["--tools", ",".join(tools)])

        allowed_tools = _coerce_str_tuple(extra_body.pop("allowed_tools", self.allowed_tools))
        if allowed_tools:
            command.extend(["--allowedTools", ",".join(allowed_tools)])

        additional_directories = _coerce_path_list(
            extra_body.pop(
                "additional_directories",
                model_request.metadata.get("additional_directories", self.additional_directories),
            )
        )
        for directory in additional_directories:
            command.extend(["--add-dir", directory])

        output_schema = extra_body.pop("output_schema", None)
        if isinstance(output_schema, Mapping):
            command.extend(["--json-schema", json.dumps(output_schema, ensure_ascii=True)])

        if bool(extra_body.pop("bare", self.bare)):
            command.append("--bare")

        if bool(extra_body.pop("no_session_persistence", self.no_session_persistence)):
            command.append("--no-session-persistence")

        return command

    def _resolve_working_directory(self, model_request: ModelRequest) -> Path:
        extra_body = dict(model_request.extra_body)
        metadata = dict(model_request.metadata)
        working_directory = extra_body.get(
            "working_directory",
            metadata.get("working_directory", self.working_directory or Path.cwd()),
        )
        return Path(str(working_directory))

    def _build_prompt(self, model_request: ModelRequest) -> str:
        prompt_parts = []
        if model_request.instructions:
            prompt_parts.append(model_request.instructions.strip())
        prompt_parts.append(_format_claude_code_input(model_request.input))
        return "\n\n".join(part for part in prompt_parts if part).strip()

    def _extract_output_text(self, data: Mapping[str, Any]) -> str:
        result = data.get("result")
        if isinstance(result, str):
            return result
        output_text = data.get("output_text")
        if isinstance(output_text, str):
            return output_text
        structured_output = data.get("structured_output")
        if structured_output is not None:
            return json.dumps(structured_output, ensure_ascii=False)
        return ""

    def _build_usage_payload(self, usage: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(usage, Mapping):
            return None
        input_tokens = _coerce_int(usage.get("input_tokens"))
        output_tokens = _coerce_int(usage.get("output_tokens"))
        cache_creation_tokens = _coerce_int(usage.get("cache_creation_input_tokens"))
        cache_read_tokens = _coerce_int(usage.get("cache_read_input_tokens"))
        reasoning_tokens = _coerce_int(
            usage.get("reasoning_tokens")
            or usage.get("thinking_tokens")
            or usage.get("reasoning_output_tokens")
        )
        total_tokens = _coerce_int(usage.get("total_tokens")) or input_tokens + output_tokens
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "input_tokens_details": {"cached_tokens": cache_read_tokens + cache_creation_tokens},
            "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
        }

    def _resolve_credential_source(self) -> str:
        if os.getenv("ANTHROPIC_AUTH_TOKEN"):
            return "claude:anthropic_auth_token"
        if os.getenv("ANTHROPIC_API_KEY"):
            return "claude:anthropic_api_key"
        if os.getenv("CLAUDE_CODE_OAUTH_TOKEN"):
            return "claude:oauth_token"
        if os.getenv("ANTHROPIC_PROFILE"):
            return "claude:anthropic_profile"
        credentials_path = Path(os.getenv("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / ".credentials.json"
        if credentials_path.exists():
            return "claude:credentials_file"
        return "claude:session"

    def _format_cli_failure(self, completed: subprocess.CompletedProcess) -> str:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        if stderr:
            return stderr
        if stdout:
            return stdout
        return f"Claude Code exited with status {completed.returncode}."

    def _track_request(
        self,
        *,
        model_request: ModelRequest,
        credential_source: str,
        success: bool,
        duration_ms: int,
        request_id: Optional[str] = None,
        response_id: Optional[str] = None,
        usage_payload: Optional[Mapping[str, Any]] = None,
        resolved_model: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        if self.usage_tracker is None:
            return
        record = TokenUsageRecord.create(
            model=resolved_model or model_request.model or "claude",
            router_name=self.name,
            credential_source=credential_source,
            success=success,
            duration_ms=duration_ms,
            request_id=request_id,
            response_id=response_id,
            error=error,
            usage_payload=usage_payload,
        )
        self.usage_tracker.record(record)


@dataclass
class KimiCodeRouter:
    name: str = "kimi"
    executable: str = "kimi"
    working_directory: Optional[Path] = None
    timeout_seconds: int = 300
    additional_directories: Tuple[str, ...] = field(default_factory=tuple)
    agent_file: Optional[Path] = None
    usage_tracker: TokenUsageTracker = field(default_factory=TokenUsageTracker)

    async def generate(self, model_request: ModelRequest) -> ModelResponse:
        return await asyncio.to_thread(self._generate_sync, model_request)

    def _generate_sync(self, model_request: ModelRequest) -> ModelResponse:
        environment = self._build_environment(model_request)
        credential_source = self._resolve_credential_source(environment)
        command = self._build_command(model_request)
        working_directory = self._resolve_working_directory(model_request)
        started_at = time.perf_counter()

        executable_path = shutil.which(self.executable)
        if executable_path is None:
            message = f"Kimi Code executable '{self.executable}' was not found on PATH."
            self._track_request(
                model_request=model_request,
                credential_source=credential_source,
                success=False,
                duration_ms=_duration_ms(started_at),
                error=message,
            )
            raise RouterRequestError(message)

        command[0] = executable_path

        try:
            completed = subprocess.run(
                command,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                cwd=str(working_directory),
                timeout=self.timeout_seconds,
                check=False,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            message = f"Kimi Code timed out after {self.timeout_seconds} seconds."
            self._track_request(
                model_request=model_request,
                credential_source=credential_source,
                success=False,
                duration_ms=_duration_ms(started_at),
                error=message,
            )
            raise RouterRequestError(message) from exc
        except OSError as exc:
            message = f"Kimi Code failed to start: {exc}"
            self._track_request(
                model_request=model_request,
                credential_source=credential_source,
                success=False,
                duration_ms=_duration_ms(started_at),
                error=message,
            )
            raise RouterRequestError(message) from exc

        if completed.returncode != 0:
            message = self._format_cli_failure(completed)
            self._track_request(
                model_request=model_request,
                credential_source=credential_source,
                success=False,
                duration_ms=_duration_ms(started_at),
                error=message,
            )
            raise RouterRequestError(message)

        data = _parse_kimi_stream_json(completed.stdout)
        response = ModelResponse(
            model=str(data.get("model") or model_request.model or "kimi"),
            output_text=_extract_kimi_output_text(data),
            raw=data,
            response_id=_coerce_optional_str(data.get("session_id") or data.get("response_id")),
            request_id=_coerce_optional_str(data.get("request_id")),
        )
        usage_payload = _normalize_cli_usage_payload(data.get("usage"))
        if not usage_payload or not _coerce_int(usage_payload.get("total_tokens")):
            usage_payload = _load_kimi_session_usage(response.response_id, environment)
        self._track_request(
            model_request=model_request,
            credential_source=credential_source,
            success=True,
            duration_ms=_duration_ms(started_at),
            request_id=response.request_id,
            response_id=response.response_id,
            usage_payload=usage_payload,
            resolved_model=response.model,
        )
        return response

    def _build_command(self, model_request: ModelRequest) -> list[str]:
        extra_body = dict(model_request.extra_body)
        output_schema = extra_body.pop("output_schema", None)
        prompt = self._build_prompt(model_request, output_schema=output_schema)
        command = [self.executable]

        if self.agent_file is not None:
            command.extend(["--agent-file", str(self.agent_file)])

        model = extra_body.pop("kimi_model", model_request.model)
        if isinstance(model, str) and model.strip() and model not in {"default", "kimi", "kimi-default"}:
            command.extend(["-m", model.strip()])

        command.extend(["-p", prompt, "--output-format", "stream-json"])

        additional_directories = _coerce_path_list(
            extra_body.pop(
                "additional_directories",
                model_request.metadata.get("additional_directories", self.additional_directories),
            )
        )
        for directory in additional_directories:
            command.extend(["--add-dir", directory])

        return command

    def _resolve_working_directory(self, model_request: ModelRequest) -> Path:
        extra_body = dict(model_request.extra_body)
        metadata = dict(model_request.metadata)
        working_directory = extra_body.get(
            "working_directory",
            metadata.get("working_directory", self.working_directory or Path.cwd()),
        )
        return Path(str(working_directory))

    def _build_prompt(self, model_request: ModelRequest, *, output_schema: Any = None) -> str:
        prompt_parts = []
        if model_request.instructions:
            prompt_parts.append(model_request.instructions.strip())
        if isinstance(output_schema, Mapping):
            prompt_parts.append(
                "Return only valid JSON matching this JSON Schema. "
                "Do not wrap it in Markdown.\n"
                + json.dumps(output_schema, ensure_ascii=False, indent=2)
            )
        prompt_parts.append(_format_kimi_code_input(model_request.input))
        return "\n\n".join(part for part in prompt_parts if part).strip()

    def _build_environment(self, model_request: ModelRequest) -> Dict[str, str]:
        environment = dict(os.environ)
        environment.setdefault("KIMI_DISABLE_TELEMETRY", "1")
        if self.agent_file is not None:
            environment.setdefault("KIMI_CODE_EXPERIMENTAL_FLAG", "1")
        extra_env = model_request.extra_body.get("env")
        if isinstance(extra_env, Mapping):
            environment.update(
                {
                    str(key): str(value)
                    for key, value in extra_env.items()
                    if value is not None
                }
            )
        return environment

    def _resolve_credential_source(self, environment: Mapping[str, str]) -> str:
        if environment.get("KIMI_MODEL_NAME") and environment.get("KIMI_MODEL_API_KEY"):
            return "kimi:model_env"

        code_home = Path(
            environment.get("KIMI_CODE_HOME") or str(Path.home() / ".kimi-code")
        ).expanduser()
        credentials_dir = code_home / "credentials"
        if credentials_dir.exists():
            try:
                if any(path.is_file() for path in credentials_dir.iterdir()):
                    return "kimi:code_home_credentials"
            except OSError:
                pass
        if (code_home / "config.toml").exists():
            return "kimi:config"
        return "kimi:session"

    def _format_cli_failure(self, completed: subprocess.CompletedProcess) -> str:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        if stderr and stdout:
            return f"{stderr}\n{stdout}"
        if stderr:
            return stderr
        if stdout:
            return stdout
        return f"Kimi Code exited with status {completed.returncode}."

    def _track_request(
        self,
        *,
        model_request: ModelRequest,
        credential_source: str,
        success: bool,
        duration_ms: int,
        request_id: Optional[str] = None,
        response_id: Optional[str] = None,
        usage_payload: Optional[Mapping[str, Any]] = None,
        resolved_model: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        if self.usage_tracker is None:
            return
        record = TokenUsageRecord.create(
            model=resolved_model or model_request.model or "kimi",
            router_name=self.name,
            credential_source=credential_source,
            success=success,
            duration_ms=duration_ms,
            request_id=request_id,
            response_id=response_id,
            error=error,
            usage_payload=usage_payload,
        )
        self.usage_tracker.record(record)


def _coerce_optional_str(value: Any) -> Optional[str]:
    if isinstance(value, str) and value:
        return value
    return None


def _normalize_codex_reasoning_effort(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized in {"minimal", "minimum"}:
        return "low"
    return normalized


def _merge_openai_output_schema(
    text_config: Any,
    output_schema: Mapping[str, Any],
    *,
    model_request: ModelRequest,
) -> Dict[str, Any]:
    text_payload = dict(text_config) if isinstance(text_config, Mapping) else {}
    if "format" in text_payload:
        return text_payload
    text_payload["format"] = {
        "type": "json_schema",
        "name": _openai_output_schema_name(model_request),
        "schema": dict(output_schema),
        "strict": True,
    }
    return text_payload


def _openai_output_schema_name(model_request: ModelRequest) -> str:
    raw = str(model_request.metadata.get("operation") or "echopath_output")
    normalized = "".join(
        char if char.isalnum() or char in {"_", "-"} else "_"
        for char in raw.strip()
    ).strip("_-")
    return (normalized or "echopath_output")[:64]


def _coerce_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _parse_kimi_stream_json(stdout: str) -> Dict[str, Any]:
    events = []
    non_json_lines = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            non_json_lines.append(line)
            continue
        if isinstance(payload, Mapping):
            events.append(dict(payload))
        else:
            non_json_lines.append(line)

    if not events:
        if non_json_lines:
            return {
                "type": "kimi_stream_json",
                "events": [],
                "non_json_lines": non_json_lines,
                "output_text": "\n".join(non_json_lines).strip(),
            }
        raise RouterRequestError("Kimi Code returned no stream-json events.")

    usage = _select_kimi_usage_payload(events)
    return {
        "type": "kimi_stream_json",
        "events": events,
        "non_json_lines": non_json_lines,
        "model": _first_kimi_value(events, ("model", "model_id", "modelName")),
        "session_id": _first_kimi_value(events, ("session_id", "sessionId")),
        "response_id": _first_kimi_value(events, ("response_id", "message_id", "messageId")),
        "request_id": _first_kimi_value(events, ("request_id", "requestId")),
        "usage": usage,
    }


def _extract_kimi_output_text(data: Mapping[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str):
        return output_text

    chunks = []
    for event in data.get("events", []):
        if not isinstance(event, Mapping):
            continue
        role = str(event.get("role") or event.get("type") or "").lower()
        if role not in {"assistant", "assistant_message", "message"}:
            message = event.get("message")
            if isinstance(message, Mapping):
                role = str(message.get("role") or message.get("type") or "").lower()
                if role in {"assistant", "assistant_message", "message"}:
                    chunks.append(_extract_kimi_content_text(message))
            continue
        chunks.append(_extract_kimi_content_text(event))

    return "\n\n".join(chunk.strip() for chunk in chunks if chunk.strip()).strip()


def _extract_kimi_content_text(event: Mapping[str, Any]) -> str:
    for key in ("content", "text", "output_text", "result"):
        text = _extract_kimi_text_value(event.get(key))
        if text:
            return text
    delta = event.get("delta")
    if isinstance(delta, Mapping):
        return _extract_kimi_content_text(delta)
    return ""


def _extract_kimi_text_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        parts = [
            _extract_kimi_text_value(value.get(key))
            for key in ("text", "content", "value")
        ]
        return "".join(part for part in parts if part).strip()
    if isinstance(value, list):
        parts = [_extract_kimi_text_value(item) for item in value]
        return "".join(part for part in parts if part).strip()
    return ""


def _first_kimi_value(events: list[Mapping[str, Any]], keys: tuple[str, ...]) -> Optional[str]:
    for event in reversed(events):
        for key in keys:
            value = event.get(key)
            if isinstance(value, str) and value:
                return value
        message = event.get("message")
        if isinstance(message, Mapping):
            for key in keys:
                value = message.get(key)
                if isinstance(value, str) and value:
                    return value
    return None


def _select_kimi_usage_payload(events: list[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    usage_payloads = []
    for event in events:
        usage_payloads.extend(_find_usage_payloads(event))
    normalized = [
        payload
        for payload in (_normalize_cli_usage_payload(item) for item in usage_payloads)
        if payload is not None
    ]
    return _sum_kimi_usage_payloads(normalized)


def _load_kimi_session_usage(
    session_id: Optional[str],
    environment: Mapping[str, str],
) -> Optional[Dict[str, Any]]:
    if not session_id or Path(session_id).name != session_id:
        return None

    home = Path(environment.get("HOME") or Path.home())
    code_home = Path(environment.get("KIMI_CODE_HOME") or home / ".kimi-code").expanduser()
    sessions_root = code_home / "sessions"
    if not sessions_root.is_dir():
        return None

    usage_payloads = []
    try:
        workspace_dirs = list(sessions_root.iterdir())
    except OSError:
        return None

    for workspace_dir in workspace_dirs:
        agents_dir = workspace_dir / session_id / "agents"
        if not agents_dir.is_dir():
            continue
        try:
            wire_paths = [agent_dir / "wire.jsonl" for agent_dir in agents_dir.iterdir()]
        except OSError:
            continue
        for wire_path in wire_paths:
            if not wire_path.is_file():
                continue
            try:
                lines = wire_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, Mapping) or event.get("type") != "usage.record":
                    continue
                normalized = _normalize_cli_usage_payload(event.get("usage"))
                if normalized is not None:
                    usage_payloads.append(normalized)

    return _sum_kimi_usage_payloads(usage_payloads)


def _sum_kimi_usage_payloads(
    normalized: list[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    if not normalized:
        return None
    return {
        "input_tokens": sum(_coerce_int(item.get("input_tokens")) for item in normalized),
        "output_tokens": sum(_coerce_int(item.get("output_tokens")) for item in normalized),
        "total_tokens": sum(_coerce_int(item.get("total_tokens")) for item in normalized),
        "input_tokens_details": {
            "cached_tokens": sum(
                _coerce_int(details.get("cached_tokens"))
                for item in normalized
                for details in [item.get("input_tokens_details")]
                if isinstance(details, Mapping)
            )
        },
        "output_tokens_details": {
            "reasoning_tokens": sum(
                _coerce_int(details.get("reasoning_tokens"))
                for item in normalized
                for details in [item.get("output_tokens_details")]
                if isinstance(details, Mapping)
            )
        },
    }


def _find_usage_payloads(value: Any) -> list[Mapping[str, Any]]:
    found = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"usage", "token_usage", "model_usage", "modelUsage"} and isinstance(item, Mapping):
                found.append(item)
            elif isinstance(item, (Mapping, list)):
                found.extend(_find_usage_payloads(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_find_usage_payloads(item))
    return found


def _coerce_command(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return shlex.split(text) if text else []
    if isinstance(value, (list, tuple)):
        command = []
        for item in value:
            text = str(item).strip()
            if text:
                command.append(text)
        return command
    text = str(value).strip()
    return shlex.split(text) if text else []


def _command_head_exists(command_head: str) -> bool:
    if not command_head:
        return False
    if os.sep in command_head or (os.altsep and os.altsep in command_head):
        return Path(command_head).expanduser().exists()
    return shutil.which(command_head) is not None


def _resolve_command_head(command: list[str], working_directory: Path) -> list[str]:
    resolved = list(command)
    command_head = resolved[0]
    if (
        command_head
        and (os.sep in command_head or (os.altsep and os.altsep in command_head))
        and not Path(command_head).expanduser().is_absolute()
    ):
        resolved[0] = str((working_directory / command_head).expanduser().resolve())
    return resolved


def _parse_external_bridge_stdout(stdout: str) -> Dict[str, Any]:
    text = str(stdout or "").strip()
    if not text:
        raise RouterRequestError("External model bridge returned no output.")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {
            "output_text": text,
            "type": "external_command_text",
        }
    if not isinstance(payload, Mapping):
        raise RouterRequestError("External model bridge returned JSON that was not an object.")
    return dict(payload)


def _extract_external_output_text(data: Mapping[str, Any]) -> str:
    for key in ("output_text", "text", "result", "response", "content"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    structured_output = data.get("structured_output")
    if structured_output is not None:
        return json.dumps(structured_output, ensure_ascii=False)
    return ""


def _format_completed_process_failure(
    completed: subprocess.CompletedProcess,
    *,
    fallback: str,
) -> str:
    stderr = (completed.stderr or "").strip()
    stdout = (completed.stdout or "").strip()
    if stderr and stdout:
        return f"{stderr}\n{stdout}"
    if stderr:
        return stderr
    if stdout:
        return stdout
    return fallback


def _normalize_cli_usage_payload(usage: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(usage, Mapping):
        return None

    kimi_uncached_input = _coerce_int(usage.get("inputOther") or usage.get("input_other"))
    kimi_cache_read = _coerce_int(usage.get("inputCacheRead") or usage.get("input_cache_read"))
    kimi_cache_creation = _coerce_int(
        usage.get("inputCacheCreation") or usage.get("input_cache_creation")
    )
    input_tokens = _coerce_int(
        usage.get("input_tokens")
        or usage.get("prompt_tokens")
        or usage.get("inputTokens")
        or usage.get("promptTokens")
    ) or kimi_uncached_input + kimi_cache_read + kimi_cache_creation
    output_tokens = _coerce_int(
        usage.get("output_tokens")
        or usage.get("completion_tokens")
        or usage.get("outputTokens")
        or usage.get("completionTokens")
        or usage.get("output")
    )
    total_tokens = _coerce_int(
        usage.get("total_tokens")
        or usage.get("totalTokens")
        or usage.get("tokens")
    ) or input_tokens + output_tokens

    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    if not isinstance(input_details, Mapping):
        input_details = {}
    output_details = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
    if not isinstance(output_details, Mapping):
        output_details = {}

    cached_tokens = (
        _coerce_int(input_details.get("cached_tokens") or input_details.get("cachedTokens"))
        + _coerce_int(usage.get("cached_input_tokens") or usage.get("cachedInputTokens"))
        + _coerce_int(usage.get("cache_read_input_tokens") or usage.get("cacheReadInputTokens"))
        + _coerce_int(usage.get("cache_creation_input_tokens") or usage.get("cacheCreationInputTokens"))
        + kimi_cache_read
        + kimi_cache_creation
    )
    reasoning_tokens = _coerce_int(
        output_details.get("reasoning_tokens")
        or output_details.get("reasoningTokens")
        or usage.get("reasoning_tokens")
        or usage.get("thinking_tokens")
        or usage.get("reasoning_output_tokens")
    )

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "input_tokens_details": {"cached_tokens": cached_tokens},
        "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
    }


def _coerce_str_tuple(value: Any) -> Tuple[str, ...]:
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


def _format_claude_code_input(value: Any) -> str:
    if isinstance(value, str):
        return value

    if isinstance(value, list):
        parts = []
        for item in value:
            if not isinstance(item, Mapping):
                continue
            item_type = item.get("type")
            if item_type == "text" and isinstance(item.get("text"), str):
                parts.append(str(item["text"]))
            elif item_type == "local_image" and isinstance(item.get("path"), str):
                parts.append(f"Analyze this image: {item['path']}")
        if parts:
            return "\n\n".join(parts)

    return json.dumps(value, ensure_ascii=False, indent=2)


def _format_kimi_code_input(value: Any) -> str:
    if isinstance(value, str):
        return value

    if isinstance(value, list):
        parts = []
        for item in value:
            if not isinstance(item, Mapping):
                continue
            item_type = item.get("type")
            if item_type == "text" and isinstance(item.get("text"), str):
                parts.append(str(item["text"]))
            elif item_type == "local_image" and isinstance(item.get("path"), str):
                parts.append(f"Image reference: @{item['path']}")
        if parts:
            return "\n\n".join(parts)

    return json.dumps(value, ensure_ascii=False, indent=2)


def _looks_like_codex_input(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, Mapping):
            return False
        item_type = item.get("type")
        if item_type not in {"text", "local_image"}:
            return False
    return True


def _normalize_openai_input(value: Any) -> Any:
    if not _looks_like_codex_input(value):
        return value

    content = []
    for item in value:
        item_type = item.get("type")
        if item_type == "text":
            content.append({"type": "input_text", "text": str(item.get("text", ""))})
            continue
        if item_type == "local_image":
            content.append(
                {
                    "type": "input_image",
                    "image_url": _image_path_to_data_url(Path(str(item.get("path", "")))),
                }
            )

    return [{"role": "user", "content": content}]


def _image_path_to_data_url(path: Path) -> str:
    mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _coerce_path_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, Path)):
        return [str(value)]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def _duration_ms(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)
