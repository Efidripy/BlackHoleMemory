"""SQLite-authoritative durable utility-feedback event adapter."""

from __future__ import annotations

from typing import Any, Mapping

from .domain import Artifact
from .utility_feedback import SCHEMA_VERSION
from .utility_feedback import UtilityEvent


ARTIFACT_TYPE = "utility_feedback_event"


def build_utility_event_artifact(event: UtilityEvent) -> Artifact:
    """Encode one immutable event; no score or lifecycle decision is stored."""

    return Artifact(
        id=f"utility_feedback_{event.project}_{event.event_id}",
        artifact_type=ARTIFACT_TYPE,
        project=event.project,
        created_at=event.observed_at,
        updated_at=event.observed_at,
        payload={
            "schema_version": SCHEMA_VERSION,
            "event": event.model_dump(mode="json"),
        },
    )


def utility_event_from_artifact(record: Mapping[str, Any], *, project: str) -> UtilityEvent:
    if str(record.get("project") or "") != project:
        raise ValueError("utility event project mismatch")
    payload = record.get("event")
    if not isinstance(payload, Mapping):
        raise ValueError("utility event artifact payload is invalid")
    event = UtilityEvent.model_validate(payload)
    if event.project != project:
        raise ValueError("utility event project mismatch")
    return event


def append_utility_event(service: Any, event: UtilityEvent) -> tuple[dict[str, Any], bool]:
    return service.append_artifact(build_utility_event_artifact(event))


def load_utility_events(service: Any, *, project: str, limit: int = 10_000) -> tuple[UtilityEvent, ...]:
    records = service.list_artifact_records(
        artifact_type=ARTIFACT_TYPE,
        project=project,
        limit=max(1, min(int(limit), 10_000)),
    )
    events: list[UtilityEvent] = []
    for record in records:
        events.append(utility_event_from_artifact(record, project=project))
    return tuple(events)


__all__ = [
    "ARTIFACT_TYPE",
    "append_utility_event",
    "build_utility_event_artifact",
    "load_utility_events",
    "utility_event_from_artifact",
]
