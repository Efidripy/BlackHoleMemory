"""Run a disposable sidecar-to-SQLite parity rehearsal without live writes."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from blackholememory.task_graph import TASK_GRAPH_BUILD_VERSION
from blackholememory.task_graph import _initialize_schema as initialize_task_graph_schema
from blackholememory.task_graph import build_task_graph as build_authoritative_task_graph


SCHEMA_VERSION = "bhm.sidecar-parity-rehearsal.v1"
ALLOWED_RELATIONS = frozenset({"depends_on"})
VALIDATOR = Path(__file__).with_name("validate-bhm-sidecar-mapping.py")


def _load_mapping_validator():
    spec = importlib.util.spec_from_file_location("bhm_sidecar_mapping", VALIDATOR)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load sidecar mapping validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_fixture(live: Path) -> None:
    live.mkdir(parents=True, exist_ok=True)
    records = {
        "memory-links.json": [
            {
                "id": "rehearsal-link-1",
                "project": "blackholememory",
                "source_id": "memory-1",
                "target_id": "memory-2",
                "relation": "supports",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "metadata": {"fixture": True},
            }
        ],
        "checkpoints.json": [
            {
                "id": "rehearsal-checkpoint-1",
                "memory_id": "memory-1",
                "project": "blackholememory",
                "checkpoint_type": "workflow",
                "title": "rehearsal",
                "content": "disposable",
            }
        ],
        "session-records.json": [
            {
                "id": "rehearsal-session-1",
                "project": "blackholememory",
                "title": "rehearsal",
                "done": "fixture complete",
                "next": "none",
                "checks": "disposable",
            }
        ],
        "tasks.json": [
            {
                "id": "rehearsal-task-record-1",
                "task_id": "rehearsal-task-1",
                "project": "blackholememory",
                "title": "rehearsal",
                "status": "open",
                "dependencies": [],
            },
            {
                "id": "rehearsal-task-record-2",
                "task_id": "rehearsal-task-2",
                "project": "blackholememory",
                "title": "dependent rehearsal",
                "status": "open",
                "dependencies": ["rehearsal-task-1"],
            },
        ],
    }
    for name, value in records.items():
        (live / name).write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _create_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE memories (memory_id TEXT);
            CREATE TABLE memory_outbox (event_id TEXT, status TEXT);
            CREATE TABLE memory_links (
                link_id TEXT PRIMARY KEY, project TEXT NOT NULL, source_id TEXT NOT NULL,
                target_id TEXT NOT NULL, relation TEXT NOT NULL, created_at TEXT,
                updated_at TEXT, metadata_json TEXT NOT NULL
            );
            CREATE TABLE memory_artifacts (
                artifact_type TEXT NOT NULL, artifact_id TEXT NOT NULL, project TEXT NOT NULL,
                memory_id TEXT, lifecycle TEXT NOT NULL, created_at TEXT, updated_at TEXT,
                payload_json TEXT NOT NULL, PRIMARY KEY (artifact_type, artifact_id)
            );
            """
        )
        initialize_task_graph_schema(connection)
        connection.commit()
    finally:
        connection.close()


