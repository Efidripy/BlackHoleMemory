from __future__ import annotations

from typing import Annotated, Any, Literal
from pydantic import Field
from .local_endpoint_policy import validate_local_endpoint
from .context_compiler import MAX_CONTEXT_TOKEN_BUDGET
from .agent_playbook_contract import build_agent_playbook

from . import bhm_mcp_base as _base
from .bhm_mcp_base import (
    DEFAULT_PROJECT,
    TAXONOMY_METADATA_HINT,
    _json_object as _json_object,
    _metadata_json_object,
    _parse_csv,
    _mcp_authority_rejection,
    mcp,
    MemoryClass,
    MemoryEventRole,
)


from .bhm_mcp_memory_curation import (
    bhm_checkpoint_create as bhm_checkpoint_create,
    bhm_checkpoint_get_latest as bhm_checkpoint_get_latest,
    bhm_checkpoint_list as bhm_checkpoint_list,
    bhm_delete_memory as bhm_delete_memory,
    bhm_detect_conflicts as bhm_detect_conflicts,
    bhm_detect_duplicates as bhm_detect_duplicates,
    bhm_get_memories_by_concept as bhm_get_memories_by_concept,
    bhm_get_memories_by_type as bhm_get_memories_by_type,
    bhm_list_pinned_memories as bhm_list_pinned_memories,
    bhm_memory_lint as bhm_memory_lint,
    bhm_merge_memories as bhm_merge_memories,
    bhm_pin_memory as bhm_pin_memory,
    bhm_project_map_get as bhm_project_map_get,
    bhm_project_map_upsert as bhm_project_map_upsert,
    bhm_project_resolve as bhm_project_resolve,
    bhm_project_retire as bhm_project_retire,
    bhm_set_memory_confidence as bhm_set_memory_confidence,
    bhm_unpin_memory as bhm_unpin_memory,
    bhm_vote_memory_quality as bhm_vote_memory_quality,
)
from .bhm_mcp_memory_records import (
    bhm_adr_create as bhm_adr_create,
    bhm_adr_list as bhm_adr_list,
    bhm_adr_supersede as bhm_adr_supersede,
    bhm_handoff_create as bhm_handoff_create,
    bhm_handoff_list as bhm_handoff_list,
    bhm_memory_timeline as bhm_memory_timeline,
    bhm_risk_register_get as bhm_risk_register_get,
    bhm_risk_register_update as bhm_risk_register_update,
    bhm_session_record_create as bhm_session_record_create,
    bhm_session_record_list as bhm_session_record_list,
    bhm_source_refs_attach as bhm_source_refs_attach,
    bhm_source_refs_get as bhm_source_refs_get,
    bhm_task_close as bhm_task_close,
    bhm_task_context_get as bhm_task_context_get,
    bhm_task_context_update as bhm_task_context_update,
    bhm_task_get as bhm_task_get,
    bhm_task_list as bhm_task_list,
    bhm_task_open as bhm_task_open,
    bhm_validation_snapshot_get as bhm_validation_snapshot_get,
    bhm_validation_snapshot_save as bhm_validation_snapshot_save,
)


@mcp.tool(name="bhm_health", description="Check BHM readiness, cutover state, and native BHM health.")
def bhm_health() -> dict[str, Any]:
    return {
        "base_url": validate_local_endpoint(_base.DEFAULT_BASE_URL),
        "ready": _base._get("/health/ready"),
        "cutover": _base._get("/health/cutover"),
        "bhm": _base._get("/bhm/health"),
    }


