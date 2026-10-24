"""Validation, memory-analysis, and task-support helpers for app_ops_task.

Functions are kept verbatim from the route module; this lower layer owns their
implementation while app_ops_task re-exports the public names.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from fastapi import HTTPException

from . import app_core, app_core_fallback, app_core_hit, app_core_hook
from . import app_runtime as rt
from .api_models import (
    EntityExtractRequest,
    LinkCycleDetectRequest,
    MemoryDetectRequest,
    MemoryRecentActivityRequest,
    MemoryVerifyRequest,
    OverlapCleanupApplyRequest,
    OverlapReportRequest,
    PolicyGuardRequest,
    SchemaUpgradeAllRequest,
    ValidationTrendReportRequest,
)
from .app_runtime import _safe_exception_text
from .memory_service import MemoryServiceNotReady
from .observation_security import contains_secret_like
from .ontology_quarantine import ARTIFACT_TYPE as ONTOLOGY_QUARANTINE_ARTIFACT_TYPE
from .ontology_quarantine import serialize_quarantine_record


def _list_ontology_quarantine(project: str, limit: int = 100) -> dict[str, Any]:
    """Return a bounded project-local worklist; no resolution is applied here."""

    canonical_project = rt._canonical_project(project)
    bounded_limit = max(1, min(int(limit), 200))
    try:
        records = rt._memory_service().list_artifact_records(
            artifact_type=ONTOLOGY_QUARANTINE_ARTIFACT_TYPE,
            project=canonical_project,
            limit=bounded_limit,
        )
    except (MemoryServiceNotReady, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "ontology_quarantine_unavailable", "reason": _safe_exception_text(exc)},
        ) from exc
    items = [serialize_quarantine_record(item) for item in records]
    return {
        "schema_version": "bhm.ontology-quarantine-list.v1",
        "project": canonical_project,
        "items": items,
        "count": len(items),
        "limit": bounded_limit,
        "execution": {
            "sqlite_mutation": False,
            "link_storage_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
        },
    }


def _serialize_adr_record(record: dict) -> dict:
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "title": record.get("title"),
        "context": record.get("context") or "",
        "decision": record.get("decision") or "",
        "consequences": record.get("consequences") or "",
        "status": record.get("status") or "accepted",
        "files": record.get("files") or [],
        "concepts": record.get("concepts") or [],
        "memory_id": record.get("memory_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
    }


def _serialize_handoff_record(record: dict) -> dict:
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "title": record.get("title"),
        "current_state": record.get("current_state") or "",
        "decisions": record.get("decisions") or "",
        "validation": record.get("validation") or "",
        "next_agent_action": record.get("next_agent_action") or "",
        "next_owner_id": record.get("next_owner_id") or "",
        "handoff_sla_deadline": record.get("handoff_sla_deadline") or "",
        "files": record.get("files") or [],
        "concepts": record.get("concepts") or [],
        "memory_id": record.get("memory_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
    }


def _serialize_validation_snapshot_record(record: dict) -> dict:
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "title": record.get("title"),
        "lint": record.get("lint") or "",
        "tests": record.get("tests") or "",
        "smoke": record.get("smoke") or "",
        "docs": record.get("docs") or "",
        "overall_status": record.get("overall_status") or "",
        "command_summary": record.get("command_summary") or "",
        "memory_id": record.get("memory_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
    }


def _recent_activity_live_memories(request: MemoryRecentActivityRequest) -> list[dict]:
    app_core_fallback._require_typed_memory_boundary(
        memory_class=request.memory_class,
        event_role=request.event_role,
        as_of=request.as_of,
        valid_from=request.valid_from,
        valid_to=request.valid_to,
        include_temporal_unknown=request.include_temporal_unknown,
    )
    items = [
        item for item in rt._load_live_memories()
        if app_core_hit._memory_matches_filters(
            item,
            project=request.project,
            memory_type=request.memory_type,
            memory_class=request.memory_class,
            event_role=request.event_role,
            as_of=request.as_of,
            valid_from=request.valid_from,
            valid_to=request.valid_to,
            include_temporal_unknown=request.include_temporal_unknown,
            include_archived=request.include_archived,
        )
    ]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    return items[: max(min(request.limit, 200), 1)]


def _get_task(task_id: str, project: str | None = None) -> dict:
    project_name = rt._canonical_project(project) if project else None
    for item in rt._load_tasks():
        if item.get("task_id") != task_id:
            continue
        if project_name and item.get("project") != project_name:
            continue
        return item
    raise HTTPException(status_code=404, detail="task not found")


def _build_reflection(project: str, max_clusters: int = 10) -> dict:
    concept_counts: dict[str, int] = {}

    memory_type_counts: dict[str, int] = {}
    for item in rt._load_live_memories():
        if not app_core_hit._memory_matches_filters(item, project=project, include_archived=False):
            continue
        memory_type = item.get("memory_type") or "unknown"
        memory_type_counts[memory_type] = memory_type_counts.get(memory_type, 0) + 1
        for concept in item.get("tags") or []:
            concept_counts[concept] = concept_counts.get(concept, 0) + 1
    clusters = [
        {"concept": concept, "count": count}
        for concept, count in sorted(concept_counts.items(), key=lambda pair: (-pair[1], pair[0]))[: max(max_clusters, 1)]
    ]
    memory_types = [
        {"type": memory_type, "count": count}
        for memory_type, count in sorted(memory_type_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:5]
    ]
    return {
        "project": project,
        "clusters": clusters,
        "memory_types": memory_types,
        "memory_count": sum(memory_type_counts.values()),
    }


def _verify_memory(request: MemoryVerifyRequest) -> dict:
    record = rt._find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return {
        "verified": True,
        "memory": app_core._serialize_memory_record(record),
    }


def _entity_extract(request: EntityExtractRequest) -> dict:
    record = rt._find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    content = record.get("content") or ""
    files = re.findall(r"\b[\w./-]+\.(?:py|ts|tsx|js|json|md|yml|yaml)\b", content)
    endpoints = re.findall(r"\b/(?:[A-Za-z0-9_.-]+/?)+", content)
    envs = re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", content)
    entities = {
        "files": sorted(set(files + ((record.get("metadata") or {}).get("files") or []))),
        "endpoints": sorted(set(endpoints)),
        "env_vars": sorted(set(envs)),
        "concepts": record.get("tags") or [],
    }
    return {"memory": app_core._serialize_memory_record(record), "entities": entities}


def _policy_guard(request: PolicyGuardRequest) -> dict:
    issues = []
    content = request.content or ""
    if contains_secret_like(content):
        issues.append({"severity": "error", "code": "possible_secret", "message": "content contains a secret-like token"})
    if len(content) > 8000:
        issues.append({"severity": "warning", "code": "too_large", "message": "content is too large for durable memory"})
    if content.count("\n") > 120:
        issues.append({"severity": "warning", "code": "raw_log_shape", "message": "content looks like a raw log dump"})
    if not request.project:
        issues.append({"severity": "warning", "code": "missing_project", "message": "project scope is missing"})
    if not request.memory_type:
        issues.append({"severity": "info", "code": "missing_type", "message": "memory type is missing"})
    return {"ok": not any(item["severity"] == "error" for item in issues), "issues": issues}


def _link_graph_stats(project: str | None = None) -> dict:
    project = rt._canonical_project(project) if project else None
    links = [item for item in rt._load_memory_links() if app_core_hook._project_matches(item.get("project"), project)]
    relation_counts = {}
    node_ids = set()
    for item in links:
        relation = item.get("relation") or "unknown"
        relation_counts[relation] = relation_counts.get(relation, 0) + 1
        node_ids.add(item.get("source_id"))
        node_ids.add(item.get("target_id"))
    return {"project": project, "link_count": len(links), "node_count": len(node_ids), "relation_counts": relation_counts}


def _reindex_memory_metadata(project: str | None = None, *, aggregate: bool = False) -> dict:
    if not project and not aggregate:
        raise HTTPException(
            status_code=403,
            detail={"code": "reindex_scope_required", "message": "project or aggregate=true is required"},
        )
    project = rt._canonical_project(project) if project else None
    live_records = rt._load_live_memories()
    updated = 0
    for item in live_records:
        if not app_core_hook._project_matches(item.get("project"), project):
            continue
        metadata = item.setdefault("metadata", {})
        content = item.get("content") or ""
        raw_title = app_core._build_memory_title(content)
        if metadata.get("raw_title") != raw_title:
            metadata["raw_title"] = raw_title
            updated += 1
    app_core._save_live_memories(live_records)
    return {"project": project, "updated": updated}


def _schema_upgrade_all(request: SchemaUpgradeAllRequest) -> dict:
    if not request.project and not request.aggregate:
        raise HTTPException(
            status_code=403,
            detail={"code": "schema_upgrade_scope_required", "message": "project or aggregate=true is required"},
        )
    project = rt._canonical_project(request.project) if request.project else None
    live_records = rt._load_live_memories()
    upgraded = 0
    for item in live_records:
        if not app_core_hook._project_matches(item.get("project"), project):
            continue
        changed = False
        metadata = item.setdefault("metadata", {})
        if "files" not in metadata:
            metadata["files"] = []
            changed = True
        if "source_refs" not in metadata:
            metadata["source_refs"] = []
            changed = True
        if "raw_title" not in metadata:
            metadata["raw_title"] = app_core._build_memory_title(item.get("content") or "")
            changed = True
        if "tags" in item and item.get("tags") is None:
            item["tags"] = []
            changed = True
        if changed:
            upgraded += 1
    app_core._save_live_memories(live_records)
    return {"project": project, "upgraded": upgraded}


def _link_cycle_detect(request: LinkCycleDetectRequest) -> dict:
    project = rt._canonical_project(request.project) if request.project else None
    links = [item for item in rt._load_memory_links() if app_core_hook._project_matches(item.get("project"), project)]
    graph: dict[str, list[str]] = {}
    for item in links:
        graph.setdefault(item.get("source_id"), []).append(item.get("target_id"))
    cycles = []
    visited = set()

    def dfs(node: str, path: list[str], seen: set[str]) -> None:
        if len(cycles) >= max(min(request.limit, 200), 1):
            return
        visited.add(node)
        for neighbor in graph.get(node, []):
            if neighbor in seen:
                start = path.index(neighbor) if neighbor in path else 0
                cycles.append(path[start:] + [neighbor])
                continue
            dfs(neighbor, path + [neighbor], seen | {neighbor})

    for node in list(graph):
        if node not in visited:
            dfs(node, [node], {node})
    return {"project": project, "cycles": cycles[: max(min(request.limit, 200), 1)], "count": len(cycles)}


def _validation_trend_report(request: ValidationTrendReportRequest) -> dict:
    project = rt._canonical_project(request.project)
    items = [item for item in app_core_hook._load_validation_snapshots() if app_core_hook._project_matches(item.get("project"), project)]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "")
    trend = [{"at": item.get("updated_at") or item.get("created_at"), "overall_status": item.get("overall_status"), "lint": item.get("lint"), "tests": item.get("tests"), "smoke": item.get("smoke")} for item in items[-max(min(request.limit, 200), 1):]]
    status_counts = Counter((item.get("overall_status") or "unknown").lower() for item in items)
    return {"project": project, "trend": trend, "status_counts": dict(status_counts)}


def _alias_stats(project: str | None = None) -> dict:
    project = rt._canonical_project(project) if project else None
    alias_counter = Counter()
    alias_projects = {}
    for item in rt._load_live_memories():
        if not app_core_hook._project_matches(item.get("project"), project):
            continue
        for alias in (item.get("metadata") or {}).get("aliases") or []:
            alias_counter[alias] += 1
            alias_projects.setdefault(alias, set()).add(item.get("project"))
    duplicates = [{"alias": alias, "count": count, "projects": sorted(alias_projects.get(alias) or [])} for alias, count in alias_counter.items() if count > 1]
    return {"project": project, "alias_count": sum(alias_counter.values()), "unique_alias_count": len(alias_counter), "duplicates": sorted(duplicates, key=lambda item: item["count"], reverse=True)}


def _overlap_report(request: OverlapReportRequest) -> dict:
    project = rt._canonical_project(request.project) if request.project else None
    duplicates = app_core._detect_duplicates(MemoryDetectRequest(project=project, limit=request.limit, include_archived=False))
    same_upsert = []
    by_key = {}
    for item in rt._load_live_memories():
        if not app_core_hit._memory_matches_filters(item, project=project, include_archived=True):
            continue
        key = ((item.get("metadata") or {}).get("upsert_key") or "")
        if not key:
            continue
        by_key.setdefault(key, []).append(item.get("source_id"))
    for key, ids in by_key.items():
        if len(ids) > 1:
            same_upsert.append({"upsert_key": key, "ids": ids})
    return {"project": project, "duplicate_candidates": duplicates[: max(min(request.limit, 200), 1)], "same_upsert_key": same_upsert}


def _overlap_cleanup_apply(request: OverlapCleanupApplyRequest) -> dict:
    if not request.project and not request.aggregate:
        raise HTTPException(
            status_code=403,
            detail={"code": "overlap_cleanup_scope_required", "message": "project or aggregate=true is required"},
        )
    project = rt._canonical_project(request.project) if request.project else None
    duplicates = app_core._detect_duplicates(MemoryDetectRequest(project=project, limit=request.limit, include_archived=False))
    merged = []
    used_ids: set[str] = set()
    plan: list[tuple[str, str]] = []
    for item in duplicates:
        source_id = item["right_id"]
        target_id = item["left_id"]
        if source_id in used_ids or target_id in used_ids:
            continue
        plan.append((source_id, target_id))
        used_ids.update((source_id, target_id))

    if not plan:
        return {"project": project, "count": 0, "items": [], "committed": True}

    planned_ids = [memory_id for pair in plan for memory_id in pair]
    canonical_project = rt._canonical_project(project) if project else None
    if rt._memory_store_is_authoritative():
        records = rt._memory_service().get_records(planned_ids, project=canonical_project)
    else:
        records = [
            item
            for item in rt._load_live_memories()
            if item.get("source_id") in planned_ids
            and app_core_hook._project_matches(item.get("project"), canonical_project)
        ]
    by_id = {str(record.get("source_id")): record for record in records}
    changed: list[dict] = []
    for source_id, target_id in plan:
        source = by_id.get(source_id)
        target = by_id.get(target_id)
        if source is None or target is None:
            raise HTTPException(status_code=404, detail="overlap cleanup memory not found")
        target, source = app_core._merge_memory_records(
            source,
            target,
            archive_source=request.archive_sources,
            target_id=target_id,
        )
        by_id[target_id] = target
        by_id[source_id] = source
        changed.extend((target, source))
        merged.append({"target": target, "source": source, "archived_source": request.archive_sources})
    app_core._save_live_memories(changed)
    return {"project": project, "count": len(merged), "items": merged, "committed": True}