def _simulate_candidate_projection(connection: sqlite3.Connection, fixture: Path) -> dict[str, int]:
    links = json.loads((fixture / "memory-links.json").read_text(encoding="utf-8"))
    checkpoints = json.loads((fixture / "checkpoints.json").read_text(encoding="utf-8"))
    sessions = json.loads((fixture / "session-records.json").read_text(encoding="utf-8"))
    for item in links:
        connection.execute(
            "INSERT INTO memory_links(link_id, project, source_id, target_id, relation, created_at, updated_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item["id"], item["project"], item["source_id"], item["target_id"],
                item["relation"], item.get("created_at"), item.get("updated_at"),
                json.dumps(item.get("metadata", {}), sort_keys=True),
            ),
        )
    for artifact_type, items in (("checkpoint", checkpoints), ("session_record", sessions)):
        for item in items:
            connection.execute(
                "INSERT INTO memory_artifacts(artifact_type, artifact_id, project, memory_id, lifecycle, created_at, updated_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    artifact_type, item["id"], item["project"], item.get("memory_id"),
                    "active", item.get("created_at"), item.get("updated_at"),
                    json.dumps(item, sort_keys=True),
                ),
            )
    return {
        "memory_links": int(connection.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]),
        "memory_artifacts": int(connection.execute("SELECT COUNT(*) FROM memory_artifacts").fetchone()[0]),
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate_task_contract(tasks: list[dict[str, Any]], *, project: str) -> None:
    """Keep the rehearsal's strict source checks before canonical materialization."""

    ordered = sorted(tasks, key=lambda item: (str(item.get("task_id") or ""), str(item.get("id") or "")))
    task_ids: set[str] = set()
    for task in ordered:
        task_id = str(task.get("task_id") or "").strip()
        if not task_id:
            raise ValueError("task_id is required")
        source_project = str(task.get("project") or "").strip()
        if source_project and source_project != project:
            raise ValueError(f"project mismatch: {source_project} != {project}")
        if task_id in task_ids:
            raise ValueError(f"duplicate task_id: {task_id}")
        task_ids.add(task_id)
    for task in ordered:
        source_id = str(task.get("task_id") or "")
        dependencies = task.get("dependencies")
        if dependencies in (None, ""):
            continue
        if not isinstance(dependencies, list):
            raise ValueError(f"dependencies must be a list: {source_id}")
        seen_dependencies: set[tuple[str, str]] = set()
        for value in dependencies:
            if isinstance(value, dict):
                dependency = str(value.get("task_id") or "").strip()
                relation = str(value.get("relation") or "").strip()
                dependency_project = str(value.get("project") or "").strip()
                if dependency_project and dependency_project != project:
                    raise ValueError(f"dependency project mismatch: {dependency_project} != {project}")
            else:
                dependency = str(value).strip()
                relation = "depends_on"
            if not dependency:
                raise ValueError(f"empty dependency: {source_id}")
            if (dependency, relation) in seen_dependencies:
                raise ValueError(f"duplicate dependency: {source_id}->{dependency}")
            seen_dependencies.add((dependency, relation))
            if dependency not in task_ids:
                raise ValueError(f"unknown dependency: {source_id}->{dependency}")
            if relation not in ALLOWED_RELATIONS:
                raise ValueError(f"relation not allowed: {relation}")


def build_task_graph(tasks: list[dict[str, Any]], *, project: str) -> dict[str, Any]:
    """Materialize the rehearsal graph through the canonical production builder."""

    _validate_task_contract(tasks, project=project)
    with tempfile.TemporaryDirectory(prefix="bhm-task-graph-materialize-") as temporary:
        database = Path(temporary) / "graph.sqlite3"
        connection = sqlite3.connect(database)
        try:
            result = build_authoritative_task_graph(
                database,
                project=project,
                tasks=tasks,
                source_kind="json_sidecar_task",
                connection=connection,
                publish=False,
                as_of="2026-01-01T00:00:00Z",
                summary_extra={"edge_completeness": "explicit_fixture", "edge_policy": "allowlisted"},
            )
            rows = connection.execute(
                "SELECT node_key, project, entity_type, entity_id, source_id, source_sha256, payload_json, node_sha256 "
                "FROM task_graph_nodes WHERE snapshot_id = ? ORDER BY node_key",
                (result["snapshot_id"],),
            ).fetchall()
            nodes = [
                {
                    "node_key": str(row[0]),
                    "project": str(row[1]),
                    "entity_type": str(row[2]),
                    "entity_id": str(row[3]),
                    "source_id": str(row[4]),
                    "source_sha256": str(row[5]),
                    "payload": json.loads(str(row[6])),
                    "node_sha256": str(row[7]),
                }
                for row in rows
            ]
            edge_rows = connection.execute(
                "SELECT edge_key, source_node_key, target_node_key, relation, source_id, source_sha256, confidence, payload_json, edge_sha256 "
                "FROM task_graph_edges WHERE snapshot_id = ? ORDER BY edge_key",
                (result["snapshot_id"],),
            ).fetchall()
            edges = [
                {
                    "edge_key": str(row[0]),
                    "source_node_key": str(row[1]),
                    "target_node_key": str(row[2]),
                    "relation": str(row[3]),
                    "source_id": str(row[4]),
                    "source_sha256": str(row[5]),
                    "confidence": float(row[6]),
                    "payload": json.loads(str(row[7])),
                    "edge_sha256": str(row[8]),
                }
                for row in edge_rows
            ]
            return {
                "builder_version": TASK_GRAPH_BUILD_VERSION,
                "project": project,
                "source_digest": _sha256(sorted(tasks, key=lambda item: (str(item.get("task_id") or ""), str(item.get("id") or "")))),
                "snapshot_id": result["snapshot_id"],
                "graph_digest": result["graph_digest"],
                "nodes": nodes,
                "edges": edges,
            }
        finally:
            connection.close()


