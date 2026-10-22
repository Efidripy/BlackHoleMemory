"""Domain module app_ops_entity: moved out of app.py (FIX-017). Cross-module calls go through module attributes."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from fastapi import HTTPException
from . import app_runtime as rt
from .app_runtime import _safe_exception_text, _write_json_atomic
from .api_models import ForgetPreviewRequest, ForgetApplyRequest, EntityCatalogRequest, EntitySearchRequest, PolicyProfileSetRequest
from .config import settings
from .mem0_adapter import StorageNotReady
from .memory_service import MemoryServiceNotReady
from .sync_service import InvalidTombstone
from .sync_service import UndoWindowExpired
from . import app_core_fallback
from . import app_core_hit
from . import app_core_hook
from . import app_core


def _entity_catalog_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "entity-catalogs.json"


def _serialize_session_record(record: dict) -> dict:
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "title": record.get("title"),
        "done": record.get("done") or "",
        "next": record.get("next") or "",
        "checks": record.get("checks") or "",
        "risks": record.get("risks") or "",
        "decisions": record.get("decisions") or "",
        "files_touched": record.get("files_touched") or [],
        "conversation_notes": record.get("conversation_notes") or "",
        "transcript_ref": record.get("transcript_ref") or "",
        "memory_id": record.get("memory_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
    }


def _load_entity_catalogs() -> list[dict]:
    path = _entity_catalog_store_path()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _save_entity_catalogs(items: list[dict]) -> Path:
    return _write_json_atomic(_entity_catalog_store_path(), items)


def _save_policy_profile(profile: dict) -> Path:
    return _write_json_atomic(app_core_hook._policy_profile_store_path(), profile)


def _list_session_records(project: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
    items = app_core_hook._load_session_records()
    if project:
        items = [item for item in items if item.get("project") == project]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    start = max(offset, 0)
    end = start + max(min(limit, 200), 1)
    return items[start:end], total


def _entity_catalog_rebuild(project: str | None = None, *, aggregate: bool = False) -> dict:
    if not project and not aggregate:
        raise HTTPException(
            status_code=403,
            detail={"code": "entity_catalog_scope_required", "message": "project or aggregate=true is required"},
        )
    canonical_project = rt._canonical_project(project) if project else None
    records = [
        item
        for item in rt._load_live_memories()
        if app_core_hit._memory_matches_filters(item, project=canonical_project, include_archived=False)
    ]
    files = Counter()
    endpoints = Counter()
    env_vars = Counter()
    concepts = Counter()
    for item in records:
        content = item.get("content") or ""
        for match in re.findall(r"\b[\w./-]+\.(?:py|ts|tsx|js|json|md|yml|yaml)\b", content):
            files[match] += 1
        for match in re.findall(r"\b/(?:[A-Za-z0-9_.-]+/?)+", content):
            endpoints[match] += 1
        for match in re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", content):
            env_vars[match] += 1
        for concept in item.get("tags") or []:
            concepts[concept] += 1
    catalog = {
        "id": f"entity_catalog_{canonical_project or 'all'}",
        "project": canonical_project,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "files": dict(files.most_common(50)),
        "endpoints": dict(endpoints.most_common(50)),
        "env_vars": dict(env_vars.most_common(50)),
        "concepts": dict(concepts.most_common(50)),
    }
    catalogs = [item for item in _load_entity_catalogs() if item.get("project") != canonical_project]
    catalogs.append(catalog)
    _save_entity_catalogs(catalogs)
    return catalog


def _entity_catalog_get(project: str | None = None) -> dict:
    project = rt._canonical_project(project) if project else None
    for item in _load_entity_catalogs():
        if item.get("project") == project:
            return item
    return _entity_catalog_rebuild(project)


def _forget_selector_values(request: ForgetPreviewRequest) -> tuple[str | None, list[str], list[str]]:
    project = rt._canonical_project(request.project) if request.project else None
    memory_ids = sorted({str(value).strip() for value in request.memory_ids if str(value).strip()})
    upsert_keys = sorted({str(value).strip() for value in request.upsert_keys if str(value).strip()})
    if not memory_ids and not upsert_keys:
        raise HTTPException(status_code=400, detail="forget preview requires memory_ids or upsert_keys")
    return project, memory_ids, upsert_keys


def _forget_preview_plan(request: ForgetPreviewRequest) -> dict:
    project, memory_ids, upsert_keys = _forget_selector_values(request)
    accepted_projects = rt._project_aliases(project)
    selected: list[dict] = []
    all_selected_ids: set[str] = set()
    for item in rt._load_live_memories():
        source_id = str(item.get("source_id") or "").strip()
        metadata = item.get("metadata") or {}
        if not source_id or (project and item.get("project") not in accepted_projects):
            continue
        id_match = not memory_ids or source_id in memory_ids
        key_match = not upsert_keys or str(metadata.get("upsert_key") or "") in upsert_keys
        if not (id_match and key_match):
            continue
        all_selected_ids.add(source_id)
        content = str(item.get("content") or "")
        content_sha256 = metadata.get("content_sha256") or hashlib.sha256(content.encode("utf-8")).hexdigest()
        selected.append(
            {
                "id": source_id,
                "project": item.get("project"),
                "title": metadata.get("raw_title") or app_core._build_memory_title(item.get("content") or ""),
                "lifecycle": app_core._memory_lifecycle(item),
                "memory_type": item.get("memory_type"),
                "upsert_key": metadata.get("upsert_key"),
                "revision_id": metadata.get("revision_id"),
                "content_sha256": content_sha256,
            }
        )
    selected.sort(key=lambda item: item["id"])
    missing_ids = sorted(set(memory_ids) - all_selected_ids)
    candidate_ids = sorted(all_selected_ids)
    digest_payload = {
        "project": project,
        "memory_ids": memory_ids,
        "upsert_keys": upsert_keys,
        "candidate_ids": candidate_ids,
        "candidate_fingerprints": [
            {
                "id": item["id"],
                "project": item.get("project"),
                "upsert_key": item.get("upsert_key"),
                "content_sha256": item.get("content_sha256"),
            }
            for item in selected
        ],
        "operation": request.operation,
        "reason": request.reason,
        "undo_window_seconds": request.undo_window_seconds,
    }
    plan_digest = hashlib.sha256(
        json.dumps(digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "project": project,
        "operation": request.operation,
        "reason": request.reason,
        "undo_window_seconds": request.undo_window_seconds,
        "memory_ids": memory_ids,
        "upsert_keys": upsert_keys,
        "candidate_count": len(selected),
        "candidates": selected[: request.limit],
        "candidate_ids": candidate_ids,
        "missing_ids": missing_ids,
        "truncated": len(selected) > request.limit,
        "plan_digest": plan_digest,
        "read_only": True,
    }


def _forget_preview(request: ForgetPreviewRequest) -> dict:
    return {"success": True, "mode": "preview", **_forget_preview_plan(request)}


def _tombstone_live_memory(memory_id: str, project: str | None, reason: str) -> dict:
    existing = rt._find_live_memory(memory_id, project)
    if existing is None:
        raise HTTPException(status_code=404, detail="memory not found")
    if app_core._memory_lifecycle(existing) == "tombstoned":
        return existing
    try:
        scoped_project = rt._canonical_project(project) if project else None
        deleted = rt._memory_service().tombstone(memory_id, project=scoped_project, reason=reason)
    except MemoryServiceNotReady as exc:
        raise StorageNotReady(str(exc)) from exc
    if deleted is None:
        raise HTTPException(status_code=409, detail="memory was already tombstoned")
    return deleted


def _restore_tombstoned_memory(memory_id: str, project: str | None, reason: str, undo_window_seconds: int) -> dict:
    existing = rt._find_live_memory(memory_id, project)
    if existing is None:
        raise HTTPException(status_code=404, detail="memory not found")
    if app_core._memory_lifecycle(existing) != "tombstoned":
        return existing
    try:
        scoped_project = rt._canonical_project(project) if project else None
        restored = rt._memory_service().restore_tombstone(
            memory_id,
            project=scoped_project,
            reason=reason,
            undo_window_seconds=undo_window_seconds,
        )
    except (InvalidTombstone, UndoWindowExpired) as exc:
        raise HTTPException(status_code=409, detail=_safe_exception_text(exc)) from exc
    except MemoryServiceNotReady as exc:
        raise StorageNotReady(str(exc)) from exc
    if restored is None:
        raise HTTPException(status_code=409, detail="memory is not tombstoned")
    return restored


def _forget_apply(request: ForgetApplyRequest) -> dict:
    if not request.confirm:
        raise HTTPException(status_code=400, detail="forget apply requires confirm=true")
    plan = _forget_preview_plan(request)
    project = plan["project"]
    if plan["missing_ids"] or plan["truncated"]:
        raise HTTPException(status_code=409, detail={"code": "forget_plan_changed", "plan": plan})
    if plan["plan_digest"] != request.preview_digest:
        raise HTTPException(status_code=409, detail={"code": "forget_preview_digest_mismatch", "plan": plan})
    results: list[dict] = []
    for candidate in plan["candidates"]:
        memory_id = candidate["id"]
        current = rt._find_live_memory(memory_id, project)
        if current is None:
            raise HTTPException(status_code=409, detail=f"memory disappeared before forget apply: {memory_id}")
        if request.operation == "tombstone":
            already = app_core._memory_lifecycle(current) == "tombstoned"
            record = _tombstone_live_memory(memory_id, project, request.reason)
            action = "already_tombstoned" if already else "tombstoned"
        else:
            already = app_core._memory_lifecycle(current) != "tombstoned"
            record = _restore_tombstoned_memory(
                memory_id,
                project,
                request.reason,
                request.undo_window_seconds,
            )
            action = "already_restored" if already else "restored"
        results.append({"id": memory_id, "action": action, "memory": app_core._serialize_memory_record(record)})
    return {
        "success": True,
        "mode": "apply",
        "operation": request.operation,
        "plan_digest": plan["plan_digest"],
        "count": len(results),
        "results": results,
    }


def _entity_search(request: EntitySearchRequest) -> dict:
    project = app_core_fallback._effective_search_project(request.project)
    catalog = _entity_catalog_get(project)
    query = request.query.lower()
    matches = []
    for kind in ("files", "endpoints", "env_vars", "concepts"):
        for value, count in (catalog.get(kind) or {}).items():
            if query in value.lower():
                matches.append({"kind": kind, "value": value, "count": count})
    matches.sort(key=lambda item: item["count"], reverse=True)
    return {"project": project, "query": request.query, "matches": matches[: max(min(request.limit, 200), 1)]}


def _policy_profile_set(request: PolicyProfileSetRequest) -> dict:
    profile = request.model_dump()
    profile["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _save_policy_profile(profile)
    return profile


def bhm_forget_preview(request: ForgetPreviewRequest) -> dict:
    return _forget_preview(request)


async def bhm_forget_apply(request: ForgetApplyRequest) -> dict:
    return await rt._run_bounded_write("bhm.forget.apply", _forget_apply, request)


def bhm_entity_catalog_get(request: EntityCatalogRequest) -> dict:
    return _entity_catalog_get(request.project)


def bhm_entity_catalog_rebuild(request: EntityCatalogRequest) -> dict:
    return _entity_catalog_rebuild(request.project, aggregate=request.aggregate)
