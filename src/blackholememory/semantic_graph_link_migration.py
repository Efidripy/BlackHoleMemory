"""Fail-closed operator migration from the legacy semantic graph to SQLite.

The legacy JSON graph is an accelerator only.  This module deliberately does
not use the normal REST/MCP link route or ``replace_links``: both paths are
designed for interactive complete snapshots and could replace unrelated
canonical links.  The operator path below is narrow, digest-bound and
transactional.  It only admits active same-project ``DEPENDS_ON`` edges when
the active ontology explicitly allows ``depends_on``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from .filesystem_boundaries import assert_safe_path
from .filesystem_boundaries import read_bytes_safely
from .ontology_registry import ACTIVATION_ARTIFACT_TYPE
from .ontology_registry import ARTIFACT_TYPE
from .ontology_registry import OntologySchema
from .ontology_registry import validate_relation
from .semantic_graph_migration_plan import SemanticGraphMigrationPlanError
from .semantic_graph_migration_plan import _read_legacy_edges
from .semantic_graph_migration_plan import _read_sqlite_state
from .sqlite_retention import sha256_file as _sqlite_sha256_file


SCHEMA_VERSION = "bhm.semantic-graph-link-migration.v1"
MIGRATION_SOURCE = "legacy_semantic_graph"
_MAX_CANDIDATES = 256
_REQUIRED_TABLES = frozenset({"memories", "memory_artifacts", "memory_links"})


class SemanticGraphLinkMigrationError(RuntimeError):
    """Raised when a migration cannot prove all authority and rollback gates."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _graph_sha256(path: Path) -> str:
    return hashlib.sha256(read_bytes_safely(path, max_bytes=8 * 1024 * 1024)).hexdigest()


def _connect(path: Path, *, read_only: bool) -> sqlite3.Connection:
    target = assert_safe_path(path).resolve()
    if not target.is_file():
        raise SemanticGraphLinkMigrationError("authoritative SQLite database is missing")
    try:
        if read_only:
            connection = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True, timeout=5.0)
            connection.execute("PRAGMA query_only=ON")
        else:
            connection = sqlite3.connect(target, timeout=30.0, isolation_level=None)
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA foreign_keys=ON")
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error as exc:
        raise SemanticGraphLinkMigrationError("authoritative SQLite database is unavailable") from exc


def _assert_database_contract(connection: sqlite3.Connection) -> None:
    tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = sorted(_REQUIRED_TABLES - tables)
    if missing:
        raise SemanticGraphLinkMigrationError(f"required SQLite tables are missing: {missing}")
    if str(connection.execute("PRAGMA quick_check").fetchone()[0]).casefold() != "ok":
        raise SemanticGraphLinkMigrationError("authoritative SQLite quick_check failed")
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise SemanticGraphLinkMigrationError("authoritative SQLite foreign_key_check failed")


def _eligible_candidates(
    edges: tuple[Mapping[str, str], ...],
    endpoints: Mapping[str, tuple[str, str]],
    schemas: Mapping[str, OntologySchema | None],
    *,
    project: str,
) -> tuple[dict[str, str], ...]:
    """Return internal raw identities only for the exact allowlisted relation."""

    candidates: dict[tuple[str, str, str, str], dict[str, str]] = {}
    for edge in edges:
        source_id = str(edge.get("source_id") or "").strip()
        target_id = str(edge.get("target_id") or "").strip()
        legacy_relation = str(edge.get("legacy_relation") or "").strip().upper()
        source = endpoints.get(source_id)
        target = endpoints.get(target_id)
        if (
            legacy_relation != "DEPENDS_ON"
            or source is None
            or target is None
            or source[0] != project
            or target[0] != project
            or source[1] != "active"
            or target[1] != "active"
        ):
            continue
        schema = schemas.get(project)
        if schema is None or not validate_relation(schema, "depends_on", "memory", "memory").ok:
            continue
        key = (project, source_id, target_id, "depends_on")
        candidates[key] = {
            "project": project,
            "source_id": source_id,
            "target_id": target_id,
            "relation": "depends_on",
            "legacy_relation": legacy_relation,
            "schema_digest": schema.digest(),
        }
    if len(candidates) > _MAX_CANDIDATES:
        raise SemanticGraphLinkMigrationError("eligible legacy link batch exceeds operator bound")
    return tuple(candidates[key] for key in sorted(candidates))


def _candidate_key(candidate: Mapping[str, str]) -> str:
    return _digest(
        {
            "schema_version": SCHEMA_VERSION,
            "source": MIGRATION_SOURCE,
            "project": candidate["project"],
            "source_id": candidate["source_id"],
            "target_id": candidate["target_id"],
            "relation": candidate["relation"],
            "legacy_relation": candidate["legacy_relation"],
            "schema_digest": candidate["schema_digest"],
        }
    )


