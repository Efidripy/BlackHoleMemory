from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from blackholememory.domain import Memory
from blackholememory.domain import MemoryLink
from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.ontology_registry import OntologySchema
from blackholememory.ontology_registry import build_activation_artifact
from blackholememory.ontology_registry import build_registry_artifact
from blackholememory.semantic_graph_link_migration import SemanticGraphLinkMigrationError
from blackholememory.semantic_graph_link_migration import apply_semantic_graph_link_migration
from blackholememory.semantic_graph_link_migration import build_semantic_graph_link_migration_plan
from blackholememory.sqlite_retention import create_verified_sqlite_backup


PROJECT = "project-a"


def _memory(source_id: str, *, lifecycle: str = "active") -> Memory:
    return Memory.from_record(
        {
            "source_system": "bhm",
            "source_id": source_id,
            "project": PROJECT,
            "agent_id": "operator",
            "memory_type": "architecture",
            "content": f"memory {source_id}",
            "created_at": "2026-08-23T10:00:00Z",
            "updated_at": "2026-08-23T10:00:00Z",
            "metadata": {"raw_title": source_id, "lifecycle": lifecycle},
        }
    )


def _schema() -> OntologySchema:
    return OntologySchema.model_validate(
        {
            "project": PROJECT,
            "owner": "operator",
            "activation_status": "active",
            "entity_types": [{"name": "memory"}],
            "relation_types": [{"name": "depends_on", "source_types": ["memory"], "target_types": ["memory"]}],
        }
    )


def _fixture(tmp_path: Path, *, graph_edges: list[dict[str, str]] | None = None) -> tuple[Path, Path, Path, str, str]:
    database = tmp_path / "memories.sqlite3"
    repository = SQLiteMemoryRepository(database)
    source = _memory("private-source-91")
    target = _memory("private-target-91")
    repository.save_memories_atomic([source, target])
    schema = _schema()
    repository.save_artifact(build_registry_artifact(schema))
    repository.save_artifact(build_activation_artifact(schema, enabled=True, updated_at="2026-08-23T10:00:00Z"))
    graph = tmp_path / "semantic_graph.json"
    graph.write_text(
        json.dumps({source.id: graph_edges or [{"target_id": target.id, "edge_type": "DEPENDS_ON"}]}),
        encoding="utf-8",
    )
    backup = tmp_path / "backup.sqlite3"
    assert create_verified_sqlite_backup(database, backup)["ok"] is True
    return database, graph, backup, source.id, target.id


def _count_links(database: Path) -> int:
    with sqlite3.connect(database) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0])


def test_plan_is_content_free_binds_legacy_file_and_deduplicates_legacy_edges(tmp_path: Path) -> None:
    database, graph, _backup, source, target = _fixture(tmp_path)
    # Replace the fixture graph after its deterministic memory ids are known.
    graph.write_text(
        json.dumps({source: [{"target_id": target, "edge_type": "DEPENDS_ON"}, {"target_id": target, "edge_type": "DEPENDS_ON"}]}),
        encoding="utf-8",
    )
    plan = build_semantic_graph_link_migration_plan(database, graph, project=PROJECT)

    assert plan["candidate_count"] == 1
    assert plan["candidate_keys_digest"]
    assert plan["bindings"]["legacy_json_sha256"]
    assert source not in str(plan)
    assert target not in str(plan)
    assert plan["execution"]["read_only"] is True
    assert plan["execution"]["sqlite_written"] is False


def test_apply_inserts_only_exact_allowlisted_link_and_keeps_graph_unchanged(tmp_path: Path) -> None:
    database, graph, backup, _source, _target = _fixture(tmp_path)
    before_graph = graph.read_bytes()
    plan = build_semantic_graph_link_migration_plan(database, graph, project=PROJECT)

    result = apply_semantic_graph_link_migration(
        database,
        graph,
        backup,
        plan,
        expected_plan_digest=plan["plan_digest"],
        confirm_operator=True,
        maintenance_window_open=True,
    )

    assert result["inserted"] == 1
    assert result["preserved_existing_canonical"] == 0
    assert _count_links(database) == 1
    assert graph.read_bytes() == before_graph
    with sqlite3.connect(database) as connection:
        metadata = json.loads(connection.execute("SELECT metadata_json FROM memory_links").fetchone()[0])
    assert metadata["migration"]["source"] == "legacy_semantic_graph"
    assert metadata["migration"]["legacy_json_sha256"] == plan["bindings"]["legacy_json_sha256"]
    assert metadata["migration"]["reviewed_plan_digest"] == plan["plan_digest"]
    assert result["execution"]["qdrant_written"] is False


