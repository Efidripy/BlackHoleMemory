"""Domain module app_core_graph: moved out of app.py (FIX-017). Cross-module calls go through module attributes."""

from __future__ import annotations

import sqlite3
from typing import Any
from . import app_runtime as rt
from .memory_service import MemoryServiceNotReady
from .temporal_contract import temporal_capability_available
from . import app_core_hit


_GRAPH_EXPANSION_RELATIONS = frozenset({"depends_on", "upgrades"})


_GRAPH_EXPANSION_LINKS_PER_NODE = 100


def _temporal_capability_available_for_runtime() -> bool:
    if not rt._memory_store_is_authoritative():
        return True
    return temporal_capability_available(rt._memory_service().repository.path)


def _extract_mem0_ids(add_result: Any) -> list[str]:
    if not isinstance(add_result, dict):
        return []
    return [
        str(item["id"])
        for item in add_result.get("results") or []
        if isinstance(item, dict) and item.get("id")
    ]


def _query_suggestions(project: str | None = None) -> list[str]:
    items = [
        item for item in rt._load_live_memories()
        if app_core_hit._memory_matches_filters(item, project=project, include_archived=False)
    ]
    tags: dict[str, int] = {}
    types: dict[str, int] = {}
    for item in items:
        for tag in item.get("tags") or []:
            tags[tag] = tags.get(tag, 0) + 1
        memory_type = item.get("memory_type")
        if memory_type:
            types[memory_type] = types.get(memory_type, 0) + 1
    suggestions = []
    for tag, _ in sorted(tags.items(), key=lambda pair: (-pair[1], pair[0]))[:5]:
        suggestions.append(f"concept:{tag}")
    for memory_type, _ in sorted(types.items(), key=lambda pair: (-pair[1], pair[0]))[:3]:
        suggestions.append(f"type:{memory_type}")
    suggestions.extend(
        [
            "latest checkpoint",
            "project map",
            "recent activity",
            "validation snapshot",
        ]
    )
    return list(dict.fromkeys(suggestions))[:12]


def _qdrant_payload_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Flatten projection metadata while retaining the canonical flat fields."""

    nested = payload.get("metadata")
    metadata = dict(nested) if isinstance(nested, dict) else {}
    for key, value in payload.items():
        if key not in {"content", "memory", "data", "metadata"}:
            metadata[key] = value
    return metadata


def _authoritative_graph_links_sync(source_ids: list[str], project_name: str) -> list[dict[str, str]]:
    """Return outgoing expansion links from SQLite whose targets are active in SQLite."""

    try:
        project = rt._canonical_project(project_name)
        service = rt._memory_service()
        links: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for source_id in source_ids:
            for link in service.list_links(project=project, memory_id=source_id, limit=_GRAPH_EXPANSION_LINKS_PER_NODE):
                target_id = str(link.get("target_id") or "").strip()
                relation = str(link.get("relation") or "").strip().lower()
                if link.get("source_id") != source_id or relation not in _GRAPH_EXPANSION_RELATIONS:
                    continue
                if not target_id or (target_id, relation) in seen:
                    continue
                seen.add((target_id, relation))
                links.append({"target_id": target_id, "edge_type": relation.upper()})
        if not links:
            return []
        active_ids = {
            str(record.get("source_id"))
            for record in service.get_records(
                [link["target_id"] for link in links],
                project=project,
                include_storage_lifecycle=True,
            )
            if record.get("lifecycle") == "active"
        }
    except (MemoryServiceNotReady, sqlite3.Error):
        return []
    return [link for link in links if link["target_id"] in active_ids]
