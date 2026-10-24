"""Vector query matching moved from app_core."""

from __future__ import annotations

from . import app_core_fallback
from . import app_core_hit
from . import app_runtime as rt
from . import app_core_memory_base as _base
from .api_models import SearchRequest

def _vector_item_matches_search_request(item: dict, request: SearchRequest) -> bool:
    metadata = item.get("metadata") or {}
    metadata_source_id = metadata.get("source_id")
    project = app_core_fallback._effective_search_project(request.project)
    live_record = _base._resolve_live_memory_for_vector_item(item, project)
    if live_record is not None:
        item["metadata"] = _base._mem0_metadata_for_record(live_record)
        return app_core_hit._memory_matches_filters(
            live_record,
            project=project,
            memory_class=request.memory_class,
            event_role=request.event_role,
            include_archived=request.include_archived,
            include_logs=request.include_logs,
            domain=request.domain,
            semantic_type=request.semantic_type,
            priority=request.priority,
            freshness_days=request.freshness_days,
        )
    if metadata_source_id:
        return False

    accepted_projects = rt._project_aliases(project)
    if metadata.get("project") not in accepted_projects:
        return False
    if request.memory_class and _base._effective_memory_class(item) != request.memory_class.value:
        return False
    if request.event_role and _base._effective_event_role(item) != request.event_role.value:
        return False
    return _base._metadata_matches_taxonomy_filters(
        metadata,
        domain=request.domain,
        semantic_type=request.semantic_type,
        priority=request.priority,
        include_archived=request.include_archived,
        include_logs=request.include_logs,
    )


