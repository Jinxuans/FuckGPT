from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


STEP_PENDING = "pending"
STEP_READY = "ready"
STEP_RUNNING = "running"
STEP_WAITING_EXTERNAL = "waiting_external"
STEP_RETRY_SCHEDULED = "retry_scheduled"
STEP_SUCCEEDED = "succeeded"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
STEP_CANCELLED = "cancelled"
STEP_NEEDS_ATTENTION = "needs_attention"

STEP_TERMINAL = {STEP_SUCCEEDED, STEP_FAILED, STEP_SKIPPED, STEP_CANCELLED}
STEP_ACTIVE = {STEP_READY, STEP_RUNNING, STEP_WAITING_EXTERNAL, STEP_RETRY_SCHEDULED, STEP_NEEDS_ATTENTION}

RUN_PENDING = "pending"
RUN_RUNNING = "running"
RUN_WAITING_EXTERNAL = "waiting_external"
RUN_RETRY_SCHEDULED = "retry_scheduled"
RUN_NEEDS_ATTENTION = "needs_attention"
RUN_CANCEL_REQUESTED = "cancel_requested"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"

RUN_TERMINAL = {RUN_SUCCEEDED, RUN_FAILED, RUN_CANCELLED}
RUN_ACTIVE = {
    RUN_PENDING,
    RUN_RUNNING,
    RUN_WAITING_EXTERNAL,
    RUN_RETRY_SCHEDULED,
    RUN_NEEDS_ATTENTION,
    RUN_CANCEL_REQUESTED,
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_duration_seconds(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return max(int(default), 0)
    if isinstance(value, (int, float)):
        return max(int(value), 0)
    text = str(value).strip().lower()
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if text[-1:] in multipliers:
        return max(int(float(text[:-1]) * multipliers[text[-1]]), 0)
    return max(int(float(text)), 0)


def get_path(context: dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = context
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
            continue
        return default
    return current


def resolve_value(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$path"}:
            return get_path(context, str(value["$path"]))
        return {key: resolve_value(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_value(item, context) for item in value]
    return value


def evaluate_condition(condition: Any, context: dict[str, Any]) -> bool:
    if condition in (None, {}, []):
        return True
    if isinstance(condition, bool):
        return condition
    if not isinstance(condition, dict):
        raise ValueError("工作流条件必须是对象")
    if "all" in condition:
        return all(evaluate_condition(item, context) for item in condition.get("all") or [])
    if "any" in condition:
        return any(evaluate_condition(item, context) for item in condition.get("any") or [])
    if "not" in condition:
        return not evaluate_condition(condition.get("not"), context)

    path = str(condition.get("path") or "")
    op = str(condition.get("op") or "truthy")
    sentinel = object()
    actual = get_path(context, path, sentinel)
    expected = resolve_value(condition.get("value"), context)
    if op == "exists":
        return actual is not sentinel and actual is not None and actual != ""
    if op == "truthy":
        return actual is not sentinel and bool(actual)
    if op == "eq":
        return actual is not sentinel and actual == expected
    if op == "ne":
        return actual is sentinel or actual != expected
    if op == "in":
        return actual is not sentinel and actual in (expected or [])
    if op == "not_in":
        return actual is sentinel or actual not in (expected or [])
    raise ValueError(f"不支持的工作流条件操作符: {op}")


@dataclass(slots=True)
class StepTransition:
    status: str
    output: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] = field(default_factory=dict)
    external_ref: str = ""
    next_run_at: datetime | None = None
    message: str = ""
    retryable: bool = False

    @classmethod
    def succeeded(cls, output: dict[str, Any] | None = None, *, message: str = "") -> "StepTransition":
        return cls(STEP_SUCCEEDED, output=dict(output or {}), message=message)

    @classmethod
    def skipped(cls, output: dict[str, Any] | None = None, *, message: str = "") -> "StepTransition":
        return cls(STEP_SKIPPED, output=dict(output or {}), message=message)

    @classmethod
    def waiting(
        cls,
        external_ref: str,
        *,
        seconds: int = 1,
        output: dict[str, Any] | None = None,
        message: str = "",
    ) -> "StepTransition":
        return cls(
            STEP_WAITING_EXTERNAL,
            output=dict(output or {}),
            external_ref=external_ref,
            next_run_at=utcnow() + timedelta(seconds=max(int(seconds), 1)),
            message=message,
        )

    @classmethod
    def failed(cls, message: str, *, code: str = "step_failed", retryable: bool = False) -> "StepTransition":
        return cls(
            STEP_FAILED,
            error={"code": code, "message": str(message)},
            message=str(message),
            retryable=retryable,
        )

    @classmethod
    def needs_attention(cls, message: str, *, code: str = "needs_attention") -> "StepTransition":
        return cls(
            STEP_NEEDS_ATTENTION,
            error={"code": code, "message": str(message)},
            message=str(message),
        )


class StepAdapter:
    key = ""

    def start(self, *, inputs: dict[str, Any], idempotency_key: str, attempt: int) -> StepTransition:
        raise NotImplementedError

    def resume(self, *, inputs: dict[str, Any], external_ref: str, attempt: int) -> StepTransition:
        raise NotImplementedError

    def cancel(self, *, inputs: dict[str, Any], external_ref: str) -> None:
        return
