"""Write and mutation operations for memory routes (create, update, batch, links, source refs).

Part of memory_routes split (FIX-017).
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from . import app_runtime as rt
from .app_runtime import _safe_exception_text
from .api_models import (
    AliasResolveRequest,
    BatchAttachSourceRefsRequest,
    BatchLinkMemoriesRequest,
    BatchMemoryIdsRequest,
    BatchRestoreRequest,
    BatchUpsertMemoriesRequest,
    MemoryAliasRequest,
    MemoryArchiveRequest,
    MemoryCompactRequest,
    MemoryConfidenceRequest,
    MemoryDeleteRequest,
    MemoryLinkDeleteRequest,
    MemoryLinkRequest,
    MemoryLintRequest,
    MemorySourceRefsRequest,
    MemoryUpsertRequest,
    MemoryVoteRequest,
    RestoreMemoryRequest,
    SourceRefsDetachRequest,
    SourceRefsReplaceRequest,
    TypeMigrateRequest,
)
from .mem0_adapter import initial_decay_metadata
from .memory_routes_base import _deps, _serialized_link_mutation


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
        except Exception as exc:  # broad: per-edge failures are returned while other edges proceed
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

