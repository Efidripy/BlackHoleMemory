"""Read, query, reporting, inspection and preview operations for memory routes.

Part of memory_routes split (FIX-017).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from fastapi import HTTPException

from . import app_runtime as rt
from .api_models import (
    HardDeleteRestorePreviewRequest,
    MemoryChangelogRequest,
    MemoryCompactionReportRequest,
    MemoryDetectRequest,
    MemoryDiffRequest,
    MemoryGcCandidatesRequest,
    MemoryMergePreviewRequest,
    MemoryStalenessReportRequest,
    MemoryTimelineRequest,
    MemoryTriageQueueRequest,
    RelationSuggestRequest,
    SecretScanRequest,
)
from .lifecycle_suggestions import build_lifecycle_suggestions
from .memory_routes_base import MemoryClass, MemoryEventRole, _deps
from .observation_security import redact_secret_text


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

