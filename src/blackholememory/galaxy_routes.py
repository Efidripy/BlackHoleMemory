"""Galaxy/Atlas surface: /bhm/galaxy, /bhm/galaxy/classic, /bhm/galaxy/data, /bhm/galaxy/stats, /bhm/galaxy/atlas.

Moved out of app.py (FIX-017). Shared app-level callables are reached through ``_deps`` (late bound,
configured once by app.py), so monkeypatching the app names keeps working.
"""

from __future__ import annotations

import time
import asyncio
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Literal
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import FileResponse
from . import app_runtime as rt
from .app_runtime import _env_float
from .api_models import GalaxyDataResponse, GalaxyStatsResponse
from .config import settings
from .resource_limits import SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS
from .filesystem_boundaries import assert_safe_path
from .galaxy_atlas import AtlasCache
from .mem0_adapter import get_qdrant_client
from .mem0_adapter import global_collection_name
from .mem0_adapter import local_collection_name
from .mem0_adapter import normalize_access_count
from .mem0_adapter import normalize_importance_score
from .runtime_storage import resolve_runtime_storage_config
from .projection_generation_binding import active_projection_generation_bindings
from .retrieval_scope import resolve_history_scope
from . import memory_contracts as _memory_contracts


MemoryClass = _memory_contracts.MemoryClass
MemoryEventRole = _memory_contracts.MemoryEventRole
STATIC_DIR = Path(__file__).parent / "static"


class _Deps:
    """Late-bound shared callables supplied by app.py."""

    def __getattr__(self, name: str):
        raise RuntimeError(f"{__name__}: dependency {name!r} is not configured")


_deps = _Deps()


def configure(**callables) -> None:
    for key, value in callables.items():
        setattr(_deps, key, value)


_GALAXY_STATS_CACHE_TTL_SECONDS = _env_float("BHM_GALAXY_STATS_CACHE_TTL_SECONDS", 300.0, 5.0, 3600.0)


_GALAXY_STATS_CACHE: dict[str, Any] | None = None


_GALAXY_STATS_CACHE_EXPIRES_AT = 0.0


_GALAXY_STATS_CACHE_LOCK = asyncio.Lock()


def _galaxy_memory_collection_names(client: Any, project: str | None) -> list[str]:
    global_name = global_collection_name()
    if project:
        route = rt._project_projection_route(project)
        names = [global_name]
        if route.collection_name is not None:
            names.insert(0, route.collection_name)
        return list(dict.fromkeys(names))

    database_path = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path
    bindings = active_projection_generation_bindings(database_path)
    active_targets = {binding.target_collection for binding in bindings}
    rollback_legacy = {local_collection_name(binding.project) for binding in bindings}

    try:
        collections = client.get_collections().collections
    except Exception:
        route = rt._project_projection_route(settings.qdrant_collection)
        names = [global_name]
        if route.collection_name is not None:
            names.insert(0, route.collection_name)
        return list(dict.fromkeys(names))

    names: list[str] = []
    for collection in collections:
        name = str(getattr(collection, "name", "") or "")
        if name == global_name or name in active_targets:
            names.append(name)
        elif name.startswith("bhm_local_memory_") and name not in rollback_legacy:
            names.append(name)
    if global_name not in names:
        names.append(global_name)
    return list(dict.fromkeys(names))


def _galaxy_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = re.split(r"[\s,|]+", value)
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    result: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _galaxy_payload_is_active(payload: dict[str, Any]) -> bool:
    lifecycle = str(payload.get("lifecycle") or payload.get("status") or "").strip().lower()
    if lifecycle in {"archived", "deprecated", "deleted", "evicted"}:
        return False
    return not any(payload.get(key) for key in ("archived_at", "archive_reason", "deleted_at", "evicted_at"))


def _galaxy_payload_text(payload: dict[str, Any]) -> str:
    text = (
        payload.get("core_insight")
        or payload.get("data")
        or payload.get("memory")
        or payload.get("content")
        or payload.get("raw_title")
        or payload.get("summary")
        or ""
    )
    return str(text or "").strip()


