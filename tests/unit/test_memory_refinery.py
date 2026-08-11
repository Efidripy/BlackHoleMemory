from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from blackholememory.domain import Memory
from blackholememory.memory_repository import MemoryRevisionConflict
from blackholememory.memory_refinery import MemoryRefineryError
from blackholememory.memory_refinery import apply_normalization_plan
from blackholememory.memory_refinery import build_normalization_plan
from blackholememory.memory_refinery import normalize_memory_record
from blackholememory.memory_refinery import prepare_rehearsal_copies
from blackholememory.memory_refinery import prove_rollback_restore
from blackholememory.memory_service import SQLiteMemoryService


REPO_ROOT = Path(__file__).resolve().parents[2]
REFINERY_SCRIPT = REPO_ROOT / "scripts" / "run-memory-refinery.py"


def _record(memory_id: str = "mem-1") -> dict:
    return {
        "source_id": memory_id,
        "project": "BlackHoleMemory",
        "memory_type": "architecture",
        "lifecycle": "active",
        "content": "# Durable memory authority\nSQLite is authoritative and Qdrant is a projection.",
        "tags": [" BHM ", "bhm", "SQLite"],
        "files": ["src/blackholememory/app.py"],
        "session_refs": [],
        "source_system": "mcp",
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
        "metadata": {},
    }


def test_normalization_is_deterministic_and_separates_lifecycle() -> None:
    first, reasons = normalize_memory_record(_record())
    second, second_reasons = normalize_memory_record(first)

    assert first["project"] == "blackholememory"
    assert first["tags"] == ["bhm", "sqlite"]
    assert first["metadata"]["display_title"] == "Durable memory authority SQLite is authoritative and Qdrant is a projection."
    assert first["summary"].startswith("Durable memory authority")
    assert first["metadata"]["domain"] == "infra"
    assert first["metadata"]["semantic_type"] == "architecture"
    assert first["metadata"]["provenance"] == "mcp"
    assert first["metadata"]["priority"] == "medium"
    assert first["metadata"]["lifecycle"] == "draft"
    assert first["metadata"]["version"] == "1.0"
    assert first["metadata"]["importance_score"] == 8
    assert "project_alias" in reasons
    assert second == first
    assert second_reasons == []


def test_tombstoned_storage_preserves_marker_and_separate_taxonomy() -> None:
    record = _record()
    record["lifecycle"] = "tombstoned"
    record["metadata"] = {"lifecycle": "tombstoned", "priority": "trivial"}
    normalized, _ = normalize_memory_record(record)

    assert normalized["lifecycle"] == "tombstoned"
    assert normalized["metadata"]["lifecycle"] == "tombstoned"
    assert normalized["metadata"]["taxonomy_lifecycle"] == "archived"
    assert normalized["metadata"]["priority"] == "low"


def test_unknown_bhm_origin_does_not_fabricate_synthetic_provenance() -> None:
    record = _record()
    record["source_system"] = "bhm"
    record["agent_id"] = "workspace"

    normalized, _ = normalize_memory_record(record)

    assert "provenance" not in normalized["metadata"]
    plan = build_normalization_plan([record])
    assert plan["quality"]["provenance_unresolved"] == 1


def test_known_project_registry_aliases_normalize_but_unknown_ids_are_preserved() -> None:
    known = _record("known")
    known["project"] = "Black Hole Memory"
    unknown = _record("unknown")
    unknown["project"] = "Client Project X"

    normalized_known, _ = normalize_memory_record(known)
    normalized_unknown, _ = normalize_memory_record(unknown)

    assert normalized_known["project"] == "blackholememory"
    assert normalized_unknown["project"] == "Client Project X"


def test_unclassified_taxonomy_uses_general_instead_of_infra() -> None:
    record = _record()
    record["project"] = "notes"
    record["memory_type"] = "fact"
    record["content"] = "The meeting starts on Tuesday."
    record["files"] = []
    record["tags"] = []

    normalized, _ = normalize_memory_record(record)

    assert normalized["metadata"]["domain"] == "general"


def test_plan_digest_changes_when_source_changes() -> None:
    record = _record()
    first = build_normalization_plan([record])
    changed = copy.deepcopy(record)
    changed["content"] += " Changed."
    second = build_normalization_plan([changed])

    assert first["records_changed"] == 1
    assert first["plan_digest"] != second["plan_digest"]
    assert first["source_state_digest"] != second["source_state_digest"]


