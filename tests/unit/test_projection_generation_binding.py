from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

import blackholememory.projection_generation_binding as binding_module
from blackholememory.freshness_migration import (
    apply_migration as apply_freshness_migration,
)
from blackholememory.freshness_migration import (
    build_migration_plan as build_freshness_migration_plan,
)
from blackholememory.governed_consolidation_migration import (
    apply_governed_consolidation_migration,
    build_governed_consolidation_migration_plan,
)
from blackholememory.memory_repository import (
    PROJECTION_GENERATION_BINDING_SCHEMA_VERSION,
    SQLiteMemoryRepository,
)
from blackholememory.projection_generation_binding import (
    ProjectionGenerationBindingError,
    active_projection_generation_bindings,
    apply_projection_generation_binding_plan,
    build_projection_generation_binding_plan,
    projection_target_set_digest,
    resolve_bound_collection,
    rollback_projection_generation_binding,
)
from blackholememory.projection_generation_binding_migration import (
    apply_projection_generation_binding_migration,
    build_projection_generation_binding_migration_plan,
    projection_generation_binding_migration_status,
)
from blackholememory.projection_reconciliation import _generation_collection_name


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _copy(source: Path, target: Path) -> None:
    with sqlite3.connect(source) as read_connection, sqlite3.connect(target) as write_connection:
        read_connection.backup(write_connection)


def _v3_database(tmp_path: Path) -> tuple[Path, Path]:
    database = tmp_path / "memories.sqlite3"
    SQLiteMemoryRepository(database).initialize()
    backup_v1 = tmp_path / "backup-v1.sqlite3"
    _copy(database, backup_v1)
    freshness = build_freshness_migration_plan(database, backup_v1, as_of="2026-10-12T00:00:00Z")
    apply_freshness_migration(database, backup_v1, freshness, expected_plan_digest=freshness["plan_digest"], confirm_operator=True, offline_verified=True)
    backup_v2 = tmp_path / "backup-v2.sqlite3"
    _copy(database, backup_v2)
    governed = build_governed_consolidation_migration_plan(database, backup_v2, as_of="2026-10-12T00:00:01Z")
    apply_governed_consolidation_migration(database, backup_v2, governed, expected_plan_digest=governed["plan_digest"], confirm_operator=True, offline_verified=True)
    backup_v3 = tmp_path / "backup-v3.sqlite3"
    _copy(database, backup_v3)
    return database, backup_v3


def _bindings() -> list[dict[str, object]]:
    generation = _digest("generation")
    source = _digest("source")
    plan = _digest("generation-plan")
    result = [
        {
            "project": "project-a",
            "target_collection": "bhm_projection_generation_project_a_exact",
            "plan_digest": plan,
            "source_basis_digest": source,
            "generation_id": generation,
            "embedding_model": "fixture-model",
            "vector_dimensions": 768,
            "distance": "Cosine",
            "expected_point_count": 2,
            "parity_digest": _digest("parity-a"),
        },
        {
            "project": "project-b",
            "target_collection": "bhm_projection_generation_project_b_exact",
            "plan_digest": plan,
            "source_basis_digest": source,
            "generation_id": generation,
            "embedding_model": "fixture-model",
            "vector_dimensions": 768,
            "distance": "Cosine",
            "expected_point_count": 3,
            "parity_digest": _digest("parity-b"),
        },
    ]
    target_set = projection_target_set_digest([str(item["target_collection"]) for item in result])
    for item in result:
        item["target_set_digest"] = target_set
    return result


def test_target_set_digest_reproduces_historical_generation_receipts() -> None:
    projects = (
        "agent-ops", "agntseospy", "allods2", "bonsai-demo", "brrrrrrrnews", "figma-design",
        "installkit", "jmaka", "laclave-mx", "legacy-bhm-codex-connector", "legacy_bhm-private",
        "lnd-video", "lnv-push-bot", "lookatme", "mcpsrv", "sojmieblo", "ubntservice",
    )
    legacy_rebuild_01 = [
        _generation_collection_name(project=project, target_generation="legacy-rebuild-01", generation_id="101d9c2f6a00fe083d3e4b0aede61831b8af3717145fcc933d028c0589d714a0")
        for project in projects
    ]
    legacy_rebuild_02 = [
        _generation_collection_name(project=project, target_generation="legacy-rebuild-02", generation_id="a9c28567f6de42a01e40ee4413aa36a8b32197ba4e267589cb78d85fa5f09590")
        for project in projects
    ]
    assert projection_target_set_digest(legacy_rebuild_01) == "8b91b79ecacbf9b3dace4885a2827e3b7dcbe315a3a0c0ec1a3a98247b45b769"
    assert projection_target_set_digest(legacy_rebuild_02) == "f4620191b68e02906b140ad70a1b91075aa7de8dc86b52aa7cf870789edef11d"


def test_target_set_digest_rejects_duplicate_and_unsafe_collection_names() -> None:
    with pytest.raises(ProjectionGenerationBindingError, match="duplicate"):
        projection_target_set_digest(["bhm_projection_generation_fixture", "bhm_projection_generation_fixture"])
    with pytest.raises(ProjectionGenerationBindingError, match="unsafe"):
        projection_target_set_digest(["bhm_local_memory_fixture"])