def _galaxy_label_from_text(text: str, fallback: str) -> str:
    first_line = str(text or "").strip().splitlines()[0].strip() if text else ""
    return (first_line or fallback)[:96]


def _galaxy_node_color(payload: dict[str, Any], collection_name: str) -> str:
    if collection_name == global_collection_name():
        return "#ffd166"
    domain = str(payload.get("domain") or "").lower()
    if domain == "frontend":
        return "#3b82f6"
    if domain == "backend":
        return "#ef4444"
    if domain == "security":
        return "#f97316"
    if domain == "product":
        return "#22c55e"
    return "#87f5c9"


def _galaxy_node_value(payload: dict[str, Any]) -> float:
    importance = normalize_importance_score(payload.get("importance_score"))
    access_count = min(normalize_access_count(payload.get("access_count")), 20)
    return round(3.2 + min(importance / 2.0, 5.0) + min(access_count / 12.0, 1.6), 2)


def _galaxy_point_node(point: Any, *, collection_name: str) -> tuple[dict[str, Any], list[str]] | None:
    payload = dict(getattr(point, "payload", None) or {})
    if not payload or not _galaxy_payload_is_active(payload):
        return None

    point_id = str(getattr(point, "id", "") or "").strip()
    source_id = str(payload.get("source_id") or payload.get("id") or point_id).strip()
    if not source_id and not point_id:
        return None

    node_id = source_id or point_id
    core_insight = _galaxy_payload_text(payload)
    tags = _galaxy_string_list(payload.get("tags") or payload.get("concepts"))
    project = rt._canonical_project(
        str(payload.get("project") or payload.get("project_name") or settings.qdrant_collection or "").strip()
    )
    memory_type = str(payload.get("memory_type") or payload.get("type") or "memory").strip() or "memory"
    node = {
        "id": node_id,
        "label": _galaxy_label_from_text(core_insight, node_id),
        "type": "memory",
        "val": _galaxy_node_value(payload),
        "color": _galaxy_node_color(payload, collection_name),
        "core_insight": core_insight[:1200],
        "tags": tags,
        "metadata": {
            key: value
            for key, value in payload.items()
            if key not in {"data", "memory", "content", "core_insight"}
        },
        "meta": {
            "project": project,
            "project_key": project,
            "memory_type": memory_type,
            "source_id": source_id,
            "product_root": "BlackHoleMemory",
            "galaxy_domain": "memory",
            "source_layer": "bhm",
            "qdrant_point_id": point_id,
            "vector_collection": collection_name,
            "tags": tags,
            "content_preview": core_insight[:320],
        },
    }
    aliases = [node_id, source_id, point_id, payload.get("mem0_hit_id")]
    aliases.extend(_galaxy_string_list(payload.get("mem0_ids")))
    aliases.extend(_galaxy_string_list(payload.get("global_mem0_ids")))
    return node, list(dict.fromkeys(str(alias).strip() for alias in aliases if str(alias or "").strip()))


