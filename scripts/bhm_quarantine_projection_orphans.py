#!/usr/bin/env python
"""Backup and reversibly quarantine reviewed Qdrant projection duplicates."""

from __future__ import annotations

# The script adds the repository's src directory before importing project modules.
# ruff: noqa: E402

import argparse
import hashlib
import json
import re
import socket
import sys
from collections import Counter
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.capability import configured_admin_capability
from blackholememory.mem0_adapter import get_qdrant_client
from blackholememory.projection_quarantine import QUARANTINE_SCHEMA_VERSION
from blackholememory.projection_quarantine import collect_quarantine_points
from blackholememory.projection_quarantine import delete_original_points
from blackholememory.projection_quarantine import ensure_quarantine_collection
from blackholememory.projection_quarantine import upsert_quarantine_points
from blackholememory.projection_quarantine import verify_original_points_absent
from blackholememory.projection_quarantine import verify_quarantine_points
from blackholememory.projection_reconciliation import QdrantSurfaceAdapter
from blackholememory.projection_reconciliation import build_projection_reconciliation_plan
from blackholememory.projection_reconciliation import classify_projection_review_entries
from blackholememory.projection_reconciliation import projection_review_classification_digest
from blackholememory.projection_reconciliation import ProjectionReviewDisposition
from blackholememory.runtime_storage import inspect_memory_store_schema
from blackholememory.runtime_endpoints import endpoint_parts


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

DEFAULT_BHM_HOST, DEFAULT_BHM_PORT = endpoint_parts("bhm_api")


DEFAULT_DATABASE = REPO_ROOT / "runtime" / "live-memory" / "memories.sqlite3"
QUARANTINE_COLLECTION_PREFIX = "bhm_quarantine_projection_"
SAFE_COLLECTION_RE = re.compile(r"^[a-z0-9_]+$")


class ProjectionQuarantineCliError(RuntimeError):
    """Raised when the operator quarantine gate cannot proceed."""