def test_apply_is_idempotent_after_a_fresh_snapshot_and_backup(tmp_path: Path) -> None:
    database, graph, backup, _source, _target = _fixture(tmp_path)
    first = build_semantic_graph_link_migration_plan(database, graph, project=PROJECT)
    apply_semantic_graph_link_migration(
        database, graph, backup, first, expected_plan_digest=first["plan_digest"], confirm_operator=True, maintenance_window_open=True
    )
    refreshed_backup = tmp_path / "backup-after-first.sqlite3"
    create_verified_sqlite_backup(database, refreshed_backup)
    refreshed = build_semantic_graph_link_migration_plan(database, graph, project=PROJECT)

    second = apply_semantic_graph_link_migration(
        database, graph, refreshed_backup, refreshed, expected_plan_digest=refreshed["plan_digest"], confirm_operator=True, maintenance_window_open=True
    )

    assert second["inserted"] == 0
    assert second["already_migrated"] == 1
    assert _count_links(database) == 1


def test_apply_preserves_preexisting_canonical_link_with_other_provenance(tmp_path: Path) -> None:
    database, graph, _backup, source, target = _fixture(tmp_path)
    repository = SQLiteMemoryRepository(database)
    repository.save_link(
        MemoryLink(
            id="link_bhm_human_001",
            project=PROJECT,
            source_id=source,
            target_id=target,
            relation="depends_on",
            metadata={"provenance": {"source": "human"}},
        )
    )
    backup = tmp_path / "backup-with-human-link.sqlite3"
    create_verified_sqlite_backup(database, backup)
    plan = build_semantic_graph_link_migration_plan(database, graph, project=PROJECT)

    result = apply_semantic_graph_link_migration(
        database, graph, backup, plan, expected_plan_digest=plan["plan_digest"], confirm_operator=True, maintenance_window_open=True
    )

    assert result["inserted"] == 0
    assert result["preserved_existing_canonical"] == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT link_id FROM memory_links").fetchone()[0] == "link_bhm_human_001"


def test_apply_rolls_back_every_insert_on_injected_failure(tmp_path: Path) -> None:
    database, graph, backup, _source, _target = _fixture(tmp_path)
    plan = build_semantic_graph_link_migration_plan(database, graph, project=PROJECT)

    with pytest.raises(SemanticGraphLinkMigrationError, match="injected"):
        apply_semantic_graph_link_migration(
            database,
            graph,
            backup,
            plan,
            expected_plan_digest=plan["plan_digest"],
            confirm_operator=True,
            maintenance_window_open=True,
            inject_failure_after=1,
        )

    assert _count_links(database) == 0


def test_apply_rejects_graph_or_link_set_drift_before_writing(tmp_path: Path) -> None:
    database, graph, backup, source, target = _fixture(tmp_path)
    plan = build_semantic_graph_link_migration_plan(database, graph, project=PROJECT)
    graph.write_text(json.dumps({source: [{"target_id": target, "edge_type": "UPGRADES"}]}), encoding="utf-8")

    with pytest.raises(SemanticGraphLinkMigrationError, match="backup does not match"):
        apply_semantic_graph_link_migration(
            database, graph, backup, plan, expected_plan_digest=plan["plan_digest"], confirm_operator=True, maintenance_window_open=True
        )
    assert _count_links(database) == 0


def test_inactive_and_unknown_relations_are_never_candidates(tmp_path: Path) -> None:
    database, graph, _backup, source, target = _fixture(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE memories SET lifecycle='archived' WHERE memory_id=?", (target,))
        connection.commit()
    graph.write_text(json.dumps({source: [{"target_id": target, "edge_type": "UPGRADES"}]}), encoding="utf-8")

    plan = build_semantic_graph_link_migration_plan(database, graph, project=PROJECT)

    assert plan["candidate_count"] == 0


def test_apply_rejects_missing_operator_gates_and_tampered_plan(tmp_path: Path) -> None:
    database, graph, backup, _source, _target = _fixture(tmp_path)
    plan = build_semantic_graph_link_migration_plan(database, graph, project=PROJECT)

    with pytest.raises(SemanticGraphLinkMigrationError, match="operator confirmation"):
        apply_semantic_graph_link_migration(
            database, graph, backup, plan, expected_plan_digest=plan["plan_digest"], maintenance_window_open=True
        )
    with pytest.raises(SemanticGraphLinkMigrationError, match="maintenance window"):
        apply_semantic_graph_link_migration(
            database, graph, backup, plan, expected_plan_digest=plan["plan_digest"], confirm_operator=True
        )
    tampered = {**plan, "candidate_count": 99}
    with pytest.raises(SemanticGraphLinkMigrationError, match="plan digest"):
        apply_semantic_graph_link_migration(
            database, graph, backup, tampered, expected_plan_digest=plan["plan_digest"], confirm_operator=True, maintenance_window_open=True
        )
    assert _count_links(database) == 0


def test_apply_rejects_authoritative_database_as_its_own_backup(tmp_path: Path) -> None:
    database, graph, _backup, _source, _target = _fixture(tmp_path)
    plan = build_semantic_graph_link_migration_plan(database, graph, project=PROJECT)

    with pytest.raises(SemanticGraphLinkMigrationError, match="distinct"):
        apply_semantic_graph_link_migration(
            database, graph, database, plan, expected_plan_digest=plan["plan_digest"], confirm_operator=True, maintenance_window_open=True
        )
    assert _count_links(database) == 0