@mcp.tool(name="bhm_health_slo", description="Evaluate bounded BHM readiness, queue, projection, and provider SLO checks.")
def bhm_health_slo(
    max_hook_queue_pending: int = 100,
    max_hook_queue_failed: int = 0,
    max_hook_queue_oldest_age_ms: int = 30_000,
    max_projection_pending: int = 0,
    max_projection_failed: int = 0,
    require_provider_ready: bool = True,
) -> dict[str, Any]:
    return _base._get(
        "/bhm/health/slo",
        {
            "max_hook_queue_pending": max_hook_queue_pending,
            "max_hook_queue_failed": max_hook_queue_failed,
            "max_hook_queue_oldest_age_ms": max_hook_queue_oldest_age_ms,
            "max_projection_pending": max_projection_pending,
            "max_projection_failed": max_projection_failed,
            "require_provider_ready": require_provider_ready,
        },
    )


@mcp.tool(name="bhm_diagnostics", description="Read compact BHM diagnostics and dependency state.")
def bhm_diagnostics() -> dict[str, Any]:
    return _base._post("/bhm/diagnostics", {})


@mcp.tool(name="bhm_agent_playbook", description="Task playbook.")
def bhm_agent_playbook(request: str = "implementation") -> dict[str, Any]:
    """Return portable guidance without reading BHM state or client files.

    ``request`` is ``task_kind`` or the compact ``task_kind:language`` form,
    for example ``code_change:ru``.
    """

    task_kind, separator, language = str(request or "implementation").strip().partition(":")
    if separator and ":" in language:
        raise ValueError("playbook request must use task_kind or task_kind:language")
    return build_agent_playbook(
        task_kind=task_kind or "implementation",
        language=language or "en",
    )


@mcp.tool(name="bhm_preflight", description="Run the BHM memory preflight pattern for a project.")
def bhm_preflight(project: str = DEFAULT_PROJECT, query: str | None = None, limit: int = 5) -> dict[str, Any]:
    search_query = query or project
    return {
        "base_url": _base.DEFAULT_BASE_URL,
        "health": _base._get("/bhm/health"),
        "diagnostics": _base._post("/bhm/diagnostics", {}),
        "profile": _base._get("/bhm/profile", {"project": project}),
        "search": _base._post(
            "/bhm/search",
            {"query": search_query, "project": project, "limit": limit},
        ),
    }


@mcp.tool(name="bhm_search", description="Native BHM search. Use history_scope=current|recent|all for an explicit history view; recent defaults to 30 days and can be narrowed with freshness_days.")
def bhm_search(
    query: str = "",
    project: str | None = None,
    memory_type: str | None = None,
    memory_class: MemoryClass | None = None,
    event_role: MemoryEventRole | None = None,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    concepts_csv: str | None = None,
    files_csv: str | None = None,
    limit: int = 10,
    offset: int = 0,
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    include_archived: bool = False,
    include_logs: bool = False,
    include_historical: bool = False,
    freshness_days: Annotated[int | None, Field(ge=1, le=3650)] = None,
    history_scope: Literal["current", "recent", "all"] | None = None,
) -> dict[str, Any]:
    body = {
        "query": query,
        "limit": limit,
        "offset": offset,
        "include_archived": include_archived,
        "include_logs": include_logs,
        **({"freshness_days": freshness_days} if freshness_days is not None else {}),
        **({"history_scope": history_scope} if history_scope is not None else {}),
        **({"include_temporal_unknown": True} if include_temporal_unknown else {}),
    }
    if include_historical:
        body["include_historical"] = True
    if project:
        body["project"] = project
    if memory_type:
        body["memory_type"] = memory_type
    if memory_class:
        body["memory_class"] = memory_class.value
    if event_role:
        body["event_role"] = event_role.value
    if as_of:
        body["as_of"] = as_of
    if valid_from:
        body["valid_from"] = valid_from
    if valid_to:
        body["valid_to"] = valid_to
    if concepts_csv is not None:
        body["concepts"] = _parse_csv(concepts_csv) or []
    if files_csv is not None:
        body["files"] = _parse_csv(files_csv) or []
    if domain:
        body["domain"] = domain
    if semantic_type:
        body["semantic_type"] = semantic_type
    if priority:
        body["priority"] = priority
    return _base._post("/bhm/search", body)