def _v4_database(tmp_path: Path) -> tuple[Path, Path]:
    database, backup = _v3_database(tmp_path)
    plan = build_projection_generation_binding_migration_plan(database, backup, as_of="2026-10-12T00:00:02Z")
    result = apply_projection_generation_binding_migration(database, backup, plan, expected_plan_digest=plan["plan_digest"], confirm_operator=True, offline_verified=True)
    assert result["action"] == "applied"
    assert projection_generation_binding_migration_status(database)["ready"] is True
    backup_v4 = tmp_path / "backup-v4.sqlite3"
    _copy(database, backup_v4)
    return database, backup_v4


def _binding_plan(database: Path, backup: Path) -> dict[str, object]:
    return build_projection_generation_binding_plan(
        _bindings(),
        database=database,
        existing_backup=backup,
        as_of="2026-10-12T01:00:00Z",
        actor_digest=_digest("operator"),
        correlation_id="fixture-binding",
    )


def test_migration_is_digest_gated_additive_and_requires_offline_proof(tmp_path: Path) -> None:
    database, backup = _v3_database(tmp_path)
    plan = build_projection_generation_binding_migration_plan(database, backup, as_of="2026-10-12T00:00:02Z")
    with pytest.raises(Exception, match="confirmation"):
        apply_projection_generation_binding_migration(database, backup, plan, expected_plan_digest=plan["plan_digest"])
    applied = apply_projection_generation_binding_migration(database, backup, plan, expected_plan_digest=plan["plan_digest"], confirm_operator=True, offline_verified=True)
    assert applied["action"] == "applied"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == PROJECTION_GENERATION_BINDING_SCHEMA_VERSION
        assert connection.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0] == 0
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_binding_plan_rejects_duplicates_and_generation_conflicts(tmp_path: Path) -> None:
    database, backup = _v4_database(tmp_path)
    duplicate = _bindings()
    duplicate[1]["project"] = duplicate[0]["project"]
    with pytest.raises(ProjectionGenerationBindingError, match="duplicate project"):
        build_projection_generation_binding_plan(duplicate, database=database, existing_backup=backup, as_of="2026-10-12T01:00:00Z", actor_digest=_digest("operator"), correlation_id="fixture")
    conflict = _bindings()
    conflict[1]["source_basis_digest"] = _digest("another-source")
    with pytest.raises(ProjectionGenerationBindingError, match="inconsistent"):
        build_projection_generation_binding_plan(conflict, database=database, existing_backup=backup, as_of="2026-10-12T01:00:00Z", actor_digest=_digest("operator"), correlation_id="fixture")


def test_apply_is_idempotent_and_rollback_preserves_history(tmp_path: Path) -> None:
    database, backup = _v4_database(tmp_path)
    plan = _binding_plan(database, backup)
    with pytest.raises(ProjectionGenerationBindingError, match="offline"):
        apply_projection_generation_binding_plan(database, backup, plan, expected_plan_digest=plan["plan_digest"], confirm_operator=True)
    first = apply_projection_generation_binding_plan(database, backup, plan, expected_plan_digest=plan["plan_digest"], confirm_operator=True, offline_verified=True)
    assert first["action"] == "applied"
    assert len(active_projection_generation_bindings(database)) == 2
    replay = apply_projection_generation_binding_plan(database, backup, plan, expected_plan_digest=plan["plan_digest"], confirm_operator=True, offline_verified=True)
    assert replay["action"] == "already-applied"
    binding = active_projection_generation_bindings(database)[0]
    rolled_back = rollback_projection_generation_binding(database, backup, project=binding.project, binding_digest=binding.binding_digest, actor_digest=_digest("operator"), correlation_id="rollback-fixture", confirm_operator=True, offline_verified=True)
    assert rolled_back["action"] == "rolled-back"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM projection_generation_bindings WHERE state = 'rolled_back'").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM projection_generation_binding_events WHERE action = 'rolled_back'").fetchone()[0] == 1


def test_resolver_fails_closed_for_absent_or_changed_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database, backup = _v4_database(tmp_path)
    legacy = "bhm_local_memory_project_a"
    resolved, reason = resolve_bound_collection(project="project-a", legacy_collection=legacy, database=database, embedding_model="fixture-model", vector_dimensions=768, distance="Cosine")
    assert (resolved, reason) == (legacy, "binding_unavailable")
    plan = _binding_plan(database, backup)
    apply_projection_generation_binding_plan(database, backup, plan, expected_plan_digest=plan["plan_digest"], confirm_operator=True, offline_verified=True)
    source = str(_bindings()[0]["source_basis_digest"])
    monkeypatch.setattr(binding_module, "_generation_source_basis_digest", lambda _memories: source)
    resolved, reason = resolve_bound_collection(project="project-a", legacy_collection=legacy, database=database, embedding_model="fixture-model", vector_dimensions=768, distance="Cosine")
    assert resolved == "bhm_projection_generation_project_a_exact"
    assert reason == "binding_active"
    mismatch, mismatch_reason = resolve_bound_collection(project="project-a", legacy_collection=legacy, database=database, embedding_model="another-model", vector_dimensions=768, distance="Cosine")
    assert (mismatch, mismatch_reason) == (None, "binding_vector_space_mismatch")
    monkeypatch.setattr(binding_module, "_generation_source_basis_digest", lambda _memories: _digest("drift"))
    drift, drift_reason = resolve_bound_collection(project="project-a", legacy_collection=legacy, database=database, embedding_model="fixture-model", vector_dimensions=768, distance="Cosine")
    assert (drift, drift_reason) == (None, "binding_source_drift")
