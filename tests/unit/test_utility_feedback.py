from __future__ import annotations

import hashlib

import pytest

from blackholememory.utility_feedback import UtilityEvent
from blackholememory.utility_feedback import utility_report


def _event(event_id: str, event_type: str, **overrides: object) -> UtilityEvent:
    values = {
        "event_id": event_id,
        "memory_id": "m1",
        "project": "p",
        "actor_id": "agent",
        "event_type": event_type,
        "observed_at": "2026-08-21T00:00:00Z",
        "request_digest": hashlib.sha256(b"request").hexdigest(),
    }
    values.update(overrides)
    return UtilityEvent.model_validate(values)


def test_feedback_aggregation_is_append_only_deterministic_and_non_destructive() -> None:
    events = (_event("1", "accepted"), _event("2", "dismissed"))
    first = utility_report(events, as_of="2026-08-22T00:00:00Z", min_samples=3)
    assert first == utility_report(tuple(reversed(events)), as_of="2026-08-22T00:00:00Z", min_samples=3)
    row = first["rows"][0]
    assert row["sample_count"] == 2
    assert row["uncertainty"] == "high"
    assert row["lifecycle_action"] == "none"
    assert first["execution"]["automatic_tombstone"] is False


def test_duplicate_event_is_idempotent_but_collision_fails_closed() -> None:
    event = _event("1", "used")
    assert utility_report((event, event), as_of="2026-08-22T00:00:00Z")["rows"][0]["sample_count"] == 1
    with pytest.raises(ValueError, match="collision"):
        utility_report((event, _event("1", "accepted")), as_of="2026-08-22T00:00:00Z")


def test_invalid_budget_parameters_reject() -> None:
    with pytest.raises(ValueError, match="half_life"):
        utility_report((), as_of="2026-08-22T00:00:00Z", half_life_days=0)
