from __future__ import annotations

from blackholememory import app_core
from blackholememory import app_ops
from blackholememory import app_runtime
from blackholememory.observation_store import ObservationStore


def _observation(event_id: str, *, project: str, timestamp: str) -> dict:
    return {
        "schemaVersion": "1.0",
        "id": event_id,
        "eventId": event_id,
        "hookType": "activity_rollup_test",
        "sessionId": "session-activity-rollup",
        "correlationId": "task-activity-rollup",
        "project": project,
        "cwd": ".",
        "timestamp": timestamp,
        "ingestedAt": timestamp,
        "source": "pytest",
        "payloadState": "sanitized",
        "sensitivity": "internal",
        "data": {},
        "metadata": {},
    }


def test_observation_activity_rollup_is_scoped_and_metadata_only(tmp_path) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite3")
    store.append(_observation("obs-a", project="blackholememory", timestamp="2026-08-10T01:00:00Z"))
    store.append(_observation("obs-b", project="blackholememory", timestamp="2026-08-10T02:00:00Z"))
    store.append(_observation("obs-c", project="other", timestamp="2026-08-10T03:00:00Z"))

    assert store.activity_rollup(project="blackholememory") == {
        "count": 2,
        "latest": "2026-08-10T02:00:00Z",
    }


def test_agent_activity_rollup_uses_observation_store_aggregate(monkeypatch, tmp_path) -> None:
    store = ObservationStore(tmp_path / "observations.sqlite3")
    store.append(_observation("obs-a", project="blackholememory", timestamp="2026-08-10T04:00:00Z"))
    monkeypatch.setattr(app_core, "_observation_store", lambda: store)
    monkeypatch.setattr(app_core, "_load_checkpoints", lambda: [])
    monkeypatch.setattr(app_ops, "_load_handoffs", lambda: [])
    monkeypatch.setattr(app_runtime, "_load_tasks", lambda: [])
    monkeypatch.setattr(app_core, "_load_session_records", lambda: [])
    monkeypatch.setattr(
        app_core,
        "_load_observations",
        lambda: (_ for _ in ()).throw(AssertionError("full observation load is forbidden")),
    )

    result = app_ops._agent_activity_rollup("blackholememory")

    assert result["counts"]["observations"] == 1
    assert result["latest"]["observations"] == "2026-08-10T04:00:00Z"
