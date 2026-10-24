"""Memory write workflows moved from app_core."""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from fastapi import HTTPException

from . import app_core_fallback, app_core_hit
from . import app_core_memory_base as _base
from . import app_core_memory_projection as _projection
from . import app_runtime as rt
from .api_models import (
    MemoryCrystallizeRequest,
    MemoryDetectRequest,
    MemoryUpdateRequest,
    MemoryUpsertRequest,
    RememberRequest,
)
from .mem0_adapter import ensure_decay_metadata, initial_decay_metadata
from .temporal_contract import normalize_temporal_fields, temporal_contract_enabled
from .typed_memory_contract import (
    TypedMemoryContractUnavailable,
    classify_new_memory,
    typed_memory_capability_available,
)

MemoryClassSource = _base.MemoryClassSource
EVENT_ROLE_SCHEMA_VERSION = _base.EVENT_ROLE_SCHEMA_VERSION

_save_live_memories = _base._save_live_memories

def _remember_live_memory(request: RememberRequest) -> dict:
    from . import app_core as _owner
    source_id = f"mem_bhm_{uuid.uuid4().hex[:16]}"
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    project = rt._canonical_project(request.project)
    temporal_request_fields = {
        key: getattr(request, key)
        for key in (
            "observed_at",
            "valid_from",
            "valid_to",
            "open_interval",
            "supersedes_revision_id",
            "source_episode_id",
            "source_uri",
            "source_digest",
        )
        if getattr(request, key) is not None
    }
    if temporal_request_fields or temporal_contract_enabled():
        app_core_fallback._require_temporal_memory_boundary(write_fields=temporal_request_fields)
    temporal_fields = {}
    if temporal_contract_enabled():
        temporal_fields = normalize_temporal_fields(
            {
                "metadata": {
                    **temporal_request_fields,
                    "observed_at": request.observed_at or now,
                    "observed_at_source": "explicit" if request.observed_at else "transaction-clock",
                    "open_interval": request.open_interval if request.open_interval is not None else True,
                }
            },
            require_observed=True,
        )
    user_metadata = _base._user_memory_metadata(request.metadata)
    procedure_contract = request.metadata.procedure_contract if request.metadata is not None else None
    capability_available = (
        typed_memory_capability_available(rt._memory_service().repository.path)
        if rt._memory_store_is_authoritative()
        else True
    )
    try:
        classification = classify_new_memory(
            explicit_class=request.memory_class,
            memory_type=request.type,
            event_role=request.event_role,
            procedure_contract=procedure_contract,
            capability_available=capability_available,
        )
    except TypedMemoryContractUnavailable as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
    memory_class = classification.memory_class
    memory_class_source = classification.source
    event_role = request.event_role or app_core_hit.MemoryEventRole.UNCLASSIFIED
    _base._validate_procedure_contract_binding(memory_class.value, user_metadata)
    metadata = initial_decay_metadata(
        {
            "raw_title": _base._build_memory_title(request.content),
            "confidence": None,
            "files": request.files or [],
            "upsert_key": request.upsert_key,
            "memory_class": memory_class.value,
            "memory_class_source": memory_class_source.value,
            "memory_class_confidence": classification.confidence,
            "memory_class_rule_id": classification.rule_id,
            "memory_class_rule_version": classification.rule_version,
            "event_role": event_role.value,
            "event_role_version": EVENT_ROLE_SCHEMA_VERSION,
            **user_metadata,
        },
        created_at=now,
    )
    record = {
        "source_system": "bhm",
        "source_id": source_id,
        "project": project,
        "agent_id": "workspace",
        "memory_type": request.type,
        "memory_class": memory_class.value,
        "memory_class_source": memory_class_source.value,
        "memory_class_confidence": classification.confidence,
        "event_role": event_role.value,
        "event_role_version": EVENT_ROLE_SCHEMA_VERSION,
        **temporal_fields,
        "content": request.content,
        "summary": None,
        "tags": request.concepts or [],
        "session_refs": [],
        "created_at": now,
        "updated_at": now,
        "metadata": metadata,
    }

    if rt._memory_store_is_authoritative():
        _projection._sync_mem0_record(record)
        _owner._save_live_memories([record])
        return record

    live_records = rt._load_live_memories()
    live_records.append(record)
    _projection._sync_mem0_record(record)
    live_records[-1] = record
    _owner._save_live_memories(live_records)
    return record


