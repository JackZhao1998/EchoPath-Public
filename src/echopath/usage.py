from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from uuid import uuid4


USAGE_CHANNELS = ("external_command", "codex_session", "claude_code", "kimi_code", "api")
USAGE_SNAPSHOT_FIELDS = (
    "requests_total",
    "requests_successful",
    "requests_failed",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
)


def default_token_usage_path() -> Path:
    configured = os.getenv("ECHOPATH_TOKEN_USAGE_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path("local_data/token_usage/requests.jsonl")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _env_or_none(name: str) -> Optional[str]:
    value = os.getenv(name, "").strip()
    return value or None


@dataclass(frozen=True)
class TokenUsageRecord:
    record_id: str
    timestamp: str
    model: str
    router_name: str
    credential_source: str
    success: bool
    duration_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    request_id: Optional[str] = None
    response_id: Optional[str] = None
    error: Optional[str] = None
    run_id: Optional[str] = None
    task_id: Optional[str] = None
    worker_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def create(
        cls,
        *,
        model: str,
        router_name: str,
        credential_source: str,
        success: bool,
        duration_ms: int,
        request_id: Optional[str] = None,
        response_id: Optional[str] = None,
        error: Optional[str] = None,
        usage_payload: Optional[Mapping[str, Any]] = None,
    ) -> "TokenUsageRecord":
        usage_payload = usage_payload or {}
        input_details = usage_payload.get("input_tokens_details", {})
        output_details = usage_payload.get("output_tokens_details", {})
        input_tokens = _coerce_int(usage_payload.get("input_tokens"))
        output_tokens = _coerce_int(usage_payload.get("output_tokens"))
        total_tokens = _coerce_int(usage_payload.get("total_tokens")) or input_tokens + output_tokens

        if not isinstance(input_details, Mapping):
            input_details = {}
        if not isinstance(output_details, Mapping):
            output_details = {}

        return cls(
            record_id=f"usage_{uuid4().hex[:12]}",
            timestamp=utc_now_iso(),
            model=model,
            router_name=router_name,
            credential_source=credential_source,
            success=success,
            duration_ms=duration_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cached_input_tokens=_coerce_int(input_details.get("cached_tokens")),
            reasoning_tokens=_coerce_int(output_details.get("reasoning_tokens")),
            request_id=request_id,
            response_id=response_id,
            error=error,
            run_id=_env_or_none("ECHOPATH_RUN_ID"),
            task_id=_env_or_none("ECHOPATH_TASK_ID"),
            worker_id=_env_or_none("ECHOPATH_WORKER_ID"),
        )


@dataclass
class TokenUsageTracker:
    path: Path = field(default_factory=default_token_usage_path)

    def record(self, record: TokenUsageRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_dict(), ensure_ascii=True))
            handle.write("\n")

    def load_records(self, limit: Optional[int] = None) -> List[TokenUsageRecord]:
        if not self.path.exists():
            return []

        records: List[TokenUsageRecord] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, Mapping):
                continue
            try:
                records.append(TokenUsageRecord(**payload))
            except TypeError:
                continue

        records.sort(key=lambda item: item.timestamp, reverse=True)
        if limit is not None:
            return records[:limit]
        return records

    def summarize(self) -> Dict[str, Any]:
        records = self.load_records()
        summary = _summarize_records(records)
        summary["by_channel"] = {
            channel: _summarize_records(
                [record for record in records if classify_usage_channel(record) == channel]
            )
            for channel in USAGE_CHANNELS
        }
        return summary

    def build_dashboard_payload(self, recent_limit: int = 50) -> Dict[str, Any]:
        recent_records = self.load_records(limit=recent_limit)
        annotated_records = [_record_to_dashboard_dict(record) for record in recent_records]
        return {
            "summary": self.summarize(),
            "recent_records": annotated_records,
            "recent_records_by_channel": {
                channel: [record for record in annotated_records if record["usage_channel"] == channel]
                for channel in USAGE_CHANNELS
            },
            "generated_at": utc_now_iso(),
        }


def token_usage_snapshot(path: Path) -> Dict[str, int]:
    summary = TokenUsageTracker(path=path).summarize()
    totals = summary.get("totals") if isinstance(summary, Mapping) else {}
    if not isinstance(totals, Mapping):
        totals = {}
    return {
        "requests_total": _coerce_int(summary.get("requests_total")) if isinstance(summary, Mapping) else 0,
        "requests_successful": _coerce_int(summary.get("requests_successful")) if isinstance(summary, Mapping) else 0,
        "requests_failed": _coerce_int(summary.get("requests_failed")) if isinstance(summary, Mapping) else 0,
        "input_tokens": _coerce_int(totals.get("input_tokens")),
        "output_tokens": _coerce_int(totals.get("output_tokens")),
        "total_tokens": _coerce_int(totals.get("total_tokens")),
        "cached_input_tokens": _coerce_int(totals.get("cached_input_tokens")),
        "reasoning_tokens": _coerce_int(totals.get("reasoning_tokens")),
    }


def token_usage_delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> Dict[str, int]:
    return {
        field: max(0, _coerce_int(after.get(field)) - _coerce_int(before.get(field)))
        for field in USAGE_SNAPSHOT_FIELDS
    }


