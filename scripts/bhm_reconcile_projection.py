#!/usr/bin/env python
"""Dry-run-first reconciliation of the canonical SQLite projection in Qdrant."""

from __future__ import annotations

# The script adds the repository's src directory before importing project modules.
# ruff: noqa: E402

import argparse
import json
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.mem0_adapter import get_project_mem0_memory
from blackholememory.mem0_adapter import get_qdrant_client
from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.projection_reconciliation import QdrantSurfaceAdapter
from blackholememory.projection_reconciliation import apply_projection_reconciliation
from blackholememory.projection_reconciliation import build_projection_reconciliation_plan
from blackholememory.qdrant_projector import QdrantProjector
from blackholememory.runtime_storage import inspect_memory_store_schema
from blackholememory.config import settings
from blackholememory.runtime_endpoints import endpoint_parts


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")


DEFAULT_DATABASE = REPO_ROOT / "runtime" / "live-memory" / "memories.sqlite3"
DEFAULT_BHM_HOST, DEFAULT_BHM_PORT = endpoint_parts("bhm_api")


class ProjectionReconciliationError(RuntimeError):
    """Raised when the operator gate cannot safely proceed."""


def _parse_timestamp(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_report(path: Path | None, report: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _serialize_plan(plan: Any, *, include_observed_payload: bool) -> dict[str, Any]:
    payload = plan.to_dict()
    if not include_observed_payload:
        for entry in payload["entries"]:
            entry.pop("observed_payload", None)
    return payload


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    plan = report.get("plan") or {}
    return {
        "success": report.get("success"),
        "mode": report.get("mode"),
        "database": report.get("database"),
        "project": report.get("project"),
        "asOf": report.get("asOf"),
        "readOnlyRehearsal": report.get("readOnlyRehearsal"),
        "writes_live_state": report.get("writes_live_state"),
        "writerBoundary": report.get("writerBoundary"),
        "metrics": report.get("metrics"),
        "counts": plan.get("counts"),
        "blockingIssues": len(plan.get("blocking_issues") or []),
        "planDigest": plan.get("plan_digest"),
        "apply": report.get("apply"),
    }


def _listener_open(host: str = DEFAULT_BHM_HOST, port: int = DEFAULT_BHM_PORT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _copy_sqlite_read_only(source: Path, destination: Path) -> None:
    """Create a consistent read-only rehearsal copy without mutating source."""

    connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(destination)
        try:
            connection.backup(target)
            target.commit()
        finally:
            target.close()
    finally:
        connection.close()


def _build_projector() -> QdrantProjector:
    from qdrant_client.http import models as qdrant_models

    client = get_qdrant_client()
    embedding_models: dict[str, Any] = {}

    def vectorizer(memory: Any) -> list[float]:
        model = embedding_models.get(memory.project)
        if model is None:
            model = get_project_mem0_memory(memory.project).embedding_model
            embedding_models[memory.project] = model
        return model.embed(memory.current_revision.content, "add")

    def ensure_collection(collection_name: str) -> None:
        if client.collection_exists(collection_name):
            return
        try:
            client.create_collection(
                collection_name=collection_name,
                vectors_config=qdrant_models.VectorParams(
                    size=settings.mem0_embedding_dims,
                    distance=qdrant_models.Distance.COSINE,
                ),
            )
        except Exception:
            if not client.collection_exists(collection_name):
                raise

    return QdrantProjector(
        client,
        vectorizer,
        expected_dimensions=settings.mem0_embedding_dims,
        ensure_collection=ensure_collection,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--project", default=None)
    parser.add_argument("--as-of", default="", help="fixed UTC ISO timestamp; required for apply")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-plan-digest", default="")
    parser.add_argument(
        "--include-observed-payload",
        action="store_true",
        help="include full observed Qdrant payloads in the report",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="print a compact summary while keeping the full report file",
    )
    parser.add_argument(
        "--allow-orphan-delete",
        action="store_true",
        help="explicitly delete REVIEW orphan points during apply",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        database = args.database.expanduser().resolve()
        schema_ok, schema_reason = inspect_memory_store_schema(database)
        if not schema_ok:
            raise ProjectionReconciliationError(
                f"SQLite target is not schema-valid: {database} ({schema_reason})"
            )
        if args.allow_orphan_delete and not args.apply:
            raise ProjectionReconciliationError("--allow-orphan-delete requires --apply")
        if args.apply and not args.as_of:
            raise ProjectionReconciliationError("--apply requires exact --as-of from reviewed dry-run")
        if args.apply and not args.confirm_plan_digest:
            raise ProjectionReconciliationError("--apply requires --confirm-plan-digest")

        targets_live = database == DEFAULT_DATABASE.resolve()
        listener_open = _listener_open()
        if args.apply and targets_live and listener_open:
            raise ProjectionReconciliationError(
                "live projection reconciliation apply requires the BHM API writer on "
                f"{DEFAULT_BHM_HOST}:{DEFAULT_BHM_PORT} to be stopped"
            )

        as_of = _parse_timestamp(args.as_of) if args.as_of else datetime.now(timezone.utc)
        if as_of is None:
            raise ProjectionReconciliationError(f"invalid --as-of timestamp: {args.as_of}")

        started = time.perf_counter()
        client = get_qdrant_client()
        surface = QdrantSurfaceAdapter(client)
        temp_root: Path | None = None
        repository_path = database
        try:
            if not args.apply:
                temp_root = Path(tempfile.mkdtemp(prefix="bhm-projection-reconcile-"))
                repository_path = temp_root / "memories.sqlite3"
                _copy_sqlite_read_only(database, repository_path)
            repository = SQLiteMemoryRepository(repository_path)
            plan = build_projection_reconciliation_plan(
                repository,
                surface,
                project=args.project,
                as_of=_utc_iso(as_of),
            )
            report: dict[str, Any] = {
                "success": True,
                "mode": "apply" if args.apply else "dry-run",
                "database": str(database),
                "project": args.project,
                "asOf": _utc_iso(as_of),
                "writerBoundary": {
                    "targetsLiveDatabase": targets_live,
                    "apiListenerOpen": listener_open,
                    "applyRequiresOfflineLiveWriter": True,
                },
                "readOnlyRehearsal": not args.apply,
                "writes_live_state": bool(args.apply),
                "metrics": {
                    "planElapsedMs": round((time.perf_counter() - started) * 1000, 3),
                },
                "plan": _serialize_plan(
                    plan,
                    include_observed_payload=args.include_observed_payload,
                ),
            }
            if args.apply:
                if plan.digest != args.confirm_plan_digest:
                    raise ProjectionReconciliationError(
                        "projection reconciliation plan digest changed; rerun dry-run"
                    )
                projector = _build_projector()
                result = apply_projection_reconciliation(
                    plan,
                    repository,
                    projector,
                    surface,
                    allow_orphan_delete=args.allow_orphan_delete,
                )
                report["apply"] = {
                    "plan_digest": result.plan_digest,
                    "upserted": result.upserted,
                    "deleted": result.deleted,
                    "reviewed": result.reviewed,
                    "failed": list(result.failed),
                    "ok": result.ok,
                }
                if not result.ok:
                    report["success"] = False
            _write_report(args.report, report)
            output = _summary(report) if args.summary_only else report
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0 if report["success"] else 1
        finally:
            if temp_root is not None:
                shutil.rmtree(temp_root, ignore_errors=True)
    except Exception as exc:
        error = {
            "success": False,
            "error": type(exc).__name__,
            "detail": str(exc),
        }
        try:
            _write_report(args.report, error)
        except Exception:
            pass
        print(json.dumps(error, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