def test_apply_requires_matching_digest_and_rejects_stale_plan(tmp_path) -> None:
    database = tmp_path / "memories.sqlite3"
    service = SQLiteMemoryService(database, allow_create=True)
    service.upsert_records([_record()])
    plan = build_normalization_plan(service.load_records(include_storage_lifecycle=True))

    with pytest.raises(MemoryRefineryError, match="digest mismatch"):
        apply_normalization_plan(database, plan, expected_plan_digest="wrong")

    stale = copy.deepcopy(service.load_records(include_storage_lifecycle=True)[0])
    stale["tags"] = ["changed"]
    service.upsert_records([stale])
    with pytest.raises(MemoryRefineryError, match="changed after"):
        apply_normalization_plan(database, plan, expected_plan_digest=plan["plan_digest"])


def test_apply_updates_copy_and_preserves_integrity(tmp_path) -> None:
    database = tmp_path / "memories.sqlite3"
    service = SQLiteMemoryService(database, allow_create=True)
    service.upsert_records([_record()])
    plan = build_normalization_plan(service.load_records(include_storage_lifecycle=True))

    result = apply_normalization_plan(
        database,
        plan,
        expected_plan_digest=plan["plan_digest"],
    )
    stored = SQLiteMemoryService(database).load_records()[0]

    assert result["records_changed"] == 1
    assert result["verification"]["ok"] is True
    assert stored["project"] == "blackholememory"
    assert stored["summary"]
    assert stored["metadata"]["version"] == "1.0"


def test_apply_updates_related_project_columns_in_same_transaction(tmp_path) -> None:
    database = tmp_path / "memories.sqlite3"
    service = SQLiteMemoryService(database, allow_create=True)
    service.upsert_records([_record()])
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO memory_links(link_id, project, source_id, target_id, relation, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("link-1", "BlackHoleMemory", "mem-1", "mem-2", "SUPPORTS", "{}"),
        )
        connection.execute(
            "INSERT INTO memory_artifacts(artifact_type, artifact_id, project, memory_id, lifecycle, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("note", "artifact-1", "BlackHoleMemory", "mem-1", "active", "{}"),
        )
        connection.commit()
    plan = build_normalization_plan(
        service.load_records(include_storage_lifecycle=True)
    )

    result = apply_normalization_plan(
        database,
        plan,
        expected_plan_digest=plan["plan_digest"],
    )

    with sqlite3.connect(database) as connection:
        link_projects = connection.execute("SELECT project FROM memory_links").fetchall()
        artifact_projects = connection.execute("SELECT project FROM memory_artifacts").fetchall()
    assert result["links_updated"] == 1
    assert result["artifacts_updated"] == 1
    assert link_projects == [("blackholememory",)]
    assert artifact_projects == [("blackholememory",)]


def test_apply_rejects_project_alias_upsert_key_collision_atomically(tmp_path) -> None:
    database = tmp_path / "memories.sqlite3"
    service = SQLiteMemoryService(database, allow_create=True)
    legacy = _record("legacy")
    legacy["upsert_key"] = "shared-key"
    canonical = _record("canonical")
    canonical["project"] = "blackholememory"
    canonical["upsert_key"] = "shared-key"
    service.upsert_records([legacy, canonical])
    plan = build_normalization_plan(
        service.load_records(include_storage_lifecycle=True)
    )

    with pytest.raises(MemoryRefineryError, match="atomic refinery apply failed"):
        apply_normalization_plan(
            database,
            plan,
            expected_plan_digest=plan["plan_digest"],
        )

    projects = {
        record["source_id"]: record["project"]
        for record in service.load_records(include_storage_lifecycle=True)
    }
    assert projects == {"legacy": "BlackHoleMemory", "canonical": "blackholememory"}


def test_repository_refinery_cas_covers_unchanged_snapshot_records(tmp_path) -> None:
    database = tmp_path / "memories.sqlite3"
    service = SQLiteMemoryService(database, allow_create=True)
    changing = _record("changing")
    unchanged = _record("unchanged")
    unchanged["project"] = "other-project"
    unchanged["metadata"] = {"domain": "general", "semantic_type": "knowledge"}
    service.upsert_records([changing, unchanged])
    before = service.load_records(include_storage_lifecycle=True)
    expected = {record["source_id"]: Memory.from_record(record) for record in before}
    normalized, _ = normalize_memory_record(
        next(record for record in before if record["source_id"] == "changing")
    )
    concurrent = _record("concurrent")
    concurrent["project"] = "other-project"
    service.upsert_records([concurrent])

    with pytest.raises(MemoryRevisionConflict, match="memory set changed"):
        service.repository.save_memories_refinery_atomic(
            [Memory.from_record(normalized)],
            expected_memories=expected,
            project_aliases={"BlackHoleMemory": "blackholememory"},
        )