def _listener_open(host: str = DEFAULT_BHM_HOST, port: int = DEFAULT_BHM_PORT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _as_of(value: str, *, required: bool) -> str:
    text = str(value or "").strip()
    if not text:
        if required:
            raise ProjectionQuarantineCliError("--apply requires exact --as-of from reviewed dry-run")
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectionQuarantineCliError(f"invalid --as-of timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _batch_id(collection_name: str) -> str:
    return collection_name.removeprefix(QUARANTINE_COLLECTION_PREFIX)


def _validate_collection_name(value: str) -> str:
    name = str(value or "").strip()
    if not name.startswith(QUARANTINE_COLLECTION_PREFIX):
        raise ProjectionQuarantineCliError(
            f"quarantine collection must start with {QUARANTINE_COLLECTION_PREFIX}"
        )
    if SAFE_COLLECTION_RE.fullmatch(name) is None:
        raise ProjectionQuarantineCliError("quarantine collection contains unsafe characters")
    return name


def _state(
    database: Path,
    *,
    project: str | None,
    as_of: str,
    client: Any,
) -> tuple[Any, Any, tuple[Any, ...], str, str]:
    repository = SQLiteMemoryRepository(database)
    memories = repository.list_memories(
        project=project,
        include_archived=True,
        include_tombstoned=True,
        limit=10_000,
    )
    plan = build_projection_reconciliation_plan(
        repository,
        QdrantSurfaceAdapter(client),
        project=project,
        as_of=as_of,
    )
    classified = classify_projection_review_entries(
        plan,
        known_memory_ids={memory.id for memory in memories},
    )
    classification_digest = projection_review_classification_digest(classified)
    return repository, plan, classified, classification_digest, str(len(memories))


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in report.items()
        if key not in {"classificationEntries", "backupPoints"}
    }


def _restore_from_manifest(manifest_path: Path, *, client: Any) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schemaVersion") != QUARANTINE_SCHEMA_VERSION:
        raise ProjectionQuarantineCliError("unsupported quarantine manifest schema")
    backup_path = Path(str(manifest.get("backupPath") or "")).resolve()
    expected_hash = str(manifest.get("backupSha256") or "")
    if not backup_path.exists() or _sha256_file(backup_path) != expected_hash:
        raise ProjectionQuarantineCliError("quarantine backup hash mismatch")
    backup = json.loads(backup_path.read_text(encoding="utf-8"))
    points = backup.get("points") or []
    if not isinstance(points, list):
        raise ProjectionQuarantineCliError("quarantine backup points are invalid")
    grouped: dict[str, list[Any]] = {}
    for item in points:
        collection = str(item.get("originalCollection") or "")
        point_id = str(item.get("originalPointId") or "")
        vector = item.get("vector")
        payload = item.get("payload")
        if not collection or not point_id or vector is None or not isinstance(payload, dict):
            raise ProjectionQuarantineCliError("quarantine backup contains an invalid point")
        grouped.setdefault(collection, []).append(
            qdrant_point(collection, point_id, vector, payload)
        )
    restored = 0
    for collection, items in grouped.items():
        for start in range(0, len(items), 64):
            client.upsert(collection_name=collection, points=items[start : start + 64], wait=True)
            restored += len(items[start : start + 64])
    return {
        "mode": "restore",
        "manifest": str(manifest_path),
        "restored": restored,
        "quarantineCollectionRetained": manifest.get("quarantineCollection"),
    }


def qdrant_point(collection: str, point_id: str, vector: Any, payload: dict[str, Any]) -> Any:
    from qdrant_client.http import models as qdrant_models

    return qdrant_models.PointStruct(id=point_id, vector=vector, payload=payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--project", default=None)
    parser.add_argument("--as-of", default="")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-plan-digest", default="")
    parser.add_argument("--confirm-classification-digest", default="")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--quarantine-collection", default="")
    parser.add_argument(
        "--disposition",
        choices=[item.value for item in ProjectionReviewDisposition if item is not ProjectionReviewDisposition.REPAIR_FIRST],
        default=ProjectionReviewDisposition.CANDIDATE_DUPLICATE.value,
    )
    parser.add_argument(
        "--allow-retain-review-quarantine",
        action="store_true",
        help="explicitly allow moving unknown retain-review points out of live retrieval collections",
    )
    parser.add_argument("--max-candidates", type=int, default=5000)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--restore-manifest", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.apply and args.restore_manifest:
            raise ProjectionQuarantineCliError("--apply cannot be combined with --restore-manifest")
        if (args.apply or args.restore_manifest) and not configured_admin_capability():
            raise ProjectionQuarantineCliError(
                "destructive Qdrant quarantine/restore requires BHM_ADMIN_CAPABILITY"
            )
        if args.restore_manifest and _listener_open():
            raise ProjectionQuarantineCliError("restore requires the BHM API writer to be stopped")
        if args.max_candidates < 0 or args.batch_size < 1:
            raise ProjectionQuarantineCliError("--max-candidates must be non-negative and --batch-size positive")
        if args.restore_manifest:
            report = _restore_from_manifest(args.restore_manifest.resolve(), client=get_qdrant_client())
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0

        database = args.database.expanduser().resolve()
        schema_ok, schema_reason = inspect_memory_store_schema(database)
        if not schema_ok:
            raise ProjectionQuarantineCliError(
                f"SQLite target is not schema-valid: {database} ({schema_reason})"
            )
        as_of = _as_of(args.as_of, required=args.apply)
        quarantine_collection = _validate_collection_name(args.quarantine_collection) if args.apply else None
        selected_disposition = ProjectionReviewDisposition(args.disposition)
        if args.apply:
            if args.confirm_plan_digest == "" or args.confirm_classification_digest == "":
                raise ProjectionQuarantineCliError(
                    "--apply requires --confirm-plan-digest and --confirm-classification-digest"
                )
            if args.backup_dir is None:
                raise ProjectionQuarantineCliError("--apply requires explicit --backup-dir")
            if _listener_open():
                raise ProjectionQuarantineCliError(
                    f"live quarantine apply requires the BHM API writer on {DEFAULT_BHM_HOST}:{DEFAULT_BHM_PORT} to be stopped"
                )
            if (
                selected_disposition is ProjectionReviewDisposition.RETAIN_REVIEW
                and not args.allow_retain_review_quarantine
            ):
                raise ProjectionQuarantineCliError(
                    "retain-review quarantine requires --allow-retain-review-quarantine"
                )

        client = get_qdrant_client()
        _repository, plan, classified, classification_digest, sqlite_count = _state(
            database,
            project=args.project,
            as_of=as_of,
            client=client,
        )
        classification_entries = [item.to_dict() for item in classified]
        disposition_counts = Counter(item["disposition"] for item in classification_entries)
        selected_count = int(disposition_counts.get(selected_disposition.value, 0))
        report: dict[str, Any] = {
            "success": True,
            "mode": "apply" if args.apply else "dry-run",
            "database": str(database),
            "project": args.project,
            "asOf": as_of,
            "readOnlyRehearsal": not args.apply,
            "writes_live_state": bool(args.apply),
            "writerBoundary": {
                "apiListenerOpen": _listener_open(),
                "applyRequiresOfflineWriter": True,
                "adminCapabilityConfigured": bool(configured_admin_capability()),
            },
            "sqliteMemoryCount": int(sqlite_count),
            "reconciliation": {
                "counts": plan.counts,
                "blockingIssues": len(plan.blocking_issues),
                "planDigest": plan.digest,
            },
            "classification": {
                "counts": dict(sorted(disposition_counts.items())),
                "classificationDigest": classification_digest,
            },
            "selectedDisposition": selected_disposition.value,
            "selectedCount": selected_count,
            "classificationEntries": classification_entries,
        }

        if args.apply:
            if plan.digest != args.confirm_plan_digest:
                raise ProjectionQuarantineCliError("projection plan digest changed; rerun dry-run")
            if classification_digest != args.confirm_classification_digest:
                raise ProjectionQuarantineCliError("classification digest changed; rerun dry-run")
            if selected_count > args.max_candidates:
                raise ProjectionQuarantineCliError(
                    f"selected count {selected_count} exceeds --max-candidates {args.max_candidates}"
                )
            if int(plan.counts.get("upsert") or 0) or int(plan.counts.get("delete") or 0):
                raise ProjectionQuarantineCliError(
                    "canonical projection has pending upsert/delete work; repair it before quarantine"
                )
            backup_dir = args.backup_dir.expanduser().resolve()
            if backup_dir.exists() and any(backup_dir.iterdir()):
                raise ProjectionQuarantineCliError(f"backup directory is not empty: {backup_dir}")
            backup_dir.mkdir(parents=True, exist_ok=True)
            batch_id = _batch_id(quarantine_collection)
            points = collect_quarantine_points(
                client,
                classified,
                batch_id=batch_id,
                disposition=selected_disposition,
            )
            backup_document = {
                "schemaVersion": QUARANTINE_SCHEMA_VERSION,
                "operation": "projection-orphan-quarantine",
                "batchId": batch_id,
                "asOf": as_of,
                "planDigest": plan.digest,
                "classificationDigest": classification_digest,
                "selectedDisposition": selected_disposition.value,
                "quarantineCollection": quarantine_collection,
                "points": [point.backup_dict() for point in points],
            }
            backup_path = backup_dir / "qdrant-orphan-points.json"
            _write_json(backup_path, backup_document)
            backup_sha256 = _sha256_file(backup_path)
            manifest = {
                "schemaVersion": QUARANTINE_SCHEMA_VERSION,
                "operation": "projection-orphan-quarantine",
                "batchId": batch_id,
                "asOf": as_of,
                "planDigest": plan.digest,
                "classificationDigest": classification_digest,
                "quarantineCollection": quarantine_collection,
                "backupPath": str(backup_path),
                "backupSha256": backup_sha256,
                "candidateCount": len(points),
                "surfaceCounts": dict(
                    sorted(Counter(item.surface for item in classified).items())
                ),
                "status": "backup-created",
            }
            manifest_path = backup_dir / "quarantine-manifest.json"
            _write_json(manifest_path, manifest)
            ensure_quarantine_collection(
                client,
                quarantine_collection,
                dimensions=768,
            )
            upsert_quarantine_points(
                client,
                quarantine_collection,
                points,
                batch_id=batch_id,
                batch_size=args.batch_size,
            )
            verify_quarantine_points(client, quarantine_collection, points)
            manifest["status"] = "quarantine-copy-verified"
            _write_json(manifest_path, manifest)
            deleted = delete_original_points(client, points, batch_size=args.batch_size * 2)
            verify_original_points_absent(client, points, batch_size=args.batch_size * 2)
            manifest["status"] = "originals-removed"
            manifest["deletedOriginalPoints"] = deleted
            _write_json(manifest_path, manifest)

            _repository, post_plan, post_classified, post_classification_digest, _ = _state(
                database,
                project=args.project,
                as_of=as_of,
                client=client,
            )
            post_counts = Counter(item.disposition.value for item in post_classified)
            report["backup"] = {
                "manifest": str(manifest_path),
                "backupPath": str(backup_path),
                "backupSha256": backup_sha256,
                "candidateCount": len(points),
                "deletedOriginalPoints": deleted,
            }
            report["postPlan"] = {
                "counts": post_plan.counts,
                "blockingIssues": len(post_plan.blocking_issues),
                "planDigest": post_plan.digest,
                "classificationCounts": dict(sorted(post_counts.items())),
                "classificationDigest": post_classification_digest,
            }
            report["classificationEntries"] = []
            manifest["postPlan"] = report["postPlan"]
            manifest["status"] = "completed"
            _write_json(manifest_path, manifest)

        _write_json(args.report.resolve(), report) if args.report else None
        print(json.dumps(_summary(report) if args.summary_only else report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        error = {
            "success": False,
            "error": type(exc).__name__,
            "detail": str(exc),
        }
        print(json.dumps(error, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