def _merge_galaxy_node(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    existing_tags = _galaxy_string_list(existing.get("tags"))
    incoming_tags = _galaxy_string_list(incoming.get("tags"))
    existing["tags"] = list(dict.fromkeys(existing_tags + incoming_tags))
    existing_meta = existing.setdefault("meta", {})
    incoming_meta = incoming.get("meta") or {}
    collections = _galaxy_string_list(existing_meta.get("vector_collections") or existing_meta.get("vector_collection"))
    collections.extend(_galaxy_string_list(incoming_meta.get("vector_collection")))
    existing_meta["vector_collections"] = list(dict.fromkeys(collections))
    if not existing.get("core_insight") and incoming.get("core_insight"):
        existing["core_insight"] = incoming["core_insight"]
    if not existing_meta.get("qdrant_point_id") and incoming_meta.get("qdrant_point_id"):
        existing_meta["qdrant_point_id"] = incoming_meta["qdrant_point_id"]


def _load_galaxy_active_nodes_sync(project: str | None, limit: int) -> tuple[list[dict[str, Any]], dict[str, str]]:
    client = get_qdrant_client()
    nodes_by_id: dict[str, dict[str, Any]] = {}
    alias_to_node_id: dict[str, str] = {}
    max_nodes = max(0, min(int(limit or 0), 5000))
    if max_nodes == 0:
        return [], {}

    for collection_name in _galaxy_memory_collection_names(client, project):
        offset = None
        while len(nodes_by_id) < max_nodes:
            try:
                points, offset = client.scroll(
                    collection_name=collection_name,
                    limit=min(256, max_nodes - len(nodes_by_id)),
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
            except Exception:
                break
            if not points:
                break

            for point in points:
                item = _galaxy_point_node(point, collection_name=collection_name)
                if item is None:
                    continue
                node, aliases = item
                node_id = node["id"]
                if node_id in nodes_by_id:
                    _merge_galaxy_node(nodes_by_id[node_id], node)
                else:
                    nodes_by_id[node_id] = node
                for alias in aliases:
                    alias_to_node_id.setdefault(alias, node_id)

            if offset is None:
                break

    nodes = sorted(nodes_by_id.values(), key=lambda node: (str((node.get("meta") or {}).get("project") or ""), node["id"]))
    return nodes, alias_to_node_id


async def _load_galaxy_active_nodes(project: str | None, limit: int) -> tuple[list[dict[str, Any]], dict[str, str]]:
    return await asyncio.to_thread(_load_galaxy_active_nodes_sync, project, limit)


def _load_galaxy_code_project_nodes_sync(project: str | None, limit: int) -> list[dict[str, Any]]:
    """Return one bounded CBM project node per current SQLite graph snapshot.

    The Galaxy receives metadata-only code summaries.  Individual code symbols
    remain on the canonical CBM graph/search surfaces; this read model only
    provides the global code-domain project constellations.
    """

    database_path = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path
    assert_safe_path(database_path)
    accepted = rt._project_aliases(project) if project else set()
    nodes: list[dict[str, Any]] = []
    with sqlite3.connect(
        database_path,
        timeout=SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS,
    ) as db:
        db.execute(
            f"PRAGMA busy_timeout={int(SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS * 1000)}"
        )
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """
            SELECT current.project, current.root_id, current.graph_snapshot_id,
                   snapshots.root_path, snapshots.repository_snapshot_id,
                   snapshots.graph_digest, snapshots.status, snapshots.summary_json
              FROM repository_code_graph_current AS current
              JOIN repository_code_graph_snapshots AS snapshots
                ON snapshots.graph_snapshot_id = current.graph_snapshot_id
             WHERE snapshots.status = 'completed'
             ORDER BY current.project
             LIMIT 256
            """
        ).fetchall()
    for row in rows:
        project_key = rt._canonical_project(str(row["project"] or "").strip())
        if not project_key or (accepted and project_key not in accepted):
            continue
        try:
            summary = json.loads(str(row["summary_json"] or "{}"))
        except (TypeError, json.JSONDecodeError):
            summary = {}
        if not isinstance(summary, dict):
            summary = {}
        display = "BlackHoleMemory" if project_key == "blackholememory" else project_key
        nodes.append(
            {
                "id": f"project::{project_key}",
                "label": display,
                "type": "project",
                "val": 14.0,
                "color": "#4cc9f0",
                "core_insight": "CBM code graph project slice",
                "tags": ["CBM", "code-graph"],
                "metadata": {
                    "domain": "code",
                    "galaxy_domain": "code",
                    "source_layer": "cbm",
                    "product_root": "BlackHoleMemory",
                },
                "meta": {
                    "project": project_key,
                    "project_key": project_key,
                    "product_root": "BlackHoleMemory",
                    "galaxy_domain": "code",
                    "source_layer": "cbm",
                    "domains": ["code"],
                    "root_id": str(row["root_id"] or ""),
                    "graph_snapshot_id": str(row["graph_snapshot_id"] or ""),
                    "repository_snapshot_id": str(row["repository_snapshot_id"] or ""),
                    "graph_digest": str(row["graph_digest"] or ""),
                    "root_path": str(row["root_path"] or ""),
                    "node_count": int(summary.get("node_count") or 0),
                    "edge_count": int(summary.get("edge_count") or 0),
                    "parser_error_count": int(summary.get("parser_error_count") or 0),
                    "content_preview": "CBM metadata-only code graph slice",
                },
            }
        )
    return nodes[: max(0, min(int(limit or 0), 256))]


async def _load_galaxy_code_project_nodes(project: str | None, limit: int) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_load_galaxy_code_project_nodes_sync, project, limit)


