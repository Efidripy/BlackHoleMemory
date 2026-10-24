"""Domain module app_projects: moved out of app.py (FIX-017). Cross-module calls go through module attributes."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from fastapi import HTTPException
from fastapi import Request
from . import app_runtime as rt
from .app_runtime import _safe_exception_text, _write_json_atomic
from .api_models import MemoryUpsertRequest, ProjectMapUpsertRequest, MemoryPinRequest, RebuildProjectSummaryRequest, ProjectSummaryListRequest, ProjectSummaryRefreshAllRequest, ProjectSummaryCompareRequest, ProjectOnlyRequest, ProjectRetirementRequest, ProjectSummaryPinRequest, ProjectMapCompareRequest, ProjectSimilarityReportRequest, SlotRequest, SlotAppendRequest, SlotReplaceRequest, SlotLabelRequest
from .config import settings
from .capability import ADMIN_CAPABILITY_HEADER
from .mem0_adapter import mem0_runtime_plan
from .project_registry import ProjectResolution
from .project_retirement import ProjectRetirementError
from .project_retirement import apply_project_retirement
from .project_retirement import preview_project_retirement
from .runtime_storage import resolve_runtime_storage_config
from . import app_core_fallback
from . import app_core_hit
from . import app_core_hook
from . import app_core


def _slot_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "slots.json"


def _serialize_project_map_record(record: dict) -> dict:
    sections = dict(record.get("sections") or {})
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "title": record.get("title"),
        "auth": sections.get("auth") or "",
        "routing": sections.get("routing") or "",
        "tests": sections.get("tests") or "",
        "deploy": sections.get("deploy") or "",
        "i18n": sections.get("i18n") or "",
        "websocket": sections.get("websocket") or "",
        "risks": sections.get("risks") or "",
        "notes": sections.get("notes") or "",
        "files": record.get("files") or [],
        "concepts": record.get("concepts") or [],
        "memory_id": record.get("memory_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
    }


def _load_mcp_registry_snapshot() -> dict[str, Any]:
    path = app_core_fallback._mcp_registry_snapshot_path()
    snapshot = app_core_fallback._read_json_snapshot(path)
    instances = snapshot.get("instances") if isinstance(snapshot, dict) else []
    if not isinstance(instances, list):
        instances = []
    return {
        "path": str(path),
        "loaded": isinstance(snapshot, dict),
        "instance_count": len(instances),
        "snapshot": snapshot,
    }


def _load_slots() -> list[dict]:
    path = _slot_store_path()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _save_slots(items: list[dict]) -> Path:
    return _write_json_atomic(_slot_store_path(), items)


def _build_project_map_sections(request: ProjectMapUpsertRequest) -> dict[str, str]:
    return {
        "auth": request.auth,
        "routing": request.routing,
        "tests": request.tests,
        "deploy": request.deploy,
        "i18n": request.i18n,
        "websocket": request.websocket,
        "risks": request.risks,
        "notes": request.notes,
    }


def _build_project_map_content(title: str, sections: dict[str, str]) -> str:
    lines = [title]
    for key in ("auth", "routing", "tests", "deploy", "i18n", "websocket", "risks", "notes"):
        lines.append(f"{key}: {sections.get(key) or ''}")
    return "\n".join(lines)


def _get_project_map(project: str) -> dict:
    accepted_projects = rt._project_aliases(project)
    for item in app_core_hook._load_project_maps():
        if item.get("project") in accepted_projects:
            return item
    raise HTTPException(status_code=404, detail="project map not found")


def _upsert_project_map(request: ProjectMapUpsertRequest) -> tuple[str, dict]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    title = (request.title or "").strip() or f"{request.project} project map"
    sections = _build_project_map_sections(request)
    concepts = list(dict.fromkeys((request.concepts or []) + ["project-map", "bhm"]))
    files = request.files or []
    upsert_key = request.upsert_key or f"project-map:{request.project}"
    content = _build_project_map_content(title, sections)

    action, memory_record = app_core._upsert_live_memory(
        MemoryUpsertRequest(
            upsert_key=upsert_key,
            project=request.project,
            type="architecture",
            content=content,
            concepts=concepts,
            files=files,
        )
    )

    maps = app_core_hook._load_project_maps()
    for item in maps:
        metadata = item.get("metadata") or {}
        if item.get("project") == request.project or metadata.get("upsert_key") == upsert_key:
            item.update(
                {
                    "title": title,
                    "sections": sections,
                    "files": files,
                    "concepts": concepts,
                    "memory_id": memory_record.get("source_id"),
                    "updated_at": now,
                }
            )
            item.setdefault("metadata", {})["upsert_key"] = upsert_key
            app_core_hook._save_project_maps(maps)
            return action, item

    project_map = {
        "id": f"project_map_bhm_{uuid.uuid4().hex[:16]}",
        "project": request.project,
        "title": title,
        "sections": sections,
        "files": files,
        "concepts": concepts,
        "memory_id": memory_record.get("source_id"),
        "created_at": now,
        "updated_at": now,
        "metadata": {
            "upsert_key": upsert_key,
        },
    }
    maps.append(project_map)
    app_core_hook._save_project_maps(maps)
    return action, project_map


def _set_memory_pin(request: MemoryPinRequest) -> dict:
    record = rt._find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    record.setdefault("metadata", {})["pinned"] = request.pinned
    record["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    app_core._append_memory_changelog(record, "set_pin", {"pinned": request.pinned})
    app_core._replace_live_memory(record)
    return record


def _rebuild_project_summary(request: RebuildProjectSummaryRequest) -> dict:
    canonical_project = rt._canonical_project(request.project)
    canonical_upsert_key = f"project-summary:{canonical_project}"
    project_map = None
    try:
        project_map = _get_project_map(canonical_project)
    except HTTPException:
        pass
    latest_checkpoint = None
    try:
        latest_checkpoint = app_core_hook._get_latest_checkpoint(canonical_project)
    except HTTPException:
        pass
    task_context = None
    try:
        task_context = app_core_hook._get_task_context(canonical_project)
    except HTTPException:
        pass
    risk_register = None
    try:
        risk_register = app_core_hook._get_risk_register(canonical_project)
    except HTTPException:
        pass
    validation_snapshot = None
    try:
        validation_snapshot = app_core_hook._get_validation_snapshot(canonical_project)
    except HTTPException:
        pass
    lines = [f"{canonical_project} project summary:"]
    if project_map:
        lines.append(f"project_map: {project_map.get('title')}")
    if latest_checkpoint:
        lines.append(f"checkpoint: {latest_checkpoint.get('title')}")
    if task_context:
        lines.append(f"task_context: {task_context.get('current_task')}")
    if risk_register:
        lines.append(f"risk_summary: {risk_register.get('summary')}")
    if validation_snapshot:
        lines.append(f"validation: {validation_snapshot.get('overall_status')}")
    action, record = app_core._upsert_live_memory(
        MemoryUpsertRequest(
            upsert_key=canonical_upsert_key,
            project=canonical_project,
            type="architecture",
            content="\n".join(lines),
            concepts=["project-summary", "bhm"],
            files=[],
        )
    )
    return {
        "action": action,
        "memory": app_core._serialize_memory_record(record),
        "canonical_upsert_key": canonical_upsert_key,
        "requested_upsert_key": request.upsert_key,
    }


def _project_summary_pin(project: str) -> dict:
    canonical_project = rt._canonical_project(project)
    record = app_core._find_live_memory_by_upsert_key(canonical_project, f"project-summary:{canonical_project}")
    if record is None:
        raise HTTPException(status_code=404, detail="project summary not found")
    pinned = _set_memory_pin(MemoryPinRequest(id=record["source_id"], project=project, pinned=True))
    return pinned


def _project_summary_list(request: ProjectSummaryListRequest) -> tuple[list[dict], int]:
    items = [
        item for item in rt._load_live_memories()
        if str((item.get("metadata") or {}).get("upsert_key") or "").startswith("project-summary:")
        and (not request.project or rt._canonical_project(item.get("project")) == rt._canonical_project(request.project))
    ]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    start = max(request.offset, 0)
    end = start + max(min(request.limit, 200), 1)
    return items[start:end], total


def _record_age_days(record: dict) -> float:
    timestamp = record.get("updated_at") or record.get("created_at")
    if not timestamp:
        return 0.0
    return max((datetime.now(timezone.utc) - app_core_hook._safe_datetime(timestamp)).total_seconds() / 86400.0, 0.0)


def _project_summary_refresh_all(request: ProjectSummaryRefreshAllRequest) -> dict:
    requested_projects = request.projects or ([request.project] if request.project else None)
    if not requested_projects and not request.aggregate:
        raise HTTPException(
            status_code=403,
            detail={"code": "refresh_scope_required", "message": "project(s) or aggregate=true is required"},
        )
    if requested_projects:
        projects = list(dict.fromkeys(rt._canonical_project(project) for project in requested_projects if str(project).strip()))
    else:
        projects = sorted(
            {
                rt._canonical_project(item.get("project"))
                for item in rt._load_live_memories()
                if item.get("project")
            }
        )
    results = []
    for project in projects:
        results.append(_rebuild_project_summary(RebuildProjectSummaryRequest(project=project)))
    return {"projects": projects, "count": len(results), "items": results, "aggregate": bool(request.aggregate and not requested_projects)}


def _project_memory_heatmap(project: str | None = None) -> dict:
    project = rt._canonical_project(project) if project else None
    records = [item for item in rt._load_live_memories() if app_core_hit._memory_matches_filters(item, project=project, include_archived=False)]
    type_counts = Counter(item.get("memory_type") or "unknown" for item in records)
    tag_counts = Counter(tag for item in records for tag in (item.get("tags") or []))
    age_buckets = {"0_7": 0, "8_30": 0, "31_90": 0, "91_plus": 0}
    for item in records:
        age = _record_age_days(item)
        if age <= 7:
            age_buckets["0_7"] += 1
        elif age <= 30:
            age_buckets["8_30"] += 1
        elif age <= 90:
            age_buckets["31_90"] += 1
        else:
            age_buckets["91_plus"] += 1
    return {"project": project, "type_counts": dict(type_counts), "top_tags": dict(tag_counts.most_common(10)), "age_buckets": age_buckets}


def _project_summary_compare(request: ProjectSummaryCompareRequest) -> dict:
    left_project = rt._canonical_project(request.left_project)
    right_project = rt._canonical_project(request.right_project)
    left = app_core._project_summary_get(left_project)
    right = app_core._project_summary_get(right_project)
    left_lines = set((left.get("content") or "").splitlines())
    right_lines = set((right.get("content") or "").splitlines())
    return {
        "left_project": left_project,
        "right_project": right_project,
        "left_only": sorted(left_lines - right_lines),
        "right_only": sorted(right_lines - left_lines),
        "shared": sorted(left_lines & right_lines),
    }


def _project_map_compare(request: ProjectMapCompareRequest) -> dict:
    left_project = rt._canonical_project(request.left_project)
    right_project = rt._canonical_project(request.right_project)
    left = _get_project_map(left_project)
    right = _get_project_map(right_project)
    left_sections = left.get("sections") or {}
    right_sections = right.get("sections") or {}
    keys = sorted(set(left_sections) | set(right_sections))
    comparison = []
    for key in keys:
        left_value = left_sections.get(key, "")
        right_value = right_sections.get(key, "")
        comparison.append({"section": key, "same": left_value == right_value, "left": left_value, "right": right_value})
    return {"left_project": left_project, "right_project": right_project, "sections": comparison}


def _project_similarity_report(request: ProjectSimilarityReportRequest) -> dict:
    canonical_project = rt._canonical_project(request.project)
    live_records = rt._load_live_memories()
    project_records: dict[str, list[dict]] = {}
    for item in live_records:
        raw_project = str(item.get("project") or "").strip()
        if not raw_project:
            continue
        project_records.setdefault(rt._canonical_project(raw_project), []).append(item)
    base_records = project_records.get(canonical_project, [])
    base_tags = set(tag for item in base_records for tag in (item.get("tags") or []))
    base_files = set(file for item in base_records for file in ((item.get("metadata") or {}).get("files") or []))
    scores = []
    for project in sorted(project_records):
        if project == canonical_project:
            continue
        records = project_records[project]
        tags = set(tag for item in records for tag in (item.get("tags") or []))
        files = set(file for item in records for file in ((item.get("metadata") or {}).get("files") or []))
        shared_tags = base_tags & tags
        shared_files = base_files & files
        score = len(shared_tags) * 1.0 + len(shared_files) * 1.5
        scores.append({"project": project, "score": round(score, 3), "shared_tags": sorted(list(shared_tags))[:10], "shared_files": sorted(list(shared_files))[:10]})
    scores.sort(key=lambda item: item["score"], reverse=True)
    return {"project": canonical_project, "similar_projects": scores[: max(min(request.limit, 200), 1)]}


def _resolve_slot(project: str, label: str) -> dict | None:
    for item in _load_slots():
        if item.get("project") == project and item.get("label") == label:
            return item
    return None


def bhm_project_map_get(project: str) -> dict:
    project_map = _get_project_map(project)
    return {"project_map": _serialize_project_map_record(project_map)}


def bhm_projects(request: Request) -> dict:
    report = rt._PROJECT_REGISTRY.report()
    principal = getattr(request.state, "bhm_caller_principal", None)
    if principal is None or principal.all_projects:
        return report
    allowed = set(principal.allowed_projects)
    report["projects"] = [item for item in report["projects"] if str(item.get("id") or "") in allowed]
    if principal.default_project not in allowed:
        report["default_project"] = next(iter(sorted(allowed)), "")
    return report


def bhm_project_resolve(project: str = "") -> dict:
    resolution: ProjectResolution = rt._PROJECT_REGISTRY.resolve(project)
    return {"resolution": resolution.as_dict()}


def bhm_project_retirement_preview(project: str) -> dict:
    """Read-only, preview-first project retirement contract."""

    project_id = rt._canonical_project(project)
    database_path = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path
    try:
        return preview_project_retirement(database_path, project_id)
    except ProjectRetirementError as exc:
        raise HTTPException(status_code=422, detail={"error": "project_retirement_preview_rejected", "detail": _safe_exception_text(exc)}) from exc


async def bhm_project_retirement_apply(request: ProjectRetirementRequest, http_request: Request) -> dict:
    """Apply an allowlisted logical retirement; never unlinks a database."""

    project_id = rt._canonical_project(request.project)
    database_path = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path
    capability = str(request.capability or http_request.headers.get(ADMIN_CAPABILITY_HEADER, ""))

    def _apply() -> dict[str, Any]:
        try:
            return apply_project_retirement(
                database_path,
                project_id,
                capability=capability,
                backup_dir=request.backup_dir,
            )
        except ProjectRetirementError as exc:
            raise HTTPException(status_code=422, detail={"error": "project_retirement_rejected", "detail": _safe_exception_text(exc)}) from exc

    return await rt._run_bounded_write("bhm.project-retirement", _apply)


async def bhm_project_map_upsert(request: ProjectMapUpsertRequest) -> dict:
    action, project_map = await rt._run_bounded_write("bhm.project-map", _upsert_project_map, request)
    return {"success": True, "action": action, "project_map": _serialize_project_map_record(project_map)}


def bhm_rebuild_project_summary(request: RebuildProjectSummaryRequest) -> dict:
    return _rebuild_project_summary(request)


def bhm_project_summary_get(project: str) -> dict:
    return {"memory": app_core._project_summary_get(project)}


def bhm_project_summary_pin(request: ProjectSummaryPinRequest) -> dict:
    record = _project_summary_pin(request.project)
    return {"success": True, "memory": app_core._serialize_memory_record(record)}


def bhm_project_summary_list(request: ProjectSummaryListRequest) -> dict:
    items, total = _project_summary_list(request)
    return {"memories": [app_core._serialize_memory_record(item) for item in items], "total": total, "limit": max(min(request.limit, 200), 1), "offset": max(request.offset, 0)}


def bhm_project_summary_refresh_all(request: ProjectSummaryRefreshAllRequest) -> dict:
    return _project_summary_refresh_all(request)


def bhm_project_memory_heatmap(request: ProjectOnlyRequest) -> dict:
    return _project_memory_heatmap(request.project)


def bhm_project_summary_compare(request: ProjectSummaryCompareRequest) -> dict:
    return _project_summary_compare(request)


def bhm_project_map_compare(request: ProjectMapCompareRequest) -> dict:
    return _project_map_compare(request)


def bhm_project_similarity_report(request: ProjectSimilarityReportRequest, http_request: Request) -> dict:
    principal = getattr(http_request.state, "bhm_caller_principal", None)
    if principal is None:
        raise HTTPException(status_code=401, detail={"code": "caller_auth_required"})
    if not principal.all_projects:
        raise HTTPException(
            status_code=403,
            detail={"code": "all_projects_capability_required", "message": "cross-project similarity requires an all-projects caller"},
        )
    return _project_similarity_report(request)


_DEFAULT_FALLBACK_MEMORY_RECORDS = app_core_hit._fallback_memory_records


def _count_profile_records(project: str) -> int:
    if app_core_hit._fallback_memory_records is not _DEFAULT_FALLBACK_MEMORY_RECORDS:
        return len(app_core_hit._fallback_memory_records(project=project, include_logs=True))
    try:
        return rt._memory_service().count_records(
            project=rt._canonical_project(project),
            include_archived=False,
            exclude_event_role="trace",
        )
    except Exception:
        return len(app_core_hit._fallback_memory_records(project=project, include_logs=True))


async def bhm_profile(project: str = "e-github-workspace") -> dict:
    memory_count, policy_snapshot, registry_snapshot = await asyncio.gather(
        asyncio.to_thread(_count_profile_records, project=project),
        asyncio.to_thread(app_core_hook._load_policy_profile_snapshot),
        asyncio.to_thread(_load_mcp_registry_snapshot),
    )
    policy_profile = policy_snapshot["profile"]
    provider_warmup = rt._get_provider_warmup_status()
    mem0_plan = mem0_runtime_plan()
    provider_ready = bool(provider_warmup.get("ready"))
    mem0_enabled = bool(mem0_plan.get("enabled"))
    if not mem0_enabled:
        status = "disabled"
    elif not provider_ready:
        status = "warming"
    elif not policy_snapshot["exists"] or not registry_snapshot["loaded"]:
        status = "degraded"
    else:
        status = "ready"
    ready = status == "ready"

    response = {
        "status": status,
        "context_flags": {
            "project": project,
            "include_logs": True,
            "policy_profile_loaded": bool(policy_snapshot["exists"]),
            "registry_loaded": bool(registry_snapshot["loaded"]),
            "provider_ready": provider_ready,
            "mem0_enabled": mem0_enabled,
            "require_project": bool(policy_profile.get("require_project")),
            "require_memory_type": bool(policy_profile.get("require_memory_type")),
            "block_secret_like": bool(policy_profile.get("block_secret_like")),
            "block_raw_logs": bool(policy_profile.get("block_raw_logs")),
        },
        "readiness": {
            "ready": ready,
            "provider_warmup": provider_warmup,
            "mem0": mem0_plan,
            "registry": {
                "path": registry_snapshot["path"],
                "loaded": bool(registry_snapshot["loaded"]),
                "instance_count": registry_snapshot["instance_count"],
            },
            "policy_profile": {
                "path": policy_snapshot["path"],
                "loaded": bool(policy_snapshot["exists"]),
            },
        },
        "profile": {
            "project": project,
            "sessionCount": memory_count,
            "memoryCount": memory_count,
            "source": "live-memory-snapshot",
        },
    }
    return response


def bhm_slots(project: str = "e-github-workspace") -> dict:
    slots = [item for item in _load_slots() if item.get("project") == project]
    return {"slots": slots}


def bhm_slot(label: str, project: str = "e-github-workspace") -> dict:
    item = _resolve_slot(project, label)
    return {"slot": item}


def bhm_slot_create(request: SlotRequest, project: str = "e-github-workspace") -> dict:
    project = request.project or project
    slots = _load_slots()
    slots = [
        item for item in slots
        if not (item.get("project") == project and item.get("label") == request.label)
    ]
    slot = {
        "project": project,
        "label": request.label,
        "content": request.content[: request.sizeLimit],
        "sizeLimit": request.sizeLimit,
        "description": request.description,
        "pinned": request.pinned,
        "scope": request.scope,
    }
    slots.append(slot)
    _save_slots(slots)
    return {"success": True, "slot": slot}


def bhm_slot_append(request: SlotAppendRequest, project: str = "e-github-workspace") -> dict:
    project = request.project or project
    slots = _load_slots()
    updated = None
    for item in slots:
        if item.get("project") == project and item.get("label") == request.label:
            combined = ((item.get("content") or "") + request.text)[: int(item.get("sizeLimit") or 2000)]
            item["content"] = combined
            updated = item
            break
    if updated is None:
        updated = {
            "project": project,
            "label": request.label,
            "content": request.text[:2000],
            "sizeLimit": 2000,
            "description": "",
            "pinned": True,
            "scope": "project",
        }
        slots.append(updated)
    _save_slots(slots)
    return {"success": True, "slot": updated}


def bhm_slot_replace(request: SlotReplaceRequest, project: str = "e-github-workspace") -> dict:
    project = request.project or project
    slots = _load_slots()
    updated = None
    for item in slots:
        if item.get("project") == project and item.get("label") == request.label:
            item["content"] = request.content[: int(item.get("sizeLimit") or 2000)]
            updated = item
            break
    if updated is None:
        updated = {
            "project": project,
            "label": request.label,
            "content": request.content[:2000],
            "sizeLimit": 2000,
            "description": "",
            "pinned": True,
            "scope": "project",
        }
        slots.append(updated)
    _save_slots(slots)
    return {"success": True, "slot": updated}


def bhm_slot_delete(label: str, project: str = "e-github-workspace") -> dict:
    slots = _load_slots()
    remaining = [
        item for item in slots
        if not (item.get("project") == project and item.get("label") == label)
    ]
    _save_slots(remaining)
    return {"success": True, "deleted": len(remaining) != len(slots)}


def bhm_slot_reflect(request: SlotLabelRequest, project: str = "e-github-workspace") -> dict:
    project = request.project or project
    slot = _resolve_slot(project, request.label)
    if slot is None:
        raise HTTPException(status_code=404, detail="slot not found")
    return {"success": True, "slot": slot, "reflection": slot.get("content", "")[:200]}



def register(app) -> None:
    """Attach this module's routes to the application."""

    app.get("/bhm/project-map")(bhm_project_map_get)
    app.get("/bhm/projects")(bhm_projects)
    app.get("/bhm/project/resolve")(bhm_project_resolve)
    app.get("/bhm/project/retirement-preview")(bhm_project_retirement_preview)
    app.post("/bhm/project/retirement/apply")(bhm_project_retirement_apply)
    app.post("/bhm/project-map")(bhm_project_map_upsert)
    app.post("/bhm/project-summary/rebuild")(bhm_rebuild_project_summary)
    app.get("/bhm/project-summary")(bhm_project_summary_get)
    app.post("/bhm/project-summary/pin")(bhm_project_summary_pin)
    app.post("/bhm/project-summary/list")(bhm_project_summary_list)
    app.post("/bhm/project-summary/refresh-all")(bhm_project_summary_refresh_all)
    app.post("/bhm/project-memory-heatmap")(bhm_project_memory_heatmap)
    app.post("/bhm/project-summary/compare")(bhm_project_summary_compare)
    app.post("/bhm/project-map/compare")(bhm_project_map_compare)
    app.post("/bhm/project-similarity-report")(bhm_project_similarity_report)
    app.get("/bhm/profile")(bhm_profile)
    app.get("/bhm/slots")(bhm_slots)
    app.get("/bhm/slot")(bhm_slot)
    app.post("/bhm/slot")(bhm_slot_create)
    app.post("/bhm/slot/append")(bhm_slot_append)
    app.post("/bhm/slot/replace")(bhm_slot_replace)
    app.delete("/bhm/slot")(bhm_slot_delete)
    app.post("/bhm/slot/reflect")(bhm_slot_reflect)
