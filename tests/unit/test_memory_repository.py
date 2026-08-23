from __future__ import annotations

import pytest

from blackholememory.domain import Artifact
from blackholememory.domain import Memory
from blackholememory.domain import MemoryLink
from blackholememory.domain import MemoryRevision
from blackholememory.memory_repository import MemoryRevisionConflict
from blackholememory.memory_repository import MemoryRepositoryIntegrityError
from blackholememory.memory_repository import SQLiteMemoryRepository


def _memory(
    *,
    memory_id: str = "mem_bhm_repo_001",
    content: str = "repository contract",
    lifecycle: str = "active",
    updated_at: str = "2026-07-13T10:00:00Z",
    upsert_key: str | None = None,
) -> Memory:
    return Memory.from_record(
        {
            "source_system": "bhm",
            "source_id": memory_id,
            "project": "blackholememory",
            "agent_id": "workspace",
            "memory_type": "architecture",
            "content": content,
            "tags": ["p2.2"],
            "session_refs": [],
            "created_at": "2026-07-13T09:00:00Z",
            "updated_at": updated_at,
            "metadata": {
                "raw_title": "Repository contract",
                "lifecycle": lifecycle,
                "upsert_key": upsert_key,
            },
        }
    )


def test_repository_initializes_wal_and_round_trips_memory(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    memory = _memory()

    first = repository.save_memory(memory)
    second = repository.save_memory(memory)
    restored = repository.get_memory(memory.id, project=memory.project)

    assert first.inserted is True
    assert first.revision_inserted is True
    assert second.inserted is False
    assert second.revision_inserted is False
    assert restored is not None
    assert restored.to_dict() == memory.to_dict()
    assert repository.health().schema_version == 1
    assert repository.health().journal_mode == "wal"
    assert repository.health().quick_check == "ok"


def test_repository_persists_revision_history_and_optimistic_conflict(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    original = _memory()
    repository.save_memory(original)
    revision = MemoryRevision(
        revision_id="rev_bhm_repo_002",
        memory_id=original.id,
        content="repository contract updated",
        content_sha256="",
        created_at="2026-07-13T11:00:00Z",
        created_by="workspace",
    )
    updated = original.model_copy(
        update={
            "current_revision": revision,
            "updated_at": "2026-07-13T11:00:00Z",
        }
    )

    result = repository.save_memory(updated, expected_revision_id=original.current_revision.revision_id)
    restored = repository.get_memory(original.id)

    assert result.revision_inserted is True
    assert restored is not None
    assert restored.current_revision.revision_id == "rev_bhm_repo_002"
    assert restored.current_revision.content == "repository contract updated"

    with pytest.raises(MemoryRevisionConflict, match="expected revision"):
        repository.save_memory(
            original,
            expected_revision_id=original.current_revision.revision_id,
        )


def test_repository_deduplicates_same_memory_content_with_new_revision_id(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    original = _memory()
    first = repository.save_memory(original)
    duplicate = original.model_copy(
        update={
            "current_revision": MemoryRevision(
                revision_id="rev_bhm_repo_duplicate",
                memory_id=original.id,
                content=original.current_revision.content,
                content_sha256="",
                created_at="2026-07-13T11:00:00Z",
            ),
        }
    )

    result = repository.save_memory(duplicate)

    assert result.deduplicated is True
    assert result.revision_inserted is False
    assert result.memory.current_revision.revision_id == original.current_revision.revision_id
    assert result.outbox_event_id == first.outbox_event_id
    assert repository.get_memory(original.id).current_revision.revision_id == original.current_revision.revision_id
    assert len(repository.list_outbox()) == 1


def test_repository_allows_same_content_hash_for_different_memories(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    first = _memory(memory_id="mem_bhm_hash_a")
    second = _memory(memory_id="mem_bhm_hash_b")

    first_result = repository.save_memory(first)
    second_result = repository.save_memory(second)

    assert first_result.revision_inserted is True
    assert second_result.revision_inserted is True
    assert second_result.deduplicated is False
    assert len(repository.list_memories(include_archived=True, include_tombstoned=True)) == 2


def test_repository_targeted_lookup_uses_ids_and_active_upsert_key(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    active = _memory(memory_id="mem_bhm_lookup_active", upsert_key="lookup:key")
    archived = _memory(
        memory_id="mem_bhm_lookup_archived",
        lifecycle="archived",
        upsert_key="lookup:archived",
    )
    unrelated = _memory(memory_id="mem_bhm_lookup_other", upsert_key="other:key")
    repository.save_memories_atomic([active, archived, unrelated])

    selected = repository.get_memories([unrelated.id, active.id], project="blackholememory")

    assert {memory.id for memory in selected} == {active.id, unrelated.id}
    assert repository.get_memories([active.id], project="other-project") == []
    assert repository.get_memory_by_upsert_key("blackholememory", "lookup:key") == active
    assert repository.get_memory_by_upsert_key("blackholememory", "lookup:archived") is None
    assert (
        repository.get_memory_by_upsert_key(
            "blackholememory",
            "lookup:archived",
            include_archived=True,
        )
        == archived
    )


def test_repository_exact_identifier_prefilter_is_project_and_lifecycle_scoped(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    matching = _memory(
        memory_id="mem_bhm_exact_match",
        content="contract_321_anchor is in the canonical content",
    )
    metadata_match = _memory(
        memory_id="mem_bhm_exact_metadata",
        content="ordinary content",
        upsert_key="contract_321_anchor:metadata",
    )
    archived = _memory(
        memory_id="mem_bhm_exact_archived",
        content="contract_321_anchor is archived",
        lifecycle="archived",
    )
    foreign = _memory(
        memory_id="mem_bhm_exact_foreign",
        content="contract_321_anchor belongs elsewhere",
    ).model_copy(update={"project": "other-project"})
    repository.save_memories_atomic([matching, metadata_match, archived, foreign])

    assert repository.find_exact_identifier_candidate_ids(
        "blackholememory", "CONTRACT_321_ANCHOR"
    ) == ["mem_bhm_exact_match", "mem_bhm_exact_metadata"]
    assert repository.find_exact_identifier_candidate_ids("other-project", "contract_321_anchor") == [
        "mem_bhm_exact_foreign"
    ]
    assert repository.find_exact_identifier_candidate_ids("blackholememory", "") == []


def test_repository_atomic_batch_rolls_back_memories_revisions_and_outbox(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    first = _memory(memory_id="mem_bhm_atomic_first")
    collision = _memory(memory_id="mem_bhm_atomic_collision", content="different content").model_copy(
        update={
            "current_revision": MemoryRevision(
                revision_id=first.current_revision.revision_id,
                memory_id="mem_bhm_atomic_collision",
                content="different content",
                content_sha256="",
                created_at="2026-07-13T10:00:00Z",
            )
        }
    )

    with pytest.raises(MemoryRepositoryIntegrityError, match="revision id collision"):
        repository.save_memories_atomic([first, collision])

    assert repository.list_memories(include_archived=True, include_tombstoned=True) == []
    assert repository.list_outbox() == []


def test_repository_atomic_batch_writes_one_outbox_event_per_memory(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    first = _memory(memory_id="mem_bhm_atomic_a")
    second = _memory(memory_id="mem_bhm_atomic_b")

    results = repository.save_memories_atomic([first, second])

    assert [result.memory.id for result in results] == [first.id, second.id]
    assert all(result.inserted for result in results)
    assert len(repository.list_outbox()) == 2


def test_repository_list_materializes_current_revisions_from_one_join(tmp_path, monkeypatch):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    first = _memory(memory_id="mem_bhm_join_a")
    second = _memory(memory_id="mem_bhm_join_b")
    repository.save_memories_atomic([first, second])

    def fail_n_plus_one(*_args, **_kwargs):
        raise AssertionError("list_memories performed a per-row revision lookup")

    monkeypatch.setattr(repository, "_revision_row_to_model", fail_n_plus_one)

    assert {memory.id for memory in repository.list_memories()} == {first.id, second.id}
    assert repository.get_memory(first.id) == first


def test_repository_reuses_revision_without_outbox_collision_for_metadata_update(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    original = _memory()
    first = repository.save_memory(original)
    changed = original.model_copy(
        update={
            "metadata": {"raw_title": "Repository contract", "quality": "reviewed"},
            "updated_at": "2026-07-13T12:00:00Z",
        }
    )

    result = repository.save_memory(changed, expected_revision_id=original.current_revision.revision_id)

    assert result.deduplicated is False
    assert result.revision_inserted is False
    assert result.memory.current_revision.revision_id == original.current_revision.revision_id
    assert result.outbox_event_id != first.outbox_event_id
    assert len(repository.list_outbox()) == 2


def test_repository_rejects_revision_id_collision_even_when_content_differs(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    original = _memory()
    repository.save_memory(original)
    collision = original.model_copy(
        update={
            "current_revision": MemoryRevision(
                revision_id=original.current_revision.revision_id,
                memory_id=original.id,
                content="different content",
                content_sha256="",
                created_at="2026-07-13T12:00:00Z",
            ),
        }
    )

    with pytest.raises(MemoryRepositoryIntegrityError, match="revision id collision"):
        repository.save_memory(collision)


def test_default_listing_hides_archived_and_tombstoned_memories(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    active = _memory(memory_id="mem_bhm_active")
    archived = _memory(memory_id="mem_bhm_archived", lifecycle="archived")
    tombstoned = _memory(memory_id="mem_bhm_tombstoned", lifecycle="purged")
    for item in (active, archived, tombstoned):
        repository.save_memory(item)

    assert [item.id for item in repository.list_memories()] == [active.id]
    assert {item.id for item in repository.list_memories(include_archived=True)} == {
        active.id,
        archived.id,
    }
    assert {
        item.id for item in repository.list_memories(include_archived=True, include_tombstoned=True)
    } == {active.id, archived.id, tombstoned.id}
    assert repository.count_memories() == 1
    assert repository.count_memories(include_archived=True) == 2
    assert repository.count_memories(include_archived=True, include_tombstoned=True) == 3


def test_repository_persists_artifacts_and_links_without_fk_assumptions(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    artifact = Artifact.from_record(
        {
            "id": "checkpoint_bhm_repo_001",
            "project": "blackholememory",
            "memory_id": "mem_bhm_orphan",
            "title": "orphan artifact",
            "created_at": "2026-07-13T09:00:00Z",
        },
        artifact_type="checkpoint",
    )
    link = MemoryLink(
        id="link_bhm_repo_001",
        project="blackholememory",
        source_id="mem_bhm_orphan",
        target_id="mem_bhm_missing",
        relation="depends_on",
        created_at="2026-07-13T09:00:00Z",
        updated_at="2026-07-13T09:00:00Z",
    )

    repository.save_artifact(artifact)
    repository.save_link(link)

    artifacts = repository.list_artifacts(project="blackholememory")
    links = repository.list_links(memory_id="mem_bhm_orphan")
    assert len(artifacts) == 1
    assert artifacts[0].to_dict() == artifact.to_dict()
    assert len(links) == 1
    assert links[0].to_dict() == link.to_dict()


def test_repository_replaces_and_deletes_complete_link_snapshot_atomically(tmp_path):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    first = MemoryLink(
        id="link_bhm_snapshot_001",
        project="blackholememory",
        source_id="mem_bhm_one",
        target_id="mem_bhm_two",
        relation="depends_on",
    )
    second = MemoryLink(
        id="link_bhm_snapshot_002",
        project="blackholememory",
        source_id="mem_bhm_two",
        target_id="mem_bhm_three",
        relation="supports",
    )
    repository.save_link(first)

    assert repository.replace_links([second]) == [second]
    assert [link.id for link in repository.list_links(project="blackholememory")] == [second.id]
    assert repository.delete_links([second.id]) == 1
    assert repository.delete_links([second.id]) == 0
    assert repository.list_links(project="blackholememory") == []
