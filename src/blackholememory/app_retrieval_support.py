"""Retrieval support helpers extracted from app_retrieval."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import HTTPException

from . import app_core, app_core_fallback, app_core_hit, app_core_hook
from . import app_runtime as rt
from .api_models import (
    ContextCompileRequest,
    ContextTierPromotionRollbackRequest,
    MemoryAdvancedSearchRequest,
    SearchRequest,
    UtilityFeedbackEventRequest,
)
from .app_runtime import ResponseTimeout, _safe_exception_text
from .caller_auth import authorize_projects
from .context_tier_promotion import (
    ContextTierPromotionDisabled,
    ContextTierPromotionError,
    ContextTierPromotionMigrationRequired,
    ContextTierPromotionStale,
    rollback_tier_promotion,
)
from .context_tier_promotion import runtime_enabled as context_tier_promotion_enabled
from .feedback_consolidation import (
    FeedbackConsolidationError,
    build_feedback_consolidation_preview,
)
from .memory_service import MemoryServiceNotReady
from .utility_feedback import UtilityEvent
from .utility_feedback_store import append_utility_event

_LOGGER = logging.getLogger(__name__)

def _is_fallback_grace_error(exc: Exception) -> bool:
    if app_core_fallback._configured_fallback_mode() != "explicit":
        return False
    if isinstance(exc, (ResponseTimeout, TimeoutError, asyncio.TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, HTTPException):
        return exc.status_code in {429, 503, 504}
    message = str(exc).lower()
    return any(token in message for token in ("timeout", "timed out", "connection", "semaphore", "backpressure"))


def _fallback_rank_records(query: str, records: list[dict]) -> list[dict]:
    if not query:
        return sorted(records, key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    scored: list[tuple[float, dict]] = []
    for record in records:
        item = {"memory": record.get("content") or "", "metadata": record.get("metadata") or {}}
        score = app_core_hook._lexical_signal(query, item) + app_core_hook._memory_type_weight(item) + app_core_hook._query_intent_weight(query, item)
        if score > 0:
            scored.append((score, record))
    scored.sort(key=lambda pair: (pair[0], pair[1].get("updated_at") or pair[1].get("created_at") or ""), reverse=True)
    return [record for _, record in scored]


def _fallback_grace_mem0_search(request: SearchRequest, reason: Exception) -> dict:
    records = app_core_hit._fallback_memory_records(
        project=app_core_fallback._effective_search_project(request.project),
        memory_class=request.memory_class,
        event_role=request.event_role,
        include_archived=request.include_archived,
        include_logs=request.include_logs,
        include_historical=request.include_historical,
        domain=request.domain,
        semantic_type=request.semantic_type,
        priority=request.priority,
        as_of=request.as_of,
        valid_from=request.valid_from,
        valid_to=request.valid_to,
        include_temporal_unknown=request.include_temporal_unknown,
        freshness_days=request.freshness_days,
    )
    records = _fallback_rank_records(request.query, records)[: max(min(request.top_k, 200), 1)]
    results = [
        {
            "id": record.get("source_id"),
            "memory": record.get("content") or "",
            "metadata": {
                **(record.get("metadata") or {}),
                "source_id": record.get("source_id"),
                "project": record.get("project"),
                "memory_type": record.get("memory_type"),
                "memory_class": app_core._effective_memory_class(record),
                "event_role": app_core._effective_event_role(record),
                "tags": record.get("tags") or [],
            },
            "score": 0.0,
        }
        for record in records
    ]
    return {
        "ok": True,
        "degraded": True,
        "read_only": True,
        "result": {"results": results},
        "fallback_grace": app_core_fallback._fallback_grace_meta("mem0.search", reason),
        "filters": {
            "project": request.project,
            "memory_class": request.memory_class.value if request.memory_class else None,
            "event_role": request.event_role.value if request.event_role else None,
            "domain": request.domain,
            "semantic_type": request.semantic_type,
            "priority": request.priority,
            "include_archived": request.include_archived,
            "include_logs": request.include_logs,
            "include_historical": request.include_historical,
            "freshness_days": request.freshness_days,
        },
    }


def _fallback_grace_memories_response(
    route: str,
    reason: Exception,
    *,
    project: str | None = None,
    memory_type: str | None = None,
    memory_class: app_core_hit.MemoryClass | str | None = None,
    event_role: app_core_hit.MemoryEventRole | str | None = None,
    concepts: list[str] | None = None,
    files: list[str] | None = None,
    query: str = "",
    include_logs: bool = False,
    include_historical: bool = False,
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    include_archived: bool = False,
    limit: int = 20,
    offset: int = 0,
    freshness_days: int | None = None,
) -> dict:
    items = app_core_hit._fallback_memory_records(
        project=project,
        memory_type=memory_type,
        memory_class=memory_class,
        event_role=event_role,
        concepts=concepts,
        files=files,
        include_archived=include_archived,
        include_logs=include_logs,
        include_historical=include_historical,
        domain=domain,
        semantic_type=semantic_type,
        priority=priority,
        as_of=as_of,
        valid_from=valid_from,
        valid_to=valid_to,
        include_temporal_unknown=include_temporal_unknown,
        freshness_days=freshness_days,
    )
    items = _fallback_rank_records(query, items)
    total = len(items)
    start = max(offset, 0)
    capped_limit = max(min(limit, 200), 1)
    window = items[start:start + capped_limit]
    return {
        "memories": [app_core._serialize_memory_record(item) for item in window],
        "total": total,
        "limit": capped_limit,
        "offset": start,
        "degraded": True,
        "read_only": True,
        "fallback_grace": app_core_fallback._fallback_grace_meta(route, reason),
    }


def _emit_memory_pulses_from_records(records: list[dict]) -> None:
    seen: set[str] = set()
    for record in records:
        node_id = str(record.get("source_id") or record.get("id") or "")
        if node_id and node_id not in seen:
            seen.add(node_id)
            rt._emit_memory_pulse(node_id, str(record.get("project") or ""))


def _emit_memory_pulses_from_mem0_items(items: list[dict]) -> None:
    seen: set[str] = set()
    for item in items:
        metadata = item.get("metadata") or {}
        node_id = str(metadata.get("source_id") or item.get("source_id") or item.get("id") or "")
        if node_id and node_id not in seen:
            seen.add(node_id)
            rt._emit_memory_pulse(node_id, str(metadata.get("project") or item.get("project") or ""))


def _authoritative_context_compile_request(
    request: ContextCompileRequest,
    *,
    project: str,
    include_archived: bool,
    include_logs: bool,
    include_historical: bool,
    freshness_days: int | None,
    history_scope: str,
    limit: int,
) -> MemoryAdvancedSearchRequest:
    """Translate context filters to the bounded SQLite retrieval contract."""

    return MemoryAdvancedSearchRequest(
        query=request.query,
        project=project,
        memory_type=request.memory_type,
        memory_class=request.memory_class,
        event_role=request.event_role,
        as_of=request.as_of,
        valid_from=request.valid_from,
        valid_to=request.valid_to,
        include_temporal_unknown=request.include_temporal_unknown,
        concepts=request.concepts,
        files=request.files,
        include_archived=include_archived,
        include_logs=include_logs,
        include_historical=include_historical,
        freshness_days=freshness_days,
        history_scope=history_scope,
        domain=request.domain,
        semantic_type=request.semantic_type,
        priority=request.priority,
        limit=limit,
        offset=0,
    )


def _record_utility_feedback_event(
    request: UtilityFeedbackEventRequest,
    *,
    principal: Any,
) -> dict[str, Any]:
    """Persist one caller-bound utility signal without changing memory state.

    The client never supplies ``actor_id``.  Binding it to the authenticated
    caller prevents a feedback event from impersonating another agent while
    retaining replay-safe, project-scoped immutable evidence.
    """

    if principal is None:
        raise HTTPException(status_code=401, detail={"code": "caller_auth_required"})
    project = rt._canonical_project(request.project)
    project_error = authorize_projects(principal, (project,), require_explicit=True)
    if project_error:
        raise HTTPException(status_code=403, detail={"code": project_error})
    try:
        service = rt._memory_service()
        if rt._find_live_memory(request.memory_id, project) is None:
            raise HTTPException(status_code=404, detail={"code": "memory_not_found_in_project"})
        event = UtilityEvent(
            event_id=request.event_id,
            memory_id=request.memory_id,
            project=project,
            actor_id=str(principal.caller_id),
            event_type=request.event_type,
            observed_at=request.observed_at,
            request_digest=request.request_digest,
            confidence=request.confidence,
        )
        _record, inserted = append_utility_event(service, event)
    except HTTPException:
        raise
    except (MemoryServiceNotReady, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "utility_feedback_unavailable", "reason": _safe_exception_text(exc)},
        ) from exc
    return {
        "schema_version": "bhm.utility-feedback.ack.v1",
        "project": project,
        "event_id": event.event_id,
        "memory_id": event.memory_id,
        "actor_binding": "authenticated-caller",
        "inserted": inserted,
        "replayed": not inserted,
        "lifecycle_action": "none",
        "side_effects": {
            "sqlite_mutation": bool(inserted),
            "qdrant_mutation": False,
            "projection_mutation": False,
            "automatic_lifecycle_change": False,
        },
    }


def _utility_feedback_consolidation_preview(
    *,
    project: str,
    as_of: str,
    half_life_days: float,
    min_samples: int,
    max_proposals: int,
) -> dict[str, Any]:
    """Expose feedback-led review worklists without enabling a repair path."""

    report = app_core_hook._utility_feedback_report(
        project=project,
        as_of=as_of,
        half_life_days=half_life_days,
        min_samples=min_samples,
    )
    try:
        preview = build_feedback_consolidation_preview(
            report,
            project=str(report["project"]),
            max_proposals=max_proposals,
        )
    except FeedbackConsolidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "utility_feedback_consolidation_preview_invalid", "reason": _safe_exception_text(exc)},
        ) from exc
    return {
        **preview,
        "lifecycle_action": "none",
        "side_effects": {
            "read_only": True,
            "sqlite_mutation": False,
            "qdrant_mutation": False,
            "projection_mutation": False,
            "automatic_lifecycle_change": False,
        },
    }


def _require_context_tier_promotion_enabled() -> None:
    if not context_tier_promotion_enabled():
        raise HTTPException(
            status_code=409,
            detail={"code": "context_tier_promotion_disabled", "reason": "BHM_CONTEXT_TIER_PROMOTION_ENABLED is not set"},
        )


def _context_tier_promotion_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ContextTierPromotionMigrationRequired):
        return HTTPException(status_code=409, detail={"code": "context_tier_promotion_migration_required"})
    if isinstance(exc, ContextTierPromotionDisabled):
        return HTTPException(status_code=409, detail={"code": "context_tier_promotion_disabled"})
    if isinstance(exc, ContextTierPromotionStale):
        return HTTPException(status_code=409, detail={"code": "context_tier_promotion_stale"})
    if isinstance(exc, ContextTierPromotionError):
        return HTTPException(status_code=422, detail={"code": "context_tier_promotion_invalid", "reason": _safe_exception_text(exc)})
    return HTTPException(status_code=503, detail={"code": "context_tier_promotion_unavailable"})


def _context_tier_promotion_project(principal: Any, project: str) -> str:
    if principal is None:
        raise HTTPException(status_code=401, detail={"code": "caller_auth_required"})
    canonical_project = rt._canonical_project(project)
    if error := authorize_projects(principal, (canonical_project,), require_explicit=True):
        raise HTTPException(status_code=403, detail={"code": error})
    return canonical_project


def _context_tier_promotion_rollback(
    request: ContextTierPromotionRollbackRequest,
    *,
    principal: Any,
) -> dict[str, Any]:
    """Run one revision-CAS rollback; the existing outbox owns projection."""

    _require_context_tier_promotion_enabled()
    project = _context_tier_promotion_project(principal, request.project)
    try:
        result = rollback_tier_promotion(
            database_path=app_core_hook._governed_consolidation_database_path(),
            project=project,
            candidate_id=request.candidate_id,
            apply=request.apply,
            confirmation=request.confirmation,
        )
    except (ContextTierPromotionError, OSError, ValueError) as exc:
        raise _context_tier_promotion_error(exc) from exc
    return {
        "project": project,
        "candidate_id": result.candidate_id,
        "status": result.status,
        "memory_id": result.memory_id,
        "outbox_event_id": result.outbox_event_id,
        "idempotent": result.idempotent,
        "deduplicated": result.deduplicated,
        "side_effects": {
            "sqlite_mutation": not result.idempotent,
            "memory_lifecycle_mutation": not result.idempotent,
            "memory_outbox_mutation": bool(result.outbox_event_id),
            "qdrant_mutation": False,
            "mem0_mutation": False,
            "projection": "existing_outbox_projector",
        },
    }


