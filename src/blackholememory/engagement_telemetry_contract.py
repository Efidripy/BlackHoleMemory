"""Pure, content-free contract for future durable BHM engagement telemetry.

This module is deliberately not a telemetry writer.  It has no SQLite, outbox,
HTTP, MCP, runtime or filesystem dependency.  A later, separately approved
rollout may use these validators before it chooses a durable sink.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = "bhm.engagement-telemetry.v1"
CORRELATION_VERSION = "bhm.engagement-correlation.v1"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class EngagementTelemetryContractError(ValueError):
    """Raised when a payload cannot be safely correlated or deduplicated."""


class EventType(StrEnum):
    TASK_BOUND = "task_bound"
    NATIVE_HEALTH_INVOKED = "native_health_invoked"
    PREFLIGHT_COMPLETED = "preflight_completed"
    RETRIEVAL_REQUESTED = "retrieval_requested"
    RETRIEVAL_COMPLETED = "retrieval_completed"
    RETRIEVAL_BLOCKED = "retrieval_blocked"
    RETRIEVAL_NOT_APPLICABLE = "retrieval_not_applicable"
    MEMORY_USE_DECLARED = "memory_use_declared"
    MEMORY_NOT_USED = "memory_not_used"
    TASK_CLOSED = "task_closed"


class OperationFamily(StrEnum):
    TASK_LIFECYCLE = "task_lifecycle"
    NATIVE_HEALTH = "native_health"
    PREFLIGHT = "preflight"
    RETRIEVAL = "retrieval"


class EventOutcome(StrEnum):
    BOUND = "bound"
    OBSERVED = "observed"
    COMPLETED = "completed"
    REQUESTED = "requested"
    BLOCKED = "blocked"
    NOT_APPLICABLE = "not_applicable"
    DECLARED = "declared"
    NOT_USED = "not_used"
    CLOSED = "closed"


class ResultBucket(StrEnum):
    NONE = "none"
    ZERO = "zero"
    ONE = "one"
    FEW = "few"
    MANY = "many"
    UNKNOWN = "unknown"


class FailureCategory(StrEnum):
    AUTHORIZATION = "authorization"
    POLICY = "policy"
    TRANSPORT = "transport"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    VALIDATION = "validation"
    INTERNAL = "internal"


_EVENT_SEMANTICS: dict[EventType, tuple[OperationFamily, EventOutcome]] = {
    EventType.TASK_BOUND: (OperationFamily.TASK_LIFECYCLE, EventOutcome.BOUND),
    EventType.NATIVE_HEALTH_INVOKED: (OperationFamily.NATIVE_HEALTH, EventOutcome.OBSERVED),
    EventType.PREFLIGHT_COMPLETED: (OperationFamily.PREFLIGHT, EventOutcome.COMPLETED),
    EventType.RETRIEVAL_REQUESTED: (OperationFamily.RETRIEVAL, EventOutcome.REQUESTED),
    EventType.RETRIEVAL_COMPLETED: (OperationFamily.RETRIEVAL, EventOutcome.COMPLETED),
    EventType.RETRIEVAL_BLOCKED: (OperationFamily.RETRIEVAL, EventOutcome.BLOCKED),
    EventType.RETRIEVAL_NOT_APPLICABLE: (OperationFamily.RETRIEVAL, EventOutcome.NOT_APPLICABLE),
    EventType.MEMORY_USE_DECLARED: (OperationFamily.RETRIEVAL, EventOutcome.DECLARED),
    EventType.MEMORY_NOT_USED: (OperationFamily.RETRIEVAL, EventOutcome.NOT_USED),
    EventType.TASK_CLOSED: (OperationFamily.TASK_LIFECYCLE, EventOutcome.CLOSED),
}


def _require_key(value: bytes | bytearray | memoryview) -> bytes:
    key = bytes(value)
    if len(key) < 16:
        raise EngagementTelemetryContractError("correlation key must be at least 16 bytes")
    return key


def _require_project(value: str) -> str:
    project = str(value or "").strip().lower()
    if not _PROJECT_RE.fullmatch(project):
        raise EngagementTelemetryContractError("project scope is invalid")
    return project


def _require_source_identifier(value: str, *, label: str) -> str:
    source = str(value or "").strip()
    if not source:
        raise EngagementTelemetryContractError(f"{label} is required")
    return source


def _digest(*, key: bytes | bytearray | memoryview, domain: str, values: Iterable[str]) -> str:
    material = "\x1f".join((CORRELATION_VERSION, domain, *values)).encode("utf-8")
    return hmac.new(_require_key(key), material, hashlib.sha256).hexdigest()


def project_scope_digest(*, project: str, key: bytes | bytearray | memoryview) -> str:
    """Return a project-only digest; it is not a reusable cross-project key."""

    return _digest(key=key, domain="project-scope", values=(_require_project(project),))


def task_binding_digest(
    *, project: str, task_identifier: str, key: bytes | bytearray | memoryview
) -> str:
    """Derive a project-separated task digest without serializing its source ID."""

    return _digest(
        key=key,
        domain="task-binding",
        values=(_require_project(project), _require_source_identifier(task_identifier, label="task identifier")),
    )


def retrieval_binding_digest(
    *,
    project: str,
    task_identifier: str,
    retrieval_identifier: str,
    key: bytes | bytearray | memoryview,
) -> str:
    """Derive an opaque operation link for retrieval → declared-use evidence."""

    return _digest(
        key=key,
        domain="retrieval-binding",
        values=(
            _require_project(project),
            _require_source_identifier(task_identifier, label="task identifier"),
            _require_source_identifier(retrieval_identifier, label="retrieval identifier"),
        ),
    )


def event_idempotency_key(
    *,
    project: str,
    task_identifier: str,
    event_type: EventType,
    occurrence_token: str,
    key: bytes | bytearray | memoryview,
) -> str:
    """Derive a deterministic idempotency key; the occurrence token is never emitted."""

    return _digest(
        key=key,
        domain="event-idempotency",
        values=(
            _require_project(project),
            _require_source_identifier(task_identifier, label="task identifier"),
            EventType(event_type).value,
            _require_source_identifier(occurrence_token, label="occurrence token"),
        ),
    )


def _validate_digest(value: str, *, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _DIGEST_RE.fullmatch(normalized):
        raise ValueError(f"{label} must be a 64-character lowercase hexadecimal digest")
    return normalized


class EngagementTaskBinding(BaseModel):
    """Safe task reference admitted to the future reporting universe."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_scope_digest: str
    task_binding_digest: str

    @field_validator("project_scope_digest", "task_binding_digest")
    @classmethod
    def _digest_only(cls, value: str, info: Any) -> str:
        return _validate_digest(value, label=str(info.field_name))


