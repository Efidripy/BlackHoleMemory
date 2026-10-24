"""Domain module app_retrieval: moved out of app.py (FIX-017). Cross-module calls go through module attributes."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Mapping, Sequence
from .app_retrieval_context import (
    _serialize_vector_hit, _context_item_from_vector_hit, _context_hit_from_authoritative_record,
    _compile_tiered_context_candidates,
 )
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from . import app_runtime as rt
from .app_runtime import ResponseTimeout, _ensure_provider_warmup_ready_sync, _run_bounded_read, _safe_exception_text  # noqa: F401
from .api_models import SearchRequest, MemoryAdvancedSearchRequest, ContextCompileRequest, AgentContextRequest, ContextTierPromotionRollbackRequest, RetrievalExplainRequest, UtilityFeedbackEventRequest, UnifiedContextCompileRequest, SearchByRefRequest, SearchByUpsertKeyRequest, HybridSearchRequest
from .config import settings
from .caller_auth import authorize_projects  # noqa: F401
from .mem0_adapter import global_collection_name
from .mem0_adapter import local_collection_name
from .context_compiler import MAX_CONTEXT_ITEM_CHARS
from .context_compiler import compile_context
from .context_profiles import resolve_context_profile
from .context_tier_promotion import ContextTierPromotionDisabled  # noqa: F401
from .context_tier_promotion import ContextTierPromotionError  # noqa: F401
from .context_tier_promotion import ContextTierPromotionMigrationRequired  # noqa: F401
from .context_tier_promotion import ContextTierPromotionStale  # noqa: F401
from .context_tier_promotion import rollback_tier_promotion  # noqa: F401
from .context_tier_promotion import runtime_enabled as context_tier_promotion_enabled  # noqa: F401
from .adaptive_profile import recommend_context_profile
from .adaptive_profile import summarize_explicit_usefulness
from .context_confidence import assess_context_confidence
from .feedback_consolidation import FeedbackConsolidationError  # noqa: F401
from .feedback_consolidation import build_feedback_consolidation_preview  # noqa: F401
from .retrieval_explanation import explain_retrieval_hit
from .runtime_storage import resolve_runtime_storage_config
from .memory_service import MemoryServiceNotReady  # noqa: F401
from .utility_feedback import UtilityEvent  # noqa: F401
from .utility_feedback_store import append_utility_event  # noqa: F401
from .memory_search_service import MemorySearchDependencies
from .memory_search_service import MemorySearchService
from .memory_search_service import read_only_side_effects
from .retrieval_scope import resolve_history_scope
from .observation_security import redact_secret_text
from .unified_context import UnifiedContextError
from .unified_context import build_unified_context_from_graph
from .unified_context import classify_context_item
from . import app_core_fallback
from . import app_core_graph
from . import app_core_hit
from . import app_core_hook
from . import app_core

from .app_retrieval_support import (
    _is_fallback_grace_error,
    _fallback_rank_records,  # noqa: F401
    _fallback_grace_mem0_search,
    _fallback_grace_memories_response,
    _emit_memory_pulses_from_records,
    _emit_memory_pulses_from_mem0_items,
    _authoritative_context_compile_request,
    _record_utility_feedback_event,
    _utility_feedback_consolidation_preview,
    _require_context_tier_promotion_enabled,  # noqa: F401
    _context_tier_promotion_error,  # noqa: F401
    _context_tier_promotion_project,  # noqa: F401
    _context_tier_promotion_rollback,
)

_LOGGER = logging.getLogger(__name__)

def _advanced_search_live_memories(request: MemoryAdvancedSearchRequest) -> tuple[list[dict], int]:
    app_core_fallback._require_typed_memory_boundary(
        memory_class=request.memory_class,
        event_role=request.event_role,
    )
    history = resolve_history_scope(
        request.history_scope,
        include_historical=request.include_historical,
        freshness_days=request.freshness_days,
    )
    project = app_core_fallback._effective_search_project(request.project)
    candidates = []
    query = (request.query or "").strip()
    for record in rt._load_live_memories():
        if not app_core_hit._memory_matches_filters(
            record,
            project=project,
            memory_type=request.memory_type,
            memory_class=request.memory_class,
            event_role=request.event_role,
            concepts=request.concepts,
            files=request.files,
            include_archived=request.include_archived,
            include_logs=request.include_logs,
            include_historical=history.include_historical,
            domain=request.domain,
            semantic_type=request.semantic_type,
            priority=request.priority,
            as_of=request.as_of,
            valid_from=request.valid_from,
            valid_to=request.valid_to,
            include_temporal_unknown=request.include_temporal_unknown,
            freshness_days=history.freshness_days,
        ):
            continue

        metadata = record.get("metadata") or {}
        item = {
            "id": record.get("source_id"),
            "memory": record.get("content") or "",
            "metadata": {
                "raw_title": metadata.get("raw_title"),
                "tags": record.get("tags") or [],
                "memory_type": record.get("memory_type"),
                "memory_class": app_core._effective_memory_class(record),
                "memory_class_source": (
                    record.get("memory_class_source")
                    or metadata.get("memory_class_source")
                    or app_core.MemoryClassSource.LEGACY_DEFAULT.value
                ),
                "event_role": app_core._effective_event_role(record),
                "event_role_version": (
                    record.get("event_role_version")
                    or metadata.get("event_role_version")
                    or app_core.EVENT_ROLE_SCHEMA_VERSION
                ),
                "project": record.get("project"),
                "files": metadata.get("files") or [],
                "archived_at": metadata.get("archived_at"),
                "lifecycle": metadata.get("lifecycle"),
                "domain": metadata.get("domain"),
                "semantic_type": metadata.get("semantic_type"),
                "priority": metadata.get("priority"),
            },
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "score": 0.0,
        }
        score = 0.0
        if query:
            score += app_core_hook._lexical_signal(query, item)
            score += app_core_hook._memory_type_weight(item)
            score += app_core_hook._query_intent_weight(query, item)
            if score <= 0:
                continue
        else:
            score = 1.0
        item["score"] = score
        candidates.append((score, record))

    candidates.sort(
        key=lambda pair: (
            pair[0],
            pair[1].get("updated_at") or pair[1].get("created_at") or "",
        ),
        reverse=True,
    )
    ordered = [record for _, record in candidates]
    total = len(ordered)
    start = max(request.offset, 0)
    end = start + max(min(request.limit, 200), 1)
    window = ordered[start:end]
    _emit_memory_pulses_from_records(window)
    return window, total


def _search_hybrid(request: HybridSearchRequest) -> dict:
    project = app_core_fallback._effective_search_project(request.project)
    advanced = _advanced_search_live_memories(
        MemoryAdvancedSearchRequest(
            query=request.query,
            project=project,
            memory_class=request.memory_class,
            event_role=request.event_role,
            as_of=request.as_of,
            valid_from=request.valid_from,
            valid_to=request.valid_to,
            include_temporal_unknown=request.include_temporal_unknown,
            domain=request.domain,
            semantic_type=request.semantic_type,
            priority=request.priority,
            include_archived=request.include_archived,
            include_logs=request.include_logs,
            limit=request.limit,
            offset=0,
        )
    )[0]
    suggestions = app_core_graph._query_suggestions(project)
    return {"memories": [app_core._serialize_memory_record(item) for item in advanced], "suggestions": suggestions[:5]}


def _search_by_source_ref(request: SearchByRefRequest) -> dict:
    project = app_core_fallback._effective_search_project(request.project)
    items = []
    for record in rt._load_live_memories():
        if not app_core_hit._memory_matches_filters(record, project=project, include_archived=False):
            continue
        refs = set(((record.get("metadata") or {}).get("source_refs") or []) + ((record.get("metadata") or {}).get("files") or []))
        if request.ref in refs:
            items.append(record)
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    return {"memories": [app_core._serialize_memory_record(item) for item in items[: max(min(request.limit, 200), 1)]], "ref": request.ref}


def _search_by_upsert_key(request: SearchByUpsertKeyRequest) -> dict:
    project = app_core_fallback._effective_search_project(request.project)
    accepted_projects = rt._project_aliases(project)
    items = []
    for record in rt._load_live_memories():
        if record.get("project") not in accepted_projects:
            continue
        if ((record.get("metadata") or {}).get("upsert_key")) == request.upsert_key:
            items.append(record)
    return {"memories": [app_core._serialize_memory_record(item) for item in items], "upsert_key": request.upsert_key}



async def bhm_unified_context_compile(request: UnifiedContextCompileRequest) -> dict[str, Any]:
    """Compile memory/code/convention/task/doc/ops channels without MCP schema drift."""

    await rt._ensure_provider_warmup_ready()
    project_name = rt._canonical_project(request.project)
    candidate_limit = min(max(int(request.limit) * 3, 20), 50)
    hits, total = await app_core_fallback.federated_search(request.query, project_name, limit=candidate_limit, include_archived=False, include_logs=False)
    strict_hits = app_core_fallback._strict_retrieval_hits(hits, project_name=project_name, include_archived=False, include_logs=False, limit=candidate_limit)
    buckets: dict[str, list[dict[str, Any]]] = {"memory": [], "tasks": [], "docs": [], "ops": []}
    for hit in strict_hits:
        item = _context_item_from_vector_hit(hit)
        bucket = classify_context_item(item)
        buckets.setdefault(bucket, buckets["memory"]).append(item)
    database_path = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path
    root_id = app_core_hook._code_graph_query_root_id(project_name, settings.repo_root)
    try:
        result = build_unified_context_from_graph(
            database_path,
            project=project_name,
            root_id=root_id,
            query=request.query,
            memory_items=buckets["memory"],
            task_items=buckets["tasks"],
            doc_items=buckets["docs"],
            ops_items=buckets["ops"],
            code_operation=request.code_operation,
            include_code=request.include_code,
            include_conventions=request.include_conventions,
            include_proposals=request.include_proposals,
            token_budget=request.token_budget,
            limit=request.limit,
            time_budget_ms=request.time_budget_ms,
        )
    except UnifiedContextError as exc:
        raise HTTPException(status_code=422, detail={"error": "unified_context_rejected", "detail": redact_secret_text(str(exc)).value[:500]}) from exc
    result["retrieval"] = {**(result.get("retrieval") or {}), "total": total, "candidate_count": len(hits), "eligible_count": len(strict_hits)}
    return result


def mem0_search(request: SearchRequest) -> dict:
    try:
        _ensure_provider_warmup_ready_sync()
        project_name = app_core_fallback._effective_search_project(request.project)
        if request.freshness_days is not None:
            memories, total = _advanced_search_live_memories(
                MemoryAdvancedSearchRequest(
                    query=request.query,
                    project=project_name,
                    memory_class=request.memory_class,
                    event_role=request.event_role,
                    include_archived=request.include_archived,
                    include_logs=request.include_logs,
                    include_historical=request.include_historical,
                    domain=request.domain,
                    semantic_type=request.semantic_type,
                    priority=request.priority,
                    freshness_days=request.freshness_days,
                    limit=request.top_k,
                )
            )
            results = [
                {
                    "id": item.get("source_id"),
                    "memory": item.get("content") or "",
                    "metadata": {**(item.get("metadata") or {}), "project": item.get("project")},
                    "score": 0.0,
                }
                for item in memories
            ]
            return {
                "ok": True,
                "result": {"results": results, "total": total},
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
                "retrieval": {"mode": "authoritative-freshness", "provider": "sqlite"},
            }
        hits, total = asyncio.run(
            app_core_fallback.federated_search(
                request.query,
                project_name,
                limit=request.top_k,
                memory_class=request.memory_class,
                event_role=request.event_role,
                domain=request.domain,
                semantic_type=request.semantic_type,
                priority=request.priority,
                include_archived=request.include_archived,
                include_logs=request.include_logs,
                include_historical=request.include_historical,
            )
        )
        result: dict = {"results": [_serialize_vector_hit(item) for item in hits], "total": total}
        _emit_memory_pulses_from_mem0_items(result["results"])
        return {
            "ok": True,
            "result": result,
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
            "retrieval": {
                "mode": "federated",
                "ranking": "rrf-hybrid",
                "local_collection": local_collection_name(project_name),
                "global_collection": global_collection_name(),
            },
        }
    except Exception as exc:  # broad: fallback-grace boundary converts provider outages to response
        _LOGGER.warning("Mem0 search failed (%s)", type(exc).__name__)
        if _is_fallback_grace_error(exc):
            return _fallback_grace_mem0_search(request, exc)
        raise


async def bhm_search_advanced(request: MemoryAdvancedSearchRequest) -> dict:
    project_name = app_core_fallback._effective_search_project(request.project)
    history = resolve_history_scope(
        request.history_scope,
        include_historical=request.include_historical,
        freshness_days=request.freshness_days,
    )
    try:
        memories, total = await _run_bounded_read(
            "bhm.search.advanced",
            _advanced_search_live_memories,
            request,
        )
        return {
            "memories": [app_core._serialize_memory_record(item) for item in memories],
            "total": total,
            "limit": max(min(request.limit, 200), 1),
            "offset": max(request.offset, 0),
            "query": request.query,
            "filters": {
                "project": request.project,
                "memory_type": request.memory_type,
                "memory_class": request.memory_class.value if request.memory_class else None,
                "event_role": request.event_role.value if request.event_role else None,
                "as_of": request.as_of,
                "valid_from": request.valid_from,
                "valid_to": request.valid_to,
                "include_temporal_unknown": request.include_temporal_unknown,
                "concepts": request.concepts or [],
                "files": request.files or [],
                "include_archived": request.include_archived,
                "include_logs": request.include_logs,
                "include_historical": history.include_historical,
                "domain": request.domain,
                "semantic_type": request.semantic_type,
                "priority": request.priority,
                "freshness_days": history.freshness_days,
                "history_scope": history.scope,
            },
            "side_effects": read_only_side_effects(),
        }
    except Exception as exc:  # broad: fallback-grace boundary converts provider outages to response
        _LOGGER.warning("Advanced search failed (%s)", type(exc).__name__)
        if _is_fallback_grace_error(exc):
            response = _fallback_grace_memories_response(
                "bhm.search.advanced",
                exc,
                project=project_name,
                memory_type=request.memory_type,
                memory_class=request.memory_class,
                event_role=request.event_role,
                as_of=request.as_of,
                valid_from=request.valid_from,
                valid_to=request.valid_to,
                include_temporal_unknown=request.include_temporal_unknown,
                concepts=request.concepts,
                files=request.files,
                query=request.query,
                include_logs=request.include_logs,
                include_historical=history.include_historical,
                domain=request.domain,
                semantic_type=request.semantic_type,
                priority=request.priority,
                freshness_days=history.freshness_days,
                include_archived=request.include_archived,
                limit=request.limit,
                offset=request.offset,
            )
            response["query"] = request.query
            response.setdefault("side_effects", read_only_side_effects())
            return response
        raise


async def bhm_search(request: MemoryAdvancedSearchRequest) -> dict:
    service = MemorySearchService(
        MemorySearchDependencies(
            ensure_provider_warmup_ready=rt._ensure_provider_warmup_ready,
            effective_search_project=app_core_fallback._effective_search_project,
            federated_search=app_core_fallback.federated_search,
            advanced_search=bhm_search_advanced,
            serialize_vector_hit=_serialize_vector_hit,
            emit_memory_pulses=_emit_memory_pulses_from_mem0_items,
            is_fallback_grace_error=_is_fallback_grace_error,
            fallback_grace_memories_response=_fallback_grace_memories_response,
            local_collection_name=local_collection_name,
            global_collection_name=global_collection_name,
        )
    )
    return await service.execute(request)


async def bhm_context_compile(
    request: ContextCompileRequest,
    http_request: Request,
) -> dict[str, Any]:
    """Compile a bounded, project-scoped context from the canonical retrieval path."""

    try:
        history = resolve_history_scope(
            request.history_scope,
            include_historical=request.include_historical,
            freshness_days=request.freshness_days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_safe_exception_text(exc)) from exc
    try:
        context_profile = resolve_context_profile(request.profile or settings.context_profile, repo_root=settings.repo_root)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_safe_exception_text(exc)) from exc
    effective_limit = request.limit if request.limit is not None else context_profile.limit
    effective_token_budget = request.token_budget if request.token_budget is not None else context_profile.token_budget
    effective_include_archived = request.include_archived or context_profile.include_archived
    effective_include_logs = request.include_logs or context_profile.include_logs
    project_name = rt._canonical_project(request.project or settings.qdrant_collection)
    candidate_limit = min(max(effective_limit, 20), 50)
    retrieval_mode = "federated"
    retrieval_provider = None
    if history.freshness_days is not None:
        # A freshness window is correctness-first: Qdrant/Mem0 may rank a
        # candidate, but only SQLite can prove that its current revision is in
        # the requested window. Avoid provider warmup entirely on this path.
        authoritative_request = _authoritative_context_compile_request(
            request,
            project=project_name,
            include_archived=effective_include_archived,
            include_logs=effective_include_logs,
            include_historical=history.include_historical,
            freshness_days=history.freshness_days,
            history_scope=history.scope,
            limit=candidate_limit,
        )
        authoritative_records, total = await _run_bounded_read(
            "bhm.context.compile.authoritative",
            _advanced_search_live_memories,
            authoritative_request,
        )
        hits = [
            _context_hit_from_authoritative_record(record)
            for record in authoritative_records
        ]
        retrieval_mode = "authoritative-freshness"
        retrieval_provider = "sqlite"
    else:
        try:
            await rt._ensure_provider_warmup_ready()
            hits, total = await app_core_fallback.federated_search(
                request.query,
                project_name,
                limit=candidate_limit,
                memory_type=request.memory_type,
                memory_class=request.memory_class,
                event_role=request.event_role,
                as_of=request.as_of,
                valid_from=request.valid_from,
                valid_to=request.valid_to,
                include_temporal_unknown=request.include_temporal_unknown,
                concepts=request.concepts,
                files=request.files,
                domain=request.domain,
                semantic_type=request.semantic_type,
                priority=request.priority,
                include_archived=effective_include_archived,
                include_logs=effective_include_logs,
                include_historical=history.include_historical,
            )
        except Exception as exc:  # broad: fallback-grace boundary preserves degraded retrieval response
            _LOGGER.warning("Context compilation failed (%s)", type(exc).__name__)
            if not _is_fallback_grace_error(exc):
                raise
            authoritative_request = _authoritative_context_compile_request(
                request,
                project=project_name,
                include_archived=effective_include_archived,
                include_logs=effective_include_logs,
                include_historical=history.include_historical,
                freshness_days=None,
                history_scope=history.scope,
                limit=candidate_limit,
            )
            authoritative_records, total = await _run_bounded_read(
                "bhm.context.compile.fallback",
                _advanced_search_live_memories,
                authoritative_request,
            )
            hits = [_context_hit_from_authoritative_record(record) for record in authoritative_records]
            retrieval_mode = "authoritative-fallback-grace"
            retrieval_provider = "sqlite"

    # Retrieval already applies these filters, but the compiler fails closed
    # once more so a degraded/monkeypatched provider cannot leak another
    # project or archived/log memory into an agent context.
    strict_hits = (
        hits
        if retrieval_mode != "federated"
        else app_core_fallback._strict_retrieval_hits(
            hits,
            project_name=project_name,
            memory_type=request.memory_type,
            memory_class=request.memory_class,
            event_role=request.event_role,
            as_of=request.as_of,
            valid_from=request.valid_from,
            valid_to=request.valid_to,
            include_temporal_unknown=request.include_temporal_unknown,
            concepts=request.concepts,
            files=request.files,
            domain=request.domain,
            semantic_type=request.semantic_type,
            priority=request.priority,
            include_archived=effective_include_archived,
            include_logs=effective_include_logs,
            include_historical=history.include_historical,
            limit=effective_limit,
        )
    )
    context_hits: Sequence[Mapping[str, Any]] = strict_hits
    tier_report: dict[str, Any] | None = None
    if request.tiered_context:
        context_hits, tier_report = _compile_tiered_context_candidates(
            strict_hits,
            token_budget=effective_token_budget,
        )
    compiled = compile_context(
        context_hits if request.tiered_context else [_context_item_from_vector_hit(hit) for hit in strict_hits],
        token_budget=effective_token_budget,
        max_item_chars=context_profile.max_item_chars,
    )
    funnel_snapshot = app_core_hook._RETRIEVAL_FUNNEL.snapshot()
    profile_recommendation = recommend_context_profile(
        request.query,
        requested_profile=request.profile,
        default_profile=settings.context_profile,
        historical_usefulness=summarize_explicit_usefulness(funnel_snapshot, project=project_name),
        filter_count=sum(
            value is not None
            for value in (
                request.memory_type,
                request.memory_class,
                request.event_role,
                request.as_of,
                request.valid_from,
                request.valid_to,
                request.include_temporal_unknown,
                request.concepts,
                request.files,
                request.domain,
                request.semantic_type,
                request.priority,
            )
        ),
    )
    context_confidence = assess_context_confidence(
        hits=strict_hits,
        included_count=int(compiled["included_count"]),
        citations=compiled["citations"],
        total_candidates=total,
        token_budget=int(compiled["token_budget"]),
        omitted_count=int(compiled["omissions"]["count"]),
    )
    funnel_counts = {
        "requested": len(hits),
        "eligible": len(strict_hits),
        "packed": int(compiled["included_count"]),
        "cited": len(compiled["citations"]),
    }
    app_core_hook._RETRIEVAL_FUNNEL.record_context(
        project=project_name,
        profile=context_profile.name,
        surface=app_core_hook._request_surface(http_request),
        requested_count=funnel_counts["requested"],
        eligible_count=funnel_counts["eligible"],
        packed_count=funnel_counts["packed"],
        cited_count=funnel_counts["cited"],
        item_ids=[citation.get("id") for citation in compiled["citations"]],
    )
    return {
        "context": compiled["text"],
        "citations": compiled["citations"],
        "provenance": compiled["provenance"],
        "omissions": compiled["omissions"],
        "query": request.query,
        "project": project_name,
        "filters": {
            "memory_type": request.memory_type,
            "memory_class": request.memory_class.value if request.memory_class else None,
            "event_role": request.event_role.value if request.event_role else None,
            "as_of": request.as_of,
            "valid_from": request.valid_from,
            "valid_to": request.valid_to,
            "include_temporal_unknown": request.include_temporal_unknown,
            "concepts": request.concepts or [],
            "files": request.files or [],
            "include_archived": effective_include_archived,
            "include_logs": effective_include_logs,
            "include_historical": history.include_historical,
            "history_scope": history.scope,
            "freshness_days": history.freshness_days,
            "domain": request.domain,
            "semantic_type": request.semantic_type,
            "priority": request.priority,
        },
        "profile": context_profile.as_dict(),
        **({"tiers": tier_report} if tier_report is not None else {}),
        "profile_recommendation": profile_recommendation,
        "context_confidence": context_confidence,
        "retrieval": {
            "mode": retrieval_mode,
            **({"provider": retrieval_provider} if retrieval_provider else {}),
            "total": total,
            "candidate_count": len(hits),
            "eligible_count": len(strict_hits),
            "included_count": compiled["included_count"],
            "omitted_count": compiled["omissions"]["count"],
            "limit": effective_limit,
            "funnel": funnel_counts,
        },
        "budget": {
            "token_budget": compiled["token_budget"],
            "estimated_tokens": compiled["estimated_tokens"],
            "truncated": compiled["truncated"],
        },
        "side_effects": read_only_side_effects(),
    }


_AGENT_CONTEXT_DEFAULT_QUERY = "current task checkpoint risks next validation"


_AGENT_CONTEXT_ARTIFACT_TEXT_LIMIT = 1_600


def _agent_context_text(value: Any, *, limit: int = _AGENT_CONTEXT_ARTIFACT_TEXT_LIMIT) -> str:
    """Return a bounded redacted string for the agent-only read package."""

    text = redact_secret_text(str(value or "")).value.strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 1)].rstrip() + "..."


def _agent_context_record(record: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    """Keep artifact output small and avoid opaque metadata/session internals."""

    result: dict[str, Any] = {}
    for field in fields:
        value = record.get(field)
        if isinstance(value, str):
            result[field] = _agent_context_text(value)
        elif isinstance(value, list):
            result[field] = [
                _agent_context_text(item, limit=320) if isinstance(item, str) else item
                for item in value[:12]
            ]
        else:
            result[field] = value
    return result


def _optional_agent_context_artifact(
    loader: Callable[[str], dict],
    serializer: Callable[[dict], dict],
    project: str,
    fields: Sequence[str],
) -> dict[str, Any] | None:
    """Missing optional task artifacts are absence, never a package failure."""

    try:
        record = loader(project)
        if not app_core_hook._project_matches(record.get("project"), project):
            return None
        return _agent_context_record(serializer(record), fields)
    except HTTPException as exc:
        if exc.status_code == 404:
            return None
        raise


def _agent_context_transport_snapshot() -> dict[str, Any]:
    """Expose only server-observable aggregate MCP state, never session IDs."""

    sessions = app_core._MCP_STREAMABLE_HTTP.contract_snapshot().get("sessions") or {}
    drift_count = max(int(sessions.get("contract_drift_count") or 0), 0)
    return {
        "server_observation": str(sessions.get("status") or "detached"),
        "attached_count": max(int(sessions.get("attached_count") or 0), 0),
        "pending_count": max(int(sessions.get("pending_count") or 0), 0),
        "active_count": max(int(sessions.get("active_count") or 0), 0),
        "contract_state": "aligned" if drift_count == 0 else "drift_detected",
        "client_tool_surface": "unverifiable_by_server",
        "configured_is_not_attach_proof": True,
    }


def _agent_context_compiler_items(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Reuse the canonical context packer with redacted SQLite-only candidates."""

    items: list[dict[str, Any]] = []
    for record in records:
        item = _context_item_from_vector_hit(_context_hit_from_authoritative_record(record))
        item["content"] = _agent_context_text(item.get("content"))
        item["title"] = _agent_context_text(item.get("title"), limit=240)
        items.append(item)
    return items


