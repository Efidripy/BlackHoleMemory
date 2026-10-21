"""Memory surface: /bhm/memory/* and /bhm/memories* (read, write, lifecycle, relations, artifacts).

Moved out of app.py (FIX-017). Shared app-level callables are reached through ``_deps`` (late bound,
configured once by app.py), so monkeypatching the app names keeps working.
"""

from __future__ import annotations

import asyncio
import copy
import functools
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping
from fastapi import HTTPException
from fastapi import Request
from . import app_runtime as rt
from .app_runtime import _run_bounded_read, _safe_exception_text
from .api_models import MemoryUpdateRequest, MemoryArchiveRequest, MemoryAdvancedSearchRequest, MemoryUsedRequest, MemoryUpsertRequest, MemoryLinkRequest, MemoryLinkDeleteRequest, MemoryMergeRequest, MemoryDetectRequest, MemoryLintRequest, MemoryDeleteRequest, MemoryConfidenceRequest, MemoryPinRequest, MemoryVoteRequest, MemorySourceRefsRequest, MemoryTimelineRequest, HardDeleteMemoryRequest, BatchUpsertMemoriesRequest, BatchLinkMemoriesRequest, BatchAttachSourceRefsRequest, SourceRefsReplaceRequest, SourceRefsDetachRequest, RestoreMemoryRequest, BatchMemoryIdsRequest, RelationSuggestRequest, MemoryCompactRequest, MemoryDiffRequest, BatchRestoreRequest, MemoryStalenessReportRequest, MemoryReviewQueueRequest, MemoryTriageQueueRequest, MemoryMergePreviewRequest, MemoryRedactRequest, SecretScanRequest, MemoryAliasRequest, AliasResolveRequest, ProjectOnlyRequest, MemorySchemaValidateRequest, MemoryGcCandidatesRequest, MemoryCompactionReportRequest, MemoryChangelogRequest, HardDeleteRestorePreviewRequest, TypeMigrateRequest
from .config import settings
from .mem0_adapter import get_qdrant_client
from .mem0_adapter import initial_decay_metadata
from .mem0_adapter import normalize_access_count
from .lifecycle_suggestions import build_lifecycle_suggestions
from . import memory_contracts as _memory_contracts
from .observation_security import redact_secret_text


MemoryClass = _memory_contracts.MemoryClass
MemoryEventRole = _memory_contracts.MemoryEventRole


class _Deps:
    """Late-bound shared callables supplied by app.py."""

    def __getattr__(self, name: str):
        raise RuntimeError(f"{__name__}: dependency {name!r} is not configured")


_deps = _Deps()


def configure(**callables) -> None:
    for key, value in callables.items():
        setattr(_deps, key, value)