def classify_usage_channel(record: TokenUsageRecord) -> str:
    if (
        record.credential_source.startswith("external_command")
        or record.router_name.endswith("-external-command")
        or record.router_name == "external-command"
    ):
        return "external_command"
    if record.credential_source.startswith("codex:") or record.router_name.endswith("-codex"):
        return "codex_session"
    if record.router_name == "codex":
        return "codex_session"
    if record.credential_source.startswith("claude:") or record.router_name.endswith("-claude"):
        return "claude_code"
    if record.router_name == "claude":
        return "claude_code"
    if record.credential_source.startswith("kimi:") or record.router_name.endswith("-kimi"):
        return "kimi_code"
    if record.router_name == "kimi":
        return "kimi_code"
    return "api"


def _record_to_dashboard_dict(record: TokenUsageRecord) -> Dict[str, Any]:
    payload = record.to_dict()
    payload["usage_channel"] = classify_usage_channel(record)
    return payload


def _empty_summary() -> Dict[str, Any]:
    return {
        "requests_total": 0,
        "requests_successful": 0,
        "requests_failed": 0,
        "latest_request_at": None,
        "totals": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_input_tokens": 0,
            "reasoning_tokens": 0,
        },
        "by_model": [],
        "by_day": [],
        "by_router": [],
        "by_task": [],
        "recent_errors": [],
    }


def _summarize_records(records: List[TokenUsageRecord]) -> Dict[str, Any]:
    if not records:
        return _empty_summary()

    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
    }
    by_model: Dict[str, Dict[str, Any]] = {}
    by_day: Dict[str, Dict[str, Any]] = {}
    by_router: Dict[str, Dict[str, Any]] = {}
    by_task: Dict[tuple[Optional[str], Optional[str], Optional[str]], Dict[str, Any]] = {}
    success_count = 0
    failure_count = 0

    for record in records:
        if record.success:
            success_count += 1
        else:
            failure_count += 1

        totals["input_tokens"] += record.input_tokens
        totals["output_tokens"] += record.output_tokens
        totals["total_tokens"] += record.total_tokens
        totals["cached_input_tokens"] += record.cached_input_tokens
        totals["reasoning_tokens"] += record.reasoning_tokens

        model_bucket = by_model.setdefault(
            record.model,
            {
                "model": record.model,
                "requests": 0,
                "successful_requests": 0,
                "failed_requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
        )
        model_bucket["requests"] += 1
        model_bucket["successful_requests"] += int(record.success)
        model_bucket["failed_requests"] += int(not record.success)
        model_bucket["input_tokens"] += record.input_tokens
        model_bucket["output_tokens"] += record.output_tokens
        model_bucket["total_tokens"] += record.total_tokens

        router_bucket = by_router.setdefault(
            record.router_name,
            {
                "router_name": record.router_name,
                "requests": 0,
                "successful_requests": 0,
                "failed_requests": 0,
                "total_tokens": 0,
            },
        )
        router_bucket["requests"] += 1
        router_bucket["successful_requests"] += int(record.success)
        router_bucket["failed_requests"] += int(not record.success)
        router_bucket["total_tokens"] += record.total_tokens

        if record.run_id or record.task_id or record.worker_id:
            task_key = (record.run_id, record.worker_id, record.task_id)
            task_bucket = by_task.setdefault(
                task_key,
                {
                    "run_id": record.run_id,
                    "worker_id": record.worker_id,
                    "task_id": record.task_id,
                    "requests": 0,
                    "successful_requests": 0,
                    "failed_requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "cached_input_tokens": 0,
                    "reasoning_tokens": 0,
                },
            )
            task_bucket["requests"] += 1
            task_bucket["successful_requests"] += int(record.success)
            task_bucket["failed_requests"] += int(not record.success)
            task_bucket["input_tokens"] += record.input_tokens
            task_bucket["output_tokens"] += record.output_tokens
            task_bucket["total_tokens"] += record.total_tokens
            task_bucket["cached_input_tokens"] += record.cached_input_tokens
            task_bucket["reasoning_tokens"] += record.reasoning_tokens

        day_key = record.timestamp[:10]
        day_bucket = by_day.setdefault(
            day_key,
            {
                "day": day_key,
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
        )
        day_bucket["requests"] += 1
        day_bucket["input_tokens"] += record.input_tokens
        day_bucket["output_tokens"] += record.output_tokens
        day_bucket["total_tokens"] += record.total_tokens

    recent_errors = [_record_to_dashboard_dict(record) for record in records if not record.success and record.error][:10]

    return {
        "requests_total": len(records),
        "requests_successful": success_count,
        "requests_failed": failure_count,
        "latest_request_at": records[0].timestamp,
        "totals": totals,
        "by_model": sorted(
            by_model.values(),
            key=lambda item: (item["total_tokens"], item["requests"]),
            reverse=True,
        ),
        "by_day": sorted(by_day.values(), key=lambda item: item["day"]),
        "by_router": sorted(
            by_router.values(),
            key=lambda item: (item["total_tokens"], item["requests"]),
            reverse=True,
        ),
        "by_task": sorted(
            by_task.values(),
            key=lambda item: (item["total_tokens"], item["requests"]),
            reverse=True,
        ),
        "recent_errors": recent_errors,
    }
