"""Build a read-only, deterministic mapping and conflict plan for JSON sidecars."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "bhm.sidecar-mapping-plan.v2"
NULL_FIELD_POLICY = {
    "schema_version": "bhm.sidecar-null-policy.v2",
    "mode": "lossless_incomplete_artifact",
    "session-records.json": {
        "done": {
            "missing": "preserve_as_incomplete_archived_artifact",
            "null": "preserve_as_incomplete_archived_artifact",
            "empty_string": "preserve_as_incomplete_archived_artifact",
            "default_invention": "forbidden",
        }
    },
    "apply_authorized": False,
}
GRAPH_BUILDER_CONTRACT = {
    "schema_version": "bhm.task-graph-builder-contract.v2",
    "status": "implemented_staged_only",
    "required": {
        "snapshot_id": "deterministic per canonical project, source digest and builder version",
        "graph_digest": "SHA-256 over canonical ordered node and edge material",
        "node_key": "stable task identity; task_id is the source candidate",
        "node_sha256": "SHA-256 over canonical node identity and payload",
        "edge_key": "stable source/target/relation identity",
        "relation": "explicit allowlisted relation; never inferred from absent dependencies",
        "edge_completeness": "unknown when no explicit edge source is present; staged snapshots must not publish task_graph_current",
    },
    "parity_receipt_required": True,
    "apply_authorized": False,
}
CANDIDATE_FILES = (
    "memory-links.json",
    "checkpoints.json",
    "session-records.json",
    "tasks.json",
)

MAPPING_SPECS: dict[str, dict[str, Any]] = {
    "memory-links.json": {
        "status": "candidate",
        "target_tables": ["memory_links"],
        "source_key": ["id"],
        "target_key": ["link_id"],
        "required_fields": ["id", "project", "source_id", "target_id", "relation"],
        "field_map": {
            "id": "link_id",
            "project": "project",
            "source_id": "source_id",
            "target_id": "target_id",
            "relation": "relation",
            "created_at": "created_at",
            "updated_at": "updated_at",
            "metadata": "metadata_json (canonical JSON)",
        },
        "conflict_policy": [
            "reject duplicate id/link_id",
            "reject duplicate (project, source_id, target_id, relation)",
            "resolve known project aliases only in preview; preserve unknown values exactly",
        ],
    },
    "checkpoints.json": {
        "status": "candidate",
        "target_tables": ["memory_artifacts"],
        "artifact_type": "checkpoint",
        "source_key": ["id"],
        "target_key": ["artifact_type", "artifact_id"],
        "required_fields": ["id", "project", "checkpoint_type", "title", "content"],
        "field_map": {
            "id": "artifact_id",
            "project": "project",
            "memory_id": "memory_id",
            "created_at": "created_at",
            "updated_at": "updated_at",
            "all remaining source fields": "payload_json (canonical JSON)",
        },
        "conflict_policy": [
            "reject duplicate (artifact_type, artifact_id)",
            "resolve known project aliases only in preview; reject unresolved case-fold collisions",
            "preserve source payload; do not silently coerce checkpoint_type or lifecycle",
        ],
    },
    "session-records.json": {
        "status": "candidate",
        "target_tables": ["memory_artifacts"],
        "artifact_type": "session_record",
        "source_key": ["id"],
        "target_key": ["artifact_type", "artifact_id"],
        "required_fields": ["id", "project", "title", "checks"],
        "field_map": {
            "id": "artifact_id",
            "project": "project",
            "memory_id": "memory_id",
            "created_at": "created_at",
            "updated_at": "updated_at",
            "all remaining source fields": "payload_json (canonical JSON)",
            "missing/empty done,next": "lossless legacy payload + incomplete archived artifact; no default value",
        },
        "conflict_policy": [
            "reject duplicate (artifact_type, artifact_id)",
            "resolve known project aliases only in preview; reject unresolved case-fold collisions",
            "require transcript/file references to remain bounded and redacted before apply",
        ],
    },
    "tasks.json": {
        "status": "candidate_staged_only",
        "target_tables": [
            "task_graph_snapshots",
            "task_graph_nodes",
            "task_graph_edges",
            "task_graph_current",
        ],
        "source_key": ["task_id"],
        "target_key": ["snapshot_id", "node_key"],
        "required_fields": ["id", "project", "task_id", "title", "status"],
        "field_map": {
            "task_id": "task_graph_nodes.entity_id",
            "project": "task_graph_nodes.project",
            "title/status/intent/scope": "task_graph_nodes.payload_json",
            "dependencies": "task_graph_edges only when explicitly present in source",
            "snapshot identity/digest": "canonical task graph builder; staged snapshot with edge_completeness=unknown",
        },
        "conflict_policy": [
            "reject duplicate task_id",
            "do not invent edge relations; absent dependencies mean edge_completeness=unknown",
            "use canonical task graph builder, stage the snapshot and do not update task_graph_current",
        ],
    },
}


def _load_authority_module():
    script = Path(__file__).with_name("validate-bhm-sidecar-authority.py")
    spec = importlib.util.spec_from_file_location("bhm_sidecar_authority", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load sidecar authority validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_project_registry(repo_root: Path):
    script = repo_root / "src" / "blackholememory" / "project_registry.py"
    if not script.exists():
        script = Path(__file__).resolve().parents[1] / "src" / "blackholememory" / "project_registry.py"
    spec = importlib.util.spec_from_file_location("bhm_project_registry", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load project registry")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.get_default_project_registry()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_records(path: Path) -> tuple[list[dict[str, Any]], str | None, str | None]:
    if not path.exists():
        return [], None, "missing"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [], None, type(exc).__name__
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        return [], None, "non_object_list"
    authority = _load_authority_module()
    digest = authority._inspect_sidecar(path).get("sha256")
    return value, str(digest) if digest else None, None


def _duplicates(records: list[dict[str, Any]], fields: tuple[str, ...]) -> list[str]:
    values = [tuple(item.get(field) for field in fields) for item in records]
    counts = Counter(values)
    return ["|".join("" if value is None else str(value) for value in key) for key, count in sorted(counts.items()) if count > 1]


def _project_collisions(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, set[str]] = defaultdict(set)
    for item in records:
        value = item.get("project")
        if isinstance(value, str) and value:
            grouped[value.casefold()].add(value)
    return [
        {"casefold": key, "variants": sorted(values)}
        for key, values in sorted(grouped.items())
        if len(values) > 1
    ]


def _project_resolution_preview(records: list[dict[str, Any]], registry: Any) -> dict[str, Any]:
    canonical_values: dict[str, set[str]] = defaultdict(set)
    known_count = 0
    unknown_count = 0
    unknown_values: set[str] = set()
    for item in records:
        raw = item.get("project")
        resolution = registry.resolve(raw)
        canonical = str(resolution.canonical)
        canonical_values[canonical].add(str(raw or ""))
        if resolution.known:
            known_count += 1
        else:
            unknown_count += 1
            if raw not in (None, ""):
                unknown_values.add(str(raw))
    return {
        "mode": "preview_only",
        "canonical_registry": "config/project-registry.json",
        "known_records": known_count,
        "unknown_records": unknown_count,
        "canonical_projects": {
            canonical: sorted(values) for canonical, values in sorted(canonical_values.items())
        },
        "unknown_project_values": sorted(unknown_values),
        "apply_authorized": False,
        "unknown_values_preserved_exactly": True,
    }


def _schema(connection: sqlite3.Connection) -> dict[str, list[str]]:
    tables: dict[str, list[str]] = {}
    names = [
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    ]
    for name in names:
        tables[name] = [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{name}")')]
    return tables


def _target_counts(connection: sqlite3.Connection, tables: dict[str, list[str]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for name in tables:
        if name in {"memory_links", "memory_artifacts", "task_graph_snapshots", "task_graph_nodes", "task_graph_edges", "task_graph_current"}:
            result[name] = int(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
    return result


def _mapping_report(
    name: str,
    records: list[dict[str, Any]],
    digest: str | None,
    parse_error: str | None,
    tables: dict[str, list[str]],
    target_counts: dict[str, int],
    registry: Any,
) -> dict[str, Any]:
    spec = MAPPING_SPECS[name]
    required = [
        field
        for field in spec["required_fields"]
        if any(field not in item or item.get(field) in (None, "") for item in records)
    ]
    source_key = tuple(spec["source_key"])
    duplicate_source_keys = _duplicates(records, source_key)
    project_collisions = _project_collisions(records)
    project_preview = _project_resolution_preview(records, registry)
    unresolved_collisions = [
        collision
        for collision in project_collisions
        if not all(
            registry.resolve(variant).known
            and registry.resolve(variant).canonical == registry.resolve(collision["variants"][0]).canonical
            for variant in collision["variants"]
        )
    ]
    missing_targets = [table for table in spec["target_tables"] if table not in tables]
    blockers: list[str] = []
    if parse_error:
        blockers.append(f"source_parse:{parse_error}")
    if not records:
        blockers.append("source_missing_or_empty")
    if required:
        blockers.append("required_fields_missing:" + ",".join(required))
    if duplicate_source_keys:
        blockers.append("duplicate_source_keys")
    if unresolved_collisions:
        blockers.append("unresolved_project_identity_collision")
    if missing_targets:
        blockers.append("target_tables_missing:" + ",".join(missing_targets))
    status = "blocked" if blockers else str(spec["status"])
    return {
        "source": name,
        "source_count": len(records),
        "source_sha256": digest,
        "status": status,
        "target_tables": spec["target_tables"],
        "source_key": spec["source_key"],
        "target_key": spec["target_key"],
        "required_fields": spec["required_fields"],
        "field_map": spec["field_map"],
        "conflict_policy": spec["conflict_policy"],
        "null_field_policy": NULL_FIELD_POLICY if name == "session-records.json" else None,
        "graph_builder_contract": GRAPH_BUILDER_CONTRACT if name == "tasks.json" else None,
        "duplicate_source_keys": duplicate_source_keys,
        "project_casefold_collisions": project_collisions,
        "resolved_project_alias_collisions": [
            collision for collision in project_collisions if collision not in unresolved_collisions
        ],
        "project_resolution_preview": project_preview,
        "missing_required_fields": required,
        "missing_target_tables": missing_targets,
        "target_counts": {table: target_counts.get(table, 0) for table in spec["target_tables"]},
        "blockers": blockers,
        "staging_ready": not blockers,
    }


def build_mapping_plan(repo_root: Path, runtime_dir: Path | None = None) -> dict[str, Any]:
    root = repo_root.resolve()
    live = (runtime_dir or root / ".runtime" / "live-memory").resolve()
    authority = _load_authority_module()
    authority_report = authority.build_report(root, live)
    registry = _load_project_registry(root)
    sidecar_reports: dict[str, tuple[list[dict[str, Any]], str | None, str | None]] = {
        name: _load_records(live / name) for name in CANDIDATE_FILES
    }
    sqlite_path = live / "memories.sqlite3"
    sqlite_error: str | None = None
    tables: dict[str, list[str]] = {}
    target_counts: dict[str, int] = {}
    if not sqlite_path.exists():
        sqlite_error = "missing"
    else:
        try:
            connection = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
            try:
                tables = _schema(connection)
                target_counts = _target_counts(connection, tables)
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            sqlite_error = type(exc).__name__

    mappings = [
        _mapping_report(name, *sidecar_reports[name], tables, target_counts, registry)
        for name in CANDIDATE_FILES
    ]
    blockers = [
        f"{mapping['source']}:{blocker}"
        for mapping in mappings
        for blocker in mapping["blockers"]
    ]
    if sqlite_error:
        blockers.append(f"sqlite:{sqlite_error}")
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": authority_report["ok"] and not sqlite_error,
        "read_only": True,
        "migration_authorized": False,
        "parity_proven": False,
        "staging_ready": not blockers,
        "authority_state": authority_report["authority_state"],
        "reconciliation_ready": authority_report["reconciliation_ready"],
        "migration_required": authority_report["migration_required"],
        "split_brain_risk": authority_report["split_brain_risk"],
        "project_registry": {
            "source": "config/project-registry.json",
            "default_project": registry.default_project,
            "mode": "preview_only",
            "apply_authorized": False,
        },
        "null_field_policy": NULL_FIELD_POLICY,
        "graph_builder_contract": GRAPH_BUILDER_CONTRACT,
        "sqlite": {
            "path": str(sqlite_path),
            "open_mode": "read-only",
            "schema_error": sqlite_error,
            "target_tables": tables,
            "target_counts": target_counts,
        },
        "mappings": mappings,
        "blockers": blockers,
        "next_action": (
            "Keep migration closed; define the explicit done-null policy, graph-builder contract "
            "and field-level parity, then run a disposable rollback rehearsal. Registry apply "
            "remains separately operator-gated before staging."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--runtime-dir", type=Path, default=None)
    parser.add_argument("--as-json", action="store_true")
    args = parser.parse_args()
    report = build_mapping_plan(args.repo_root, args.runtime_dir)
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"ok={report['ok']} read_only={report['read_only']} "
            f"staging_ready={report['staging_ready']} migration_authorized={report['migration_authorized']}"
        )
        print(f"mappings={len(report['mappings'])} blockers={len(report['blockers'])}")
        for mapping in report["mappings"]:
            print(f"{mapping['source']}: status={mapping['status']} records={mapping['source_count']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
