"""Context projection and tier-selection helpers for retrieval."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from . import app_core
from . import app_core_hit
from .context_tiers import ContextTier, TierBudget, TieredContextItem, compile_tiered_context

def _serialize_vector_hit(hit: dict) -> dict:
    metadata = dict(hit.get("metadata") or {})
    content = app_core_hit._search_hit_content(hit)
    context_origin = app_core_hit._search_hit_origin(hit)
    mem0_hit_id = hit.get("id")
    live_source_id = metadata.get("source_id") or hit.get("source_id") or mem0_hit_id
    if mem0_hit_id and live_source_id != mem0_hit_id:
        metadata.setdefault("mem0_hit_id", mem0_hit_id)
    return {
        "id": live_source_id,
        "title": metadata.get("raw_title") or app_core._build_memory_title(content),
        "project": metadata.get("project"),
        "type": metadata.get("memory_type"),
        "memory_class": app_core._effective_memory_class(hit),
        "memory_class_source": metadata.get("memory_class_source") or app_core.MemoryClassSource.LEGACY_DEFAULT.value,
        "memory_class_confidence": metadata.get("memory_class_confidence"),
        "event_role": app_core._effective_event_role(hit),
        "event_role_version": metadata.get("event_role_version") or app_core.EVENT_ROLE_SCHEMA_VERSION,
        "content": content,
        "memory": content,
        "concepts": metadata.get("tags") or [],
        "files": metadata.get("files") or [],
        "source_system": metadata.get("source_system"),
        "agent_id": metadata.get("agent_id"),
        "created_at": metadata.get("created_at") or hit.get("created_at"),
        "updated_at": metadata.get("updated_at") or hit.get("updated_at"),
        "archived_at": metadata.get("archived_at"),
        "archive_reason": metadata.get("archive_reason"),
        "upsert_key": metadata.get("upsert_key"),
        "session_refs": metadata.get("session_refs") or [],
        "metadata": metadata,
        "score": float(hit.get("score") or 0.0),
        "context_origin": context_origin,
    }


def _context_item_from_vector_hit(hit: dict) -> dict[str, Any]:
    """Project a retrieval hit into the deliberately small context contract."""

    metadata = dict(hit.get("metadata") or {})
    content = app_core_hit._search_hit_content(hit).strip()
    live_source_id = metadata.get("source_id") or hit.get("source_id") or hit.get("id") or ""
    source_refs = metadata.get("source_refs") or hit.get("source_refs") or []
    files = metadata.get("files") or hit.get("files") or []
    source_system = metadata.get("source_system") or hit.get("source_system")
    source_kind = (
        metadata.get("source_kind")
        or metadata.get("provenance")
        or hit.get("source_kind")
        or hit.get("provenance")
    )
    agent_id = metadata.get("agent_id") or hit.get("agent_id")
    session_refs = metadata.get("session_refs") or hit.get("session_refs") or []
    context_origin = app_core_hit._search_hit_origin(hit)
    return {
        "id": live_source_id,
        "title": metadata.get("raw_title") or app_core._build_memory_title(content),
        "project": metadata.get("project") or hit.get("project"),
        "content": content,
        "score": float(hit.get("score") or 0.0),
        "context_origin": context_origin,
        "metadata": {
            "source_refs": source_refs,
            "files": files,
            "source_id": live_source_id,
            "source_system": source_system,
            "source_kind": source_kind,
            "agent_id": agent_id,
            "session_refs": session_refs,
            "context_origin": context_origin,
            "memory_class": app_core._effective_memory_class(hit),
            "memory_class_source": metadata.get("memory_class_source") or app_core.MemoryClassSource.LEGACY_DEFAULT.value,
            "event_role": app_core._effective_event_role(hit),
            "event_role_version": metadata.get("event_role_version") or app_core.EVENT_ROLE_SCHEMA_VERSION,
        },
    }


def _context_hit_from_authoritative_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt an already-filtered SQLite record to the context hit contract."""

    serialized = app_core._serialize_memory_record(dict(record))
    metadata = dict(serialized.get("metadata") or {})
    metadata.update(
        {
            "source_id": serialized.get("id"),
            "project": serialized.get("project"),
            "memory_type": serialized.get("type"),
            "memory_class": serialized.get("memory_class"),
            "event_role": serialized.get("event_role"),
            "context_origin": app_core_hit._VECTOR_CONTEXT_LOCAL,
        }
    )
    return {
        "id": serialized.get("id"),
        "source_id": serialized.get("id"),
        "content": serialized.get("content") or "",
        "memory": serialized.get("content") or "",
        # The authoritative lexical path is ordered, but does not produce a
        # vector similarity score. Keep that distinction explicit.
        "score": 0.0,
        "context_origin": app_core_hit._VECTOR_CONTEXT_LOCAL,
        "metadata": metadata,
    }


