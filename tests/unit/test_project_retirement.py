from __future__ import annotations

import sqlite3

import pytest

from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.project_retirement import ProjectRetirementError
from blackholememory.project_retirement import apply_project_retirement
from blackholememory.project_retirement import preview_project_retirement
from blackholememory.project_retirement import PROJECT_RETIREMENT_ALLOWLIST_ENV
from blackholememory.project_retirement import PROJECT_RETIREMENT_CAPABILITY_ENV


def _memory(memory_id: str, project: str):
    from blackholememory.domain import Memory

    return Memory.from_record(
        {
            "source_system": "test",
            "source_id": memory_id,
            "project": project,
            "agent_id": "test",
            "memory_type": "fact",
            "content": f"retirement fixture {memory_id}",
            "tags": ["fixture"],
            "session_refs": [],
            "created_at": "2026-07-25T10:00:00Z",
            "updated_at": "2026-07-25T10:00:00Z",
            "metadata": {},
        }
    )


def test_preview_is_read_only_and_reports_project_scope(tmp_path):
    database = tmp_path / "memories.sqlite3"
    repository = SQLiteMemoryRepository(database)
    repository.save_memory(_memory("mem_fixture_a", "fixture-project"))
    before = database.read_bytes()

    report = preview_project_retirement(database, "fixture-project")

    assert report["action"] == "preview"
    assert report["counts"]["memories_active_or_archived"] == 1
    assert report["requires_explicit_apply"] is True
    assert database.read_bytes() == before


def test_apply_requires_explicit_allowlist_and_capability(tmp_path, monkeypatch):
    database = tmp_path / "memories.sqlite3"
    repository = SQLiteMemoryRepository(database)
    repository.save_memory(_memory("mem_fixture_a", "fixture-project"))

    with pytest.raises(ProjectRetirementError, match="allowlist"):
        apply_project_retirement(database, "fixture-project", capability="secret")

    monkeypatch.setenv(PROJECT_RETIREMENT_ALLOWLIST_ENV, "fixture-project")
    monkeypatch.setenv(PROJECT_RETIREMENT_CAPABILITY_ENV, "secret")
    result = apply_project_retirement(database, "fixture-project", capability="secret", backup_dir=tmp_path / "backups")

    assert result["action"] == "retired"
    assert result["tombstoned_memory_count"] == 1
    assert result["backup"]["quick_check"] == "ok"
    assert result["rollback"]["available"] is True
    assert result["execution"]["physical_database_unlink"] is False
    restored = repository.get_memory("mem_fixture_a")
    assert restored is not None
    assert restored.lifecycle.value == "tombstoned"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM project_retirement_events").fetchone()[0] == 1


def test_protected_project_cannot_be_applied(tmp_path, monkeypatch):
    database = tmp_path / "memories.sqlite3"
    repository = SQLiteMemoryRepository(database)
    repository.save_memory(_memory("mem_fixture_a", "blackholememory"))
    monkeypatch.setenv(PROJECT_RETIREMENT_ALLOWLIST_ENV, "blackholememory")
    monkeypatch.setenv(PROJECT_RETIREMENT_CAPABILITY_ENV, "secret")

    with pytest.raises(ProjectRetirementError, match="protected"):
        apply_project_retirement(database, "blackholememory", capability="secret")
