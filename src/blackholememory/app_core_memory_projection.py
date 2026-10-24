"""Vector projection synchronization moved from app_core."""

from __future__ import annotations

from . import app_core_hit
from . import app_core_memory_base as _base
from . import app_runtime as rt
from .mem0_adapter import (
    ensure_decay_metadata,
    global_collection_name,
    local_collection_name,
)


def _sync_mem0_record(record: dict) -> dict[str, list[str]]:
    metadata = ensure_decay_metadata(
        record.setdefault("metadata", {}),
        fallback_at=record.get("created_at") or record.get("updated_at") or rt._utc_now_iso(),
    )
    record["metadata"] = metadata
    vector_targets = _base._vector_targets_for_record(record)
    global_ids_existing = _base._vector_id_list(metadata.get("global_mem0_ids"))
    if global_ids_existing and "global" not in vector_targets:
        vector_targets.append("global")

    local_collection = local_collection_name(record.get("project"))
    if rt._memory_store_is_authoritative():
        # SQLite is the write authority after cutover.  Mem0's ``add``/``update``
        # methods write directly to Qdrant and therefore bypass the transactional
        # outbox, creating orphan projections when the explicit projector is
        # intentionally offline.  Keep routing metadata for the queued projector,
        # but never mutate Mem0/Qdrant from an authoritative route.
        metadata["vector_targets"] = vector_targets
        metadata["vector_scope"] = "local+global" if "global" in vector_targets else "local"
        vector_collections = [local_collection]
        if "global" in vector_targets:
            vector_collections.append(global_collection_name())
        metadata["vector_collections"] = vector_collections
        return {
            "local": _base._vector_id_list(metadata.get("mem0_ids")),
            "global": global_ids_existing if "global" in vector_targets else [],
        }

    local_ids = app_core_hit._write_vector_record(
        record=record,
        collection_name=local_collection,
        context_origin=app_core_hit._VECTOR_CONTEXT_LOCAL,
        vector_targets=vector_targets,
        existing_ids=_base._vector_id_list(metadata.get("mem0_ids")),
    )
    metadata["mem0_ids"] = local_ids

    global_ids: list[str] = []
    if "global" in vector_targets:
        global_ids = app_core_hit._write_vector_record(
            record=record,
            collection_name=global_collection_name(),
            context_origin=app_core_hit._VECTOR_CONTEXT_GLOBAL,
            vector_targets=vector_targets,
            existing_ids=global_ids_existing,
        )
        metadata["global_mem0_ids"] = global_ids

    vector_collections = [local_collection]
    if "global" in vector_targets:
        vector_collections.append(global_collection_name())
    metadata["vector_targets"] = vector_targets
    metadata["vector_scope"] = "local+global" if "global" in vector_targets else "local"
    metadata["vector_collections"] = vector_collections
    return {"local": local_ids, "global": global_ids}


def _update_mem0_record(record: dict) -> None:
    _sync_mem0_record(record)