def _temporal_request_fields(request: Any) -> dict[str, Any]:
    return {
        key: getattr(request, key)
        for key in (
            "observed_at",
            "valid_from",
            "valid_to",
            "open_interval",
            "supersedes_revision_id",
            "source_episode_id",
            "source_uri",
            "source_digest",
        )
        if getattr(request, key, None) is not None
    }


def _apply_temporal_update(item: dict[str, Any], request: Any, *, now: str) -> None:
    request_fields = _temporal_request_fields(request)
    if not request_fields and not temporal_contract_enabled():
        return
    if temporal_contract_enabled():
        app_core_fallback._require_temporal_memory_boundary(write_fields=request_fields)
        existing = normalize_temporal_fields(item)
        merged = {**existing, **request_fields}
        if "observed_at" not in request_fields:
            merged["observed_at"] = now
            merged["observed_at_source"] = "transaction-clock"
        normalized = normalize_temporal_fields({"metadata": merged}, require_observed=True)
        item.update(normalized)


def _update_live_memory(request: MemoryUpdateRequest) -> dict:
    from . import app_core as _owner
    app_core_fallback._require_typed_memory_boundary(
        memory_class=request.memory_class,
        event_role=request.event_role,
    )
    if rt._memory_store_is_authoritative():
        item = rt._find_live_memory(request.id, request.project)
        if item is None:
            raise HTTPException(status_code=404, detail="memory not found in live store")
        item = copy.deepcopy(item)
        now = rt._utc_now_iso()
        _apply_temporal_update(item, request, now=now)
        canonical_project = rt._canonical_project(request.project) if request.project else None
        if canonical_project:
            item["project"] = canonical_project
        if request.type:
            item["memory_type"] = request.type
        if request.memory_class is not None:
            item["memory_class"] = request.memory_class.value
            item["memory_class_source"] = MemoryClassSource.CALLER_EXPLICIT.value
            item.setdefault("metadata", {})["memory_class"] = request.memory_class.value
            item["metadata"]["memory_class_source"] = MemoryClassSource.CALLER_EXPLICIT.value
        if request.event_role is not None:
            item["event_role"] = request.event_role.value
            item["event_role_version"] = EVENT_ROLE_SCHEMA_VERSION
            item.setdefault("metadata", {})["event_role"] = request.event_role.value
            item["metadata"]["event_role_version"] = EVENT_ROLE_SCHEMA_VERSION
        if request.content is not None:
            item["content"] = request.content
            item.setdefault("metadata", {})["raw_title"] = _base._build_memory_title(request.content)
        if request.concepts is not None:
            item["tags"] = request.concepts
        if request.files is not None:
            item.setdefault("metadata", {})["files"] = request.files
        if request.metadata_patch is not None:
            item.setdefault("metadata", {}).update(_base._user_memory_metadata(request.metadata_patch))
        _base._validate_procedure_contract_binding(_base._effective_memory_class(item), item.setdefault("metadata", {}))
        item["updated_at"] = now
        item["metadata"] = ensure_decay_metadata(
            item.setdefault("metadata", {}),
            fallback_at=item.get("created_at") or now,
        )
        _base._append_memory_changelog(
            item,
            "update",
            {
                "type": request.type,
                "memory_class": request.memory_class.value if request.memory_class else None,
                "event_role": request.event_role.value if request.event_role else None,
                "content_changed": request.content is not None,
                "metadata_changed": request.metadata_patch is not None,
            },
        )
        _projection._update_mem0_record(item)
        _owner._save_live_memories([item])
        return item

    live_records = rt._load_live_memories()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    canonical_project = rt._canonical_project(request.project) if request.project else None
    accepted_projects = rt._project_aliases(request.project)

    for item in live_records:
        if item.get("source_id") != request.id:
            continue
        if request.project and item.get("project") not in accepted_projects:
            continue

        _apply_temporal_update(item, request, now=now)

        if canonical_project:
            item["project"] = canonical_project
        if request.type:
            item["memory_type"] = request.type
        if request.memory_class is not None:
            item["memory_class"] = request.memory_class.value
            item["memory_class_source"] = MemoryClassSource.CALLER_EXPLICIT.value
            item.setdefault("metadata", {})["memory_class"] = request.memory_class.value
            item["metadata"]["memory_class_source"] = MemoryClassSource.CALLER_EXPLICIT.value
        if request.event_role is not None:
            item["event_role"] = request.event_role.value
            item["event_role_version"] = EVENT_ROLE_SCHEMA_VERSION
            item.setdefault("metadata", {})["event_role"] = request.event_role.value
            item["metadata"]["event_role_version"] = EVENT_ROLE_SCHEMA_VERSION
        if request.content is not None:
            item["content"] = request.content
            item.setdefault("metadata", {})["raw_title"] = _base._build_memory_title(request.content)
        if request.concepts is not None:
            item["tags"] = request.concepts
        if request.files is not None:
            item.setdefault("metadata", {})["files"] = request.files
        if request.metadata_patch is not None:
            item.setdefault("metadata", {}).update(_base._user_memory_metadata(request.metadata_patch))
        _base._validate_procedure_contract_binding(_base._effective_memory_class(item), item.setdefault("metadata", {}))

        item["updated_at"] = now
        item["metadata"] = ensure_decay_metadata(
            item.setdefault("metadata", {}),
            fallback_at=item.get("created_at") or now,
        )
        _base._append_memory_changelog(
            item,
            "update",
            {
                "type": request.type,
                "memory_class": request.memory_class.value if request.memory_class else None,
                "event_role": request.event_role.value if request.event_role else None,
                "content_changed": request.content is not None,
                "metadata_changed": request.metadata_patch is not None,
            },
        )
        _projection._update_mem0_record(item)
        _owner._save_live_memories(live_records)
        return item

    raise HTTPException(status_code=404, detail="memory not found in live store")


