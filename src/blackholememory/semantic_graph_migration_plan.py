"""Read-only, content-free migration planning for the legacy semantic graph.

The JSON graph is a historical accelerator, not an authority.  This module
never copies, maps, deletes, or activates an edge.  It only classifies each
legacy edge against current SQLite endpoint state and an explicitly activated
per-project ontology schema, so an operator can later select a narrow typed
batch without guessing relation semantics.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .filesystem_boundaries import assert_safe_path
from .filesystem_boundaries import read_bytes_safely
from .ontology_registry import ACTIVATION_ARTIFACT_TYPE
from .ontology_registry import ARTIFACT_TYPE
from .ontology_registry import OntologyRegistryError
from .ontology_registry import OntologySchema
from .ontology_registry import resolve_active_schema


SCHEMA_VERSION = "bhm.semantic-graph-migration-plan.v1"
_MAX_LEGACY_EDGES = 10_000
_MAX_CANDIDATES = 256
_SQLITE_IN_CHUNK = 500
_LEGACY_RELATION_EXACT_MAP = {"DEPENDS_ON": "depends_on"}


class SemanticGraphMigrationPlanError(RuntimeError):
    """Raised when an authoritative planning input is incomplete or unsafe."""


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _id_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_legacy_edges(path: Path) -> tuple[dict[str, str], ...]:
    safe_path = assert_safe_path(path).resolve()
    if not safe_path.is_file():
        raise SemanticGraphMigrationPlanError("legacy semantic graph is missing")
    try:
        raw = json.loads(read_bytes_safely(safe_path, max_bytes=8 * 1024 * 1024).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SemanticGraphMigrationPlanError("legacy semantic graph is invalid") from exc
    if not isinstance(raw, Mapping):
        raise SemanticGraphMigrationPlanError("legacy semantic graph must be an object")

    edges: list[dict[str, str]] = []
    for source_id, items in raw.items():
        source = str(source_id or "").strip()
        if not source or not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            target = str(item.get("target_id") or "").strip()
            relation = str(item.get("edge_type") or "").strip().upper()
            if not target or not relation:
                continue
            edges.append({"source_id": source, "target_id": target, "legacy_relation": relation})
            if len(edges) > _MAX_LEGACY_EDGES:
                raise SemanticGraphMigrationPlanError("legacy semantic graph exceeds edge bound")
    return tuple(sorted(edges, key=lambda item: (item["source_id"], item["target_id"], item["legacy_relation"])))


def _read_sqlite_state(
    database: Path,
    endpoint_ids: tuple[str, ...],
) -> tuple[dict[str, tuple[str, str]], dict[str, OntologySchema | None]]:
    safe_path = assert_safe_path(database).resolve()
    if not safe_path.is_file():
        raise SemanticGraphMigrationPlanError("authoritative SQLite database is missing")
    if not endpoint_ids:
        return {}, {}

    try:
        connection = sqlite3.connect(f"file:{safe_path.as_posix()}?mode=ro", uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        if str(connection.execute("PRAGMA quick_check").fetchone()[0]).casefold() != "ok":
            raise SemanticGraphMigrationPlanError("authoritative SQLite quick_check failed")
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"memories", "memory_artifacts"}.issubset(tables):
            raise SemanticGraphMigrationPlanError("authoritative SQLite planning tables are missing")

        endpoints: dict[str, tuple[str, str]] = {}
        for start in range(0, len(endpoint_ids), _SQLITE_IN_CHUNK):
            chunk = endpoint_ids[start : start + _SQLITE_IN_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            for row in connection.execute(
                f"SELECT memory_id, project, lifecycle FROM memories WHERE memory_id IN ({placeholders})",
                chunk,
            ):
                endpoints[str(row["memory_id"])] = (str(row["project"]), str(row["lifecycle"]))

        projects = tuple(sorted({project for project, _lifecycle in endpoints.values()}))
        schemas = _read_active_schemas(connection, projects)
    except SemanticGraphMigrationPlanError:
        raise
    except sqlite3.Error as exc:
        raise SemanticGraphMigrationPlanError("authoritative SQLite planning read failed") from exc
    finally:
        if "connection" in locals():
            connection.close()
    return endpoints, schemas


def _read_active_schemas(
    connection: sqlite3.Connection,
    projects: tuple[str, ...],
) -> dict[str, OntologySchema | None]:
    result: dict[str, OntologySchema | None] = {project: None for project in projects}
    if not projects:
        return result
    placeholders = ",".join("?" for _ in projects)
    records_by_project: dict[str, list[dict[str, Any]]] = {project: [] for project in projects}
    markers: dict[str, dict[str, Any]] = {}
    rows = connection.execute(
        "SELECT artifact_type, artifact_id, project, payload_json "
        "FROM memory_artifacts "
        f"WHERE project IN ({placeholders}) AND artifact_type IN (?, ?)",
        (*projects, ARTIFACT_TYPE, ACTIVATION_ARTIFACT_TYPE),
    )
    for row in rows:
        project = str(row["project"])
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except json.JSONDecodeError as exc:
            raise SemanticGraphMigrationPlanError("ontology artifact payload is invalid") from exc
        if not isinstance(payload, Mapping):
            raise SemanticGraphMigrationPlanError("ontology artifact payload is invalid")
        record = {"id": str(row["artifact_id"]), "project": project, **dict(payload)}
        if str(row["artifact_type"]) == ARTIFACT_TYPE:
            records_by_project[project].append(record)
        elif record["id"] == f"ontology_activation_{project}":
            if project in markers:
                raise SemanticGraphMigrationPlanError("ontology activation marker is ambiguous")
            markers[project] = record
    for project in projects:
        marker = markers.get(project)
        if marker is None:
            continue
        try:
            result[project] = resolve_active_schema(
                project=project,
                registry_records=records_by_project[project],
                activation_record=marker,
            )
        except OntologyRegistryError as exc:
            raise SemanticGraphMigrationPlanError("active ontology schema is invalid") from exc
    return result


def build_semantic_graph_migration_plan(
    edges: tuple[Mapping[str, str], ...],
    endpoints: Mapping[str, tuple[str, str]],
    active_schemas: Mapping[str, OntologySchema | None],
    *,
    project: str | None = None,
) -> dict[str, Any]:
    """Classify legacy edges with no implicit relation mapping or write path."""

    selected_project = str(project or "").strip()
    reason_counts: Counter[str] = Counter()
    candidates: list[dict[str, str]] = []
    normalized_edges: list[dict[str, str]] = []
    for edge in edges:
        source = str(edge.get("source_id") or "").strip()
        target = str(edge.get("target_id") or "").strip()
        legacy_relation = str(edge.get("legacy_relation") or "").strip().upper()
        if not source or not target or not legacy_relation:
            raise SemanticGraphMigrationPlanError("legacy edge is incomplete")
        normalized_edges.append({"source_id": source, "target_id": target, "legacy_relation": legacy_relation})
    if len(normalized_edges) > _MAX_LEGACY_EDGES:
        raise SemanticGraphMigrationPlanError("legacy semantic graph exceeds edge bound")

    for edge in sorted(normalized_edges, key=lambda item: (item["source_id"], item["target_id"], item["legacy_relation"])):
        source = endpoints.get(edge["source_id"])
        target = endpoints.get(edge["target_id"])
        candidate_project = source[0] if source is not None else ""
        relation = _LEGACY_RELATION_EXACT_MAP.get(edge["legacy_relation"])
        schema = active_schemas.get(candidate_project)
        if source is None or target is None:
            reason = "endpoint_missing_from_sqlite"
        elif source[0] != target[0]:
            reason = "cross_project"
        elif source[1] != "active" or target[1] != "active":
            reason = "endpoint_not_active"
        elif selected_project and source[0] != selected_project:
            reason = "outside_selected_project"
        elif relation is None:
            reason = "legacy_relation_requires_schema_decision"
        elif schema is None:
            reason = "ontology_schema_not_active"
        elif schema.relation(relation) is None:
            reason = "ontology_relation_not_allowlisted"
        else:
            reason = "eligible_operator_review"
            if len(candidates) < _MAX_CANDIDATES:
                candidates.append(
                    {
                        "project": source[0],
                        "legacy_relation": edge["legacy_relation"],
                        "relation": relation,
                        "schema_digest": schema.digest(),
                        "source_id_digest": _id_digest(edge["source_id"]),
                        "target_id_digest": _id_digest(edge["target_id"]),
                        "required_gates": "same_snapshot,backup,dry_run,operator_approval,parity_smoke",
                    }
                )
        reason_counts[reason] += 1

    authority_snapshot = sorted(
        {
            (identifier, project_name, lifecycle)
            for identifier, (project_name, lifecycle) in endpoints.items()
        }
    )
    schema_snapshot = {
        project_name: schema.digest() if schema is not None else ""
        for project_name, schema in sorted(active_schemas.items())
    }
    core = {
        "schema_version": SCHEMA_VERSION,
        "project": selected_project or None,
        "edge_count": len(normalized_edges),
        "reason_counts": dict(sorted(reason_counts.items())),
        "candidate_count": len(candidates),
        "candidate_omitted_count": reason_counts["eligible_operator_review"] - len(candidates),
        "candidates": candidates,
        "bindings": {
            "authority_snapshot_digest": _digest(
                [( _id_digest(identifier), project_name, lifecycle) for identifier, project_name, lifecycle in authority_snapshot]
            ),
            "legacy_graph_digest": _digest(
                [
                    {
                        "source_id_digest": _id_digest(edge["source_id"]),
                        "target_id_digest": _id_digest(edge["target_id"]),
                        "legacy_relation": edge["legacy_relation"],
                    }
                    for edge in sorted(normalized_edges, key=lambda item: (item["source_id"], item["target_id"], item["legacy_relation"]))
                ]
            ),
            "active_schema_digests": schema_snapshot,
        },
        "execution": {
            "read_only": True,
            "sqlite_mutation": False,
            "legacy_json_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
            "schema_activation": False,
            "link_migration_apply": False,
            "raw_content_disclosed": False,
            "raw_memory_ids_disclosed": False,
        },
        "operator_gates": {
            "automatic_relation_mapping": False,
            "automatic_link_migration": False,
            "required_before_apply": ["same_snapshot", "hash_verified_backup", "typed_dry_run", "operator_approval", "post_apply_parity_smoke"],
        },
    }
    return {**core, "plan_digest": _digest(core)}


def build_live_semantic_graph_migration_plan(
    database: Path | str,
    semantic_graph: Path | str,
    *,
    project: str | None = None,
) -> dict[str, Any]:
    """Read current sources through SQLite ``mode=ro`` and bounded JSON input."""

    edges, endpoints, schemas = load_semantic_graph_migration_inputs(database, semantic_graph)
    return build_semantic_graph_migration_plan(edges, endpoints, schemas, project=project)


def load_semantic_graph_migration_inputs(
    database: Path | str,
    semantic_graph: Path | str,
) -> tuple[tuple[dict[str, str], ...], dict[str, tuple[str, str]], dict[str, OntologySchema | None]]:
    """Load bounded legacy inputs through the same read-only authority boundary."""

    edges = _read_legacy_edges(Path(semantic_graph))
    endpoint_ids = tuple(sorted({edge["source_id"] for edge in edges} | {edge["target_id"] for edge in edges}))
    endpoints, schemas = _read_sqlite_state(Path(database), endpoint_ids)
    return edges, endpoints, schemas


__all__ = [
    "SCHEMA_VERSION",
    "SemanticGraphMigrationPlanError",
    "build_live_semantic_graph_migration_plan",
    "build_semantic_graph_migration_plan",
    "load_semantic_graph_migration_inputs",
]