def _link_set_digest(database: Path, project: str) -> str:
    connection = _connect(database, read_only=True)
    try:
        _assert_database_contract(connection)
        rows = connection.execute(
            "SELECT link_id, source_id, target_id, relation, created_at, updated_at, metadata_json "
            "FROM memory_links WHERE project=? ORDER BY link_id",
            (project,),
        ).fetchall()
    finally:
        connection.close()
    return _digest(
        [
            {
                "link_id_digest": _text_digest(str(row["link_id"])),
                "source_id_digest": _text_digest(str(row["source_id"])),
                "target_id_digest": _text_digest(str(row["target_id"])),
                "relation": str(row["relation"]),
                "created_at": str(row["created_at"] or ""),
                "updated_at": str(row["updated_at"] or ""),
                "metadata_digest": _text_digest(str(row["metadata_json"])),
            }
            for row in rows
        ]
    )


def _ontology_artifact_bindings(database: Path, project: str, schema: OntologySchema | None) -> dict[str, str]:
    if schema is None:
        return {"schema_digest": "", "activation_payload_digest": "", "registry_payload_digest": ""}
    connection = _connect(database, read_only=True)
    try:
        rows = connection.execute(
            "SELECT artifact_type, artifact_id, payload_json FROM memory_artifacts "
            "WHERE project=? AND artifact_type IN (?, ?)",
            (project, ARTIFACT_TYPE, ACTIVATION_ARTIFACT_TYPE),
        ).fetchall()
    finally:
        connection.close()
    activation = next((row for row in rows if str(row["artifact_type"]) == ACTIVATION_ARTIFACT_TYPE and str(row["artifact_id"]) == f"ontology_activation_{project}"), None)
    if activation is None:
        raise SemanticGraphLinkMigrationError("active ontology activation artifact is missing")
    try:
        activation_payload = json.loads(str(activation["payload_json"]))
    except json.JSONDecodeError as exc:
        raise SemanticGraphLinkMigrationError("active ontology activation artifact is invalid") from exc
    if not isinstance(activation_payload, Mapping):
        raise SemanticGraphLinkMigrationError("active ontology activation artifact is invalid")
    registry_id = str(activation_payload.get("registry_artifact_id") or "")
    registry = next((row for row in rows if str(row["artifact_type"]) == ARTIFACT_TYPE and str(row["artifact_id"]) == registry_id), None)
    if registry is None:
        raise SemanticGraphLinkMigrationError("active ontology registry artifact is missing")
    try:
        registry_payload = json.loads(str(registry["payload_json"]))
    except json.JSONDecodeError as exc:
        raise SemanticGraphLinkMigrationError("active ontology registry artifact is invalid") from exc
    return {
        "schema_digest": schema.digest(),
        "activation_payload_digest": _digest(activation_payload),
        "registry_payload_digest": _digest(registry_payload),
    }


def _schema_sql_digest(database: Path) -> str:
    connection = _connect(database, read_only=True)
    try:
        rows = connection.execute(
            "SELECT type, name, COALESCE(sql, '') AS sql FROM sqlite_master "
            "WHERE name IN ('memories', 'memory_artifacts', 'memory_links') ORDER BY type, name"
        ).fetchall()
    finally:
        connection.close()
    return _digest([dict(row) for row in rows])


def _raw_candidates(database: Path, semantic_graph: Path, project: str) -> tuple[dict[str, str], ...]:
    try:
        edges, endpoints, schemas = _load_inputs(database, semantic_graph)
    except SemanticGraphMigrationPlanError as exc:
        raise SemanticGraphLinkMigrationError(str(exc)) from exc
    return _eligible_candidates(edges, endpoints, schemas, project=project)


def _load_inputs(database: Path, semantic_graph: Path) -> tuple[tuple[dict[str, str], ...], dict[str, tuple[str, str]], dict[str, OntologySchema | None]]:
    # The planner owns safe bounded JSON parsing and read-only SQLite endpoint
    # lookup.  Keeping this adapter narrow prevents the operator path from
    # acquiring a second relation-mapping implementation.
    edges = _read_legacy_edges(semantic_graph)
    endpoint_ids = tuple(sorted({edge["source_id"] for edge in edges} | {edge["target_id"] for edge in edges}))
    return (edges, *_read_sqlite_state(database, endpoint_ids))