def _upsert_live_memory(request: MemoryUpsertRequest) -> tuple[str, dict]:
    from . import app_core as _owner
    canonical_project = rt._canonical_project(request.project)
    existing = _base._find_live_memory_by_upsert_key(canonical_project, request.upsert_key)
    if existing is None:
        record = _remember_live_memory(
            RememberRequest(
                project=canonical_project,
                type=request.type,
                memory_class=request.memory_class,
                event_role=request.event_role,
                observed_at=request.observed_at,
                valid_from=request.valid_from,
                valid_to=request.valid_to,
                open_interval=request.open_interval,
                supersedes_revision_id=request.supersedes_revision_id,
                source_episode_id=request.source_episode_id,
                source_uri=request.source_uri,
                source_digest=request.source_digest,
                content=request.content,
                concepts=request.concepts,
                files=request.files,
                upsert_key=request.upsert_key,
                metadata=request.metadata,
            )
        )
        return "created", record

    metadata_patch = _base._user_memory_metadata(request.metadata) if request.metadata is not None else None
    updated = _update_live_memory(
        MemoryUpdateRequest(
            id=existing["source_id"],
            project=canonical_project,
            type=request.type,
            memory_class=request.memory_class,
            event_role=request.event_role,
            observed_at=request.observed_at,
            valid_from=request.valid_from,
            valid_to=request.valid_to,
            open_interval=request.open_interval,
            supersedes_revision_id=request.supersedes_revision_id,
            source_episode_id=request.source_episode_id,
            source_uri=request.source_uri,
            source_digest=request.source_digest,
            content=request.content,
            concepts=request.concepts,
            files=request.files,
            metadata_patch=metadata_patch,
        )
    )
    updated.setdefault("metadata", {})["upsert_key"] = request.upsert_key
    if rt._memory_store_is_authoritative():
        _owner._save_live_memories([updated])
        if metadata_patch is not None:
            updated.setdefault("metadata", {}).update(metadata_patch)
        updated["metadata"] = ensure_decay_metadata(
            updated["metadata"],
            fallback_at=updated.get("created_at") or updated.get("updated_at"),
        )
        return "updated", updated
    live_records = rt._load_live_memories()
    for item in live_records:
        if item.get("source_id") == updated.get("source_id"):
            item_metadata = item.setdefault("metadata", {})
            if metadata_patch is not None:
                item_metadata.update(metadata_patch)
            item_metadata["upsert_key"] = request.upsert_key
            item["metadata"] = ensure_decay_metadata(item_metadata, fallback_at=item.get("created_at") or item.get("updated_at"))
            break
    _owner._save_live_memories(live_records)
    if metadata_patch is not None:
        updated.setdefault("metadata", {}).update(metadata_patch)
    updated["metadata"]["upsert_key"] = request.upsert_key
    updated["metadata"] = ensure_decay_metadata(
        updated["metadata"],
        fallback_at=updated.get("created_at") or updated.get("updated_at"),
    )
    return "updated", updated


