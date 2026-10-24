"""Domain module app_ops_admin: moved out of app.py (FIX-017). Cross-module calls go through module attributes."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from fastapi import HTTPException, Request

from . import app_core, app_core_hit, app_core_hook, app_ops_task2
from . import app_runtime as rt
from .api_models import (
    AdminExportRequest,
    AdminImportApplyRequest,
    AdminImportPreviewRequest,
    ArtifactBatchDeleteRequest,
    ArtifactBatchRelinkRequest,
    ArtifactBatchRestoreRequest,
    ArtifactDeleteRequest,
    ArtifactListRequest,
    ArtifactRestoreRequest,
    CheckpointCreateRequest,
    HardDeleteMemoryRequest,
    IntegrityRepairStrictRequest,
    MemoryUpsertRequest,
    OrphanArtifactRelinkRequest,
    RepairLiveIndexesRequest,
    RestoreMemoryRequest,
    StrictSchemaValidateRequest,
)
from .app_runtime import _write_json_atomic
from .config import settings
from .filesystem_boundaries import (
    FilesystemReadLimitError,
    assert_safe_path,
    read_bytes_safely,
)
from .ingress_admission import IngressKind, admit_ingress
from .memory_routes import _serialized_link_mutation
from .security_boundaries import SecurityBoundaryError, resolve_under_root

MAX_ADMIN_SNAPSHOT_BYTES = 16 * 1024 * 1024


def _batch_project_scope(
    items: Iterable[Mapping[str, Any]],
    requested_project: str | None = None,
    *,
    require_project: bool = True,
) -> str | None:
    """Resolve one project contour for a batch request and reject mixed scopes.

    Caller authorization intentionally permits multiple projects when the
    principal has access to all of them.  Batch writes still need one explicit
    contour so an otherwise-valid request cannot partially operate across
    tenants.  Item-level project values may be omitted when the batch-level
    project supplies the scope.
    """

    requested = str(requested_project or "").strip()
    canonical_requested = rt._canonical_project(requested) if requested else None
    nested: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise HTTPException(status_code=422, detail={"code": "batch_item_invalid"})
        raw_project = str(item.get("project") or "").strip()
        if raw_project:
            nested.add(rt._canonical_project(raw_project))

    if len(nested) > 1:
        raise HTTPException(
            status_code=400,
            detail={"code": "batch_project_mismatch", "projects": sorted(nested)},
        )
    nested_project = next(iter(nested), None)
    if canonical_requested and nested_project and canonical_requested != nested_project:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "batch_project_mismatch",
                "projects": sorted({canonical_requested, nested_project}),
            },
        )

    effective = canonical_requested or nested_project
    if require_project and not effective:
        raise HTTPException(status_code=400, detail={"code": "batch_project_required"})
    return effective


def _batch_mapping_items(
    items: list[dict],
    requested_project: str | None = None,
    *,
    required_fields: tuple[str, ...] = (),
    require_project: bool = True,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Validate raw JSON batch items before any mutation is attempted."""

    project = _batch_project_scope(items, requested_project, require_project=require_project)
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise HTTPException(status_code=422, detail={"code": "batch_item_invalid", "index": index})
        candidate = dict(item)
        for field in required_fields:
            value = candidate.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise HTTPException(
                    status_code=422,
                    detail={"code": "batch_item_invalid", "index": index, "field": field},
                )
        if project:
            candidate["project"] = project
        normalized.append(candidate)
    return project, normalized


def _save_checkpoints(items: list[dict]) -> Path:
    return _write_json_atomic(app_core_hook._checkpoint_store_path(), items)


def _build_checkpoint_content(request: CheckpointCreateRequest) -> str:
    if request.content and request.content.strip():
        return request.content.strip()
    lines = [
        f"{request.project} checkpoint:",
        f"done: {request.done}",
        f"next: {request.next}",
        f"checks: {request.checks}",
        f"risks/notes: {request.risks}",
    ]
    return "\n".join(lines)


def _checkpoint_upsert_key(request: CheckpointCreateRequest, project: str, title: str) -> str:
    if request.upsert_key and request.upsert_key.strip():
        return request.upsert_key.strip()
    checkpoint_type = re.sub(r"[^a-z0-9._-]+", "-", (request.checkpoint_type or "workflow").strip().lower()).strip("-")
    title_slug = re.sub(r"[^a-z0-9._-]+", "-", title.strip().lower()).strip("-")
    return f"checkpoint:{project}:{checkpoint_type or 'workflow'}:{title_slug or 'checkpoint'}"