@mcp.tool(name="bhm_context_compile", description="Compile a bounded project-scoped context with citations for the current task.")
def bhm_context_compile(
    query: str,
    project: str | None = None,
    memory_type: str | None = None,
    memory_class: MemoryClass | None = None,
    event_role: MemoryEventRole | None = None,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    concepts_csv: str | None = None,
    files_csv: str | None = None,
    profile: str | None = None,
    tiered_context: bool = False,
    token_budget: Annotated[int | None, Field(ge=64, le=MAX_CONTEXT_TOKEN_BUDGET)] = None,
    limit: Annotated[int | None, Field(ge=1, le=50)] = None,
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    include_archived: bool = False,
    include_logs: bool = False,
    include_historical: bool = False,
    freshness_days: Annotated[int | None, Field(ge=1, le=3650)] = None,
    history_scope: Literal["current", "recent", "all"] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "query": query,
        "include_archived": include_archived,
        "include_logs": include_logs,
        **({"include_temporal_unknown": True} if include_temporal_unknown else {}),
        **({"tiered_context": True} if tiered_context else {}),
    }
    if include_historical:
        body["include_historical"] = True
    active_profile = profile or _base._read_native_env_value("BHM_CONTEXT_PROFILE")
    if active_profile:
        body["profile"] = active_profile
    if token_budget is not None:
        body["token_budget"] = token_budget
    if limit is not None:
        body["limit"] = limit
    if project:
        body["project"] = project
    if memory_type:
        body["memory_type"] = memory_type
    if memory_class:
        body["memory_class"] = memory_class.value
    if event_role:
        body["event_role"] = event_role.value
    if as_of:
        body["as_of"] = as_of
    if valid_from:
        body["valid_from"] = valid_from
    if valid_to:
        body["valid_to"] = valid_to
    if concepts_csv is not None:
        body["concepts"] = _parse_csv(concepts_csv) or []
    if files_csv is not None:
        body["files"] = _parse_csv(files_csv) or []
    if domain:
        body["domain"] = domain
    if semantic_type:
        body["semantic_type"] = semantic_type
    if priority:
        body["priority"] = priority
    if freshness_days is not None:
        body["freshness_days"] = freshness_days
    if history_scope is not None:
        body["history_scope"] = history_scope
    return _base._post("/bhm/context/compile", body)


@mcp.tool(
    name="bhm_agent_context",
    description="Get bounded read-only task context.",
)
def bhm_agent_context(project: str) -> dict[str, Any]:
    """Compose only the bounded project-scoped read contract; it never writes state."""

    body: dict[str, Any] = {
        "project": project,
    }
    return _base._post("/bhm/agent-context", body)


@mcp.tool(name="bhm_explain_retrieval", description="Explain bounded BHM ranking, fusion, diversity, decay, and routing signals.")
def bhm_explain_retrieval(
    query: str,
    project: str | None = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
    memory_type: str | None = None,
    memory_class: MemoryClass | None = None,
    event_role: MemoryEventRole | None = None,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    concepts_csv: str | None = None,
    files_csv: str | None = None,
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    include_archived: bool = False,
    include_logs: bool = False,
    include_historical: bool = False,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "query": query,
        "limit": limit,
        "include_archived": include_archived,
        "include_logs": include_logs,
        **({"include_temporal_unknown": True} if include_temporal_unknown else {}),
    }
    if include_historical:
        body["include_historical"] = True
    if project:
        body["project"] = project
    if memory_type:
        body["memory_type"] = memory_type
    if memory_class:
        body["memory_class"] = memory_class.value
    if event_role:
        body["event_role"] = event_role.value
    if as_of:
        body["as_of"] = as_of
    if valid_from:
        body["valid_from"] = valid_from
    if valid_to:
        body["valid_to"] = valid_to
    if concepts_csv is not None:
        body["concepts"] = _parse_csv(concepts_csv) or []
    if files_csv is not None:
        body["files"] = _parse_csv(files_csv) or []
    if domain:
        body["domain"] = domain
    if semantic_type:
        body["semantic_type"] = semantic_type
    if priority:
        body["priority"] = priority
    return _base._post("/bhm/retrieval/explain", body)


