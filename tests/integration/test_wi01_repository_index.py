from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import SQLiteRepositoryIndexStore
from blackholememory.repository_index import index_repository


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_repository_index_coexists_with_canonical_memory_store(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("def run():\n    return 'ok'\n", encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    database = tmp_path / "memories.sqlite3"
    memory = SQLiteMemoryRepository(database)
    memory.initialize()

    result = index_repository(
        root,
        database,
        project="demo",
        source=RepositorySourceProvenance(owner="fixture"),
    )

    assert result["ok"] is True
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert "memories" in tables
    assert "memory_outbox" in tables
    assert "repository_index_snapshots" in tables
    assert "repository_source_imports" in tables
    assert user_version == 1
    current = SQLiteRepositoryIndexStore(database).current_snapshot("demo", result["state"]["root_id"])
    assert current["snapshot_id"] == result["snapshot_id"]


def test_empty_v1_repository_schema_migrates_with_verified_backup(tmp_path: Path) -> None:
    database = tmp_path / "memories.sqlite3"
    SQLiteMemoryRepository(database).initialize()
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE repository_index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO repository_index_meta(key, value) VALUES ('schema_version', '1');
            CREATE TABLE repository_index_jobs (job_id TEXT PRIMARY KEY);
            """
        )
        connection.commit()
    finally:
        connection.close()
    store = SQLiteRepositoryIndexStore(database)
    backup = tmp_path / "backup" / "memories-v1.sqlite3"

    result = store.migrate_empty_v1_to_v2(backup)

    assert result["ok"] is True
    assert result["action"] == "migrated-empty-v1-to-v2"
    assert result["backup_quick_check"] == "ok"
    assert len(result["backup_sha256"]) == 64
    assert result["memory_counts_before"] == result["memory_counts_after"]
    assert store.inspect_schema()["ready"] is True
    assert backup.is_file()
