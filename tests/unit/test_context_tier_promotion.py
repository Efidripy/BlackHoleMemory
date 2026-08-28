from __future__ import annotations

import shutil

import pytest

from blackholememory.context_tier_promotion import ContextTierPromotionDisabled
from blackholememory.context_tier_promotion import ContextTierPromotionStale
from blackholememory.context_tier_promotion import apply_tier_promotion
from blackholememory.context_tier_promotion import build_tier_promotion_plan
from blackholememory.context_tier_promotion import dry_run_tier_promotion
from blackholememory.context_tier_promotion import rollback_tier_promotion
from blackholememory.context_tier_promotion_migration import apply_context_tier_promotion_migration
from blackholememory.context_tier_promotion_migration import build_context_tier_promotion_migration_plan
from blackholememory.domain import Memory
from blackholememory.domain import MemoryRevision
from blackholememory.domain import content_sha256
from blackholememory.memory_repository import SQLiteMemoryRepository


PROJECT = "tier-promotion-project"
SESSION = "session-alpha"


def _memory(memory_id: str, content: str, *, project: str = PROJECT, session: str = SESSION) -> Memory:
    return Memory.from_record(
        {
            "source_system": "bhm",
            "source_id": memory_id,
            "project": project,
            "memory_type": "fact",
            "content": content,
            "created_at": "2026-08-28T10:00:00Z",
            "updated_at": "2026-08-28T10:00:00Z",
            "session_refs": [session],
            "metadata": {"scope": "session", "session_id": session, "raw_title": memory_id},
        }
    )


def _ready_repository(tmp_path):
    database = tmp_path / "memories.sqlite3"
    repository = SQLiteMemoryRepository(database)
    repository.save_memory(_memory("mem_bhm_session", "A session conclusion is durable exactly as written."))
    backup = tmp_path / "backup.sqlite3"
    shutil.copy2(database, backup)
    plan = build_context_tier_promotion_migration_plan(database, backup)
    result = apply_context_tier_promotion_migration(
        database, backup, plan, expected_plan_digest=plan["plan_digest"], confirm_operator=True, offline_verified=True
    )
    assert result["action"] == "applied"
    return repository, database


def _plan(repository: SQLiteMemoryRepository, *, content: str = "A session conclusion is durable exactly as written."):
    return build_tier_promotion_plan(
        repository=repository,
        project=PROJECT,
        session_id=SESSION,
        source_memory_ids=["mem_bhm_session"],
        candidate_content=content,
        title="Durable conclusion",
    )


def test_plan_and_dry_run_are_snapshot_bound_and_non_mutating(tmp_path) -> None:
    repository, _database = _ready_repository(tmp_path)
    plan = _plan(repository)
    before = len(repository.list_outbox())

    result = dry_run_tier_promotion(repository=repository, plan=plan)

    assert result["current"] is True
    assert result["execution"]["sqlite_mutation"] is False
    assert len(repository.list_outbox()) == before
    assert plan["lock"]["state"] == "not_acquired"
    assert "A session conclusion" not in str({key: value for key, value in plan.items() if key != "candidate"})


def test_apply_promotes_same_aggregate_emits_outbox_and_is_idempotent(tmp_path) -> None:
    repository, database = _ready_repository(tmp_path)
    plan = _plan(repository)
    before_outbox = len(repository.list_outbox())

    first = apply_tier_promotion(database_path=database, plan=plan, apply=True, confirmation=plan["candidate_id"], policy_enabled=True)
    second = apply_tier_promotion(database_path=database, plan=plan, apply=True, confirmation=plan["candidate_id"], policy_enabled=True)

    assert first.status == "applied"
    assert first.memory_id == "mem_bhm_session"
    assert first.outbox_event_id
    assert second.idempotent is True and second.memory_id == first.memory_id
    promoted = repository.get_memory("mem_bhm_session", project=PROJECT)
    assert promoted is not None
    assert promoted.current_revision.content == "A session conclusion is durable exactly as written."
    assert promoted.metadata["context_tier"] == "project"
    assert promoted.metadata["context_tier_promotion"]["candidate_id"] == plan["candidate_id"]
    assert len(repository.list_outbox()) == before_outbox + 1


def test_policy_disabled_stale_and_cross_session_all_fail_closed(tmp_path) -> None:
    repository, database = _ready_repository(tmp_path)
    plan = _plan(repository)
    with pytest.raises(ContextTierPromotionDisabled):
        apply_tier_promotion(database_path=database, plan=plan, apply=True, confirmation=plan["candidate_id"], policy_enabled=False)
    source = repository.get_memory("mem_bhm_session", project=PROJECT)
    assert source is not None
    changed_content = "A session conclusion changed after the candidate snapshot."
    repository.save_memory(
        source.model_copy(
            update={
                "updated_at": "2026-08-28T11:00:00Z",
                "current_revision": MemoryRevision(
                    revision_id="rev_bhm_changed",
                    memory_id=source.id,
                    content=changed_content,
                    content_sha256=content_sha256(changed_content),
                    created_at="2026-08-28T11:00:00Z",
                ),
            }
        ),
        expected_revision_id=source.current_revision.revision_id,
    )
    with pytest.raises(ContextTierPromotionStale):
        apply_tier_promotion(database_path=database, plan=plan, apply=True, confirmation=plan["candidate_id"], policy_enabled=True)
    with pytest.raises(ContextTierPromotionStale):
        build_tier_promotion_plan(repository=repository, project=PROJECT, session_id="foreign-session", source_memory_ids=["mem_bhm_session"], candidate_content="A session conclusion is durable exactly as written.", title="x")


def test_exact_content_in_other_active_memory_suppresses_duplicate(tmp_path) -> None:
    repository, database = _ready_repository(tmp_path)
    repository.save_memory(_memory("mem_bhm_existing", "A session conclusion is durable exactly as written.", session="older-session"))
    plan = _plan(repository)
    before = len(repository.list_outbox())

    result = apply_tier_promotion(database_path=database, plan=plan, apply=True, confirmation=plan["candidate_id"], policy_enabled=True)

    assert result.status == "deduplicated" and result.deduplicated is True
    assert result.memory_id == "mem_bhm_existing"
    assert result.outbox_event_id is None
    source = repository.get_memory("mem_bhm_session", project=PROJECT)
    assert source is not None and source.metadata.get("context_tier") is None
    assert len(repository.list_outbox()) == before


def test_explicit_rollback_preserves_revision_and_restores_heuristic_tier(tmp_path) -> None:
    repository, database = _ready_repository(tmp_path)
    plan = _plan(repository)
    applied = apply_tier_promotion(database_path=database, plan=plan, apply=True, confirmation=plan["candidate_id"], policy_enabled=True)
    before = repository.get_memory("mem_bhm_session", project=PROJECT)
    assert before is not None

    result = rollback_tier_promotion(database_path=database, candidate_id=applied.candidate_id, apply=True, confirmation=applied.candidate_id, policy_enabled=True)
    replay = rollback_tier_promotion(database_path=database, candidate_id=applied.candidate_id, apply=True, confirmation=applied.candidate_id, policy_enabled=True)

    restored = repository.get_memory("mem_bhm_session", project=PROJECT)
    assert result.status == "rolled_back" and result.outbox_event_id
    assert replay.idempotent is True
    assert restored is not None
    assert restored.current_revision.revision_id == before.current_revision.revision_id
    assert restored.metadata.get("context_tier") is None
    assert restored.metadata.get("context_tier_promotion") is None
