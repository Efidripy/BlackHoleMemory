from __future__ import annotations

import hashlib
import sqlite3

import pytest

from blackholememory.memory_doctor import MemoryDoctorSnapshotError
from blackholememory.memory_doctor import build_memory_doctor_repair_proposal
from blackholememory.memory_doctor import load_authoritative_sqlite_snapshot
from blackholememory.memory_doctor import load_qdrant_projection_snapshot
from blackholememory.memory_doctor import run_authoritative_sqlite_memory_doctor
from blackholememory.memory_doctor import run_authoritative_projection_memory_doctor
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


def test_doctor_checks_explicit_ontology_and_shared_governance_annotations_without_leaking_owner() -> None:
    report = run_memory_doctor(
        (
            {"memory_id": "m1", "project": "p", "content": "hidden", "metadata": {"ontology_schema_digest": "a" * 64, "shared_visibility": "project", "owner_id": "private-owner", "sensitivity": "restricted"}},
            {"memory_id": "m2", "project": "p", "content": "hidden", "metadata": {"ontology_schema_digest": "not-a-digest", "shared_visibility": "invalid", "sensitivity": "secret"}},
        ),
        expected_ontology_digests={"p": "b" * 64},
    )

    codes = {item["reason_code"] for item in report["findings"]}
    assert {"ontology_schema_digest_mismatch", "ontology_schema_digest_invalid", "shared_visibility_invalid", "shared_sensitivity_invalid"} <= codes
    assert "private-owner" not in str(report)


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


class _ProjectionPoint:
    def __init__(self, point_id: str, payload: dict[str, str]) -> None:
        self.id = point_id
        self.payload = payload


class _ReadOnlyQdrantClient:
    def __init__(self, pages: dict[str, list[list[_ProjectionPoint]]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, object]] = []

    def scroll(self, **kwargs):
        self.calls.append(kwargs)
        collection = str(kwargs["collection_name"])
        index = int(kwargs["offset"] or 0)
        page = self.pages[collection][index]
        next_offset = index + 1 if index + 1 < len(self.pages[collection]) else None
        return page, next_offset


def test_qdrant_snapshot_is_injected_bounded_deterministic_and_redacted() -> None:
    client = _ReadOnlyQdrantClient(
        {
            "z": [[_ProjectionPoint("p2", {"source_id": "m2", "project": "project-a", "lifecycle": "active", "revision_id": "r2", "content_sha256": "digest-2", "data": "secret two", "metadata": "never disclose"})]],
            "a": [[_ProjectionPoint("p1", {"source_id": "m1", "project": "project-a", "lifecycle": "active", "revision_id": "r1", "content_sha256": "digest-1", "content": "secret one", "projection_payload_digest": "payload-1"})]],
        }
    )

    snapshot = load_qdrant_projection_snapshot(client, ["z", "a"], project="project-a")

    assert [item["collection"] for item in snapshot["records"]] == ["a", "z"]
    assert snapshot["record_count"] == 2
    assert snapshot["coverage"] == "complete"
    assert snapshot["execution"]["qdrant_mutation"] is False
    assert snapshot["execution"]["vectors_requested"] is False
    assert "secret one" not in str(snapshot)
    assert "secret two" not in str(snapshot)
    assert "never disclose" not in str(snapshot)
    assert all(call["with_vectors"] is False for call in client.calls)
    assert all(call["with_payload"] is True for call in client.calls)
    assert all(set(call) == {"collection_name", "limit", "offset", "with_payload", "with_vectors"} for call in client.calls)


def test_qdrant_snapshot_declares_bounded_partial_coverage(tmp_path) -> None:
    database = tmp_path / "memories.sqlite3"
    _authoritative_fixture(database)
    client = _ReadOnlyQdrantClient(
        {
            "project-a": [
                [_ProjectionPoint("p1", {"source_id": "m1", "project": "other", "lifecycle": "active", "revision_id": "r1", "content_sha256": "d1"})],
                [_ProjectionPoint("p2", {"source_id": "m2", "project": "project-a", "lifecycle": "active", "revision_id": "r2", "content_sha256": "d2"})],
            ]
        }
    )

    snapshot = load_qdrant_projection_snapshot(
        client, ["project-a"], project="project-a", max_pages=1
    )

    assert snapshot["record_count"] == 0
    assert snapshot["coverage"] == "bounded_partial"
    assert snapshot["scan"] == {
        "page_count": 1,
        "page_budget": 1,
        "complete": False,
        "limit_reached": False,
    }