def _run_project_isolation_rehearsal() -> dict[str, Any]:
    """Prove that identical task IDs remain isolated across source projects."""

    task_a = {
        "id": "project-a-record",
        "task_id": "shared-task-id",
        "project": "project-a",
        "title": "project A",
        "status": "open",
        "dependencies": [],
    }
    task_b = {**task_a, "id": "project-b-record", "project": "project-b", "title": "project B"}
    graph_a = build_task_graph([task_a], project="project-a")
    graph_b = build_task_graph([task_b], project="project-b")
    mismatch_rejected = False
    try:
        build_task_graph([task_b], project="project-a")
    except ValueError as error:
        mismatch_rejected = str(error) == "project mismatch: project-b != project-a"
    canonical_alias_rejected = False
    try:
        build_task_graph([{**task_a, "project": "BlackHoleMemory"}], project="blackholememory")
    except ValueError as error:
        canonical_alias_rejected = str(error) == "project mismatch: BlackHoleMemory != blackholememory"
    cross_project_dependency_rejected = False
    try:
        build_task_graph(
            [
                {
                    **task_a,
                    "dependencies": [{"task_id": "shared-task-id", "project": "project-b"}],
                }
            ],
            project="project-a",
        )
    except ValueError as error:
        cross_project_dependency_rejected = (
            str(error) == "dependency project mismatch: project-b != project-a"
        )
    return {
        "proven": bool(
            graph_a["nodes"][0]["node_key"] != graph_b["nodes"][0]["node_key"]
            and graph_a["snapshot_id"] != graph_b["snapshot_id"]
            and graph_a["graph_digest"] != graph_b["graph_digest"]
            and mismatch_rejected
            and canonical_alias_rejected
            and cross_project_dependency_rejected
        ),
        "same_task_id": True,
        "node_keys_distinct": graph_a["nodes"][0]["node_key"] != graph_b["nodes"][0]["node_key"],
        "snapshots_distinct": graph_a["snapshot_id"] != graph_b["snapshot_id"],
        "graph_digests_distinct": graph_a["graph_digest"] != graph_b["graph_digest"],
        "project_mismatch_rejected": mismatch_rejected,
        "canonical_alias_rejected": canonical_alias_rejected,
        "cross_project_dependency_rejected": cross_project_dependency_rejected,
        "conflict_matrix_proven": bool(canonical_alias_rejected and cross_project_dependency_rejected),
        "projects": ["project-a", "project-b"],
    }


