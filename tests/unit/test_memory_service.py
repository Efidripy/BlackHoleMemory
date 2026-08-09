from __future__ import annotations

import pytest

import blackholememory.memory_service as memory_service_module
from blackholememory.memory_service import MemoryServiceNotReady
from blackholememory.memory_service import MemoryServiceValidationError
from blackholememory.memory_service import SQLiteMemoryService
from blackholememory.outbox import OutboxStatus
from blackholememory.resource_limits import SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS


def _record(memory_id: str = "mem_bhm_service_001") -> dict:
    return {
        "source_system": "bhm",
        "source_id": memory_id,
        "project": "blackholememory",
        "agent_id": "workspace",
        "memory_type": "architecture",
        "content": "service contract",
        "tags": ["p3.13"],
        "session_refs": [],
        "created_at": "2026-07-13T12:00:00Z",
        "updated_at": "2026-07-13T12:00:00Z",
        "metadata": {"raw_title": "Service contract", "custom": {"keep": True}},
        "custom_top_level": {"source": "test"},
    }


def test_service_roundtrip_preserves_record_shape_and_revision_identity(tmp_path):
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    original = _record()

    service.upsert_records([original])
    loaded = service.load_records()
    service.upsert_records(loaded)

    assert loaded[0]["source_id"] == original["source_id"]
    assert loaded[0]["custom_top_level"] == {"source": "test"}
    assert loaded[0]["metadata"]["revision_id"]
    assert len(service.repository.list_outbox(status=OutboxStatus.PENDING)) == 1


def test_service_normalizes_stale_hash_when_route_changes_content(tmp_path):
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    service.upsert_records([_record()])
    updated = service.load_records()[0]
    old_revision = updated["metadata"]["revision_id"]
    updated["content"] = "service contract updated"
    updated["updated_at"] = "2026-07-13T12:01:00Z"

    service.upsert_records([updated])
    current = service.repository.get_memory("mem_bhm_service_001")

    assert current is not None
    assert current.current_revision.content == "service contract updated"
    assert current.current_revision.revision_id != old_revision


def test_service_tombstone_is_explicit_and_does_not_physical_delete(tmp_path):
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    service.upsert_records([_record()])

    deleted = service.tombstone("mem_bhm_service_001", reason="test delete")

    assert deleted is not None
    assert deleted["metadata"]["lifecycle"] == "tombstoned"
    assert service.repository.get_memory("mem_bhm_service_001") is not None


def test_service_lifecycle_lookup_is_project_scoped(tmp_path):
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    service.upsert_records([_record()])

    assert service.get_record("mem_bhm_service_001", project="other-project") is None
    assert service.tombstone(
        "mem_bhm_service_001",
        project="other-project",
        reason="foreign-project-attempt",
    ) is None
    current = service.repository.get_memory("mem_bhm_service_001", project="blackholememory")

    assert current is not None
    assert current.lifecycle.value == "active"


def test_service_outbox_status_reports_bounded_counts(tmp_path):
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    service.upsert_records([_record()])

    status = service.outbox_status()

    assert status["pending"] == 1
    assert status["failed"] == 0
    assert status["total"] == 1


def test_service_readonly_sqlite_probes_use_registry_busy_timeout(tmp_path, monkeypatch):
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    service.upsert_records([_record()])

    original_connect = memory_service_module.sqlite3.connect
    calls: list[dict[str, object]] = []

    def recording_connect(*args, **kwargs):
        calls.append(dict(kwargs))
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(memory_service_module.sqlite3, "connect", recording_connect)
    service.outbox_status()

    assert calls
    assert all(call.get("timeout") == SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS for call in calls)


def test_service_validates_all_input_before_mutation(tmp_path):
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)

    with pytest.raises(MemoryServiceValidationError, match="duplicate memory id"):
        service.upsert_records([_record(), _record()])

    assert service.repository.list_memories(include_archived=True, include_tombstoned=True) == []


def test_service_missing_authoritative_target_is_not_ready(tmp_path):
    service = SQLiteMemoryService(tmp_path / "missing.sqlite3")

    with pytest.raises(MemoryServiceNotReady, match="does not exist"):
        service.load_records()


def test_service_does_not_initialize_an_empty_authoritative_target(tmp_path):
    path = tmp_path / "empty.sqlite3"
    path.touch()
    service = SQLiteMemoryService(path)

    with pytest.raises(MemoryServiceNotReady, match="schema is missing"):
        service.load_records()


def test_service_rejects_hardlinked_authoritative_target(tmp_path):
    outside = tmp_path / "outside.sqlite3"
    outside.write_bytes(b"do-not-touch")
    target = tmp_path / "memories.sqlite3"
    try:
        target.hardlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    service = SQLiteMemoryService(target, allow_create=True)
    with pytest.raises(MemoryServiceNotReady, match="not ready"):
        service.upsert_records([_record()])
    assert outside.read_bytes() == b"do-not-touch"


def test_service_rejects_reparse_authoritative_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    service = SQLiteMemoryService(linked_parent / "memories.sqlite3", allow_create=True)
    with pytest.raises(MemoryServiceNotReady, match="not ready"):
        service.load_records()
    assert not (outside / "memories.sqlite3").exists()
