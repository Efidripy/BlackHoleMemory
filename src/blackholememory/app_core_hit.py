"""Domain module app_core_hit: moved out of app.py (FIX-017). Cross-module calls go through module attributes."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from qdrant_client.http import models as qdrant_models

from . import app_core_fallback, app_core_graph
from . import app_core_memory_base as app_core
from . import app_runtime as rt
from . import memory_contracts as _memory_contracts
from .app_runtime import _env_float
from .config import settings
from .embedding_reuse import search_with_precomputed_embedding
from .mem0_adapter import (
    decay_lambda_for_payload,
    ensure_decay_metadata,
    get_global_core_memory,
    get_project_mem0_memory,
    get_qdrant_client,
    global_collection_name,
    lexical_score,
    local_collection_name,
    memory_decay_score,
)
from .projection_generation_binding import (
    ProjectionRouteUnavailable,
    resolve_projection_route,
)
from .retention import parse_timestamp
from .retrieval_diversity import mmr_select
from .retrieval_fusion import weighted_rank_fusion
from .runtime_storage import resolve_runtime_storage_config
from .temporal_contract import TemporalValidationError, temporal_matches

MemoryClass = _memory_contracts.MemoryClass


MemoryEventRole = _memory_contracts.MemoryEventRole


_VECTOR_CONTEXT_LOCAL = "LOCAL"


_VECTOR_CONTEXT_GLOBAL = "GLOBAL"


_GRAPH_FUSION_WEIGHT = 0.04


_MMR_LAMBDA = min(_env_float("BHM_MMR_LAMBDA", 0.94, 0.0), 1.0)


def _fallback_memory_records(
    *,
    project: str | None = None,
    memory_type: str | None = None,
    memory_class: MemoryClass | str | None = None,
    event_role: MemoryEventRole | str | None = None,
    concepts: list[str] | None = None,
    files: list[str] | None = None,
    include_archived: bool = False,
    include_logs: bool = False,
    include_historical: bool = False,
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    freshness_days: int | None = None,
) -> list[dict]:
    return [
        item for item in rt._load_live_memories()
        if _memory_matches_filters(
            item,
            project=project,
            memory_type=memory_type,
            memory_class=memory_class,
            event_role=event_role,
            concepts=concepts,
            files=files,
            include_archived=include_archived,
            include_logs=include_logs,
            include_historical=include_historical,
            domain=domain,
            semantic_type=semantic_type,
            priority=priority,
            as_of=as_of,
            valid_from=valid_from,
            valid_to=valid_to,
            include_temporal_unknown=include_temporal_unknown,
            freshness_days=freshness_days,
        )
    ]


def _freshness_matches(record: Mapping[str, Any], freshness_days: int | None) -> bool:
    """Keep only records updated within the requested UTC freshness window."""

    if freshness_days is None:
        return True
    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    updated_at = parse_timestamp(
        record.get("updated_at")
        or metadata.get("updated_at")
    )
    if updated_at is None:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=freshness_days)
    return updated_at >= cutoff


def _memory_matches_filters(
    record: dict,
    project: str | None = None,
    memory_type: str | None = None,
    memory_class: MemoryClass | str | None = None,
    event_role: MemoryEventRole | str | None = None,
    concepts: list[str] | None = None,
    files: list[str] | None = None,
    include_archived: bool = False,
    include_logs: bool = True,
    include_historical: bool = False,
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    freshness_days: int | None = None,
) -> bool:
    metadata = record.get("metadata") or {}
    record_concepts = set(record.get("tags") or [])
    record_files = set(metadata.get("files") or [])
    accepted_projects = rt._project_aliases(project)

    if not app_core._metadata_matches_taxonomy_filters(
        metadata,
        domain=domain,
        semantic_type=semantic_type,
        priority=priority,
        include_archived=include_archived,
        include_logs=include_logs,
    ):
        return False
    if not _freshness_matches(record, freshness_days):
        return False
    try:
        if not temporal_matches(
            record,
            as_of=as_of,
            valid_from=valid_from,
            valid_to=valid_to,
            include_temporal_unknown=include_temporal_unknown,
        ):
            return False
    except TemporalValidationError:
        return False
    if accepted_projects and record.get("project") not in accepted_projects:
        return False
    if memory_type and record.get("memory_type") != memory_type:
        return False
    requested_memory_class = (
        memory_class.value if isinstance(memory_class, MemoryClass) else str(memory_class or "").strip().lower()
    )
    if requested_memory_class and app_core._effective_memory_class(record) != requested_memory_class:
        return False
    requested_event_role = (
        event_role.value if isinstance(event_role, MemoryEventRole) else str(event_role or "").strip().lower()
    )
    if requested_event_role and app_core._effective_event_role(record) != requested_event_role:
        return False
    if (
        not include_historical
        and requested_event_role != MemoryEventRole.TRACE.value
        and app_core._effective_event_role(record) == MemoryEventRole.TRACE.value
    ):
        return False
    if concepts and not set(concepts).issubset(record_concepts):
        return False
    if files and not set(files).issubset(record_files):
        return False
    return True


def _normalized_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _semantic_graph_node_aliases(item: dict) -> list[str]:
    metadata = item.get("metadata") or {}
    aliases = [
        metadata.get("source_id"),
        item.get("source_id"),
        item.get("id"),
        metadata.get("mem0_hit_id"),
    ]
    return list(dict.fromkeys(str(alias).strip() for alias in aliases if str(alias or "").strip()))


def _write_vector_record(
    *,
    record: dict,
    collection_name: str,
    context_origin: str,
    vector_targets: list[str],
    existing_ids: list[str],
) -> list[str]:
    memory = get_global_core_memory() if context_origin == _VECTOR_CONTEXT_GLOBAL else get_project_mem0_memory(record.get("project"))
    metadata = app_core._vector_metadata_for_record(
        record,
        collection_name=collection_name,
        context_origin=context_origin,
        vector_targets=vector_targets,
    )
    content = record.get("content") or ""
    agent_id = record.get("agent_id") or "workspace"
    updated_ids: list[str] = []
    for mem0_id in existing_ids:
        try:
            memory.update(mem0_id, data=content, metadata=metadata)
        except Exception as exc:  # broad: best-effort Mem0 update must not block authoritative flow
            app_core_fallback._LOGGER.warning("Mem0 vector update skipped (%s)", type(exc).__name__)
            continue
        updated_ids.append(mem0_id)
    if updated_ids:
        return updated_ids

    add_result = memory.add(
        [{"role": "user", "content": content}],
        user_id=settings.mem0_user_id,
        agent_id=agent_id,
        metadata=metadata,
        infer=False,
    )
    return app_core_graph._extract_mem0_ids(add_result)


def _float_score(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _apply_decay_to_vector_hit(hit: dict, now: datetime) -> dict:
    metadata = ensure_decay_metadata(
        dict(hit.get("metadata") or {}),
        fallback_at=(
            hit.get("last_accessed_at")
            or hit.get("updated_at")
            or hit.get("created_at")
            or (hit.get("metadata") or {}).get("updated_at")
            or (hit.get("metadata") or {}).get("created_at")
            or rt._utc_now_iso()
        ),
    )
    raw_score = _float_score(hit.get("score"))
    final_score = memory_decay_score(metadata, raw_qdrant_score=raw_score, now=now)
    metadata["raw_qdrant_score"] = raw_score
    metadata["decay_score"] = final_score
    metadata["decay_lambda_per_day"] = decay_lambda_for_payload(metadata)
    hit["metadata"] = metadata
    hit["score"] = final_score
    return hit


def _apply_decay_to_vector_hits(hits: list[dict], now: datetime | None = None) -> list[dict]:
    current = now or datetime.now(timezone.utc)
    return [_apply_decay_to_vector_hit(hit, current) for hit in hits]


def _search_hit_content(hit: dict) -> str:
    return str(hit.get("content") or hit.get("memory") or "")


def _search_hit_origin(hit: dict) -> str:
    metadata = hit.get("metadata") or {}
    origin = str(hit.get("context_origin") or metadata.get("context_origin") or "").upper()
    return origin if origin in {_VECTOR_CONTEXT_LOCAL, _VECTOR_CONTEXT_GLOBAL} else _VECTOR_CONTEXT_LOCAL


def _search_hit_collection(hit: dict) -> str:
    metadata = hit.get("metadata") or {}
    return str(hit.get("vector_collection") or metadata.get("vector_collection") or "")


def _hybrid_hit_key(hit: dict, fallback_index: int) -> str:
    metadata = hit.get("metadata") or {}
    candidates = [
        metadata.get("source_id"),
        hit.get("source_id"),
        metadata.get("upsert_key"),
        hit.get("id"),
        metadata.get("mem0_hit_id"),
    ]
    for candidate in candidates:
        key = str(candidate or "").strip()
        if key:
            return key
    content_key = _normalized_text(_search_hit_content(hit))
    if content_key:
        return f"content::{content_key}"
    return f"hit::{fallback_index}"


def _hybrid_hit_text(hit: dict) -> str:
    metadata = hit.get("metadata") or {}
    parts = [
        _search_hit_content(hit),
        metadata.get("raw_title"),
        " ".join(str(tag) for tag in metadata.get("tags") or []),
        " ".join(str(file) for file in metadata.get("files") or []),
        metadata.get("upsert_key"),
    ]
    return " ".join(str(part) for part in parts if str(part or "").strip())


def _rank_hybrid_vector_hits(query: str, hits: list[dict], *, k: int = 60) -> list[dict]:
    if not hits:
        return []

    indexed_hits: list[tuple[int, str, dict]] = []
    seen_keys: set[str] = set()
    for index, hit in enumerate(hits):
        key = _hybrid_hit_key(hit, index)
        if key in seen_keys:
            key = f"{key}::{index}"
        seen_keys.add(key)
        indexed_hits.append((index, key, hit))

    semantic_order = sorted(
        indexed_hits,
        key=lambda entry: (
            float(entry[2].get("score") or 0.0),
            1 if _search_hit_origin(entry[2]) == _VECTOR_CONTEXT_LOCAL else 0,
            str(
                entry[2].get("updated_at")
                or (entry[2].get("metadata") or {}).get("updated_at")
                or (entry[2].get("metadata") or {}).get("created_at")
                or ""
            ),
            -entry[0],
        ),
        reverse=True,
    )
    semantic_ranks = {key: rank for rank, (_, key, _) in enumerate(semantic_order, start=1)}

    lexical_scores = {key: lexical_score(query, _hybrid_hit_text(hit)) for _, key, hit in indexed_hits}
    lexical_order = sorted(
        indexed_hits,
        key=lambda entry: (
            -lexical_scores[entry[1]],
            semantic_ranks[entry[1]],
            entry[0],
        ),
    )
    lexical_ranks = {key: rank for rank, (_, key, _) in enumerate(lexical_order, start=1)}
    graph_candidates = [
        entry
        for entry in indexed_hits
        if float((entry[2].get("metadata") or {}).get("graph_score") or 0.0) > 0.0
    ]
    graph_order = sorted(
        graph_candidates,
        key=lambda entry: (
            -float((entry[2].get("metadata") or {}).get("graph_score") or 0.0),
            entry[0],
        ),
    )
    graph_ranks = {key: rank for rank, (_, key, _) in enumerate(graph_order, start=1)}
    fused_scores = weighted_rank_fusion(
        {
            "semantic": semantic_ranks,
            "lexical": lexical_ranks,
            "graph": graph_ranks,
        },
        k=k,
        # Graph expansions are useful corroboration, not a replacement for a
        # strong semantic/lexical match; keep their influence bounded.
        weights={"graph": _GRAPH_FUSION_WEIGHT},
    )

    ranked = sorted(
        indexed_hits,
        key=lambda entry: (
            -fused_scores[entry[1]],
            semantic_ranks[entry[1]],
            lexical_ranks[entry[1]],
            -float(entry[2].get("score") or 0.0),
            -lexical_scores[entry[1]],
            entry[0],
        ),
    )
    mmr_selections = mmr_select(
        [_hybrid_hit_text(hit) for _, _key, hit in ranked],
        [fused_scores[key] for _, key, _hit in ranked],
        lambda_param=_MMR_LAMBDA,
    )
    diversified: list[dict] = []
    for mmr_rank, selection in enumerate(mmr_selections, start=1):
        _index, key, hit = ranked[selection.index]
        metadata = dict(hit.get("metadata") or {})
        metadata["semantic_rank"] = semantic_ranks[key]
        metadata["lexical_rank"] = lexical_ranks[key]
        if key in graph_ranks:
            metadata["graph_rank"] = graph_ranks[key]
        metadata["fusion_channels"] = [
            "semantic",
            "lexical",
            *(["graph"] if key in graph_ranks else []),
        ]
        metadata["rrf_score"] = fused_scores[key]
        metadata["fusion_score"] = fused_scores[key]
        metadata["mmr_rank"] = mmr_rank
        metadata["mmr_score"] = selection.mmr_score
        metadata["diversity_penalty"] = selection.redundancy
        hit["metadata"] = metadata
        diversified.append(hit)

    return diversified


def _normalize_collection_hit(hit: dict, *, collection_name: str, context_origin: str) -> dict:
    item = dict(hit)
    metadata = dict(item.get("metadata") or {})
    metadata["context_origin"] = context_origin
    metadata["context_origins"] = [context_origin]
    metadata["vector_collection"] = collection_name
    metadata["vector_collections"] = [collection_name]
    content = _search_hit_content(item)
    item["memory"] = content
    item["content"] = content
    item["metadata"] = metadata
    item["context_origin"] = context_origin
    item["vector_collection"] = collection_name
    return item


def _qdrant_point_to_vector_hit(
    point: Any,
    *,
    collection_name: str,
    context_origin: str,
    score_override: Any | None = None,
) -> dict | None:
    payload = dict(getattr(point, "payload", None) or {})
    content = str(payload.get("data") or payload.get("memory") or payload.get("content") or "").strip()
    if not content:
        return None
    metadata = app_core_graph._qdrant_payload_metadata(payload)
    metadata["mem0_hit_id"] = str(getattr(point, "id", "") or "")
    raw_score = payload.get("score") or payload.get("decay_score")
    if raw_score in (None, "") and score_override is not None:
        raw_score = score_override
    hit = {
        "id": str(getattr(point, "id", "") or ""),
        "content": content,
        "memory": content,
        "score": _float_score(raw_score),
        "metadata": metadata,
    }
    return _normalize_collection_hit(hit, collection_name=collection_name, context_origin=context_origin)


def _fetch_qdrant_hit_by_source_id_sync(target_id: str, project_name: str) -> dict | None:
    client = get_qdrant_client()
    source_id = str(target_id or "").strip()
    if not source_id:
        return None
    scroll_filter = qdrant_models.Filter(
        must=[
            qdrant_models.FieldCondition(
                key="source_id",
                match=qdrant_models.MatchValue(value=source_id),
            )
        ]
    )
    route = rt._project_projection_route(project_name)
    collections: list[tuple[str, str]] = []
    if route.collection_name is not None:
        collections.append((route.collection_name, _VECTOR_CONTEXT_LOCAL))
    collections.append((global_collection_name(), _VECTOR_CONTEXT_GLOBAL))
    for collection_name, context_origin in collections:
        try:
            points, _offset = client.scroll(
                collection_name=collection_name,
                scroll_filter=scroll_filter,
                limit=1,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:  # broad: Qdrant fallback probe must continue across unavailable collections
            app_core_fallback._LOGGER.warning("Qdrant scroll probe skipped (%s)", type(exc).__name__)
            continue
        if not points:
            continue
        return _qdrant_point_to_vector_hit(points[0], collection_name=collection_name, context_origin=context_origin)

    for collection_name, context_origin in collections:
        try:
            points = client.retrieve(
                collection_name=collection_name,
                ids=[source_id],
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:  # broad: Qdrant fallback probe must continue across unavailable collections
            app_core_fallback._LOGGER.warning("Qdrant retrieve probe skipped (%s)", type(exc).__name__)
            continue
        if not points:
            continue
        return _qdrant_point_to_vector_hit(points[0], collection_name=collection_name, context_origin=context_origin)
    return None


async def _fetch_qdrant_hit_by_source_id(target_id: str, project_name: str) -> dict | None:
    return await asyncio.to_thread(_fetch_qdrant_hit_by_source_id_sync, target_id, project_name)


def _copy_graph_expansion_hit(hit: dict, *, source_id: str, link_type: str, graph_rank: int) -> dict:
    item = dict(hit)
    metadata = dict(item.get("metadata") or {})
    metadata["graph_metadata"] = {
        "is_graph_expansion": True,
        "extended_from": source_id,
        "link_type": link_type,
    }
    metadata["graph_rank"] = graph_rank
    metadata["graph_score"] = round(1.0 / max(graph_rank, 1), 6)
    item["metadata"] = metadata
    return item


async def _augment_hits_with_graph_expansion(hits: list[dict], project_name: str, now: datetime) -> list[dict]:
    if not hits:
        return hits

    existing_by_id: dict[str, dict] = {}
    for hit in hits:
        for node_id in _semantic_graph_node_aliases(hit):
            existing_by_id.setdefault(node_id, hit)

    seed_hits = hits[:2]
    seed_ids = {alias for hit in seed_hits for alias in _semantic_graph_node_aliases(hit)}
    expansions_by_source: dict[str, list[dict]] = {}
    promoted_ids: set[str] = set()
    graph_rank = 0

    for seed in seed_hits:
        source_aliases = _semantic_graph_node_aliases(seed)
        source_id = source_aliases[0] if source_aliases else ""
        if not source_aliases or not source_id:
            continue
        links = await asyncio.to_thread(app_core_graph._authoritative_graph_links_sync, source_aliases, project_name)
        for link in links:
            target_id = str(link.get("target_id") or "").strip()
            link_type = str(link.get("edge_type") or "").strip().upper()
            if not target_id or target_id == source_id or target_id in seed_ids or target_id in promoted_ids:
                continue
            target_hit = existing_by_id.get(target_id)
            if target_hit is None:
                target_hit = await _fetch_qdrant_hit_by_source_id(target_id, project_name)
                if target_hit is not None:
                    target_hit = _apply_decay_to_vector_hit(target_hit, now)
            if target_hit is None:
                continue
            graph_rank += 1
            expansions_by_source.setdefault(source_id, []).append(
                _copy_graph_expansion_hit(
                    target_hit,
                    source_id=source_id,
                    link_type=link_type,
                    graph_rank=graph_rank,
                )
            )
            promoted_ids.add(target_id)

    if not promoted_ids:
        return hits

    expanded: list[dict] = []
    for hit in hits:
        aliases = _semantic_graph_node_aliases(hit)
        node_id = aliases[0] if aliases else ""
        if any(alias in promoted_ids for alias in aliases):
            continue
        expanded.append(hit)
        if node_id:
            expanded.extend(expansions_by_source.get(node_id, []))
    return expanded


def _search_memory_collection(
    *,
    query: str,
    project_name: str,
    context_origin: str,
    limit: int,
    candidate_filters: dict[str, Any] | None = None,
    query_embedding: Any | None = None,
) -> list[dict]:
    if not query.strip():
        return []
    if context_origin == _VECTOR_CONTEXT_GLOBAL:
        collection_name = global_collection_name()
        memory = get_global_core_memory()
    else:
        legacy_collection = local_collection_name(project_name)
        database_path = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path
        route = resolve_projection_route(
            project=project_name,
            legacy_collection=legacy_collection,
            database=database_path,
            embedding_model=settings.mem0_embedding_model,
            vector_dimensions=settings.mem0_embedding_dims,
            distance="Cosine",
        )
        if route.collection_name is None:
            raise ProjectionRouteUnavailable(
                f"local projection route unavailable: {route.reason}"
            )
        collection_name = route.collection_name
        memory = get_project_mem0_memory(
            project_name,
            collection_name=collection_name,
            ensure_collection=collection_name == legacy_collection,
        )

    filters = candidate_filters or {"user_id": settings.mem0_user_id}
    if query_embedding is None:
        result = memory.search(query, top_k=max(limit, 1), filters=filters)
    else:
        result = search_with_precomputed_embedding(
            memory,
            query,
            query_embedding,
            top_k=max(limit, 1),
            filters=filters,
        )
    if isinstance(result, dict):
        raw_hits = result.get("results") or []
    elif isinstance(result, list):
        raw_hits = result
    else:
        raw_hits = []
    normalized_hits = [
        _normalize_collection_hit(hit, collection_name=collection_name, context_origin=context_origin)
        for hit in raw_hits
        if isinstance(hit, dict)
    ]
    return normalized_hits[: max(limit, 1)]


def _merge_unique_strings(*values: Any) -> list[str]:
    merged: list[str] = []
    for value in values:
        if isinstance(value, str):
            candidates = [value]
        elif isinstance(value, (list, tuple, set)):
            candidates = list(value)
        else:
            candidates = []
        for candidate in candidates:
            item = str(candidate).strip()
            if item and item not in merged:
                merged.append(item)
    return merged


def merge_and_sort_hits(local_hits: list[dict], global_hits: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}

    def hit_key(hit: dict, fallback_index: int) -> str:
        content_key = _normalized_text(_search_hit_content(hit))
        if content_key:
            return f"content::{content_key}"
        metadata = hit.get("metadata") or {}
        stable_id = hit.get("id") or hit.get("source_id") or metadata.get("source_id") or metadata.get("upsert_key")
        return f"id::{stable_id or fallback_index}"

    def hit_rank(hit: dict) -> tuple[float, int, str]:
        origin_rank = 1 if _search_hit_origin(hit) == _VECTOR_CONTEXT_LOCAL else 0
        metadata = hit.get("metadata") or {}
        updated = str(hit.get("updated_at") or metadata.get("updated_at") or metadata.get("created_at") or "")
        return (float(hit.get("score") or 0.0), origin_rank, updated)

    for index, hit in enumerate([*local_hits, *global_hits]):
        key = hit_key(hit, index)
        origin = _search_hit_origin(hit)
        collection_name = _search_hit_collection(hit)
        current = merged.get(key)
        if current is None:
            metadata = dict(hit.get("metadata") or {})
            metadata["context_origins"] = _merge_unique_strings(metadata.get("context_origins"), origin)
            metadata["vector_collections"] = _merge_unique_strings(metadata.get("vector_collections"), collection_name)
            hit["metadata"] = metadata
            merged[key] = hit
            continue

        current_metadata = dict(current.get("metadata") or {})
        origins = _merge_unique_strings(current_metadata.get("context_origins"), origin)
        collections = _merge_unique_strings(current_metadata.get("vector_collections"), collection_name)
        if hit_rank(hit) > hit_rank(current):
            next_metadata = dict(hit.get("metadata") or {})
            next_metadata["context_origins"] = origins
            next_metadata["vector_collections"] = collections
            hit["metadata"] = next_metadata
            merged[key] = hit
        else:
            current_metadata["context_origins"] = origins
            current_metadata["vector_collections"] = collections
            current["metadata"] = current_metadata

    ordered = list(merged.values())
    ordered.sort(key=hit_rank, reverse=True)
    return ordered


def _vector_hit_matches_filters(
    hit: dict,
    *,
    project: str | None = None,
    memory_type: str | None = None,
    memory_class: MemoryClass | str | None = None,
    event_role: MemoryEventRole | str | None = None,
    concepts: list[str] | None = None,
    files: list[str] | None = None,
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    include_archived: bool = False,
    include_logs: bool = False,
    include_historical: bool = False,
    freshness_days: int | None = None,
) -> bool:
    metadata = hit.get("metadata") or {}
    accepted_projects = rt._project_aliases(project)
    if accepted_projects and metadata.get("project") not in accepted_projects:
        return False
    if memory_type and metadata.get("memory_type") != memory_type:
        return False
    requested_memory_class = (
        memory_class.value if isinstance(memory_class, MemoryClass) else str(memory_class or "").strip().lower()
    )
    if requested_memory_class and app_core._effective_memory_class(hit) != requested_memory_class:
        return False
    requested_event_role = (
        event_role.value if isinstance(event_role, MemoryEventRole) else str(event_role or "").strip().lower()
    )
    if requested_event_role and app_core._effective_event_role(hit) != requested_event_role:
        return False
    if (
        not include_historical
        and requested_event_role != MemoryEventRole.TRACE.value
        and app_core._effective_event_role(hit) == MemoryEventRole.TRACE.value
    ):
        return False
    if concepts and not set(concepts).issubset(set(metadata.get("tags") or [])):
        return False
    if files and not set(files).issubset(set(metadata.get("files") or [])):
        return False
    if any(value is not None for value in (as_of, valid_from, valid_to)) or include_temporal_unknown:
        try:
            if not temporal_matches(
                metadata,
                as_of=as_of,
                valid_from=valid_from,
                valid_to=valid_to,
                include_temporal_unknown=include_temporal_unknown,
            ):
                return False
        except TemporalValidationError:
            return False
    if not _freshness_matches(hit, freshness_days):
        return False
    return app_core._metadata_matches_taxonomy_filters(
        metadata,
        domain=domain,
        semantic_type=semantic_type,
        priority=priority,
        include_archived=include_archived,
        include_logs=include_logs,
    )


def _bounded_trace_duration_ms(value: float) -> float:
    return round(max(0.0, min(float(value), 60_000.0)), 3)