async def bhm_agent_context(request: AgentContextRequest, http_request: Request) -> dict[str, Any]:
    """Assemble task-aware agent context without provider warmup or state writes."""

    project = rt._canonical_project(request.project)
    query = _agent_context_text(request.query, limit=500) or _AGENT_CONTEXT_DEFAULT_QUERY
    query_source = "caller" if request.query and str(request.query).strip() else "default-current-task"
    retrieval_request = MemoryAdvancedSearchRequest(
        query=query,
        project=project,
        include_archived=False,
        include_logs=False,
        include_historical=False,
        history_scope="current",
        limit=request.retrieval_limit,
        offset=0,
    )
    records, total = await _run_bounded_read(
        "bhm.agent-context.sqlite-retrieval",
        _advanced_search_live_memories,
        retrieval_request,
    )
    # The authoritative search already applies these filters. Repeat them at
    # the assembly boundary so a future adapter or a test double cannot turn
    # this single-project package into a cross-project/history leak.
    records = [
        record
        for record in records
        if app_core_hit._memory_matches_filters(
            record,
            project=project,
            include_archived=False,
            include_logs=False,
            include_historical=False,
        )
    ]
    compiled = compile_context(
        _agent_context_compiler_items(records),
        token_budget=request.token_budget,
        max_item_chars=MAX_CONTEXT_ITEM_CHARS,
    )
    all_tasks, _ = app_core_hook._list_tasks(project, status=None, limit=50, offset=0)
    active_tasks = [
        item
        for item in all_tasks
        if app_core_hook._project_matches(item.get("project"), project)
        and str(item.get("status") or "open").strip().casefold() not in {"closed", "done", "archived"}
    ]
    principal = getattr(http_request.state, "bhm_caller_principal", None)
    if principal is None:
        raise HTTPException(status_code=401, detail={"code": "caller_auth_required"})

    return {
        "project": project,
        "request_authorized": True,
        "authorization": {
            "caller_id": str(principal.caller_id),
            "project": project,
            "caller_scope": "all_projects" if principal.all_projects else "scoped",
            "shared_read_feature_enabled": app_core_hook._shared_memory_read_enabled(),
            "shared_write": False,
            "shared_write_enabled": False,
        },
        "mcp_transport": _agent_context_transport_snapshot(),
        "task_state": {
            "task_context": _optional_agent_context_artifact(
                app_core_hook._get_task_context,
                app_core_hook._serialize_task_context_record,
                project,
                ("id", "project", "title", "current_task", "status", "pending_items", "guidance", "next_step", "files_touched", "created_at", "updated_at"),
            ),
            "latest_checkpoint": _optional_agent_context_artifact(
                app_core_hook._get_latest_checkpoint,
                app_core_hook._serialize_checkpoint_record,
                project,
                ("id", "project", "checkpoint_type", "title", "content", "done", "next", "checks", "risks", "concepts", "files", "created_at", "updated_at"),
            ),
            "risk_register": _optional_agent_context_artifact(
                app_core_hook._get_risk_register,
                app_core_hook._serialize_risk_register_record,
                project,
                ("id", "project", "title", "summary", "top_risks", "mitigations", "owner", "created_at", "updated_at"),
            ),
            "active_tasks": {
                "items": [
                    _agent_context_record(
                        app_core_hook._serialize_task_record(item),
                        ("task_id", "project", "title", "intent", "scope_in", "scope_out", "repo", "owner", "status", "files_touched", "done", "next", "checks", "risks", "decisions", "validation", "opened_at", "updated_at"),
                    )
                    for item in active_tasks[: request.active_task_limit]
                ],
                "total": len(active_tasks),
                "limit": request.active_task_limit,
                "excluded_statuses": ["closed", "done", "archived"],
            },
            "project_summary": _optional_agent_context_artifact(
                app_core._project_summary_get,
                lambda record: record,
                project,
                ("id", "title", "project", "type", "content", "concepts", "files", "source_system", "source_digest", "created_at", "updated_at", "lifecycle"),
            ),
        },
        "retrieval": {
            "source": "sqlite-authoritative",
            "mode": "authoritative-current",
            "query": query,
            "query_source": query_source,
            "history_scope": "current",
            "total": total,
            "candidate_count": len(records),
            "included_count": compiled["included_count"],
            "context": compiled["text"],
            "citations": compiled["citations"],
            "provenance": compiled["provenance"],
            "omissions": compiled["omissions"],
            "freshness": {"ordered_by": "lexical_score_then_updated_at", "as_of": None},
            "budget": {
                "token_budget": compiled["token_budget"],
                "estimated_tokens": compiled["estimated_tokens"],
                "truncated": compiled["truncated"],
            },
        },
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "writes_mem0": False,
            "writes_task_state": False,
            "model_started": False,
        },
    }


