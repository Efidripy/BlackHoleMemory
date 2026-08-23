from __future__ import annotations

import json
import sqlite3

import pytest

from blackholememory.ontology_registry import OntologySchema
from blackholememory.semantic_graph_migration_plan import SemanticGraphMigrationPlanError
from blackholememory.semantic_graph_migration_plan import build_live_semantic_graph_migration_plan
from blackholememory.semantic_graph_migration_plan import build_semantic_graph_migration_plan


def _schema(project: str = "project-a") -> OntologySchema:
    return OntologySchema.model_validate(
        {
            "project": project,
            "owner": "operator",
            "activation_status": "active",
            "entity_types": [{"name": "memory"}],
            "relation_types": [{"name": "depends_on", "source_types": ["memory"], "target_types": ["memory"]}],
        }
    )


def test_plan_is_deterministic_content_free_and_never_remaps_upgrades() -> None:
    schema = _schema()
    edges = (
        {"source_id": "private-source-91", "target_id": "private-target-91", "legacy_relation": "DEPENDS_ON"},
        {"source_id": "private-source-91", "target_id": "private-upgrade-91", "legacy_relation": "UPGRADES"},
        {"source_id": "private-source-91", "target_id": "private-other-91", "legacy_relation": "DEPENDS_ON"},
    )
    endpoints = {
        "private-source-91": ("project-a", "active"),
        "private-target-91": ("project-a", "active"),
        "private-upgrade-91": ("project-a", "active"),
        "private-other-91": ("project-b", "active"),
    }
    first = build_semantic_graph_migration_plan(edges, endpoints, {"project-a": schema})
    second = build_semantic_graph_migration_plan(tuple(reversed(edges)), endpoints, {"project-a": schema})

    assert first == second
    assert first["reason_counts"] == {
        "cross_project": 1,
        "eligible_operator_review": 1,
        "legacy_relation_requires_schema_decision": 1,
    }
    assert first["candidate_count"] == 1
    assert first["candidates"][0]["relation"] == "depends_on"
    assert "private-source-91" not in str(first)
    assert "private-target-91" not in str(first)
    assert first["execution"]["link_migration_apply"] is False
    assert first["operator_gates"]["automatic_relation_mapping"] is False


def test_plan_fails_closed_for_inactive_or_missing_schema() -> None:
    edge = ({"source_id": "source", "target_id": "target", "legacy_relation": "DEPENDS_ON"},)
    endpoints = {"source": ("project-a", "active"), "target": ("project-a", "active")}

    no_schema = build_semantic_graph_migration_plan(edge, endpoints, {})
    assert no_schema["reason_counts"] == {"ontology_schema_not_active": 1}

    inactive = build_semantic_graph_migration_plan(
        edge,
        {"source": ("project-a", "archived"), "target": ("project-a", "active")},
        {"project-a": _schema()},
    )
    assert inactive["reason_counts"] == {"endpoint_not_active": 1}


def _database(path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE memories (memory_id TEXT PRIMARY KEY, project TEXT NOT NULL, lifecycle TEXT NOT NULL);
        CREATE TABLE memory_artifacts (
            artifact_type TEXT NOT NULL,
            artifact_id TEXT NOT NULL,
            project TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (artifact_type, artifact_id)
        );
        """
    )
    connection.executemany(
        "INSERT INTO memories(memory_id, project, lifecycle) VALUES (?, ?, ?)",
        [("source", "project-a", "active"), ("target", "project-a", "active")],
    )
    schema = _schema()
    registry = {"schema": schema.canonical_payload(), "schema_digest": schema.digest()}
    activation = {
        "enabled": True,
        "registry_artifact_id": f"ontology_project-a_1_{schema.digest()[:16]}",
        "schema_digest": schema.digest(),
    }
    connection.executemany(
        "INSERT INTO memory_artifacts(artifact_type, artifact_id, project, payload_json) VALUES (?, ?, ?, ?)",
        [
            ("ontology_registry", activation["registry_artifact_id"], "project-a", json.dumps(registry)),
            ("ontology_registry_activation", "ontology_activation_project-a", "project-a", json.dumps(activation)),
        ],
    )
    connection.commit()
    connection.close()


def test_live_adapter_reads_sqlite_read_only_and_does_not_modify_sources(tmp_path) -> None:
    database = tmp_path / "memories.sqlite3"
    graph = tmp_path / "semantic_graph.json"
    _database(database)
    graph.write_text(json.dumps({"source": [{"target_id": "target", "edge_type": "DEPENDS_ON"}]}), encoding="utf-8")
    before_database = database.read_bytes()
    before_graph = graph.read_bytes()

    plan = build_live_semantic_graph_migration_plan(database, graph, project="project-a")

    assert plan["reason_counts"] == {"eligible_operator_review": 1}
    assert plan["execution"] == {
        "read_only": True,
        "sqlite_mutation": False,
        "legacy_json_mutation": False,
        "qdrant_mutation": False,
        "mem0_mutation": False,
        "schema_activation": False,
        "link_migration_apply": False,
        "raw_content_disclosed": False,
        "raw_memory_ids_disclosed": False,
    }
    assert database.read_bytes() == before_database
    assert graph.read_bytes() == before_graph


def test_live_adapter_fails_closed_for_invalid_activation_marker(tmp_path) -> None:
    database = tmp_path / "memories.sqlite3"
    graph = tmp_path / "semantic_graph.json"
    _database(database)
    graph.write_text(json.dumps({"source": [{"target_id": "target", "edge_type": "DEPENDS_ON"}]}), encoding="utf-8")
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE memory_artifacts SET payload_json = ? WHERE artifact_type = 'ontology_registry_activation'",
        (json.dumps({"enabled": True}),),
    )
    connection.commit()
    connection.close()

    with pytest.raises(SemanticGraphMigrationPlanError, match="active ontology schema is invalid"):
        build_live_semantic_graph_migration_plan(database, graph, project="project-a")
