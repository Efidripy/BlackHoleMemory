"""Typed transactional-outbox event contract used by the SQLite repository."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Self

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator


class OutboxStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class OutboxError(RuntimeError):
    """Base outbox error."""


class OutboxLeaseLost(OutboxError):
    """Raised when a worker tries to ack/fail an expired or foreign lease."""


def _text(value: Any, field_name: str, *, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{field_name} must not be empty")
        return None
    normalized = str(value).strip()
    if not normalized and required:
        raise ValueError(f"{field_name} must not be empty")
    return normalized or None


def _timestamp(value: Any, field_name: str, *, required: bool = True) -> str | None:
    normalized = _text(value, field_name, required=required)
    if normalized is None:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class OutboxEvent(BaseModel):
    """Immutable event snapshot; lease fields are changed by repository methods."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    aggregate_type: str
    aggregate_id: str
    event_type: str
    event_version: int = Field(default=1, ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    status: OutboxStatus = OutboxStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    available_at: str
    claimed_at: str | None = None
    claim_token: str | None = None
    last_error: str | None = None
    created_at: str
    updated_at: str

    @field_validator("event_id", "aggregate_type", "aggregate_id", "event_type", mode="before")
    @classmethod
    def _required_text(cls, value: Any, info: Any) -> str:
        return _text(value, f"outbox.{info.field_name}") or ""

    @field_validator("available_at", "created_at", "updated_at", mode="before")
    @classmethod
    def _required_timestamps(cls, value: Any, info: Any) -> str:
        return _timestamp(value, f"outbox.{info.field_name}") or ""

    @field_validator("claimed_at", mode="before")
    @classmethod
    def _optional_claimed_at(cls, value: Any) -> str | None:
        return _timestamp(value, "outbox.claimed_at", required=False)

    @field_validator("claim_token", "last_error", mode="before")
    @classmethod
    def _optional_text(cls, value: Any) -> str | None:
        normalized = _text(value, "outbox optional value", required=False)
        if normalized and len(normalized) > 2_000:
            return normalized[:2_000]
        return normalized

    @field_validator("payload", mode="before")
    @classmethod
    def _payload_object(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValueError("outbox.payload must be an object")
        return copy.deepcopy(dict(value))

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    def with_lease(self, *, status: OutboxStatus, attempts: int, now: str, claim_token: str | None) -> Self:
        return self.model_copy(
            update={
                "status": status,
                "attempts": attempts,
                "claimed_at": now if status is OutboxStatus.PROCESSING else None,
                "claim_token": claim_token if status is OutboxStatus.PROCESSING else None,
                "updated_at": now,
            }
        )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
