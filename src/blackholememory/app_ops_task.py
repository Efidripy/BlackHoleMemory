"""Domain module app_ops_task: moved out of app.py (FIX-017). Cross-module calls go through module attributes."""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from fastapi import HTTPException
from fastapi import Request
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.responses import FileResponse
from .ui_routes import _UI_SESSIONS
from . import app_runtime as rt
from .app_runtime import _safe_exception_text
from .api_models import RememberRequest, MemoryRecentActivityRequest, MemoryUpsertRequest, ConventionMemoryPreviewRequest, FactoryIntegrationPreviewRequest, MigrationPreviewRequest, SecurityTrustBoundaryPreviewRequest, MemoryDetectRequest, MemoryDeleteRequest, AdrCreateRequest, AdrSupersedeRequest, HandoffCreateRequest, SessionRecordCreateRequest, TaskOpenRequest, TaskCloseRequest, TaskIngressControlRequest, TaskContextUpdateRequest, RiskRegisterUpdateRequest, ValidationSnapshotSaveRequest, RepairLiveIndexesRequest, EntityExtractRequest, PolicyGuardRequest, ArtifactRestoreRequest, OrphanArtifactRelinkRequest, SchemaUpgradeAllRequest, RecentFailuresFeedRequest, ProjectOnlyRequest, ScopedMaintenanceRequest, ArtifactDeleteRequest, ArtifactListRequest, LinkCycleDetectRequest, ValidationTrendReportRequest, EntitySearchRequest, ArtifactBatchDeleteRequest, ArtifactBatchRelinkRequest, ArtifactBatchRestoreRequest, StrictSchemaValidateRequest, IntegrityRepairStrictRequest, AdminExportRequest, PolicyProfileSetRequest, OverlapReportRequest, OverlapCleanupApplyRequest, ReflectRequest, MemoryVerifyRequest
from .config import settings
from .capability import ADMIN_CAPABILITY_HEADER
from .capability import is_admin_capability_valid
from .caller_auth import authorize_projects
from .caller_auth import configured_caller_principal
from .caller_auth import extract_request_projects
from .caller_auth import is_caller_token_valid
from .caller_auth import parse_bearer_token
from .ui_session import UI_SESSION_COOKIE
from .ui_origin_policy import websocket_origin_is_allowed
from .graph import build_graph
from .ingress_admission import IngressKind
from .runtime_storage import resolve_runtime_storage_config
from .memory_service import MemoryServiceNotReady
from .ontology_quarantine import ARTIFACT_TYPE as ONTOLOGY_QUARANTINE_ARTIFACT_TYPE
from .ontology_quarantine import serialize_quarantine_record
from .retention import build_retention_plan
from .retention import load_retention_policy
from .retention import parse_timestamp
from .retention import RetentionPolicyError
from .retention import summarize_retention_plan
from .observation_security import contains_secret_like
from .observation_security import redact_secret_text
from .convention_memory import ConventionMemoryError
from .convention_memory import preview_convention_memory
from .factory_integration import FactoryIntegrationError
from .factory_integration import build_factory_integration_preview
from .migration_compatibility import MigrationCompatibilityError
from .migration_compatibility import build_migration_preview
from .security_trust_boundary import SecurityTrustBoundaryError
from .security_trust_boundary import build_security_trust_boundary_preview
from . import app_core_fallback
from . import app_core_graph
from . import app_core_hit
from . import app_core_hook
from . import app_core
from . import app_ops
from . import app_ops_admin
from . import app_ops_entity
from . import app_ops_task2


def _retention_policy_path() -> Path:
    configured = os.getenv("BHM_RETENTION_POLICY_PATH", "").strip()
    return Path(configured).expanduser() if configured else settings.repo_root / "config" / "retention-policy.json"


def _websocket_origin_is_allowed(websocket: WebSocket, *, require_exact_origin: bool) -> bool:
    return websocket_origin_is_allowed(
        host_header=websocket.headers.get("host"),
        origin_header=websocket.headers.get("origin"),
        canonical_port=settings.port,
        require_exact_origin=require_exact_origin,
    )


graph = build_graph()


STATIC_DIR = Path(__file__).parent / "static"


def _request_ingress(request: Request) -> IngressKind:
    """Downgrade the known MCP bridge; callers do not select their trust class."""

    surface = str(request.headers.get("X-BHM-Caller-Surface") or "").strip().casefold()
    if surface == IngressKind.MCP.value:
        return IngressKind.MCP
    # A bearer proves only caller access. An authority write additionally needs
    # the separate operator capability; otherwise the generic REST surface is
    # a non-authoritative tool ingress.
    capability = request.headers.get(ADMIN_CAPABILITY_HEADER, "")
    return IngressKind.OPERATOR if is_admin_capability_valid(capability) else IngressKind.TOOL