def _galaxy_memory_record_node(record: dict[str, Any]) -> dict[str, Any] | None:
    memory_id = str(record.get("source_id") or record.get("id") or "").strip()
    project_key = rt._canonical_project(str(record.get("project") or "").strip())
    if not memory_id or not project_key or _deps._memory_lifecycle(record) != "active":
        return None

    metadata = dict(record.get("metadata") or {})
    content = str(record.get("content") or record.get("memory") or "").strip()
    tags = _galaxy_string_list(record.get("tags") or metadata.get("tags") or metadata.get("concepts"))
    memory_type = str(record.get("memory_type") or metadata.get("memory_type") or "memory").strip() or "memory"
    memory_class = str(record.get("memory_class") or metadata.get("memory_class") or MemoryClass.UNCLASSIFIED.value)
    event_role = str(record.get("event_role") or metadata.get("event_role") or MemoryEventRole.UNCLASSIFIED.value)
    updated_at = str(record.get("updated_at") or record.get("created_at") or "")
    node_payload = {
        **metadata,
        "project": project_key,
        "memory_type": memory_type,
        "importance_score": record.get("importance_score") or metadata.get("importance_score"),
        "access_count": record.get("access_count") or metadata.get("access_count"),
    }
    return {
        "id": memory_id,
        "label": _galaxy_label_from_text(content, memory_id),
        "type": "memory",
        "val": _galaxy_node_value(node_payload),
        "color": _galaxy_node_color(node_payload, "sqlite-authoritative"),
        "core_insight": content[:1200],
        "tags": tags,
        "metadata": {
            **metadata,
            "project": project_key,
            "memory_type": memory_type,
            "memory_class": memory_class,
            "event_role": event_role,
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "lifecycle": "active",
        },
        "meta": {
            "project": project_key,
            "project_key": project_key,
            "memory_type": memory_type,
            "memory_class": memory_class,
            "event_role": event_role,
            "source_id": memory_id,
            "product_root": "BlackHoleMemory",
            "galaxy_domain": "memory",
            "source_layer": "sqlite",
            "authority": "sqlite-authoritative",
            "updated_at": updated_at,
            "tags": tags,
            "content_preview": content[:320],
        },
    }


