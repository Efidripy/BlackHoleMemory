"""Deterministic, rollback-friendly reconciliation of legacy JSON sidecars.

SQLite remains authoritative.  This module only imports the four previously
identified candidate sidecars, preserves their original records inside a
provenance envelope, and never manufactures absent session fields or task
relations.  It is intentionally explicit and is used by a CLI that defaults
to plan-only mode.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

from .task_graph import build_task_graph


SCHEMA_VERSION = "bhm.sidecar-reconciliation.v1"
PLAN_VERSION = "bhm.sidecar-reconciliation-plan.v1"
SOURCE_NAMES = ("memory-links.json", "checkpoints.json", "session-records.json", "tasks.json")


class SidecarReconciliationError(ValueError):
    """Raised before a migration can partially alter an authoritative store."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def _load_records(path: Path) -> tuple[list[dict[str, Any]], str]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SidecarReconciliationError(f"cannot read {path.name}: {type(exc).__name__}") from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise SidecarReconciliationError(f"{path.name} must be a JSON object list")
    return [dict(item) for item in value], sha256(raw)


def _required(record: Mapping[str, Any], field: str, source: str) -> str:
    value = str(record.get(field) or "").strip()
    if not value:
        raise SidecarReconciliationError(f"{source} record is missing required {field}")
    return value


def _duplicates(records: list[dict[str, Any]], field: str, source: str) -> None:
    values = [_required(record, field, source) for record in records]
    duplicate = next((value for value in sorted(values) if values.count(value) > 1), None)
    if duplicate is not None:
        raise SidecarReconciliationError(f"{source} has duplicate {field}: {duplicate}")


def _artifact_row(record: Mapping[str, Any], *, source: str, source_digest: str, artifact_type: str) -> dict[str, Any]:
    record_id = _required(record, "id", source)
    project = _required(record, "project", source)
    missing = [field for field in ("done", "next", "checks") if record.get(field) in (None, "")]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": "legacy_sidecar_artifact",
        "source": {
            "file": source,
            "file_sha256": source_digest,
            "record_id": record_id,
            "record_sha256": sha256(canonical_json(record)),
        },
        # The record is deliberately lossless.  In particular, absent done or
        # next is recorded as absent rather than converted into a completion.
        "legacy_record": dict(record),
        "completeness": {"missing_or_empty": missing, "defaulted_fields": []},
    }
    complete = not missing
    return {
        "artifact_type": artifact_type,
        "artifact_id": record_id,
        "project": project,
        "memory_id": record.get("memory_id"),
        # Incomplete legacy session records are preserved but must never be
        # mistaken for complete active operator evidence.
        "lifecycle": "active" if complete else "archived",
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "payload": payload,
    }


def _link_row(record: Mapping[str, Any], *, source_digest: str) -> dict[str, Any]:
    source = "memory-links.json"
    link_id = _required(record, "id", source)
    project = _required(record, "project", source)
    source_id = _required(record, "source_id", source)
    target_id = _required(record, "target_id", source)
    relation = _required(record, "relation", source)
    metadata = record.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise SidecarReconciliationError("memory-links.json metadata must be an object")
    return {
        "link_id": link_id,
        "project": project,
        "source_id": source_id,
        "target_id": target_id,
        "relation": relation,
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": {
            "legacy_metadata": metadata,
            "sidecar_provenance": {
                "file": source,
                "file_sha256": source_digest,
                "record_sha256": sha256(canonical_json(record)),
            },
        },
    }


def build_reconciliation_plan(runtime_dir: Path | str) -> dict[str, Any]:
    """Build a pure plan from sidecar data; this function never opens SQLite."""

    root = Path(runtime_dir).resolve()
    inputs: dict[str, list[dict[str, Any]]] = {}
    source_digests: dict[str, str] = {}
    for name in SOURCE_NAMES:
        records, digest = _load_records(root / name)
        inputs[name] = records
        source_digests[name] = digest

    _duplicates(inputs["memory-links.json"], "id", "memory-links.json")
    _duplicates(inputs["checkpoints.json"], "id", "checkpoints.json")
    _duplicates(inputs["session-records.json"], "id", "session-records.json")
    _duplicates(inputs["tasks.json"], "task_id", "tasks.json")

    links = [_link_row(record, source_digest=source_digests["memory-links.json"]) for record in inputs["memory-links.json"]]
    artifacts = [
        *[
            _artifact_row(record, source="checkpoints.json", source_digest=source_digests["checkpoints.json"], artifact_type="checkpoint")
            for record in inputs["checkpoints.json"]
        ],
        *[
            _artifact_row(record, source="session-records.json", source_digest=source_digests["session-records.json"], artifact_type="session_record")
            for record in inputs["session-records.json"]
        ],
    ]
    by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in inputs["tasks.json"]:
        project = _required(record, "project", "tasks.json")
        _required(record, "id", "tasks.json")
        _required(record, "title", "tasks.json")
        _required(record, "status", "tasks.json")
        by_project[project].append(record)

    task_groups = [
        {
            "project": project,
            "records": sorted(records, key=lambda item: str(item["task_id"])),
            "source_sha256": source_digests["tasks.json"],
            "source_kind": "json_sidecar_task",
            # Absent dependency fields create no edge; only explicit fields are
            # passed into the canonical graph builder.
            "edge_policy": "explicit_source_relations_only",
        }
        for project, records in sorted(by_project.items())
    ]
    material = {
        "plan_version": PLAN_VERSION,
        "source_digests": source_digests,
        "links": links,
        "artifacts": artifacts,
        "task_groups": task_groups,
    }
    return {
        **material,
        "plan_digest": sha256(canonical_json(material)),
        "counts": {
            "links": len(links),
            "artifacts": len(artifacts),
            "task_projects": len(task_groups),
            "task_nodes": sum(len(group["records"]) for group in task_groups),
            "session_records_with_missing_completion_fields": sum(
                1
                for record in inputs["session-records.json"]
                if record.get("done") in (None, "") or record.get("next") in (None, "")
            ),
        },
        "policy": {
            "sqlite_authoritative": True,
            "writes_qdrant": False,
            "writes_mem0": False,
            "invented_session_fields": False,
            "inferred_task_edges": False,
        },
    }