async def bhm_explain_retrieval(request: RetrievalExplainRequest) -> dict[str, Any]:
    """Explain bounded ranking signals without returning raw retrieval metadata."""

    await rt._ensure_provider_warmup_ready()
    project_name = rt._canonical_project(request.project or settings.qdrant_collection)
    candidate_limit = min(max(request.limit, 20), 50)
    hits, total = await app_core_fallback.federated_search(
        request.query,
        project_name,
        limit=candidate_limit,
        memory_type=request.memory_type,
        memory_class=request.memory_class,
        event_role=request.event_role,
        as_of=request.as_of,
        valid_from=request.valid_from,
        valid_to=request.valid_to,
        include_temporal_unknown=request.include_temporal_unknown,
        concepts=request.concepts,
        files=request.files,
        domain=request.domain,
        semantic_type=request.semantic_type,
        priority=request.priority,
        include_archived=request.include_archived,
        include_logs=request.include_logs,
        include_historical=request.include_historical,
    )
    strict_hits = app_core_fallback._strict_retrieval_hits(
        hits,
        project_name=project_name,
        memory_type=request.memory_type,
        memory_class=request.memory_class,
        event_role=request.event_role,
        as_of=request.as_of,
        valid_from=request.valid_from,
        valid_to=request.valid_to,
        include_temporal_unknown=request.include_temporal_unknown,
        concepts=request.concepts,
        files=request.files,
        domain=request.domain,
        semantic_type=request.semantic_type,
        priority=request.priority,
        include_archived=request.include_archived,
        include_logs=request.include_logs,
        include_historical=request.include_historical,
        limit=request.limit,
    )
    explanations = [explain_retrieval_hit(hit, rank=index) for index, hit in enumerate(strict_hits, start=1)]
    return {
        "query": request.query,
        "project": project_name,
        "results": explanations,
        "total": total,
        "retrieval": {
            "mode": "federated",
            "ranking": "weighted-fusion+mmr+typed-decay",
            "candidate_count": len(hits),
            "included_count": len(strict_hits),
            "limit": request.limit,
        },
        "filters": {
            "memory_type": request.memory_type,
            "memory_class": request.memory_class.value if request.memory_class else None,
            "event_role": request.event_role.value if request.event_role else None,
            "as_of": request.as_of,
            "valid_from": request.valid_from,
            "valid_to": request.valid_to,
            "include_temporal_unknown": request.include_temporal_unknown,
            "concepts": request.concepts or [],
            "files": request.files or [],
            "include_archived": request.include_archived,
            "include_logs": request.include_logs,
            "include_historical": request.include_historical,
            "domain": request.domain,
            "semantic_type": request.semantic_type,
            "priority": request.priority,
        },
        "side_effects": read_only_side_effects(),
    }


