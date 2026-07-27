from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import StrictStr
from pydantic import field_validator
from pydantic import model_validator

from .observation_security import ObservationSensitivity


OBSERVATION_SCHEMA_VERSION = "1.0"
ObservationPayloadState = Literal["raw", "sanitized"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(value: datetime) -> str:
    normalized = value
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=timezone.utc)
    normalized = normalized.astimezone(timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z")


class ObservationIngressV1(BaseModel):
    """Versioned BHM observation ingress contract.

    CamelCase fields are the stable wire format. A pre-validation adapter
    accepts snake_case and explicit v1 aliases without relying on Pydantic
    field aliases that are incompatible with the pinned FastAPI version.
    """

    model_config = ConfigDict(extra="forbid")

    schemaVersion: Literal["1.0"] = OBSERVATION_SCHEMA_VERSION
    eventId: StrictStr | None = None
    hookType: StrictStr = Field(min_length=1)
    sessionId: StrictStr = Field(min_length=1)
    correlationId: StrictStr | None = None
    parentEventId: StrictStr | None = None
    project: StrictStr = Field(min_length=1)
    cwd: StrictStr = ""
    timestamp: datetime | None = None
    source: StrictStr = "hook"
    endpoint: StrictStr | None = None
    payloadState: ObservationPayloadState = "raw"
    sensitivity: ObservationSensitivity = "internal"
    data: Any = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_wire_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value

        normalized = dict(value)
        aliases = {
            "schema_version": "schemaVersion",
            "event_id": "eventId",
            "id": "eventId",
            "eventType": "hookType",
            "event_type": "hookType",
            "session_id": "sessionId",
            "correlation_id": "correlationId",
            "parent_event_id": "parentEventId",
            "occurredAt": "timestamp",
            "occurred_at": "timestamp",
            "payload_state": "payloadState",
            "sensitivity_level": "sensitivity",
            "payload": "data",
        }

        for source, target in aliases.items():
            if source not in normalized:
                continue
            source_value = normalized.pop(source)
            if target in normalized and normalized[target] != source_value:
                raise ValueError(f"conflicting values for {source} and {target}")
            normalized[target] = source_value

        return normalized

    @field_validator("hookType", "sessionId", "project", "source")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("eventId", "correlationId", "parentEventId", "endpoint")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("cwd")
    @classmethod
    def normalize_cwd(cls, value: str) -> str:
        return value.strip()

def build_observation_record(
    request: ObservationIngressV1,
    *,
    endpoint: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a durable v1-compatible record without duplicating payload data."""

    ingested_at = now or _utc_now()
    occurred_at = request.timestamp or ingested_at
    event_id = request.eventId or f"obs_bhm_{uuid.uuid4().hex}"
    correlation_id = request.correlationId or request.sessionId

    record = request.model_dump(mode="json", exclude_none=True)
    record["schemaVersion"] = OBSERVATION_SCHEMA_VERSION
    record["id"] = event_id
    record["eventId"] = event_id
    record["correlationId"] = correlation_id
    record["timestamp"] = _utc_iso(occurred_at)
    record["ingestedAt"] = _utc_iso(ingested_at)

    resolved_endpoint = endpoint or request.endpoint
    if resolved_endpoint:
        record["endpoint"] = resolved_endpoint

    return record
