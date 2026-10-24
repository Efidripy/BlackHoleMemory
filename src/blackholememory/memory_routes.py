"""Memory surface: /bhm/memory/* and /bhm/memories* (read, write, lifecycle, relations, artifacts).

Moved out of app.py (FIX-017). Shared app-level callables are reached through ``_deps`` (late bound,
configured once by app.py), so monkeypatching the app names keeps working.
Sub-operations partitioned into memory_routes_base, memory_routes_read, memory_routes_write.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request

from . import app_runtime as rt
from .api_models import (
    AliasResolveRequest,
    BatchAttachSourceRefsRequest,
    BatchLinkMemoriesRequest,
    BatchMemoryIdsRequest,
    BatchRestoreRequest,
    BatchUpsertMemoriesRequest,
    HardDeleteMemoryRequest,
    HardDeleteRestorePreviewRequest,
    MemoryAdvancedSearchRequest,
    MemoryAliasRequest,
    MemoryArchiveRequest,
    MemoryChangelogRequest,
    MemoryCompactionReportRequest,
    MemoryCompactRequest,
    MemoryConfidenceRequest,
    MemoryDetectRequest,
    MemoryDiffRequest,
    MemoryGcCandidatesRequest,
    MemoryLinkDeleteRequest,
    MemoryLinkRequest,
    MemoryLintRequest,
    MemoryMergePreviewRequest,
    MemoryMergeRequest,
    MemoryPinRequest,
    MemoryRedactRequest,
    MemoryReviewQueueRequest,
    MemorySchemaValidateRequest,
    MemorySourceRefsRequest,
    MemoryStalenessReportRequest,
    MemoryTimelineRequest,
    MemoryTriageQueueRequest,
    MemoryUpdateRequest,
    MemoryUpsertRequest,
    MemoryUsedRequest,
    MemoryVoteRequest,
    ProjectOnlyRequest,
    RestoreMemoryRequest,
    SecretScanRequest,
    SourceRefsDetachRequest,
    SourceRefsReplaceRequest,
    TypeMigrateRequest,
)
from .app_runtime import _run_bounded_read
from .config import settings

# Re-exports from memory_routes_base
from .memory_routes_base import (
    MemoryClass,  # noqa: F401
    MemoryEventRole,  # noqa: F401
    _access_updates_for_hits,  # noqa: F401
    _Deps,  # noqa: F401
    _deps,  # noqa: F401
    _schedule_vector_access_updates,  # noqa: F401
    _serialized_link_mutation,  # noqa: F401
    _update_vector_access_payloads,  # noqa: F401
    _update_vector_access_payloads_async,  # noqa: F401
    configure,  # noqa: F401
)

# Re-exports from memory_routes_read
from .memory_routes_read import (
    _list_archived_memories,  # noqa: F401
    _list_live_memories,  # noqa: F401
    _list_pinned_memories,  # noqa: F401
    _memory_changelog,  # noqa: F401
    _memory_compaction_report,  # noqa: F401
    _memory_diff,  # noqa: F401
    _memory_gc_candidates,  # noqa: F401
    _memory_merge_preview,  # noqa: F401
    _memory_restore_hard_deleted_preview,  # noqa: F401
    _memory_schema_validate,  # noqa: F401
    _memory_staleness_report,  # noqa: F401
    _memory_timeline,  # noqa: F401
    _memory_triage_queue,  # noqa: F401
    _memory_usage_stats,  # noqa: F401
    _secret_scan_existing_memories,  # noqa: F401
)

# Re-exports from memory_routes_write
from .memory_routes_write import (
    _add_semantic_dependency_links,  # noqa: F401
    _alias_resolve,  # noqa: F401
    _archive_live_memory,  # noqa: F401
    _attach_source_refs,  # noqa: F401
    _batch_archive_memories,  # noqa: F401
    _batch_attach_source_refs,  # noqa: F401
    _batch_delete_memories,  # noqa: F401
    _batch_link_memories,  # noqa: F401
    _batch_restore_memories,  # noqa: F401
    _batch_unlink_memories,  # noqa: F401
    _batch_upsert_memories,  # noqa: F401
    _compact_memory,  # noqa: F401
    _delete_memory_link,  # noqa: F401
    _detach_source_refs,  # noqa: F401
    _get_memory_links,  # noqa: F401
    _get_source_refs,  # noqa: F401
    _lint_memory,  # noqa: F401
    _memory_alias_add,  # noqa: F401
    _memory_alias_remove,  # noqa: F401
    _memory_type_migrate,  # noqa: F401
    _replace_source_refs,  # noqa: F401
    _semantic_dependency_target_id,  # noqa: F401
    _serialize_duplicate_candidate,  # noqa: F401
    _set_memory_confidence,  # noqa: F401
    _vote_memory_quality,  # noqa: F401
)

_LOGGER = logging.getLogger(__name__)


async def bhm_memories_list(
    project: str | None = None,
    memory_type: str | None = None,
    memory_class: MemoryClass | None = None,
    event_role: MemoryEventRole | None = None,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    include_archived: bool = False,
    include_historical: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    try:
        await rt._ensure_provider_warmup_ready()
        window, total, effective_limit, effective_offset = await _run_bounded_read(
            "bhm.memories",
            _list_live_memories,
            project=project,
            memory_type=memory_type,
            memory_class=memory_class,
            event_role=event_role,
            as_of=as_of,
            valid_from=valid_from,
            valid_to=valid_to,
            include_temporal_unknown=include_temporal_unknown,
            include_archived=include_archived,
            include_historical=include_historical,
            limit=limit,
            offset=offset,
        )
        return {
            "memories": [_deps._serialize_memory_record(item) for item in window],
            "total": total,
            "limit": effective_limit,
            "offset": effective_offset,
            "filters": {
                "project": project,
                "memory_type": memory_type,
                "memory_class": memory_class.value if memory_class else None,
                "event_role": event_role.value if event_role else None,
                "include_archived": include_archived,
                "include_historical": include_historical,
            },
        }
    except Exception as exc:  # broad: fallback-grace boundary converts provider outages to response
        _LOGGER.warning("Memory route failed (%s)", type(exc).__name__)
        if _deps._is_fallback_grace_error(exc):
            return _deps._fallback_grace_memories_response(
                "bhm.memories",
                exc,
                project=project,
                memory_type=memory_type,
                memory_class=memory_class,
                event_role=event_role,
                as_of=as_of,
                valid_from=valid_from,
                valid_to=valid_to,
                include_temporal_unknown=include_temporal_unknown,
                include_archived=include_archived,
                include_historical=include_historical,
                limit=limit,
                offset=offset,
            )
        raise


async def bhm_memory_update(request: MemoryUpdateRequest) -> dict:
    record = await rt._run_bounded_write("bhm.memory.update", _deps._update_live_memory, request)
    return {"success": True, "memory": _deps._serialize_memory_record(record)}


async def bhm_memory_archive(request: MemoryArchiveRequest) -> dict:
    record = await rt._run_bounded_write("bhm.memory.archive", _archive_live_memory, request)
    return {"success": True, "memory": _deps._serialize_memory_record(record)}


async def bhm_memory_used(
    request: MemoryUsedRequest,
) -> dict[str, Any]:
    """Record an explicit, bounded access signal for retrieved memories."""

    await rt._ensure_provider_warmup_ready()
    project_name = rt._canonical_project(request.project or settings.qdrant_collection)
    requested_ids = list(dict.fromkeys(str(memory_id or "").strip() for memory_id in request.ids))
    requested_ids = [memory_id for memory_id in requested_ids if memory_id]
    fetched_hits: list[dict] = []
    missing_ids: list[str] = []
    for memory_id in requested_ids:
        hit = await _deps._fetch_qdrant_hit_by_source_id(memory_id, project_name)
        if hit is None:
            missing_ids.append(memory_id)
            continue
        strict_hits = _deps._strict_retrieval_hits(
            [hit],
            project_name=project_name,
            include_archived=False,
            include_logs=False,
            limit=1,
        )
        if not strict_hits:
            missing_ids.append(memory_id)
            continue
        fetched_hits.extend(strict_hits)

    accessed_at = rt._utc_now_iso()
    updates = _access_updates_for_hits(fetched_hits, accessed_at)
    if updates:
        _schedule_vector_access_updates(fetched_hits)
    used_ids = [
        str((hit.get("metadata") or {}).get("source_id") or hit.get("source_id") or hit.get("id") or "")
        for hit in fetched_hits
    ]
    funnel_matched_count = _deps._RETRIEVAL_FUNNEL().record_memory_used(
        project=project_name,
        item_ids=used_ids,
    )
    return {
        "success": True,
        "project": project_name,
        "requested_count": len(requested_ids),
        "used_count": len(used_ids),
        "scheduled_count": len(updates),
        "used_ids": list(dict.fromkeys(memory_id for memory_id in used_ids if memory_id)),
        "missing_ids": missing_ids,
        "accessed_at": accessed_at,
        "reason": request.reason,
        "funnel_matched_count": funnel_matched_count,
        "side_effects": {
            "read_only": False,
            "sqlite_mutation": False,
            "qdrant_mutation": bool(updates),
            "projection_mutation": bool(updates),
            "projection_update": "explicit-access-feedback",
        },
    }


async def bhm_memory_upsert(request: MemoryUpsertRequest, http_request: Request) -> dict:
    _deps._admit_new_content(
        ingress=_deps._request_ingress(http_request),
        project=request.project,
        actor=_deps._request_actor(http_request),
        payload=request.model_dump(mode="json"),
        requires_authority=True,
    )
    action, record = await rt._run_bounded_write("bhm.memory.upsert", _deps._upsert_live_memory, request)
    semantic_graph = await _add_semantic_dependency_links(record, request.project)
    return {
        "success": True,
        "action": action,
        "memory": _deps._serialize_memory_record(record),
        "semantic_graph": semantic_graph,
    }


async def bhm_memory_merge(request: MemoryMergeRequest) -> dict:
    result = await rt._run_bounded_write("bhm.memory.merge", _deps._merge_memories, request)
    return {
        "success": True,
        "target": _deps._serialize_memory_record(result["target"]),
        "source": _deps._serialize_memory_record(result["source"]),
        "archived_source": result["archived_source"],
    }


def bhm_memory_detect_duplicates(request: MemoryDetectRequest) -> dict:
    duplicates = _deps._detect_duplicates(request)
    return {"duplicates": [_serialize_duplicate_candidate(item) for item in duplicates], "limit": max(min(request.limit, 200), 1)}


def bhm_memory_detect_conflicts(request: MemoryDetectRequest) -> dict:
    conflicts = _deps._detect_conflicts(request)
    return {"conflicts": [_deps._serialize_conflict_candidate(item) for item in conflicts], "limit": max(min(request.limit, 200), 1)}


def bhm_memory_lint(request: MemoryLintRequest) -> dict:
    return _lint_memory(request)


async def bhm_memory_delete_hard(request: HardDeleteMemoryRequest) -> dict:
    deleted = await rt._run_bounded_write("bhm.memory.delete-hard", _deps._delete_live_memory_hard, request)
    return {"success": True, "memory": _deps._serialize_memory_record(deleted)}


async def bhm_memories_batch_archive(request: BatchMemoryIdsRequest) -> dict:
    return await rt._run_bounded_write("bhm.memories.batch-archive", _batch_archive_memories, request)


async def bhm_memories_batch_delete(request: BatchMemoryIdsRequest) -> dict:
    return await rt._run_bounded_write("bhm.memories.batch-delete", _batch_delete_memories, request)


async def bhm_memories_batch_link(request: BatchLinkMemoriesRequest) -> dict:
    return await rt._run_bounded_write("bhm.memories.batch-link", _batch_link_memories, request)


async def bhm_memories_batch_unlink(request: BatchMemoryIdsRequest) -> dict:
    return await rt._run_bounded_write("bhm.memories.batch-unlink", _batch_unlink_memories, request)


def bhm_memories_by_concept(
    concept: str,
    project: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    request = MemoryAdvancedSearchRequest(
        query="",
        project=project,
        concepts=[concept],
        limit=limit,
        offset=offset,
    )
    memories, total = _deps._advanced_search_live_memories(request)
    return {
        "memories": [_deps._serialize_memory_record(item) for item in memories],
        "total": total,
        "concept": concept,
        "limit": max(min(limit, 200), 1),
        "offset": max(offset, 0),
    }


def bhm_memories_by_type(
    memory_type: str,
    project: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    items = [
        item for item in rt._load_live_memories()
        if _deps._memory_matches_filters(item, project=project, memory_type=memory_type, include_archived=False)
    ]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    window = items[max(offset, 0):max(offset, 0) + max(min(limit, 200), 1)]
    return {
        "memories": [_deps._serialize_memory_record(item) for item in window],
        "total": total,
        "memory_type": memory_type,
        "limit": max(min(limit, 200), 1),
        "offset": max(offset, 0),
    }


async def bhm_memory_set_confidence(request: MemoryConfidenceRequest) -> dict:
    record = await rt._run_bounded_write("bhm.memory.confidence", _set_memory_confidence, request)
    return {"success": True, "memory": _deps._serialize_memory_record(record)}


async def bhm_memory_pin(request: MemoryPinRequest) -> dict:
    record = await rt._run_bounded_write("bhm.memory.pin", _deps._set_memory_pin, request)
    return {"success": True, "memory": _deps._serialize_memory_record(record)}


async def bhm_memory_vote_quality(request: MemoryVoteRequest) -> dict:
    record = await rt._run_bounded_write("bhm.memory.vote-quality", _vote_memory_quality, request)
    return {"success": True, "memory": _deps._serialize_memory_record(record)}


def bhm_memories_pinned(
    project: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    memories, total = _list_pinned_memories(project=project, limit=limit, offset=offset)
    return {
        "memories": [_deps._serialize_memory_record(item) for item in memories],
        "total": total,
        "limit": max(min(limit, 200), 1),
        "offset": max(offset, 0),
    }


def bhm_memories_archived(project: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    memories, total = _list_archived_memories(project, limit, offset)
    return {
        "memories": [_deps._serialize_memory_record(item) for item in memories],
        "total": total,
        "limit": max(min(limit, 200), 1),
        "offset": max(offset, 0),
    }


async def bhm_memory_source_refs_attach(request: MemorySourceRefsRequest) -> dict:
    record = await rt._run_bounded_write("bhm.memory.source-refs", _attach_source_refs, request)
    return {"success": True, "memory": _deps._serialize_memory_record(record)}


def bhm_memory_source_refs_get(id: str, project: str | None = None) -> dict:
    return _get_source_refs(id, project)


def bhm_memory_timeline(request: MemoryTimelineRequest) -> dict:
    items = _memory_timeline(request)
    return {
        "timeline": [_deps._serialize_memory_record(item) for item in items],
        "limit": max(min(request.limit, 200), 1),
        "filters": {
            "project": request.project,
            "concept": request.concept,
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


async def bhm_memory_source_refs_replace(request: SourceRefsReplaceRequest) -> dict:
    record = await rt._run_bounded_write("bhm.memory.source-refs.replace", _replace_source_refs, request)
    return {"success": True, "memory": _deps._serialize_memory_record(record)}


async def bhm_memory_source_refs_detach(request: SourceRefsDetachRequest) -> dict:
    record = await rt._run_bounded_write("bhm.memory.source-refs.detach", _detach_source_refs, request)
    return {"success": True, "memory": _deps._serialize_memory_record(record)}


async def bhm_memory_restore(request: RestoreMemoryRequest) -> dict:
    record = await rt._run_bounded_write("bhm.memory.restore", _deps._restore_archived_memory, request)
    return {"success": True, "memory": _deps._serialize_memory_record(record)}


async def bhm_memories_batch_upsert(request: BatchUpsertMemoriesRequest, http_request: Request) -> dict:
    scope = request.project or next((item.project for item in request.items if item.project), None) or rt._canonical_project(None)
    _deps._admit_new_content(
        ingress=_deps._request_ingress(http_request),
        project=scope,
        actor=_deps._request_actor(http_request),
        payload=request.model_dump(mode="json"),
        requires_authority=True,
    )
    return await rt._run_bounded_write("bhm.memories.batch-upsert", _batch_upsert_memories, request)


async def bhm_memory_source_refs_batch(request: BatchAttachSourceRefsRequest) -> dict:
    return await rt._run_bounded_write("bhm.memory.source-refs.batch", _batch_attach_source_refs, request)


def bhm_memory_diff(request: MemoryDiffRequest) -> dict:
    return _memory_diff(request)


def bhm_memory_compact(request: MemoryCompactRequest) -> dict:
    record = _compact_memory(request)
    return {"success": True, "memory": _deps._serialize_memory_record(record)}


def bhm_memory_schema_validate(request: MemorySchemaValidateRequest) -> dict:
    return _memory_schema_validate(request.id, request.project)


def bhm_memory_type_migrate(request: TypeMigrateRequest) -> dict:
    record = _memory_type_migrate(request)
    return {"success": True, "memory": _deps._serialize_memory_record(record)}


def bhm_memory_restore_batch(request: BatchRestoreRequest) -> dict:
    return _batch_restore_memories(request)


def bhm_memory_staleness_report(request: MemoryStalenessReportRequest) -> dict:
    return _memory_staleness_report(request)


def bhm_memory_review_queue(request: MemoryReviewQueueRequest) -> dict:
    return _deps._memory_review_queue(request)


def bhm_memory_triage_queue(request: MemoryTriageQueueRequest) -> dict:
    return _memory_triage_queue(request)


def bhm_memory_merge_preview(request: MemoryMergePreviewRequest) -> dict:
    return _memory_merge_preview(request)


def bhm_memory_redact(request: MemoryRedactRequest) -> dict:
    return _deps._memory_redact(request)


def bhm_secret_scan_existing_memories(request: SecretScanRequest) -> dict:
    return _secret_scan_existing_memories(request)


def bhm_memory_alias_add(request: MemoryAliasRequest) -> dict:
    record = _memory_alias_add(request)
    return {"success": True, "memory": _deps._serialize_memory_record(record)}


def bhm_memory_alias_remove(request: MemoryAliasRequest) -> dict:
    record = _memory_alias_remove(request)
    return {"success": True, "memory": _deps._serialize_memory_record(record)}


def bhm_alias_resolve(request: AliasResolveRequest) -> dict:
    return _alias_resolve(request)


def bhm_memory_usage_stats(request: ProjectOnlyRequest) -> dict:
    return _memory_usage_stats(request.project)


def bhm_memory_restore_hard_deleted_preview(request: HardDeleteRestorePreviewRequest) -> dict:
    return _memory_restore_hard_deleted_preview(request)


def bhm_memory_gc_candidates(request: MemoryGcCandidatesRequest) -> dict:
    return _memory_gc_candidates(request)


def bhm_memory_compaction_report(request: MemoryCompactionReportRequest) -> dict:
    return _memory_compaction_report(request)


def bhm_memory_changelog(request: MemoryChangelogRequest) -> dict:
    return _memory_changelog(request)


def bhm_memory_normalize_metadata(request: ProjectOnlyRequest) -> dict:
    return _deps._normalize_memory_metadata(request.project)


def bhm_memory_links_get(id: str, project: str) -> dict:
    links = _get_memory_links(id, project)
    return {"links": [_deps._serialize_memory_link(item) for item in links]}


async def bhm_memory_link_create(request: MemoryLinkRequest) -> dict:
    link = await rt._run_bounded_write("bhm.memory.link", _deps._create_memory_link, request)
    return {"success": True, "link": _deps._serialize_memory_link(link)}


async def bhm_memory_link_delete(request: MemoryLinkDeleteRequest) -> dict:
    deleted = await rt._run_bounded_write("bhm.memory.link.delete", _delete_memory_link, request)
    return {"success": True, "deleted": deleted}


def register(app) -> None:
    """Attach the moved routes to the application at their original position."""

    app.get("/bhm/memories")(bhm_memories_list)
    app.post("/bhm/memory/update")(bhm_memory_update)
    app.post("/bhm/memory/archive")(bhm_memory_archive)
    app.post("/bhm/memory/used")(bhm_memory_used)
    app.post("/bhm/memory/upsert")(bhm_memory_upsert)
    app.post("/bhm/memory/merge")(bhm_memory_merge)
    app.post("/bhm/memory/detect-duplicates")(bhm_memory_detect_duplicates)
    app.post("/bhm/memory/detect-conflicts")(bhm_memory_detect_conflicts)
    app.post("/bhm/memory/lint")(bhm_memory_lint)
    app.delete("/bhm/memory/hard")(bhm_memory_delete_hard)
    app.post("/bhm/memories/batch-archive")(bhm_memories_batch_archive)
    app.post("/bhm/memories/batch-delete")(bhm_memories_batch_delete)
    app.post("/bhm/memories/batch-link")(bhm_memories_batch_link)
    app.post("/bhm/memories/batch-unlink")(bhm_memories_batch_unlink)
    app.get("/bhm/memories/by-concept")(bhm_memories_by_concept)
    app.get("/bhm/memories/by-type")(bhm_memories_by_type)
    app.post("/bhm/memory/confidence")(bhm_memory_set_confidence)
    app.post("/bhm/memory/pin")(bhm_memory_pin)
    app.post("/bhm/memory/vote-quality")(bhm_memory_vote_quality)
    app.get("/bhm/memories/pinned")(bhm_memories_pinned)
    app.get("/bhm/memories/archived")(bhm_memories_archived)
    app.post("/bhm/memory/source-refs")(bhm_memory_source_refs_attach)
    app.get("/bhm/memory/source-refs")(bhm_memory_source_refs_get)
    app.post("/bhm/memory/timeline")(bhm_memory_timeline)
    app.post("/bhm/memory/source-refs/replace")(bhm_memory_source_refs_replace)
    app.post("/bhm/memory/source-refs/detach")(bhm_memory_source_refs_detach)
    app.post("/bhm/memory/restore")(bhm_memory_restore)
    app.post("/bhm/memories/batch-upsert")(bhm_memories_batch_upsert)
    app.post("/bhm/memory/source-refs/batch")(bhm_memory_source_refs_batch)
    app.post("/bhm/memory/diff")(bhm_memory_diff)
    app.post("/bhm/memory/compact")(bhm_memory_compact)
    app.post("/bhm/memory/schema-validate")(bhm_memory_schema_validate)
    app.post("/bhm/memory/type-migrate")(bhm_memory_type_migrate)
    app.post("/bhm/memory/restore-batch")(bhm_memory_restore_batch)
    app.post("/bhm/memory/staleness-report")(bhm_memory_staleness_report)
    app.post("/bhm/memory/review-queue")(bhm_memory_review_queue)
    app.post("/bhm/memory/triage-queue")(bhm_memory_triage_queue)
    app.post("/bhm/memory/merge-preview")(bhm_memory_merge_preview)
    app.post("/bhm/memory/redact")(bhm_memory_redact)
    app.post("/bhm/memory/secret-scan")(bhm_secret_scan_existing_memories)
    app.post("/bhm/memory/alias/add")(bhm_memory_alias_add)
    app.post("/bhm/memory/alias/remove")(bhm_memory_alias_remove)
    app.post("/bhm/memory/alias/resolve")(bhm_alias_resolve)
    app.post("/bhm/memory/usage-stats")(bhm_memory_usage_stats)
    app.post("/bhm/memory/restore-hard-deleted-preview")(bhm_memory_restore_hard_deleted_preview)
    app.post("/bhm/memory/gc-candidates")(bhm_memory_gc_candidates)
    app.post("/bhm/memory/compaction-report")(bhm_memory_compaction_report)
    app.post("/bhm/memory/changelog")(bhm_memory_changelog)
    app.post("/bhm/memory/normalize-metadata")(bhm_memory_normalize_metadata)
    app.get("/bhm/memory/links")(bhm_memory_links_get)
    app.post("/bhm/memory/link")(bhm_memory_link_create)
    app.delete("/bhm/memory/link")(bhm_memory_link_delete)

