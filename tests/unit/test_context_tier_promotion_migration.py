from __future__ import annotations

import shutil

import pytest

from blackholememory.context_tier_promotion_migration import ContextTierPromotionMigrationError
from blackholememory.context_tier_promotion_migration import apply_context_tier_promotion_migration
from blackholememory.context_tier_promotion_migration import build_context_tier_promotion_migration_plan
from blackholememory.context_tier_promotion_migration import context_tier_promotion_migration_status
from blackholememory.domain import Memory
from blackholememory.memory_repository import SQLiteMemoryRepository


def _seed(repository: SQLiteMemoryRepository) -> None:
    repository.save_memory(
        Memory.from_record(
            {
                "source_system": "bhm",
                "source_id": "mem_bhm_seed",
                "project": "migration-project",
                "memory_type": "fact",
                "content": "seed memory",
                "created_at": "2026-08-28T10:00:00Z",
                "updated_at": "2026-08-28T10:00:00Z",
                "metadata": {"raw_title": "seed"},
            }
        )
    )


def test_promotion_schema_requires_sealed_plan_backup_and_offline_gate(tmp_path) -> None:
    database = tmp_path / "memory.sqlite3"
    repository = SQLiteMemoryRepository(database)
    _seed(repository)
    backup = tmp_path / "backup.sqlite3"
    shutil.copy2(database, backup)
    plan = build_context_tier_promotion_migration_plan(database, backup)

    with pytest.raises(ContextTierPromotionMigrationError, match="explicit confirmation"):
        apply_context_tier_promotion_migration(database, backup, plan, expected_plan_digest=plan["plan_digest"])
    with pytest.raises(ContextTierPromotionMigrationError, match="injected"):
        apply_context_tier_promotion_migration(database, backup, plan, expected_plan_digest=plan["plan_digest"], confirm_operator=True, offline_verified=True, inject_failure=True)
    assert context_tier_promotion_migration_status(database)["ready"] is False

    result = apply_context_tier_promotion_migration(database, backup, plan, expected_plan_digest=plan["plan_digest"], confirm_operator=True, offline_verified=True)
    assert result["action"] == "applied"
    assert context_tier_promotion_migration_status(database)["ready"] is True
    assert repository.get_memory("mem_bhm_seed", project="migration-project") is not None
    # A status probe must close its read-only SQLite handle on Windows.
    backup.unlink()
