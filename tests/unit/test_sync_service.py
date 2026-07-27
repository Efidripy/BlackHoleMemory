from __future__ import annotations

import pytest

from blackholememory.domain import Memory
from blackholememory.domain import MemoryRevision
from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.sync_service import MemoryAlreadyExists
from blackholememory.sync_service import MemoryLifecycleService
from blackholememory.sync_service import UndoWindowExpired


def _memory(*, content: str = "sync contract", memory_id: str = "mem_bhm_sync_001") -> Memory:
    return Memory.from_record(
        {
            "source_system": "bhm",
            "source_id": memory_id,
            "project": "blackholememory",
            "agent_id": "workspace",
            "memory_type": "architecture",
            "content": content,
            "tags": ["p2.5"],
            "session_refs": [],
            "created_at": "2026-07-13T13:00:00Z",
            "updated_at": "2026-07-13T13:00:00Z",
            "metadata": {"raw_title": "Sync contract"},
        }
    )


def test_create_update_archive_and_tombstone_are_event_backed(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    service = MemoryLifecycleService(repository)
    original = _memory()

    created = service.create(original)
    with pytest.raises(MemoryAlreadyExists):
        service.create(original)

    updated = original.model_copy(
        update={
            "current_revision": MemoryRevision(
                revision_id="rev_bhm_sync_002",
                memory_id=original.id,
                content="sync contract updated",
                content_sha256="",
                created_at="2026-07-13T13:05:00Z",
                created_by="workspace",
            ),
            "updated_at": "2026-07-13T13:05:00Z",
        }
    )
    changed = service.update(updated, expected_revision_id=original.current_revision.revision_id)
    archived = service.archive(changed.memory, reason="phase close")
    tombstoned = service.delete(archived.memory, reason="bounded test tombstone")

    assert created.action == "created"
    assert changed.action == "updated"
    assert archived.memory.lifecycle.value == "archived"
    assert tombstoned.memory.lifecycle.value == "tombstoned"
    assert [event.event_type for event in repository.list_outbox()] == [
        "memory.created",
        "memory.updated",
        "memory.archived",
        "memory.tombstoned",
    ]
    assert repository.list_memories() == []


def test_upsert_reports_revision_content_deduplication(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    service = MemoryLifecycleService(repository)
    original = _memory()
    created = service.create(original)
    duplicate = original.model_copy(
        update={
            "current_revision": MemoryRevision(
                revision_id="rev_bhm_sync_duplicate",
                memory_id=original.id,
                content=original.current_revision.content,
                content_sha256="",
                created_at="2026-07-13T13:05:00Z",
            ),
        }
    )

    result = service.upsert(duplicate)

    assert result.deduplicated is True
    assert result.revision_inserted is False
    assert result.outbox_event_id == created.outbox_event_id
    assert len(repository.list_outbox()) == 1


def test_tombstone_can_be_restored_only_inside_bounded_window(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    now = ["2026-07-13T13:00:00Z"]
    service = MemoryLifecycleService(
        repository,
        undo_window_seconds=60,
        clock=lambda: now[0],
    )
    original = _memory()
    service.create(original)
    tombstoned = service.delete(original, reason="mistake")
    now[0] = "2026-07-13T13:00:30Z"

    restored = service.restore(tombstoned.memory, reason="undo mistake")

    assert restored.action == "restored"
    assert restored.memory.lifecycle.value == "active"
    assert repository.get_memory(original.id).lifecycle.value == "active"
    assert [event.event_type for event in repository.list_outbox()] == [
        "memory.created",
        "memory.tombstoned",
        "memory.restored",
    ]


def test_expired_tombstone_is_not_restored(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    now = ["2026-07-13T13:00:00Z"]
    service = MemoryLifecycleService(repository, undo_window_seconds=60, clock=lambda: now[0])
    original = _memory()
    service.create(original)
    tombstoned = service.delete(original)
    now[0] = "2026-07-13T13:02:00Z"

    with pytest.raises(UndoWindowExpired):
        service.restore(tombstoned.memory)

    assert repository.get_memory(original.id).lifecycle.value == "tombstoned"


def test_restore_preserves_archived_state_before_tombstone(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    now = ["2026-07-13T13:00:00Z"]
    service = MemoryLifecycleService(repository, undo_window_seconds=60, clock=lambda: now[0])
    original = _memory()
    service.create(original)
    archived = service.archive(original, reason="keep but hide")
    tombstoned = service.delete(archived.memory, reason="temporary removal")
    now[0] = "2026-07-13T13:00:30Z"

    restored = service.restore(tombstoned.memory)

    assert restored.memory.lifecycle.value == "archived"
    assert repository.list_memories() == []
    assert repository.list_memories(include_archived=True)[0].lifecycle.value == "archived"