def _create_checkpoint(request: CheckpointCreateRequest) -> tuple[str, dict]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    content = _build_checkpoint_content(request)
    title = (request.title or "").strip() or f"{request.project} checkpoint"
    concepts = list(dict.fromkeys((request.concepts or []) + ["checkpoint", "bhm"]))
    files = request.files or []
    project = rt._canonical_project(request.project)
    upsert_key = _checkpoint_upsert_key(request, project, title)

    action, memory_record = app_core._upsert_live_memory(
        MemoryUpsertRequest(
            upsert_key=upsert_key,
            project=project,
            type=request.checkpoint_type,
            memory_class=app_core_hit.MemoryClass.EPISODIC,
            event_role=app_core_hit.MemoryEventRole.TRACE,
            content=content,
            concepts=concepts,
            files=files,
            metadata={"artifact_kind": "checkpoint"},
        )
    )

    checkpoints = app_core_hook._load_checkpoints()
    for item in checkpoints:
        metadata = item.get("metadata") or {}
        if item.get("project") in rt._project_aliases(project) and metadata.get("upsert_key") == upsert_key:
            item.update(
                {
                    "checkpoint_type": request.checkpoint_type,
                    "title": title,
                    "content": content,
                    "done": request.done,
                    "next": request.next,
                    "checks": request.checks,
                    "risks": request.risks,
                    "concepts": concepts,
                    "files": files,
                    "memory_id": memory_record.get("source_id"),
                    "updated_at": now,
                }
            )
            item["project"] = project
            item.setdefault("metadata", {})["upsert_key"] = upsert_key
            item["metadata"]["artifact_kind"] = "checkpoint"
            _save_checkpoints(checkpoints)
            return action, item

    checkpoint = {
        "id": f"checkpoint_bhm_{uuid.uuid4().hex[:16]}",
        "project": project,
        "checkpoint_type": request.checkpoint_type,
        "title": title,
        "content": content,
        "done": request.done,
        "next": request.next,
        "checks": request.checks,
        "risks": request.risks,
        "concepts": concepts,
        "files": files,
        "memory_id": memory_record.get("source_id"),
        "created_at": now,
        "updated_at": now,
        "metadata": {
            "upsert_key": upsert_key,
            "artifact_kind": "checkpoint",
        },
    }
    checkpoints.append(checkpoint)
    _save_checkpoints(checkpoints)
    return action, checkpoint