def _serialized_link_mutation(func):
    """Hold the link lock across load->save: replace_links deletes links missing from a stale snapshot."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with rt._LINK_STORE_LOCK:
            return func(*args, **kwargs)

    return wrapper


def _serialize_duplicate_candidate(item: dict) -> dict:
    return {
        "left_id": item.get("left_id"),
        "right_id": item.get("right_id"),
        "project": item.get("project"),
        "score": item.get("score"),
        "reason": item.get("reason"),
        "left_title": item.get("left_title"),
        "right_title": item.get("right_title"),
    }


def _get_memory_links(memory_id: str, project: str) -> list[dict]:
    if rt._find_live_memory(memory_id, project) is None:
        raise HTTPException(status_code=404, detail="memory not found")
    accepted_projects = rt._project_aliases(project)
    links = [
        item for item in rt._load_memory_links()
        if item.get("project") in accepted_projects
        and (item.get("source_id") == memory_id or item.get("target_id") == memory_id)
    ]
    links.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    return links


def _list_live_memories(
    *,
    project: str | None,
    memory_type: str | None,
    memory_class: MemoryClass | str | None = None,
    event_role: MemoryEventRole | str | None = None,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    include_archived: bool,
    include_historical: bool = False,
    limit: int,
    offset: int,
) -> tuple[list[dict], int, int, int]:
    _deps._require_typed_memory_boundary(memory_class=memory_class, event_role=event_role)
    _deps._require_temporal_memory_boundary(
        as_of=as_of,
        valid_from=valid_from,
        valid_to=valid_to,
        include_temporal_unknown=include_temporal_unknown,
    )
    items = [
        item
        for item in rt._load_live_memories()
        if _deps._memory_matches_filters(
            item,
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
        )
    ]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    effective_limit = max(min(limit, 200), 1)
    effective_offset = max(offset, 0)
    return items[effective_offset : effective_offset + effective_limit], total, effective_limit, effective_offset


def _archive_live_memory(request: MemoryArchiveRequest) -> dict:
    if rt._memory_store_is_authoritative():
        item = rt._find_live_memory(request.id, request.project)
        if item is None:
            raise HTTPException(status_code=404, detail="memory not found in live store")
        item = copy.deepcopy(item)
        now = rt._utc_now_iso()
        metadata = item.setdefault("metadata", {})
        metadata["archived_at"] = now
        metadata["archive_reason"] = request.reason
        item["updated_at"] = now
        _deps._append_memory_changelog(item, "archive", {"reason": request.reason})
        _deps._save_live_memories([item])
        rt._emit_memory_pulse(item.get("source_id"), str(item.get("project") or ""))
        return item

    live_records = rt._load_live_memories()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    accepted_projects = rt._project_aliases(request.project)

    for item in live_records:
        if item.get("source_id") != request.id:
            continue
        if request.project and item.get("project") not in accepted_projects:
            continue

        metadata = item.setdefault("metadata", {})
        metadata["archived_at"] = now
        metadata["archive_reason"] = request.reason
        item["updated_at"] = now
        _deps._append_memory_changelog(item, "archive", {"reason": request.reason})
        _deps._save_live_memories(live_records)
        rt._emit_memory_pulse(item.get("source_id"), str(item.get("project") or ""))
        return item

    raise HTTPException(status_code=404, detail="memory not found in live store")


async def _semantic_dependency_target_id(project_name: str, source_id: str, keyword: str) -> str | None:
    search_kwargs = {
        "limit": 5,
        "offset": 0,
        "include_archived": False,
        "include_logs": False,
        "include_graph_expansion": False,
    }
    for memory_type in ("knowledge-crystal", None):
        hits, _total = await _deps.federated_search(
            keyword,
            project_name,
            memory_type=memory_type,
            **search_kwargs,
        )
        for hit in hits:
            candidate_id = _deps._semantic_graph_node_id(hit)
            if candidate_id and candidate_id != source_id:
                return candidate_id
    return None


async def _add_semantic_dependency_links(record: dict, project_name: str) -> dict[str, Any]:
    source_id = str(record.get("source_id") or "").strip()
    metadata = record.get("metadata") or {}
    dependencies = _deps._normalize_linked_dependencies(metadata.get("linked_dependencies"))
    if not source_id or not dependencies:
        return {"created": [], "errors": []}

    created: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    for dependency in dependencies:
        keyword = dependency["target_core_insight_keyword"]
        edge_type = dependency["edge_type"]
        try:
            target_id = await _semantic_dependency_target_id(project_name, source_id, keyword)
            if not target_id:
                errors.append({"keyword": keyword, "edge_type": edge_type, "error": "target_not_found"})
                continue
            # SQLite is the only relation store; the shared route enforces live endpoints and ontology admission.
            await rt._run_bounded_write(
                "bhm.memory.link",
                _deps._create_memory_link,
                MemoryLinkRequest(
                    source_id=source_id,
                    target_id=target_id,
                    relation=edge_type.lower(),
                    project=project_name,
                ),
            )
            created.append({"source_id": source_id, "target_id": target_id, "edge_type": edge_type, "keyword": keyword})
        except Exception as exc:
            errors.append({"keyword": keyword, "edge_type": edge_type, "error": _safe_exception_text(exc)})
    return {"created": created, "errors": errors}


@_serialized_link_mutation
def _delete_memory_link(request: MemoryLinkDeleteRequest) -> bool:
    project = rt._canonical_project(request.project)
    links = rt._load_memory_links()
    remaining = [
        item for item in links
        if not (
            item.get("project") == project
            and item.get("source_id") == request.source_id
            and item.get("target_id") == request.target_id
            and item.get("relation") == request.relation
        )
    ]
    rt._save_memory_links(remaining)
    return len(remaining) != len(links)


def _lint_memory(request: MemoryLintRequest) -> dict:
    record = rt._find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")

    issues = _deps._lint_memory_record(record)

    return {
        "memory": _deps._serialize_memory_record(record),
        "issues": issues,
        "ok": not any(item["severity"] == "error" for item in issues),
    }


def _set_memory_confidence(request: MemoryConfidenceRequest) -> dict:
    if request.confidence < 0 or request.confidence > 1:
        raise HTTPException(status_code=400, detail="confidence must be between 0 and 1")
    record = rt._find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    record.setdefault("metadata", {})["confidence"] = request.confidence
    record["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _deps._append_memory_changelog(record, "set_confidence", {"confidence": request.confidence})
    _deps._replace_live_memory(record)
    return record


def _vote_memory_quality(request: MemoryVoteRequest) -> dict:
    if request.vote < 1 or request.vote > 5:
        raise HTTPException(status_code=400, detail="vote must be between 1 and 5")
    record = rt._find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    metadata = record.setdefault("metadata", {})
    votes = list(metadata.get("quality_votes") or [])
    votes.append({"voter": request.voter, "vote": request.vote})
    metadata["quality_votes"] = votes
    metadata["quality_score"] = round(sum(item["vote"] for item in votes) / len(votes), 3)
    record["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _deps._append_memory_changelog(record, "vote_quality", {"vote": request.vote, "voter": request.voter, "quality_score": metadata["quality_score"]})
    _deps._replace_live_memory(record)
    return record


def _attach_source_refs(request: MemorySourceRefsRequest) -> dict:
    record = rt._find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    metadata = record.setdefault("metadata", {})
    existing = list(metadata.get("source_refs") or [])
    metadata["source_refs"] = list(dict.fromkeys(existing + request.refs))
    record["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _deps._replace_live_memory(record)
    return record


def _get_source_refs(id: str, project: str | None = None) -> dict:
    record = rt._find_live_memory(id, project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    metadata = record.get("metadata") or {}
    return {"memory": _deps._serialize_memory_record(record), "source_refs": metadata.get("source_refs") or []}


def _replace_source_refs(request: SourceRefsReplaceRequest) -> dict:
    record = rt._find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    metadata = record.setdefault("metadata", {})
    metadata["source_refs"] = list(dict.fromkeys(request.refs))
    record["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _deps._replace_live_memory(record)
    return record


def _detach_source_refs(request: SourceRefsDetachRequest) -> dict:
    record = rt._find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    metadata = record.setdefault("metadata", {})
    existing = list(metadata.get("source_refs") or [])
    metadata["source_refs"] = [item for item in existing if item not in set(request.refs)]
    record["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _deps._replace_live_memory(record)
    return record


def _list_pinned_memories(project: str | None = None, limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
    items = [
        item for item in rt._load_live_memories()
        if _deps._memory_matches_filters(item, project=project, include_archived=False)
        and bool((item.get("metadata") or {}).get("pinned"))
    ]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    start = max(offset, 0)
    end = start + max(min(limit, 200), 1)
    return items[start:end], total


def _memory_timeline(request: MemoryTimelineRequest) -> list[dict]:
    _deps._require_typed_memory_boundary(
        memory_class=request.memory_class,
        event_role=request.event_role,
    )
    items = [
        item for item in rt._load_live_memories()
        if _deps._memory_matches_filters(
            item,
            project=request.project,
            memory_type=request.memory_type,
            memory_class=request.memory_class,
            event_role=request.event_role,
            concepts=[request.concept] if request.concept else None,
            as_of=request.as_of,
            valid_from=request.valid_from,
            valid_to=request.valid_to,
            include_temporal_unknown=request.include_temporal_unknown,
            include_archived=request.include_archived,
        )
    ]
    items.sort(key=lambda item: item.get("created_at") or item.get("updated_at") or "")
    return items[: max(min(request.limit, 200), 1)]


def _batch_upsert_memories(request: BatchUpsertMemoriesRequest) -> dict:
    project = _deps._batch_project_scope(
        ({"project": item.project} for item in request.items),
        request.project,
        require_project=False,
    )
    project = project or rt._canonical_project(None)
    request_payload = request.model_dump(mode="json")
    request_digest = hashlib.sha256(
        json.dumps(request_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    operation_id = f"mutation_{request_digest[:24]}"
    keys = [item.upsert_key for item in request.items]
    if len(keys) != len(set(keys)):
        raise HTTPException(
            status_code=422,
            detail={"code": "batch_duplicate_upsert_key"},
        )

    if rt._memory_store_is_authoritative():
        service = rt._memory_service()
        now = rt._utc_now_iso()
        pending: list[dict] = []
        results: list[dict] = []
        upserted_ids: dict[str, str] = {}
        for item in request.items:
            existing = service.get_record_by_upsert_key(project, item.upsert_key)
            user_metadata = _deps._user_memory_metadata(item.metadata)
            if existing is None:
                metadata = initial_decay_metadata(
                    {
                        "raw_title": _deps._build_memory_title(item.content),
                        "confidence": None,
                        "files": item.files or [],
                        "upsert_key": item.upsert_key,
                        **user_metadata,
                    },
                    created_at=now,
                )
                record = {
                    "source_system": "bhm",
                    "source_id": f"mem_bhm_{uuid.uuid4().hex[:16]}",
                    "project": project,
                    "agent_id": "workspace",
                    "memory_type": item.type,
                    "content": item.content,
                    "summary": None,
                    "tags": item.concepts or [],
                    "session_refs": [],
                    "created_at": now,
                    "updated_at": now,
                    "metadata": metadata,
                }
                _deps._sync_mem0_record(record)
                pending.append(record)
                action = "created"
            else:
                existing_metadata = dict(existing.get("metadata") or {})
                unchanged = (
                    existing.get("memory_type") == item.type
                    and existing.get("content") == item.content
                    and list(existing.get("tags") or []) == list(item.concepts or [])
                    and list(existing_metadata.get("files") or []) == list(item.files or [])
                    and all(existing_metadata.get(key) == value for key, value in user_metadata.items())
                )
                if unchanged:
                    record = existing
                    action = "unchanged"
                else:
                    record = copy.deepcopy(existing)
                    metadata = record.setdefault("metadata", {})
                    metadata.update(user_metadata)
                    metadata["raw_title"] = _deps._build_memory_title(item.content)
                    metadata["files"] = item.files or []
                    metadata["upsert_key"] = item.upsert_key
                    record["project"] = project
                    record["memory_type"] = item.type
                    record["content"] = item.content
                    record["tags"] = item.concepts or []
                    record["updated_at"] = now
                    _deps._append_memory_changelog(
                        record,
                        "batch_upsert",
                        {"operation_id": operation_id},
                    )
                    _deps._sync_mem0_record(record)
                    pending.append(record)
                    action = "updated"
            upserted_ids[item.upsert_key] = record["source_id"]
            results.append({"action": action, "memory": _deps._serialize_memory_record(record)})
        if pending:
            service.upsert_records(pending)
        return {
            "items": results,
            "count": len(results),
            "upserted_ids": upserted_ids,
            "operation_id": operation_id,
            "request_digest": request_digest,
            "committed": True,
        }

    results = []
    upserted_ids: dict[str, str] = {}
    for item in request.items:
        action, record = _deps._upsert_live_memory(
            MemoryUpsertRequest(
                upsert_key=item.upsert_key,
                project=project,
                type=item.type,
                content=item.content,
                concepts=item.concepts,
                files=item.files,
                metadata=item.metadata,
            )
        )
        upserted_ids[item.upsert_key] = record["source_id"]
        results.append({"action": action, "memory": _deps._serialize_memory_record(record)})
    return {
        "items": results,
        "count": len(results),
        "upserted_ids": upserted_ids,
        "operation_id": operation_id,
        "request_digest": request_digest,
        "committed": True,
    }


def _batch_attach_source_refs(request: BatchAttachSourceRefsRequest) -> dict:
    project, items = _deps._batch_mapping_items(
        request.items,
        request.project,
        required_fields=("id",),
    )
    for item in items:
        if not isinstance(item.get("refs"), list):
            raise HTTPException(status_code=422, detail={"code": "batch_item_invalid", "field": "refs"})
        if rt._find_live_memory(str(item["id"]), project) is None:
            raise HTTPException(status_code=404, detail="memory not found")
    results = []
    for item in items:
        record = _attach_source_refs(
            MemorySourceRefsRequest(
                id=item["id"],
                project=project,
                refs=item.get("refs") or [],
            )
        )
        results.append(_deps._serialize_memory_record(record))
    return {"memories": results, "count": len(results)}


def _compact_memory(request: MemoryCompactRequest) -> dict:
    record = rt._find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    metadata = record.setdefault("metadata", {})
    metadata["compacted_from"] = record.get("content") or ""
    record["content"] = request.summary
    metadata["raw_title"] = _deps._build_memory_title(request.summary)
    record["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _deps._append_memory_changelog(record, "compact", {"summary_chars": len(request.summary)})
    _deps._replace_live_memory(record)
    return record


def _batch_archive_memories(request: BatchMemoryIdsRequest) -> dict:
    project, normalized_items = _deps._batch_mapping_items(request.items, request.project, required_fields=("id",))
    for item in normalized_items:
        if rt._find_live_memory(str(item["id"]), project) is None:
            raise HTTPException(status_code=404, detail="memory not found in live store")
    items = []
    for item in normalized_items:
        archived = _archive_live_memory(MemoryArchiveRequest(id=item["id"], project=project, reason=item.get("reason", "batch_archive")))
        items.append(_deps._serialize_memory_record(archived))
    return {"memories": items, "count": len(items)}


def _batch_delete_memories(request: BatchMemoryIdsRequest) -> dict:
    project, normalized_items = _deps._batch_mapping_items(request.items, request.project, required_fields=("id",))
    for item in normalized_items:
        if rt._find_live_memory(str(item["id"]), project) is None:
            raise HTTPException(status_code=404, detail="memory not found in live store")
    items = []
    for item in normalized_items:
        deleted = _deps._delete_live_memory(MemoryDeleteRequest(id=item["id"], project=project))
        items.append(_deps._serialize_memory_record(deleted))
    return {"memories": items, "count": len(items)}


def _batch_link_memories(request: BatchLinkMemoriesRequest) -> dict:
    project = _deps._batch_project_scope(
        ({"project": item.project} for item in request.items),
        request.project,
    )
    for item in request.items:
        if rt._find_live_memory(item.source_id, project) is None or rt._find_live_memory(item.target_id, project) is None:
            raise HTTPException(status_code=404, detail="source or target memory not found")
    links = []
    for item in request.items:
        link = _deps._create_memory_link(
            MemoryLinkRequest(
                source_id=item.source_id,
                target_id=item.target_id,
                relation=item.relation,
                project=project,
                metadata=item.metadata,
                ontology_schema_digest=item.ontology_schema_digest,
            )
        )
        links.append(_deps._serialize_memory_link(link))
    return {"links": links, "count": len(links)}


def _batch_unlink_memories(request: BatchMemoryIdsRequest) -> dict:
    project, items = _deps._batch_mapping_items(
        request.items,
        request.project,
        required_fields=("source_id", "target_id", "relation"),
    )
    deleted = []
    for item in items:
        deleted.append(
            _delete_memory_link(
                MemoryLinkDeleteRequest(
                    source_id=item["source_id"],
                    target_id=item["target_id"],
                    relation=item["relation"],
                    project=project,
                )
            )
        )
    return {"count": len(deleted), "deleted": deleted}


def _memory_diff(request: MemoryDiffRequest) -> dict:
    left = rt._find_live_memory(request.left_id, request.project)
    right = rt._find_live_memory(request.right_id, request.project)
    if left is None or right is None:
        raise HTTPException(status_code=404, detail="memory not found")
    left_lines = (left.get("content") or "").splitlines()
    right_lines = (right.get("content") or "").splitlines()
    return {
        "left": _deps._serialize_memory_record(left),
        "right": _deps._serialize_memory_record(right),
        "same_content": (left.get("content") or "") == (right.get("content") or ""),
        "left_only": [line for line in left_lines if line not in right_lines],
        "right_only": [line for line in right_lines if line not in left_lines],
    }


def _memory_schema_validate(id: str, project: str | None = None) -> dict:
    record = rt._find_live_memory(id, project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    required = ["source_id", "project", "memory_type", "content", "created_at", "updated_at", "metadata"]
    missing = [field for field in required if field not in record or record.get(field) is None]
    return {"memory": _deps._serialize_memory_record(record), "missing_fields": missing, "ok": len(missing) == 0}


def _memory_type_migrate(request: TypeMigrateRequest) -> dict:
    record = rt._find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    old_type = record.get("memory_type")
    record["memory_type"] = request.new_type
    record["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _deps._append_memory_changelog(record, "type_migrate", {"old_type": old_type, "new_type": request.new_type})
    _deps._replace_live_memory(record)
    return record


def _list_archived_memories(project: str | None = None, limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
    items = [
        item for item in rt._load_live_memories()
        if _deps._memory_matches_filters(item, project=project, include_archived=True) and _deps._is_archived_memory(item)
    ]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    start = max(offset, 0)
    end = start + max(min(limit, 200), 1)
    return items[start:end], total


def _batch_restore_memories(request: BatchRestoreRequest) -> dict:
    project, items = _deps._batch_mapping_items(request.items, request.project, required_fields=("id",))
    for item in items:
        if rt._find_live_memory(str(item["id"]), project) is None:
            raise HTTPException(status_code=404, detail="memory not found")
    restored = []
    for item in items:
        record = _deps._restore_archived_memory(RestoreMemoryRequest(id=item["id"], project=project))
        restored.append(_deps._serialize_memory_record(record))
    return {"memories": restored, "count": len(restored)}


def _memory_staleness_report(request: MemoryStalenessReportRequest) -> dict:
    project = rt._canonical_project(request.project) if request.project else None
    stale = []
    for item in rt._load_live_memories():
        if not _deps._memory_matches_filters(item, project=project, include_archived=False):
            continue
        age_days = _deps._record_age_days(item)
        if age_days >= request.days:
            stale.append({"age_days": round(age_days, 1), "memory": _deps._serialize_memory_record(item)})
    stale.sort(key=lambda item: item["age_days"], reverse=True)
    return {"project": project, "days": request.days, "items": stale[: max(min(request.limit, 200), 1)]}


def _memory_triage_queue(request: MemoryTriageQueueRequest) -> dict:
    project = rt._canonical_project(request.project) if request.project else None
    records = [
        item for item in rt._load_live_memories()
        if _deps._memory_matches_filters(item, project=project, include_archived=False)
    ]
    by_id = {str(item.get("source_id")): item for item in records}
    duplicates = _deps._detect_duplicates(MemoryDetectRequest(project=project, limit=200, include_archived=False))
    conflicts = _deps._detect_conflicts(MemoryDetectRequest(project=project, limit=200, include_archived=False))
    suggestions = _deps._relation_suggest(RelationSuggestRequest(project=project, limit=200)).get("suggestions", [])
    queue: list[dict] = []
    seen_queue_ids: set[str] = set()
    for item in duplicates:
        source_id, target_id = item["left_id"], item["right_id"]
        status = _deps._combined_review_status([by_id.get(source_id), by_id.get(target_id)])
        if status in {"resolved", "dismissed"} and not request.include_closed:
            continue
        queue_id = _deps._review_queue_id("duplicate", item.get("project"), [source_id, target_id], f"{item['reason']}|{item['score']}")
        if queue_id in seen_queue_ids:
            continue
        seen_queue_ids.add(queue_id)
        queue.append({"queue_id": queue_id, "kind": "duplicate", "status": status, "score": item["score"], "memory_ids": [source_id, target_id], "payload": item})
    for item in conflicts:
        status = _deps._combined_review_status([by_id.get(item["left_id"]), by_id.get(item["right_id"])])
        if status in {"resolved", "dismissed"} and not request.include_closed:
            continue
        queue_id = item["queue_id"]
        if queue_id in seen_queue_ids:
            continue
        seen_queue_ids.add(queue_id)
        queue.append({"queue_id": queue_id, "kind": "conflict", "status": status, "score": item["score"], "memory_ids": [item["left_id"], item["right_id"]], "payload": item})
    for item in suggestions:
        source_id, target_id = item["source_id"], item["target_id"]
        status = _deps._combined_review_status([by_id.get(source_id), by_id.get(target_id)])
        if status in {"resolved", "dismissed"} and not request.include_closed:
            continue
        queue_id = _deps._review_queue_id(item["relation"], project, [source_id, target_id], f"{item['reason']}|{item['score']}")
        if queue_id in seen_queue_ids:
            continue
        seen_queue_ids.add(queue_id)
        queue.append({"queue_id": queue_id, "kind": "relation_suggestion", "status": status, "score": item["score"], "memory_ids": [source_id, target_id], "payload": item})
    queue.sort(key=lambda item: (item.get("score") or 0.0, item.get("queue_id") or ""), reverse=True)
    bounded_queue = queue[: max(min(request.limit, 200), 1)]
    return {
        "project": project,
        "items": bounded_queue,
        "lifecycle_suggestions": build_lifecycle_suggestions(bounded_queue),
    }


def _memory_merge_preview(request: MemoryMergePreviewRequest) -> dict:
    if request.source_id == request.target_id:
        raise HTTPException(status_code=400, detail="source_id and target_id must differ")
    source = rt._find_live_memory(request.source_id, request.project)
    target = rt._find_live_memory(request.target_id, request.project)
    if source is None or target is None:
        raise HTTPException(status_code=404, detail="memory not found")
    project = rt._canonical_project(request.project)
    merged_tags = sorted(set((source.get("tags") or []) + (target.get("tags") or [])))
    merged_files = sorted(set(((source.get("metadata") or {}).get("files") or []) + ((target.get("metadata") or {}).get("files") or [])))
    merged_content = (target.get("content") or "").strip()
    if (source.get("content") or "").strip() and (source.get("content") or "").strip() not in merged_content:
        merged_content = f"{merged_content}\n\n[source supplement]\n{source.get('content')}".strip()
    affected_links: list[dict[str, Any]] = []
    remapped_keys: dict[tuple[str, str, str], list[str]] = {}
    for link in rt._load_memory_links():
        if not _deps._project_matches(link.get("project"), project):
            continue
        source_id = str(link.get("source_id") or "")
        target_id = str(link.get("target_id") or "")
        if request.source_id not in {source_id, target_id}:
            continue
        remapped_source = request.target_id if source_id == request.source_id else source_id
        remapped_target = request.target_id if target_id == request.source_id else target_id
        relation = str(link.get("relation") or "")
        key = (remapped_source, remapped_target, relation)
        link_id = str(link.get("id") or "")
        remapped_keys.setdefault(key, []).append(link_id)
        ontology = (link.get("metadata") or {}).get("ontology")
        ontology = ontology if isinstance(ontology, Mapping) else {}
        affected_links.append(
            {
                "id": link_id,
                "relation": relation,
                "direction": "outgoing" if source_id == request.source_id else "incoming",
                "becomes_self_link": remapped_source == remapped_target,
                "remapped_endpoint_ids": {
                    "source": remapped_source,
                    "target": remapped_target,
                },
                "ontology": {
                    "schema_digest": str(ontology.get("schema_digest") or ""),
                    "revision": ontology.get("revision"),
                    "admission": str(ontology.get("admission") or ""),
                },
            }
        )
    affected_links.sort(key=lambda item: (item["relation"], item["direction"], item["id"]))
    collisions = [
        {"remapped_key": {"source_id": key[0], "target_id": key[1], "relation": key[2]}, "link_ids": sorted(ids)}
        for key, ids in sorted(remapped_keys.items())
        if len(ids) > 1
    ]
    plan = {
        "project": project,
        "source_id": request.source_id,
        "target_id": request.target_id,
        "source_content_sha256": _deps._memory_content_sha256(source),
        "target_content_sha256": _deps._memory_content_sha256(target),
        "affected_links": affected_links,
        "collisions": collisions,
    }
    plan_digest = hashlib.sha256(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "source": _deps._serialize_memory_record(source),
        "target": _deps._serialize_memory_record(target),
        "preview": {"tags": merged_tags, "files": merged_files, "content": merged_content},
        "resolution": {
            "schema_version": "bhm.memory-merge-preview.v2",
            "project": project,
            "plan_digest": plan_digest,
            "affected_link_count": len(affected_links),
            "self_link_count": sum(1 for item in affected_links if item["becomes_self_link"]),
            "collision_count": len(collisions),
            "affected_links": affected_links,
            "collisions": collisions,
        },
        "rollback": {
            "apply_performed": False,
            "required_before_apply": ["same_snapshot_recheck", "verified_backup", "explicit_operator_approval"],
        },
        "execution": {"sqlite_mutation": False, "qdrant_mutation": False, "mem0_mutation": False},
    }


def _secret_scan_existing_memories(request: SecretScanRequest) -> dict:
    project = rt._canonical_project(request.project) if request.project else None
    findings = []
    for item in rt._load_live_memories():
        if not _deps._memory_matches_filters(item, project=project, include_archived=True):
            continue
        redaction = redact_secret_text(item.get("content") or "")
        if redaction.replacements:
            findings.append(
                {
                    "memory": _deps._serialize_memory_record(item),
                    "match_count": redaction.replacements,
                    "keywords": sorted(set(redaction.kinds)),
                }
            )
    findings.sort(key=lambda item: item["match_count"], reverse=True)
    return {"project": project, "findings": findings[: max(min(request.limit, 500), 1)]}


def _memory_alias_add(request: MemoryAliasRequest) -> dict:
    record = rt._find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    aliases = (record.setdefault("metadata", {})).setdefault("aliases", [])
    if request.alias not in aliases:
        aliases.append(request.alias)
        record["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _deps._append_memory_changelog(record, "alias_add", {"alias": request.alias})
        _deps._replace_live_memory(record)
    return record


def _memory_alias_remove(request: MemoryAliasRequest) -> dict:
    record = rt._find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    aliases = (record.setdefault("metadata", {})).setdefault("aliases", [])
    record["metadata"]["aliases"] = [item for item in aliases if item != request.alias]
    record["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _deps._append_memory_changelog(record, "alias_remove", {"alias": request.alias})
    _deps._replace_live_memory(record)
    return record


def _alias_resolve(request: AliasResolveRequest) -> dict:
    matches = []
    accepted_projects = rt._project_aliases(request.project)
    for item in rt._load_live_memories():
        if request.project and item.get("project") not in accepted_projects:
            continue
        aliases = (item.get("metadata") or {}).get("aliases") or []
        if request.alias in aliases:
            matches.append(_deps._serialize_memory_record(item))
    return {"alias": request.alias, "memories": matches}


def _memory_usage_stats(project: str | None = None) -> dict:
    project = rt._canonical_project(project) if project else None
    records = [item for item in rt._load_live_memories() if _deps._memory_matches_filters(item, project=project, include_archived=True)]
    archived = sum(1 for item in records if _deps._is_archived_memory(item))
    pinned = sum(1 for item in records if (item.get("metadata") or {}).get("pinned"))
    content_chars = sum(len(item.get("content") or "") for item in records)
    refs = sum(len((item.get("metadata") or {}).get("source_refs") or []) for item in records)
    links = len([item for item in rt._load_memory_links() if _deps._project_matches(item.get("project"), project)])
    return {
        "project": project,
        "memory_count": len(records),
        "archived_count": archived,
        "pinned_count": pinned,
        "content_chars": content_chars,
        "source_ref_count": refs,
        "link_count": links,
    }


def _memory_restore_hard_deleted_preview(request: HardDeleteRestorePreviewRequest) -> dict:
    record = rt._find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    project = rt._canonical_project(request.project or record.get("project"))
    artifact_refs = []
    for artifact_type, (loader, _) in _deps._artifact_store_pairs().items():
        for item in loader():
            if not _deps._project_matches(item.get("project"), project):
                continue
            if item.get("memory_id") == request.id:
                artifact_refs.append({"artifact_type": artifact_type, "artifact_id": item.get("id"), "title": item.get("title")})
    links = [
        item
        for item in rt._load_memory_links()
        if _deps._project_matches(item.get("project"), project)
        and (item.get("source_id") == request.id or item.get("target_id") == request.id)
    ]
    preview = {
        "project": project,
        "memory": _deps._serialize_memory_record(record),
        "artifact_dependencies": artifact_refs,
        "link_count": len(links),
        "restorable_after_hard_delete": len(artifact_refs) > 0,
        "reconstruction_sources": ["artifact stores"] if artifact_refs else [],
        "warnings": [
            "hard delete removes dependent canonical artifact refs"
            if artifact_refs else "without surviving artifacts, reconstruction will be weak or impossible"
        ],
    }
    return preview


def _memory_gc_candidates(request: MemoryGcCandidatesRequest) -> dict:
    project = rt._canonical_project(request.project) if request.project else None
    candidates = []
    referenced_memory_ids = set()
    for _, (loader, _) in _deps._artifact_store_pairs().items():
        for item in loader():
            if not _deps._project_matches(item.get("project"), project):
                continue
            if item.get("memory_id"):
                referenced_memory_ids.add(item["memory_id"])
    for item in rt._load_live_memories():
        if not _deps._memory_matches_filters(item, project=project, include_archived=True):
            continue
        reasons = []
        age_days = _deps._record_age_days(item)
        metadata = item.get("metadata") or {}
        if _deps._is_archived_memory(item) and age_days >= request.stale_days:
            reasons.append("archived_stale")
        if item.get("source_id") not in referenced_memory_ids and not (metadata.get("pinned")):
            reasons.append("unreferenced")
        if not (metadata.get("source_refs") or []) and not (metadata.get("files") or []):
            reasons.append("no_source_refs")
        if (metadata.get("confidence") is not None) and metadata.get("confidence") < 0.4:
            reasons.append("low_confidence")
        if reasons:
            candidates.append({"reasons": reasons, "age_days": round(age_days, 1), "memory": _deps._serialize_memory_record(item)})
    candidates.sort(key=lambda item: (len(item["reasons"]), item["age_days"]), reverse=True)
    return {"project": project, "items": candidates[: max(min(request.limit, 200), 1)]}


def _memory_compaction_report(request: MemoryCompactionReportRequest) -> dict:
    project = rt._canonical_project(request.project) if request.project else None
    items = []
    for item in rt._load_live_memories():
        if not _deps._memory_matches_filters(item, project=project, include_archived=False):
            continue
        content = item.get("content") or ""
        line_count = len(content.splitlines())
        char_count = len(content)
        looks_loggy = line_count >= request.min_lines or char_count >= request.min_chars
        if looks_loggy:
            items.append({"char_count": char_count, "line_count": line_count, "memory": _deps._serialize_memory_record(item)})
    items.sort(key=lambda item: (item["char_count"], item["line_count"]), reverse=True)
    return {"project": project, "items": items[: max(min(request.limit, 200), 1)]}


def _memory_changelog(request: MemoryChangelogRequest) -> dict:
    record = rt._find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    metadata = record.get("metadata") or {}
    events = list(metadata.get("changelog") or [])
    inferred = [{"at": record.get("created_at"), "action": "created", "details": {}}]
    for key, action in (("archived_at", "archived_at"), ("restored_at", "restored_at"), ("redacted_at", "redacted_at"), ("confidence_updated_at", "confidence_updated_at")):
        if metadata.get(key):
            inferred.append({"at": metadata.get(key), "action": action, "details": {}})
    if metadata.get("quality_votes"):
        for vote in metadata.get("quality_votes")[-10:]:
            inferred.append({"at": vote.get("at"), "action": "vote_quality", "details": vote})
    all_events = [item for item in inferred + events if item.get("at")]
    all_events.sort(key=lambda item: item.get("at"))
    return {"memory": _deps._serialize_memory_record(record), "events": all_events[-max(min(request.limit, 200), 1):]}


def _access_updates_for_hits(hits: list[dict], accessed_at: str) -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for hit in hits:
        metadata = hit.get("metadata") or {}
        collection_name = _deps._search_hit_collection(hit)
        point_id = hit.get("id") or metadata.get("mem0_hit_id")
        if not collection_name or not point_id:
            continue
        key = (collection_name, str(point_id))
        if key in seen:
            continue
        seen.add(key)
        updates.append(
            {
                "collection_name": collection_name,
                "point_id": point_id,
                "payload": {
                    "access_count": normalize_access_count(metadata.get("access_count")) + 1,
                    "last_accessed_at": accessed_at,
                },
            }
        )
    return updates


def _update_vector_access_payloads(updates: list[dict[str, Any]]) -> None:
    client = get_qdrant_client()
    for update in updates:
        try:
            client.set_payload(
                collection_name=update["collection_name"],
                payload=update["payload"],
                points=[update["point_id"]],
            )
        except Exception as exc:
            print(f"[WARN] BHM decay access update failed: {_safe_exception_text(exc)}", flush=True)


async def _update_vector_access_payloads_async(updates: list[dict[str, Any]]) -> None:
    await asyncio.to_thread(_update_vector_access_payloads, updates)


def _schedule_vector_access_updates(hits: list[dict]) -> None:
    updates = _access_updates_for_hits(hits, rt._utc_now_iso())
    if not updates:
        return
    asyncio.create_task(_update_vector_access_payloads_async(updates))


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
    except Exception as exc:
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
