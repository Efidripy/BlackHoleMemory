from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from blackholememory.domain import Artifact
from blackholememory.domain import Memory
from blackholememory.domain import MemoryLink
from blackholememory.freshness_migration import apply_migration as apply_freshness
from blackholememory.freshness_migration import build_migration_plan as build_freshness_plan
from blackholememory.memory_class_migration import CAPABILITY_KEY
from blackholememory.memory_class_migration import CAPABILITY_VERSION
from blackholememory.memory_class_migration import MEMORY_CLASS_COLUMNS
from blackholememory.memory_class_migration import MEMORY_CLASS_INDEX
from blackholememory.memory_class_migration import MemoryClassMigrationError
from blackholememory.memory_class_migration import apply_migration
from blackholememory.memory_class_migration import build_migration_plan
import blackholememory.memory_class_migration as memory_class_migration
from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.typed_memory_contract import typed_memory_capability_available


def _v2_database(tmp_path: Path) -> tuple[Path, Path]:
    database = tmp_path / "memory.sqlite3"
    repository = SQLiteMemoryRepository(database)
    repository.initialize()
    backup_v1 = tmp_path / "memory-v1.sqlite3"
    shutil.copy2(database, backup_v1)
    freshness_plan = build_freshness_plan(
        database,
        backup_v1,
        as_of="2026-08-22T00:00:00Z",
    )
    apply_freshness(
        database,
        backup_v1,
        freshness_plan,
        expected_plan_digest=freshness_plan["plan_digest"],
        confirm_operator=True,
        offline_verified=True,
    )
    backup_v2 = tmp_path / "memory-v2.sqlite3"
    shutil.copy2(database, backup_v2)
    return database, backup_v2


def _seeded_v2_database(tmp_path: Path, *, invalid_typed_metadata: bool = False) -> tuple[Path, Path]:
    database = tmp_path / "memory.sqlite3"
    repository = SQLiteMemoryRepository(database)
    repository.initialize()
    repository.save_memory(
        Memory.from_record(
            {
                "source_id": "mem_bhm_seed_typed",
                "project": "blackholememory",
                "memory_type": "knowledge",
                "content": "typed seed content",
                "concepts": ["typed", "seed"],
                "created_at": "2026-08-21T10:00:00Z",
                "updated_at": "2026-08-21T10:01:00Z",
                "metadata": {
                    "memory_class": "semantic",
                    "memory_class_source": "review-confirmed",
                    "memory_class_confidence": 0.95,
                    "event_role": "fact",
                    "event_role_version": "1",
                    "semantic_type": "knowledge",
                },
            }
        )
    )
    repository.save_memory(
        Memory.from_record(
            {
                "source_id": "mem_bhm_seed_legacy",
                "project": "other-project",
                "memory_type": "workflow",
                "content": "legacy seed content",
                "created_at": "2026-08-21T11:00:00Z",
                "updated_at": "2026-08-21T11:01:00Z",
                "metadata": {"semantic_type": "workflow"},
            }
        )
    )
    repository.save_link(
        MemoryLink(
            id="link_bhm_seed",
            project="blackholememory",
            source_id="mem_bhm_seed_typed",
            target_id="mem_bhm_seed_legacy",
            relation="references",
            created_at="2026-08-21T11:02:00Z",
            updated_at="2026-08-21T11:02:00Z",
        )
    )
    repository.save_artifact(
        Artifact.from_record(
            {
                "id": "checkpoint_bhm_seed",
                "project": "blackholememory",
                "memory_id": "mem_bhm_seed_typed",
                "title": "seed checkpoint",
                "created_at": "2026-08-21T11:03:00Z",
            },
            artifact_type="checkpoint",
        )
    )
    if invalid_typed_metadata:
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE memories SET metadata_json = json_set(metadata_json, '$.memory_class', 'invalid') "
                "WHERE memory_id = 'mem_bhm_seed_typed'"
            )
            connection.commit()

    backup_v1 = tmp_path / "memory-v1.sqlite3"
    shutil.copy2(database, backup_v1)
    freshness_plan = build_freshness_plan(
        database,
        backup_v1,
        as_of="2026-08-22T00:00:00Z",
    )
    apply_freshness(
        database,
        backup_v1,
        freshness_plan,
        expected_plan_digest=freshness_plan["plan_digest"],
        confirm_operator=True,
        offline_verified=True,
    )
    backup_v2 = tmp_path / "memory-v2.sqlite3"
    shutil.copy2(database, backup_v2)
    return database, backup_v2


def _schema(path: Path) -> tuple[int, set[str], set[str], str | None]:
    with sqlite3.connect(path) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(memories)")}
        indexes = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        marker = connection.execute(
            "SELECT value FROM memory_store_meta WHERE key = ?",
            (CAPABILITY_KEY,),
        ).fetchone()
    return version, columns, indexes, str(marker[0]) if marker else None


