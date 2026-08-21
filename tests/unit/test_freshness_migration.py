from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from blackholememory.freshness_migration import FreshnessMigrationError
from blackholememory.freshness_migration import MIGRATION_TABLES
from blackholememory.freshness_migration import apply_migration
from blackholememory.freshness_migration import build_migration_plan
from blackholememory.memory_repository import SQLiteMemoryRepository


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    database = tmp_path / "memories.sqlite3"
    SQLiteMemoryRepository(database).initialize()
    backup = tmp_path / "backup.sqlite3"
    with sqlite3.connect(database) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    return database, backup


def _schema(database: Path) -> tuple[int, set[str]]:
    with sqlite3.connect(database) as connection:
        return (
            int(connection.execute("PRAGMA user_version").fetchone()[0]),
            {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")},
        )


def test_plan_is_read_only_and_has_exact_digest(tmp_path: Path) -> None:
    database, backup = _fixture(tmp_path)
    before = database.read_bytes()
    plan = build_migration_plan(database, backup, as_of="2026-08-21T18:00:00Z")
    assert plan["database"]["user_version"] == 1
    assert plan["existing_full_backup"]["quick_check"] == "ok"
    assert plan["expected"]["target_user_version"] == 2
    assert plan["plan_digest"] == hashlib.sha256(
        json.dumps({key: value for key, value in plan.items() if key != "plan_digest"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert database.read_bytes() == before


def test_apply_is_transactional_and_rolls_back_injected_failure(tmp_path: Path) -> None:
    database, backup = _fixture(tmp_path)
    plan = build_migration_plan(database, backup, as_of="2026-08-21T18:00:00Z")
    with pytest.raises(FreshnessMigrationError, match="injected"):
        apply_migration(database, backup, plan, expected_plan_digest=plan["plan_digest"], confirm_operator=True, offline_verified=True, inject_failure=True)
    version, tables = _schema(database)
    assert version == 1
    assert not tables.intersection(MIGRATION_TABLES)


def test_apply_creates_only_additive_schema_and_is_idempotent(tmp_path: Path) -> None:
    database, backup = _fixture(tmp_path)
    plan = build_migration_plan(database, backup, as_of="2026-08-21T18:00:00Z")
    first = apply_migration(database, backup, plan, expected_plan_digest=plan["plan_digest"], confirm_operator=True, offline_verified=True)
    assert first["action"] == "applied"
    assert first["wal_checkpoint"] == [0, 0, 0]
    version, tables = _schema(database)
    assert version == 2
    assert MIGRATION_TABLES.issubset(tables)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0] == 0
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    second = apply_migration(database, backup, plan, expected_plan_digest=plan["plan_digest"], confirm_operator=True, offline_verified=True)
    assert second["action"] == "already-current"


def test_apply_rejects_missing_operator_or_offline_proof(tmp_path: Path) -> None:
    database, backup = _fixture(tmp_path)
    plan = build_migration_plan(database, backup, as_of="2026-08-21T18:00:00Z")
    with pytest.raises(FreshnessMigrationError, match="operator"):
        apply_migration(database, backup, plan, expected_plan_digest=plan["plan_digest"], offline_verified=True)
    with pytest.raises(FreshnessMigrationError, match="offline"):
        apply_migration(database, backup, plan, expected_plan_digest=plan["plan_digest"], confirm_operator=True)