def _crystallize_memories(request: MemoryCrystallizeRequest) -> tuple[str, dict]:
    from . import app_core as _owner
    if len(request.source_ids) < 1:
        raise HTTPException(status_code=400, detail="source_ids must not be empty")

    source_records = []
    for memory_id in request.source_ids:
        record = rt._find_live_memory(memory_id, request.project)
        if record is None:
            raise HTTPException(status_code=404, detail=f"source memory not found: {memory_id}")
        source_records.append(record)

    source_titles = [
        (record.get("metadata") or {}).get("raw_title") or _base._build_memory_title(record.get("content") or "")
        for record in source_records
    ]
    source_files = sorted(
        {
            file_path
            for record in source_records
            for file_path in ((record.get("metadata") or {}).get("files") or [])
        }
    )
    files = request.files if request.files is not None else source_files

    lines = [
        f"{request.title}",
        f"summary: {request.summary}",
        "source_ids:",
    ]
    for source_id in request.source_ids:
        lines.append(f"- {source_id}")
    if source_titles:
        lines.append("source_titles:")
        for title in source_titles:
            lines.append(f"- {title}")
    content = "\n".join(lines)

    if request.upsert_key:
        action, record = _upsert_live_memory(
            MemoryUpsertRequest(
                upsert_key=request.upsert_key,
                project=request.project,
                type=request.target_type,
                content=content,
                concepts=request.concepts,
                files=files,
            )
        )
    else:
        record = _remember_live_memory(
            RememberRequest(
                project=request.project,
                type=request.target_type,
                content=content,
                concepts=request.concepts,
                files=files,
            )
        )
        action = "created"

    metadata = record.setdefault("metadata", {})
    metadata["crystallized_from"] = request.source_ids
    metadata["crystallized_summary"] = request.summary
    metadata["crystallized_title"] = request.title
    if request.upsert_key:
        metadata["upsert_key"] = request.upsert_key

    live_records = rt._load_live_memories()
    for item in live_records:
        if item.get("source_id") == record.get("source_id"):
            item.setdefault("metadata", {}).update(metadata)
            break
    _owner._save_live_memories(live_records)
    for source_id in request.source_ids:
        rt._emit_memory_pulse(source_id, request.project)
    rt._emit_memory_pulse(record.get("source_id"), request.project)
    return action, record


def _merge_memory_records(
    source: dict,
    target: dict,
    *,
    archive_source: bool,
    target_id: str,
    now: str | None = None,
) -> tuple[dict, dict]:
    source = copy.deepcopy(source)
    target = copy.deepcopy(target)
    source_metadata = dict(source.get("metadata") or {})
    target_metadata = target.setdefault("metadata", {})
    source_files = set(source_metadata.get("files") or [])
    target_files = set(target_metadata.get("files") or [])
    source_tags = set(source.get("tags") or [])
    target_tags = set(target.get("tags") or [])
    merged_lines = [
        target.get("content") or "",
        "",
        "merged_memory:",
        f"- source_id: {source.get('source_id')}",
        f"- source_title: {source_metadata.get('raw_title') or _base._build_memory_title(source.get('content') or '')}",
        "source_content:",
        source.get("content") or "",
    ]
    target["content"] = "\n".join(line for line in merged_lines if line is not None).strip()
    target["tags"] = sorted(target_tags | source_tags)
    target_metadata["files"] = sorted(target_files | source_files)
    target_metadata["raw_title"] = _base._build_memory_title(target.get("content") or "")
    target_metadata["merged_from"] = sorted(
        set(target_metadata.get("merged_from") or []) | {source.get("source_id")}
    )
    timestamp = now or rt._utc_now_iso()
    target["updated_at"] = timestamp

    if archive_source:
        source_metadata = source.setdefault("metadata", {})
        source_metadata["archived_at"] = timestamp
        source_metadata["archive_reason"] = f"merged_into:{target_id}"
        source_metadata["merged_into"] = target_id
        source["updated_at"] = timestamp
        _base._append_memory_changelog(source, "archive", {"reason": f"merged_into:{target_id}"})

    return target, source