def test_plan_is_read_only_and_apply_adds_only_capability_schema(tmp_path: Path) -> None:
    database, backup = _v2_database(tmp_path)
    before_bytes = database.read_bytes()

    plan = build_migration_plan(database, backup)

    assert database.read_bytes() == before_bytes
    assert plan["execution"]["sqlite_written"] is False
    result = apply_migration(
        database,
        backup,
        plan,
        expected_plan_digest=plan["plan_digest"],
        confirm_operator=True,
        offline_verified=True,
    )
    version, columns, indexes, marker = _schema(database)
    assert result["action"] == "applied"
    assert version == 2
    assert set(MEMORY_CLASS_COLUMNS).issubset(columns)
    assert MEMORY_CLASS_INDEX in indexes
    assert marker == CAPABILITY_VERSION


def test_apply_requires_operator_offline_and_exact_digest(tmp_path: Path) -> None:
    database, backup = _v2_database(tmp_path)
    plan = build_migration_plan(database, backup)

    with pytest.raises(MemoryClassMigrationError, match="operator confirmation"):
        apply_migration(database, backup, plan, expected_plan_digest=plan["plan_digest"])
    with pytest.raises(MemoryClassMigrationError, match="offline"):
        apply_migration(
            database,
            backup,
            plan,
            expected_plan_digest=plan["plan_digest"],
            confirm_operator=True,
        )
    with pytest.raises(MemoryClassMigrationError, match="digest"):
        apply_migration(
            database,
            backup,
            plan,
            expected_plan_digest="0" * 64,
            confirm_operator=True,
            offline_verified=True,
        )


def test_injected_failure_rolls_back_entire_capability_schema(tmp_path: Path) -> None:
    database, backup = _v2_database(tmp_path)
    plan = build_migration_plan(database, backup)

    with pytest.raises(MemoryClassMigrationError, match="injected"):
        apply_migration(
            database,
            backup,
            plan,
            expected_plan_digest=plan["plan_digest"],
            confirm_operator=True,
            offline_verified=True,
            inject_failure=True,
        )

    version, columns, indexes, marker = _schema(database)
    assert version == 2
    assert not (set(MEMORY_CLASS_COLUMNS) & columns)
    assert MEMORY_CLASS_INDEX not in indexes
    assert marker is None


def test_precommit_postcondition_failure_rolls_back_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database, backup = _v2_database(tmp_path)
    plan = build_migration_plan(database, backup)

    original = memory_class_migration._connection_schema_state

    def fail_schema_state(connection):
        state = original(connection)
        state["capability_version"] = None
        return state

    monkeypatch.setattr(memory_class_migration, "_connection_schema_state", fail_schema_state)
    with pytest.raises(MemoryClassMigrationError, match="capability marker"):
        apply_migration(
            database,
            backup,
            plan,
            expected_plan_digest=plan["plan_digest"],
            confirm_operator=True,
            offline_verified=True,
        )

    version, columns, indexes, marker = _schema(database)
    assert version == 2
    assert not (set(MEMORY_CLASS_COLUMNS) & columns)
    assert MEMORY_CLASS_INDEX not in indexes
    assert marker is None


def test_seeded_migration_preserves_logical_state_and_backfills_valid_metadata(tmp_path: Path) -> None:
    database, backup = _seeded_v2_database(tmp_path)
    backup_digest = memory_class_migration.sha256_file(backup)
    before = memory_class_migration._database_fingerprint(database)
    plan = build_migration_plan(database, backup)

    apply_migration(
        database,
        backup,
        plan,
        expected_plan_digest=plan["plan_digest"],
        confirm_operator=True,
        offline_verified=True,
    )

    after = memory_class_migration._database_fingerprint(database)
    assert after["counts"] == before["counts"]
    assert after["authority_digest"] == before["authority_digest"]
    assert after["logical_digests"] == before["logical_digests"]
    assert memory_class_migration.sha256_file(backup) == backup_digest
    assert typed_memory_capability_available(database) is True
    with sqlite3.connect(database) as connection:
        rows = {
            str(row[0]): tuple(row[1:])
            for row in connection.execute(
                "SELECT memory_id, memory_type, memory_class, memory_class_source, "
                "memory_class_confidence, event_role, event_role_version "
                "FROM memories ORDER BY memory_id"
            )
        }
    assert rows["mem_bhm_seed_typed"] == (
        "knowledge",
        "semantic",
        "review-confirmed",
        0.95,
        "fact",
        "1",
    )
    assert rows["mem_bhm_seed_legacy"] == (
        "workflow",
        "unclassified",
        "legacy-default",
        None,
        "unclassified",
        "1",
    )


def test_invalid_seeded_metadata_fails_closed_and_rolls_back_schema(tmp_path: Path) -> None:
    database, backup = _seeded_v2_database(tmp_path, invalid_typed_metadata=True)
    plan = build_migration_plan(database, backup)

    with pytest.raises(MemoryClassMigrationError, match="invalid memory_class"):
        apply_migration(
            database,
            backup,
            plan,
            expected_plan_digest=plan["plan_digest"],
            confirm_operator=True,
            offline_verified=True,
        )

    version, columns, indexes, marker = _schema(database)
    assert version == 2
    assert not (set(MEMORY_CLASS_COLUMNS) & columns)
    assert MEMORY_CLASS_INDEX not in indexes
    assert marker is None
    assert typed_memory_capability_available(database) is False
