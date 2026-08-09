import sqlite3
from datetime import datetime, timezone

import pytest

from blackholememory.memory_graph import MemoryGraphError
from blackholememory.memory_graph import build_memory_graph
from blackholememory.memory_graph import explain_memory_graph
from blackholememory.memory_graph import query_memory_graph


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _sources():
    records = [
        {
            "source_id": "mem-old",
            "project": "fixture",
            "memory_type": "fact",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "metadata": {"raw_title": "old fact", "source_refs": ["session:s1"]},
        },
        {
            "source_id": "mem-new",
            "project": "fixture",
            "memory_type": "fact",
            "created_at": "2026-02-01T00:00:00Z",
            "updated_at": "2026-02-01T00:00:00Z",
            "metadata": {"raw_title": "new fact", "supersedes": "mem-old", "source_refs": ["adr:1"]},
        },
        {
            "source_id": "mem-invalid",
            "project": "fixture",
            "memory_type": "fact",
            "valid_from": "2026-03-01T00:00:00Z",
            "valid_until": "2026-02-01T00:00:00Z",
            "recorded_at": "2026-03-01T00:00:00Z",
        },
        {"source_id": "mem-other", "project": "other", "memory_type": "fact", "created_at": "2026-01-01T00:00:00Z"},
    ]
    links = [
        {"source_id": "mem-new", "target_id": "mem-old", "relation": "supersedes", "project": "fixture", "valid_from": "2026-02-01T00:00:00Z"},
        {"source_id": "mem-new", "target_id": "missing", "relation": "related_to", "project": "fixture", "valid_from": "2026-02-01T00:00:00Z"},
    ]
    sessions = [{"id": "session-1", "project": "fixture", "created_at": "2026-01-01T00:00:00Z", "metadata": {"source_refs": ["obs:1"]}}]
    return records, links, sessions


def test_memory_graph_build_replay_provenance_and_quarantine(tmp_path):
    records, links, sessions = _sources()
    database = tmp_path / "memory.sqlite3"
    built = build_memory_graph(database, project="fixture", records=records, links=links, session_records=sessions)
    assert built["ok"] is True
    assert built["summary"]["node_count"] == 3
    assert built["summary"]["edge_count"] == 1
    assert built["summary"]["invalid_temporal_count"] == 1
    assert built["summary"]["unresolved_edge_count"] == 1

    before = query_memory_graph(database, project="fixture", operation="as_of", as_of="2026-01-15T00:00:00Z", limit=20)
    after = query_memory_graph(database, project="fixture", operation="as_of", as_of="2026-02-15T00:00:00Z", limit=20)
    assert {item["entity_id"] for item in before["nodes"]} == {"mem-old", "session-1"}
    assert {item["entity_id"] for item in after["nodes"]} == {"mem-old", "mem-new", "session-1"}
    assert before["provenance"]["complete"] is True
    assert all(item.get("provenance") for item in after["nodes"] + after["edges"])

    explained = explain_memory_graph(database, project="fixture", operation="supersession", query="mem-new")
    assert explained["explain"]["reason_codes"]
    assert explained["execution"]["writes_sqlite"] is False


def test_memory_graph_lkg_rollback_and_query_is_read_only(tmp_path):
    records, links, sessions = _sources()
    database = tmp_path / "memory.sqlite3"
    built = build_memory_graph(database, project="fixture", records=records[:2], links=links[:1], session_records=sessions)
    current = query_memory_graph(database, project="fixture")
    with pytest.raises(MemoryGraphError, match="injected publish failure"):
        build_memory_graph(database, project="fixture", records=records[:1], links=(), session_records=sessions, fail_after_stage="before_publish")
    after = query_memory_graph(database, project="fixture")
    assert current["snapshot_id"] == built["snapshot_id"] == after["snapshot_id"]
    assert current["response_digest"] == after["response_digest"]
    with sqlite3.connect(database) as connection:
        snapshot_count = connection.execute("SELECT COUNT(*) FROM memory_graph_snapshots").fetchone()[0]
    assert snapshot_count == 1


def test_memory_graph_is_project_scoped_and_bounded(tmp_path):
    database = tmp_path / "memory.sqlite3"
    build_memory_graph(
        database,
        project="fixture",
        records=[{"source_id": "m1", "project": "fixture", "created_at": "2026-01-01T00:00:00Z"}, {"source_id": "m2", "project": "other", "created_at": "2026-01-01T00:00:00Z"}],
    )
    result = query_memory_graph(database, project="fixture", operation="search", query="m2", limit=8)
    assert result["nodes"] == []
    assert result["budget"]["within_time_budget"] is True


def test_memory_graph_build_rejects_hardlinked_target(tmp_path):
    outside = tmp_path / "outside.sqlite3"
    outside.write_bytes(b"do-not-touch")
    target = tmp_path / "memory.sqlite3"
    try:
        target.hardlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(OSError, match="hardlink"):
        build_memory_graph(target, project="fixture")
    assert outside.read_bytes() == b"do-not-touch"


def test_memory_graph_build_rejects_reparse_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(OSError, match="symlink|junction|reparse"):
        build_memory_graph(linked_parent / "memory.sqlite3", project="fixture")
    assert not (outside / "memory.sqlite3").exists()
