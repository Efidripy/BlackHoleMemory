from __future__ import annotations

import pytest

import blackholememory.memory_service as memory_service_module
from blackholememory.domain import Artifact
from blackholememory.memory_service import MemoryServiceNotReady
from blackholememory.memory_service import MemoryServiceValidationError
from blackholememory.memory_service import SQLiteMemoryService
from blackholememory.outbox import OutboxStatus
from blackholememory.resource_limits import SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS


def _record(
    memory_id: str = "mem_bhm_service_001",
    *,
    upsert_key: str | None = None,
) -> dict:
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
        "metadata": {
            "raw_title": "Service contract",
            "custom": {"keep": True},
            "upsert_key": upsert_key,
        },
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


def test_service_targeted_record_lookups_do_not_require_full_store_load(tmp_path, monkeypatch):
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    first = _record("mem_bhm_targeted_a", upsert_key="targeted:a")
    second = _record("mem_bhm_targeted_b", upsert_key="targeted:b")
    service.upsert_records([first, second])

    monkeypatch.setattr(
        service,
        "load_records",
        lambda: (_ for _ in ()).throw(AssertionError("full store load used")),
    )

    assert service.get_record("mem_bhm_targeted_a")["source_id"] == "mem_bhm_targeted_a"
    assert {item["source_id"] for item in service.get_records(["mem_bhm_targeted_b"])} == {
        "mem_bhm_targeted_b"
    }
    assert service.get_record_by_upsert_key("blackholememory", "targeted:a")["source_id"] == (
        "mem_bhm_targeted_a"
    )


def test_service_list_records_is_newest_first_and_project_scoped(tmp_path):
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    older = _record("mem_bhm_old")
    newer = _record("mem_bhm_new")
    newer["updated_at"] = "2026-07-14T12:00:00Z"
    foreign = _record("mem_bhm_foreign")
    foreign["project"] = "other-project"
    foreign["updated_at"] = "2026-07-15T12:00:00Z"
    service.upsert_records([older, newer, foreign])

    records = service.list_records(project="blackholememory", limit=None)

    assert [record["source_id"] for record in records] == ["mem_bhm_new", "mem_bhm_old"]
    assert service.list_projects() == ["other-project", "blackholememory"]


def test_service_all_mode_walks_every_bounded_repository_page(tmp_path, monkeypatch):
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    service.repository.initialize()
    calls: list[tuple[int, int]] = []

    class FakeMemory:
        def __init__(self, index: int) -> None:
            self.index = index
            self.lifecycle = type("LifecycleValue", (), {"value": "active"})()

        def to_record(self) -> dict:
            return {"source_id": f"mem-{self.index}"}

    def fake_list_memories(**kwargs):
        limit = int(kwargs["limit"])
        offset = int(kwargs["offset"])
        calls.append((limit, offset))
        remaining = max(0, 10_001 - offset)
        return [FakeMemory(offset + index) for index in range(min(limit, remaining))]

    monkeypatch.setattr(service.repository, "list_memories", fake_list_memories)

    records = service.list_records(limit=None)

    assert len(records) == 10_001
    assert calls == [(10_000, 0), (10_000, 10_000)]


def test_service_artifact_roundtrip_is_project_scoped_and_paged(tmp_path):
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    service.save_artifact(
        Artifact(
            id="artifact-primary",
            artifact_type="ontology_registry",
            project="blackholememory",
            created_at="2026-08-23T12:00:00Z",
            payload={"schema_digest": "a" * 64},
        )
    )
    service.save_artifact(
        Artifact(
            id="artifact-foreign",
            artifact_type="ontology_registry",
            project="other-project",
            created_at="2026-08-23T12:01:00Z",
            payload={"schema_digest": "b" * 64},
        )
    )

    assert service.list_artifact_records(
        artifact_type="ontology_registry",
        project="blackholememory",
        limit=1,
    ) == [{
        "id": "artifact-primary",
        "project": "blackholememory",
        "memory_id": None,
        "created_at": "2026-08-23T12:00:00Z",
        "updated_at": None,
        "schema_digest": "a" * 64,
    }]


def test_service_append_artifact_is_idempotent_but_rejects_a_different_collision(tmp_path):
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    artifact = Artifact(
        id="audit-event",
        artifact_type="shared_memory_policy_audit",
        project="blackholememory",
        created_at="2026-08-23T12:00:00Z",
        updated_at="2026-08-23T12:00:00Z",
        payload={"decision": "deny"},
    )

    first, first_inserted = service.append_artifact(artifact)
    second, second_inserted = service.append_artifact(artifact)

    assert first_inserted is True
    assert second_inserted is False
    assert first == second
    with pytest.raises(Exception, match="immutable artifact id collision"):
        service.append_artifact(
            artifact.model_copy(update={"payload": {"decision": "allow"}})
        )


def test_service_bulk_upsert_rolls_back_on_second_outbox_failure(tmp_path, monkeypatch):
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    original_append = service.repository._append_memory_event
    calls = 0

    def fail_second(connection, memory, *, inserted):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic second-item failure")
        return original_append(connection, memory, inserted=inserted)

    monkeypatch.setattr(service.repository, "_append_memory_event", fail_second)

    with pytest.raises(RuntimeError, match="second-item failure"):
        service.upsert_records([_record("mem_bhm_rollback_a"), _record("mem_bhm_rollback_b")])

    assert service.repository.list_memories(include_archived=True, include_tombstoned=True) == []
    assert service.repository.list_outbox() == []


def test_service_bulk_noop_does_not_append_revisions_or_outbox(tmp_path):
    service = SQLiteMemoryService(tmp_path / "memories.sqlite3", allow_create=True)
    records = [_record("mem_bhm_noop_a"), _record("mem_bhm_noop_b")]
    service.upsert_records(records)
    initial = service.load_records()
    initial_revisions = {
        item["source_id"]: item["metadata"]["revision_id"]
        for item in initial
    }

    service.upsert_records(initial)
    repeated = service.load_records()

    assert {
        item["source_id"]: item["metadata"]["revision_id"]
        for item in repeated
    } == initial_revisions
    assert len(service.repository.list_outbox()) == 2


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
