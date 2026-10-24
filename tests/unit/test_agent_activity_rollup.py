from __future__ import annotations

from blackholememory import app_core_hook, app_ops_admin, app_ops_task2, app_runtime
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
    monkeypatch.setattr(app_core_hook, "_observation_store", lambda: store)
    monkeypatch.setattr(app_core_hook, "_load_checkpoints", lambda: [])
    monkeypatch.setattr(app_ops_task2, "_load_handoffs", lambda: [])
    monkeypatch.setattr(app_runtime, "_load_tasks", lambda: [])
    monkeypatch.setattr(app_core_hook, "_load_session_records", lambda: [])
    monkeypatch.setattr(
        app_core_hook,
        "_load_observations",
        lambda: (_ for _ in ()).throw(AssertionError("full observation load is forbidden")),
    )

    result = app_ops_admin._agent_activity_rollup("blackholememory")

    assert result["counts"]["observations"] == 1
    assert result["latest"]["observations"] == "2026-08-10T04:00:00Z"


def test_activity_rollup_matches_the_joined_reference_with_and_without_purged_rows(tmp_path) -> None:
    import sqlite3

    store = ObservationStore(tmp_path / "observations.sqlite3")
    for event_id, project, timestamp in (
        ("obs-1", "alpha", "2026-08-10T01:00:00Z"),
        ("obs-2", "alpha", "2026-08-10T02:00:00Z"),
        ("obs-3", "alpha", "2026-08-10T03:00:00Z"),
        ("obs-4", "beta", "2026-08-10T04:00:00Z"),
    ):
        store.append(_observation(event_id, project=project, timestamp=timestamp))
    with sqlite3.connect(store.path) as connection:  # stored ids are project-scoped (alphaobs-2)
        full_id = {name: connection.execute("SELECT event_id FROM observation_events WHERE event_id LIKE ?", (f"%{name}",)).fetchone()[0] for name in ("obs-2", "obs-3")}
    store.archive([full_id["obs-2"]], archive_reason="test")

    def reference(project: str | None) -> dict:
        clause, params = ("AND e.project = ?", [project]) if project else ("", [])
        with sqlite3.connect(store.path) as connection:
            count, latest = connection.execute(
                "SELECT COUNT(*), COALESCE(MAX(NULLIF(e.occurred_at, '')), '') FROM observation_events e "
                "JOIN observation_state s ON s.event_id = e.event_id WHERE s.lifecycle <> 'purged' " + clause,
                params,
            ).fetchone()
        return {"count": count, "latest": latest}

    for project in ("alpha", "beta", None):  # archived rows still count; nothing purged yet (fast path)
        assert store.activity_rollup(project=project) == reference(project)
    assert store.activity_rollup(project="alpha")["count"] == 3

    with sqlite3.connect(store.path) as connection:  # purged rows must drop out of count and latest (joined path)
        connection.execute("UPDATE observation_state SET lifecycle = 'purged' WHERE event_id = ?", (full_id["obs-3"],))
    for project in ("alpha", "beta", None):
        assert store.activity_rollup(project=project) == reference(project)
    assert store.activity_rollup(project="alpha") == {"count": 2, "latest": "2026-08-10T02:00:00Z"}
