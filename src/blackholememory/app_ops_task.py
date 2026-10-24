"""Domain module app_ops_task: moved out of app.py (FIX-017). Cross-module calls go through module attributes."""

from __future__ import annotations

import asyncio
import os
import uuid
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
from .api_models import RememberRequest, MemoryRecentActivityRequest, MemoryUpsertRequest, ConventionMemoryPreviewRequest, FactoryIntegrationPreviewRequest, MigrationPreviewRequest, SecurityTrustBoundaryPreviewRequest, MemoryDeleteRequest, AdrCreateRequest, AdrSupersedeRequest, HandoffCreateRequest, SessionRecordCreateRequest, TaskOpenRequest, TaskCloseRequest, TaskIngressControlRequest, TaskContextUpdateRequest, RiskRegisterUpdateRequest, ValidationSnapshotSaveRequest, RepairLiveIndexesRequest, EntityExtractRequest, PolicyGuardRequest, ArtifactRestoreRequest, OrphanArtifactRelinkRequest, SchemaUpgradeAllRequest, RecentFailuresFeedRequest, ProjectOnlyRequest, ScopedMaintenanceRequest, ArtifactDeleteRequest, ArtifactListRequest, LinkCycleDetectRequest, ValidationTrendReportRequest, EntitySearchRequest, ArtifactBatchDeleteRequest, ArtifactBatchRelinkRequest, ArtifactBatchRestoreRequest, StrictSchemaValidateRequest, IntegrityRepairStrictRequest, AdminExportRequest, PolicyProfileSetRequest, OverlapReportRequest, OverlapCleanupApplyRequest, ReflectRequest, MemoryVerifyRequest
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
from .retention import build_retention_plan
from .retention import load_retention_policy
from .retention import parse_timestamp
from .retention import RetentionPolicyError
from .retention import summarize_retention_plan
from .observation_security import redact_secret_text
from .convention_memory import ConventionMemoryError
from .convention_memory import preview_convention_memory
from .factory_integration import FactoryIntegrationError
from .factory_integration import build_factory_integration_preview
from .migration_compatibility import MigrationCompatibilityError
from .migration_compatibility import build_migration_preview
from .security_trust_boundary import SecurityTrustBoundaryError
from .security_trust_boundary import build_security_trust_boundary_preview
from . import app_core_graph
from . import app_core_hook
from . import app_core
from . import app_ops
from . import app_ops_admin
from . import app_ops_entity
from . import app_ops_task2
from .app_ops_task_helpers import (
    _list_ontology_quarantine,
    _serialize_adr_record,
    _serialize_handoff_record,
    _serialize_validation_snapshot_record,
    _recent_activity_live_memories,
    _get_task,
    _build_reflection,
    _verify_memory,
    _entity_extract,
    _policy_guard,
    _link_graph_stats,
    _reindex_memory_metadata,
    _schema_upgrade_all,
    _link_cycle_detect,
    _validation_trend_report,
    _alias_stats,
    _overlap_report,
    _overlap_cleanup_apply,
)

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

_graph: Any = None

def _get_graph() -> Any:

    global _graph

    if _graph is None:

        _graph = build_graph()

    return _graph

def __getattr__(name: str) -> Any:

    if name == "graph":

        return _get_graph()

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

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
    state = _get_graph().invoke({"action": "status"})
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
