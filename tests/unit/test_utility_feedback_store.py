from __future__ import annotations

import hashlib

import pytest

from blackholememory.memory_service import SQLiteMemoryService
from blackholememory.utility_feedback import UtilityEvent
from blackholememory.utility_feedback_store import append_utility_event
from blackholememory.utility_feedback_store import load_utility_events


def _event(event_id: str = "event-1") -> UtilityEvent:
    return UtilityEvent(
        event_id=event_id,
        memory_id="memory-1",
        project="blackholememory",
        actor_id="agent-1",
        event_type="accepted",
        observed_at="2026-08-23T12:00:00Z",
        request_digest=hashlib.sha256(b"request").hexdigest(),
    )


def test_utility_event_storage_is_idempotent_and_project_scoped(tmp_path) -> None:
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    event = _event()
    first, inserted = append_utility_event(service, event)
    replay, replay_inserted = append_utility_event(service, event)

    assert inserted is True
    assert replay_inserted is False
    assert first == replay
    assert load_utility_events(service, project="blackholememory") == (event,)
    assert load_utility_events(service, project="other-project") == ()


def test_utility_event_id_collision_fails_closed(tmp_path) -> None:
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    append_utility_event(service, _event())
    conflicting = UtilityEvent.model_validate(
        {
            **_event().model_dump(mode="json"),
            "event_type": "dismissed",
        }
    )
    with pytest.raises(Exception, match="immutable artifact id collision"):
        append_utility_event(service, conflicting)
