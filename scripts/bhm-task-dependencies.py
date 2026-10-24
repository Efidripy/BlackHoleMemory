"""Local operator CLI for the explicit SQLite task-dependency ledger.

This is intentionally not an API or MCP write route.  It requires both the
authoritative SQLite target and the bounded task source used to validate each
endpoint.  It never infers a relation from legacy sidecars.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from blackholememory.filesystem_boundaries import assert_safe_path
from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.memory_service import SQLiteMemoryService
from blackholememory.task_dependencies import ARTIFACT_TYPE
from blackholememory.task_dependencies import COMPLETENESS_ARTIFACT_TYPE
from blackholememory.task_dependencies import SCHEMA_VERSION
from blackholememory.task_dependencies import TaskDependencyDeclaration
from blackholememory.task_dependencies import TaskDependencyCompletenessAttestation
from blackholememory.task_dependencies import TaskDependencyError
from blackholememory.task_dependencies import append_task_dependency
from blackholememory.task_dependencies import append_task_dependency_completeness_attestation
from blackholememory.task_dependencies import load_task_dependencies
from blackholememory.task_dependencies import load_task_dependency_completeness_attestation


def _emit(value: object, report: str | None = None) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    print(rendered)
    if report:
        target = assert_safe_path(Path(report).expanduser())
        replace_bytes_safely(target, (rendered + "\n").encode("utf-8"))


def _load_tasks(path: str, *, project: str) -> tuple[list[dict[str, Any]], str]:
    target = assert_safe_path(Path(path).expanduser())
    source_bytes = target.read_bytes()
    payload = json.loads(source_bytes.decode("utf-8"))
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        raise TaskDependencyError("tasks source must be a JSON array")
    tasks = [dict(item) for item in payload if isinstance(item, dict)]
    if len(tasks) != len(payload):
        raise TaskDependencyError("tasks source contains a non-object record")
    if len(tasks) > 2_048:
        raise TaskDependencyError("tasks source bound exceeded")
    return [task for task in tasks if str(task.get("project") or project) == project], hashlib.sha256(source_bytes).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("plan", "list", "declare", "attest"), default="plan")
    parser.add_argument("--database")
    parser.add_argument("--tasks-json")
    parser.add_argument("--project", default="blackholememory")
    parser.add_argument("--task-id")
    parser.add_argument("--depends-on-task-id")
    parser.add_argument("--declared-by")
    parser.add_argument("--declared-at")
    parser.add_argument("--report")
    args = parser.parse_args()
    try:
        if args.action == "plan":
            _emit({
                "schema_version": SCHEMA_VERSION,
                "ok": True,
                "action": "plan",
                "artifact_type": ARTIFACT_TYPE,
                "writes_sqlite": False,
                "writes_qdrant": False,
                "writes_mem0": False,
                "publishes_task_graph_current": False,
            }, args.report)
            return 0
        if not args.database or not args.tasks_json:
            raise TaskDependencyError("--database and --tasks-json are required")
        database = assert_safe_path(Path(args.database).expanduser())
        tasks, source_sha256 = _load_tasks(args.tasks_json, project=args.project)
        service = SQLiteMemoryService(database)
        if args.action == "list":
            declarations = load_task_dependencies(service, project=args.project, tasks=tasks)
            attestation = load_task_dependency_completeness_attestation(
                service,
                project=args.project,
                tasks=tasks,
                source_sha256=source_sha256,
                declarations=declarations,
            )
            _emit({
                "schema_version": SCHEMA_VERSION,
                "ok": True,
                "action": "list",
                "project": args.project,
                "declaration_count": len(declarations),
                "declaration_digests": sorted(item.digest() for item in declarations.values()),
                "source_sha256": source_sha256,
                "edge_completeness": "owner-attested-complete" if attestation else "explicit-declarations-only",
                "completeness_attestation_digest": attestation.digest() if attestation else None,
                "writes_sqlite": False,
                "writes_qdrant": False,
                "writes_mem0": False,
                "publishes_task_graph_current": False,
            }, args.report)
            return 0
        if args.action == "attest":
            declarations = load_task_dependencies(service, project=args.project, tasks=tasks)
            attestation = TaskDependencyCompletenessAttestation.from_tasks(
                project=args.project,
                tasks=tasks,
                source_sha256=source_sha256,
                declarations=list(declarations.values()),
                declared_by=args.declared_by,
                declared_at=args.declared_at,
            )
            _record, inserted = append_task_dependency_completeness_attestation(
                service,
                attestation,
                tasks=tasks,
                source_sha256=source_sha256,
            )
            _emit({
                "schema_version": SCHEMA_VERSION,
                "ok": True,
                "action": "attest",
                "artifact_type": COMPLETENESS_ARTIFACT_TYPE,
                "project": args.project,
                "inserted": inserted,
                "attestation_digest": attestation.digest(),
                "source_sha256": source_sha256,
                "task_count": attestation.task_count,
                "declaration_count": len(declarations),
                "edge_completeness": "owner-attested-complete",
                "writes_sqlite": bool(inserted),
                "writes_qdrant": False,
                "writes_mem0": False,
                "publishes_task_graph_current": False,
            }, args.report)
            return 0
        declaration = TaskDependencyDeclaration(
            project=args.project,
            task_id=args.task_id,
            depends_on_task_id=args.depends_on_task_id,
            declared_by=args.declared_by,
            declared_at=args.declared_at,
        )
        _record, inserted = append_task_dependency(service, declaration, tasks=tasks)
        _emit({
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "action": "declare",
            "project": args.project,
            "inserted": inserted,
            "declaration_digest": declaration.digest(),
            "writes_sqlite": bool(inserted),
            "writes_qdrant": False,
            "writes_mem0": False,
            "publishes_task_graph_current": False,
        }, args.report)
        return 0
    except (OSError, ValueError, json.JSONDecodeError, TaskDependencyError) as exc:
        _emit({"schema_version": SCHEMA_VERSION, "ok": False, "error": type(exc).__name__, "detail": str(exc)[:1_000]}, args.report)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