@mcp.tool(name="bhm_memory_used", description="Record explicit use of retrieved BHM memories for access feedback.")
def bhm_memory_used(
    ids_csv: str,
    project: str | None = None,
    reason: Annotated[str, Field(max_length=200)] = "",
) -> dict[str, Any]:
    body: dict[str, Any] = {"ids": _parse_csv(ids_csv) or [], "reason": reason}
    if project:
        body["project"] = project
    return _base._post("/bhm/memory/used", body)


@mcp.tool(name="bhm_get_memory", description="Get a single live BHM memory entry by id.")
def bhm_get_memory(id: str, project: str | None = None) -> dict[str, Any]:
    params = {"id": id}
    if project:
        params["project"] = project
    return _base._get("/bhm/memory", params)


@mcp.tool(name="bhm_list_memories", description="List live BHM memory entries with optional project and type filters.")
def bhm_list_memories(
    project: str | None = None,
    memory_type: str | None = None,
    memory_class: MemoryClass | None = None,
    event_role: MemoryEventRole | None = None,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    include_archived: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit, "offset": offset, "include_archived": include_archived}
    if include_temporal_unknown:
        params["include_temporal_unknown"] = True
    if project:
        params["project"] = project
    if memory_type:
        params["memory_type"] = memory_type
    if memory_class:
        params["memory_class"] = memory_class.value
    if event_role:
        params["event_role"] = event_role.value
    if as_of:
        params["as_of"] = as_of
    if valid_from:
        params["valid_from"] = valid_from
    if valid_to:
        params["valid_to"] = valid_to
    return _base._get("/bhm/memories", params)


@mcp.tool(name="bhm_update_memory", description=f"Update a live BHM memory entry by id. {TAXONOMY_METADATA_HINT}")
def bhm_update_memory(
    id: str,
    project: str | None = None,
    memory_type: str | None = None,
    memory_class: MemoryClass | None = None,
    event_role: MemoryEventRole | None = None,
    observed_at: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    open_interval: bool | None = None,
    supersedes_revision_id: str | None = None,
    source_episode_id: str | None = None,
    source_uri: str | None = None,
    source_digest: str | None = None,
    content: str | None = None,
    concepts_csv: str | None = None,
    files_csv: str | None = None,
    metadata_patch_json: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"id": id}
    if project:
        body["project"] = project
    if memory_type:
        body["type"] = memory_type
    if memory_class:
        body["memory_class"] = memory_class.value
    if event_role:
        body["event_role"] = event_role.value
    for key, value in {
        "observed_at": observed_at,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "open_interval": open_interval,
        "supersedes_revision_id": supersedes_revision_id,
        "source_episode_id": source_episode_id,
        "source_uri": source_uri,
        "source_digest": source_digest,
    }.items():
        if value is not None:
            body[key] = value
    if content is not None:
        body["content"] = content
    if concepts_csv is not None:
        body["concepts"] = _parse_csv(concepts_csv) or []
    if files_csv is not None:
        body["files"] = _parse_csv(files_csv) or []
    if metadata_patch_json is not None:
        body["metadata_patch"] = _metadata_json_object(metadata_patch_json)
    return _base._post("/bhm/memory/update", body)


@mcp.tool(name="bhm_archive_memory", description="Archive a live BHM memory entry by id.")
def bhm_archive_memory(id: str, project: str | None = None, reason: str = "") -> dict[str, Any]:
    body: dict[str, Any] = {"id": id, "reason": reason}
    if project:
        body["project"] = project
    return _base._post("/bhm/memory/archive", body)