def _restore_archived_memory(request: RestoreMemoryRequest) -> dict:
    record = rt._find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    metadata = record.setdefault("metadata", {})
    metadata.pop("archived_at", None)
    metadata.pop("archive_reason", None)
    metadata["restored_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    record["updated_at"] = metadata["restored_at"]
    app_core._append_memory_changelog(record, "restore", {})
    app_core._replace_live_memory(record)
    return record


def _list_handoffs(project: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
    items = app_ops_task2._load_handoffs()
    if project:
        items = [item for item in items if item.get("project") == project]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    start = max(offset, 0)
    end = start + max(min(limit, 200), 1)
    return items[start:end], total


def _artifact_integrity_audit(project: str | None = None) -> dict:
    project = rt._canonical_project(project) if project else None
    memory_ids = {item.get("source_id") for item in rt._load_live_memories() if app_core_hook._project_matches(item.get("project"), project)}
    artifacts = {
        "checkpoints": [item for item in app_core_hook._load_checkpoints() if app_core_hook._project_matches(item.get("project"), project)],
        "project_maps": [item for item in app_core_hook._load_project_maps() if app_core_hook._project_matches(item.get("project"), project)],
        "adrs": [item for item in app_ops_task2._load_adrs() if app_core_hook._project_matches(item.get("project"), project)],
        "handoffs": [item for item in app_ops_task2._load_handoffs() if app_core_hook._project_matches(item.get("project"), project)],
        "session_records": [item for item in app_core_hook._load_session_records() if app_core_hook._project_matches(item.get("project"), project)],
        "tasks": [item for item in rt._load_tasks() if app_core_hook._project_matches(item.get("project"), project)],
        "task_contexts": [item for item in app_core_hook._load_task_contexts() if app_core_hook._project_matches(item.get("project"), project)],
        "risk_registers": [item for item in app_core_hook._load_risk_registers() if app_core_hook._project_matches(item.get("project"), project)],
        "validation_snapshots": [item for item in app_core_hook._load_validation_snapshots() if app_core_hook._project_matches(item.get("project"), project)],
    }
    orphans = {}
    for name, items in artifacts.items():
        missing = [item.get("id") for item in items if item.get("memory_id") and item.get("memory_id") not in memory_ids]
        if missing:
            orphans[name] = missing
    return {"project": project, "orphans": orphans, "ok": len(orphans) == 0}


def _artifact_find(artifact_type: str, artifact_id: str, project: str | None = None) -> tuple[list[dict], callable, dict]:
    pairs = app_ops_task2._artifact_store_pairs()
    if artifact_type not in pairs:
        raise HTTPException(status_code=400, detail="unsupported artifact type")
    loader, saver = pairs[artifact_type]
    items = loader()
    canonical_project = rt._canonical_project(project) if project else None
    for item in items:
        if item.get("id") == artifact_id and (
            not canonical_project or rt._canonical_project(item.get("project")) == canonical_project
        ):
            return items, saver, item
    raise HTTPException(status_code=404, detail="artifact not found")


def _artifact_restore(request: ArtifactRestoreRequest) -> dict:
    items, saver, artifact = _artifact_find(request.artifact_type, request.artifact_id, request.project)
    memory_id = artifact.get("memory_id")
    artifact_project = rt._canonical_project(
        artifact.get("project") or request.project or "e-github-workspace"
    )
    action = "none"
    restored_memory = None
    if memory_id:
        record = rt._find_live_memory(memory_id, artifact_project)
        if record is not None and app_core._is_archived_memory(record):
            restored_memory = _restore_archived_memory(
                RestoreMemoryRequest(id=memory_id, project=artifact_project)
            )
            action = "restored_memory"
    else:
        content_parts = [artifact.get("title") or artifact.get("project") or request.artifact_type]
        for field in ("content", "summary", "decision", "current_task", "done", "overall_status"):
            value = artifact.get(field)
            if value:
                content_parts.append(str(value))
        upsert_action, memory = app_core._upsert_live_memory(
            MemoryUpsertRequest(
                upsert_key=f"{request.artifact_type}:{artifact.get('project')}:{artifact.get('id')}",
                project=artifact_project,
                type="workflow",
                content="\n".join(content_parts),
                concepts=[request.artifact_type, "artifact-restore", "bhm"],
                files=artifact.get("files") or artifact.get("files_touched") or [],
            )
        )
        artifact["memory_id"] = memory.get("source_id")
        artifact["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        saver(items)
        action = upsert_action
        restored_memory = memory
    return {
        "artifact_type": request.artifact_type,
        "artifact": artifact,
        "action": action,
        "memory": app_core._serialize_memory_record(restored_memory) if restored_memory else None,
    }


def _orphan_artifact_relink(request: OrphanArtifactRelinkRequest) -> dict:
    items, saver, artifact = _artifact_find(request.artifact_type, request.artifact_id, request.project)
    target = rt._find_live_memory(request.target_memory_id, request.project or artifact.get("project"))
    if target is None:
        raise HTTPException(status_code=404, detail="target memory not found")
    artifact["memory_id"] = request.target_memory_id
    artifact["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    saver(items)
    return {"artifact_type": request.artifact_type, "artifact": artifact, "memory": app_core._serialize_memory_record(target)}


def _agent_activity_rollup(project: str | None = None) -> dict:
    project = rt._canonical_project(project) if project else None
    artifacts = {
        "checkpoints": [item for item in app_core_hook._load_checkpoints() if app_core_hook._project_matches(item.get("project"), project)],
        "handoffs": [item for item in app_ops_task2._load_handoffs() if app_core_hook._project_matches(item.get("project"), project)],
        "tasks": [item for item in rt._load_tasks() if app_core_hook._project_matches(item.get("project"), project)],
        "session_records": [item for item in app_core_hook._load_session_records() if app_core_hook._project_matches(item.get("project"), project)],
    }
    with app_core_hook._OBSERVATION_STORE_LOCK:
        observation_rollup = app_core_hook._observation_store().activity_rollup(project=project)
    counts = {name: len(items) for name, items in artifacts.items()}
    counts["observations"] = int(observation_rollup.get("count") or 0)
    latest = {
        name: max((item.get("updated_at") or item.get("created_at") or "" for item in items), default="")
        for name, items in artifacts.items()
    }
    latest["observations"] = str(observation_rollup.get("latest") or "")
    return {
        "project": project,
        "counts": counts,
        "latest": latest,
    }


def _artifact_delete(request: ArtifactDeleteRequest) -> dict:
    items, saver, artifact = _artifact_find(request.artifact_type, request.artifact_id, request.project)
    remaining = [item for item in items if item.get("id") != request.artifact_id]
    saver(remaining)
    deleted_memory = None
    if request.delete_backing_memory and artifact.get("memory_id"):
        deleted_memory = app_ops_task2._delete_live_memory_hard(HardDeleteMemoryRequest(id=artifact["memory_id"], project=request.project or artifact.get("project")))
    return {
        "artifact_type": request.artifact_type,
        "deleted": True,
        "artifact_id": request.artifact_id,
        "backing_memory_deleted": bool(deleted_memory),
        "backing_memory": app_core._serialize_memory_record(deleted_memory) if deleted_memory else None,
    }


def _artifact_list_by_type(request: ArtifactListRequest) -> dict:
    loader, _ = app_ops_task2._artifact_store_pairs().get(request.artifact_type) or (None, None)
    if loader is None:
        raise HTTPException(status_code=400, detail="unsupported artifact type")
    items = loader()
    if request.project:
        items = [item for item in items if app_core_hook._project_matches(item.get("project"), request.project)]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    start = max(request.offset, 0)
    end = start + max(min(request.limit, 200), 1)
    return {"artifact_type": request.artifact_type, "items": items[start:end], "total": total, "limit": max(min(request.limit, 200), 1), "offset": start}


def _artifact_usage_stats(project: str | None = None) -> dict:
    project = rt._canonical_project(project) if project else None
    memory_ids = {item.get("source_id") for item in rt._load_live_memories() if app_core_hook._project_matches(item.get("project"), project)}
    counts = {}
    referenced_memory_ids = set()
    orphan_counts = {}
    for artifact_type, (loader, _) in app_ops_task2._artifact_store_pairs().items():
        items = [item for item in loader() if app_core_hook._project_matches(item.get("project"), project)]
        counts[artifact_type] = len(items)
        orphan_counts[artifact_type] = sum(1 for item in items if item.get("memory_id") and item.get("memory_id") not in memory_ids)
        referenced_memory_ids.update(item.get("memory_id") for item in items if item.get("memory_id"))
    return {
        "project": project,
        "artifact_counts": counts,
        "orphan_counts": orphan_counts,
        "backed_memory_count": len(referenced_memory_ids),
        "unreferenced_memory_count": sum(1 for item in rt._load_live_memories() if app_core_hook._project_matches(item.get("project"), project) and item.get("source_id") not in referenced_memory_ids),
    }


def _artifact_batch_delete(request: ArtifactBatchDeleteRequest) -> dict:
    project = _batch_project_scope((), request.project)
    for artifact_id in request.artifact_ids:
        _artifact_find(request.artifact_type, artifact_id, project)
    results = []
    for artifact_id in request.artifact_ids:
        results.append(
            _artifact_delete(
                ArtifactDeleteRequest(
                    artifact_type=request.artifact_type,
                    artifact_id=artifact_id,
                    project=project,
                    delete_backing_memory=request.delete_backing_memory,
                )
            )
        )
    return {"artifact_type": request.artifact_type, "count": len(results), "items": results}


def _artifact_batch_relink(request: ArtifactBatchRelinkRequest) -> dict:
    project, items = _batch_mapping_items(
        request.items,
        request.project,
        required_fields=("artifact_id", "target_memory_id"),
    )
    for item in items:
        _artifact_find(request.artifact_type, str(item["artifact_id"]), project)
        if rt._find_live_memory(str(item["target_memory_id"]), project) is None:
            raise HTTPException(status_code=404, detail="target memory not found")
    results = []
    for item in items:
        results.append(
            _orphan_artifact_relink(
                OrphanArtifactRelinkRequest(
                    artifact_type=request.artifact_type,
                    artifact_id=item["artifact_id"],
                    target_memory_id=item["target_memory_id"],
                    project=project,
                )
            )
        )
    return {"artifact_type": request.artifact_type, "count": len(results), "items": results}


def _artifact_batch_restore(request: ArtifactBatchRestoreRequest) -> dict:
    project = _batch_project_scope((), request.project)
    for artifact_id in request.artifact_ids:
        _artifact_find(request.artifact_type, artifact_id, project)
    results = []
    for artifact_id in request.artifact_ids:
        results.append(
            _artifact_restore(
                ArtifactRestoreRequest(
                    artifact_type=request.artifact_type,
                    artifact_id=artifact_id,
                    project=project,
                )
            )
        )
    return {"artifact_type": request.artifact_type, "count": len(results), "items": results}


def _schema_validate_strict(request: StrictSchemaValidateRequest) -> dict:
    project = rt._canonical_project(request.project) if request.project else None
    memory_items = []
    for item in rt._load_live_memories():
        if not app_core_hit._memory_matches_filters(item, project=project, include_archived=request.include_archived):
            continue
        serialized = app_core._serialize_memory_record(item)
        missing = []
        for field in ("id", "project", "type", "content", "created_at", "updated_at", "metadata"):
            if serialized.get(field) in (None, "", []):
                if field != "metadata":
                    missing.append(field)
        if "raw_title" not in (serialized.get("metadata") or {}):
            missing.append("metadata.raw_title")
        if "files" not in (serialized.get("metadata") or {}):
            missing.append("metadata.files")
        if "source_refs" not in (serialized.get("metadata") or {}):
            missing.append("metadata.source_refs")
        if missing:
            memory_items.append({"memory": serialized, "missing_fields": missing})
    artifact_items = _artifact_integrity_audit(project).get("orphans", {})
    return {"project": project, "memory_issues": memory_items, "artifact_orphans": artifact_items, "ok": not memory_items and not artifact_items}


def _normalize_memory_metadata(project: str | None = None) -> dict:
    project = rt._canonical_project(project) if project else None
    live_records = rt._load_live_memories()
    updated = 0
    for item in live_records:
        if not app_core_hook._project_matches(item.get("project"), project):
            continue
        metadata = item.setdefault("metadata", {})
        changed = False
        if "raw_title" not in metadata:
            metadata["raw_title"] = app_core._build_memory_title(item.get("content") or "")
            changed = True
        if "files" not in metadata:
            metadata["files"] = []
            changed = True
        if "source_refs" not in metadata:
            metadata["source_refs"] = []
            changed = True
        if "aliases" not in metadata:
            metadata["aliases"] = []
            changed = True
        if "changelog" not in metadata:
            metadata["changelog"] = []
            changed = True
        if changed:
            updated += 1
    app_core._save_live_memories(live_records)
    return {"project": project, "updated": updated}


def _integrity_repair_strict(request: IntegrityRepairStrictRequest) -> dict:
    if not request.project and not request.aggregate:
        raise HTTPException(
            status_code=403,
            detail={"code": "repair_scope_required", "message": "project or aggregate=true is required"},
        )
    project = rt._canonical_project(request.project) if request.project else None
    repairs = []
    if request.normalize_metadata:
        repairs.append({"normalize_metadata": _normalize_memory_metadata(project)})
    repairs.append(
        {
            "repair_live_indexes": app_ops_task2._repair_live_indexes(
                RepairLiveIndexesRequest(
                    project=project,
                    aggregate=request.aggregate,
                    remove_orphan_links=request.remove_orphan_links,
                    remove_orphan_artifacts=request.remove_orphan_artifacts,
                )
            )
        }
    )
    return {"project": project, "repairs": repairs, "strict_validation": _schema_validate_strict(StrictSchemaValidateRequest(project=project))}


def _admin_snapshot_path(value: str | Path, *, require_leaf: bool = False) -> Path:
    root = assert_safe_path(
        settings.runtime_dir / "admin-exports",
        reject_hardlink_target=False,
    )
    try:
        candidate = resolve_under_root(root, value, require_leaf=require_leaf)
    except SecurityBoundaryError as exc:
        raise HTTPException(status_code=400, detail="admin snapshot path must remain under admin-exports") from exc
    return assert_safe_path(candidate)


def _load_admin_snapshot_payload(path: Path) -> dict[str, Any]:
    """Read one bounded, object-shaped admin snapshot and fail closed."""

    try:
        raw = read_bytes_safely(path, max_bytes=MAX_ADMIN_SNAPSHOT_BYTES).decode("utf-8")
        payload = json.loads(raw)
    except FilesystemReadLimitError as exc:
        raise HTTPException(status_code=413, detail={"code": "admin_snapshot_too_large"}) from exc
    except HTTPException:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail={"code": "admin_snapshot_invalid"}) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail={"code": "admin_snapshot_invalid"})
    return payload


def _authorize_admin_snapshot_project(
    http_request: Request,
    requested_project: str | None,
    payload: Mapping[str, Any],
) -> None:
    """Fail closed when an admin snapshot crosses a caller project boundary."""

    payload_project = str(payload.get("project") or "").strip()
    scope_project = str(requested_project or payload_project).strip()
    principal = getattr(http_request.state, "bhm_caller_principal", None)
    if principal is None:
        raise HTTPException(status_code=401, detail={"code": "caller_auth_required"})

    if scope_project:
        app_core_hook._authorize_resource_project(http_request, scope_project)
        canonical_scope = rt._canonical_project(scope_project)
        if payload_project and rt._canonical_project(payload_project) != canonical_scope:
            raise HTTPException(status_code=403, detail={"code": "caller_project_forbidden"})
    elif not principal.all_projects:
        raise HTTPException(status_code=403, detail={"code": "caller_project_required"})
    else:
        canonical_scope = ""

    # A scoped snapshot must not hide a second project inside nested records.
    if canonical_scope:
        nested_items: list[Mapping[str, Any]] = []
        nested_items.extend(item for item in payload.get("memories") or [] if isinstance(item, Mapping))
        nested_items.extend(item for item in payload.get("links") or [] if isinstance(item, Mapping))
        for values in (payload.get("artifacts") or {}).values():
            if isinstance(values, list):
                nested_items.extend(item for item in values if isinstance(item, Mapping))
        for item in nested_items:
            item_project = str(item.get("project") or "").strip()
            if not item_project or rt._canonical_project(item_project) != canonical_scope:
                raise HTTPException(status_code=403, detail={"code": "caller_project_forbidden"})


def _canonicalize_admin_snapshot_payload(payload: dict[str, Any], requested_project: str | None) -> str | None:
    """Canonicalize an already-authorized snapshot before any import writes."""

    raw_scope = str(requested_project or payload.get("project") or "").strip()
    scope = rt._canonical_project(raw_scope) if raw_scope else None
    nested_items: list[dict[str, Any]] = []
    memories = payload.get("memories") or []
    links = payload.get("links") or []
    artifacts = payload.get("artifacts") or {}
    if not isinstance(memories, list) or not isinstance(links, list) or not isinstance(artifacts, Mapping):
        raise HTTPException(status_code=422, detail={"code": "admin_snapshot_invalid"})
    unknown_artifacts = sorted(set(str(key) for key in artifacts) - set(app_ops_task2._artifact_store_pairs()))
    if unknown_artifacts:
        raise HTTPException(
            status_code=422,
            detail={"code": "admin_snapshot_unknown_artifact_type", "artifact_types": unknown_artifacts},
        )
    if any(not isinstance(item, dict) for item in memories) or any(not isinstance(item, dict) for item in links):
        raise HTTPException(status_code=422, detail={"code": "admin_snapshot_invalid"})
    nested_items.extend(memories)
    nested_items.extend(links)
    for values in artifacts.values():
        if not isinstance(values, list):
            raise HTTPException(status_code=422, detail={"code": "admin_snapshot_invalid"})
        if any(not isinstance(item, dict) for item in values):
            raise HTTPException(status_code=422, detail={"code": "admin_snapshot_invalid"})
        nested_items.extend(values)

    discovered = {
        rt._canonical_project(item.get("project"))
        for item in nested_items
        if str(item.get("project") or "").strip()
    }
    if scope:
        if discovered and discovered != {scope}:
            raise HTTPException(status_code=403, detail={"code": "caller_project_forbidden"})
    elif any(not str(item.get("project") or "").strip() for item in nested_items):
        raise HTTPException(status_code=422, detail={"code": "admin_snapshot_project_required"})

    for item in nested_items:
        item["project"] = scope or rt._canonical_project(item.get("project"))
    payload["project"] = scope
    return scope


def _validate_admin_snapshot_ownership(payload: Mapping[str, Any]) -> None:
    """Reject snapshot links/artifacts that cross memory project ownership.

    Project labels on nested records are not sufficient: links and artifacts
    carry foreign keys which can otherwise bind an allowed-project snapshot to
    a memory owned by another project.  Validate the complete relationship
    graph before the import writes any authoritative sidecar data.
    """

    incoming_memories = payload.get("memories") or []
    incoming_by_id: dict[str, str] = {}
    for item in incoming_memories:
        if not isinstance(item, Mapping):
            continue
        source_id = str(item.get("source_id") or "").strip()
        project = str(item.get("project") or "").strip()
        if not source_id or not project:
            continue
        canonical = rt._canonical_project(project)
        previous = incoming_by_id.get(source_id)
        if previous is not None and previous != canonical:
            raise HTTPException(
                status_code=403,
                detail={"code": "caller_project_forbidden"},
            )
        incoming_by_id[source_id] = canonical

    existing_by_id: dict[str, str] = {}
    for item in rt._load_live_memories():
        source_id = str(item.get("source_id") or "").strip()
        project = str(item.get("project") or "").strip()
        if source_id and project:
            existing_by_id[source_id] = rt._canonical_project(project)

    # Do not let either merge mode silently bind an incoming memory id to a
    # different project already present in the authoritative store.
    for source_id, incoming_project in incoming_by_id.items():
        existing_project = existing_by_id.get(source_id)
        if existing_project is not None and existing_project != incoming_project:
            raise HTTPException(
                status_code=403,
                detail={"code": "caller_project_forbidden"},
            )

    memory_projects = {**existing_by_id, **incoming_by_id}
    for item in payload.get("links") or []:
        if not isinstance(item, Mapping):
            raise HTTPException(status_code=422, detail={"code": "admin_snapshot_invalid"})
        link_project = str(item.get("project") or "").strip()
        source_id = str(item.get("source_id") or "").strip()
        target_id = str(item.get("target_id") or "").strip()
        if not link_project or not source_id or not target_id:
            raise HTTPException(status_code=422, detail={"code": "admin_snapshot_link_invalid"})
        canonical_link_project = rt._canonical_project(link_project)
        for endpoint_id in (source_id, target_id):
            endpoint_project = memory_projects.get(endpoint_id)
            if endpoint_project != canonical_link_project:
                raise HTTPException(
                    status_code=403,
                    detail={"code": "caller_project_forbidden"},
                )

    for artifact_type, items in (payload.get("artifacts") or {}).items():
        existing_artifacts = {
            str(item.get("id")): rt._canonical_project(item.get("project"))
            for item in app_ops_task2._artifact_store_pairs().get(artifact_type, (lambda: [], lambda _items: None))[0]()
            if isinstance(item, Mapping) and str(item.get("id") or "").strip() and str(item.get("project") or "").strip()
        }
        for item in items:
            if not isinstance(item, Mapping):
                raise HTTPException(status_code=422, detail={"code": "admin_snapshot_invalid"})
            artifact_project = str(item.get("project") or "").strip()
            if not artifact_project:
                raise HTTPException(status_code=422, detail={"code": "admin_snapshot_project_required"})
            canonical_artifact_project = rt._canonical_project(artifact_project)
            artifact_id = str(item.get("id") or "").strip()
            if artifact_id and artifact_id in existing_artifacts and existing_artifacts[artifact_id] != canonical_artifact_project:
                raise HTTPException(
                    status_code=403,
                    detail={"code": "caller_project_forbidden"},
                )
            memory_id = str(item.get("memory_id") or "").strip()
            if memory_id and memory_projects.get(memory_id) != canonical_artifact_project:
                raise HTTPException(
                    status_code=403,
                    detail={"code": "caller_project_forbidden"},
                )


def _admin_export(request: AdminExportRequest) -> dict:
    export_dir = settings.runtime_dir / "admin-exports"
    assert_safe_path(export_dir, reject_hardlink_target=False)
    project = rt._canonical_project(request.project) if request.project else None
    export_name = request.export_name or f"bhm-admin-export-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    target = _admin_snapshot_path(export_name, require_leaf=True)
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "project": project,
        "include_archived": request.include_archived,
        "memories": [
            item for item in rt._load_live_memories()
            if app_core_hit._memory_matches_filters(item, project=project, include_archived=request.include_archived)
        ],
        "links": [
            item
            for item in rt._load_memory_links()
            if not project or rt._canonical_project(item.get("project")) == project
        ],
        "artifacts": {},
    }
    if request.include_artifacts:
        for artifact_type, (loader, _) in app_ops_task2._artifact_store_pairs().items():
            payload["artifacts"][artifact_type] = [
                item
                for item in loader()
                if not project or rt._canonical_project(item.get("project")) == project
            ]
    _write_json_atomic(target, payload)
    return {"path": str(target), "memory_count": len(payload["memories"]), "link_count": len(payload["links"]), "artifact_counts": {key: len(value) for key, value in payload["artifacts"].items()}}


def _admin_import_preview(request: AdminImportPreviewRequest, *, http_request: Request) -> dict:
    path = _admin_snapshot_path(request.path)
    # lgtm [py/path-injection]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="import path not found")
    # lgtm [py/path-injection]
    payload = _load_admin_snapshot_payload(path)
    _authorize_admin_snapshot_project(http_request, request.project, payload)
    scope = _canonicalize_admin_snapshot_payload(payload, request.project)
    _validate_admin_snapshot_ownership(payload)
    return {
        "path": str(path),
        "project": scope,
        "memory_count": len(payload.get("memories") or []),
        "link_count": len(payload.get("links") or []),
        "artifact_counts": {key: len(value) for key, value in (payload.get("artifacts") or {}).items()},
    }


@_serialized_link_mutation
def _admin_import_apply(request: AdminImportApplyRequest, *, http_request: Request) -> dict:
    if request.merge_mode not in {"upsert", "replace"}:
        raise HTTPException(status_code=422, detail={"code": "admin_snapshot_merge_mode_invalid"})
    path = _admin_snapshot_path(request.path)
    # lgtm [py/path-injection]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="import path not found")
    # lgtm [py/path-injection]
    payload = _load_admin_snapshot_payload(path)
    _authorize_admin_snapshot_project(http_request, request.project, payload)
    scope = _canonicalize_admin_snapshot_payload(payload, request.project)
    _validate_admin_snapshot_ownership(payload)
    imported = {"memories": 0, "links": 0, "artifacts": 0}
    live_records = rt._load_live_memories()
    by_id = {item.get("source_id"): item for item in live_records}
    for item in payload.get("memories") or []:
        existing = by_id.get(item.get("source_id"))
        if request.merge_mode == "replace" and existing is not None:
            existing_project = rt._canonical_project(existing.get("project"))
            incoming_project = rt._canonical_project(item.get("project"))
            if existing_project != incoming_project:
                raise HTTPException(status_code=403, detail={"code": "caller_project_forbidden"})
        if request.merge_mode == "replace" or item.get("source_id") not in by_id:
            by_id[item.get("source_id")] = item
            imported["memories"] += 1
    app_core._save_live_memories(list(by_id.values()))
    links = rt._load_memory_links()
    seen = {(item.get("source_id"), item.get("target_id"), item.get("relation"), item.get("project")) for item in links}
    for item in payload.get("links") or []:
        key = (item.get("source_id"), item.get("target_id"), item.get("relation"), item.get("project"))
        if request.merge_mode == "replace" or key not in seen:
            links.append(item)
            seen.add(key)
            imported["links"] += 1
    rt._save_memory_links(links)
    for artifact_type, items in (payload.get("artifacts") or {}).items():
        pair = app_ops_task2._artifact_store_pairs().get(artifact_type)
        if not pair:
            continue
        loader, saver = pair
        existing = loader()
        indexed = {item.get("id"): item for item in existing}
        for item in items:
            if request.merge_mode == "replace" or item.get("id") not in indexed:
                indexed[item.get("id")] = item
                imported["artifacts"] += 1
        saver(list(indexed.values()))
    return {"path": str(path), "project": scope, "merge_mode": request.merge_mode, "imported": imported}


async def bhm_checkpoint_create(request: CheckpointCreateRequest) -> dict:
    action, checkpoint = await rt._run_bounded_write("bhm.checkpoint", _create_checkpoint, request)
    return {"success": True, "action": action, "checkpoint": app_core_hook._serialize_checkpoint_record(checkpoint)}


def bhm_admin_import_preview(request: AdminImportPreviewRequest, http_request: Request) -> dict:
    result = _admin_import_preview(request, http_request=http_request)
    result["admission"] = admit_ingress(
        ingress=IngressKind.IMPORT,
        project=str(result["project"]),
        actor=app_core_hook._request_actor(http_request),
        payload={"path": request.path, "scope": result["project"]},
    ).receipt()
    return result


def bhm_admin_import_apply(request: AdminImportApplyRequest, http_request: Request) -> dict:
    # Imports remain preview-only until a typed reviewed-promotion path exists.
    app_core._admit_new_content(
        ingress=IngressKind.IMPORT,
        project=request.project or "",
        actor=app_core_hook._request_actor(http_request),
        payload=request.model_dump(mode="json"),
        requires_authority=True,
    )
    return _admin_import_apply(request, http_request=http_request)