def _load_galaxy_authoritative_nodes_sync(
    project: str | None,
    limit: int | None,
    memory_class: MemoryClass | None = None,
    event_role: MemoryEventRole | None = None,
    history_scope: str | None = None,
    freshness_days: int | None = None,
) -> tuple[list[dict[str, Any]], int, list[str]]:
    project_key = rt._canonical_project(project) if project else None
    service = rt._memory_service()
    class_value = memory_class.value if memory_class else None
    role_value = event_role.value if event_role else None
    history = resolve_history_scope(history_scope, freshness_days=freshness_days)
    if history.scope == "current" and history.freshness_days is None and (memory_class is not None or event_role is not None):
        count_kwargs: dict[str, Any] = {"project": project_key}
        list_kwargs: dict[str, Any] = {"project": project_key, "limit": limit}
        if class_value is not None:
            count_kwargs["memory_class"] = class_value
            list_kwargs["memory_class"] = class_value
        if role_value is not None:
            count_kwargs["event_role"] = role_value
            list_kwargs["event_role"] = role_value
        total = service.count_records(**count_kwargs)
        records = service.list_records(**list_kwargs)
        available_projects = [project_key] if project_key and total > 0 else [] if project_key else service.list_projects()
        nodes = [node for record in records if (node := _galaxy_memory_record_node(record)) is not None]
        return nodes, total, available_projects
    # Read the complete bounded SQLite authority page set before applying the
    # scope predicate, so recent/current modes do not lose records to an early
    # limit or a noisy historical prefix.
    list_kwargs: dict[str, Any] = {"project": project_key, "limit": None}
    if class_value is not None:
        list_kwargs["memory_class"] = class_value
    if role_value is not None:
        list_kwargs["event_role"] = role_value
    all_records = service.list_records(**list_kwargs)
    records = [
        record
        for record in all_records
        if _deps._memory_matches_filters(
            record,
            project=project_key,
            memory_class=memory_class,
            event_role=event_role,
            include_archived=False,
            include_logs=True,
            include_historical=history.include_historical,
            freshness_days=history.freshness_days,
        )
    ]
    total = len(records)
    if limit is not None:
        records = records[:limit]
    nodes = [node for record in records if (node := _galaxy_memory_record_node(record)) is not None]
    # A scoped Galaxy response must not disclose project names outside the
    # caller's requested scope.  ``list_projects()`` is intentionally global
    # for operator surfaces, so derive the suggestions from the filtered
    # records when a project is selected.
    available_projects = (
        [project_key]
        if project_key and total > 0
        else []
        if project_key
        else service.list_projects()
    )
    return nodes, total, available_projects


async def _load_galaxy_authoritative_nodes(
    project: str | None,
    limit: int | None,
    memory_class: MemoryClass | None = None,
    event_role: MemoryEventRole | None = None,
    history_scope: str | None = None,
    freshness_days: int | None = None,
) -> tuple[list[dict[str, Any]], int, list[str]]:
    return await asyncio.to_thread(
        _load_galaxy_authoritative_nodes_sync,
        project,
        limit,
        memory_class,
        event_role,
        history_scope,
        freshness_days,
    )


def _load_galaxy_authoritative_links_sync(project: str | None) -> list[dict[str, Any]]:
    project_key = rt._canonical_project(project) if project else None
    return rt._memory_service().list_links(project=project_key, limit=None)