def run_rehearsal(repo_root: Path) -> dict[str, Any]:
    root = repo_root.resolve()
    production_db = (root / ".runtime" / "live-memory" / "memories.sqlite3").resolve()
    with tempfile.TemporaryDirectory(prefix="bhm-sidecar-rehearsal-") as temporary:
        temporary_root = Path(temporary)
        live = temporary_root / ".runtime" / "live-memory"
        _write_fixture(live)
        database = live / "memories.sqlite3"
        _create_database(database)
        validator = _load_mapping_validator()
        plan = validator.build_mapping_plan(root, live)
        tasks = json.loads((live / "tasks.json").read_text(encoding="utf-8"))
        graph_first = build_task_graph(tasks, project="blackholememory")
        graph_second = build_task_graph(tasks, project="blackholememory")
        deterministic_graph = graph_first == graph_second
        project_isolation = _run_project_isolation_rehearsal()
        before = {
            "memory_links": 0,
            "memory_artifacts": 0,
        }
        connection = sqlite3.connect(database)
        try:
            connection.execute("BEGIN")
            projected = _simulate_candidate_projection(connection, live)
            graph_projected_result = build_authoritative_task_graph(
                database,
                project="blackholememory",
                tasks=tasks,
                source_kind="json_sidecar_task",
                connection=connection,
                publish=False,
                as_of="2026-01-01T00:00:00Z",
                summary_extra={"edge_completeness": "explicit_fixture", "edge_policy": "allowlisted"},
            )
            graph_projected = {
                "task_graph_nodes": int(connection.execute("SELECT COUNT(*) FROM task_graph_nodes").fetchone()[0]),
                "task_graph_edges": int(connection.execute("SELECT COUNT(*) FROM task_graph_edges").fetchone()[0]),
                "task_graph_snapshots": int(connection.execute("SELECT COUNT(*) FROM task_graph_snapshots").fetchone()[0]),
                "task_graph_current": int(connection.execute("SELECT COUNT(*) FROM task_graph_current").fetchone()[0]),
            }
            connection.rollback()
            after = {
                "memory_links": int(connection.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]),
                "memory_artifacts": int(connection.execute("SELECT COUNT(*) FROM memory_artifacts").fetchone()[0]),
                "task_graph_nodes": int(connection.execute("SELECT COUNT(*) FROM task_graph_nodes").fetchone()[0]),
                "task_graph_edges": int(connection.execute("SELECT COUNT(*) FROM task_graph_edges").fetchone()[0]),
                "task_graph_snapshots": int(connection.execute("SELECT COUNT(*) FROM task_graph_snapshots").fetchone()[0]),
                "task_graph_current": int(connection.execute("SELECT COUNT(*) FROM task_graph_current").fetchone()[0]),
            }
        finally:
            connection.close()
        before.update({"task_graph_nodes": 0, "task_graph_edges": 0, "task_graph_snapshots": 0, "task_graph_current": 0})
        rollback_verified = (
            after == before
            and projected == {"memory_links": 1, "memory_artifacts": 2}
            and graph_projected == {"task_graph_nodes": 2, "task_graph_edges": 1, "task_graph_snapshots": 1, "task_graph_current": 0}
        )
        task_mapping = next(item for item in plan["mappings"] if item["source"] == "tasks.json")
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": bool(plan["ok"] and rollback_verified and project_isolation["proven"]),
            "production_mutation": False,
            "production_database_path": str(production_db),
            "temporary_database_path": str(database),
            "validator_read_only": plan["read_only"],
            "staging_authorized": False,
            "parity_proven": False,
            "fixture_parity_proven": bool(deterministic_graph and rollback_verified and project_isolation["proven"]),
            "project_isolation_proven": project_isolation["proven"],
            "project_conflict_matrix_proven": project_isolation["conflict_matrix_proven"],
            "project_isolation": project_isolation,
            "rollback_verified": rollback_verified,
            "projected_candidate_counts": projected,
        "projected_graph_counts": graph_projected,
            "post_rollback_counts": after,
            "graph": {
                "snapshot_id": graph_first["snapshot_id"],
                "graph_digest": graph_first["graph_digest"],
                "builder_version": graph_first["builder_version"],
                "node_count": len(graph_first["nodes"]),
                "edge_count": len(graph_first["edges"]),
                "deterministic": deterministic_graph,
                "relation_policy": sorted(ALLOWED_RELATIONS),
            },
            "canonical_projection": {
                "builder_version": graph_projected_result["execution"].get("builder_version", TASK_GRAPH_BUILD_VERSION),
                "publication": graph_projected_result["publication"],
                "current_pointer_written": graph_projected["task_graph_current"] > 0,
            },
            "blocked_sources": [task_mapping["source"]],
            "blockers": task_mapping["blockers"],
            "contracts": {
                "null_field_policy": plan["null_field_policy"]["schema_version"],
                "graph_builder": plan["graph_builder_contract"]["schema_version"],
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    result = run_rehearsal(args.repo_root)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