def bhm_search_hybrid(request: HybridSearchRequest) -> dict:
    return _search_hybrid(request)


def bhm_search_by_source_ref(request: SearchByRefRequest) -> dict:
    return _search_by_source_ref(request)


def bhm_search_by_upsert_key(request: SearchByUpsertKeyRequest) -> dict:
    return _search_by_upsert_key(request)


async def bhm_utility_feedback_record(
    request: UtilityFeedbackEventRequest,
    http_request: Request,
) -> dict[str, Any]:
    """Append an authenticated utility event; it never updates memory lifecycle."""

    return await rt._run_bounded_write(
        "bhm.utility_feedback.record",
        _record_utility_feedback_event,
        request,
        principal=getattr(http_request.state, "bhm_caller_principal", None),
    )


async def bhm_utility_feedback_report(
    project: str = Query(min_length=1, max_length=160),
    as_of: str = Query(min_length=20, max_length=64),
    half_life_days: float = Query(default=30.0, ge=0.25, le=3_650),
    min_samples: int = Query(default=3, ge=1, le=10_000),
) -> dict[str, Any]:
    """Return a deterministic, read-only utility report for one project."""

    return await _run_bounded_read(
        "bhm.utility_feedback.report",
        app_core_hook._utility_feedback_report,
        project=project,
        as_of=as_of,
        half_life_days=half_life_days,
        min_samples=min_samples,
    )


