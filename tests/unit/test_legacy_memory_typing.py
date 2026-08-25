from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from blackholememory.domain import Memory
from blackholememory.freshness_migration import apply_migration as apply_freshness
from blackholememory.freshness_migration import build_migration_plan as build_freshness_plan
from blackholememory.legacy_memory_typing import LegacyMemoryTypingError
from blackholememory.legacy_memory_typing import apply_legacy_memory_typing
from blackholememory.legacy_memory_typing import build_legacy_memory_typing_plan
from blackholememory.memory_class_migration import apply_migration as apply_typed_schema
from blackholememory.memory_class_migration import build_migration_plan as build_typed_schema_plan
from blackholememory.memory_repository import SQLiteMemoryRepository


def _memory(memory_id: str, memory_type: str, title: str) -> Memory:
    return Memory.from_record({"source_id": memory_id, "project": "blackholememory", "memory_type": memory_type, "title": title, "content": f"{title} body", "created_at": "2026-08-25T12:00:00Z", "updated_at": "2026-08-25T12:00:00Z", "metadata": {"raw_title": title}})


def _typed_database(tmp_path: Path) -> tuple[SQLiteMemoryRepository, Path, Path]:
    database = tmp_path / "memories.sqlite3"
    repository = SQLiteMemoryRepository(database)
    for memory_id, memory_type, title in (
        ("trace", "workflow", "blackholememory hybrid session record:"),
        ("checkpoint", "checkpoint", "legacy checkpoint"),
        ("decision", "decision", "SQLite is authoritative"),
        ("fact", "fact", "Qdrant is rebuildable"),
        ("code", "workflow", "code metadata project=blackholememory path=src/app.py"),
        ("compact", "transient-context", "BHM pre-compact transit buffer:"),
        ("runbook", "runbook", "How to recover the local service"),
        ("workflow", "workflow", "Historical workflow receipt"),
        ("deploy", "runbook", "lnv-push deploy runbook:"),
        ("unknown", "workflow", "ambiguous workflow"),
    ):
        repository.save_memory(_memory(memory_id, memory_type, title))
    freshness_backup = tmp_path / "freshness.sqlite3"
    shutil.copy2(database, freshness_backup)
    freshness_plan = build_freshness_plan(database, freshness_backup, as_of="2026-08-25T12:01:00Z")
    apply_freshness(database, freshness_backup, freshness_plan, expected_plan_digest=freshness_plan["plan_digest"], confirm_operator=True, offline_verified=True)
    typed_backup = tmp_path / "typed.sqlite3"
    shutil.copy2(database, typed_backup)
    typed_plan = build_typed_schema_plan(database, typed_backup)
    apply_typed_schema(database, typed_backup, typed_plan, expected_plan_digest=typed_plan["plan_digest"], confirm_operator=True, offline_verified=True)
    recovery_backup = tmp_path / "recovery.sqlite3"
    shutil.copy2(database, recovery_backup)
    return SQLiteMemoryRepository(database), database, recovery_backup


def test_plan_is_read_only_and_only_maps_structurally_unambiguous_rows(tmp_path: Path) -> None:
    repository, database, backup = _typed_database(tmp_path)
    before = database.read_bytes()
    plan = build_legacy_memory_typing_plan(database, backup)
    assert database.read_bytes() == before
    assert plan["summary"]["target_count"] == 9
    with pytest.raises(LegacyMemoryTypingError, match="operator confirmation"):
        apply_legacy_memory_typing(database, backup, plan, expected_plan_digest=plan["plan_digest"])
    result = apply_legacy_memory_typing(database, backup, plan, expected_plan_digest=plan["plan_digest"], confirm_operator=True, offline_verified=True)
    assert result["target_count"] == len(result["outbox_event_ids"]) == 9
    trace = repository.get_memory("trace", project="blackholememory")
    checkpoint = repository.get_memory("checkpoint", project="blackholememory")
    decision = repository.get_memory("decision", project="blackholememory")
    fact = repository.get_memory("fact", project="blackholememory")
    code = repository.get_memory("code", project="blackholememory")
    compact = repository.get_memory("compact", project="blackholememory")
    runbook = repository.get_memory("runbook", project="blackholememory")
    workflow = repository.get_memory("workflow", project="blackholememory")
    deploy = repository.get_memory("deploy", project="blackholememory")
    unknown = repository.get_memory("unknown", project="blackholememory")
    assert trace and checkpoint and decision and fact and code and compact and runbook and workflow and deploy and unknown
    assert (trace.memory_class.value, trace.event_role.value) == ("episodic", "trace")
    assert (checkpoint.memory_class.value, checkpoint.event_role.value) == ("episodic", "trace")
    assert (decision.memory_class.value, decision.event_role.value) == ("semantic", "decision")
    assert (fact.memory_class.value, fact.event_role.value) == ("semantic", "fact")
    assert (code.memory_class.value, code.event_role.value) == ("episodic", "trace")
    assert (compact.memory_class.value, compact.event_role.value) == ("episodic", "trace")
    assert (runbook.memory_class.value, runbook.event_role.value) == ("episodic", "trace")
    assert (workflow.memory_class.value, workflow.event_role.value) == ("episodic", "trace")
    assert (deploy.memory_class.value, deploy.event_role.value) == ("episodic", "trace")
    assert (unknown.memory_class.value, unknown.event_role.value) == ("unclassified", "unclassified")
    assert unknown.current_revision.content == "ambiguous workflow body"


def test_apply_fails_closed_after_the_target_set_changes(tmp_path: Path) -> None:
    repository, database, backup = _typed_database(tmp_path)
    plan = build_legacy_memory_typing_plan(database, backup)
    changed = repository.get_memory("decision", project="blackholememory")
    assert changed is not None
    repository.save_memory(changed.model_copy(update={"title": "changed"}))
    with pytest.raises(LegacyMemoryTypingError, match="changed since plan"):
        apply_legacy_memory_typing(database, backup, plan, expected_plan_digest=plan["plan_digest"], confirm_operator=True, offline_verified=True)
