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
    _json_object,
    _metadata_json_object,
    _parse_csv,
    _mcp_authority_rejection,
    mcp,
    MemoryClass,
    MemoryEventRole,
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


@mcp.tool(name="bhm_checkpoint_create", description="Create or update a first-class BHM checkpoint and index it in live memory.")
def bhm_checkpoint_create(
    project: str,
    checkpoint_type: str = "workflow",
    title: str | None = None,
    content: str | None = None,
    done: str = "",
    next: str = "",
    checks: str = "",
    risks: str = "",
    concepts_csv: str | None = None,
    files_csv: str | None = None,
    upsert_key: str | None = None,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/checkpoint",
        {
            "project": project,
            "checkpoint_type": checkpoint_type,
            "title": title,
            "content": content,
            "done": done,
            "next": next,
            "checks": checks,
            "risks": risks,
            "concepts": _parse_csv(concepts_csv),
            "files": _parse_csv(files_csv),
            "upsert_key": upsert_key,
        },
    )


@mcp.tool(name="bhm_checkpoint_list", description="List first-class BHM checkpoints with optional project and type filters.")
def bhm_checkpoint_list(
    project: str | None = None,
    checkpoint_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if project:
        params["project"] = project
    if checkpoint_type:
        params["checkpoint_type"] = checkpoint_type
    return _base._get("/bhm/checkpoints", params)


@mcp.tool(name="bhm_checkpoint_get_latest", description="Get the latest first-class BHM checkpoint for a project.")
def bhm_checkpoint_get_latest(project: str, checkpoint_type: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"project": project}
    if checkpoint_type:
        params["checkpoint_type"] = checkpoint_type
    return _base._get("/bhm/checkpoint/latest", params)


@mcp.tool(name="bhm_project_map_get", description="Get the canonical first-class BHM project map for a project.")
def bhm_project_map_get(project: str) -> dict[str, Any]:
    return _base._get("/bhm/project-map", {"project": project})


@mcp.tool(name="bhm_project_resolve", description="Resolve a project id or alias to the canonical BHM project identity.")
def bhm_project_resolve(project: str = "") -> dict[str, Any]:
    return _base._get("/bhm/project/resolve", {"project": project})


@mcp.tool(
    name="bhm_project_retire",
    description=(
        "Preview or explicitly apply a safe logical project retirement. "
        "Apply requires the configured admin capability and project allowlist; "
        "it tombstones SQLite records and never unlinks a database."
    ),
)
def bhm_project_retire(
    project: str,
    apply: bool = False,
    capability: str = "",
    backup_dir: str | None = None,
) -> dict[str, Any]:
    if not apply:
        return _base._get("/bhm/project/retirement-preview", {"project": project})
    return _base._post(
        "/bhm/project/retirement/apply",
        {"project": project, "capability": capability, "backup_dir": backup_dir},
    )


@mcp.tool(name="bhm_project_map_upsert", description="Create or update the canonical first-class BHM project map for a project.")
def bhm_project_map_upsert(
    project: str,
    title: str | None = None,
    auth: str = "",
    routing: str = "",
    tests: str = "",
    deploy: str = "",
    i18n: str = "",
    websocket: str = "",
    risks: str = "",
    notes: str = "",
    files_csv: str | None = None,
    concepts_csv: str | None = None,
    upsert_key: str | None = None,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/project-map",
        {
            "project": project,
            "title": title,
            "auth": auth,
            "routing": routing,
            "tests": tests,
            "deploy": deploy,
            "i18n": i18n,
            "websocket": websocket,
            "risks": risks,
            "notes": notes,
            "files": _parse_csv(files_csv),
            "concepts": _parse_csv(concepts_csv),
            "upsert_key": upsert_key,
        },
    )


@mcp.tool(name="bhm_merge_memories", description="Merge one live BHM memory into another and optionally archive the source.")
def bhm_merge_memories(source_id: str, target_id: str, project: str, archive_source: bool = True) -> dict[str, Any]:
    return _base._post(
        "/bhm/memory/merge",
        {
            "source_id": source_id,
            "target_id": target_id,
            "project": project,
            "archive_source": archive_source,
        },
    )


@mcp.tool(name="bhm_detect_duplicates", description="Detect likely duplicate live BHM memories using deterministic heuristics.")
def bhm_detect_duplicates(project: str | None = None, limit: int = 20, include_archived: bool = False) -> dict[str, Any]:
    return _base._post(
        "/bhm/memory/detect-duplicates",
        {
            "project": project,
            "limit": limit,
            "include_archived": include_archived,
        },
    )


@mcp.tool(name="bhm_detect_conflicts", description="Detect likely conflicting live BHM memories using deterministic heuristics.")
def bhm_detect_conflicts(project: str | None = None, limit: int = 20, include_archived: bool = False) -> dict[str, Any]:
    return _base._post(
        "/bhm/memory/detect-conflicts",
        {
            "project": project,
            "limit": limit,
            "include_archived": include_archived,
        },
    )


@mcp.tool(name="bhm_memory_lint", description="Lint a live BHM memory entry for compactness, scope, and likely secret leakage.")
def bhm_memory_lint(id: str, project: str | None = None) -> dict[str, Any]:
    return _base._post(
        "/bhm/memory/lint",
        {
            "id": id,
            "project": project,
        },
    )


@mcp.tool(name="bhm_delete_memory", description="Delete a live BHM memory entry by id from the canonical live store.")
def bhm_delete_memory(id: str, project: str | None = None) -> dict[str, Any]:
    return _base._delete_json("/bhm/memory", {"id": id, "project": project})


@mcp.tool(name="bhm_get_memories_by_concept", description="List live BHM memories that contain a required concept/tag.")
def bhm_get_memories_by_concept(concept: str, project: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    params: dict[str, Any] = {"concept": concept, "limit": limit, "offset": offset}
    if project:
        params["project"] = project
    return _base._get("/bhm/memories/by-concept", params)


@mcp.tool(name="bhm_get_memories_by_type", description="List live BHM memories by memory type.")
def bhm_get_memories_by_type(memory_type: str, project: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    params: dict[str, Any] = {"memory_type": memory_type, "limit": limit, "offset": offset}
    if project:
        params["project"] = project
    return _base._get("/bhm/memories/by-type", params)


@mcp.tool(name="bhm_set_memory_confidence", description="Set an explicit confidence score on a live BHM memory.")
def bhm_set_memory_confidence(id: str, confidence: float, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memory/confidence", {"id": id, "project": project, "confidence": confidence})


@mcp.tool(name="bhm_pin_memory", description="Pin a live BHM memory for quick project recall.")
def bhm_pin_memory(id: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memory/pin", {"id": id, "project": project, "pinned": True})


@mcp.tool(name="bhm_vote_memory_quality", description="Add a lightweight 1-5 quality vote to a live BHM memory.")
def bhm_vote_memory_quality(id: str, vote: int, project: str | None = None, voter: str = "agent") -> dict[str, Any]:
    return _base._post("/bhm/memory/vote-quality", {"id": id, "project": project, "vote": vote, "voter": voter})


@mcp.tool(name="bhm_unpin_memory", description="Remove the pinned flag from a live BHM memory.")
def bhm_unpin_memory(id: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memory/pin", {"id": id, "project": project, "pinned": False})


@mcp.tool(name="bhm_list_pinned_memories", description="List pinned live BHM memories for a project.")
def bhm_list_pinned_memories(project: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if project:
        params["project"] = project
    return _base._get("/bhm/memories/pinned", params)


@mcp.tool(name="bhm_adr_create", description="Create or update a first-class BHM ADR entry.")
def bhm_adr_create(
    project: str,
    title: str,
    context: str = "",
    decision: str = "",
    consequences: str = "",
    status: str = "accepted",
    files_csv: str | None = None,
    concepts_csv: str | None = None,
    upsert_key: str | None = None,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/adr",
        {
            "project": project,
            "title": title,
            "context": context,
            "decision": decision,
            "consequences": consequences,
            "status": status,
            "files": _parse_csv(files_csv),
            "concepts": _parse_csv(concepts_csv),
            "upsert_key": upsert_key,
        },
    )


@mcp.tool(name="bhm_adr_list", description="List first-class BHM ADR entries.")
def bhm_adr_list(project: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if project:
        params["project"] = project
    return _base._get("/bhm/adrs", params)


@mcp.tool(name="bhm_adr_supersede", description="Mark one ADR as superseded by another and create a supersedes relation.")
def bhm_adr_supersede(project: str, old_id: str, new_id: str) -> dict[str, Any]:
    return _base._post("/bhm/adr/supersede", {"project": project, "old_id": old_id, "new_id": new_id})


@mcp.tool(name="bhm_handoff_create", description="Create or update a first-class BHM handoff record.")
def bhm_handoff_create(
    project: str,
    title: str,
    current_state: str = "",
    decisions: str = "",
    validation: str = "",
    next_agent_action: str = "",
    next_owner_id: str = "",
    handoff_sla_deadline: str = "",
    files_csv: str | None = None,
    concepts_csv: str | None = None,
    upsert_key: str | None = None,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/handoff",
        {
            "project": project,
            "title": title,
            "current_state": current_state,
            "decisions": decisions,
            "validation": validation,
            "next_agent_action": next_agent_action,
            "next_owner_id": next_owner_id,
            "handoff_sla_deadline": handoff_sla_deadline,
            "files": _parse_csv(files_csv),
            "concepts": _parse_csv(concepts_csv),
            "upsert_key": upsert_key,
        },
    )


@mcp.tool(name="bhm_handoff_list", description="List first-class BHM handoff records.")
def bhm_handoff_list(project: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if project:
        params["project"] = project
    return _base._get("/bhm/handoffs", params)


@mcp.tool(name="bhm_session_record_create", description="Create or update a first-class BHM session record.")
def bhm_session_record_create(
    project: str,
    title: str,
    done: str = "",
    next: str = "",
    checks: str = "",
    risks: str = "",
    decisions: str = "",
    files_touched_csv: str | None = None,
    conversation_notes: str = "",
    transcript_ref: str = "",
    upsert_key: str | None = None,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/session-record",
        {
            "project": project,
            "title": title,
            "done": done,
            "next": next,
            "checks": checks,
            "risks": risks,
            "decisions": decisions,
            "files_touched": _parse_csv(files_touched_csv),
            "conversation_notes": conversation_notes,
            "transcript_ref": transcript_ref,
            "upsert_key": upsert_key,
        },
    )


@mcp.tool(name="bhm_session_record_list", description="List first-class BHM session records.")
def bhm_session_record_list(project: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if project:
        params["project"] = project
    return _base._get("/bhm/session-records", params)


@mcp.tool(name="bhm_task_open", description="Open or resume an idempotent BHM task and its canonical session record.")
def bhm_task_open(
    task_id: Annotated[str, Field(min_length=1, max_length=256)],
    intent: Annotated[str, Field(min_length=1, max_length=8000)],
    project: str = DEFAULT_PROJECT,
    title: str = "",
    scope_in_csv: str | None = None,
    scope_out_csv: str | None = None,
    repo: str = "",
    owner: str = "",
    session_id: str = "",
    correlation_id: str = "",
    files_touched_csv: str | None = None,
    metadata_json: str | None = None,
    upsert_key: str | None = None,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/task/open",
        {
            "project": project,
            "task_id": task_id,
            "intent": intent,
            "title": title,
            "scope_in": _parse_csv(scope_in_csv) or [],
            "scope_out": _parse_csv(scope_out_csv) or [],
            "repo": repo,
            "owner": owner,
            "session_id": session_id,
            "correlation_id": correlation_id,
            "files_touched": _parse_csv(files_touched_csv) or [],
            "metadata": _json_object(metadata_json) or {},
            "upsert_key": upsert_key,
        },
    )


@mcp.tool(name="bhm_task_close", description="Close an idempotent BHM task and update its canonical session record once.")
def bhm_task_close(
    task_id: Annotated[str, Field(min_length=1, max_length=256)],
    project: str = DEFAULT_PROJECT,
    done: str = "",
    next_step: str = "",
    checks: str = "",
    risks: str = "",
    decisions: str = "",
    validation: str = "",
    files_touched_csv: str | None = None,
    conversation_notes: str = "",
    transcript_ref: str = "",
    metadata_json: str | None = None,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/task/close",
        {
            "project": project,
            "task_id": task_id,
            "done": done,
            "next": next_step,
            "checks": checks,
            "risks": risks,
            "decisions": decisions,
            "validation": validation,
            "files_touched": _parse_csv(files_touched_csv),
            "conversation_notes": conversation_notes,
            "transcript_ref": transcript_ref,
            "metadata": _json_object(metadata_json) or {},
        },
    )


@mcp.tool(name="bhm_task_get", description="Get one BHM task by task id and optional project.")
def bhm_task_get(task_id: str, project: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"task_id": task_id}
    if project:
        params["project"] = project
    return _base._get("/bhm/task", params)


@mcp.tool(name="bhm_task_list", description="List BHM tasks with optional project and status filters.")
def bhm_task_list(
    project: str | None = None,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if project:
        params["project"] = project
    if status:
        params["status"] = status
    return _base._get("/bhm/tasks", params)


@mcp.tool(name="bhm_task_context_update", description="Create or update the canonical first-class BHM task context for a project.")
def bhm_task_context_update(
    project: str,
    title: str = "active-task",
    current_task: str = "",
    status: str = "",
    pending_items: str = "",
    guidance: str = "",
    next_step: str = "",
    files_touched_csv: str | None = None,
    upsert_key: str | None = None,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/task-context",
        {
            "project": project,
            "title": title,
            "current_task": current_task,
            "status": status,
            "pending_items": pending_items,
            "guidance": guidance,
            "next_step": next_step,
            "files_touched": _parse_csv(files_touched_csv),
            "upsert_key": upsert_key,
        },
    )


@mcp.tool(name="bhm_task_context_get", description="Get the canonical first-class BHM task context for a project.")
def bhm_task_context_get(project: str) -> dict[str, Any]:
    return _base._get("/bhm/task-context", {"project": project})


@mcp.tool(name="bhm_risk_register_update", description="Create or update the canonical first-class BHM risk register for a project.")
def bhm_risk_register_update(
    project: str,
    title: str = "risk-register",
    summary: str = "",
    top_risks_csv: str | None = None,
    mitigations_csv: str | None = None,
    owner: str = "",
    upsert_key: str | None = None,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/risk-register",
        {
            "project": project,
            "title": title,
            "summary": summary,
            "top_risks": _parse_csv(top_risks_csv),
            "mitigations": _parse_csv(mitigations_csv),
            "owner": owner,
            "upsert_key": upsert_key,
        },
    )


@mcp.tool(name="bhm_risk_register_get", description="Get the canonical first-class BHM risk register for a project.")
def bhm_risk_register_get(project: str) -> dict[str, Any]:
    return _base._get("/bhm/risk-register", {"project": project})


@mcp.tool(name="bhm_validation_snapshot_save", description="Create or update the latest first-class BHM validation snapshot for a project.")
def bhm_validation_snapshot_save(
    project: str,
    title: str = "validation-snapshot",
    lint: str = "",
    tests: str = "",
    smoke: str = "",
    docs: str = "",
    overall_status: str = "",
    command_summary: str = "",
    upsert_key: str | None = None,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/validation-snapshot",
        {
            "project": project,
            "title": title,
            "lint": lint,
            "tests": tests,
            "smoke": smoke,
            "docs": docs,
            "overall_status": overall_status,
            "command_summary": command_summary,
            "upsert_key": upsert_key,
        },
    )


@mcp.tool(name="bhm_validation_snapshot_get", description="Get the latest first-class BHM validation snapshot for a project.")
def bhm_validation_snapshot_get(project: str) -> dict[str, Any]:
    return _base._get("/bhm/validation-snapshot", {"project": project})


@mcp.tool(name="bhm_source_refs_attach", description="Attach canonical source references to a live BHM memory.")
def bhm_source_refs_attach(id: str, refs_csv: str, project: str | None = None) -> dict[str, Any]:
    return _base._post("/bhm/memory/source-refs", {"id": id, "project": project, "refs": _parse_csv(refs_csv) or []})


@mcp.tool(name="bhm_source_refs_get", description="Get canonical source references for a live BHM memory.")
def bhm_source_refs_get(id: str, project: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"id": id}
    if project:
        params["project"] = project
    return _base._get("/bhm/memory/source-refs", params)


@mcp.tool(name="bhm_memory_timeline", description="Get a chronological live BHM memory timeline with optional project, concept, or type filters.")
def bhm_memory_timeline(
    project: str | None = None,
    concept: str | None = None,
    memory_type: str | None = None,
    memory_class: MemoryClass | None = None,
    event_role: MemoryEventRole | None = None,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    include_archived: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    return _base._post(
        "/bhm/memory/timeline",
        {
            "project": project,
            "concept": concept,
            "memory_type": memory_type,
            "memory_class": memory_class.value if memory_class else None,
            "event_role": event_role.value if event_role else None,
            "as_of": as_of,
            "valid_from": valid_from,
            "valid_to": valid_to,
            **({"include_temporal_unknown": True} if include_temporal_unknown else {}),
            "include_archived": include_archived,
            "limit": limit,
        },
    )