async def _build_galaxy_data(
    project: str | None,
    limit: int | None,
    domain: str = "memory",
    memory_class: MemoryClass | None = None,
    event_role: MemoryEventRole | None = None,
    history_scope: str | None = None,
    freshness_days: int | None = None,
) -> dict[str, Any]:
    """Build a newest-first visual read model from authoritative SQLite."""

    _deps._require_typed_memory_boundary(memory_class=memory_class, event_role=event_role)
    del domain  # Kept as a compatibility query parameter for older launchers.
    history = resolve_history_scope(history_scope, freshness_days=freshness_days)
    requested_project = rt._canonical_project(project) if project else ""
    if memory_class is None and event_role is None:
        if history.scope == "current" and history.freshness_days is None:
            memory_nodes, total_memory_count, available_projects = await _load_galaxy_authoritative_nodes(
                requested_project or None,
                limit,
            )
        else:
            memory_nodes, total_memory_count, available_projects = await _load_galaxy_authoritative_nodes(
                requested_project or None,
                limit,
                history_scope=history.scope,
                freshness_days=history.freshness_days,
            )
    else:
        if history.scope == "current" and history.freshness_days is None:
            memory_nodes, total_memory_count, available_projects = await _load_galaxy_authoritative_nodes(
                requested_project or None,
                limit,
                memory_class,
                event_role,
            )
        else:
            memory_nodes, total_memory_count, available_projects = await _load_galaxy_authoritative_nodes(
                requested_project or None,
                limit,
                memory_class,
                event_role,
                history.scope,
                history.freshness_days,
            )
    visible_memory_ids = {str(node["id"]) for node in memory_nodes}
    persisted_links = await asyncio.to_thread(_load_galaxy_authoritative_links_sync, requested_project or None)

    semantic_links: list[dict[str, str]] = []
    seen_links: set[tuple[str, str, str]] = set()
    for item in persisted_links:
        source = str(item.get("source_id") or "").strip()
        target = str(item.get("target_id") or "").strip()
        relation = str(item.get("relation") or "related").strip()
        if source not in visible_memory_ids or target not in visible_memory_ids or source == target:
            continue
        key = (source, target, relation.casefold())
        if key in seen_links:
            continue
        seen_links.add(key)
        semantic_links.append({"source": source, "target": target, "type": relation})

    project_keys = list(
        dict.fromkeys(
            str((node.get("meta") or {}).get("project_key") or "")
            for node in memory_nodes
            if str((node.get("meta") or {}).get("project_key") or "")
        )
    )
    project_hubs = [
        {
            "id": f"project::{project_key}",
            "label": "BlackHoleMemory" if project_key == "blackholememory" else project_key,
            "type": "project",
            "val": 13.0,
            "color": "#cbd5e1",
            "metadata": {"domain": "memory", "authority": "sqlite-authoritative"},
            "meta": {
                "project": project_key,
                "project_key": project_key,
                "product_root": "BlackHoleMemory",
                "source_layer": "sqlite",
                "authority": "sqlite-authoritative",
            },
        }
        for project_key in project_keys
    ]
    containment_links = [
        {
            "source": f"project::{str((node.get('meta') or {}).get('project_key') or '')}",
            "target": str(node["id"]),
            "type": "belongs_to_project",
        }
        for node in memory_nodes
    ]

    nodes: list[dict[str, Any]] = [*project_hubs, *memory_nodes]
    links: list[dict[str, str]] = [*containment_links, *semantic_links]
    if not requested_project and project_hubs:
        root_id = "galaxy-root::bhm"
        nodes.insert(
            0,
            {
                "id": root_id,
                "label": "BlackHoleMemory",
                "type": "root",
                "val": 22.0,
                "color": "#f7f7f2",
                "metadata": {"domain": "memory", "authority": "sqlite-authoritative"},
                "meta": {"product_root": "BlackHoleMemory", "source_layer": "sqlite", "authority": "sqlite-authoritative"},
            },
        )
        links = [
            *({"source": root_id, "target": str(hub["id"]), "type": "root_branch"} for hub in project_hubs),
            *links,
        ]

    summary_filters: dict[str, Any] = {
        "memory_class": memory_class.value if memory_class else None,
        "event_role": event_role.value if event_role else None,
    }
    if history_scope is not None or freshness_days is not None:
        summary_filters.update({"history_scope": history.scope, "freshness_days": history.freshness_days})
    summary = {
        "project": requested_project or "all-projects",
        "memory_records": len(memory_nodes),
        "returned_memory_count": len(memory_nodes),
        "total_memory_count": total_memory_count,
        "node_count": len(nodes),
        "link_count": len(links),
        "top_projects": available_projects,
        "mode": "all" if limit is None else "limited",
        "limit": limit,
        "filters": summary_filters,
        "authority": "sqlite-authoritative",
        "order": "updated_at_desc",
        "generated_at": rt._utc_now_iso(),
    }
    payload = GalaxyDataResponse(nodes=nodes, links=links, summary=summary)
    return payload.model_dump(mode="json")


