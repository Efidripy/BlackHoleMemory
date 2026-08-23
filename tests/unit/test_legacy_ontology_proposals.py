from __future__ import annotations

import json
import sqlite3

from blackholememory.legacy_ontology_proposals import SCHEMA_VERSION
from blackholememory.legacy_ontology_proposals import build_legacy_ontology_proposals
from blackholememory.legacy_ontology_proposals import build_live_legacy_ontology_proposals
from blackholememory.ontology_registry import OntologySchema


def _active_schema(project: str) -> OntologySchema:
    return OntologySchema.model_validate(
        {
            "project": project,
            "owner": "operator",
            "activation_status": "active",
            "entity_types": [{"name": "memory"}],
            "relation_types": [{"name": "depends_on", "source_types": ["memory"], "target_types": ["memory"]}],
        }
    )


def test_proposals_are_deterministic_and_never_map_upgrades() -> None:
    edges = (
        {"source_id": "a", "target_id": "b", "legacy_relation": "DEPENDS_ON"},
        {"source_id": "a", "target_id": "c", "legacy_relation": "UPGRADES"},
        {"source_id": "x", "target_id": "y", "legacy_relation": "DEPENDS_ON"},
    )
    endpoints = {"a": ("project-a", "active"), "b": ("project-a", "active"), "c": ("project-a", "active"), "x": ("project-b", "active"), "y": ("project-b", "active")}
    first = build_legacy_ontology_proposals(edges, endpoints, {"project-b": _active_schema("project-b")})
    second = build_legacy_ontology_proposals(tuple(reversed(edges)), endpoints, {"project-b": _active_schema("project-b")})

    assert first == second
    assert first["schema_version"] == SCHEMA_VERSION
    assert first["reason_counts"] == {"active_schema_already_present": 1, "legacy_relation_requires_schema_decision": 1, "proposal_candidate": 1}
    assert first["proposals"][0]["project"] == "project-a"
    assert first["proposals"][0]["schema"]["activation_status"] == "proposal"
    assert first["proposals"][0]["schema"]["relation_types"][0]["name"] == "depends_on"
    assert "\"a\"" not in json.dumps(first)
    assert first["execution"]["schema_activated"] is False


def test_live_proposal_adapter_reads_sources_without_mutation(tmp_path) -> None:
    database = tmp_path / "memories.sqlite3"
    graph = tmp_path / "semantic_graph.json"
    connection = sqlite3.connect(database)
    connection.executescript("CREATE TABLE memories (memory_id TEXT PRIMARY KEY, project TEXT NOT NULL, lifecycle TEXT NOT NULL); CREATE TABLE memory_artifacts (artifact_type TEXT NOT NULL, artifact_id TEXT NOT NULL, project TEXT NOT NULL, payload_json TEXT NOT NULL);")
    connection.executemany("INSERT INTO memories(memory_id, project, lifecycle) VALUES (?, ?, ?)", [("source", "project-a", "active"), ("target", "project-a", "active")])
    connection.commit()
    connection.close()
    graph.write_text(json.dumps({"source": [{"target_id": "target", "edge_type": "DEPENDS_ON"}]}), encoding="utf-8")
    before_database, before_graph = database.read_bytes(), graph.read_bytes()

    report = build_live_legacy_ontology_proposals(database, graph)

    assert report["proposals"][0]["project"] == "project-a"
    assert database.read_bytes() == before_database
    assert graph.read_bytes() == before_graph
