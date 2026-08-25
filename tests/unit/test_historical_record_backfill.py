from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from blackholememory.domain import Memory
from blackholememory.historical_record_backfill import HistoricalRecordBackfillError
from blackholememory.historical_record_backfill import apply_historical_record_backfill
from blackholememory.historical_record_backfill import build_historical_record_backfill_plan
from blackholememory.memory_class_migration import apply_migration as apply_memory_class_migration
from blackholememory.memory_class_migration import build_migration_plan as build_memory_class_migration_plan
from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.freshness_migration import apply_migration as apply_freshness
from blackholememory.freshness_migration import build_migration_plan as build_freshness_plan


def _memory(memory_id: str, key: str, content: str) -> Memory:
    return Memory.from_record(
        {
            "source_id": memory_id,
            "project": "blackholememory",
            "memory_type": "workflow",
            "upsert_key": key,
            "content": content,
            "created_at": "2026-08-25T12:00:00Z",
            "updated_at": "2026-08-25T12:00:00Z",
            "metadata": {"raw_title": content[:40]},
        }
    )


def _typed_repository(tmp_path: Path) -> tuple[SQLiteMemoryRepository, Path, Path]:
    database = tmp_path / "memories.sqlite3"
    repository = SQLiteMemoryRepository(database)
    repository.save_memory(_memory("mem_bhm_checkpoint", "checkpoint:blackholememory:workflow:fixture", "checkpoint trace"))
    repository.save_memory(_memory("mem_bhm_session", "session-record:blackholememory:fixture", "session trace"))
    freshness_backup = tmp_path / "freshness.sqlite3"
    shutil.copy2(database, freshness_backup)
    freshness_plan = build_freshness_plan(database, freshness_backup, as_of="2026-08-25T12:01:00Z")
    apply_freshness(
        database,
        freshness_backup,
        freshness_plan,
        expected_plan_digest=freshness_plan["plan_digest"],
        confirm_operator=True,
        offline_verified=True,
    )
    typed_backup = tmp_path / "typed.sqlite3"
    shutil.copy2(database, typed_backup)
    typed_plan = build_memory_class_migration_plan(database, typed_backup)
    apply_memory_class_migration(
        database,
        typed_backup,
        typed_plan,
        expected_plan_digest=typed_plan["plan_digest"],
        confirm_operator=True,
        offline_verified=True,
    )
    recovery_backup = tmp_path / "recovery.sqlite3"
    shutil.copy2(database, recovery_backup)
    return SQLiteMemoryRepository(database), database, recovery_backup


def test_plan_is_read_only_and_apply_updates_only_historical_classification(tmp_path: Path) -> None:
    repository, database, backup = _typed_repository(tmp_path)
    before_checkpoint = repository.get_memory("mem_bhm_checkpoint", project="blackholememory")
    before_session = repository.get_memory("mem_bhm_session", project="blackholememory")
    assert before_checkpoint is not None and before_session is not None
    before_bytes = database.read_bytes()

    plan = build_historical_record_backfill_plan(database, backup)

    assert database.read_bytes() == before_bytes
    assert plan["execution"]["read_only"] is True
    assert plan["summary"]["target_count"] == 2
    with pytest.raises(HistoricalRecordBackfillError, match="operator confirmation"):
        apply_historical_record_backfill(database, backup, plan, expected_plan_digest=plan["plan_digest"])

    result = apply_historical_record_backfill(
        database,
        backup,
        plan,
        expected_plan_digest=plan["plan_digest"],
        confirm_operator=True,
        offline_verified=True,
    )

    assert result["target_count"] == len(result["outbox_event_ids"]) == 2
    after_checkpoint = repository.get_memory("mem_bhm_checkpoint", project="blackholememory")
    after_session = repository.get_memory("mem_bhm_session", project="blackholememory")
    assert after_checkpoint is not None and after_session is not None
    for before, after, kind in (
        (before_checkpoint, after_checkpoint, "checkpoint"),
        (before_session, after_session, "session-record"),
    ):
        assert after.current_revision.revision_id == before.current_revision.revision_id
        assert after.current_revision.content == before.current_revision.content
        assert after.memory_class.value == "episodic"
        assert after.event_role.value == "trace"
        assert after.metadata["artifact_kind"] == kind
        assert after.metadata["historical_record_backfill"]["plan_digest"] == plan["plan_digest"]


def test_apply_fails_closed_when_exact_target_set_already_changed(tmp_path: Path) -> None:
    _repository, database, backup = _typed_repository(tmp_path)
    plan = build_historical_record_backfill_plan(database, backup)
    apply_historical_record_backfill(
        database,
        backup,
        plan,
        expected_plan_digest=plan["plan_digest"],
        confirm_operator=True,
        offline_verified=True,
    )

    with pytest.raises(HistoricalRecordBackfillError, match="changed since plan"):
        apply_historical_record_backfill(
            database,
            backup,
            plan,
            expected_plan_digest=plan["plan_digest"],
            confirm_operator=True,
            offline_verified=True,
        )