async def _get_global_galaxy_stats() -> dict[str, Any]:
    """Return cached global totals without repeatedly scrolling all Qdrant collections."""

    global _GALAXY_STATS_CACHE, _GALAXY_STATS_CACHE_EXPIRES_AT
    now = time.monotonic()
    if _GALAXY_STATS_CACHE is not None and now < _GALAXY_STATS_CACHE_EXPIRES_AT:
        return dict(_GALAXY_STATS_CACHE)

    async with _GALAXY_STATS_CACHE_LOCK:
        now = time.monotonic()
        if _GALAXY_STATS_CACHE is not None and now < _GALAXY_STATS_CACHE_EXPIRES_AT:
            return dict(_GALAXY_STATS_CACHE)
        payload = await _build_galaxy_data(None, None, domain="memory")
        stats = {
            "schema_version": "bhm.galaxy.stats.v1",
            "scope": "all-projects",
            "node_count": len(payload.get("nodes") or []),
            "link_count": len(payload.get("links") or []),
            "authority": "sqlite-authoritative",
            "bounded": False,
            "limit": None,
        }
        _GALAXY_STATS_CACHE = stats
        _GALAXY_STATS_CACHE_EXPIRES_AT = time.monotonic() + _GALAXY_STATS_CACHE_TTL_SECONDS
        return dict(stats)


async def bhm_galaxy_data(
    project: str | None = None,
    limit: str = "200",
    domain: str = "memory",
    memory_class: MemoryClass | None = None,
    event_role: MemoryEventRole | None = None,
    history_scope: Literal["current", "recent", "all"] | None = None,
    freshness_days: int | None = None,
) -> dict:
    """Return the newest SQLite-authoritative memory view for Galaxy."""

    normalized_limit = str(limit or "").strip().casefold()
    if normalized_limit in {"all", "max", "*"}:
        parsed_limit: int | None = None
    else:
        try:
            parsed_limit = int(normalized_limit)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"code": "galaxy_limit_invalid"}) from exc
        if parsed_limit < 1:
            raise HTTPException(status_code=422, detail={"code": "galaxy_limit_invalid"})
    try:
        resolve_history_scope(history_scope, freshness_days=freshness_days)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "galaxy_history_scope_invalid"}) from exc
    if history_scope is None and freshness_days is None and memory_class is None and event_role is None:
        return await _build_galaxy_data(project, parsed_limit, domain=domain)
    if memory_class is None and event_role is None:
        return await _build_galaxy_data(
            project,
            parsed_limit,
            domain=domain,
            history_scope=history_scope,
            freshness_days=freshness_days,
        )
    return await _build_galaxy_data(
        project,
        parsed_limit,
        domain=domain,
        memory_class=memory_class,
        event_role=event_role,
        history_scope=history_scope,
        freshness_days=freshness_days,
    )


async def bhm_galaxy_stats() -> dict[str, Any]:
    """Return lightweight counts for the same global graph rendered by Galaxy."""
    return await _get_global_galaxy_stats()


_ATLAS_CACHE: AtlasCache | None = None


def _atlas_cache() -> AtlasCache:
    global _ATLAS_CACHE
    if _ATLAS_CACHE is None:
        config = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir)
        _ATLAS_CACHE = AtlasCache(
            settings.runtime_dir / "galaxy" / "atlas-cache.json",
            config.database_path,
            settings.qdrant_url,
        )
    return _ATLAS_CACHE


async def bhm_galaxy_atlas(request: Request) -> dict[str, Any]:
    """Whole-memory 3D atlas (positions, projects, links). Cached; rebuilt in the background."""

    principal = getattr(request.state, "bhm_caller_principal", None)
    if principal is None or not principal.all_projects:
        raise HTTPException(status_code=403, detail={"code": "galaxy_atlas_all_projects_required"})
    return await asyncio.to_thread(_atlas_cache().snapshot)


def bhm_galaxy_view() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "galaxy-atlas.html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


def bhm_galaxy_classic_view() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "galaxy.html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )



def register(app) -> None:
    """Attach the moved routes to the application at their original position."""

    app.get("/bhm/galaxy/data", response_model=GalaxyDataResponse)(bhm_galaxy_data)
    app.get("/bhm/galaxy/stats", response_model=GalaxyStatsResponse)(bhm_galaxy_stats)
    app.get("/bhm/galaxy/atlas", include_in_schema=False)(bhm_galaxy_atlas)
    app.get("/bhm/galaxy", response_class=FileResponse)(bhm_galaxy_view)
    app.get("/bhm/galaxy/classic", response_class=FileResponse)(bhm_galaxy_classic_view)