class EventMetadata(BaseModel):
    """Fixed metadata vocabulary: no free-form text or identifiers are admitted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_family: OperationFamily
    outcome: EventOutcome
    result_bucket: ResultBucket = ResultBucket.UNKNOWN
    failure_category: FailureCategory | None = None


class EngagementEvent(BaseModel):
    """Content-free event shape for a future durable writer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    event_id: str
    event_type: EventType
    occurred_at: datetime
    instrumentation_version: str = Field(min_length=1, max_length=64)
    project_scope_digest: str
    task_binding_digest: str
    retrieval_binding_digest: str | None = None
    metadata: EventMetadata

    @field_validator("event_id", "project_scope_digest", "task_binding_digest")
    @classmethod
    def _digest_only(cls, value: str, info: Any) -> str:
        return _validate_digest(value, label=str(info.field_name))

    @field_validator("retrieval_binding_digest")
    @classmethod
    def _optional_digest_only(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_digest(value, label="retrieval_binding_digest")

    @field_validator("instrumentation_version")
    @classmethod
    def _safe_version(cls, value: str) -> str:
        version = str(value or "").strip()
        if not _VERSION_RE.fullmatch(version):
            raise ValueError("instrumentation_version is invalid")
        return version

    @field_validator("occurred_at")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _event_semantics_are_exact(self) -> "EngagementEvent":
        expected_family, expected_outcome = _EVENT_SEMANTICS[self.event_type]
        if self.metadata.operation_family is not expected_family:
            raise ValueError("event_type and operation_family do not match")
        if self.metadata.outcome is not expected_outcome:
            raise ValueError("event_type and outcome do not match")
        if self.event_type is EventType.RETRIEVAL_BLOCKED and self.metadata.failure_category is None:
            raise ValueError("retrieval_blocked requires a safe failure_category")
        if self.event_type is not EventType.RETRIEVAL_BLOCKED and self.metadata.failure_category is not None:
            raise ValueError("failure_category is permitted only for retrieval_blocked")
        retrieval_correlated = {
            EventType.RETRIEVAL_REQUESTED,
            EventType.RETRIEVAL_COMPLETED,
            EventType.RETRIEVAL_BLOCKED,
            EventType.RETRIEVAL_NOT_APPLICABLE,
            EventType.MEMORY_USE_DECLARED,
            EventType.MEMORY_NOT_USED,
        }
        if self.event_type in retrieval_correlated and self.retrieval_binding_digest is None:
            raise ValueError("retrieval-correlated event requires retrieval_binding_digest")
        if self.event_type not in retrieval_correlated and self.retrieval_binding_digest is not None:
            raise ValueError("retrieval_binding_digest is forbidden for non-retrieval event")
        return self


def serialize_event(event: EngagementEvent) -> dict[str, Any]:
    """Return the only admitted payload form; Pydantic has already rejected extras."""

    return event.model_dump(mode="json")


def deduplicate_events(events: Sequence[EngagementEvent]) -> tuple[EngagementEvent, ...]:
    """Ignore byte-equivalent retries, reject conflicts that reuse an idempotency key."""

    unique: dict[str, EngagementEvent] = {}
    for event in events:
        existing = unique.get(event.event_id)
        if existing is None:
            unique[event.event_id] = event
            continue
        if serialize_event(existing) != serialize_event(event):
            raise EngagementTelemetryContractError("conflicting events share an idempotency key")
    return tuple(unique[key] for key in sorted(unique))


def _step(*, eligible: int, proven: int, **terminal_counts: int) -> dict[str, int]:
    terminal_total = sum(terminal_counts.values())
    accounted = max(proven, terminal_total)
    unknown = max(eligible - accounted, 0)
    return {
        "eligible": eligible,
        "proven": proven,
        **terminal_counts,
        "event_absent_unknown": unknown,
        "unknown": unknown,
    }


def _exclusive_terminal_step(
    *,
    eligible: int,
    event_types_by_task: Iterable[set[EventType]],
    labels: dict[EventType, str],
) -> dict[str, int]:
    counts = {label: 0 for label in labels.values()}
    counts["mixed"] = 0
    for event_types in event_types_by_task:
        matching = event_types.intersection(labels)
        if len(matching) == 1:
            counts[labels[next(iter(matching))]] += 1
        elif len(matching) > 1:
            counts["mixed"] += 1
    return _step(eligible=eligible, proven=sum(counts.values()), **counts)


def build_engagement_report(
    *,
    project_scope_digest: str,
    eligible_tasks: Sequence[EngagementTaskBinding],
    events: Sequence[EngagementEvent],
) -> dict[str, Any]:
    """Build a report that preserves event absence as ``unknown``.

    The function accepts one project scope only.  A mismatched scope or an event
    without a task in the eligible universe is rejected instead of being joined
    heuristically.
    """

    scope = _validate_digest(project_scope_digest, label="project_scope_digest")
    bindings: set[str] = set()
    for task in eligible_tasks:
        if task.project_scope_digest != scope:
            raise EngagementTelemetryContractError("eligible task has a cross-project binding")
        if task.task_binding_digest in bindings:
            raise EngagementTelemetryContractError("eligible task binding is duplicated")
        bindings.add(task.task_binding_digest)

    unique_events = deduplicate_events(events)
    by_task: dict[str, set[EventType]] = {binding: set() for binding in bindings}
    for event in unique_events:
        if event.project_scope_digest != scope:
            raise EngagementTelemetryContractError("event has a cross-project binding")
        if event.task_binding_digest not in by_task:
            raise EngagementTelemetryContractError("event task binding is not eligible")
        by_task[event.task_binding_digest].add(event.event_type)

    eligible = len(bindings)

    def count(*types: EventType) -> int:
        return sum(bool(event_types.intersection(types)) for event_types in by_task.values())

    return {
        "schema_version": SCHEMA_VERSION,
        "project_scope_digest": scope,
        "eligible_tasks": eligible,
        "dedupe": {"input_events": len(events), "accepted_events": len(unique_events)},
        "funnel": {
            "task_bound": _step(eligible=eligible, proven=count(EventType.TASK_BOUND)),
            "native_interaction_observed": _step(
                eligible=eligible, proven=count(EventType.NATIVE_HEALTH_INVOKED)
            ),
            "preflight_completed": _step(
                eligible=eligible, proven=count(EventType.PREFLIGHT_COMPLETED)
            ),
            "retrieval_requested": _step(
                eligible=eligible, proven=count(EventType.RETRIEVAL_REQUESTED)
            ),
            "retrieval_terminal": _exclusive_terminal_step(
                eligible=eligible,
                event_types_by_task=by_task.values(),
                labels={
                    EventType.RETRIEVAL_COMPLETED: "completed",
                    EventType.RETRIEVAL_BLOCKED: "blocked",
                    EventType.RETRIEVAL_NOT_APPLICABLE: "not_applicable",
                },
            ),
            "memory_use": _exclusive_terminal_step(
                eligible=eligible,
                event_types_by_task=by_task.values(),
                labels={
                    EventType.MEMORY_USE_DECLARED: "declared",
                    EventType.MEMORY_NOT_USED: "not_used",
                },
            ),
            "task_closed": _step(eligible=eligible, proven=count(EventType.TASK_CLOSED)),
        },
        "semantics": {
            "event_absence": "unknown",
            "not_used_requires": EventType.MEMORY_NOT_USED.value,
            "reasoning_quality_proven": False,
            "synthetic_correlation": False,
        },
    }


def source_only_execution_metadata() -> dict[str, bool]:
    """Machine-readable guard for this design-only slice."""

    return {
        "sqlite_write": False,
        "outbox_write": False,
        "runtime_writer": False,
        "mcp_handler_change": False,
        "telemetry_delivery": False,
    }


def canonical_event_json(event: EngagementEvent) -> str:
    """A deterministic representation used only for pure test assertions."""

    return json.dumps(serialize_event(event), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "CORRELATION_VERSION",
    "SCHEMA_VERSION",
    "EngagementEvent",
    "EngagementTaskBinding",
    "EngagementTelemetryContractError",
    "EventMetadata",
    "EventOutcome",
    "EventType",
    "FailureCategory",
    "OperationFamily",
    "ResultBucket",
    "build_engagement_report",
    "canonical_event_json",
    "deduplicate_events",
    "event_idempotency_key",
    "project_scope_digest",
    "retrieval_binding_digest",
    "serialize_event",
    "source_only_execution_metadata",
    "task_binding_digest",
]