@mcp.tool(name="bhm_forget_preview", description="Preview a bounded, reversible BHM forget operation without mutating memory.")
def bhm_forget_preview(
    memory_ids_csv: str | None = None,
    upsert_keys_csv: str | None = None,
    project: str | None = None,
    operation: Literal["tombstone", "undo"] = "tombstone",
    reason: Annotated[str, Field(max_length=200)] = "forget",
    undo_window_seconds: Annotated[int, Field(ge=1, le=604800)] = 900,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/forget/preview",
        {
            "memory_ids": _parse_csv(memory_ids_csv) or [],
            "upsert_keys": _parse_csv(upsert_keys_csv) or [],
            "project": project,
            "operation": operation,
            "reason": reason,
            "undo_window_seconds": undo_window_seconds,
            "limit": limit,
        },
    )


@mcp.tool(name="bhm_forget_apply", description="Apply a previously previewed reversible BHM forget plan; requires admin capability.")
def bhm_forget_apply(
    preview_digest: Annotated[str, Field(min_length=64, max_length=64)],
    memory_ids_csv: str | None = None,
    upsert_keys_csv: str | None = None,
    project: str | None = None,
    operation: Literal["tombstone", "undo"] = "tombstone",
    reason: Annotated[str, Field(max_length=200)] = "forget",
    undo_window_seconds: Annotated[int, Field(ge=1, le=604800)] = 900,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    confirm: bool = False,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/forget/apply",
        {
            "preview_digest": preview_digest,
            "memory_ids": _parse_csv(memory_ids_csv) or [],
            "upsert_keys": _parse_csv(upsert_keys_csv) or [],
            "project": project,
            "operation": operation,
            "reason": reason,
            "undo_window_seconds": undo_window_seconds,
            "limit": limit,
            "confirm": confirm,
        },
    )


@mcp.tool(name="bhm_search_advanced", description="Search live BHM memories with structured filters and explicit history_scope=current|recent|all.")
def bhm_search_advanced(
    query: str = "",
    project: str | None = None,
    memory_type: str | None = None,
    memory_class: MemoryClass | None = None,
    event_role: MemoryEventRole | None = None,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    concepts_csv: str | None = None,
    files_csv: str | None = None,
    include_archived: bool = False,
    include_logs: bool = False,
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    freshness_days: Annotated[int | None, Field(ge=1, le=3650)] = None,
    history_scope: Literal["current", "recent", "all"] | None = None,
    limit: int = 10,
    offset: int = 0,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "query": query,
        "include_archived": include_archived,
        "include_logs": include_logs,
        **({"freshness_days": freshness_days} if freshness_days is not None else {}),
        **({"history_scope": history_scope} if history_scope is not None else {}),
        **({"include_temporal_unknown": True} if include_temporal_unknown else {}),
        "limit": limit,
        "offset": offset,
    }
    if project:
        body["project"] = project
    if memory_type:
        body["memory_type"] = memory_type
    if memory_class:
        body["memory_class"] = memory_class.value
    if event_role:
        body["event_role"] = event_role.value
    if as_of:
        body["as_of"] = as_of
    if valid_from:
        body["valid_from"] = valid_from
    if valid_to:
        body["valid_to"] = valid_to
    if concepts_csv is not None:
        body["concepts"] = _parse_csv(concepts_csv) or []
    if files_csv is not None:
        body["files"] = _parse_csv(files_csv) or []
    if domain:
        body["domain"] = domain
    if semantic_type:
        body["semantic_type"] = semantic_type
    if priority:
        body["priority"] = priority
    return _base._post("/bhm/search/advanced", body)


@mcp.tool(name="bhm_recent_activity", description="Get recent live BHM memory activity with optional filters.")
def bhm_recent_activity(
    project: str | None = None,
    memory_type: str | None = None,
    memory_class: MemoryClass | None = None,
    event_role: MemoryEventRole | None = None,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    include_archived: bool = False,
    limit: int = 10,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "include_archived": include_archived,
        "limit": limit,
        **({"include_temporal_unknown": True} if include_temporal_unknown else {}),
    }
    if project:
        body["project"] = project
    if memory_type:
        body["memory_type"] = memory_type
    if memory_class:
        body["memory_class"] = memory_class.value
    if event_role:
        body["event_role"] = event_role.value
    if as_of:
        body["as_of"] = as_of
    if valid_from:
        body["valid_from"] = valid_from
    if valid_to:
        body["valid_to"] = valid_to
    return _base._post("/bhm/recent-activity", body)