def _list_ontology_quarantine(project: str, limit: int = 100) -> dict[str, Any]:
    """Return a bounded project-local worklist; no resolution is applied here."""

    canonical_project = rt._canonical_project(project)
    bounded_limit = max(1, min(int(limit), 200))
    try:
        records = rt._memory_service().list_artifact_records(
            artifact_type=ONTOLOGY_QUARANTINE_ARTIFACT_TYPE,
            project=canonical_project,
            limit=bounded_limit,
        )
    except (MemoryServiceNotReady, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "ontology_quarantine_unavailable", "reason": _safe_exception_text(exc)},
        ) from exc
    items = [serialize_quarantine_record(item) for item in records]
    return {
        "schema_version": "bhm.ontology-quarantine-list.v1",
        "project": canonical_project,
        "items": items,
        "count": len(items),
        "limit": bounded_limit,
        "execution": {
            "sqlite_mutation": False,
            "link_storage_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
        },
    }


def _serialize_adr_record(record: dict) -> dict:
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "title": record.get("title"),
        "context": record.get("context") or "",
        "decision": record.get("decision") or "",
        "consequences": record.get("consequences") or "",
        "status": record.get("status") or "accepted",
        "files": record.get("files") or [],
        "concepts": record.get("concepts") or [],
        "memory_id": record.get("memory_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
    }


def _serialize_handoff_record(record: dict) -> dict:
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "title": record.get("title"),
        "current_state": record.get("current_state") or "",
        "decisions": record.get("decisions") or "",
        "validation": record.get("validation") or "",
        "next_agent_action": record.get("next_agent_action") or "",
        "next_owner_id": record.get("next_owner_id") or "",
        "handoff_sla_deadline": record.get("handoff_sla_deadline") or "",
        "files": record.get("files") or [],
        "concepts": record.get("concepts") or [],
        "memory_id": record.get("memory_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
    }


def _serialize_validation_snapshot_record(record: dict) -> dict:
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "title": record.get("title"),
        "lint": record.get("lint") or "",
        "tests": record.get("tests") or "",
        "smoke": record.get("smoke") or "",
        "docs": record.get("docs") or "",
        "overall_status": record.get("overall_status") or "",
        "command_summary": record.get("command_summary") or "",
        "memory_id": record.get("memory_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
    }


def _recent_activity_live_memories(request: MemoryRecentActivityRequest) -> list[dict]:
    app_core_fallback._require_typed_memory_boundary(
        memory_class=request.memory_class,
        event_role=request.event_role,
        as_of=request.as_of,
        valid_from=request.valid_from,
        valid_to=request.valid_to,
        include_temporal_unknown=request.include_temporal_unknown,
    )
    items = [
        item for item in rt._load_live_memories()
        if app_core_hit._memory_matches_filters(
            item,
            project=request.project,
            memory_type=request.memory_type,
            memory_class=request.memory_class,
            event_role=request.event_role,
            as_of=request.as_of,
            valid_from=request.valid_from,
            valid_to=request.valid_to,
            include_temporal_unknown=request.include_temporal_unknown,
            include_archived=request.include_archived,
        )
    ]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    return items[: max(min(request.limit, 200), 1)]


def _create_adr(request: AdrCreateRequest) -> tuple[str, dict]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    title = request.title.strip()
    concepts = list(dict.fromkeys((request.concepts or []) + ["adr", "architecture", "bhm"]))
    files = request.files or []
    upsert_key = request.upsert_key or f"adr:{request.project}:{title.lower()}"
    content = "\n".join(
        [
            title,
            f"context: {request.context}",
            f"decision: {request.decision}",
            f"consequences: {request.consequences}",
            f"status: {request.status}",
        ]
    )
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
    items = app_ops_task2._load_adrs()
    for item in items:
        if item.get("project") == request.project and (item.get("metadata") or {}).get("upsert_key") == upsert_key:
            item.update(
                {
                    "title": title,
                    "context": request.context,
                    "decision": request.decision,
                    "consequences": request.consequences,
                    "status": request.status,
                    "files": files,
                    "concepts": concepts,
                    "memory_id": memory_record.get("source_id"),
                    "updated_at": now,
                }
            )
            app_ops_task2._save_adrs(items)
            return action, item
    record = {
        "id": f"adr_bhm_{uuid.uuid4().hex[:16]}",
        "project": request.project,
        "title": title,
        "context": request.context,
        "decision": request.decision,
        "consequences": request.consequences,
        "status": request.status,
        "files": files,
        "concepts": concepts,
        "memory_id": memory_record.get("source_id"),
        "created_at": now,
        "updated_at": now,
        "metadata": {"upsert_key": upsert_key},
    }
    items.append(record)
    app_ops_task2._save_adrs(items)
    return action, record


