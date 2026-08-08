"""Read-only catalog of the live Qdrant projection surface.

The catalog is deliberately an observation boundary.  It only calls Qdrant
collection inspection methods and reads backup manifests; it never creates,
updates or deletes a collection or a point.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .mem0_adapter import global_collection_name
from .mem0_adapter import local_collection_name
from .project_registry import ProjectRegistry
from .project_registry import get_default_project_registry


CATALOG_SCHEMA_VERSION = "1.0"
_LOCAL_PREFIX = "bhm_local_memory_"
_QUARANTINE_PREFIX = "bhm_quarantine_"
_BACKUP_MANIFEST_NAME = "quarantine-manifest.json"


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _collection_names(client: Any) -> list[str]:
    response = client.get_collections()
    entries = _value(response, "collections", ()) or ()
    names = {_value(entry, "name") for entry in entries}
    return sorted(str(name) for name in names if str(name or "").strip())


def _point_count(client: Any, name: str) -> tuple[int | None, str | None]:
    try:
        response = client.get_collection(collection_name=name)
    except Exception:  # A catalog must expose an inspection failure without backend details.
        return None, "qdrant_inspection_failed"
    raw = _value(response, "points_count")
    if raw is None:
        raw = _value(response, "vectors_count")
    try:
        return (None if raw is None else int(raw)), None
    except (TypeError, ValueError):
        return None, "invalid_points_count"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_path(raw_path: str, manifest_path: Path, approved_root: Path | None = None) -> Path | None:
    raw = str(raw_path or "").strip()
    if not raw:
        return None
    backup_root = (approved_root or manifest_path.parent).resolve()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = manifest_path.parent / candidate
    try:
        resolved_root = backup_root.resolve()
        resolved = candidate.resolve()
    except OSError:
        return None
    # Older receipts stored the pre-cutover absolute runtime root.  Preserve
    # compatibility by resolving only the basename beside the trusted
    # manifest; never reuse the legacy absolute destination itself.
    if resolved_root not in resolved.parents:
        fallback = (manifest_path.parent / candidate.name).resolve()
        if resolved_root not in fallback.parents:
            return None
        resolved = fallback
    if resolved == resolved_root:
        return None
    if not resolved.is_file() or resolved.is_symlink():
        return None
    return resolved


def _load_quarantine_manifests(backup_root: Path | None) -> dict[str, dict[str, Any]]:
    if backup_root is None or not backup_root.exists():
        return {}
    result: dict[str, dict[str, Any]] = {}
    approved_root = backup_root.resolve()
    try:
        paths = sorted(backup_root.rglob(_BACKUP_MANIFEST_NAME))
    except OSError:
        return {}
    for manifest_path in paths:
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        collection = str(payload.get("quarantineCollection") or "").strip()
        if not collection:
            continue
        # A manifest may name an absolute path, but it must remain beneath the
        # operator-selected backup root.  Never fall back to ``candidate.name``
        # because that turns a traversal into an arbitrary readable-file sink.
        try:
            manifest_path = manifest_path.resolve()
            if manifest_path == approved_root or approved_root not in manifest_path.parents:
                continue
        except OSError:
            continue
        backup_path = _manifest_path(str(payload.get("backupPath") or ""), manifest_path, approved_root)
        expected_hash = str(payload.get("backupSha256") or "").strip().lower()
        actual_hash = None
        if backup_path is not None:
            try:
                actual_hash = _sha256(backup_path)
            except OSError:
                actual_hash = None
        status = str(payload.get("status") or "unknown").strip().lower()
        verified = status == "completed" and backup_path is not None
        if expected_hash:
            verified = verified and actual_hash == expected_hash
        result[collection] = {
            "manifest_path": str(manifest_path),
            "status": status,
            "backup_path": None if backup_path is None else str(backup_path),
            "backup_sha256": expected_hash or None,
            "backup_sha256_actual": actual_hash,
            "backup_verified": bool(verified),
            "candidate_count": payload.get("candidateCount"),
            "deleted_original_points": payload.get("deletedOriginalPoints"),
            "post_plan": payload.get("postPlan") if isinstance(payload.get("postPlan"), dict) else None,
        }
    return result


def _project_from_local_name(name: str) -> str:
    return name.removeprefix(_LOCAL_PREFIX).strip("_") or "unregistered"


def _classify_collection(
    name: str,
    point_count: int | None,
    *,
    registry: ProjectRegistry,
) -> dict[str, Any]:
    global_name = global_collection_name()
    canonical_projects = {
        local_collection_name(definition["id"]): definition["id"]
        for definition in registry.report()["projects"]
    }
    if name == global_name:
        return {
            "owner": "bhm",
            "project": "global",
            "role": "global-core",
            "classification": "active",
            "labels": ["active"],
            "rebuildability": "authoritative-sqlite",
            "decision": "retain",
        }
    if name in canonical_projects:
        project = canonical_projects[name]
        labels = ["active"]
        if point_count == 0:
            labels.append("empty")
        return {
            "owner": "bhm",
            "project": project,
            "role": "project-projection",
            "classification": "active",
            "labels": labels,
            "rebuildability": "authoritative-sqlite",
            "decision": "retain",
        }
    lowered = name.casefold()
    if lowered.startswith(_QUARANTINE_PREFIX):
        labels = ["quarantine"]
        if point_count == 0:
            labels.append("empty")
        return {
            "owner": "bhm",
            "project": "out-of-band",
            "role": "quarantine-projection",
            "classification": "quarantine",
            "labels": labels,
            "rebuildability": "retained-backup-only",
            "decision": "retain-review",
        }
    if lowered.startswith(_LOCAL_PREFIX):
        project = _project_from_local_name(name)
        if "demo" in lowered:
            classification, role, rebuildability, decision = "demo", "demo-fixture", "regenerable-fixture", "review"
            labels = ["demo"]
        elif "smoke" in lowered or "debug" in lowered or "trace_link" in lowered:
            classification, role, rebuildability, decision = "smoke", "smoke-fixture", "regenerable-fixture", "review"
            labels = ["smoke"]
        else:
            classification, role, rebuildability, decision = "review", "unregistered-project-projection", "manual-review", "review"
            labels = ["review"]
        if point_count == 0:
            labels.append("empty")
        return {
            "owner": "bhm" if classification != "review" else "unregistered",
            "project": project,
            "role": role,
            "classification": classification,
            "labels": labels,
            "rebuildability": rebuildability,
            "decision": decision,
        }
    labels = ["review"]
    if point_count == 0:
        labels.append("empty")
    return {
        "owner": "unregistered",
        "project": "unregistered",
        "role": "external-orphan",
        "classification": labels[0],
        "labels": labels,
        "rebuildability": "manual-review",
        "decision": "review",
    }


def _backup_state(
    name: str,
    manifest: dict[str, Any] | None,
    *,
    authoritative_projection: bool = False,
) -> dict[str, Any]:
    if manifest is not None:
        verified = bool(manifest.get("backup_verified"))
        if verified:
            status = "verified_completed"
            restore = "available_from_verified_backup"
        elif manifest.get("status") == "completed":
            status = "manifest_completed_unverified"
            restore = "manual_verification_required"
        else:
            status = f"manifest_{manifest.get('status') or 'unknown'}"
            restore = "manual_review_required"
        return {
            "status": status,
            "restore_status": restore,
            "manifest_path": manifest.get("manifest_path"),
            "backup_path": manifest.get("backup_path"),
            "backup_sha256": manifest.get("backup_sha256"),
            "backup_sha256_actual": manifest.get("backup_sha256_actual"),
            "candidate_count": manifest.get("candidate_count"),
            "deleted_original_points": manifest.get("deleted_original_points"),
            "post_plan": manifest.get("post_plan"),
        }
    if authoritative_projection:
        return {
            "status": "authoritative_sqlite_rebuild_path",
            "restore_status": "rebuild_from_sqlite",
            "manifest_path": None,
            "backup_path": None,
            "backup_sha256": None,
            "backup_sha256_actual": None,
            "candidate_count": None,
            "deleted_original_points": None,
            "post_plan": None,
        }
    return {
        "status": "not_found",
        "restore_status": "manual_review_required",
        "manifest_path": None,
        "backup_path": None,
        "backup_sha256": None,
        "backup_sha256_actual": None,
        "candidate_count": None,
        "deleted_original_points": None,
        "post_plan": None,
    }


def build_qdrant_catalog(
    client: Any,
    *,
    backup_root: Path | None = None,
    registry: ProjectRegistry | None = None,
    qdrant_url: str | None = None,
) -> dict[str, Any]:
    """Return a deterministic, read-only catalog for every live collection."""

    resolved_registry = registry or get_default_project_registry()
    names = _collection_names(client)
    manifests = _load_quarantine_manifests(backup_root)
    collections: list[dict[str, Any]] = []
    inspection_errors: list[dict[str, str]] = []
    for name in names:
        point_count, inspection_error = _point_count(client, name)
        if inspection_error:
            inspection_errors.append({"collection": name, "error": inspection_error})
        classification = _classify_collection(name, point_count, registry=resolved_registry)
        backup = _backup_state(
            name,
            manifests.get(name),
            authoritative_projection=classification["classification"] == "active",
        )
        collections.append(
            {
                "name": name,
                "owner": classification["owner"],
                "project": classification["project"],
                "role": classification["role"],
                "point_count": point_count,
                "classification": classification["classification"],
                "labels": classification["labels"],
                "rebuildability": classification["rebuildability"],
                "decision": classification["decision"],
                "backup_status": backup["status"],
                "restore_status": backup["restore_status"],
                "backup": backup,
            }
        )
    counts: dict[str, int] = {}
    label_counts: dict[str, int] = {}
    for item in collections:
        counts[item["classification"]] = counts.get(item["classification"], 0) + 1
        for label in item["labels"]:
            label_counts[label] = label_counts.get(label, 0) + 1
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "source": "qdrant-live-read-only",
        "qdrant_url": qdrant_url,
        "read_only": True,
        "mutations": {"qdrant": False, "filesystem": False, "sqlite": False},
        "inventory": {
            "collection_count": len(collections),
            "point_count_known": sum(item["point_count"] is not None for item in collections),
            "total_points": sum(item["point_count"] or 0 for item in collections),
            "classification_counts": dict(sorted(counts.items())),
            "label_counts": dict(sorted(label_counts.items())),
        },
        "inspection_errors": inspection_errors,
        "collections": collections,
    }
