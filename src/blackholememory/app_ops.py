"""Domain module app_ops: moved out of app.py (FIX-017). Cross-module calls go through module attributes."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from fastapi.responses import FileResponse

from . import (
    app_core,
    app_core_hit,
    app_core_hook,
    app_ops_admin,
    app_ops_entity,
    app_ops_task,
    app_ops_task2,
)
from . import app_runtime as rt
from .api_models import (
    EntityLinkMemoriesRequest,
    MemoryDetectRequest,
    MemoryLinkRequest,
    MemoryRedactRequest,
    MemoryReviewQueueRequest,
    PolicyEnforceMemoryRequest,
    ProjectOnlyRequest,
    RelationApplySuggestionsRequest,
    RelationConfidenceRequest,
    RelationPruneLowQualityRequest,
    RelationSuggestRequest,
    RelationVoteRequest,
    ReviewQueueApplyRequest,
    TriageQueueApplyRequest,
)
from .app_runtime import _safe_exception_text
from .lifecycle_suggestions import build_lifecycle_suggestions
from .mem0_adapter import StorageNotReady
from .memory_routes import _serialized_link_mutation
from .memory_service import MemoryServiceNotReady
from .observation_security import contains_secret_like, redact_secret_text
from .ontology_quarantine import build_quarantine_artifact, serialize_quarantine_record
from .ontology_registry import (
    ACTIVATION_ARTIFACT_TYPE as ONTOLOGY_ACTIVATION_ARTIFACT_TYPE,
)
from .ontology_registry import ARTIFACT_TYPE as ONTOLOGY_REGISTRY_ARTIFACT_TYPE
from .ontology_registry import (
    OntologyRegistryError,
    OntologyRelationWrite,
    admit_relation_write,
    resolve_active_schema,
)
from .security_boundaries import SecurityBoundaryError, compile_bounded_regex

_CUSTOM_REDACTION_MAX_PATTERN_LENGTH = 120


_CUSTOM_REDACTION_MAX_INPUT_CHARS = 64 * 1024


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
        if app_core_hit._memory_matches_filters(item, project=request.project, include_archived=request.include_archived)
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
            text = app_core_hit._normalized_text(item.get("content"))
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
            left_text = app_core_hit._normalized_text(left.get("content"))
            right_text = app_core_hit._normalized_text(right.get("content"))
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


def _adr_supersede(project: str, old_id: str, new_id: str) -> dict:
    adrs = app_ops_task2._load_adrs()
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
    app_ops_task2._save_adrs(adrs)
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


def _integrity_audit(project: str | None = None) -> dict:
    canonical_project = rt._canonical_project(project) if project else None
    memories = [
        item for item in rt._load_live_memories()
        if app_core_hit._memory_matches_filters(item, project=canonical_project, include_archived=True)
    ]
    memory_ids = {item.get("source_id") for item in memories}
    orphan_links = [
        item for item in rt._load_memory_links()
        if (not canonical_project or app_core_hook._project_matches(item.get("project"), canonical_project))
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


def _memory_review_queue(request: MemoryReviewQueueRequest) -> dict:
    project = rt._canonical_project(request.project) if request.project else None
    records = [
        item for item in rt._load_live_memories()
        if app_core_hit._memory_matches_filters(item, project=project, include_archived=False)
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
            link = _create_memory_link(MemoryLinkRequest(source_id=left["source_id"], target_id=right["source_id"], relation=request.relation, project=project, metadata={"entity": request.entity}))
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
            and app_core_hook._project_matches(item.get("project"), canonical_project)
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


def _policy_enforce_memory(request: PolicyEnforceMemoryRequest) -> dict:
    record = rt._find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    profile = app_core_hook._load_policy_profile()
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


def bhm_relation_suggest(request: RelationSuggestRequest) -> dict:
    return _relation_suggest(request)


def bhm_relation_apply_suggestions(request: RelationApplySuggestionsRequest) -> dict:
    return _relation_apply_suggestions(request)


def bhm_relation_confidence_set(request: RelationConfidenceRequest) -> dict:
    link = _relation_confidence_set(request)
    return {"success": True, "link": link}


def bhm_relation_vote_quality(request: RelationVoteRequest) -> dict:
    link = _relation_vote_quality(request)
    return {"success": True, "link": link}


def bhm_link_orphan_scan(request: ProjectOnlyRequest) -> dict:
    return _link_orphan_scan(request.project)


def bhm_entity_link_memories(request: EntityLinkMemoriesRequest) -> dict:
    return _entity_link_memories(request)


def bhm_relation_prune_low_quality(request: RelationPruneLowQualityRequest) -> dict:
    return _relation_prune_low_quality(request)


def bhm_review_queue_apply(request: ReviewQueueApplyRequest) -> dict:
    return _review_queue_apply(request)


def bhm_triage_queue_apply(request: TriageQueueApplyRequest) -> dict:
    return _triage_queue_apply(request)


def bhm_policy_enforce_memory(request: PolicyEnforceMemoryRequest) -> dict:
    return _policy_enforce_memory(request)


def register(app) -> None:
    """Attach this module's routes to the application."""

    app.get("/", response_class=FileResponse)(app_ops_task.index)
    app.get("/favicon.ico", response_class=FileResponse)(app_ops_task.favicon_ico)
    app.get("/favicon.svg", response_class=FileResponse)(app_ops_task.favicon_svg)
    app.get("/static/redoc.standalone.js", response_class=FileResponse)(app_ops_task.redoc_bundle)
    app.get("/static/3d-force-graph.min.js", response_class=FileResponse)(app_ops_task.force_graph_bundle)
    app.get("/static/three.module.min.js", response_class=FileResponse)(app_ops_task.three_module_bundle)
    app.get("/static/three.core.min.js", response_class=FileResponse)(app_ops_task.three_core_bundle)
    app.get("/redoc")(app_ops_task.local_redoc)
    app.post("/bhm/conventions/preview", include_in_schema=False)(app_ops_task.bhm_conventions_preview)
    app.post("/bhm/factories/preview", include_in_schema=False)(app_ops_task.bhm_factories_preview)
    app.post("/bhm/migration/preview", include_in_schema=False)(app_ops_task.bhm_migration_preview)
    app.post("/bhm/security/trust-boundary/preview", include_in_schema=False)(app_ops_task.bhm_security_trust_boundary_preview)
    app.get("/graph/status")(app_ops_task.graph_status)
    app.websocket("/bhm/ws")(app_ops_task.bhm_memory_pulse_ws)
    app.get("/bhm/memory")(app_ops_task.bhm_memory_get)
    app.post("/bhm/forget/preview")(app_ops_entity.bhm_forget_preview)
    app.post("/bhm/forget/apply")(app_ops_entity.bhm_forget_apply)
    app.post("/bhm/recent-activity")(app_ops_task.bhm_recent_activity)
    app.post("/bhm/checkpoint")(app_ops_admin.bhm_checkpoint_create)
    app.get("/bhm/checkpoints")(app_ops_task.bhm_checkpoint_list)
    app.get("/bhm/checkpoint/latest")(app_ops_task.bhm_checkpoint_get_latest)
    app.delete("/bhm/memory")(app_ops_task.bhm_memory_delete)
    app.post("/bhm/adr")(app_ops_task.bhm_adr_create)
    app.get("/bhm/adrs")(app_ops_task.bhm_adr_list)
    app.post("/bhm/adr/supersede")(app_ops_task.bhm_adr_supersede)
    app.post("/bhm/handoff")(app_ops_task.bhm_handoff_create)
    app.get("/bhm/handoffs")(app_ops_task.bhm_handoff_list)
    app.post("/bhm/session-record")(app_ops_task.bhm_session_record_create)
    app.get("/bhm/session-records")(app_ops_task.bhm_session_record_list)
    app.post("/bhm/task/open")(app_ops_task.bhm_task_open)
    app.post("/bhm/task/close")(app_ops_task.bhm_task_close)
    app.get("/bhm/task")(app_ops_task.bhm_task_get)
    app.get("/bhm/tasks")(app_ops_task.bhm_task_list)
    app.post("/bhm/task-context")(app_ops_task.bhm_task_context_update)
    app.get("/bhm/task-context")(app_ops_task.bhm_task_context_get)
    app.post("/bhm/risk-register")(app_ops_task.bhm_risk_register_update)
    app.get("/bhm/risk-register")(app_ops_task.bhm_risk_register_get)
    app.post("/bhm/validation-snapshot")(app_ops_task.bhm_validation_snapshot_save)
    app.get("/bhm/validation-snapshot")(app_ops_task.bhm_validation_snapshot_get)
    app.get("/bhm/query-suggestions")(app_ops_task.bhm_query_suggestions)
    app.post("/bhm/integrity-audit")(app_ops_task.bhm_integrity_audit)
    app.get("/bhm/audit")(app_ops_task.bhm_audit)
    app.post("/bhm/artifact-integrity-audit")(app_ops_task.bhm_artifact_integrity_audit)
    app.post("/bhm/repair-live-indexes")(app_ops_task.bhm_repair_live_indexes)
    app.post("/bhm/entity-extract")(app_ops_task.bhm_entity_extract)
    app.post("/bhm/relation-suggest")(bhm_relation_suggest)
    app.post("/bhm/link-graph-stats")(app_ops_task.bhm_link_graph_stats)
    app.post("/bhm/reindex-memory-metadata")(app_ops_task.bhm_reindex_memory_metadata)
    app.post("/bhm/artifact/restore")(app_ops_task.bhm_artifact_restore)
    app.post("/bhm/artifact/relink")(app_ops_task.bhm_orphan_artifact_relink)
    app.post("/bhm/relation/apply-suggestions")(bhm_relation_apply_suggestions)
    app.get("/bhm/ontology/quarantine")(app_ops_task.bhm_ontology_quarantine_list)
    app.post("/bhm/schema/upgrade-all")(app_ops_task.bhm_schema_upgrade_all)
    app.post("/bhm/agent-activity-rollup")(app_ops_task.bhm_agent_activity_rollup)
    app.post("/bhm/relation/confidence")(bhm_relation_confidence_set)
    app.post("/bhm/relation/vote-quality")(bhm_relation_vote_quality)
    app.post("/bhm/entity-catalog/get")(app_ops_entity.bhm_entity_catalog_get)
    app.post("/bhm/entity-catalog/rebuild")(app_ops_entity.bhm_entity_catalog_rebuild)
    app.post("/bhm/recent-failures-feed")(app_ops_task.bhm_recent_failures_feed)
    app.post("/bhm/artifact/delete")(app_ops_task.bhm_artifact_delete)
    app.post("/bhm/artifact/list-by-type")(app_ops_task.bhm_artifact_list_by_type)
    app.post("/bhm/artifact/usage-stats")(app_ops_task.bhm_artifact_usage_stats)
    app.post("/bhm/link/cycle-detect")(app_ops_task.bhm_link_cycle_detect)
    app.post("/bhm/link/orphan-scan")(bhm_link_orphan_scan)
    app.post("/bhm/validation/trend-report")(app_ops_task.bhm_validation_trend_report)
    app.post("/bhm/entity/search")(app_ops_task.bhm_entity_search)
    app.post("/bhm/entity/link-memories")(bhm_entity_link_memories)
    app.post("/bhm/alias/stats")(app_ops_task.bhm_alias_stats)
    app.post("/bhm/relation/prune-low-quality")(bhm_relation_prune_low_quality)
    app.post("/bhm/review-queue/apply")(bhm_review_queue_apply)
    app.post("/bhm/triage-queue/apply")(bhm_triage_queue_apply)
    app.post("/bhm/artifact/batch-delete")(app_ops_task.bhm_artifact_batch_delete)
    app.post("/bhm/artifact/batch-relink")(app_ops_task.bhm_artifact_batch_relink)
    app.post("/bhm/artifact/batch-restore")(app_ops_task.bhm_artifact_batch_restore)
    app.post("/bhm/schema/validate-strict")(app_ops_task.bhm_schema_validate_strict)
    app.post("/bhm/integrity/repair-strict")(app_ops_task.bhm_integrity_repair_strict)
    app.post("/bhm/admin/export")(app_ops_task.bhm_admin_export)
    app.post("/bhm/admin/import-preview")(app_ops_admin.bhm_admin_import_preview)
    app.post("/bhm/admin/import-apply")(app_ops_admin.bhm_admin_import_apply)
    app.get("/bhm/admin/task-ingress/status")(app_ops_task.bhm_admin_task_ingress_status)
    app.post("/bhm/admin/task-ingress/freeze")(app_ops_task.bhm_admin_task_ingress_freeze)
    app.post("/bhm/admin/task-ingress/resume")(app_ops_task.bhm_admin_task_ingress_resume)
    app.get("/bhm/policy/profile")(app_ops_task.bhm_policy_profile_get)
    app.post("/bhm/policy/profile")(app_ops_task.bhm_policy_profile_set)
    app.post("/bhm/policy/enforce-memory")(bhm_policy_enforce_memory)
    app.post("/bhm/overlap/report")(app_ops_task.bhm_overlap_report)
    app.post("/bhm/overlap/cleanup-apply")(app_ops_task.bhm_overlap_cleanup_apply)
    app.post("/bhm/policy-guard")(app_ops_task.bhm_policy_guard)
    app.post("/bhm/reflect")(app_ops_task.bhm_reflect)
    app.post("/bhm/verify")(app_ops_task.bhm_verify)
    app.post("/bhm/remember")(app_ops_task.bhm_remember)
    app.get("/bhm/observations/store/status")(app_ops_task.bhm_observation_store_status)
    app.get("/bhm/retention/status")(app_ops_task.bhm_retention_status)
