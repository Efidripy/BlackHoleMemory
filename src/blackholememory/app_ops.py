"""Domain module app_ops: moved out of app.py (FIX-017). Cross-module calls go through module attributes."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import threading
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from fastapi import HTTPException
from fastapi import Request
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.responses import FileResponse
from .memory_routes import _serialized_link_mutation
from .ui_routes import _UI_SESSIONS
from . import app_runtime as rt
from .app_runtime import _safe_exception_text, _write_json_atomic
from .api_models import RememberRequest, ForgetPreviewRequest, ForgetApplyRequest, MemoryRecentActivityRequest, MemoryUpsertRequest, MemoryLinkRequest, ConventionMemoryPreviewRequest, FactoryIntegrationPreviewRequest, MigrationPreviewRequest, SecurityTrustBoundaryPreviewRequest, CheckpointCreateRequest, MemoryDetectRequest, MemoryDeleteRequest, AdrCreateRequest, AdrSupersedeRequest, HandoffCreateRequest, SessionRecordCreateRequest, TaskOpenRequest, TaskCloseRequest, TaskIngressControlRequest, TaskContextUpdateRequest, RiskRegisterUpdateRequest, ValidationSnapshotSaveRequest, HardDeleteMemoryRequest, RestoreMemoryRequest, RepairLiveIndexesRequest, EntityExtractRequest, RelationSuggestRequest, PolicyGuardRequest, ArtifactRestoreRequest, OrphanArtifactRelinkRequest, MemoryReviewQueueRequest, RelationApplySuggestionsRequest, SchemaUpgradeAllRequest, MemoryRedactRequest, RelationConfidenceRequest, RelationVoteRequest, EntityCatalogRequest, RecentFailuresFeedRequest, ProjectOnlyRequest, ScopedMaintenanceRequest, ArtifactDeleteRequest, ArtifactListRequest, LinkCycleDetectRequest, ValidationTrendReportRequest, EntitySearchRequest, EntityLinkMemoriesRequest, RelationPruneLowQualityRequest, ReviewQueueApplyRequest, TriageQueueApplyRequest, ArtifactBatchDeleteRequest, ArtifactBatchRelinkRequest, ArtifactBatchRestoreRequest, StrictSchemaValidateRequest, IntegrityRepairStrictRequest, AdminExportRequest, AdminImportPreviewRequest, AdminImportApplyRequest, PolicyProfileSetRequest, PolicyEnforceMemoryRequest, OverlapReportRequest, OverlapCleanupApplyRequest, ReflectRequest, MemoryVerifyRequest
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
from .filesystem_boundaries import assert_safe_path
from .filesystem_boundaries import FilesystemReadLimitError
from .filesystem_boundaries import read_bytes_safely
from .graph import build_graph
from .ingress_admission import IngressKind
from .ingress_admission import admit_ingress
from .mem0_adapter import StorageNotReady
from .lifecycle_suggestions import build_lifecycle_suggestions
from .runtime_storage import resolve_runtime_storage_config
from .memory_service import MemoryServiceNotReady
from .ontology_registry import ACTIVATION_ARTIFACT_TYPE as ONTOLOGY_ACTIVATION_ARTIFACT_TYPE
from .ontology_registry import ARTIFACT_TYPE as ONTOLOGY_REGISTRY_ARTIFACT_TYPE
from .ontology_registry import OntologyRegistryError
from .ontology_registry import OntologyRelationWrite
from .ontology_registry import admit_relation_write
from .ontology_registry import resolve_active_schema
from .ontology_quarantine import ARTIFACT_TYPE as ONTOLOGY_QUARANTINE_ARTIFACT_TYPE
from .ontology_quarantine import build_quarantine_artifact
from .ontology_quarantine import serialize_quarantine_record
from .sync_service import InvalidTombstone
from .sync_service import UndoWindowExpired
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
from .security_boundaries import SecurityBoundaryError
from .security_boundaries import compile_bounded_regex
from .security_boundaries import resolve_under_root
from . import app_core


_CUSTOM_REDACTION_MAX_PATTERN_LENGTH = 120


_CUSTOM_REDACTION_MAX_INPUT_CHARS = 64 * 1024


def _retention_policy_path() -> Path:
    configured = os.getenv("BHM_RETENTION_POLICY_PATH", "").strip()
    return Path(configured).expanduser() if configured else settings.repo_root / "config" / "retention-policy.json"


MAX_ADMIN_SNAPSHOT_BYTES = 16 * 1024 * 1024


def _websocket_origin_is_allowed(websocket: WebSocket, *, require_exact_origin: bool) -> bool:
    return websocket_origin_is_allowed(
        host_header=websocket.headers.get("host"),
        origin_header=websocket.headers.get("origin"),
        canonical_port=settings.port,
        require_exact_origin=require_exact_origin,
    )


graph = build_graph()


STATIC_DIR = Path(__file__).parent / "static"


_TASK_LIFECYCLE_LOCK = threading.RLock()


_TASK_INGRESS_FROZEN = False


_TASK_INGRESS_REASON = ""


_TASK_INGRESS_CHANGED_AT: str | None = None


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


def _active_ontology_schema(project: str):
    """Resolve one explicitly activated project ontology from SQLite.

    No marker means compatibility mode. A malformed or dangling active marker
    fails closed, because accepting an unpinned relation would defeat the
    registry contract.
    """

    if not rt._memory_store_is_authoritative():
        return None
    canonical_project = rt._canonical_project(project)
    try:
        service = rt._memory_service()
        markers = service.list_artifact_records(
            artifact_type=ONTOLOGY_ACTIVATION_ARTIFACT_TYPE,
            project=canonical_project,
            limit=8,
        )
        marker = next(
            (item for item in markers if item.get("id") == f"ontology_activation_{canonical_project}"),
            None,
        )
        if marker is None:
            return None
        registries = service.list_artifact_records(
            artifact_type=ONTOLOGY_REGISTRY_ARTIFACT_TYPE,
            project=canonical_project,
            limit=128,
        )
        return resolve_active_schema(
            project=canonical_project,
            registry_records=registries,
            activation_record=marker,
        )
    except MemoryServiceNotReady as exc:
        raise StorageNotReady(str(exc)) from exc
    except OntologyRegistryError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "ontology_registry_activation_invalid", "reason": str(exc)},
        ) from exc


def _append_ontology_quarantine(receipt: Any) -> dict[str, Any]:
    """Persist a rejected link intent without admitting a link or projection."""

    artifact = build_quarantine_artifact(receipt)
    try:
        stored, inserted = rt._memory_service().append_artifact(artifact)
    except (MemoryServiceNotReady, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "ontology_quarantine_unavailable", "reason": _safe_exception_text(exc)},
        ) from exc
    return {**serialize_quarantine_record(stored), "stored": inserted}


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


def _adr_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "adrs.json"


def _handoff_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "handoffs.json"


def _entity_catalog_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "entity-catalogs.json"


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


def _delete_live_memory(request: MemoryDeleteRequest) -> dict:
    try:
        scoped_project = rt._canonical_project(request.project) if request.project else None
        existing = rt._memory_service().get_record(request.id, project=scoped_project)
        accepted_projects = rt._project_aliases(request.project)
        if existing is None or (
            request.project and existing.get("project") not in accepted_projects
        ):
            raise HTTPException(status_code=404, detail="memory not found in SQLite store")
        deleted = rt._memory_service().tombstone(
            request.id,
            project=scoped_project,
            reason="user_delete",
        )
    except MemoryServiceNotReady as exc:
        raise StorageNotReady(str(exc)) from exc
    if deleted is None:
        raise HTTPException(status_code=404, detail="memory not found in SQLite store")
    return deleted


@_serialized_link_mutation
def _delete_live_memory_hard(request: HardDeleteMemoryRequest) -> dict:
    deleted = _delete_live_memory(MemoryDeleteRequest(id=request.id, project=request.project))
    accepted_projects = rt._project_aliases(request.project) if request.project else None

    def owns_item(item: dict) -> bool:
        if not accepted_projects:
            return True
        return item.get("project") in accepted_projects

    links = [
        item for item in rt._load_memory_links()
        if not (
            owns_item(item)
            and (item.get("source_id") == request.id or item.get("target_id") == request.id)
        )
    ]
    rt._save_memory_links(links)
    for loader, saver in (
        (app_core._load_checkpoints, _save_checkpoints),
        (app_core._load_project_maps, app_core._save_project_maps),
        (_load_adrs, _save_adrs),
        (_load_handoffs, _save_handoffs),
        (app_core._load_session_records, _save_session_records),
        (rt._load_tasks, rt._save_tasks),
        (app_core._load_task_contexts, _save_task_contexts),
        (app_core._load_risk_registers, _save_risk_registers),
        (app_core._load_validation_snapshots, _save_validation_snapshots),
    ):
        items = loader()
        items = [
            item
            for item in items
            if not (owns_item(item) and item.get("memory_id") == request.id)
        ]
        saver(items)
    return deleted


def _serialize_memory_link(link: dict) -> dict:
    return {
        "id": link.get("id"),
        "project": link.get("project"),
        "source_id": link.get("source_id"),
        "target_id": link.get("target_id"),
        "relation": link.get("relation"),
        "created_at": link.get("created_at"),
        "updated_at": link.get("updated_at"),
        "metadata": link.get("metadata") or {},
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


def _serialize_conflict_candidate(item: dict) -> dict:
    return {
        "queue_id": item.get("queue_id"),
        "left_id": item.get("left_id"),
        "right_id": item.get("right_id"),
        "project": item.get("project"),
        "score": item.get("score"),
        "reason": item.get("reason"),
        "left_title": item.get("left_title"),
        "right_title": item.get("right_title"),
        "shared_tags": item.get("shared_tags") or [],
        "left_revision_id": item.get("left_revision_id"),
        "right_revision_id": item.get("right_revision_id"),
        "left_content_sha256": item.get("left_content_sha256"),
        "right_content_sha256": item.get("right_content_sha256"),
    }


def _recent_activity_live_memories(request: MemoryRecentActivityRequest) -> list[dict]:
    app_core._require_typed_memory_boundary(
        memory_class=request.memory_class,
        event_role=request.event_role,
        as_of=request.as_of,
        valid_from=request.valid_from,
        valid_to=request.valid_to,
        include_temporal_unknown=request.include_temporal_unknown,
    )
    items = [
        item for item in rt._load_live_memories()
        if app_core._memory_matches_filters(
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


def _save_checkpoints(items: list[dict]) -> Path:
    return _write_json_atomic(app_core._checkpoint_store_path(), items)


def _load_adrs() -> list[dict]:
    path = _adr_store_path()
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return json.loads(raw)


def _save_adrs(items: list[dict]) -> Path:
    return _write_json_atomic(_adr_store_path(), items)


def _load_handoffs() -> list[dict]:
    path = _handoff_store_path()
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return json.loads(raw)


def _save_handoffs(items: list[dict]) -> Path:
    return _write_json_atomic(_handoff_store_path(), items)


def _save_session_records(items: list[dict]) -> Path:
    return _write_json_atomic(app_core._session_record_store_path(), items)


def _save_task_contexts(items: list[dict]) -> Path:
    return _write_json_atomic(app_core._task_context_store_path(), items)


def _save_risk_registers(items: list[dict]) -> Path:
    return _write_json_atomic(app_core._risk_register_store_path(), items)


def _save_validation_snapshots(items: list[dict]) -> Path:
    return _write_json_atomic(app_core._validation_snapshot_store_path(), items)


def _load_entity_catalogs() -> list[dict]:
    path = _entity_catalog_store_path()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _save_entity_catalogs(items: list[dict]) -> Path:
    return _write_json_atomic(_entity_catalog_store_path(), items)


def _save_policy_profile(profile: dict) -> Path:
    return _write_json_atomic(app_core._policy_profile_store_path(), profile)


@_serialized_link_mutation
def _create_memory_link(request: MemoryLinkRequest) -> dict:
    project = rt._canonical_project(request.project)
    if request.source_id == request.target_id:
        raise HTTPException(status_code=400, detail="source_id and target_id must differ")
    if rt._find_live_memory(request.source_id, project) is None:
        raise HTTPException(status_code=404, detail="source memory not found")
    if rt._find_live_memory(request.target_id, project) is None:
        raise HTTPException(status_code=404, detail="target memory not found")

    metadata = app_core._metadata_to_dict(request.metadata)
    schema = _active_ontology_schema(project)
    if schema is not None:
        schema_digest = schema.digest()
        if request.ontology_schema_digest is not None and request.ontology_schema_digest != schema_digest:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ontology_schema_digest_mismatch",
                    "expected_schema_digest": schema_digest,
                },
            )
        receipt = admit_relation_write(
            schema,
            OntologyRelationWrite(
                project=project,
                schema_digest=schema_digest,
                relation=request.relation,
                source_id=request.source_id,
                source_type="memory",
                target_id=request.target_id,
                target_type="memory",
            ),
        )
        if receipt.decision != "allow":
            quarantine = _append_ontology_quarantine(receipt)
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ontology_link_quarantined",
                    "reason_code": receipt.reason_code,
                    "schema_digest": receipt.schema_digest,
                    "relation": receipt.relation,
                    "quarantine": quarantine,
                },
            )
        metadata["ontology"] = {
            "schema_digest": receipt.schema_digest,
            "revision": schema.revision,
            "relation": receipt.relation,
            "source_type": receipt.source_type,
            "target_type": receipt.target_type,
            "admission": "allow",
        }

    links = rt._load_memory_links()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for item in links:
        if (
            item.get("project") == project
            and item.get("source_id") == request.source_id
            and item.get("target_id") == request.target_id
            and item.get("relation") == request.relation
        ):
            item["updated_at"] = now
            item["metadata"] = metadata or item.get("metadata") or {}
            rt._save_memory_links(links)
            return item

    link = {
        "id": f"link_bhm_{uuid.uuid4().hex[:16]}",
        "project": project,
        "source_id": request.source_id,
        "target_id": request.target_id,
        "relation": request.relation,
        "created_at": now,
        "updated_at": now,
        "metadata": metadata,
    }
    links.append(link)
    rt._save_memory_links(links)
    return link


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
            memory_class=app_core.MemoryClass.EPISODIC,
            event_role=app_core.MemoryEventRole.TRACE,
            content=content,
            concepts=concepts,
            files=files,
            metadata={"artifact_kind": "checkpoint"},
        )
    )

    checkpoints = app_core._load_checkpoints()
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


def _memory_content_sha256(record: dict) -> str:
    metadata = record.get("metadata") or {}
    return str(metadata.get("content_sha256") or hashlib.sha256(str(record.get("content") or "").encode("utf-8")).hexdigest())


def _review_status(record: dict) -> str:
    value = str((record.get("metadata") or {}).get("review_status") or "open").strip().lower()
    return value if value in {"open", "needs_review", "resolved", "dismissed"} else "open"


def _combined_review_status(records: list[dict]) -> str:
    statuses = {_review_status(record) for record in records if record}
    if "needs_review" in statuses:
        return "needs_review"
    if "open" in statuses or not statuses:
        return "open"
    if statuses == {"resolved"}:
        return "resolved"
    if statuses == {"dismissed"}:
        return "dismissed"
    return "open"


def _review_queue_id(kind: str, project: str | None, memory_ids: list[str], fingerprint: str = "") -> str:
    payload = {
        "kind": kind,
        "project": rt._canonical_project(project) if project else None,
        "memory_ids": sorted(str(item) for item in memory_ids),
        "fingerprint": fingerprint,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"review_{digest[:24]}"


def _detect_conflicts(request: MemoryDetectRequest) -> list[dict]:
    records = [
        item for item in rt._load_live_memories()
        if app_core._memory_matches_filters(item, project=request.project, include_archived=request.include_archived)
    ]
    groups: dict[tuple[str, str], list[dict]] = {}
    for item in records:
        groups.setdefault((str(item.get("project") or ""), str(item.get("memory_type") or "")), []).append(item)

    candidates: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    for (project, _memory_type), group in sorted(groups.items()):
        ordered = sorted(group, key=lambda item: str(item.get("source_id") or ""))
        title_buckets: dict[str, list[dict]] = {}
        prefix_tag_buckets: dict[tuple[str, str], list[dict]] = {}
        for item in ordered:
            metadata = item.get("metadata") or {}
            title = str(metadata.get("raw_title") or "").strip().lower()
            text = app_core._normalized_text(item.get("content"))
            if title:
                title_buckets.setdefault(title, []).append(item)
            prefix = text[:80]
            if prefix:
                for tag in set(str(value) for value in (item.get("tags") or []) if str(value).strip()):
                    prefix_tag_buckets.setdefault((prefix, tag), []).append(item)

        candidate_pairs: set[tuple[str, str]] = set()
        for bucket in title_buckets.values():
            for left_index, left in enumerate(bucket):
                for right in bucket[left_index + 1:]:
                    candidate_pairs.add(tuple(sorted((str(left.get("source_id")), str(right.get("source_id"))))))
        for bucket in prefix_tag_buckets.values():
            for left_index, left in enumerate(bucket):
                for right in bucket[left_index + 1:]:
                    candidate_pairs.add(tuple(sorted((str(left.get("source_id")), str(right.get("source_id"))))))

        by_id = {str(item.get("source_id")): item for item in ordered}
        for left_id, right_id in sorted(candidate_pairs):
            if (left_id, right_id) in seen_pairs:
                continue
            seen_pairs.add((left_id, right_id))
            left = by_id[left_id]
            right = by_id[right_id]
            left_metadata = left.get("metadata") or {}
            right_metadata = right.get("metadata") or {}
            left_title = str(left_metadata.get("raw_title") or "").strip()
            right_title = str(right_metadata.get("raw_title") or "").strip()
            left_text = app_core._normalized_text(left.get("content"))
            right_text = app_core._normalized_text(right.get("content"))
            shared_tags = sorted(set(str(value) for value in (left.get("tags") or [])) & set(str(value) for value in (right.get("tags") or [])))
            same_title = bool(left_title and right_title and left_title.lower() == right_title.lower())
            same_prefix = bool(left_text[:80] and left_text[:80] == right_text[:80])
            materially_different = bool(left_text and right_text and left_text != right_text)

            score = 0.0
            reason = ""
            if same_title and materially_different:
                score = 0.9
                reason = "same_title_different_content"
            elif shared_tags and materially_different and same_prefix:
                score = 0.82
                reason = "shared_tags_divergent_content"
            if score <= 0:
                continue
            fingerprint = "|".join(
                [
                    reason,
                    _memory_content_sha256(left),
                    _memory_content_sha256(right),
                    ",".join(shared_tags),
                ]
            )
            candidates.append(
                {
                    "queue_id": _review_queue_id("contradiction", project, [left_id, right_id], fingerprint),
                    "left_id": left_id,
                    "right_id": right_id,
                    "project": project,
                    "score": score,
                    "reason": reason,
                    "left_title": left_title or app_core._build_memory_title(left.get("content") or ""),
                    "right_title": right_title or app_core._build_memory_title(right.get("content") or ""),
                    "shared_tags": shared_tags,
                    "left_revision_id": left_metadata.get("revision_id"),
                    "right_revision_id": right_metadata.get("revision_id"),
                    "left_content_sha256": _memory_content_sha256(left),
                    "right_content_sha256": _memory_content_sha256(right),
                }
            )
    candidates.sort(key=lambda item: (item.get("score") or 0.0, item.get("queue_id") or ""), reverse=True)
    return candidates[: max(min(request.limit, 200), 1)]


def _lint_memory_record(record: dict) -> list[dict]:
    issues: list[dict] = []
    content = record.get("content") or ""
    metadata = record.get("metadata") or {}
    if len(content.strip()) < 20:
        issues.append({"severity": "warning", "code": "too_short", "message": "memory content is very short"})
    if len(content) > 4000:
        issues.append({"severity": "warning", "code": "too_long", "message": "memory content is unusually long"})
    if not record.get("project"):
        issues.append({"severity": "error", "code": "missing_project", "message": "memory has no project scope"})
    if not record.get("memory_type"):
        issues.append({"severity": "error", "code": "missing_type", "message": "memory has no type"})
    if not (record.get("tags") or []):
        issues.append({"severity": "warning", "code": "missing_concepts", "message": "memory has no concepts/tags"})
    if not (metadata.get("files") or []):
        issues.append({"severity": "info", "code": "missing_files", "message": "memory has no linked files"})
    if contains_secret_like(content):
        issues.append({"severity": "error", "code": "possible_secret", "message": "memory may contain a secret-like token"})
    if content.count("\n") > 80:
        issues.append({"severity": "warning", "code": "log_like_blob", "message": "memory looks more like a raw log than compact durable knowledge"})
    return issues


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


def _adr_supersede(project: str, old_id: str, new_id: str) -> dict:
    adrs = _load_adrs()
    old_record = next((item for item in adrs if item.get("id") == old_id and item.get("project") == project), None)
    new_record = next((item for item in adrs if item.get("id") == new_id and item.get("project") == project), None)
    if old_record is None:
        raise HTTPException(status_code=404, detail="old ADR not found")
    if new_record is None:
        raise HTTPException(status_code=404, detail="new ADR not found")
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    old_record["status"] = "superseded"
    old_record["updated_at"] = now
    old_record.setdefault("metadata", {})["superseded_by"] = new_id
    new_record.setdefault("metadata", {})["supersedes"] = old_id
    new_record["updated_at"] = now
    _save_adrs(adrs)
    if old_record.get("memory_id") and new_record.get("memory_id"):
        _create_memory_link(
            MemoryLinkRequest(
                source_id=new_record["memory_id"],
                target_id=old_record["memory_id"],
                relation="supersedes",
                project=project,
                metadata={"artifact": "adr"},
            )
        )
    return {"old": old_record, "new": new_record}


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
    items = _load_adrs()
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
            _save_adrs(items)
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
    _save_adrs(items)
    return action, record


def _list_adrs(project: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
    items = _load_adrs()
    if project:
        items = [item for item in items if item.get("project") == project]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    start = max(offset, 0)
    end = start + max(min(limit, 200), 1)
    return items[start:end], total


def _create_handoff(request: HandoffCreateRequest) -> tuple[str, dict]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    title = request.title.strip()
    concepts = list(dict.fromkeys((request.concepts or []) + ["handoff", "bhm"]))
    files = request.files or []
    upsert_key = request.upsert_key or f"handoff:{request.project}:{title.lower()}"
    content = "\n".join(
        [
            title,
            f"current_state: {request.current_state}",
            f"decisions: {request.decisions}",
            f"validation: {request.validation}",
            f"next_agent_action: {request.next_agent_action}",
            f"next_owner_id: {request.next_owner_id}",
            f"handoff_sla_deadline: {request.handoff_sla_deadline}",
        ]
    )
    action, memory_record = app_core._upsert_live_memory(
        MemoryUpsertRequest(
            upsert_key=upsert_key,
            project=request.project,
            type="workflow",
            content=content,
            concepts=concepts,
            files=files,
        )
    )
    items = _load_handoffs()
    for item in items:
        if item.get("project") == request.project and (item.get("metadata") or {}).get("upsert_key") == upsert_key:
            item.update(
                {
                    "title": title,
                    "current_state": request.current_state,
                    "decisions": request.decisions,
                    "validation": request.validation,
                    "next_agent_action": request.next_agent_action,
                    "next_owner_id": request.next_owner_id,
                    "handoff_sla_deadline": request.handoff_sla_deadline,
                    "files": files,
                    "concepts": concepts,
                    "memory_id": memory_record.get("source_id"),
                    "updated_at": now,
                }
            )
            _save_handoffs(items)
            return action, item
    record = {
        "id": f"handoff_bhm_{uuid.uuid4().hex[:16]}",
        "project": request.project,
        "title": title,
        "current_state": request.current_state,
        "decisions": request.decisions,
        "validation": request.validation,
        "next_agent_action": request.next_agent_action,
        "next_owner_id": request.next_owner_id,
        "handoff_sla_deadline": request.handoff_sla_deadline,
        "files": files,
        "concepts": concepts,
        "memory_id": memory_record.get("source_id"),
        "created_at": now,
        "updated_at": now,
        "metadata": {"upsert_key": upsert_key},
    }
    items.append(record)
    _save_handoffs(items)
    return action, record


def _list_handoffs(project: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
    items = _load_handoffs()
    if project:
        items = [item for item in items if item.get("project") == project]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    start = max(offset, 0)
    end = start + max(min(limit, 200), 1)
    return items[start:end], total


def _session_upsert_key(request: SessionRecordCreateRequest, project: str, title: str) -> str:
    if request.upsert_key and request.upsert_key.strip():
        return request.upsert_key.strip()
    title_slug = re.sub(r"[^a-z0-9._-]+", "-", title.strip().lower()).strip("-")
    return f"session-record:{project}:{title_slug or 'session'}"


def _create_session_record(request: SessionRecordCreateRequest) -> tuple[str, dict]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    title = request.title.strip()
    files_touched = request.files_touched or []
    concepts = ["hybrid-session", "checkpoint", "conversation", "bhm"]
    project = rt._canonical_project(request.project)
    upsert_key = _session_upsert_key(request, project, title)
    content = "\n".join(
        [
            f"{request.project} hybrid session record:",
            f"title: {title}",
            f"done: {request.done}",
            f"next: {request.next}",
            f"checks: {request.checks}",
            f"risks/notes: {request.risks}",
            f"decisions: {request.decisions}",
            f"files_touched: {', '.join(files_touched)}",
            f"conversation_notes: {request.conversation_notes}",
            f"transcript_ref: {request.transcript_ref}",
        ]
    )
    action, memory_record = app_core._upsert_live_memory(
        MemoryUpsertRequest(
            upsert_key=upsert_key,
            project=project,
            type="workflow",
            memory_class=app_core.MemoryClass.EPISODIC,
            event_role=app_core.MemoryEventRole.TRACE,
            content=content,
            concepts=concepts,
            files=files_touched,
            metadata={"artifact_kind": "session-record"},
        )
    )
    items = app_core._load_session_records()
    for item in items:
        if item.get("project") in rt._project_aliases(project) and (item.get("metadata") or {}).get("upsert_key") == upsert_key:
            item.update(
                {
                    "title": title,
                    "done": request.done,
                    "next": request.next,
                    "checks": request.checks,
                    "risks": request.risks,
                    "decisions": request.decisions,
                    "files_touched": files_touched,
                    "conversation_notes": request.conversation_notes,
                    "transcript_ref": request.transcript_ref,
                    "memory_id": memory_record.get("source_id"),
                    "updated_at": now,
                }
            )
            item.setdefault("metadata", {})["artifact_kind"] = "session-record"
            _save_session_records(items)
            return action, item
    record = {
        "id": f"session_bhm_{uuid.uuid4().hex[:16]}",
        "project": project,
        "title": title,
        "done": request.done,
        "next": request.next,
        "checks": request.checks,
        "risks": request.risks,
        "decisions": request.decisions,
        "files_touched": files_touched,
        "conversation_notes": request.conversation_notes,
        "transcript_ref": request.transcript_ref,
        "memory_id": memory_record.get("source_id"),
        "created_at": now,
        "updated_at": now,
        "metadata": {"upsert_key": upsert_key, "artifact_kind": "session-record"},
    }
    items.append(record)
    _save_session_records(items)
    return action, record


def _list_session_records(project: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
    items = app_core._load_session_records()
    if project:
        items = [item for item in items if item.get("project") == project]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    start = max(offset, 0)
    end = start + max(min(limit, 200), 1)
    return items[start:end], total


def _open_task_unlocked(request: TaskOpenRequest) -> tuple[str, dict]:
    project = rt._canonical_project(request.project)
    task_id = request.task_id.strip()
    intent = request.intent.strip()
    title = request.title.strip() or f"Task {task_id}"
    upsert_key = request.upsert_key or f"task:{project}:{task_id}"
    session_upsert_key = f"session-record:task:{project}:{task_id}"
    tasks = rt._load_tasks()
    existing = next(
        (
            item
            for item in tasks
            if item.get("project") == project
            and (
                item.get("task_id") == task_id
                or (item.get("metadata") or {}).get("upsert_key") == upsert_key
            )
        ),
        None,
    )

    requested_fields = {
        "project": project,
        "task_id": task_id,
        "title": title,
        "intent": intent,
        "scope_in": list(dict.fromkeys(request.scope_in)),
        "scope_out": list(dict.fromkeys(request.scope_out)),
        "repo": request.repo.strip(),
        "owner": request.owner.strip(),
        "session_id": request.session_id.strip(),
        "correlation_id": request.correlation_id.strip(),
        "files_touched": list(dict.fromkeys(request.files_touched)),
        "status": "open",
    }
    if existing is not None and all(
        existing.get(field) == value for field, value in requested_fields.items()
    ) and existing.get("session_record_id") and existing.get("memory_id"):
        return "already_open", existing

    now = rt._utc_now_iso()
    previous = existing or {}
    session_id = requested_fields["session_id"] or str(previous.get("session_id") or "")
    correlation_id = requested_fields["correlation_id"] or str(previous.get("correlation_id") or "")
    session_title = title
    session_notes = "\n".join(
        [
            f"task_id: {task_id}",
            f"intent: {intent}",
            f"scope_in: {', '.join(requested_fields['scope_in'])}",
            f"scope_out: {', '.join(requested_fields['scope_out'])}",
            f"repo: {requested_fields['repo']}",
            f"owner: {requested_fields['owner']}",
            f"session_id: {session_id}",
            f"correlation_id: {correlation_id}",
        ]
    )
    session_action, session_record = _create_session_record(
        SessionRecordCreateRequest(
            project=project,
            title=session_title,
            next=intent,
            checks="task_open",
            decisions=f"task_id: {task_id}",
            files_touched=requested_fields["files_touched"],
            conversation_notes=session_notes,
            transcript_ref=session_id,
            upsert_key=session_upsert_key,
        )
    )
    attributes = dict((previous.get("metadata") or {}).get("attributes") or {})
    attributes.update(request.metadata)
    record = {
        "id": previous.get("id") or f"task_bhm_{uuid.uuid4().hex[:16]}",
        **requested_fields,
        "session_id": session_id,
        "correlation_id": correlation_id,
        "session_record_id": session_record.get("id"),
        "memory_id": session_record.get("memory_id"),
        "opened_at": previous.get("opened_at") or now,
        "created_at": previous.get("created_at") or now,
        "updated_at": now,
        "metadata": {
            "upsert_key": upsert_key,
            "session_upsert_key": session_upsert_key,
            "attributes": attributes,
            "session_action": session_action,
        },
    }
    if existing is None:
        tasks.append(record)
        action = "created"
    else:
        index = tasks.index(existing)
        tasks[index] = record
        action = "reopened" if previous.get("status") == "closed" else "updated"
    rt._save_tasks(tasks)
    return action, record


def _task_ingress_status_unlocked() -> dict[str, Any]:
    """Return only bounded in-process maintenance state; nothing is persisted."""

    safe_reason = redact_secret_text(_TASK_INGRESS_REASON).value.strip()[:240]
    return {
        "frozen": _TASK_INGRESS_FROZEN,
        "reason": safe_reason if _TASK_INGRESS_FROZEN else "",
        "changed_at": _TASK_INGRESS_CHANGED_AT,
    }


def _task_ingress_status() -> dict[str, Any]:
    with _TASK_LIFECYCLE_LOCK:
        return _task_ingress_status_unlocked()


def _normalize_task_ingress_reason(reason: str) -> str:
    normalized = " ".join(reason.split())
    normalized = redact_secret_text(normalized).value.strip()
    if not normalized:
        raise HTTPException(status_code=422, detail={"code": "task_ingress_reason_required"})
    if len(normalized) > 240:
        raise HTTPException(status_code=422, detail={"code": "task_ingress_reason_too_long"})
    return normalized


def _freeze_task_ingress(request: TaskIngressControlRequest) -> tuple[str, dict[str, Any]]:
    """Freeze only task open/close writers after any active lifecycle completes."""

    global _TASK_INGRESS_FROZEN, _TASK_INGRESS_REASON, _TASK_INGRESS_CHANGED_AT
    reason = _normalize_task_ingress_reason(request.reason)
    with _TASK_LIFECYCLE_LOCK:
        if _TASK_INGRESS_FROZEN:
            return "already_frozen", _task_ingress_status_unlocked()
        _TASK_INGRESS_FROZEN = True
        _TASK_INGRESS_REASON = reason
        _TASK_INGRESS_CHANGED_AT = rt._utc_now_iso()
        return "frozen", _task_ingress_status_unlocked()


def _resume_task_ingress(request: TaskIngressControlRequest) -> tuple[str, dict[str, Any]]:
    """Reopen only task open/close writers; normal memory ingress is unaffected."""

    global _TASK_INGRESS_FROZEN, _TASK_INGRESS_REASON, _TASK_INGRESS_CHANGED_AT
    _normalize_task_ingress_reason(request.reason)
    with _TASK_LIFECYCLE_LOCK:
        if not _TASK_INGRESS_FROZEN:
            return "already_open", _task_ingress_status_unlocked()
        _TASK_INGRESS_FROZEN = False
        _TASK_INGRESS_REASON = ""
        _TASK_INGRESS_CHANGED_AT = rt._utc_now_iso()
        return "opened", _task_ingress_status_unlocked()


def _raise_if_task_ingress_frozen() -> None:
    if _TASK_INGRESS_FROZEN:
        raise HTTPException(status_code=503, detail={"code": "task_ingress_frozen"})


def _open_task(request: TaskOpenRequest) -> tuple[str, dict]:
    with _TASK_LIFECYCLE_LOCK:
        _raise_if_task_ingress_frozen()
        return _open_task_unlocked(request)


def _close_task_unlocked(request: TaskCloseRequest) -> tuple[str, dict]:
    project = rt._canonical_project(request.project)
    task_id = request.task_id.strip()
    tasks = rt._load_tasks()
    index = next(
        (
            position
            for position, item in enumerate(tasks)
            if item.get("project") == project and item.get("task_id") == task_id
        ),
        None,
    )
    if index is None:
        raise HTTPException(status_code=404, detail="task not found")

    existing = tasks[index]
    files_touched = list(
        dict.fromkeys(
            request.files_touched
            if request.files_touched is not None
            else list(existing.get("files_touched") or [])
        )
    )
    requested_fields = {
        "done": request.done,
        "next": request.next,
        "checks": request.checks,
        "risks": request.risks,
        "decisions": request.decisions,
        "validation": request.validation,
        "files_touched": files_touched,
        "conversation_notes": request.conversation_notes,
        "transcript_ref": request.transcript_ref or str(existing.get("session_id") or ""),
    }
    if existing.get("status") == "closed" and all(
        existing.get(field) == value for field, value in requested_fields.items()
    ):
        return "already_closed", existing

    session_upsert_key = str(
        (existing.get("metadata") or {}).get("session_upsert_key")
        or f"session-record:task:{project}:{task_id}"
    )
    session_notes = "\n".join(
        [
            f"task_id: {task_id}",
            f"intent: {existing.get('intent') or ''}",
            f"validation: {request.validation}",
            request.conversation_notes,
        ]
    )
    session_action, session_record = _create_session_record(
        SessionRecordCreateRequest(
            project=project,
            title=str(existing.get("title") or f"Task {task_id}"),
            done=request.done,
            next=request.next,
            checks=request.checks,
            risks=request.risks,
            decisions=request.decisions,
            files_touched=files_touched,
            conversation_notes=session_notes,
            transcript_ref=requested_fields["transcript_ref"],
            upsert_key=session_upsert_key,
        )
    )

    now = rt._utc_now_iso()
    metadata = dict(existing.get("metadata") or {})
    attributes = dict(metadata.get("attributes") or {})
    attributes.update(request.metadata)
    metadata["attributes"] = attributes
    metadata["close_action"] = session_action
    record = {
        **existing,
        **requested_fields,
        "status": "closed",
        "closed_at": now,
        "updated_at": now,
        "session_record_id": session_record.get("id"),
        "memory_id": session_record.get("memory_id"),
        "metadata": metadata,
    }
    tasks[index] = record
    rt._save_tasks(tasks)
    return ("reclosed" if existing.get("status") == "closed" else "closed"), record


def _close_task(request: TaskCloseRequest) -> tuple[str, dict]:
    with _TASK_LIFECYCLE_LOCK:
        _raise_if_task_ingress_frozen()
        return _close_task_unlocked(request)


def _get_task(task_id: str, project: str | None = None) -> dict:
    project_name = rt._canonical_project(project) if project else None
    for item in rt._load_tasks():
        if item.get("task_id") != task_id:
            continue
        if project_name and item.get("project") != project_name:
            continue
        return item
    raise HTTPException(status_code=404, detail="task not found")


def _upsert_task_context(request: TaskContextUpdateRequest) -> tuple[str, dict]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    upsert_key = request.upsert_key or f"task-context:{request.project}"
    files_touched = request.files_touched or []
    content = "\n".join(
        [
            f"{request.project} task context:",
            f"title: {request.title}",
            f"current_task: {request.current_task}",
            f"status: {request.status}",
            f"pending_items: {request.pending_items}",
            f"guidance: {request.guidance}",
            f"next_step: {request.next_step}",
            f"files_touched: {', '.join(files_touched)}",
        ]
    )
    action, memory_record = app_core._upsert_live_memory(
        MemoryUpsertRequest(
            upsert_key=upsert_key,
            project=request.project,
            type="workflow",
            content=content,
            concepts=["task-context", "bhm"],
            files=files_touched,
        )
    )
    items = app_core._load_task_contexts()
    for item in items:
        if item.get("project") == request.project and (item.get("metadata") or {}).get("upsert_key") == upsert_key:
            item.update(
                {
                    "title": request.title,
                    "current_task": request.current_task,
                    "status": request.status,
                    "pending_items": request.pending_items,
                    "guidance": request.guidance,
                    "next_step": request.next_step,
                    "files_touched": files_touched,
                    "memory_id": memory_record.get("source_id"),
                    "updated_at": now,
                }
            )
            _save_task_contexts(items)
            return action, item
    record = {
        "id": f"task_context_bhm_{uuid.uuid4().hex[:16]}",
        "project": request.project,
        "title": request.title,
        "current_task": request.current_task,
        "status": request.status,
        "pending_items": request.pending_items,
        "guidance": request.guidance,
        "next_step": request.next_step,
        "files_touched": files_touched,
        "memory_id": memory_record.get("source_id"),
        "created_at": now,
        "updated_at": now,
        "metadata": {"upsert_key": upsert_key},
    }
    items.append(record)
    _save_task_contexts(items)
    return action, record


def _upsert_risk_register(request: RiskRegisterUpdateRequest) -> tuple[str, dict]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    upsert_key = request.upsert_key or f"risk-register:{request.project}"
    top_risks = request.top_risks or []
    mitigations = request.mitigations or []
    content = "\n".join(
        [
            f"{request.project} risk register:",
            f"title: {request.title}",
            f"summary: {request.summary}",
            "top_risks:",
            *[f"- {item}" for item in top_risks],
            "mitigations:",
            *[f"- {item}" for item in mitigations],
            f"owner: {request.owner}",
        ]
    )
    action, memory_record = app_core._upsert_live_memory(
        MemoryUpsertRequest(
            upsert_key=upsert_key,
            project=request.project,
            type="workflow",
            content=content,
            concepts=["risk-register", "bhm"],
            files=[],
        )
    )
    items = app_core._load_risk_registers()
    for item in items:
        if item.get("project") == request.project and (item.get("metadata") or {}).get("upsert_key") == upsert_key:
            item.update(
                {
                    "title": request.title,
                    "summary": request.summary,
                    "top_risks": top_risks,
                    "mitigations": mitigations,
                    "owner": request.owner,
                    "memory_id": memory_record.get("source_id"),
                    "updated_at": now,
                }
            )
            _save_risk_registers(items)
            return action, item
    record = {
        "id": f"risk_bhm_{uuid.uuid4().hex[:16]}",
        "project": request.project,
        "title": request.title,
        "summary": request.summary,
        "top_risks": top_risks,
        "mitigations": mitigations,
        "owner": request.owner,
        "memory_id": memory_record.get("source_id"),
        "created_at": now,
        "updated_at": now,
        "metadata": {"upsert_key": upsert_key},
    }
    items.append(record)
    _save_risk_registers(items)
    return action, record


def _save_validation_snapshot_record(request: ValidationSnapshotSaveRequest) -> tuple[str, dict]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    upsert_key = request.upsert_key or f"validation-snapshot:{request.project}"
    content = "\n".join(
        [
            f"{request.project} validation snapshot:",
            f"title: {request.title}",
            f"lint: {request.lint}",
            f"tests: {request.tests}",
            f"smoke: {request.smoke}",
            f"docs: {request.docs}",
            f"overall_status: {request.overall_status}",
            f"command_summary: {request.command_summary}",
        ]
    )
    action, memory_record = app_core._upsert_live_memory(
        MemoryUpsertRequest(
            upsert_key=upsert_key,
            project=request.project,
            type="workflow",
            content=content,
            concepts=["validation-snapshot", "bhm"],
            files=[],
        )
    )
    items = app_core._load_validation_snapshots()
    for item in items:
        if item.get("project") == request.project and (item.get("metadata") or {}).get("upsert_key") == upsert_key:
            item.update(
                {
                    "title": request.title,
                    "lint": request.lint,
                    "tests": request.tests,
                    "smoke": request.smoke,
                    "docs": request.docs,
                    "overall_status": request.overall_status,
                    "command_summary": request.command_summary,
                    "memory_id": memory_record.get("source_id"),
                    "updated_at": now,
                }
            )
            _save_validation_snapshots(items)
            return action, item
    record = {
        "id": f"validation_bhm_{uuid.uuid4().hex[:16]}",
        "project": request.project,
        "title": request.title,
        "lint": request.lint,
        "tests": request.tests,
        "smoke": request.smoke,
        "docs": request.docs,
        "overall_status": request.overall_status,
        "command_summary": request.command_summary,
        "memory_id": memory_record.get("source_id"),
        "created_at": now,
        "updated_at": now,
        "metadata": {"upsert_key": upsert_key},
    }
    items.append(record)
    _save_validation_snapshots(items)
    return action, record


def _build_reflection(project: str, max_clusters: int = 10) -> dict:
    concept_counts: dict[str, int] = {}
    memory_type_counts: dict[str, int] = {}
    for item in rt._load_live_memories():
        if not app_core._memory_matches_filters(item, project=project, include_archived=False):
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


def _integrity_audit(project: str | None = None) -> dict:
    canonical_project = rt._canonical_project(project) if project else None
    memories = [
        item for item in rt._load_live_memories()
        if app_core._memory_matches_filters(item, project=canonical_project, include_archived=True)
    ]
    memory_ids = {item.get("source_id") for item in memories}
    orphan_links = [
        item for item in rt._load_memory_links()
        if (not canonical_project or app_core._project_matches(item.get("project"), canonical_project))
        and (item.get("source_id") not in memory_ids or item.get("target_id") not in memory_ids)
    ]
    duplicate_upsert_keys = []
    seen: dict[tuple[str, str], str] = {}
    for item in memories:
        upsert_key = (item.get("metadata") or {}).get("upsert_key")
        if not upsert_key:
            continue
        key = (rt._canonical_project(item.get("project")) if item.get("project") else "", upsert_key)
        if key in seen:
            duplicate_upsert_keys.append({"project": key[0], "upsert_key": upsert_key, "ids": [seen[key], item.get("source_id")]})
        else:
            seen[key] = item.get("source_id")
    archived_but_pinned = [
        item.get("source_id") for item in memories
        if app_core._is_archived_memory(item) and bool((item.get("metadata") or {}).get("pinned"))
    ]
    return {
        "project": canonical_project,
        "orphan_links": [_serialize_memory_link(item) for item in orphan_links],
        "duplicate_upsert_keys": duplicate_upsert_keys,
        "archived_but_pinned": archived_but_pinned,
        "ok": len(orphan_links) == 0 and len(duplicate_upsert_keys) == 0 and len(archived_but_pinned) == 0,
    }


@_serialized_link_mutation
def _repair_live_indexes(request: RepairLiveIndexesRequest) -> dict:
    if not request.project and not request.aggregate:
        raise HTTPException(
            status_code=403,
            detail={"code": "repair_scope_required", "message": "project or aggregate=true is required"},
        )
    project = rt._canonical_project(request.project) if request.project else None
    report_before = _integrity_audit(project)
    removed_links = 0
    removed_artifacts = 0
    if request.remove_orphan_links:
        memories = rt._load_live_memories()
        memory_ids = {
            item.get("source_id")
            for item in memories
            if app_core._project_matches(item.get("project"), project)
        }
        links = rt._load_memory_links()
        filtered = []
        for item in links:
            if project and not app_core._project_matches(item.get("project"), project):
                filtered.append(item)
                continue
            if item.get("source_id") in memory_ids and item.get("target_id") in memory_ids:
                filtered.append(item)
        removed_links = len(links) - len(filtered)
        rt._save_memory_links(filtered)
    if request.remove_orphan_artifacts:
        memory_ids = {
            item.get("source_id")
            for item in rt._load_live_memories()
            if app_core._project_matches(item.get("project"), project)
        }
        for loader, saver in (
            (app_core._load_checkpoints, _save_checkpoints),
            (app_core._load_project_maps, app_core._save_project_maps),
            (_load_adrs, _save_adrs),
            (_load_handoffs, _save_handoffs),
            (app_core._load_session_records, _save_session_records),
            (app_core._load_task_contexts, _save_task_contexts),
            (app_core._load_risk_registers, _save_risk_registers),
            (app_core._load_validation_snapshots, _save_validation_snapshots),
        ):
            items = loader()
            filtered = []
            for item in items:
                if project and not app_core._project_matches(item.get("project"), project):
                    filtered.append(item)
                    continue
                if not item.get("memory_id") or item.get("memory_id") in memory_ids:
                    filtered.append(item)
            removed_artifacts += len(items) - len(filtered)
            saver(filtered)
    report_after = _integrity_audit(project)
    return {
        "project": project,
        "aggregate": bool(request.aggregate and not project),
        "before": report_before,
        "after": report_after,
        "removed_links": removed_links,
        "removed_artifacts": removed_artifacts,
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


def _relation_suggest(request: RelationSuggestRequest) -> dict:
    project = rt._canonical_project(request.project) if request.project else None
    duplicates = app_core._detect_duplicates(MemoryDetectRequest(project=project, limit=request.limit, include_archived=False))
    conflicts = _detect_conflicts(MemoryDetectRequest(project=project, limit=request.limit, include_archived=False))
    suggestions = []
    for item in duplicates:
        suggestions.append({"relation": "duplicate_of", "score": item["score"], "source_id": item["left_id"], "target_id": item["right_id"], "reason": item["reason"]})
    for item in conflicts:
        suggestions.append({"relation": "conflicts_with", "score": item["score"], "source_id": item["left_id"], "target_id": item["right_id"], "reason": item["reason"]})
    records = [
        item for item in rt._load_live_memories()
        if app_core._memory_matches_filters(item, project=project, include_archived=False)
    ]
    for left_index, left in enumerate(records):
        for right in records[left_index + 1:]:
            # Aggregate suggestions are still tenant-safe: shared files or
            # tags must never create a cross-project relation candidate.
            if rt._canonical_project(left.get("project")) != rt._canonical_project(right.get("project")):
                continue
            left_files = set((left.get("metadata") or {}).get("files") or [])
            right_files = set((right.get("metadata") or {}).get("files") or [])
            shared_files = sorted(left_files & right_files)
            left_tags = set(left.get("tags") or [])
            right_tags = set(right.get("tags") or [])
            shared_tags = sorted(left_tags & right_tags)
            suggestion_project = rt._canonical_project(left.get("project"))
            if shared_files:
                suggestions.append(
                    {
                        "relation": "relates_to",
                        "score": 0.7,
                        "source_id": left.get("source_id"),
                        "target_id": right.get("source_id"),
                        "project": suggestion_project,
                        "reason": f"shared_files:{', '.join(shared_files[:3])}",
                    }
                )
            elif shared_tags:
                suggestions.append(
                    {
                        "relation": "relates_to",
                        "score": 0.6,
                        "source_id": left.get("source_id"),
                        "target_id": right.get("source_id"),
                        "project": suggestion_project,
                        "reason": f"shared_concepts:{', '.join(shared_tags[:3])}",
                    }
                )
    suggestions.sort(key=lambda item: item["score"], reverse=True)
    return {"project": project, "suggestions": suggestions[: max(min(request.limit, 200), 1)]}


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


def _artifact_integrity_audit(project: str | None = None) -> dict:
    project = rt._canonical_project(project) if project else None
    memory_ids = {item.get("source_id") for item in rt._load_live_memories() if app_core._project_matches(item.get("project"), project)}
    artifacts = {
        "checkpoints": [item for item in app_core._load_checkpoints() if app_core._project_matches(item.get("project"), project)],
        "project_maps": [item for item in app_core._load_project_maps() if app_core._project_matches(item.get("project"), project)],
        "adrs": [item for item in _load_adrs() if app_core._project_matches(item.get("project"), project)],
        "handoffs": [item for item in _load_handoffs() if app_core._project_matches(item.get("project"), project)],
        "session_records": [item for item in app_core._load_session_records() if app_core._project_matches(item.get("project"), project)],
        "tasks": [item for item in rt._load_tasks() if app_core._project_matches(item.get("project"), project)],
        "task_contexts": [item for item in app_core._load_task_contexts() if app_core._project_matches(item.get("project"), project)],
        "risk_registers": [item for item in app_core._load_risk_registers() if app_core._project_matches(item.get("project"), project)],
        "validation_snapshots": [item for item in app_core._load_validation_snapshots() if app_core._project_matches(item.get("project"), project)],
    }
    orphans = {}
    for name, items in artifacts.items():
        missing = [item.get("id") for item in items if item.get("memory_id") and item.get("memory_id") not in memory_ids]
        if missing:
            orphans[name] = missing
    return {"project": project, "orphans": orphans, "ok": len(orphans) == 0}


def _link_graph_stats(project: str | None = None) -> dict:
    project = rt._canonical_project(project) if project else None
    links = [item for item in rt._load_memory_links() if app_core._project_matches(item.get("project"), project)]
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
        if not app_core._project_matches(item.get("project"), project):
            continue
        metadata = item.setdefault("metadata", {})
        content = item.get("content") or ""
        raw_title = app_core._build_memory_title(content)
        if metadata.get("raw_title") != raw_title:
            metadata["raw_title"] = raw_title
            updated += 1
    app_core._save_live_memories(live_records)
    return {"project": project, "updated": updated}


def _artifact_store_pairs() -> dict[str, tuple[callable, callable]]:
    return {
        "checkpoint": (app_core._load_checkpoints, _save_checkpoints),
        "project_map": (app_core._load_project_maps, app_core._save_project_maps),
        "adr": (_load_adrs, _save_adrs),
        "handoff": (_load_handoffs, _save_handoffs),
        "session_record": (app_core._load_session_records, _save_session_records),
        "task": (rt._load_tasks, rt._save_tasks),
        "task_context": (app_core._load_task_contexts, _save_task_contexts),
        "risk_register": (app_core._load_risk_registers, _save_risk_registers),
        "validation_snapshot": (app_core._load_validation_snapshots, _save_validation_snapshots),
    }


def _artifact_find(artifact_type: str, artifact_id: str, project: str | None = None) -> tuple[list[dict], callable, dict]:
    pairs = _artifact_store_pairs()
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


def _memory_review_queue(request: MemoryReviewQueueRequest) -> dict:
    project = rt._canonical_project(request.project) if request.project else None
    records = [
        item for item in rt._load_live_memories()
        if app_core._memory_matches_filters(item, project=project, include_archived=False)
    ]
    by_id = {str(item.get("source_id")): item for item in records}
    queue: list[dict] = []
    for item in records:
        issues = _lint_memory_record(item)
        confidence = (item.get("metadata") or {}).get("confidence")
        reasons = [issue["code"] for issue in issues if issue["severity"] in {"error", "warning"}]
        if confidence is not None and confidence < 0.5:
            reasons.append("low_confidence")
        if not ((item.get("metadata") or {}).get("source_refs") or []):
            reasons.append("missing_source_refs")
        if not reasons:
            continue
        status = _review_status(item)
        if status in {"resolved", "dismissed"} and not request.include_closed:
            continue
        queue.append(
            {
                "queue_id": _review_queue_id("quality", item.get("project"), [item["source_id"]], "|".join(sorted(set(reasons))) + "|" + _memory_content_sha256(item)),
                "kind": "quality",
                "status": status,
                "score": min(1.0, 0.35 + 0.15 * len(set(reasons))),
                "reasons": sorted(set(reasons)),
                "memory_ids": [item["source_id"]],
                "memory": app_core._serialize_memory_record(item),
            }
        )

    if request.include_conflicts:
        conflicts = _detect_conflicts(
            MemoryDetectRequest(project=project, limit=200, include_archived=False)
        )
        for conflict in conflicts:
            left = by_id.get(conflict["left_id"])
            right = by_id.get(conflict["right_id"])
            status = _combined_review_status([left, right])
            if status in {"resolved", "dismissed"} and not request.include_closed:
                continue
            queue.append(
                {
                    "queue_id": conflict["queue_id"],
                    "kind": "contradiction",
                    "status": status,
                    "score": conflict["score"],
                    "reasons": [conflict["reason"]],
                    "memory_ids": [conflict["left_id"], conflict["right_id"]],
                    "payload": _serialize_conflict_candidate(conflict),
                }
            )
    queue.sort(key=lambda item: (item.get("score") or 0.0, item.get("queue_id") or ""), reverse=True)
    bounded_queue = queue[: max(min(request.limit, 200), 1)]
    return {
        "project": project,
        "items": bounded_queue,
        "lifecycle_suggestions": build_lifecycle_suggestions(bounded_queue),
    }


def _relation_apply_suggestions(request: RelationApplySuggestionsRequest) -> dict:
    if not request.project and not request.aggregate:
        raise HTTPException(
            status_code=403,
            detail={"code": "relation_apply_scope_required", "message": "project or aggregate=true is required"},
        )
    canonical_project = rt._canonical_project(request.project) if request.project else None
    suggestions = _relation_suggest(
        RelationSuggestRequest(project=canonical_project, limit=request.limit)
    ).get("suggestions", [])
    created = []
    for item in suggestions:
        if item["score"] < request.min_score:
            continue
        if item["relation"] == "relates_to" and not request.include_relates_to:
            continue
        link = _create_memory_link(
            MemoryLinkRequest(
                source_id=item["source_id"],
                target_id=item["target_id"],
                relation=item["relation"],
                project=canonical_project
                or rt._canonical_project(
                    rt._find_live_memory(item["source_id"]).get("project")
                ),
                metadata={"suggested": True, "score": item["score"], "reason": item["reason"]},
            )
        )
        created.append(_serialize_memory_link(link))
    return {"project": request.project, "links": created, "count": len(created)}


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
        if not app_core._project_matches(item.get("project"), project):
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


def _redact_memory_record(record: dict, request: MemoryRedactRequest) -> tuple[dict, int, bool]:
    original = record.get("content") or ""
    redaction_kinds: list[str] = []
    if request.patterns:
        if len(original) > _CUSTOM_REDACTION_MAX_INPUT_CHARS:
            raise HTTPException(status_code=413, detail="custom redaction input exceeds the bounded limit")
        redacted = original
        replacements = 0
        for pattern in request.patterns:
            try:
                compiled = compile_bounded_regex(
                    pattern,
                    field="redaction pattern",
                    max_length=_CUSTOM_REDACTION_MAX_PATTERN_LENGTH,
                )
            except SecurityBoundaryError as exc:
                raise HTTPException(status_code=422, detail=_safe_exception_text(exc)) from exc
            redacted, count = compiled.subn(
                lambda match: ((match.group(1) if match.lastindex else "") + request.replacement),
                redacted,
            )
            replacements += count
        if replacements:
            redaction_kinds.append("custom")
    else:
        redaction = redact_secret_text(original)
        redacted = re.sub(r"\[REDACTED:[^\]]+\]", request.replacement, redaction.value)
        replacements = redaction.replacements
        redaction_kinds.extend(sorted(set(redaction.kinds)))
    metadata = record.setdefault("metadata", {})
    removed_legacy_plaintext = metadata.pop("content_before_redaction", None) is not None
    changed = bool(replacements or removed_legacy_plaintext)
    if changed:
        metadata["redacted_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        metadata["redaction_count"] = replacements
        metadata["redaction_kinds"] = redaction_kinds
        metadata["content_before_redaction_sha256"] = hashlib.sha256(original.encode("utf-8")).hexdigest()
        metadata["content_before_redaction_chars"] = len(original)
        record["content"] = redacted
        record["updated_at"] = metadata["redacted_at"]
        app_core._append_memory_changelog(
            record,
            "redact",
            {
                "replacements": replacements,
                "kinds": redaction_kinds,
                "removed_legacy_plaintext": removed_legacy_plaintext,
            },
        )
    return record, replacements, changed


def _memory_redact(request: MemoryRedactRequest) -> dict:
    record = rt._find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    record, replacements, changed = _redact_memory_record(copy.deepcopy(record), request)
    if changed:
        app_core._replace_live_memory(record)
    return {"memory": app_core._serialize_memory_record(record), "replacements": replacements}


def _agent_activity_rollup(project: str | None = None) -> dict:
    project = rt._canonical_project(project) if project else None
    artifacts = {
        "checkpoints": [item for item in app_core._load_checkpoints() if app_core._project_matches(item.get("project"), project)],
        "handoffs": [item for item in _load_handoffs() if app_core._project_matches(item.get("project"), project)],
        "tasks": [item for item in rt._load_tasks() if app_core._project_matches(item.get("project"), project)],
        "session_records": [item for item in app_core._load_session_records() if app_core._project_matches(item.get("project"), project)],
    }
    with app_core._OBSERVATION_STORE_LOCK:
        observation_rollup = app_core._observation_store().activity_rollup(project=project)
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


@_serialized_link_mutation
def _relation_confidence_set(request: RelationConfidenceRequest) -> dict:
    project = rt._canonical_project(request.project)
    links = rt._load_memory_links()
    for item in links:
        if item.get("source_id") == request.source_id and item.get("target_id") == request.target_id and item.get("relation") == request.relation and rt._canonical_project(item.get("project")) == project:
            metadata = item.setdefault("metadata", {})
            metadata["confidence"] = request.confidence
            metadata["confidence_updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            rt._save_memory_links(links)
            return _serialize_memory_link(item)
    raise HTTPException(status_code=404, detail="relation not found")


@_serialized_link_mutation
def _relation_vote_quality(request: RelationVoteRequest) -> dict:
    project = rt._canonical_project(request.project)
    links = rt._load_memory_links()
    for item in links:
        if item.get("source_id") == request.source_id and item.get("target_id") == request.target_id and item.get("relation") == request.relation and rt._canonical_project(item.get("project")) == project:
            metadata = item.setdefault("metadata", {})
            votes = metadata.setdefault("quality_votes", [])
            votes.append({"vote": request.vote, "voter": request.voter, "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")})
            metadata["quality_score"] = round(sum(v["vote"] for v in votes) / len(votes), 3)
            rt._save_memory_links(links)
            return _serialize_memory_link(item)
    raise HTTPException(status_code=404, detail="relation not found")


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
        if app_core._memory_matches_filters(item, project=canonical_project, include_archived=False)
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


def _recent_failures_feed(request: RecentFailuresFeedRequest) -> dict:
    project = rt._canonical_project(request.project) if request.project else None
    failures = []
    for item in app_core._load_validation_snapshots():
        if not app_core._project_matches(item.get("project"), project):
            continue
        if (item.get("overall_status") or "").lower() in {"failed", "error", "red"}:
            failures.append({"kind": "validation_snapshot", "artifact": _serialize_validation_snapshot_record(item)})
    for item in _load_handoffs():
        if not app_core._project_matches(item.get("project"), project):
            continue
        text = " ".join([item.get("current_state") or "", item.get("validation") or ""]).lower()
        if any(token in text for token in ("fail", "error", "blocked")):
            failures.append({"kind": "handoff", "artifact": _serialize_handoff_record(item)})
    failures.sort(key=lambda item: item["artifact"].get("updated_at") or item["artifact"].get("created_at") or "", reverse=True)
    return {"project": project, "items": failures[: max(min(request.limit, 200), 1)]}


def _artifact_delete(request: ArtifactDeleteRequest) -> dict:
    items, saver, artifact = _artifact_find(request.artifact_type, request.artifact_id, request.project)
    remaining = [item for item in items if item.get("id") != request.artifact_id]
    saver(remaining)
    deleted_memory = None
    if request.delete_backing_memory and artifact.get("memory_id"):
        deleted_memory = _delete_live_memory_hard(HardDeleteMemoryRequest(id=artifact["memory_id"], project=request.project or artifact.get("project")))
    return {
        "artifact_type": request.artifact_type,
        "deleted": True,
        "artifact_id": request.artifact_id,
        "backing_memory_deleted": bool(deleted_memory),
        "backing_memory": app_core._serialize_memory_record(deleted_memory) if deleted_memory else None,
    }


def _artifact_list_by_type(request: ArtifactListRequest) -> dict:
    loader, _ = _artifact_store_pairs().get(request.artifact_type) or (None, None)
    if loader is None:
        raise HTTPException(status_code=400, detail="unsupported artifact type")
    items = loader()
    if request.project:
        items = [item for item in items if app_core._project_matches(item.get("project"), request.project)]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    start = max(request.offset, 0)
    end = start + max(min(request.limit, 200), 1)
    return {"artifact_type": request.artifact_type, "items": items[start:end], "total": total, "limit": max(min(request.limit, 200), 1), "offset": start}


def _artifact_usage_stats(project: str | None = None) -> dict:
    project = rt._canonical_project(project) if project else None
    memory_ids = {item.get("source_id") for item in rt._load_live_memories() if app_core._project_matches(item.get("project"), project)}
    counts = {}
    referenced_memory_ids = set()
    orphan_counts = {}
    for artifact_type, (loader, _) in _artifact_store_pairs().items():
        items = [item for item in loader() if app_core._project_matches(item.get("project"), project)]
        counts[artifact_type] = len(items)
        orphan_counts[artifact_type] = sum(1 for item in items if item.get("memory_id") and item.get("memory_id") not in memory_ids)
        referenced_memory_ids.update(item.get("memory_id") for item in items if item.get("memory_id"))
    return {
        "project": project,
        "artifact_counts": counts,
        "orphan_counts": orphan_counts,
        "backed_memory_count": len(referenced_memory_ids),
        "unreferenced_memory_count": sum(1 for item in rt._load_live_memories() if app_core._project_matches(item.get("project"), project) and item.get("source_id") not in referenced_memory_ids),
    }


def _link_cycle_detect(request: LinkCycleDetectRequest) -> dict:
    project = rt._canonical_project(request.project) if request.project else None
    links = [item for item in rt._load_memory_links() if app_core._project_matches(item.get("project"), project)]
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


def _link_orphan_scan(project: str | None = None) -> dict:
    project = rt._canonical_project(project) if project else None
    memory_ids = {item.get("source_id") for item in rt._load_live_memories() if app_core._project_matches(item.get("project"), project)}
    orphans = []
    for item in rt._load_memory_links():
        if not app_core._project_matches(item.get("project"), project):
            continue
        if item.get("source_id") not in memory_ids or item.get("target_id") not in memory_ids:
            orphans.append(_serialize_memory_link(item))
    return {"project": project, "orphan_links": orphans, "count": len(orphans)}


def _validation_trend_report(request: ValidationTrendReportRequest) -> dict:
    project = rt._canonical_project(request.project)
    items = [item for item in app_core._load_validation_snapshots() if app_core._project_matches(item.get("project"), project)]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "")
    trend = [{"at": item.get("updated_at") or item.get("created_at"), "overall_status": item.get("overall_status"), "lint": item.get("lint"), "tests": item.get("tests"), "smoke": item.get("smoke")} for item in items[-max(min(request.limit, 200), 1):]]
    status_counts = Counter((item.get("overall_status") or "unknown").lower() for item in items)
    return {"project": project, "trend": trend, "status_counts": dict(status_counts)}


def _entity_search(request: EntitySearchRequest) -> dict:
    project = app_core._effective_search_project(request.project)
    catalog = _entity_catalog_get(project)
    query = request.query.lower()
    matches = []
    for kind in ("files", "endpoints", "env_vars", "concepts"):
        for value, count in (catalog.get(kind) or {}).items():
            if query in value.lower():
                matches.append({"kind": kind, "value": value, "count": count})
    matches.sort(key=lambda item: item["count"], reverse=True)
    return {"project": project, "query": request.query, "matches": matches[: max(min(request.limit, 200), 1)]}


def _entity_link_memories(request: EntityLinkMemoriesRequest) -> dict:
    project = rt._canonical_project(request.project)
    candidates = []
    needle = request.entity.lower()
    for item in rt._load_live_memories():
        if not app_core._memory_matches_filters(item, project=project, include_archived=False):
            continue
        haystack = " ".join([item.get("content") or "", " ".join(item.get("tags") or []), " ".join((item.get("metadata") or {}).get("files") or []), " ".join((item.get("metadata") or {}).get("source_refs") or [])]).lower()
        if needle in haystack:
            candidates.append(item)
    candidates = candidates[: max(min(request.limit, 50), 2)]
    links = []
    for idx, left in enumerate(candidates):
        for right in candidates[idx + 1:]:
            link = _create_memory_link(MemoryLinkRequest(source_id=left["source_id"], target_id=right["source_id"], relation=request.relation, project=project, metadata={"entity": request.entity}))
            links.append(_serialize_memory_link(link))
    return {"project": project, "entity": request.entity, "matched_memory_ids": [item["source_id"] for item in candidates], "links": links, "count": len(links)}


def _alias_stats(project: str | None = None) -> dict:
    project = rt._canonical_project(project) if project else None
    alias_counter = Counter()
    alias_projects = {}
    for item in rt._load_live_memories():
        if not app_core._project_matches(item.get("project"), project):
            continue
        for alias in (item.get("metadata") or {}).get("aliases") or []:
            alias_counter[alias] += 1
            alias_projects.setdefault(alias, set()).add(item.get("project"))
    duplicates = [{"alias": alias, "count": count, "projects": sorted(alias_projects.get(alias) or [])} for alias, count in alias_counter.items() if count > 1]
    return {"project": project, "alias_count": sum(alias_counter.values()), "unique_alias_count": len(alias_counter), "duplicates": sorted(duplicates, key=lambda item: item["count"], reverse=True)}


@_serialized_link_mutation
def _relation_prune_low_quality(request: RelationPruneLowQualityRequest) -> dict:
    if not request.project and not request.aggregate:
        raise HTTPException(
            status_code=403,
            detail={"code": "relation_prune_scope_required", "message": "project or aggregate=true is required"},
        )
    project = rt._canonical_project(request.project) if request.project else None
    links = rt._load_memory_links()
    kept = []
    removed = []
    for item in links:
        if project and not app_core._project_matches(item.get("project"), project):
            kept.append(item)
            continue
        metadata = item.get("metadata") or {}
        confidence = metadata.get("confidence")
        quality = metadata.get("quality_score")
        remove = False
        if confidence is not None and confidence <= request.max_confidence:
            remove = True
        if quality is not None and quality <= request.max_quality_score:
            remove = True
        if request.remove_unscored and confidence is None and quality is None:
            remove = True
        if remove:
            removed.append(_serialize_memory_link(item))
        else:
            kept.append(item)
    rt._save_memory_links(kept)
    return {"project": project, "removed": removed, "count": len(removed)}


def _review_queue_apply(request: ReviewQueueApplyRequest) -> dict:
    queue = _memory_review_queue(
        MemoryReviewQueueRequest(
            project=request.project,
            limit=max(request.limit, len(request.queue_ids), 1),
            include_conflicts=True,
            include_closed=bool(request.queue_ids),
        )
    ).get("items", [])
    if request.queue_ids:
        requested_ids = {str(value).strip() for value in request.queue_ids if str(value).strip()}
        queue = [item for item in queue if item.get("queue_id") in requested_ids]
    applied: list[dict] = []
    effective_status = request.status if request.mark_needs_review else None
    missing_queue_ids = sorted(
        {str(value).strip() for value in request.queue_ids if str(value).strip()}
        - {str(item.get("queue_id")) for item in queue}
    )
    selected_memory_ids = list(
        dict.fromkeys(
            str(value)
            for item in queue
            for value in item.get("memory_ids") or []
            if str(value)
        )
    )
    canonical_project = rt._canonical_project(request.project) if request.project else None
    authoritative = rt._memory_store_is_authoritative()
    if authoritative:
        baseline_records = rt._memory_service().get_records(
            selected_memory_ids,
            project=canonical_project,
        )
    else:
        baseline_records = [
            item
            for item in rt._load_live_memories()
            if item.get("source_id") in selected_memory_ids
            and app_core._project_matches(item.get("project"), canonical_project)
        ]
    records_by_id = {
        str(record.get("source_id")): copy.deepcopy(record)
        for record in baseline_records
        if record.get("source_id")
    }
    changed_ids: set[str] = set()
    for item in queue:
        memory_ids = [str(value) for value in item.get("memory_ids") or []]
        records = [records_by_id[memory_id] for memory_id in memory_ids if memory_id in records_by_id]
        if not records:
            continue
        before_statuses = [_review_status(record) for record in records]
        redacted_ids: list[str] = []
        if request.auto_redact_secrets:
            for record in records:
                _record, replacements, changed = _redact_memory_record(
                    record,
                    MemoryRedactRequest(id=record["source_id"], project=record.get("project")),
                )
                if changed:
                    changed_ids.add(str(record["source_id"]))
                if replacements:
                    redacted_ids.append(record["source_id"])
        updated_records: list[dict] = []
        for memory_id in memory_ids:
            record = records_by_id.get(memory_id)
            if record is None:
                continue
            metadata = record.setdefault("metadata", {})
            status_changed = bool(effective_status and _review_status(record) != effective_status)
            was_redacted = memory_id in redacted_ids
            if not status_changed and not was_redacted:
                updated_records.append(record)
                continue
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            if effective_status:
                metadata["review_status"] = effective_status
                metadata["review_updated_at"] = now
                metadata["review_reason"] = ",".join(item.get("reasons") or [])
                metadata["review_queue_id"] = item.get("queue_id")
            app_core._append_memory_changelog(
                record,
                "review_queue_apply",
                {
                    "queue_id": item.get("queue_id"),
                    "kind": item.get("kind"),
                    "status": effective_status,
                    "reasons": item.get("reasons") or [],
                    "redacted": record["source_id"] in redacted_ids,
                },
            )
            record["updated_at"] = now
            changed_ids.add(memory_id)
            updated_records.append(record)
        if effective_status and all(status == effective_status for status in before_statuses) and not redacted_ids:
            action = "already_" + effective_status
        elif not effective_status and not redacted_ids:
            action = "already_noop"
        else:
            action = "updated"
        applied.append(
            {
                "queue_id": item.get("queue_id"),
                "kind": item.get("kind"),
                "action": action,
                "status": effective_status or _combined_review_status(updated_records),
                "memory_ids": [record["source_id"] for record in updated_records],
                "memories": [app_core._serialize_memory_record(record) for record in updated_records],
            }
        )
    if changed_ids:
        changed_records = [records_by_id[memory_id] for memory_id in selected_memory_ids if memory_id in changed_ids]
        if authoritative:
            rt._memory_service().upsert_records(changed_records)
        else:
            replacements = {record["source_id"]: record for record in changed_records}
            merged_records = [
                replacements.get(str(record.get("source_id")), record)
                for record in rt._load_live_memories()
            ]
            app_core._save_live_memories(merged_records)
    return {
        "project": request.project,
        "status": effective_status,
        "count": len(applied),
        "items": applied,
        "missing_queue_ids": missing_queue_ids,
    }


def _triage_queue_apply(request: TriageQueueApplyRequest) -> dict:
    created = _relation_apply_suggestions(
        RelationApplySuggestionsRequest(
            project=request.project,
            min_score=request.min_score,
            limit=request.limit,
            include_relates_to=request.include_relates_to,
        )
    )
    return {"project": request.project, "count": created.get("count", 0), "links": created.get("links", [])}


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
        if not app_core._memory_matches_filters(item, project=project, include_archived=request.include_archived):
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
        if not app_core._project_matches(item.get("project"), project):
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
            "repair_live_indexes": _repair_live_indexes(
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
        app_core._authorize_resource_project(http_request, scope_project)
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
    unknown_artifacts = sorted(set(str(key) for key in artifacts) - set(_artifact_store_pairs()))
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
            for item in _artifact_store_pairs().get(artifact_type, (lambda: [], lambda _items: None))[0]()
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
            if app_core._memory_matches_filters(item, project=project, include_archived=request.include_archived)
        ],
        "links": [
            item
            for item in rt._load_memory_links()
            if not project or rt._canonical_project(item.get("project")) == project
        ],
        "artifacts": {},
    }
    if request.include_artifacts:
        for artifact_type, (loader, _) in _artifact_store_pairs().items():
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
        pair = _artifact_store_pairs().get(artifact_type)
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


def _policy_profile_set(request: PolicyProfileSetRequest) -> dict:
    profile = request.model_dump()
    profile["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _save_policy_profile(profile)
    return profile


def _policy_enforce_memory(request: PolicyEnforceMemoryRequest) -> dict:
    record = rt._find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    profile = app_core._load_policy_profile()
    content = record.get("content") or ""
    issues = []
    if profile.get("require_project") and not record.get("project"):
        issues.append("missing_project")
    if profile.get("require_memory_type") and not record.get("memory_type"):
        issues.append("missing_memory_type")
    if len(content) > int(profile.get("max_content_chars") or 8000):
        issues.append("content_too_large")
    if content.count("\n") > int(profile.get("max_lines") or 120):
        issues.append("too_many_lines")
    secret_like = contains_secret_like(content)
    if profile.get("block_secret_like") and secret_like:
        issues.append("secret_like")
        if request.auto_redact:
            _memory_redact(MemoryRedactRequest(id=request.id, project=request.project))
            record = rt._find_live_memory(request.id, request.project) or record
    raw_log_like = content.count("\n") > int(profile.get("max_lines") or 120)
    if profile.get("block_raw_logs") and raw_log_like:
        issues.append("raw_log_like")
    return {"memory": app_core._serialize_memory_record(record), "profile": profile, "issues": issues, "ok": not issues}


def _overlap_report(request: OverlapReportRequest) -> dict:
    project = rt._canonical_project(request.project) if request.project else None
    duplicates = app_core._detect_duplicates(MemoryDetectRequest(project=project, limit=request.limit, include_archived=False))
    same_upsert = []
    by_key = {}
    for item in rt._load_live_memories():
        if not app_core._memory_matches_filters(item, project=project, include_archived=True):
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
            and app_core._project_matches(item.get("project"), canonical_project)
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
    root_id = request.root_id or app_core._code_graph_query_root_id(project, settings.repo_root)
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


def bhm_forget_preview(request: ForgetPreviewRequest) -> dict:
    return _forget_preview(request)


async def bhm_forget_apply(request: ForgetApplyRequest) -> dict:
    return await rt._run_bounded_write("bhm.forget.apply", _forget_apply, request)


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


async def bhm_checkpoint_create(request: CheckpointCreateRequest) -> dict:
    action, checkpoint = await rt._run_bounded_write("bhm.checkpoint", _create_checkpoint, request)
    return {"success": True, "action": action, "checkpoint": app_core._serialize_checkpoint_record(checkpoint)}


def bhm_checkpoint_list(
    project: str | None = None,
    checkpoint_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    checkpoints, total = app_core._list_checkpoints(project, checkpoint_type, limit, offset)
    return {
        "checkpoints": [app_core._serialize_checkpoint_record(item) for item in checkpoints],
        "total": total,
        "limit": max(min(limit, 200), 1),
        "offset": max(offset, 0),
    }


def bhm_checkpoint_get_latest(project: str, checkpoint_type: str | None = None) -> dict:
    checkpoint = app_core._get_latest_checkpoint(project, checkpoint_type)
    return {"checkpoint": app_core._serialize_checkpoint_record(checkpoint)}


async def bhm_memory_delete(request: MemoryDeleteRequest) -> dict:
    deleted = await rt._run_bounded_write("bhm.memory.delete", _delete_live_memory, request)
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
        _adr_supersede,
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
    action, record = await rt._run_bounded_write("bhm.handoff", _create_handoff, request)
    return {"success": True, "action": action, "handoff": _serialize_handoff_record(record)}


def bhm_handoff_list(project: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    items, total = _list_handoffs(project, limit, offset)
    return {
        "handoffs": [_serialize_handoff_record(item) for item in items],
        "total": total,
        "limit": max(min(limit, 200), 1),
        "offset": max(offset, 0),
    }


async def bhm_session_record_create(request: SessionRecordCreateRequest) -> dict:
    action, record = await rt._run_bounded_write("bhm.session-record", _create_session_record, request)
    return {"success": True, "action": action, "session_record": _serialize_session_record(record)}


def bhm_session_record_list(project: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    items, total = _list_session_records(project, limit, offset)
    return {
        "session_records": [_serialize_session_record(item) for item in items],
        "total": total,
        "limit": max(min(limit, 200), 1),
        "offset": max(offset, 0),
    }


async def bhm_task_open(request: TaskOpenRequest) -> dict:
    action, record = await rt._run_bounded_write("bhm.task.open", _open_task, request)
    return {"success": True, "action": action, "task": app_core._serialize_task_record(record)}


async def bhm_task_close(request: TaskCloseRequest) -> dict:
    action, record = await rt._run_bounded_write("bhm.task.close", _close_task, request)
    return {"success": True, "action": action, "task": app_core._serialize_task_record(record)}


def bhm_task_get(task_id: str, project: str | None = None) -> dict:
    return {"task": app_core._serialize_task_record(_get_task(task_id, project))}


def bhm_task_list(
    project: str | None = None,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    items, total = app_core._list_tasks(project, status, limit, offset)
    return {
        "tasks": [app_core._serialize_task_record(item) for item in items],
        "total": total,
        "limit": max(min(limit, 200), 1),
        "offset": max(offset, 0),
    }


async def bhm_task_context_update(request: TaskContextUpdateRequest) -> dict:
    action, record = await rt._run_bounded_write("bhm.task-context", _upsert_task_context, request)
    return {"success": True, "action": action, "task_context": app_core._serialize_task_context_record(record)}


def bhm_task_context_get(project: str) -> dict:
    record = app_core._get_task_context(project)
    return {"task_context": app_core._serialize_task_context_record(record)}


async def bhm_risk_register_update(request: RiskRegisterUpdateRequest) -> dict:
    action, record = await rt._run_bounded_write("bhm.risk-register", _upsert_risk_register, request)
    return {"success": True, "action": action, "risk_register": app_core._serialize_risk_register_record(record)}


def bhm_risk_register_get(project: str) -> dict:
    record = app_core._get_risk_register(project)
    return {"risk_register": app_core._serialize_risk_register_record(record)}


async def bhm_validation_snapshot_save(request: ValidationSnapshotSaveRequest) -> dict:
    action, record = await rt._run_bounded_write("bhm.validation-snapshot", _save_validation_snapshot_record, request)
    return {"success": True, "action": action, "validation_snapshot": _serialize_validation_snapshot_record(record)}


def bhm_validation_snapshot_get(project: str) -> dict:
    record = app_core._get_validation_snapshot(project)
    return {"validation_snapshot": _serialize_validation_snapshot_record(record)}


def bhm_query_suggestions(project: str | None = None) -> dict:
    return {"suggestions": app_core._query_suggestions(project)}


def bhm_integrity_audit(project: str | None = None) -> dict:
    return _integrity_audit(project)


def bhm_audit(project: str = "e-github-workspace", operation: str = "", limit: int = 50) -> dict:
    return {
        "project": project,
        "operation": operation,
        "limit": max(min(limit, 200), 1),
        "audit": _integrity_audit(project),
    }


def bhm_artifact_integrity_audit(request: ProjectOnlyRequest) -> dict:
    return _artifact_integrity_audit(request.project)


def bhm_repair_live_indexes(request: RepairLiveIndexesRequest) -> dict:
    return _repair_live_indexes(request)


def bhm_entity_extract(request: EntityExtractRequest) -> dict:
    return _entity_extract(request)


def bhm_relation_suggest(request: RelationSuggestRequest) -> dict:
    return _relation_suggest(request)


def bhm_link_graph_stats(request: ProjectOnlyRequest) -> dict:
    return _link_graph_stats(request.project)


def bhm_reindex_memory_metadata(request: ScopedMaintenanceRequest) -> dict:
    return _reindex_memory_metadata(request.project, aggregate=request.aggregate)


def bhm_artifact_restore(request: ArtifactRestoreRequest) -> dict:
    return _artifact_restore(request)


def bhm_orphan_artifact_relink(request: OrphanArtifactRelinkRequest) -> dict:
    return _orphan_artifact_relink(request)


def bhm_relation_apply_suggestions(request: RelationApplySuggestionsRequest) -> dict:
    return _relation_apply_suggestions(request)


def bhm_ontology_quarantine_list(project: str, limit: int = 100) -> dict:
    return _list_ontology_quarantine(project, limit)


def bhm_schema_upgrade_all(request: SchemaUpgradeAllRequest) -> dict:
    return _schema_upgrade_all(request)


def bhm_agent_activity_rollup(request: ProjectOnlyRequest) -> dict:
    return _agent_activity_rollup(request.project)


def bhm_relation_confidence_set(request: RelationConfidenceRequest) -> dict:
    link = _relation_confidence_set(request)
    return {"success": True, "link": link}


def bhm_relation_vote_quality(request: RelationVoteRequest) -> dict:
    link = _relation_vote_quality(request)
    return {"success": True, "link": link}


def bhm_entity_catalog_get(request: EntityCatalogRequest) -> dict:
    return _entity_catalog_get(request.project)


def bhm_entity_catalog_rebuild(request: EntityCatalogRequest) -> dict:
    return _entity_catalog_rebuild(request.project, aggregate=request.aggregate)


def bhm_recent_failures_feed(request: RecentFailuresFeedRequest) -> dict:
    return _recent_failures_feed(request)


def bhm_artifact_delete(request: ArtifactDeleteRequest) -> dict:
    return _artifact_delete(request)


def bhm_artifact_list_by_type(request: ArtifactListRequest) -> dict:
    return _artifact_list_by_type(request)


def bhm_artifact_usage_stats(request: ProjectOnlyRequest) -> dict:
    return _artifact_usage_stats(request.project)


def bhm_link_cycle_detect(request: LinkCycleDetectRequest) -> dict:
    return _link_cycle_detect(request)


def bhm_link_orphan_scan(request: ProjectOnlyRequest) -> dict:
    return _link_orphan_scan(request.project)


def bhm_validation_trend_report(request: ValidationTrendReportRequest) -> dict:
    return _validation_trend_report(request)


def bhm_entity_search(request: EntitySearchRequest) -> dict:
    return _entity_search(request)


def bhm_entity_link_memories(request: EntityLinkMemoriesRequest) -> dict:
    return _entity_link_memories(request)


def bhm_alias_stats(request: ProjectOnlyRequest) -> dict:
    return _alias_stats(request.project)


def bhm_relation_prune_low_quality(request: RelationPruneLowQualityRequest) -> dict:
    return _relation_prune_low_quality(request)


def bhm_review_queue_apply(request: ReviewQueueApplyRequest) -> dict:
    return _review_queue_apply(request)


def bhm_triage_queue_apply(request: TriageQueueApplyRequest) -> dict:
    return _triage_queue_apply(request)


def bhm_artifact_batch_delete(request: ArtifactBatchDeleteRequest) -> dict:
    return _artifact_batch_delete(request)


def bhm_artifact_batch_relink(request: ArtifactBatchRelinkRequest) -> dict:
    return _artifact_batch_relink(request)


def bhm_artifact_batch_restore(request: ArtifactBatchRestoreRequest) -> dict:
    return _artifact_batch_restore(request)


def bhm_schema_validate_strict(request: StrictSchemaValidateRequest) -> dict:
    return _schema_validate_strict(request)


def bhm_integrity_repair_strict(request: IntegrityRepairStrictRequest) -> dict:
    return _integrity_repair_strict(request)


def bhm_admin_export(request: AdminExportRequest) -> dict:
    return _admin_export(request)


def bhm_admin_import_preview(request: AdminImportPreviewRequest, http_request: Request) -> dict:
    result = _admin_import_preview(request, http_request=http_request)
    result["admission"] = admit_ingress(
        ingress=IngressKind.IMPORT,
        project=str(result["project"]),
        actor=app_core._request_actor(http_request),
        payload={"path": request.path, "scope": result["project"]},
    ).receipt()
    return result


def bhm_admin_import_apply(request: AdminImportApplyRequest, http_request: Request) -> dict:
    # Imports remain preview-only until a typed reviewed-promotion path exists.
    app_core._admit_new_content(
        ingress=IngressKind.IMPORT,
        project=request.project or "",
        actor=app_core._request_actor(http_request),
        payload=request.model_dump(mode="json"),
        requires_authority=True,
    )
    return _admin_import_apply(request, http_request=http_request)


def bhm_admin_task_ingress_status() -> dict:
    return {"task_ingress": _task_ingress_status()}


def bhm_admin_task_ingress_freeze(request: TaskIngressControlRequest) -> dict:
    action, status = _freeze_task_ingress(request)
    return {"success": True, "action": action, "task_ingress": status}


def bhm_admin_task_ingress_resume(request: TaskIngressControlRequest) -> dict:
    action, status = _resume_task_ingress(request)
    return {"success": True, "action": action, "task_ingress": status}


async def bhm_policy_profile_get() -> dict:
    snapshot = await asyncio.to_thread(app_core._load_policy_profile_snapshot)
    return snapshot["profile"]


async def bhm_policy_profile_set(request: PolicyProfileSetRequest) -> dict:
    return await asyncio.to_thread(_policy_profile_set, request)


def bhm_policy_enforce_memory(request: PolicyEnforceMemoryRequest) -> dict:
    return _policy_enforce_memory(request)


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
            actor=app_core._request_actor(http_request),
            payload=request.model_dump(mode="json"),
            requires_authority=True,
        )
    return await app_core._remember_authoritative(request, actor=app_core._request_actor(http_request))


def bhm_observation_store_status(integrity: bool = False) -> dict:
    store = app_core._observation_store()
    return store.status(integrity_check=integrity)


def bhm_retention_status(project: str | None = None, as_of: str | None = None) -> dict:
    try:
        policy = load_retention_policy(_retention_policy_path())
        effective_as_of = parse_timestamp(as_of) if as_of else datetime.now(timezone.utc)
        if effective_as_of is None:
            raise HTTPException(status_code=422, detail={"error": "invalid_retention_as_of"})
        plan = build_retention_plan(
            app_core._observation_store().retention_candidates(project=project),
            app_core._hook_queue().retention_candidates(project=project),
            policy,
            as_of=effective_as_of,
        )
        return {
            "success": True,
            "mode": "dry-run",
            "project": project or "",
            "plan": summarize_retention_plan(plan),
            "stores": {
                "observations": app_core._observation_store().status(),
                "hookQueue": app_core._hook_queue().status(),
            },
        }
    except HTTPException:
        raise
    except (OSError, RetentionPolicyError) as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "retention_policy_unavailable", "detail": _safe_exception_text(exc)},
        ) from exc



def register(app) -> None:
    """Attach this module's routes to the application."""

    app.get("/", response_class=FileResponse)(index)
    app.get("/favicon.ico", response_class=FileResponse)(favicon_ico)
    app.get("/favicon.svg", response_class=FileResponse)(favicon_svg)
    app.get("/static/redoc.standalone.js", response_class=FileResponse)(redoc_bundle)
    app.get("/static/3d-force-graph.min.js", response_class=FileResponse)(force_graph_bundle)
    app.get("/static/three.module.min.js", response_class=FileResponse)(three_module_bundle)
    app.get("/static/three.core.min.js", response_class=FileResponse)(three_core_bundle)
    app.get("/redoc")(local_redoc)
    app.post("/bhm/conventions/preview", include_in_schema=False)(bhm_conventions_preview)
    app.post("/bhm/factories/preview", include_in_schema=False)(bhm_factories_preview)
    app.post("/bhm/migration/preview", include_in_schema=False)(bhm_migration_preview)
    app.post("/bhm/security/trust-boundary/preview", include_in_schema=False)(bhm_security_trust_boundary_preview)
    app.get("/graph/status")(graph_status)
    app.websocket("/bhm/ws")(bhm_memory_pulse_ws)
    app.get("/bhm/memory")(bhm_memory_get)
    app.post("/bhm/forget/preview")(bhm_forget_preview)
    app.post("/bhm/forget/apply")(bhm_forget_apply)
    app.post("/bhm/recent-activity")(bhm_recent_activity)
    app.post("/bhm/checkpoint")(bhm_checkpoint_create)
    app.get("/bhm/checkpoints")(bhm_checkpoint_list)
    app.get("/bhm/checkpoint/latest")(bhm_checkpoint_get_latest)
    app.delete("/bhm/memory")(bhm_memory_delete)
    app.post("/bhm/adr")(bhm_adr_create)
    app.get("/bhm/adrs")(bhm_adr_list)
    app.post("/bhm/adr/supersede")(bhm_adr_supersede)
    app.post("/bhm/handoff")(bhm_handoff_create)
    app.get("/bhm/handoffs")(bhm_handoff_list)
    app.post("/bhm/session-record")(bhm_session_record_create)
    app.get("/bhm/session-records")(bhm_session_record_list)
    app.post("/bhm/task/open")(bhm_task_open)
    app.post("/bhm/task/close")(bhm_task_close)
    app.get("/bhm/task")(bhm_task_get)
    app.get("/bhm/tasks")(bhm_task_list)
    app.post("/bhm/task-context")(bhm_task_context_update)
    app.get("/bhm/task-context")(bhm_task_context_get)
    app.post("/bhm/risk-register")(bhm_risk_register_update)
    app.get("/bhm/risk-register")(bhm_risk_register_get)
    app.post("/bhm/validation-snapshot")(bhm_validation_snapshot_save)
    app.get("/bhm/validation-snapshot")(bhm_validation_snapshot_get)
    app.get("/bhm/query-suggestions")(bhm_query_suggestions)
    app.post("/bhm/integrity-audit")(bhm_integrity_audit)
    app.get("/bhm/audit")(bhm_audit)
    app.post("/bhm/artifact-integrity-audit")(bhm_artifact_integrity_audit)
    app.post("/bhm/repair-live-indexes")(bhm_repair_live_indexes)
    app.post("/bhm/entity-extract")(bhm_entity_extract)
    app.post("/bhm/relation-suggest")(bhm_relation_suggest)
    app.post("/bhm/link-graph-stats")(bhm_link_graph_stats)
    app.post("/bhm/reindex-memory-metadata")(bhm_reindex_memory_metadata)
    app.post("/bhm/artifact/restore")(bhm_artifact_restore)
    app.post("/bhm/artifact/relink")(bhm_orphan_artifact_relink)
    app.post("/bhm/relation/apply-suggestions")(bhm_relation_apply_suggestions)
    app.get("/bhm/ontology/quarantine")(bhm_ontology_quarantine_list)
    app.post("/bhm/schema/upgrade-all")(bhm_schema_upgrade_all)
    app.post("/bhm/agent-activity-rollup")(bhm_agent_activity_rollup)
    app.post("/bhm/relation/confidence")(bhm_relation_confidence_set)
    app.post("/bhm/relation/vote-quality")(bhm_relation_vote_quality)
    app.post("/bhm/entity-catalog/get")(bhm_entity_catalog_get)
    app.post("/bhm/entity-catalog/rebuild")(bhm_entity_catalog_rebuild)
    app.post("/bhm/recent-failures-feed")(bhm_recent_failures_feed)
    app.post("/bhm/artifact/delete")(bhm_artifact_delete)
    app.post("/bhm/artifact/list-by-type")(bhm_artifact_list_by_type)
    app.post("/bhm/artifact/usage-stats")(bhm_artifact_usage_stats)
    app.post("/bhm/link/cycle-detect")(bhm_link_cycle_detect)
    app.post("/bhm/link/orphan-scan")(bhm_link_orphan_scan)
    app.post("/bhm/validation/trend-report")(bhm_validation_trend_report)
    app.post("/bhm/entity/search")(bhm_entity_search)
    app.post("/bhm/entity/link-memories")(bhm_entity_link_memories)
    app.post("/bhm/alias/stats")(bhm_alias_stats)
    app.post("/bhm/relation/prune-low-quality")(bhm_relation_prune_low_quality)
    app.post("/bhm/review-queue/apply")(bhm_review_queue_apply)
    app.post("/bhm/triage-queue/apply")(bhm_triage_queue_apply)
    app.post("/bhm/artifact/batch-delete")(bhm_artifact_batch_delete)
    app.post("/bhm/artifact/batch-relink")(bhm_artifact_batch_relink)
    app.post("/bhm/artifact/batch-restore")(bhm_artifact_batch_restore)
    app.post("/bhm/schema/validate-strict")(bhm_schema_validate_strict)
    app.post("/bhm/integrity/repair-strict")(bhm_integrity_repair_strict)
    app.post("/bhm/admin/export")(bhm_admin_export)
    app.post("/bhm/admin/import-preview")(bhm_admin_import_preview)
    app.post("/bhm/admin/import-apply")(bhm_admin_import_apply)
    app.get("/bhm/admin/task-ingress/status")(bhm_admin_task_ingress_status)
    app.post("/bhm/admin/task-ingress/freeze")(bhm_admin_task_ingress_freeze)
    app.post("/bhm/admin/task-ingress/resume")(bhm_admin_task_ingress_resume)
    app.get("/bhm/policy/profile")(bhm_policy_profile_get)
    app.post("/bhm/policy/profile")(bhm_policy_profile_set)
    app.post("/bhm/policy/enforce-memory")(bhm_policy_enforce_memory)
    app.post("/bhm/overlap/report")(bhm_overlap_report)
    app.post("/bhm/overlap/cleanup-apply")(bhm_overlap_cleanup_apply)
    app.post("/bhm/policy-guard")(bhm_policy_guard)
    app.post("/bhm/reflect")(bhm_reflect)
    app.post("/bhm/verify")(bhm_verify)
    app.post("/bhm/remember")(bhm_remember)
    app.get("/bhm/observations/store/status")(bhm_observation_store_status)
    app.get("/bhm/retention/status")(bhm_retention_status)