def build_semantic_graph_link_migration_plan(
    database: str | Path,
    semantic_graph: str | Path,
    *,
    project: str,
) -> dict[str, Any]:
    """Build a content-free read-only operator plan with exact bindings."""

    database_path = assert_safe_path(database).resolve()
    graph_path = assert_safe_path(semantic_graph).resolve()
    canonical_project = str(project or "").strip()
    if not canonical_project:
        raise SemanticGraphLinkMigrationError("project is required")
    candidates = _raw_candidates(database_path, graph_path, canonical_project)
    _edges, endpoints, schemas = _load_inputs(database_path, graph_path)
    schema = schemas.get(canonical_project)
    endpoint_snapshot = sorted(
        (identifier, name, lifecycle)
        for identifier, (name, lifecycle) in endpoints.items()
        if name == canonical_project
    )
    candidate_view = [
        {
            "candidate_key": _candidate_key(candidate),
            "project": canonical_project,
            "source_id_digest": _text_digest(candidate["source_id"]),
            "target_id_digest": _text_digest(candidate["target_id"]),
            "relation": candidate["relation"],
            "legacy_relation": candidate["legacy_relation"],
            "schema_digest": candidate["schema_digest"],
        }
        for candidate in candidates
    ]
    core = {
        "schema_version": SCHEMA_VERSION,
        "project": canonical_project,
        "candidate_count": len(candidate_view),
        "candidate_keys": candidate_view,
        "candidate_keys_digest": _digest([item["candidate_key"] for item in candidate_view]),
        "bindings": {
            "legacy_json_sha256": _graph_sha256(graph_path),
            "sqlite_endpoint_snapshot_digest": _digest(
                [( _text_digest(identifier), name, lifecycle) for identifier, name, lifecycle in endpoint_snapshot]
            ),
            "sqlite_schema_digest": _schema_sql_digest(database_path),
            "active_ontology": _ontology_artifact_bindings(database_path, canonical_project, schema),
            "project_link_set_digest": _link_set_digest(database_path, canonical_project),
        },
        "execution": {
            "read_only": True,
            "sqlite_written": False,
            "legacy_json_written": False,
            "qdrant_written": False,
            "mem0_written": False,
        },
        "operator_gates": {
            "automatic_apply": False,
            "required_before_apply": [
                "explicit_operator_confirmation",
                "maintenance_window_or_offline_writer_proof",
                "hash_verified_same_snapshot_backup",
                "same_snapshot_recheck",
                "one_transaction",
            ],
        },
    }
    return {**core, "plan_digest": _digest(core)}


def _verify_plan(plan: Mapping[str, Any], expected_plan_digest: str) -> None:
    payload = dict(plan)
    actual = str(payload.pop("plan_digest", ""))
    if not expected_plan_digest or actual != expected_plan_digest or _digest(payload) != expected_plan_digest:
        raise SemanticGraphLinkMigrationError("migration plan digest mismatch; rebuild dry-run")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise SemanticGraphLinkMigrationError("unsupported semantic-link migration plan")


def _verify_backup(plan: Mapping[str, Any], backup: Path, graph: Path, project: str) -> dict[str, Any]:
    backup_plan = build_semantic_graph_link_migration_plan(backup, graph, project=project)
    for key in ("candidate_keys_digest", "bindings"):
        if backup_plan.get(key) != plan.get(key):
            raise SemanticGraphLinkMigrationError("verified backup does not match the reviewed authority snapshot")
    connection = _connect(backup, read_only=True)
    try:
        _assert_database_contract(connection)
    finally:
        connection.close()
    return {"sha256": _sqlite_sha256_file(backup), "bytes": int(backup.stat().st_size), "quick_check": "ok"}


def _migration_metadata(
    candidate: Mapping[str, str],
    candidate_key: str,
    *,
    legacy_json_sha256: str,
    reviewed_plan_digest: str,
) -> dict[str, Any]:
    return {
        "ontology": {
            "schema_digest": candidate["schema_digest"],
            "relation": candidate["relation"],
            "source_type": "memory",
            "target_type": "memory",
            "admission": "legacy-migration",
        },
        "migration": {
            "schema_version": SCHEMA_VERSION,
            "source": MIGRATION_SOURCE,
            "candidate_key": candidate_key,
            "legacy_relation": candidate["legacy_relation"],
            "legacy_json_sha256": legacy_json_sha256,
            "reviewed_plan_digest": reviewed_plan_digest,
        },
    }