def _detect_duplicates(request: MemoryDetectRequest) -> list[dict]:
    limit = max(min(request.limit, 200), 1)
    records = sorted(
        [
        item for item in rt._load_live_memories()
        if app_core_hit._memory_matches_filters(item, project=request.project, include_archived=request.include_archived)
        ],
        key=lambda item: (str(item.get("project") or ""), str(item.get("source_id") or "")),
    )
    derived: dict[str, dict[str, Any]] = {}
    exact_content: dict[tuple[str, str], list[dict]] = {}
    title_and_files: dict[tuple[str, str, tuple[str, ...]], list[dict]] = {}
    titles: dict[tuple[str, str], list[dict]] = {}
    files_and_prefix: dict[tuple[str, tuple[str, ...], str], list[dict]] = {}
    for item in records:
        memory_id = str(item.get("source_id") or "")
        project = str(item.get("project") or "")
        metadata = item.get("metadata") or {}
        title = str(metadata.get("raw_title") or "").strip()
        normalized_title = title.casefold()
        text = app_core_hit._normalized_text(item.get("content"))
        files = tuple(sorted(str(value) for value in metadata.get("files") or []))
        derived[memory_id] = {
            "record": item,
            "title": title,
            "display_title": title or _base._build_memory_title(item.get("content") or ""),
        }
        if text:
            exact_content.setdefault((project, text), []).append(item)
        if normalized_title:
            titles.setdefault((project, normalized_title), []).append(item)
            if files:
                title_and_files.setdefault((project, normalized_title, files), []).append(item)
        if files and text[:120]:
            files_and_prefix.setdefault((project, files, text[:120]), []).append(item)

    candidates: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    candidate_budget = max(limit * 4, 200)

    def add_bucket_pairs(buckets: Mapping[Any, list[dict]], score: float, reason: str) -> None:
        for key in sorted(buckets, key=repr):
            bucket = sorted(buckets[key], key=lambda item: str(item.get("source_id") or ""))
            for left_index, left in enumerate(bucket):
                left_id = str(left.get("source_id") or "")
                for right in bucket[left_index + 1:]:
                    right_id = str(right.get("source_id") or "")
                    pair = (left_id, right_id)
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    candidates.append(
                        {
                            "left_id": left_id,
                            "right_id": right_id,
                            "project": left.get("project"),
                            "score": score,
                            "reason": reason,
                            "left_title": derived[left_id]["display_title"],
                            "right_title": derived[right_id]["display_title"],
                        }
                    )
                    if len(candidates) >= candidate_budget:
                        return

    add_bucket_pairs(exact_content, 1.0, "identical_content")
    if len(candidates) < candidate_budget:
        add_bucket_pairs(title_and_files, 0.92, "same_title_same_files")
    if len(candidates) < candidate_budget:
        add_bucket_pairs(titles, 0.82, "same_title")
    if len(candidates) < candidate_budget:
        add_bucket_pairs(files_and_prefix, 0.78, "same_files_similar_prefix")
    candidates.sort(key=lambda item: (item.get("score") or 0.0, item.get("left_title") or ""), reverse=True)
    return candidates[:limit]


def _project_summary_get(project: str) -> dict:
    canonical_project = rt._canonical_project(project)
    record = _base._find_live_memory_by_upsert_key(canonical_project, f"project-summary:{canonical_project}")
    if record is None:
        raise HTTPException(status_code=404, detail="project summary not found")
    return _base._serialize_memory_record(record)


