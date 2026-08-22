from __future__ import annotations

import hashlib
import sqlite3

import pytest

from blackholememory.memory_doctor import MemoryDoctorSnapshotError
from blackholememory.memory_doctor import load_authoritative_sqlite_snapshot
from blackholememory.memory_doctor import run_authoritative_sqlite_memory_doctor
from blackholememory.memory_doctor import run_memory_doctor


def test_doctor_is_read_only_redacted_and_deterministic() -> None:
    records = (
        {"source_id": "m1", "project": "p", "content": "secret one", "authority_seq": 3, "projection_seq": 2},
        {"source_id": "m2", "project": "p", "content": "secret one"},
        {"source_id": "m3", "project": "p", "content": "different", "supersedes_revision_id": "rev1"},
    )
    report = run_memory_doctor(records, projection_watermark=2)
    assert report == run_memory_doctor(tuple(reversed(records)), projection_watermark=2)
    codes = {item["reason_code"] for item in report["findings"]}
    assert {"exact_active_duplicate", "projection_stale", "projection_watermark_lag", "supersession_lineage_incomplete"} <= codes
    assert "secret one" not in str(report)
    assert report["execution"]["repair_apply"] is False


def test_doctor_exposes_invalid_identity_without_failing_open() -> None:
    report = run_memory_doctor(({"content": "x"},))
    assert report["findings"][0]["reason_code"] == "memory_identity_missing"


def _authoritative_fixture(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE memories (
                memory_id TEXT PRIMARY KEY,
                project TEXT NOT NULL,
                lifecycle TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                current_revision_id TEXT NOT NULL,
                authority_seq INTEGER,
                projection_seq INTEGER
            );
            CREATE TABLE memory_revisions (
                revision_id TEXT PRIMARY KEY,
                content_sha256 TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO memory_revisions(revision_id, content_sha256) VALUES (?, ?)",
            [
                ("r1", hashlib.sha256(b"secret one").hexdigest()),
                ("r2", hashlib.sha256(b"secret one").hexdigest()),
            ],
        )
        connection.executemany(
            """INSERT INTO memories(
                memory_id, project, lifecycle, metadata_json, current_revision_id, authority_seq, projection_seq
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                ("m1", "project-a", "active", '{"source_digest":"source-1"}', "r1", 2, 1),
                ("m2", "project-a", "active", '{}', "r2", 1, 1),
            ],
        )


def test_authoritative_sqlite_snapshot_is_bounded_content_free_and_read_only(tmp_path) -> None:
    database = tmp_path / "memories.sqlite3"
    _authoritative_fixture(database)
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    snapshot = load_authoritative_sqlite_snapshot(database, project="project-a")

    assert snapshot["authority"] == "sqlite-authoritative"
    assert snapshot["record_count"] == 2
    assert snapshot["execution"]["sqlite_mutation"] is False
    assert snapshot["database"]["path_disclosed"] is False
    assert "secret one" not in str(snapshot)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before

    report = run_authoritative_sqlite_memory_doctor(database, project="project-a")
    assert report["authority_snapshot"]["snapshot_digest"] == snapshot["snapshot_digest"]
    assert {item["reason_code"] for item in report["findings"]} >= {
        "exact_active_duplicate",
        "projection_stale",
    }
    assert "memory_identity_missing" not in {item["reason_code"] for item in report["findings"]}
    assert "memory_id_duplicate" not in {item["reason_code"] for item in report["findings"]}
    assert "secret one" not in str(report)


def test_authoritative_sqlite_snapshot_fails_closed_for_missing_or_unbounded_input(tmp_path) -> None:
    with pytest.raises(MemoryDoctorSnapshotError, match="missing"):
        load_authoritative_sqlite_snapshot(tmp_path / "missing.sqlite3")
    with pytest.raises(MemoryDoctorSnapshotError, match="within"):
        load_authoritative_sqlite_snapshot(tmp_path / "missing.sqlite3", limit=10_001)