def test_qdrant_parity_reports_missing_and_mismatched_projection_identity(tmp_path) -> None:
    database = tmp_path / "memories.sqlite3"
    _authoritative_fixture(database)
    client = _ReadOnlyQdrantClient(
        {
            "project-a": [[
                _ProjectionPoint("p-mismatch", {"source_id": "m1", "project": "project-a", "lifecycle": "active", "revision_id": "obsolete", "content_sha256": "wrong"}),
                _ProjectionPoint("p-missing", {"source_id": "missing", "project": "project-a", "lifecycle": "active", "revision_id": "r9", "content_sha256": "d9"}),
                _ProjectionPoint("p-incomplete", {"source_id": "m2", "project": "project-a"}),
            ]]
        }
    )

    report = run_authoritative_projection_memory_doctor(database, client, ["project-a"], project="project-a")

    codes = {item["reason_code"] for item in report["findings"]}
    assert {"projection_revision_mismatch", "projection_content_digest_mismatch", "projection_source_missing_in_authority_snapshot", "projection_identity_incomplete"} <= codes
    assert report["authority_snapshot"]["authority"] == "sqlite-authoritative"
    assert report["projection_snapshot"]["authority"] == "qdrant-rebuildable-projection"
    assert report["execution"]["qdrant_mutation"] is False


def test_qdrant_parity_reports_expected_missing_only_after_complete_coverage(tmp_path) -> None:
    database = tmp_path / "memories.sqlite3"
    _authoritative_fixture(database)
    empty_client = _ReadOnlyQdrantClient({"project-a": [[]]})

    complete = run_authoritative_projection_memory_doctor(
        database,
        empty_client,
        ["project-a"],
        project="project-a",
        expected_collections_by_project={"project-a": ["project-a"]},
    )

    missing = [item for item in complete["findings"] if item["reason_code"] == "projection_expected_point_missing"]
    assert {item["memory_id"] for item in missing} == {"m1", "m2"}
    assert complete["projection_snapshot"]["expected_collections_by_project"] == {"project-a": ("project-a",)}

    partial_client = _ReadOnlyQdrantClient(
        {"project-a": [[_ProjectionPoint("noise", {"source_id": "noise", "project": "other", "lifecycle": "active", "revision_id": "r", "content_sha256": "d"})], []]}
    )
    partial = run_authoritative_projection_memory_doctor(
        database,
        partial_client,
        ["project-a"],
        project="project-a",
        max_pages=1,
        expected_collections_by_project={"project-a": ["project-a"]},
    )
    codes = {item["reason_code"] for item in partial["findings"]}
    assert "projection_expected_coverage_unproven" in codes
    assert "projection_expected_point_missing" not in codes


def test_doctor_repair_proposals_are_bounded_deterministic_and_non_executable() -> None:
    report = run_memory_doctor(
        (
            {"source_id": "m1", "project": "p", "content": "private", "authority_seq": 2, "projection_seq": 1},
            {"source_id": "m2", "project": "p", "content": "private"},
            {"source_id": "m3", "project": "p", "content": "other", "supersedes_revision_id": "r1"},
        )
    )

    proposal = build_memory_doctor_repair_proposal(
        report, authority_snapshot_digest="a" * 64
    )

    assert proposal == build_memory_doctor_repair_proposal(
        report, authority_snapshot_digest="a" * 64
    )
    assert {item["repair_kind"] for item in proposal["proposals"]} >= {
        "duplicate_merge_review",
        "projection_rebuild_review",
        "supersession_review",
    }
    assert all(item["apply_performed"] is False for item in proposal["proposals"])
    assert proposal["execution"]["auto_apply"] is False
    assert proposal["execution"]["sqlite_mutation"] is False
    assert "private" not in str(proposal)


def test_doctor_repair_proposals_fail_closed_for_unbound_or_malformed_reports() -> None:
    with pytest.raises(MemoryDoctorSnapshotError, match="report_digest"):
        build_memory_doctor_repair_proposal({"findings": []})
    with pytest.raises(MemoryDoctorSnapshotError, match="finding"):
        build_memory_doctor_repair_proposal(
            {"report_digest": "a" * 64, "findings": ["not-an-object"]}
        )