async def bhm_utility_feedback_consolidation_preview(
    project: str = Query(min_length=1, max_length=160),
    as_of: str = Query(min_length=20, max_length=64),
    half_life_days: float = Query(default=30.0, ge=0.25, le=3_650),
    min_samples: int = Query(default=3, ge=1, le=10_000),
    max_proposals: int = Query(default=64, ge=1, le=256),
) -> dict[str, Any]:
    """Return a caller-safe, proposal-only feedback consolidation worklist."""

    return await _run_bounded_read(
        "bhm.utility_feedback.consolidation_preview",
        _utility_feedback_consolidation_preview,
        project=project,
        as_of=as_of,
        half_life_days=half_life_days,
        min_samples=min_samples,
        max_proposals=max_proposals,
    )


async def bhm_context_tier_promotion_rollback(
    request: ContextTierPromotionRollbackRequest,
    http_request: Request,
) -> dict[str, Any]:
    """Admin-only exact-revision rollback for a durable tier promotion."""

    return await rt._run_bounded_write(
        "bhm.context_tier_promotion.rollback",
        _context_tier_promotion_rollback,
        request,
        principal=getattr(http_request.state, "bhm_caller_principal", None),
    )



def register(app) -> None:
    """Attach this module's routes to the application."""

    app.post("/bhm/context/unified/compile", include_in_schema=False)(bhm_unified_context_compile)
    app.post("/mem0/search")(mem0_search)
    app.post("/bhm/search/advanced")(bhm_search_advanced)
    app.post("/bhm/search")(bhm_search)
    app.post("/bhm/context/compile")(bhm_context_compile)
    app.post("/bhm/agent-context")(bhm_agent_context)
    app.post("/bhm/retrieval/explain")(bhm_explain_retrieval)
    app.post("/bhm/search/hybrid")(bhm_search_hybrid)
    app.post("/bhm/search/by-source-ref")(bhm_search_by_source_ref)
    app.post("/bhm/search/by-upsert-key")(bhm_search_by_upsert_key)
    app.post("/bhm/utility-feedback/event")(bhm_utility_feedback_record)
    app.get("/bhm/utility-feedback/report")(bhm_utility_feedback_report)
    app.get("/bhm/utility-feedback/consolidation-preview")(bhm_utility_feedback_consolidation_preview)
    app.post("/bhm/context-tier-promotion/rollback")(bhm_context_tier_promotion_rollback)