def _is_exact_migration_row(metadata_json: str, candidate_key: str) -> bool:
    try:
        metadata = json.loads(metadata_json)
    except json.JSONDecodeError:
        return False
    migration = metadata.get("migration") if isinstance(metadata, Mapping) else None
    return bool(
        isinstance(migration, Mapping)
        and migration.get("schema_version") == SCHEMA_VERSION
        and migration.get("source") == MIGRATION_SOURCE
        and migration.get("candidate_key") == candidate_key
    )


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def apply_semantic_graph_link_migration(
    database: str | Path,
    semantic_graph: str | Path,
    existing_backup: str | Path,
    plan: Mapping[str, Any],
    *,
    expected_plan_digest: str,
    confirm_operator: bool = False,
    maintenance_window_open: bool = False,
    inject_failure_after: int | None = None,
) -> dict[str, Any]:
    """Apply exactly one reviewed batch without replacing canonical links.

    ``maintenance_window_open`` is an explicit proof from the operator that
    writers were drained or the service was stopped.  The SQLite immediate
    transaction then protects the database part of that window.  The function
    is never called by API/MCP request handling.
    """

    if not confirm_operator:
        raise SemanticGraphLinkMigrationError("apply requires explicit operator confirmation")
    if not maintenance_window_open:
        raise SemanticGraphLinkMigrationError("apply requires maintenance window or offline writer proof")
    if inject_failure_after is not None and inject_failure_after < 1:
        raise SemanticGraphLinkMigrationError("inject_failure_after must be positive")
    _verify_plan(plan, expected_plan_digest)
    database_path = assert_safe_path(database).resolve()
    graph_path = assert_safe_path(semantic_graph).resolve()
    backup_path = assert_safe_path(existing_backup).resolve()
    if backup_path == database_path:
        raise SemanticGraphLinkMigrationError("existing backup must be distinct from authoritative SQLite")
    project = str(plan.get("project") or "").strip()
    if not project:
        raise SemanticGraphLinkMigrationError("migration plan has no project")
    backup = _verify_backup(plan, backup_path, graph_path, project)

    connection = _connect(database_path, read_only=False)
    inserted = no_op = preserved = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        # Rebuild after acquiring the writer lock.  This detects any endpoint,
        # schema, link-set or graph drift since the reviewed dry-run.
        locked_plan = build_semantic_graph_link_migration_plan(database_path, graph_path, project=project)
        if locked_plan.get("plan_digest") != expected_plan_digest:
            raise SemanticGraphLinkMigrationError("authoritative inputs changed since plan; rebuild dry-run")
        candidates = _raw_candidates(database_path, graph_path, project)
        for candidate in candidates:
            candidate_key = _candidate_key(candidate)
            existing = connection.execute(
                "SELECT link_id, metadata_json FROM memory_links WHERE project=? AND source_id=? AND target_id=? AND relation=?",
                (candidate["project"], candidate["source_id"], candidate["target_id"], candidate["relation"]),
            ).fetchone()
            if existing is not None:
                if _is_exact_migration_row(str(existing["metadata_json"]), candidate_key):
                    no_op += 1
                else:
                    preserved += 1
                continue
            link_id = f"link_bhm_{candidate_key[:16]}"
            collision = connection.execute("SELECT 1 FROM memory_links WHERE link_id=?", (link_id,)).fetchone()
            if collision is not None:
                raise SemanticGraphLinkMigrationError("deterministic migration link id collision")
            now = _now()
            connection.execute(
                "INSERT INTO memory_links(link_id, project, source_id, target_id, relation, created_at, updated_at, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    link_id,
                    candidate["project"],
                    candidate["source_id"],
                    candidate["target_id"],
                    candidate["relation"],
                    now,
                    now,
                    _canonical_json(
                        _migration_metadata(
                            candidate,
                            candidate_key,
                            legacy_json_sha256=str(plan["bindings"]["legacy_json_sha256"]),
                            reviewed_plan_digest=expected_plan_digest,
                        )
                    ),
                ),
            )
            inserted += 1
            if inject_failure_after is not None and inserted >= inject_failure_after:
                raise SemanticGraphLinkMigrationError("injected semantic-link migration failure")
        if _graph_sha256(graph_path) != str(plan["bindings"]["legacy_json_sha256"]):
            raise SemanticGraphLinkMigrationError("legacy semantic graph changed during apply")
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise SemanticGraphLinkMigrationError("semantic-link migration foreign-key verification failed")
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "action": "applied",
        "project": project,
        "plan_digest": expected_plan_digest,
        "inserted": inserted,
        "already_migrated": no_op,
        "preserved_existing_canonical": preserved,
        "backup": backup,
        "execution": {"sqlite_written": bool(inserted), "legacy_json_written": False, "qdrant_written": False, "mem0_written": False},
        "rollback": "stop writers, verify the recorded backup, restore it offline, then run readiness and link parity smoke",
    }


__all__ = [
    "MIGRATION_SOURCE",
    "SCHEMA_VERSION",
    "SemanticGraphLinkMigrationError",
    "apply_semantic_graph_link_migration",
    "build_semantic_graph_link_migration_plan",
]