def _list_adrs(project: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
    items = app_ops_task2._load_adrs()
    if project:
        items = [item for item in items if item.get("project") == project]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    start = max(offset, 0)
    end = start + max(min(limit, 200), 1)
    return items[start:end], total


def _get_task(task_id: str, project: str | None = None) -> dict:
    project_name = rt._canonical_project(project) if project else None
    for item in rt._load_tasks():
        if item.get("task_id") != task_id:
            continue
        if project_name and item.get("project") != project_name:
            continue
        return item
    raise HTTPException(status_code=404, detail="task not found")


def _build_reflection(project: str, max_clusters: int = 10) -> dict:
    concept_counts: dict[str, int] = {}
    memory_type_counts: dict[str, int] = {}
    for item in rt._load_live_memories():
        if not app_core_hit._memory_matches_filters(item, project=project, include_archived=False):
            continue
        memory_type = item.get("memory_type") or "unknown"
        memory_type_counts[memory_type] = memory_type_counts.get(memory_type, 0) + 1
        for concept in item.get("tags") or []:
            concept_counts[concept] = concept_counts.get(concept, 0) + 1
    clusters = [
        {"concept": concept, "count": count}
        for concept, count in sorted(concept_counts.items(), key=lambda pair: (-pair[1], pair[0]))[: max(max_clusters, 1)]
    ]
    memory_types = [
        {"type": memory_type, "count": count}
        for memory_type, count in sorted(memory_type_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:5]
    ]
    return {
        "project": project,
        "clusters": clusters,
        "memory_types": memory_types,
        "memory_count": sum(memory_type_counts.values()),
    }


def _verify_memory(request: MemoryVerifyRequest) -> dict:
    record = rt._find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return {
        "verified": True,
        "memory": app_core._serialize_memory_record(record),
    }


def _entity_extract(request: EntityExtractRequest) -> dict:
    record = rt._find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    content = record.get("content") or ""
    files = re.findall(r"\b[\w./-]+\.(?:py|ts|tsx|js|json|md|yml|yaml)\b", content)
    endpoints = re.findall(r"\b/(?:[A-Za-z0-9_.-]+/?)+", content)
    envs = re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", content)
    entities = {
        "files": sorted(set(files + ((record.get("metadata") or {}).get("files") or []))),
        "endpoints": sorted(set(endpoints)),
        "env_vars": sorted(set(envs)),
        "concepts": record.get("tags") or [],
    }
    return {"memory": app_core._serialize_memory_record(record), "entities": entities}


def _policy_guard(request: PolicyGuardRequest) -> dict:
    issues = []
    content = request.content or ""
    if contains_secret_like(content):
        issues.append({"severity": "error", "code": "possible_secret", "message": "content contains a secret-like token"})
    if len(content) > 8000:
        issues.append({"severity": "warning", "code": "too_large", "message": "content is too large for durable memory"})
    if content.count("\n") > 120:
        issues.append({"severity": "warning", "code": "raw_log_shape", "message": "content looks like a raw log dump"})
    if not request.project:
        issues.append({"severity": "warning", "code": "missing_project", "message": "project scope is missing"})
    if not request.memory_type:
        issues.append({"severity": "info", "code": "missing_type", "message": "memory type is missing"})
    return {"ok": not any(item["severity"] == "error" for item in issues), "issues": issues}


def _link_graph_stats(project: str | None = None) -> dict:
    project = rt._canonical_project(project) if project else None
    links = [item for item in rt._load_memory_links() if app_core_hook._project_matches(item.get("project"), project)]
    relation_counts = {}
    node_ids = set()
    for item in links:
        relation = item.get("relation") or "unknown"
        relation_counts[relation] = relation_counts.get(relation, 0) + 1
        node_ids.add(item.get("source_id"))
        node_ids.add(item.get("target_id"))
    return {"project": project, "link_count": len(links), "node_count": len(node_ids), "relation_counts": relation_counts}


def _reindex_memory_metadata(project: str | None = None, *, aggregate: bool = False) -> dict:
    if not project and not aggregate:
        raise HTTPException(
            status_code=403,
            detail={"code": "reindex_scope_required", "message": "project or aggregate=true is required"},
        )
    project = rt._canonical_project(project) if project else None
    live_records = rt._load_live_memories()
    updated = 0
    for item in live_records:
        if not app_core_hook._project_matches(item.get("project"), project):
            continue
        metadata = item.setdefault("metadata", {})
        content = item.get("content") or ""
        raw_title = app_core._build_memory_title(content)
        if metadata.get("raw_title") != raw_title:
            metadata["raw_title"] = raw_title
            updated += 1
    app_core._save_live_memories(live_records)
    return {"project": project, "updated": updated}


def _schema_upgrade_all(request: SchemaUpgradeAllRequest) -> dict:
    if not request.project and not request.aggregate:
        raise HTTPException(
            status_code=403,
            detail={"code": "schema_upgrade_scope_required", "message": "project or aggregate=true is required"},
        )
    project = rt._canonical_project(request.project) if request.project else None
    live_records = rt._load_live_memories()
    upgraded = 0
    for item in live_records:
        if not app_core_hook._project_matches(item.get("project"), project):
            continue
        changed = False
        metadata = item.setdefault("metadata", {})
        if "files" not in metadata:
            metadata["files"] = []
            changed = True
        if "source_refs" not in metadata:
            metadata["source_refs"] = []
            changed = True
        if "raw_title" not in metadata:
            metadata["raw_title"] = app_core._build_memory_title(item.get("content") or "")
            changed = True
        if "tags" in item and item.get("tags") is None:
            item["tags"] = []
            changed = True
        if changed:
            upgraded += 1
    app_core._save_live_memories(live_records)
    return {"project": project, "upgraded": upgraded}


def _recent_failures_feed(request: RecentFailuresFeedRequest) -> dict:
    project = rt._canonical_project(request.project) if request.project else None
    failures = []
    for item in app_core_hook._load_validation_snapshots():
        if not app_core_hook._project_matches(item.get("project"), project):
            continue
        if (item.get("overall_status") or "").lower() in {"failed", "error", "red"}:
            failures.append({"kind": "validation_snapshot", "artifact": _serialize_validation_snapshot_record(item)})
    for item in app_ops_task2._load_handoffs():
        if not app_core_hook._project_matches(item.get("project"), project):
            continue
        text = " ".join([item.get("current_state") or "", item.get("validation") or ""]).lower()
        if any(token in text for token in ("fail", "error", "blocked")):
            failures.append({"kind": "handoff", "artifact": _serialize_handoff_record(item)})
    failures.sort(key=lambda item: item["artifact"].get("updated_at") or item["artifact"].get("created_at") or "", reverse=True)
    return {"project": project, "items": failures[: max(min(request.limit, 200), 1)]}


def _link_cycle_detect(request: LinkCycleDetectRequest) -> dict:
    project = rt._canonical_project(request.project) if request.project else None
    links = [item for item in rt._load_memory_links() if app_core_hook._project_matches(item.get("project"), project)]
    graph: dict[str, list[str]] = {}
    for item in links:
        graph.setdefault(item.get("source_id"), []).append(item.get("target_id"))
    cycles = []
    visited = set()

    def dfs(node: str, path: list[str], seen: set[str]) -> None:
        if len(cycles) >= max(min(request.limit, 200), 1):
            return
        visited.add(node)
        for neighbor in graph.get(node, []):
            if neighbor in seen:
                start = path.index(neighbor) if neighbor in path else 0
                cycles.append(path[start:] + [neighbor])
                continue
            dfs(neighbor, path + [neighbor], seen | {neighbor})

    for node in list(graph):
        if node not in visited:
            dfs(node, [node], {node})
    return {"project": project, "cycles": cycles[: max(min(request.limit, 200), 1)], "count": len(cycles)}


def _validation_trend_report(request: ValidationTrendReportRequest) -> dict:
    project = rt._canonical_project(request.project)
    items = [item for item in app_core_hook._load_validation_snapshots() if app_core_hook._project_matches(item.get("project"), project)]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "")
    trend = [{"at": item.get("updated_at") or item.get("created_at"), "overall_status": item.get("overall_status"), "lint": item.get("lint"), "tests": item.get("tests"), "smoke": item.get("smoke")} for item in items[-max(min(request.limit, 200), 1):]]
    status_counts = Counter((item.get("overall_status") or "unknown").lower() for item in items)
    return {"project": project, "trend": trend, "status_counts": dict(status_counts)}