@mcp.tool(name="bhm_upsert_memory", description="Reject direct MCP authority writes; use a future typed review and promotion flow.")
def bhm_upsert_memory(
    upsert_key: str,
    content: str,
    project: str = DEFAULT_PROJECT,
    memory_type: str = "workflow",
    memory_class: MemoryClass | None = None,
    event_role: MemoryEventRole | None = None,
    observed_at: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    open_interval: bool | None = None,
    supersedes_revision_id: str | None = None,
    source_episode_id: str | None = None,
    source_uri: str | None = None,
    source_digest: str | None = None,
    concepts_csv: str | None = None,
    files_csv: str | None = None,
    metadata_json: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "upsert_key": upsert_key,
        "content": content,
        "project": project,
        "type": memory_type,
        "concepts": _parse_csv(concepts_csv),
        "files": _parse_csv(files_csv),
    }
    if metadata_json is not None:
        body["metadata"] = _metadata_json_object(metadata_json)
    if memory_class:
        body["memory_class"] = memory_class.value
    if event_role:
        body["event_role"] = event_role.value
    for key, value in {
        "observed_at": observed_at,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "open_interval": open_interval,
        "supersedes_revision_id": supersedes_revision_id,
        "source_episode_id": source_episode_id,
        "source_uri": source_uri,
        "source_digest": source_digest,
    }.items():
        if value is not None:
            body[key] = value
    return _mcp_authority_rejection(project=project, payload=body)


@mcp.tool(name="bhm_get_memory_links", description="Get explicit live BHM memory links for a memory id.")
def bhm_get_memory_links(id: str, project: str) -> dict[str, Any]:
    return _base._get("/bhm/memory/links", {"id": id, "project": project})


@mcp.tool(name="bhm_link_memories", description=f"Create or update an explicit directed link between two live BHM memories. {TAXONOMY_METADATA_HINT}")
def bhm_link_memories(
    source_id: str,
    target_id: str,
    relation: str,
    project: str,
    metadata_json: str | None = None,
    ontology_schema_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "source_id": source_id,
        "target_id": target_id,
        "relation": relation,
        "project": project,
    }
    if metadata_json is not None:
        body["metadata"] = _metadata_json_object(metadata_json)
    if ontology_schema_digest is not None:
        body["ontology_schema_digest"] = ontology_schema_digest
    return _base._post("/bhm/memory/link", body)


@mcp.tool(name="bhm_unlink_memories", description="Delete an explicit directed link between two live BHM memories.")
def bhm_unlink_memories(source_id: str, target_id: str, relation: str, project: str) -> dict[str, Any]:
    return _base._delete_json(
        "/bhm/memory/link",
        {
            "source_id": source_id,
            "target_id": target_id,
            "relation": relation,
            "project": project,
        },
    )


@mcp.tool(name="bhm_crystallize", description="Create or update a crystallized summary memory from selected live BHM memory ids.")
def bhm_crystallize(
    source_ids_csv: str,
    project: str,
    title: str,
    summary: str,
    target_type: str = "pattern",
    concepts_csv: str | None = None,
    files_csv: str | None = None,
    upsert_key: str | None = None,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/crystallize",
        {
            "source_ids": _parse_csv(source_ids_csv) or [],
            "project": project,
            "title": title,
            "summary": summary,
            "target_type": target_type,
            "concepts": _parse_csv(concepts_csv),
            "files": _parse_csv(files_csv),
            "upsert_key": upsert_key,
        },
    )
