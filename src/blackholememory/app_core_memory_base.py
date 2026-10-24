"""Memory record primitives moved from app_core."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from fastapi import HTTPException
from pydantic import BaseModel

from . import app_runtime as rt
from . import memory_contracts as _memory_contracts
from .mem0_adapter import StorageNotReady
from .memory_service import MemoryServiceNotReady
from .vector_routing import route_vector_targets

MemoryMetadata = _memory_contracts.MemoryMetadata
MemoryClassSource = _memory_contracts.MemoryClassSource
EVENT_ROLE_SCHEMA_VERSION = _memory_contracts.EVENT_ROLE_SCHEMA_VERSION

def _save_live_memories(items: list[dict]) -> Path:
    try:
        return rt._memory_service().upsert_records(items)
    except MemoryServiceNotReady as exc:
        raise StorageNotReady(str(exc)) from exc


def _replace_live_memory(record: dict) -> None:
    source_id = str(record.get("source_id") or "").strip()
    if not source_id:
        raise HTTPException(status_code=422, detail="memory source_id is required")
    try:
        project = rt._canonical_project(record.get("project")) if record.get("project") else None
        if rt._memory_service().get_record(source_id, project=project) is not None:
            rt._memory_service().upsert_records([record])
            return
    except MemoryServiceNotReady:
        pass
    live_records = rt._load_live_memories()
    for index, item in enumerate(live_records):
        if item.get("source_id") != record.get("source_id"):
            continue
        live_records[index] = record
        from . import app_core as _owner
        _owner._save_live_memories(live_records)
        return
    raise HTTPException(status_code=404, detail="memory not found in live store")


def _append_memory_changelog(record: dict, action: str, details: dict | None = None) -> dict:
    metadata = record.setdefault("metadata", {})
    changelog = metadata.setdefault("changelog", [])
    changelog.append(
        {
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "action": action,
            "details": details or {},
        }
    )
    metadata["changelog"] = changelog[-200:]
    return record


def _serialize_memory_record(record: dict) -> dict:
    metadata = dict(record.get("metadata") or {})
    memory_class = str(
        record.get("memory_class")
        or metadata.get("memory_class")
        or _memory_contracts.MemoryClass.UNCLASSIFIED.value
    )
    return {
        "id": record.get("source_id"),
        "title": metadata.get("raw_title") or _build_memory_title(record.get("content") or ""),
        "project": record.get("project"),
        "type": record.get("memory_type"),
        "memory_class": memory_class,
        "memory_class_source": (
            record.get("memory_class_source")
            or metadata.get("memory_class_source")
            or MemoryClassSource.LEGACY_DEFAULT.value
        ),
        "memory_class_confidence": (
            record.get("memory_class_confidence")
            if record.get("memory_class_confidence") is not None
            else metadata.get("memory_class_confidence")
        ),
        "event_role": _effective_event_role(record),
        "event_role_version": (
            record.get("event_role_version")
            or metadata.get("event_role_version")
            or EVENT_ROLE_SCHEMA_VERSION
        ),
        "observed_at": record.get("observed_at") or metadata.get("observed_at"),
        "observed_at_source": record.get("observed_at_source") or metadata.get("observed_at_source") or "legacy-unknown",
        "valid_from": record.get("valid_from") or metadata.get("valid_from"),
        "valid_to": record.get("valid_to") or metadata.get("valid_to"),
        "open_interval": (
            record.get("open_interval")
            if record.get("open_interval") is not None
            else metadata.get("open_interval", True)
        ),
        "supersedes_revision_id": record.get("supersedes_revision_id") or metadata.get("supersedes_revision_id"),
        "source_episode_id": record.get("source_episode_id") or metadata.get("source_episode_id"),
        "source_uri": record.get("source_uri") or metadata.get("source_uri"),
        "source_digest": record.get("source_digest") or metadata.get("source_digest"),
        "content": record.get("content"),
        "concepts": record.get("tags") or [],
        "files": metadata.get("files") or [],
        "source_system": record.get("source_system"),
        "agent_id": record.get("agent_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "lifecycle": _memory_lifecycle(record),
        "archived_at": metadata.get("archived_at"),
        "archive_reason": metadata.get("archive_reason"),
        "upsert_key": metadata.get("upsert_key"),
        "session_refs": record.get("session_refs") or [],
        "metadata": metadata,
    }


def _is_archived_memory(record: dict) -> bool:
    metadata = record.get("metadata") or {}
    lifecycle = str(metadata.get("lifecycle") or "").lower()
    return bool(metadata.get("archived_at") or record.get("archived_at") or lifecycle in {"archived", "deprecated"})


def _memory_lifecycle(record: dict) -> str:
    metadata = record.get("metadata") or {}
    lifecycle = str(record.get("lifecycle") or metadata.get("lifecycle") or "active").strip().lower()
    if lifecycle in {"tombstone", "tombstoned", "purged", "deleted"}:
        return "tombstoned"
    if lifecycle in {"archived", "archive", "deprecated"} or _is_archived_memory(record):
        return "archived"
    return "active"


def _effective_memory_class(record: Mapping[str, Any]) -> str:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    return str(
        record.get("memory_class")
        or metadata.get("memory_class")
        or _memory_contracts.MemoryClass.UNCLASSIFIED.value
    ).strip().lower()


def _effective_event_role(record: Mapping[str, Any]) -> str:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    return str(
        record.get("event_role")
        or metadata.get("event_role")
        or _memory_contracts.MemoryEventRole.UNCLASSIFIED.value
    ).strip().lower()


def _metadata_matches_taxonomy_filters(
    metadata: dict[str, Any],
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    include_archived: bool = False,
    include_logs: bool = True,
) -> bool:
    lifecycle = str(metadata.get("lifecycle") or "").lower()
    if lifecycle in {"tombstone", "tombstoned", "purged", "deleted"}:
        return False
    if not include_archived and (metadata.get("archived_at") or lifecycle in {"archived", "deprecated"}):
        return False

    record_semantic_type = str(metadata.get("semantic_type") or "").lower()
    if not include_logs and record_semantic_type in {"log", "error"}:
        return False

    if domain and metadata.get("domain") != domain:
        return False
    if semantic_type and metadata.get("semantic_type") != semantic_type:
        return False
    if priority and metadata.get("priority") != priority:
        return False

    return True


def _find_live_memory_by_upsert_key(project: str, upsert_key: str) -> dict | None:
    try:
        record = rt._memory_service().get_record_by_upsert_key(
            rt._canonical_project(project),
            upsert_key,
        )
        if record is not None:
            return record
    except MemoryServiceNotReady:
        pass
    accepted_projects = rt._project_aliases(project)
    for item in rt._load_live_memories():
        metadata = item.get("metadata") or {}
        # Tombstoned/archived records are historical state, not a live
        # idempotency target. A new guarded upsert must create an active
        # record; silently updating a tombstone would make a successful
        # projection invisible to retrieval and leave rollback ambiguous.
        lifecycle = str(item.get("lifecycle") or metadata.get("lifecycle") or "active")
        if (
            item.get("project") in accepted_projects
            and lifecycle == "active"
            and metadata.get("upsert_key") == upsert_key
        ):
            return item
    return None


def _resolve_live_memory_for_vector_item(item: dict, project: str | None = None) -> dict | None:
    metadata = item.get("metadata") or {}
    candidate_ids = [
        metadata.get("source_id"),
        metadata.get("id"),
        item.get("source_id"),
        item.get("id"),
        item.get("hash"),
    ]
    for candidate_id in candidate_ids:
        if not candidate_id:
            continue
        record = rt._find_live_memory(str(candidate_id), project)
        if record is not None:
            return record

    return None


def _build_memory_title(content: str) -> str:
    first_line = (content or "").strip().splitlines()[0] if (content or "").strip() else "memory"
    return first_line[:80]


_PROTECTED_MEMORY_METADATA_KEYS = frozenset(
    {
        "project",
        "source_system",
        "source_id",
        "memory_type",
        "memory_class",
        "memory_class_source",
        "memory_class_confidence",
        "event_role",
        "event_role_version",
        "tags",
        "session_refs",
        "created_at",
        "updated_at",
        "raw_title",
        "confidence",
        "files",
        "upsert_key",
        "archived_at",
        "archive_reason",
        "changelog",
        "mem0_ids",
        "access_count",
        "last_accessed_at",
    }
)


def _metadata_to_dict(metadata: MemoryMetadata | dict[str, Any] | None) -> dict[str, Any]:
    if metadata is None:
        return {}
    if isinstance(metadata, BaseModel):
        if hasattr(metadata, "model_dump"):
            return metadata.model_dump(mode="json", exclude_none=True)
        return metadata.dict(exclude_none=True)
    return dict(metadata)


def _user_memory_metadata(metadata: MemoryMetadata | dict[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    metadata_payload = _metadata_to_dict(metadata)
    return {
        key: value
        for key, value in metadata_payload.items()
        if key not in _PROTECTED_MEMORY_METADATA_KEYS
    }


def _validate_procedure_contract_binding(memory_class: str, metadata: Mapping[str, Any]) -> None:
    has_contract = isinstance(metadata.get("procedure_contract"), Mapping)
    is_procedural = str(memory_class or "").strip().casefold() == _memory_contracts.MemoryClass.PROCEDURAL.value
    if is_procedural and not has_contract:
        raise HTTPException(
            status_code=422,
            detail={"code": "procedural_memory_requires_procedure_contract"},
        )
    if has_contract and not is_procedural:
        raise HTTPException(
            status_code=422,
            detail={"code": "procedure_contract_requires_procedural_memory_class"},
        )


def _mem0_metadata_for_record(record: dict) -> dict[str, Any]:
    metadata = record.get("metadata") or {}
    return {
        **metadata,
        "project": record["project"],
        "source_system": record["source_system"],
        "source_id": record["source_id"],
        "memory_type": record["memory_type"],
        "memory_class": record.get("memory_class") or metadata.get("memory_class") or _memory_contracts.MemoryClass.UNCLASSIFIED.value,
        "memory_class_source": record.get("memory_class_source") or metadata.get("memory_class_source") or MemoryClassSource.LEGACY_DEFAULT.value,
        "memory_class_confidence": record.get("memory_class_confidence") if record.get("memory_class_confidence") is not None else metadata.get("memory_class_confidence"),
        "event_role": record.get("event_role") or metadata.get("event_role") or _memory_contracts.MemoryEventRole.UNCLASSIFIED.value,
        "event_role_version": record.get("event_role_version") or metadata.get("event_role_version") or EVENT_ROLE_SCHEMA_VERSION,
        "tags": record["tags"],
        "session_refs": record["session_refs"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    }


def _vector_targets_for_record(record: dict) -> list[str]:
    return list(route_vector_targets(record).targets)


def _vector_id_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = []
    return [str(item) for item in items if str(item).strip()]


def _vector_metadata_for_record(
    record: dict,
    *,
    collection_name: str,
    context_origin: str,
    vector_targets: list[str],
) -> dict[str, Any]:
    metadata = _mem0_metadata_for_record(record)
    metadata["context_origin"] = context_origin
    metadata["context_origins"] = [context_origin]
    metadata["vector_collection"] = collection_name
    metadata["vector_targets"] = vector_targets
    metadata["vector_scope"] = "local+global" if "global" in vector_targets else "local"
    return metadata


