from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from blackholememory.domain import Memory
from blackholememory.exact_identifier_index_migration import EXACT_IDENTIFIER_INDEX_CAPABILITY_KEY
from blackholememory.exact_identifier_index_migration import EXACT_IDENTIFIER_INDEX_CAPABILITY_VERSION
from blackholememory.exact_identifier_index_migration import EXACT_IDENTIFIER_INDEX_TABLE
from blackholememory.exact_identifier_index_migration import ExactIdentifierIndexMigrationError
from blackholememory.exact_identifier_index_migration import apply_migration
from blackholememory.exact_identifier_index_migration import build_migration_plan
from blackholememory.freshness_migration import apply_migration as apply_freshness
from blackholememory.freshness_migration import build_migration_plan as build_freshness_plan
from blackholememory.memory_repository import SQLiteMemoryRepository


def _memory(memory_id: str, content: str, *, project: str = "blackholememory", lifecycle: str = "active") -> Memory:
    return Memory.from_record(
        {
            "source_id": memory_id,
            "project": project,
            "memory_type": "knowledge",
            "content": content,
            "lifecycle": lifecycle,
            "created_at": "2026-08-24T10:00:00Z",
            "updated_at": "2026-08-24T10:00:00Z",
            "metadata": {"semantic_type": "knowledge"},
        }
    )


def _sqlite_backup(database: Path, backup: Path) -> None:
    if backup.exists():
        backup.unlink()
    source = sqlite3.connect(database)
    target = sqlite3.connect(backup)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _v2_database(tmp_path: Path) -> tuple[SQLiteMemoryRepository, Path, Path]:
    database = tmp_path / "memory.sqlite3"
    repository = SQLiteMemoryRepository(database)
    repository.initialize()
    backup_v1 = tmp_path / "memory-v1.sqlite3"
    _sqlite_backup(database, backup_v1)
    freshness_plan = build_freshness_plan(database, backup_v1, as_of="2026-08-24T00:00:00Z")
    apply_freshness(
        database,
        backup_v1,
        freshness_plan,
        expected_plan_digest=freshness_plan["plan_digest"],
        confirm_operator=True,
        offline_verified=True,
    )
    backup_v2 = tmp_path / "memory-v2.sqlite3"
    _sqlite_backup(database, backup_v2)
    return repository, database, backup_v2


def test_old_store_is_fail_closed_without_wide_scan(tmp_path: Path) -> None:
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    repository.save_memory(_memory("mem_bhm_old", "contract_301_exact_identifier"))

    assert repository.find_exact_identifier_candidate_ids("blackholememory", "contract_301_exact_identifier") == []


def test_migration_backfills_index_and_preserves_authoritative_state(tmp_path: Path) -> None:
    repository, database, backup = _v2_database(tmp_path)
    repository.save_memories_atomic(
        [
            _memory("mem_bhm_exact_a", "contract_301_exact_identifier"),
            _memory("mem_bhm_exact_metadata", "ordinary text").model_copy(
                update={"metadata": {"nested": {"identifier": "contract_301_metadata_identifier"}}}
            ),
            _memory("mem_bhm_exact_b", "ordinary text", project="other-project"),
            _memory("mem_bhm_exact_c", "contract_301_exact_identifier", lifecycle="archived"),
        ]
    )
    _sqlite_backup(database, backup)
    plan = build_migration_plan(database, backup)

    result = apply_migration(
        database,
        backup,
        plan,
        expected_plan_digest=plan["plan_digest"],
        confirm_operator=True,
        offline_verified=True,
    )

    assert result["expected_index"]["row_count"] > 0
    assert result["database"]["authority_digest"] == plan["database"]["authority_digest"]
    assert repository.find_exact_identifier_candidate_ids("blackholememory", "CONTRACT_301_EXACT_IDENTIFIER") == [
        "mem_bhm_exact_a"
    ]
    assert repository.find_exact_identifier_candidate_ids("other-project", "contract_301_exact_identifier") == []
    assert repository.find_exact_identifier_candidate_ids("blackholememory", "contract_301_metadata_identifier") == [
        "mem_bhm_exact_metadata"
    ]
    with sqlite3.connect(database) as connection:
        marker = connection.execute(
            "SELECT value FROM memory_store_meta WHERE key = ?", (EXACT_IDENTIFIER_INDEX_CAPABILITY_KEY,)
        ).fetchone()
    assert marker == (EXACT_IDENTIFIER_INDEX_CAPABILITY_VERSION,)


def test_save_update_and_tombstone_maintain_derived_index(tmp_path: Path) -> None:
    repository, database, backup = _v2_database(tmp_path)
    repository.save_memory(_memory("mem_bhm_exact_mutable", "contract_301_old_identifier"))
    _sqlite_backup(database, backup)
    plan = build_migration_plan(database, backup)
    apply_migration(
        database, backup, plan, expected_plan_digest=plan["plan_digest"], confirm_operator=True, offline_verified=True
    )
    repository.save_memory(_memory("mem_bhm_exact_mutable", "contract_301_new_identifier"))

    assert repository.find_exact_identifier_candidate_ids("blackholememory", "contract_301_old_identifier") == []
    assert repository.find_exact_identifier_candidate_ids("blackholememory", "contract_301_new_identifier") == [
        "mem_bhm_exact_mutable"
    ]
    repository.tombstone_project("blackholememory")
    assert repository.find_exact_identifier_candidate_ids("blackholememory", "contract_301_new_identifier") == []


def test_migration_failure_rolls_back_schema_and_marker(tmp_path: Path) -> None:
    _repository, database, backup = _v2_database(tmp_path)
    plan = build_migration_plan(database, backup)

    with pytest.raises(ExactIdentifierIndexMigrationError, match="injected"):
        apply_migration(
            database,
            backup,
            plan,
            expected_plan_digest=plan["plan_digest"],
            confirm_operator=True,
            offline_verified=True,
            inject_failure=True,
        )
    with sqlite3.connect(database) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (EXACT_IDENTIFIER_INDEX_TABLE,)
        ).fetchone()
        marker = connection.execute(
            "SELECT value FROM memory_store_meta WHERE key = ?", (EXACT_IDENTIFIER_INDEX_CAPABILITY_KEY,)
        ).fetchone()
    assert table is None
    assert marker is None


def test_lookup_query_plan_uses_token_primary_key_not_content_scan(tmp_path: Path) -> None:
    repository, database, backup = _v2_database(tmp_path)
    repository.save_memory(_memory("mem_bhm_exact_plan", "contract_301_query_plan"))
    _sqlite_backup(database, backup)
    plan = build_migration_plan(database, backup)
    apply_migration(
        database, backup, plan, expected_plan_digest=plan["plan_digest"], confirm_operator=True, offline_verified=True
    )
    with sqlite3.connect(database) as connection:
        details = [
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT m.memory_id FROM memory_identifier_tokens AS t "
                "JOIN memories AS m ON m.memory_id = t.memory_id "
                "WHERE t.project = ? AND t.token = ? AND m.project = t.project AND m.lifecycle = 'active' "
                "ORDER BY t.memory_id LIMIT ?",
                ("blackholememory", "contract_301_query_plan", 20),
            ).fetchall()
        ]
    assert any("memory_identifier_tokens" in detail and "SEARCH" in detail for detail in details)
    assert all("memory_revisions" not in detail for detail in details)