def _alias_stats(project: str | None = None) -> dict:
    project = rt._canonical_project(project) if project else None
    alias_counter = Counter()
    alias_projects = {}
    for item in rt._load_live_memories():
        if not app_core_hook._project_matches(item.get("project"), project):
            continue
        for alias in (item.get("metadata") or {}).get("aliases") or []:
            alias_counter[alias] += 1
            alias_projects.setdefault(alias, set()).add(item.get("project"))
    duplicates = [{"alias": alias, "count": count, "projects": sorted(alias_projects.get(alias) or [])} for alias, count in alias_counter.items() if count > 1]
    return {"project": project, "alias_count": sum(alias_counter.values()), "unique_alias_count": len(alias_counter), "duplicates": sorted(duplicates, key=lambda item: item["count"], reverse=True)}


def _overlap_report(request: OverlapReportRequest) -> dict:
    project = rt._canonical_project(request.project) if request.project else None
    duplicates = app_core._detect_duplicates(MemoryDetectRequest(project=project, limit=request.limit, include_archived=False))
    same_upsert = []
    by_key = {}
    for item in rt._load_live_memories():
        if not app_core_hit._memory_matches_filters(item, project=project, include_archived=True):
            continue
        key = ((item.get("metadata") or {}).get("upsert_key") or "")
        if not key:
            continue
        by_key.setdefault(key, []).append(item.get("source_id"))
    for key, ids in by_key.items():
        if len(ids) > 1:
            same_upsert.append({"upsert_key": key, "ids": ids})
    return {"project": project, "duplicate_candidates": duplicates[: max(min(request.limit, 200), 1)], "same_upsert_key": same_upsert}


def _overlap_cleanup_apply(request: OverlapCleanupApplyRequest) -> dict:
    if not request.project and not request.aggregate:
        raise HTTPException(
            status_code=403,
            detail={"code": "overlap_cleanup_scope_required", "message": "project or aggregate=true is required"},
        )
    project = rt._canonical_project(request.project) if request.project else None
    duplicates = app_core._detect_duplicates(MemoryDetectRequest(project=project, limit=request.limit, include_archived=False))
    merged = []
    used_ids: set[str] = set()
    plan: list[tuple[str, str]] = []
    for item in duplicates:
        source_id = item["right_id"]
        target_id = item["left_id"]
        if source_id in used_ids or target_id in used_ids:
            continue
        plan.append((source_id, target_id))
        used_ids.update((source_id, target_id))

    if not plan:
        return {"project": project, "count": 0, "items": [], "committed": True}

    planned_ids = [memory_id for pair in plan for memory_id in pair]
    canonical_project = rt._canonical_project(project) if project else None
    if rt._memory_store_is_authoritative():
        records = rt._memory_service().get_records(planned_ids, project=canonical_project)
    else:
        records = [
            item
            for item in rt._load_live_memories()
            if item.get("source_id") in planned_ids
            and app_core_hook._project_matches(item.get("project"), canonical_project)
        ]
    by_id = {str(record.get("source_id")): record for record in records}
    changed: list[dict] = []
    for source_id, target_id in plan:
        source = by_id.get(source_id)
        target = by_id.get(target_id)
        if source is None or target is None:
            raise HTTPException(status_code=404, detail="overlap cleanup memory not found")
        target, source = app_core._merge_memory_records(
            source,
            target,
            archive_source=request.archive_sources,
            target_id=target_id,
        )
        by_id[target_id] = target
        by_id[source_id] = source
        changed.extend((target, source))
        merged.append({"target": target, "source": source, "archived_source": request.archive_sources})
    app_core._save_live_memories(changed)
    return {"project": project, "count": len(merged), "items": merged, "committed": True}