def _file_sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_rehearsal_keeps_rollback_backup_immutable_and_proves_restore(tmp_path) -> None:
    source = tmp_path / "source.sqlite3"
    backup = tmp_path / "rollback.sqlite3"
    working = tmp_path / "working.sqlite3"
    restore_probe = tmp_path / "restore.sqlite3"
    service = SQLiteMemoryService(source, allow_create=True)
    service.upsert_records([_record()])

    copies = prepare_rehearsal_copies(source, backup, working)
    backup_sha256 = _file_sha256(backup)
    backup_before = SQLiteMemoryService(backup).load_records()[0]
    backup_outbox_before = copies["rollback_backup"]["verification"]["outbox"]
    plan = build_normalization_plan(
        SQLiteMemoryService(working).load_records(include_storage_lifecycle=True)
    )
    applied = apply_normalization_plan(working, plan, expected_plan_digest=plan["plan_digest"])
    proof = prove_rollback_restore(
        backup,
        restore_probe,
        expected_backup_sha256=copies["rollback_backup"]["sha256_before"],
        expected_fingerprint=copies["rollback_backup"]["logical_fingerprint"],
    )

    backup_after = SQLiteMemoryService(backup).load_records()[0]
    working_after = SQLiteMemoryService(working).load_records()[0]
    restored = SQLiteMemoryService(restore_probe).load_records()[0]
    assert applied["database"] == str(working.resolve())
    assert _file_sha256(backup) == backup_sha256
    assert backup_before == backup_after == restored
    assert backup_after["project"] == "BlackHoleMemory"
    assert not backup_after.get("summary")
    assert working_after["project"] == "blackholememory"
    assert working_after["summary"]
    assert copies["rollback_backup"]["verification"]["outbox"] == backup_outbox_before
    assert proof["ok"] is True
    assert proof["expected_fingerprint"]["fingerprint"] == proof["restored_fingerprint"]["fingerprint"]


@pytest.mark.parametrize(
    ("source_name", "backup_name", "working_name"),
    [
        ("same.sqlite3", "same.sqlite3", "working.sqlite3"),
        ("source.sqlite3", "same.sqlite3", "same.sqlite3"),
        ("same.sqlite3", "backup.sqlite3", "same.sqlite3"),
    ],
)
def test_rehearsal_rejects_path_collisions_before_writing(
    tmp_path,
    source_name: str,
    backup_name: str,
    working_name: str,
) -> None:
    source = tmp_path / source_name
    SQLiteMemoryService(source, allow_create=True).upsert_records([_record()])

    with pytest.raises(MemoryRefineryError, match="must be distinct"):
        prepare_rehearsal_copies(source, tmp_path / backup_name, tmp_path / working_name)


def test_restore_proof_rejects_tampered_backup(tmp_path) -> None:
    source = tmp_path / "source.sqlite3"
    backup = tmp_path / "rollback.sqlite3"
    working = tmp_path / "working.sqlite3"
    SQLiteMemoryService(source, allow_create=True).upsert_records([_record()])
    copies = prepare_rehearsal_copies(source, backup, working)
    backup.write_bytes(backup.read_bytes() + b"tampered")

    with pytest.raises(MemoryRefineryError, match="digest mismatch before"):
        prove_rollback_restore(
            backup,
            tmp_path / "restore.sqlite3",
            expected_backup_sha256=copies["rollback_backup"]["sha256_before"],
            expected_fingerprint=copies["rollback_backup"]["logical_fingerprint"],
        )