def _tier_for_context_hit(hit: Mapping[str, Any]) -> ContextTier:
    """Classify a retrieved hit without changing its authority or lifecycle."""

    metadata = hit.get("metadata") if isinstance(hit.get("metadata"), Mapping) else {}
    # A durable tier is a canonical SQLite metadata decision made by the
    # explicit promotion contour.  It must precede the legacy session_refs
    # heuristic, otherwise a promoted aggregate would remain invisible as a
    # project-tier record merely because its provenance preserves its session.
    explicit_tier = str(metadata.get("context_tier") or "").strip().casefold()
    if explicit_tier in {tier.value for tier in ContextTier}:
        return ContextTier(explicit_tier)
    if app_core._memory_lifecycle(hit) == "archived":
        return ContextTier.ARCHIVAL
    if app_core._effective_memory_class(dict(hit)) == app_core_hit.MemoryClass.WORKING.value:
        return ContextTier.WORKING
    session_refs = metadata.get("session_refs") or hit.get("session_refs") or []
    scope = str(metadata.get("scope") or metadata.get("visibility") or "").strip().casefold()
    return ContextTier.SESSION if session_refs or scope == "session" else ContextTier.PROJECT


def _tier_budget_for_context(token_budget: int) -> TierBudget:
    """Derive bounded per-tier byte budgets from the existing token budget."""

    total_bytes = max(4, int(token_budget) * 4)
    working = max(1, total_bytes // 5)
    session = max(1, total_bytes // 4)
    archival = max(1, total_bytes // 10)
    return TierBudget(
        working_bytes=working,
        session_bytes=session,
        project_bytes=max(1, total_bytes - working - session - archival),
        archival_bytes=archival,
    )


def _compile_tiered_context_candidates(
    hits: Sequence[Mapping[str, Any]], *, token_budget: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select context candidates through the explicit, read-only tier contract."""

    items: list[TieredContextItem] = []
    candidates: dict[str, dict[str, Any]] = {}
    source_ids: dict[str, str] = {}
    for index, raw_hit in enumerate(hits, start=1):
        hit = dict(raw_hit)
        projected = _context_item_from_vector_hit(hit)
        source_id = str(projected.get("id") or f"tier-context-{index}")[:160]
        candidate_id = source_id if source_id not in candidates else f"{source_id[:140]}-{index}"
        content = str(projected.get("content") or "").strip()
        metadata = projected.get("metadata") if isinstance(projected.get("metadata"), Mapping) else {}
        digest = str(
            metadata.get("content_sha256")
            or metadata.get("content_digest")
            or hit.get("content_sha256")
            or hit.get("content_digest")
            or ""
        ).strip().lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            digest = hashlib.sha256(
                json.dumps({"id": source_id, "content": content}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
        observed_at = str(metadata.get("observed_at") or hit.get("observed_at") or "") or None
        items.append(
            TieredContextItem(
                memory_id=candidate_id,
                tier=_tier_for_context_hit(hit),
                text=content or "[empty]",
                source_digest=digest,
                observed_at=observed_at,
                priority=max(-100, min(100, int(round(float(hit.get("score") or 0.0) * 100)))),
            )
        )
        candidates[candidate_id] = projected
        source_ids[candidate_id] = source_id

    report = compile_tiered_context(tuple(items), _tier_budget_for_context(token_budget))
    selected = [candidates[item["memory_id"]] for item in report["included"]]
    return selected, {
        **report,
        "included": [
            {"id": source_ids[item["memory_id"]], "tier": item["tier"], "source_digest": item["source_digest"], "reason": item["reason"]}
            for item in report["included"]
        ],
        "omitted": [{**item, "id": source_ids.get(item["memory_id"], item["memory_id"])} for item in report["omitted"]],
        "execution": {"sqlite_mutation": False, "qdrant_mutation": False, "promotion": "none", "selection": "explicit-tiered-context"},
    }