def index() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


def favicon_ico() -> FileResponse:
    return FileResponse(STATIC_DIR / "bhm-favicon.svg", media_type="image/svg+xml")


def favicon_svg() -> FileResponse:
    return FileResponse(STATIC_DIR / "bhm-favicon.svg", media_type="image/svg+xml")


def redoc_bundle() -> FileResponse:
    return FileResponse(STATIC_DIR / "redoc.standalone.js", media_type="application/javascript")


def force_graph_bundle() -> FileResponse:
    return FileResponse(STATIC_DIR / "3d-force-graph.min.js", media_type="application/javascript")


def three_module_bundle() -> FileResponse:
    return FileResponse(STATIC_DIR / "three.module.min.js", media_type="application/javascript")


def three_core_bundle() -> FileResponse:
    return FileResponse(STATIC_DIR / "three.core.min.js", media_type="application/javascript")


def local_redoc() -> HTMLResponse:
    html = f"""<!DOCTYPE html>
<html>
<head>
  <title>{settings.app_name} - ReDoc</title>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <style>
    body {{
      margin: 0;
      padding: 0;
      background: #05070f;
    }}
  </style>
</head>
<body>
  <noscript>ReDoc requires Javascript to function.</noscript>
  <redoc spec-url="/openapi.json"></redoc>
  <script src="/static/redoc.standalone.js"></script>
</body>
</html>"""
    return HTMLResponse(html)


def _convention_memory_preview_response(request: ConventionMemoryPreviewRequest) -> dict[str, Any]:
    project = rt._canonical_project(request.project)
    database_path = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path
    root_id = request.root_id or app_core_hook._code_graph_query_root_id(project, settings.repo_root)
    try:
        return preview_convention_memory(
            database_path,
            project=project,
            root_id=root_id,
            graph_snapshot_id=request.graph_snapshot_id,
        )
    except ConventionMemoryError as exc:
        detail = redact_secret_text(str(exc)).value[:500]
        status_code = 503 if "unavailable" in detail.casefold() else 422
        raise HTTPException(status_code=status_code, detail={"error": "convention_preview_rejected", "detail": detail}) from exc


def bhm_conventions_preview(request: ConventionMemoryPreviewRequest) -> dict[str, Any]:
    """Internal read-only convention/architecture-memory preview."""

    return _convention_memory_preview_response(request)