def _existing_exact(connection: sqlite3.Connection, row: Mapping[str, Any], *, table: str) -> bool:
    if table == "memory_links":
        existing = connection.execute(
            "SELECT project, source_id, target_id, relation, created_at, updated_at, metadata_json "
            "FROM memory_links WHERE link_id = ?",
            (row["link_id"],),
        ).fetchone()
        expected = (
            row["project"], row["source_id"], row["target_id"], row["relation"], row["created_at"], row["updated_at"], canonical_json(row["metadata"]),
        )
    else:
        existing = connection.execute(
            "SELECT project, memory_id, lifecycle, created_at, updated_at, payload_json "
            "FROM memory_artifacts WHERE artifact_type = ? AND artifact_id = ?",
            (row["artifact_type"], row["artifact_id"]),
        ).fetchone()
        expected = (
            row["project"], row["memory_id"], row["lifecycle"], row["created_at"], row["updated_at"], canonical_json(row["payload"]),
        )
    return existing is not None and tuple(existing) == expected


def _assert_no_conflicting_natural_link(connection: sqlite3.Connection, row: Mapping[str, Any]) -> None:
    existing = connection.execute(
        "SELECT link_id FROM memory_links WHERE project = ? AND source_id = ? AND target_id = ? AND relation = ?",
        (row["project"], row["source_id"], row["target_id"], row["relation"]),
    ).fetchone()
    if existing is not None and str(existing[0]) != str(row["link_id"]):
        raise SidecarReconciliationError(f"link natural-key conflict for {row['link_id']}")


def apply_reconciliation_plan(database_path: Path | str, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Apply an exact plan in one SQLite transaction; Qdrant and Mem0 stay untouched."""

    material = {key: plan[key] for key in ("plan_version", "source_digests", "links", "artifacts", "task_groups")}
    if plan.get("plan_version") != PLAN_VERSION or sha256(canonical_json(material)) != plan.get("plan_digest"):
        raise SidecarReconciliationError("plan digest mismatch")
    path = Path(database_path).resolve()
    if not path.exists():
        raise SidecarReconciliationError("authoritative SQLite database is missing")
    connection = sqlite3.connect(str(path), timeout=5.0)
    connection.row_factory = sqlite3.Row
    inserted_links = inserted_artifacts = existing_links = existing_artifacts = 0
    graph_results: list[dict[str, Any]] = []
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        for row in plan["links"]:
            _assert_no_conflicting_natural_link(connection, row)
            if _existing_exact(connection, row, table="memory_links"):
                existing_links += 1
                continue
            conflict = connection.execute("SELECT 1 FROM memory_links WHERE link_id = ?", (row["link_id"],)).fetchone()
            if conflict is not None:
                raise SidecarReconciliationError(f"link id conflict for {row['link_id']}")
            connection.execute(
                "INSERT INTO memory_links(link_id,project,source_id,target_id,relation,created_at,updated_at,metadata_json) VALUES(?,?,?,?,?,?,?,?)",
                (row["link_id"], row["project"], row["source_id"], row["target_id"], row["relation"], row["created_at"], row["updated_at"], canonical_json(row["metadata"])),
            )
            inserted_links += 1
        for row in plan["artifacts"]:
            if _existing_exact(connection, row, table="memory_artifacts"):
                existing_artifacts += 1
                continue
            conflict = connection.execute(
                "SELECT 1 FROM memory_artifacts WHERE artifact_type = ? AND artifact_id = ?",
                (row["artifact_type"], row["artifact_id"]),
            ).fetchone()
            if conflict is not None:
                raise SidecarReconciliationError(f"artifact conflict for {row['artifact_type']}:{row['artifact_id']}")
            connection.execute(
                "INSERT INTO memory_artifacts(artifact_type,artifact_id,project,memory_id,lifecycle,created_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?,?)",
                (row["artifact_type"], row["artifact_id"], row["project"], row["memory_id"], row["lifecycle"], row["created_at"], row["updated_at"], canonical_json(row["payload"])),
            )
            inserted_artifacts += 1
        for group in plan["task_groups"]:
            graph_results.append(
                build_task_graph(
                    path,
                    project=str(group["project"]),
                    tasks=group["records"],
                    source_kind=str(group["source_kind"]),
                    connection=connection,
                    publish=False,
                    summary_extra={
                        "source_file": "tasks.json",
                        "source_sha256": group["source_sha256"],
                        "edge_completeness": "unknown",
                        "edge_policy": group["edge_policy"],
                    },
                )
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "plan_digest": plan["plan_digest"],
        "links": {"inserted": inserted_links, "existing_exact": existing_links},
        "artifacts": {"inserted": inserted_artifacts, "existing_exact": existing_artifacts},
        "task_graphs": [
            {"project": item["project"], "publication": item["publication"], "snapshot_id": item["snapshot_id"], "graph_digest": item["graph_digest"], "summary": item["summary"]}
            for item in graph_results
        ],
        "execution": {"writes_sqlite": True, "writes_qdrant": False, "writes_mem0": False, "models_started": False},
    }
