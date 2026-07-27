"""Deterministic Qdrant retention preview and non-mutating restore drill."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .projection_quarantine import json_sha256


RETENTION_SCHEMA_VERSION = "1.0"
_MANIFEST_NAME = "quarantine-manifest.json"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _resolve_backup_path(manifest_path: Path, raw_path: str) -> Path | None:
    candidate = Path(str(raw_path or ""))
    if candidate.is_file():
        return candidate
    sibling = manifest_path.parent / candidate.name
    return sibling if sibling.is_file() else None


def _manifest_paths(backup_root: Path | None) -> list[Path]:
    if backup_root is None or not backup_root.exists():
        return []
    try:
        return sorted(backup_root.rglob(_MANIFEST_NAME))
    except OSError:
        return []


def _validate_backup_manifest(path: Path) -> dict[str, Any]:
    errors: list[str] = []
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            "manifest": str(path),
            "collection": None,
            "status": "invalid",
            "restore_ready": False,
            "points": 0,
            "errors": [f"manifest_read:{type(exc).__name__}"],
        }
    if not isinstance(manifest, dict):
        return {
            "manifest": str(path),
            "collection": None,
            "status": "invalid",
            "restore_ready": False,
            "points": 0,
            "errors": ["manifest_not_object"],
        }
    collection = str(manifest.get("quarantineCollection") or "").strip() or None
    status = str(manifest.get("status") or "unknown").strip().lower()
    if status != "completed":
        errors.append(f"status:{status}")
    backup_path = _resolve_backup_path(path, str(manifest.get("backupPath") or ""))
    expected_backup_hash = str(manifest.get("backupSha256") or "").strip().lower()
    actual_backup_hash = None
    points: list[Any] = []
    if backup_path is None:
        errors.append("backup_missing")
    else:
        try:
            actual_backup_hash = hashlib.sha256(backup_path.read_bytes()).hexdigest()
        except OSError as exc:
            errors.append(f"backup_read:{type(exc).__name__}")
        if expected_backup_hash and actual_backup_hash != expected_backup_hash:
            errors.append("backup_sha256_mismatch")
        try:
            backup_document = json.loads(backup_path.read_text(encoding="utf-8"))
            points = backup_document.get("points") if isinstance(backup_document, dict) else []
            if not isinstance(points, list):
                errors.append("backup_points_not_list")
                points = []
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(f"backup_json:{type(exc).__name__}")
    point_ids: set[tuple[str, str]] = set()
    payload_hash_mismatches = 0
    vector_hash_mismatches = 0
    invalid_points = 0
    for point in points:
        if not isinstance(point, dict):
            invalid_points += 1
            continue
        original_collection = str(point.get("originalCollection") or "")
        original_point_id = str(point.get("originalPointId") or "")
        quarantine_point_id = str(point.get("quarantinePointId") or "")
        payload = point.get("payload")
        vector = point.get("vector")
        if not original_collection or not original_point_id or not quarantine_point_id or not isinstance(payload, dict):
            invalid_points += 1
            continue
        key = (original_collection, original_point_id)
        if key in point_ids:
            errors.append("duplicate_original_point")
        point_ids.add(key)
        if point.get("payloadSha256") and str(point["payloadSha256"]).lower() != json_sha256(payload):
            payload_hash_mismatches += 1
        if point.get("vectorSha256") and str(point["vectorSha256"]).lower() != json_sha256(vector):
            vector_hash_mismatches += 1
    expected_count = manifest.get("candidateCount")
    if expected_count is not None and int(expected_count) != len(points):
        errors.append("candidate_count_mismatch")
    if invalid_points:
        errors.append("invalid_points")
    if payload_hash_mismatches:
        errors.append("payload_hash_mismatch")
    if vector_hash_mismatches:
        errors.append("vector_hash_mismatch")
    return {
        "manifest": str(path),
        "collection": collection,
        "status": status,
        "restore_ready": not errors and collection is not None,
        "points": len(points),
        "original_collections": len({key[0] for key in point_ids}),
        "duplicate_original_points": max(0, len(points) - len(point_ids)),
        "invalid_points": invalid_points,
        "payload_hash_mismatches": payload_hash_mismatches,
        "vector_hash_mismatches": vector_hash_mismatches,
        "backup_sha256": expected_backup_hash or None,
        "backup_sha256_actual": actual_backup_hash,
        "errors": sorted(set(errors)),
    }


def build_qdrant_retention_preview(
    lifecycle_report: dict[str, Any],
    *,
    backup_root: Path | None = None,
) -> dict[str, Any]:
    """Build a stable preview; no Qdrant, SQLite or backup mutation occurs."""

    collections = []
    decision_counts: Counter[str] = Counter()
    for item in sorted(lifecycle_report.get("collections", []), key=lambda value: str(value.get("name") or "")):
        decision = str(item.get("decision") or "review")
        decision_counts[decision] += 1
        observed = item.get("observed") if isinstance(item.get("observed"), dict) else {}
        collections.append(
            {
                "name": item.get("name"),
                "classification": item.get("classification"),
                "point_count": item.get("point_count"),
                "decision": decision,
                "decision_reasons": list(item.get("decision_reasons") or []),
                "backup_status": item.get("backup_status"),
                "restore_status": item.get("restore_status"),
                "known_source_points": observed.get("known_source_points"),
                "unknown_source_points": observed.get("unknown_source_points"),
                "canonical_current_points": observed.get("canonical_current_points"),
                "repair_first_points": observed.get("repair_first_points"),
            }
        )
    eligible_for_apply = [item["name"] for item in collections if item["decision"] == "purge"]
    digest_payload = {
        "schema_version": RETENTION_SCHEMA_VERSION,
        "collections": collections,
        "reconciliation": lifecycle_report.get("reconciliation", {}),
        "apply_contract": {
            "eligible_for_apply": eligible_for_apply,
            "capability_required": True,
            "offline_writer_required": True,
            "explicit_confirmation_required": True,
            "backup_directory_required": True,
        },
    }
    return {
        "schema_version": RETENTION_SCHEMA_VERSION,
        "source": "qdrant-lifecycle-preview-read-only",
        "read_only": True,
        "mutations": {"qdrant": False, "filesystem": False, "sqlite": False},
        "backup_root": None if backup_root is None else str(backup_root),
        "decision_counts": dict(sorted(decision_counts.items())),
        "eligible_for_apply": eligible_for_apply,
        "blocked_apply_reasons": [
            "no_collection_has_purge_decision",
            "capability_required",
            "reviewed_preview_digest_required",
            "explicit_confirmation_required",
            "backup_directory_required",
            "offline_writer_required",
        ],
        "apply_contract": {
            "mutation_enabled": False,
            "capability_required": True,
            "capability_name": "BHM_ADMIN_CAPABILITY",
            "offline_writer_required": True,
            "explicit_confirmation_required": True,
            "backup_directory_required": True,
            "reviewed_preview_digest_required": True,
        },
        "collections": collections,
        "preview_digest": _digest(digest_payload),
    }


def run_qdrant_restore_drill(
    backup_root: Path | None,
    *,
    lifecycle_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate every quarantine backup as an in-memory restore rehearsal."""

    manifests = [_validate_backup_manifest(path) for path in _manifest_paths(backup_root)]
    ready = [item for item in manifests if item["restore_ready"]]
    drill_payload = [
        {
            "collection": item["collection"],
            "status": item["status"],
            "points": item["points"],
            "backup_sha256": item["backup_sha256"],
            "restore_ready": item["restore_ready"],
            "errors": item["errors"],
        }
        for item in sorted(manifests, key=lambda value: str(value.get("collection") or ""))
    ]
    active_rebuildable = 0
    if lifecycle_report is not None:
        active_rebuildable = sum(
            item.get("classification") == "active"
            and item.get("restore_status") == "rebuild_from_sqlite"
            for item in lifecycle_report.get("collections", [])
        )
    return {
        "schema_version": RETENTION_SCHEMA_VERSION,
        "source": "qdrant-backup-restore-drill-read-only",
        "read_only": True,
        "mutations": {"qdrant": False, "filesystem": False, "sqlite": False},
        "staging_mode": "in-memory-validation",
        "manifest_count": len(manifests),
        "restore_ready_count": len(ready),
        "restore_points": sum(int(item["points"]) for item in ready),
        "active_sqlite_rebuildable_count": int(active_rebuildable),
        "inspection_errors": [
            {"collection": item.get("collection"), "errors": item["errors"]}
            for item in manifests
            if item["errors"]
        ],
        "manifests": manifests,
        "drill_digest": _digest(drill_payload),
    }