def test_rehearse_cli_writes_only_the_working_copy(tmp_path) -> None:
    source = tmp_path / "source.sqlite3"
    backup = tmp_path / "rollback.sqlite3"
    working = tmp_path / "working.sqlite3"
    restore_probe = tmp_path / "restore.sqlite3"
    plan = tmp_path / "plan.json"
    receipt = tmp_path / "receipt.json"
    SQLiteMemoryService(source, allow_create=True).upsert_records([_record()])

    completed = subprocess.run(
        [
            sys.executable,
            str(REFINERY_SCRIPT),
            "rehearse",
            "--database",
            str(source),
            "--backup",
            str(backup),
            "--working-copy",
            str(working),
            "--restore-probe",
            str(restore_probe),
            "--plan",
            str(plan),
            "--receipt",
            str(receipt),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["apply"]["database"] == str(working.resolve())
    assert payload["restore_proof"]["ok"] is True
    assert payload["rollback_backup"]["sha256_before"] == payload["rollback_backup"]["sha256_after_rehearsal"]
    assert SQLiteMemoryService(backup).load_records()[0]["project"] == "BlackHoleMemory"
    assert SQLiteMemoryService(working).load_records()[0]["project"] == "blackholememory"


def test_rehearse_cli_preserves_tombstoned_storage_lifecycle(tmp_path) -> None:
    source = tmp_path / "source.sqlite3"
    backup = tmp_path / "rollback.sqlite3"
    working = tmp_path / "working.sqlite3"
    restore_probe = tmp_path / "restore.sqlite3"
    plan = tmp_path / "plan.json"
    receipt = tmp_path / "receipt.json"
    service = SQLiteMemoryService(source, allow_create=True)
    service.upsert_records([_record()])
    service.tombstone("mem-1", reason="refinery regression")

    completed = subprocess.run(
        [
            sys.executable,
            str(REFINERY_SCRIPT),
            "rehearse",
            "--database",
            str(source),
            "--backup",
            str(backup),
            "--working-copy",
            str(working),
            "--restore-probe",
            str(restore_probe),
            "--plan",
            str(plan),
            "--receipt",
            str(receipt),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    stored = SQLiteMemoryService(working).repository.get_memory("mem-1")
    assert stored is not None
    assert stored.lifecycle.value == "tombstoned"
    assert stored.metadata["lifecycle"] == "tombstoned"
    assert stored.metadata["taxonomy_lifecycle"] == "archived"


def test_rehearse_cli_rejects_existing_backup_before_writing(tmp_path) -> None:
    source = tmp_path / "source.sqlite3"
    backup = tmp_path / "rollback.sqlite3"
    sentinel = b"sealed rollback anchor"
    SQLiteMemoryService(source, allow_create=True).upsert_records([_record()])
    backup.write_bytes(sentinel)

    completed = subprocess.run(
        [
            sys.executable,
            str(REFINERY_SCRIPT),
            "rehearse",
            "--database",
            str(source),
            "--backup",
            str(backup),
            "--working-copy",
            str(tmp_path / "working.sqlite3"),
            "--restore-probe",
            str(tmp_path / "restore.sqlite3"),
            "--plan",
            str(tmp_path / "plan.json"),
            "--receipt",
            str(tmp_path / "receipt.json"),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode != 0
    assert "must not already exist" in completed.stderr
    assert backup.read_bytes() == sentinel
    assert not (tmp_path / "working.sqlite3").exists()


def test_rehearse_cli_rejects_json_database_path_collision(tmp_path) -> None:
    source = tmp_path / "source.sqlite3"
    SQLiteMemoryService(source, allow_create=True).upsert_records([_record()])
    source_before = _file_sha256(source)

    completed = subprocess.run(
        [
            sys.executable,
            str(REFINERY_SCRIPT),
            "rehearse",
            "--database",
            str(source),
            "--backup",
            str(tmp_path / "rollback.sqlite3"),
            "--working-copy",
            str(tmp_path / "working.sqlite3"),
            "--restore-probe",
            str(tmp_path / "restore.sqlite3"),
            "--plan",
            str(source),
            "--receipt",
            str(tmp_path / "receipt.json"),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode != 0
    assert "must be distinct" in completed.stderr
    assert _file_sha256(source) == source_before
    assert not (tmp_path / "rollback.sqlite3").exists()


def test_apply_cli_rejects_receipt_database_collision_before_mutation(tmp_path) -> None:
    database = tmp_path / "memories.sqlite3"
    plan_path = tmp_path / "plan.json"
    service = SQLiteMemoryService(database, allow_create=True)
    service.upsert_records([_record()])
    plan = build_normalization_plan(service.load_records(include_storage_lifecycle=True))
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    database_before = _file_sha256(database)

    completed = subprocess.run(
        [
            sys.executable,
            str(REFINERY_SCRIPT),
            "apply",
            "--database",
            str(database),
            "--plan",
            str(plan_path),
            "--expected-plan-digest",
            plan["plan_digest"],
            "--receipt",
            str(database),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode != 0
    assert "must be distinct" in completed.stderr
    assert _file_sha256(database) == database_before


def test_apply_cli_requires_explicit_live_authorization_for_every_database(tmp_path) -> None:
    database = tmp_path / "memories.sqlite3"
    plan_path = tmp_path / "plan.json"
    receipt = tmp_path / "receipt.json"
    service = SQLiteMemoryService(database, allow_create=True)
    service.upsert_records([_record()])
    plan = build_normalization_plan(
        service.load_records(include_storage_lifecycle=True)
    )
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    database_before = _file_sha256(database)

    completed = subprocess.run(
        [
            sys.executable,
            str(REFINERY_SCRIPT),
            "apply",
            "--database",
            str(database),
            "--plan",
            str(plan_path),
            "--expected-plan-digest",
            plan["plan_digest"],
            "--receipt",
            str(receipt),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode != 0
    assert "requires explicit --allow-live" in completed.stderr
    assert _file_sha256(database) == database_before
    assert not receipt.exists()
