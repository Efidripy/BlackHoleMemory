from __future__ import annotations

import hashlib
import sqlite3

import pytest

from blackholememory.memory_doctor import MemoryDoctorSnapshotError
from blackholememory.memory_doctor_cli import SCHEMA_VERSION
from blackholememory.memory_doctor_cli import build_read_only_memory_doctor_report
from blackholememory.memory_doctor_cli import main


def _database(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE memories (
                memory_id TEXT PRIMARY KEY,
                project TEXT NOT NULL,
                lifecycle TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                current_revision_id TEXT NOT NULL
            );
            CREATE TABLE memory_revisions (
                revision_id TEXT PRIMARY KEY,
                content_sha256 TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO memory_revisions(revision_id, content_sha256) VALUES (?, ?)",
            ("revision-1", hashlib.sha256(b"private memory content").hexdigest()),
        )
        connection.execute(
            "INSERT INTO memories(memory_id, project, lifecycle, metadata_json, current_revision_id) VALUES (?, ?, ?, ?, ?)",
            ("memory-1", "project-a", "active", "{}", "revision-1"),
        )


def test_cli_report_is_bounded_content_free_and_read_only(tmp_path) -> None:
    database = tmp_path / "memories.sqlite3"
    _database(database)
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    report = build_read_only_memory_doctor_report(database, project="project-a")

    assert report["schema_version"] == SCHEMA_VERSION
    assert report["ok"] is True
    assert report["doctor"]["authority_snapshot"]["record_count"] == 1
    assert report["scope"] == {
        "authority": "sqlite-authoritative",
        "projection_checked": False,
        "repair_available": False,
        "database_path_disclosed": False,
    }
    assert all(value is False for value in report["execution"].values() if isinstance(value, bool) and value is not report["execution"]["read_only"])
    assert report["execution"]["read_only"] is True
    assert "private memory content" not in str(report)
    assert str(database) not in str(report)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_cli_report_keeps_snapshot_limit_fail_closed(tmp_path) -> None:
    database = tmp_path / "memories.sqlite3"
    _database(database)

    with pytest.raises(MemoryDoctorSnapshotError, match="within"):
        build_read_only_memory_doctor_report(database, limit=10_001)


def test_cli_optional_report_is_a_copy_not_a_bhm_mutation(tmp_path, capsys) -> None:
    database = tmp_path / "memories.sqlite3"
    report_path = tmp_path / "doctor-report.json"
    _database(database)
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    assert main(["--database", str(database), "--project", "project-a", "--report", str(report_path)]) == 0

    standard_output = capsys.readouterr().out
    saved_output = report_path.read_text(encoding="utf-8")
    assert saved_output == standard_output
    assert str(database) not in saved_output
    assert "private memory content" not in saved_output
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
