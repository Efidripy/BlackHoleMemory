"""Ontology, relation management, and memory linking for app_ops."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from . import app_core
from . import app_core_hit
from . import app_core_hook
from . import app_runtime as rt
from .app_runtime import _safe_exception_text
from .api_models import (
    EntityLinkMemoriesRequest,
    MemoryDetectRequest,
    MemoryLinkRequest,
    RelationApplySuggestionsRequest,
    RelationConfidenceRequest,
    RelationPruneLowQualityRequest,
    RelationSuggestRequest,
    RelationVoteRequest,
    TriageQueueApplyRequest,
)
from .mem0_adapter import StorageNotReady
from .memory_routes import _serialized_link_mutation
from .memory_service import MemoryServiceNotReady
from .ontology_quarantine import build_quarantine_artifact, serialize_quarantine_record
from .ontology_registry import (
    ACTIVATION_ARTIFACT_TYPE as ONTOLOGY_ACTIVATION_ARTIFACT_TYPE,
    ARTIFACT_TYPE as ONTOLOGY_REGISTRY_ARTIFACT_TYPE,
    OntologyRegistryError,
    OntologyRelationWrite,
    admit_relation_write,
    resolve_active_schema,
)
from .app_ops_review import _detect_conflicts


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
    from . import app_ops as _owner
    active_schema_fn = getattr(_owner, "_active_ontology_schema", _active_ontology_schema)
    schema = active_schema_fn(project)
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

def _relation_suggest(request: RelationSuggestRequest) -> dict:
    project = rt._canonical_project(request.project) if request.project else None
    duplicates = app_core._detect_duplicates(MemoryDetectRequest(project=project, limit=request.limit, include_archived=False))
    from . import app_ops as _owner
    detect_conflicts_fn = getattr(_owner, "_detect_conflicts", _detect_conflicts)
    conflicts = detect_conflicts_fn(MemoryDetectRequest(project=project, limit=request.limit, include_archived=False))
    suggestions = []
    for item in duplicates:
        suggestions.append({"relation": "duplicate_of", "score": item["score"], "source_id": item["left_id"], "target_id": item["right_id"], "reason": item["reason"]})
    for item in conflicts:
        suggestions.append({"relation": "conflicts_with", "score": item["score"], "source_id": item["left_id"], "target_id": item["right_id"], "reason": item["reason"]})
    records = [
        item for item in rt._load_live_memories()
        if app_core_hit._memory_matches_filters(item, project=project, include_archived=False)
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

def _relation_apply_suggestions(request: RelationApplySuggestionsRequest) -> dict:
    if not request.project and not request.aggregate:
        raise HTTPException(
            status_code=403,
            detail={"code": "relation_apply_scope_required", "message": "project or aggregate=true is required"},
        )
    canonical_project = rt._canonical_project(request.project) if request.project else None
    from . import app_ops as _owner
    suggest_fn = getattr(_owner, "_relation_suggest", _relation_suggest)
    suggestions = suggest_fn(
        RelationSuggestRequest(project=canonical_project, limit=request.limit)
    ).get("suggestions", [])
    created = []
    for item in suggestions:
        if item["score"] < request.min_score:
            continue
        if item["relation"] == "relates_to" and not request.include_relates_to:
            continue
        create_link_fn = getattr(_owner, "_create_memory_link", _create_memory_link)
        link = create_link_fn(
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
        serialize_fn = getattr(_owner, "_serialize_memory_link", _serialize_memory_link)
        created.append(serialize_fn(link))
    return {"project": request.project, "links": created, "count": len(created)}

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

def _link_orphan_scan(project: str | None = None) -> dict:
    project = rt._canonical_project(project) if project else None
    memory_ids = {item.get("source_id") for item in rt._load_live_memories() if app_core_hook._project_matches(item.get("project"), project)}
    orphans = []
    for item in rt._load_memory_links():
        if not app_core_hook._project_matches(item.get("project"), project):
            continue
        if item.get("source_id") not in memory_ids or item.get("target_id") not in memory_ids:
            orphans.append(_serialize_memory_link(item))
    return {"project": project, "orphan_links": orphans, "count": len(orphans)}

def _entity_link_memories(request: EntityLinkMemoriesRequest) -> dict:
    project = rt._canonical_project(request.project)
    candidates = []
    needle = request.entity.lower()
    for item in rt._load_live_memories():
        if not app_core_hit._memory_matches_filters(item, project=project, include_archived=False):
            continue
        haystack = " ".join([item.get("content") or "", " ".join(item.get("tags") or []), " ".join((item.get("metadata") or {}).get("files") or []), " ".join((item.get("metadata") or {}).get("source_refs") or [])]).lower()
        if needle in haystack:
            candidates.append(item)
    candidates = candidates[: max(min(request.limit, 50), 2)]
    links = []
    for idx, left in enumerate(candidates):
        for right in candidates[idx + 1:]:
            from . import app_ops as _owner
            create_link_fn = getattr(_owner, "_create_memory_link", _create_memory_link)
            link = create_link_fn(MemoryLinkRequest(source_id=left["source_id"], target_id=right["source_id"], relation=request.relation, project=project, metadata={"entity": request.entity}))
            links.append(_serialize_memory_link(link))
    return {"project": project, "entity": request.entity, "matched_memory_ids": [item["source_id"] for item in candidates], "links": links, "count": len(links)}

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
        if project and not app_core_hook._project_matches(item.get("project"), project):
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

def _triage_queue_apply(request: TriageQueueApplyRequest) -> dict:
    from . import app_ops as _owner
    apply_fn = getattr(_owner, "_relation_apply_suggestions", _relation_apply_suggestions)
    created = apply_fn(
        RelationApplySuggestionsRequest(
            project=request.project,
            min_score=request.min_score,
            limit=request.limit,
            include_relates_to=request.include_relates_to,
        )
    )
    return {"project": request.project, "count": created.get("count", 0), "links": created.get("links", [])}