def bhm_factories_preview(request: FactoryIntegrationPreviewRequest) -> dict[str, Any]:
    """Internal evidence-first QA/incident/documentation crosswalk preview."""

    try:
        return build_factory_integration_preview(
            request.artifacts,
            request.documents,
            project=rt._canonical_project(request.project),
            changed_paths=request.changed_paths,
            code_items=request.code_items,
            task_items=request.task_items,
            risk_class=request.risk_class,
            max_items=request.max_items,
        )
    except FactoryIntegrationError as exc:
        raise HTTPException(status_code=422, detail={"error": "factory_integration_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc


def bhm_migration_preview(request: MigrationPreviewRequest) -> dict[str, Any]:
    """Internal dry-run migration and compatibility preview."""

    try:
        kwargs: dict[str, Any] = {
            "source_kind": request.source_kind,
            "source_url": request.source_url,
            "source_commit": request.source_commit,
            "source_license": request.source_license,
            "input_schema": request.input_schema,
            "reviewer": request.reviewer,
            "project": rt._canonical_project(request.project) if request.project else None,
            "dry_run": request.dry_run,
        }
        if request.approved_licenses:
            kwargs["approved_licenses"] = request.approved_licenses
        return build_migration_preview(request.records, **kwargs)
    except MigrationCompatibilityError as exc:
        raise HTTPException(status_code=422, detail={"error": "migration_preview_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc


def bhm_security_trust_boundary_preview(request: SecurityTrustBoundaryPreviewRequest) -> dict[str, Any]:
    """Internal read-only security, prompt-injection and trust preview."""

    try:
        return build_security_trust_boundary_preview(
            request.items,
            project=rt._canonical_project(request.project),
            source_kind=request.source_kind,
            source_url=request.source_url,
            source_commit=request.source_commit,
            source_license=request.source_license,
            reviewer=request.reviewer,
            project_roots=request.project_roots,
            paths=request.paths,
            mcp_endpoints=request.mcp_endpoints,
            proposed_actions=request.proposed_actions,
            route=request.route,
            method=request.method,
            capability=request.capability,
            mutation_requested=request.mutation_requested,
            operator_approved=request.operator_approved,
            feature_enabled=request.feature_enabled,
            max_items=request.max_items,
        )
    except SecurityTrustBoundaryError as exc:
        raise HTTPException(status_code=422, detail={"error": "security_trust_boundary_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc


def graph_status() -> dict:
    state = graph.invoke({"action": "status"})
    return {"ok": True, "state": state}


async def bhm_memory_pulse_ws(websocket: WebSocket) -> None:
    configured_principal = configured_caller_principal()
    supplied = parse_bearer_token(websocket.headers.get("authorization"))
    bearer_authenticated = configured_principal is not None and is_caller_token_valid(supplied)
    ui_session_token = "" if bearer_authenticated else str(websocket.cookies.get(UI_SESSION_COOKIE) or "")
    ui_session_lease = _UI_SESSIONS.resolve_session_lease(
        ui_session_token,
        expected_principal=configured_principal,
    )
    principal = configured_principal if bearer_authenticated else (ui_session_lease[0] if ui_session_lease else None)
    if principal is None:
        await websocket.close(code=4401, reason="caller_auth_required")
        return
    if not _websocket_origin_is_allowed(websocket, require_exact_origin=not bearer_authenticated):
        await websocket.close(code=4403, reason="caller_origin_rejected")
        return

    requested_projects = extract_request_projects(websocket.query_params)
    project_error = authorize_projects(
        principal,
        requested_projects,
        require_explicit=not principal.all_projects,
    )
    if project_error:
        await websocket.close(code=4403, reason=project_error)
        return
    subscribed_projects = None if principal.all_projects and not requested_projects else frozenset(requested_projects)
    try:
        await rt._MEMORY_PULSE_BUS.connect(websocket, subscribed_projects)
    except RuntimeError as exc:
        if str(exc) == "pulse_client_limit":
            await websocket.close(code=1013, reason="pulse_client_limit")
            return
        raise
    try:
        while True:
            if bearer_authenticated:
                await websocket.receive_text()
                continue
            lease = _UI_SESSIONS.resolve_session_lease(
                ui_session_token,
                expected_principal=configured_principal,
            )
            if lease is None:
                await websocket.close(code=4408, reason="ui_session_expired")
                return
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=max(min(lease[1], 1.0), 0.01))
            except asyncio.TimeoutError:
                continue
    except WebSocketDisconnect:
        rt._MEMORY_PULSE_BUS.disconnect(websocket)
    finally:
        rt._MEMORY_PULSE_BUS.disconnect(websocket)


def bhm_memory_get(id: str, project: str | None = None) -> dict:
    record = rt._find_live_memory(id, project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found in live store")
    return {"memory": app_core._serialize_memory_record(record)}


def bhm_recent_activity(request: MemoryRecentActivityRequest) -> dict:
    memories = _recent_activity_live_memories(request)
    return {
        "memories": [app_core._serialize_memory_record(item) for item in memories],
        "limit": max(min(request.limit, 200), 1),
        "filters": {
            "project": request.project,
            "memory_type": request.memory_type,
            "memory_class": request.memory_class.value if request.memory_class else None,
            "event_role": request.event_role.value if request.event_role else None,
            "as_of": request.as_of,
            "valid_from": request.valid_from,
            "valid_to": request.valid_to,
            "include_temporal_unknown": request.include_temporal_unknown,
            "include_archived": request.include_archived,
        },
    }


def bhm_checkpoint_list(
    project: str | None = None,
    checkpoint_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    checkpoints, total = app_core_hook._list_checkpoints(project, checkpoint_type, limit, offset)
    return {
        "checkpoints": [app_core_hook._serialize_checkpoint_record(item) for item in checkpoints],
        "total": total,
        "limit": max(min(limit, 200), 1),
        "offset": max(offset, 0),
    }


def bhm_checkpoint_get_latest(project: str, checkpoint_type: str | None = None) -> dict:
    checkpoint = app_core_hook._get_latest_checkpoint(project, checkpoint_type)
    return {"checkpoint": app_core_hook._serialize_checkpoint_record(checkpoint)}


async def bhm_memory_delete(request: MemoryDeleteRequest) -> dict:
    deleted = await rt._run_bounded_write("bhm.memory.delete", app_ops_task2._delete_live_memory, request)
    return {"success": True, "memory": app_core._serialize_memory_record(deleted)}


async def bhm_adr_create(request: AdrCreateRequest) -> dict:
    action, record = await rt._run_bounded_write("bhm.adr", _create_adr, request)
    return {"success": True, "action": action, "adr": _serialize_adr_record(record)}


def bhm_adr_list(project: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    items, total = _list_adrs(project, limit, offset)
    return {
        "adrs": [_serialize_adr_record(item) for item in items],
        "total": total,
        "limit": max(min(limit, 200), 1),
        "offset": max(offset, 0),
    }


async def bhm_adr_supersede(request: AdrSupersedeRequest) -> dict:
    result = await rt._run_bounded_write(
        "bhm.adr.supersede",
        app_ops._adr_supersede,
        request.project,
        request.old_id,
        request.new_id,
    )
    return {
        "success": True,
        "old": _serialize_adr_record(result["old"]),
        "new": _serialize_adr_record(result["new"]),
    }


async def bhm_handoff_create(request: HandoffCreateRequest) -> dict:
    action, record = await rt._run_bounded_write("bhm.handoff", app_ops_task2._create_handoff, request)
    return {"success": True, "action": action, "handoff": _serialize_handoff_record(record)}


def bhm_handoff_list(project: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    items, total = app_ops_admin._list_handoffs(project, limit, offset)
    return {
        "handoffs": [_serialize_handoff_record(item) for item in items],
        "total": total,
        "limit": max(min(limit, 200), 1),
        "offset": max(offset, 0),
    }


async def bhm_session_record_create(request: SessionRecordCreateRequest) -> dict:
    action, record = await rt._run_bounded_write("bhm.session-record", app_ops_task2._create_session_record, request)
    return {"success": True, "action": action, "session_record": app_ops_entity._serialize_session_record(record)}


def bhm_session_record_list(project: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    items, total = app_ops_entity._list_session_records(project, limit, offset)
    return {
        "session_records": [app_ops_entity._serialize_session_record(item) for item in items],
        "total": total,
        "limit": max(min(limit, 200), 1),
        "offset": max(offset, 0),
    }


async def bhm_task_open(request: TaskOpenRequest) -> dict:
    action, record = await rt._run_bounded_write("bhm.task.open", app_ops_task2._open_task, request)
    return {"success": True, "action": action, "task": app_core_hook._serialize_task_record(record)}


async def bhm_task_close(request: TaskCloseRequest) -> dict:
    action, record = await rt._run_bounded_write("bhm.task.close", app_ops_task2._close_task, request)
    return {"success": True, "action": action, "task": app_core_hook._serialize_task_record(record)}


def bhm_task_get(task_id: str, project: str | None = None) -> dict:
    return {"task": app_core_hook._serialize_task_record(_get_task(task_id, project))}


def bhm_task_list(
    project: str | None = None,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    items, total = app_core_hook._list_tasks(project, status, limit, offset)
    return {
        "tasks": [app_core_hook._serialize_task_record(item) for item in items],
        "total": total,
        "limit": max(min(limit, 200), 1),
        "offset": max(offset, 0),
    }


async def bhm_task_context_update(request: TaskContextUpdateRequest) -> dict:
    action, record = await rt._run_bounded_write("bhm.task-context", app_ops_task2._upsert_task_context, request)
    return {"success": True, "action": action, "task_context": app_core_hook._serialize_task_context_record(record)}


def bhm_task_context_get(project: str) -> dict:
    record = app_core_hook._get_task_context(project)
    return {"task_context": app_core_hook._serialize_task_context_record(record)}


async def bhm_risk_register_update(request: RiskRegisterUpdateRequest) -> dict:
    action, record = await rt._run_bounded_write("bhm.risk-register", app_ops_task2._upsert_risk_register, request)
    return {"success": True, "action": action, "risk_register": app_core_hook._serialize_risk_register_record(record)}


def bhm_risk_register_get(project: str) -> dict:
    record = app_core_hook._get_risk_register(project)
    return {"risk_register": app_core_hook._serialize_risk_register_record(record)}


async def bhm_validation_snapshot_save(request: ValidationSnapshotSaveRequest) -> dict:
    action, record = await rt._run_bounded_write("bhm.validation-snapshot", app_ops_task2._save_validation_snapshot_record, request)
    return {"success": True, "action": action, "validation_snapshot": _serialize_validation_snapshot_record(record)}


def bhm_validation_snapshot_get(project: str) -> dict:
    record = app_core_hook._get_validation_snapshot(project)
    return {"validation_snapshot": _serialize_validation_snapshot_record(record)}


def bhm_query_suggestions(project: str | None = None) -> dict:
    return {"suggestions": app_core_graph._query_suggestions(project)}


def bhm_integrity_audit(project: str | None = None) -> dict:
    return app_ops._integrity_audit(project)


def bhm_audit(project: str = "e-github-workspace", operation: str = "", limit: int = 50) -> dict:
    return {
        "project": project,
        "operation": operation,
        "limit": max(min(limit, 200), 1),
        "audit": app_ops._integrity_audit(project),
    }


def bhm_artifact_integrity_audit(request: ProjectOnlyRequest) -> dict:
    return app_ops_admin._artifact_integrity_audit(request.project)


def bhm_repair_live_indexes(request: RepairLiveIndexesRequest) -> dict:
    return app_ops_task2._repair_live_indexes(request)


def bhm_entity_extract(request: EntityExtractRequest) -> dict:
    return _entity_extract(request)


def bhm_link_graph_stats(request: ProjectOnlyRequest) -> dict:
    return _link_graph_stats(request.project)


def bhm_reindex_memory_metadata(request: ScopedMaintenanceRequest) -> dict:
    return _reindex_memory_metadata(request.project, aggregate=request.aggregate)


def bhm_artifact_restore(request: ArtifactRestoreRequest) -> dict:
    return app_ops_admin._artifact_restore(request)


def bhm_orphan_artifact_relink(request: OrphanArtifactRelinkRequest) -> dict:
    return app_ops_admin._orphan_artifact_relink(request)


def bhm_ontology_quarantine_list(project: str, limit: int = 100) -> dict:
    return _list_ontology_quarantine(project, limit)


def bhm_schema_upgrade_all(request: SchemaUpgradeAllRequest) -> dict:
    return _schema_upgrade_all(request)


def bhm_agent_activity_rollup(request: ProjectOnlyRequest) -> dict:
    return app_ops_admin._agent_activity_rollup(request.project)


def bhm_recent_failures_feed(request: RecentFailuresFeedRequest) -> dict:
    return _recent_failures_feed(request)


def bhm_artifact_delete(request: ArtifactDeleteRequest) -> dict:
    return app_ops_admin._artifact_delete(request)


def bhm_artifact_list_by_type(request: ArtifactListRequest) -> dict:
    return app_ops_admin._artifact_list_by_type(request)


def bhm_artifact_usage_stats(request: ProjectOnlyRequest) -> dict:
    return app_ops_admin._artifact_usage_stats(request.project)


def bhm_link_cycle_detect(request: LinkCycleDetectRequest) -> dict:
    return _link_cycle_detect(request)


def bhm_validation_trend_report(request: ValidationTrendReportRequest) -> dict:
    return _validation_trend_report(request)


def bhm_entity_search(request: EntitySearchRequest) -> dict:
    return app_ops_entity._entity_search(request)


def bhm_alias_stats(request: ProjectOnlyRequest) -> dict:
    return _alias_stats(request.project)


def bhm_artifact_batch_delete(request: ArtifactBatchDeleteRequest) -> dict:
    return app_ops_admin._artifact_batch_delete(request)


def bhm_artifact_batch_relink(request: ArtifactBatchRelinkRequest) -> dict:
    return app_ops_admin._artifact_batch_relink(request)


def bhm_artifact_batch_restore(request: ArtifactBatchRestoreRequest) -> dict:
    return app_ops_admin._artifact_batch_restore(request)


def bhm_schema_validate_strict(request: StrictSchemaValidateRequest) -> dict:
    return app_ops_admin._schema_validate_strict(request)


def bhm_integrity_repair_strict(request: IntegrityRepairStrictRequest) -> dict:
    return app_ops_admin._integrity_repair_strict(request)


def bhm_admin_export(request: AdminExportRequest) -> dict:
    return app_ops_admin._admin_export(request)


def bhm_admin_task_ingress_status() -> dict:
    return {"task_ingress": app_ops_task2._task_ingress_status()}


def bhm_admin_task_ingress_freeze(request: TaskIngressControlRequest) -> dict:
    action, status = app_ops_task2._freeze_task_ingress(request)
    return {"success": True, "action": action, "task_ingress": status}


def bhm_admin_task_ingress_resume(request: TaskIngressControlRequest) -> dict:
    action, status = app_ops_task2._resume_task_ingress(request)
    return {"success": True, "action": action, "task_ingress": status}


async def bhm_policy_profile_get() -> dict:
    snapshot = await asyncio.to_thread(app_core_hook._load_policy_profile_snapshot)
    return snapshot["profile"]


async def bhm_policy_profile_set(request: PolicyProfileSetRequest) -> dict:
    return await asyncio.to_thread(app_ops_entity._policy_profile_set, request)


def bhm_overlap_report(request: OverlapReportRequest) -> dict:
    return _overlap_report(request)


def bhm_overlap_cleanup_apply(request: OverlapCleanupApplyRequest) -> dict:
    return _overlap_cleanup_apply(request)


def bhm_policy_guard(request: PolicyGuardRequest) -> dict:
    return _policy_guard(request)


def bhm_reflect(request: ReflectRequest) -> dict:
    return {"success": True, "reflection": _build_reflection(request.project, request.maxClusters)}


def bhm_verify(request: MemoryVerifyRequest) -> dict:
    return _verify_memory(request)


async def bhm_remember(request: RememberRequest, http_request: Request) -> dict:
    ingress = _request_ingress(http_request)
    if ingress is not IngressKind.OPERATOR:
        app_core._admit_new_content(
            ingress=ingress,
            project=request.project,
            actor=app_core_hook._request_actor(http_request),
            payload=request.model_dump(mode="json"),
            requires_authority=True,
        )
    return await app_core._remember_authoritative(request, actor=app_core_hook._request_actor(http_request))


def bhm_observation_store_status(integrity: bool = False) -> dict:
    store = app_core_hook._observation_store()
    return store.status(integrity_check=integrity)


def bhm_retention_status(project: str | None = None, as_of: str | None = None) -> dict:
    try:
        policy = load_retention_policy(_retention_policy_path())
        effective_as_of = parse_timestamp(as_of) if as_of else datetime.now(timezone.utc)
        if effective_as_of is None:
            raise HTTPException(status_code=422, detail={"error": "invalid_retention_as_of"})
        plan = build_retention_plan(
            app_core_hook._observation_store().retention_candidates(project=project),
            app_core_hook._hook_queue().retention_candidates(project=project),
            policy,
            as_of=effective_as_of,
        )
        return {
            "success": True,
            "mode": "dry-run",
            "project": project or "",
            "plan": summarize_retention_plan(plan),
            "stores": {
                "observations": app_core_hook._observation_store().status(),
                "hookQueue": app_core_hook._hook_queue().status(),
            },
        }
    except HTTPException:
        raise
    except (OSError, RetentionPolicyError) as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "retention_policy_unavailable", "detail": _safe_exception_text(exc)},
        ) from exc
