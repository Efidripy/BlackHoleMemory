from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated
from typing import Any
from typing import Literal

import httpx
from fastmcp import FastMCP
from pydantic import BaseModel
from pydantic import Field

from .config import settings
from . import memory_contracts as _memory_contracts
from .context_compiler import MAX_CONTEXT_TOKEN_BUDGET
from .local_endpoint_policy import MAX_RESPONSE_BYTES
from .local_endpoint_policy import validate_local_endpoint
from .resource_limits import BHM_INTERNAL_HTTP_TIMEOUT_SECONDS
from .resource_limits import BHM_CODE_GRAPH_HTTP_TIMEOUT_SECONDS
from .resource_limits import BHM_CODE_INDEX_HTTP_TIMEOUT_SECONDS
from .resource_limits import BHM_CODE_STATUS_HTTP_TIMEOUT_SECONDS
from .resource_limits import BHM_INDEX_MAX_FILES_PER_RUN

MemoryMetadata = _memory_contracts.MemoryMetadata
MemoryClass = _memory_contracts.MemoryClass
MemoryEventRole = _memory_contracts.MemoryEventRole
MetadataActionability = _memory_contracts.MetadataActionability
MetadataDomain = _memory_contracts.MetadataDomain
MetadataLanguage = _memory_contracts.MetadataLanguage
MetadataLifecycle = _memory_contracts.MetadataLifecycle
MetadataPriority = _memory_contracts.MetadataPriority
MetadataProvenance = _memory_contracts.MetadataProvenance
MetadataRetention = _memory_contracts.MetadataRetention
MetadataScope = _memory_contracts.MetadataScope
MetadataSemanticType = _memory_contracts.MetadataSemanticType
MetadataSensitivity = _memory_contracts.MetadataSensitivity
MetadataStakeholder = _memory_contracts.MetadataStakeholder
MetadataVerification = _memory_contracts.MetadataVerification

DEFAULT_PROJECT = "e-github-workspace"
DEFAULT_BASE_URL = os.getenv("BHM_MCP_BASE_URL", f"http://{settings.host}:{settings.port}")
TAXONOMY_METADATA_HINT = (
    "Typed metadata taxonomy values: lifecycle=draft|validated|deprecated|archived; "
    "provenance=github|mcp|llm|human|synthetic; priority=critical|high|medium|low|normal|trivial; "
    "domain=frontend|backend|infra|security|product|general; sensitivity=public|internal|restricted; "
    "scope=global|service|feature|local; retention=transient|short-term|long-term|permanent; "
    "verification=unverified|peer-reviewed|trusted; actionability=task|info|decision|query; "
    "stakeholder=core-team|devops|frontend-squad|product-owner; language=en|ru|code-python|code-ts; "
    "semantic_type=architecture|bugfix|feature|refactor|knowledge|fact|log|error|decision-log|requirement; "
    "memory_class=episodic|semantic|procedural|working|unclassified; "
    "event_role=fact|decision|qa|trace|feedback|skill_run|unclassified; "
    "version=string; importance_score=1..10."
)

mcp = FastMCP(
    "bhm",
    instructions=(
        "BHM is the primary workspace memory system. "
        "Use these tools for memory search, remember, preflight, slots, lessons, "
        "and diagnostics against the local BlackHoleMemory runtime. "
        "Prefer stable public tools for normal agent work. "
        "Treat deprecated compatibility candidates as transitional helpers and prefer their newer replacements."
    ),
)


class BhmBatchUpsertItem(BaseModel):
    upsert_key: str
    content: str
    project: str | None = None
    memory_type: str = "workflow"
    memory_class: MemoryClass | None = None
    event_role: MemoryEventRole | None = None
    observed_at: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    open_interval: bool | None = None
    supersedes_revision_id: str | None = None
    source_episode_id: str | None = None
    source_uri: str | None = None
    source_digest: str | None = None
    concepts: list[str] | None = None
    files: list[str] | None = None
    metadata: MemoryMetadata | None = None


class BhmBatchLinkItem(BaseModel):
    source_id: str
    target_id: str
    relation: str
    project: str
    metadata: MemoryMetadata | None = None
    ontology_schema_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None


def _read_process_or_user_env_value(key: str) -> str | None:
    direct = str(os.getenv(key) or "").strip()
    if direct:
        return direct
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            value, _ = winreg.QueryValueEx(handle, key)
    except (ImportError, FileNotFoundError, OSError):
        return None
    return str(value or "").strip() or None


def _client(*, timeout: float | None = None) -> httpx.Client:
    # The MCP compatibility bridge is local-only. Validate the configured
    # origin before constructing an authenticated client so a malformed or
    # remote BHM_MCP_BASE_URL can never receive the caller bearer token.
    base_url = validate_local_endpoint(DEFAULT_BASE_URL)
    caller_token = _read_process_or_user_env_value("BHM_CALLER_TOKEN")
    if not caller_token:
        raise RuntimeError("BHM caller credential is unavailable; initialize BHM_CALLER_TOKEN before using MCP")
    headers = {
        "Authorization": f"Bearer {caller_token}",
        "X-BHM-Caller-Surface": "mcp",
    }
    # Admin-surface tools remain capability-gated. Supplying the local
    # operator capability here lets an explicitly configured MCP client call
    # those tools without making the routine core catalog privileged.
    admin_capability = _read_process_or_user_env_value("BHM_ADMIN_CAPABILITY") or _read_process_or_user_env_value(
        "BHM_MCP_ADMIN_CAPABILITY"
    )
    if admin_capability:
        headers["x-bhm-admin-capability"] = admin_capability
    return httpx.Client(
        base_url=base_url,
        timeout=float(BHM_INTERNAL_HTTP_TIMEOUT_SECONDS if timeout is None else timeout),
        headers=headers,
        follow_redirects=False,
        trust_env=False,
    )


def _bounded_json_response(response: httpx.Response) -> Any:
    """Reject oversized MCP→REST responses before JSON parsing."""

    content_length = response.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise ValueError("MCP REST response has invalid content-length") from exc
        if declared_length < 0 or declared_length > MAX_RESPONSE_BYTES:
            raise ValueError("MCP REST response exceeded bounded limit")
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ValueError("MCP REST response exceeded bounded limit")
    return response.json()


def _read_native_env_value(key: str) -> str | None:
    direct = _read_process_or_user_env_value(key)
    if direct:
        return direct
    env_path = Path.home() / ".bhm" / ".env"
    if not env_path.exists():
        return None
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            current_key, current_value = line.split("=", 1)
            if current_key.strip() == key:
                return current_value.split("#", 1)[0].strip() or None
    except OSError:
        return None
    return None


def _parse_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [item.strip() for item in value.split(",")]
    items = [item for item in items if item]
    return items or None


def _jsonable_or_text(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _json_object(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("metadata_json must be a JSON object")
    return parsed


def _metadata_payload(value: MemoryMetadata | dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, BaseModel):
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json", exclude_none=True)
        return value.dict(exclude_none=True)
    return MemoryMetadata(**value).model_dump(mode="json", exclude_none=True)


def _metadata_json_object(value: str | None) -> dict[str, Any] | None:
    return _metadata_payload(_json_object(value))


def _model_dump(value: BaseModel) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return value.dict(exclude_none=True)


def _batch_upsert_item_payload(item: BhmBatchUpsertItem) -> dict[str, Any]:
    payload = _model_dump(item)
    payload["type"] = payload.pop("memory_type", "workflow")
    return payload


def _batch_link_item_payload(item: BhmBatchLinkItem) -> dict[str, Any]:
    return _model_dump(item)


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    with _client() as client:
        response = client.get(path, params=params)
        response.raise_for_status()
        return _bounded_json_response(response)


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    timeout: float | None = None
    if path == "/bhm/code-tools":
        operation = str(body.get("operation") or "").casefold()
        if operation in {"status", "coverage", "projects"}:
            timeout = float(BHM_CODE_STATUS_HTTP_TIMEOUT_SECONDS)
        elif operation in {"index", "watch"}:
            timeout = float(
                BHM_CODE_GRAPH_HTTP_TIMEOUT_SECONDS
                if body.get("graph_only") or (body.get("build_graph") and not body.get("defer_graph"))
                else BHM_CODE_INDEX_HTTP_TIMEOUT_SECONDS
            )
    with _client(timeout=timeout) as client:
        response = client.post(path, json=body)
        response.raise_for_status()
        return _bounded_json_response(response)


def _delete(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    with _client() as client:
        response = client.delete(path, params=params)
        response.raise_for_status()
        return _bounded_json_response(response)


def _delete_json(path: str, body: dict[str, Any]) -> dict[str, Any]:
    with _client() as client:
        response = client.request("DELETE", path, json=body)
        response.raise_for_status()
        return _bounded_json_response(response)


@mcp.tool(name="bhm_health", description="Check BHM readiness, cutover state, and native BHM health.")
def bhm_health() -> dict[str, Any]:
    return {
        "base_url": validate_local_endpoint(DEFAULT_BASE_URL),
        "ready": _get("/health/ready"),
        "cutover": _get("/health/cutover"),
        "bhm": _get("/bhm/health"),
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
    return _get(
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
    return _post("/bhm/diagnostics", {})


@mcp.tool(name="bhm_preflight", description="Run the BHM memory preflight pattern for a project.")
def bhm_preflight(project: str = DEFAULT_PROJECT, query: str | None = None, limit: int = 5) -> dict[str, Any]:
    search_query = query or project
    return {
        "base_url": DEFAULT_BASE_URL,
        "health": _get("/bhm/health"),
        "diagnostics": _post("/bhm/diagnostics", {}),
        "profile": _get("/bhm/profile", {"project": project}),
        "search": _post(
            "/bhm/search",
            {"query": search_query, "project": project, "limit": limit},
        ),
    }


@mcp.tool(name="bhm_search", description="Native BHM search. Historical checkpoint/session trace records are excluded by default; request include_historical or event_role=trace to inspect them.")
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
) -> dict[str, Any]:
    body = {
        "query": query,
        "limit": limit,
        "offset": offset,
        "include_archived": include_archived,
        "include_logs": include_logs,
        "include_historical": include_historical,
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
    return _post("/bhm/search", body)


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
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "query": query,
        "include_archived": include_archived,
        "include_logs": include_logs,
        "include_historical": include_historical,
        **({"include_temporal_unknown": True} if include_temporal_unknown else {}),
        **({"tiered_context": True} if tiered_context else {}),
    }
    active_profile = profile or _read_native_env_value("BHM_CONTEXT_PROFILE")
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
    return _post("/bhm/context/compile", body)


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
        "include_historical": include_historical,
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
    return _post("/bhm/retrieval/explain", body)


@mcp.tool(name="bhm_memory_used", description="Record explicit use of retrieved BHM memories for access feedback.")
def bhm_memory_used(
    ids_csv: str,
    project: str | None = None,
    reason: Annotated[str, Field(max_length=200)] = "",
) -> dict[str, Any]:
    body: dict[str, Any] = {"ids": _parse_csv(ids_csv) or [], "reason": reason}
    if project:
        body["project"] = project
    return _post("/bhm/memory/used", body)


@mcp.tool(name="bhm_get_memory", description="Get a single live BHM memory entry by id.")
def bhm_get_memory(id: str, project: str | None = None) -> dict[str, Any]:
    params = {"id": id}
    if project:
        params["project"] = project
    return _get("/bhm/memory", params)


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
    return _get("/bhm/memories", params)


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
    return _post("/bhm/memory/update", body)


@mcp.tool(name="bhm_archive_memory", description="Archive a live BHM memory entry by id.")
def bhm_archive_memory(id: str, project: str | None = None, reason: str = "") -> dict[str, Any]:
    body: dict[str, Any] = {"id": id, "reason": reason}
    if project:
        body["project"] = project
    return _post("/bhm/memory/archive", body)


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
    return _post(
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
    return _post(
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


@mcp.tool(name="bhm_search_advanced", description="Search live BHM memories with structured filters.")
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
    limit: int = 10,
    offset: int = 0,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "query": query,
        "include_archived": include_archived,
        "include_logs": include_logs,
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
    return _post("/bhm/search/advanced", body)


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
    return _post("/bhm/recent-activity", body)


@mcp.tool(name="bhm_upsert_memory", description=f"Create or update a live BHM memory entry using an explicit upsert key. {TAXONOMY_METADATA_HINT}")
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
    return _post(
        "/bhm/memory/upsert",
        body,
    )


@mcp.tool(name="bhm_get_memory_links", description="Get explicit live BHM memory links for a memory id.")
def bhm_get_memory_links(id: str, project: str) -> dict[str, Any]:
    return _get("/bhm/memory/links", {"id": id, "project": project})


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
    return _post("/bhm/memory/link", body)


@mcp.tool(name="bhm_unlink_memories", description="Delete an explicit directed link between two live BHM memories.")
def bhm_unlink_memories(source_id: str, target_id: str, relation: str, project: str) -> dict[str, Any]:
    return _delete_json(
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
    return _post(
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
    return _post(
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
    return _get("/bhm/checkpoints", params)


@mcp.tool(name="bhm_checkpoint_get_latest", description="Get the latest first-class BHM checkpoint for a project.")
def bhm_checkpoint_get_latest(project: str, checkpoint_type: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"project": project}
    if checkpoint_type:
        params["checkpoint_type"] = checkpoint_type
    return _get("/bhm/checkpoint/latest", params)


@mcp.tool(name="bhm_project_map_get", description="Get the canonical first-class BHM project map for a project.")
def bhm_project_map_get(project: str) -> dict[str, Any]:
    return _get("/bhm/project-map", {"project": project})


@mcp.tool(name="bhm_project_resolve", description="Resolve a project id or alias to the canonical BHM project identity.")
def bhm_project_resolve(project: str = "") -> dict[str, Any]:
    return _get("/bhm/project/resolve", {"project": project})


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
        return _get("/bhm/project/retirement-preview", {"project": project})
    return _post(
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
    return _post(
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
    return _post(
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
    return _post(
        "/bhm/memory/detect-duplicates",
        {
            "project": project,
            "limit": limit,
            "include_archived": include_archived,
        },
    )


@mcp.tool(name="bhm_detect_conflicts", description="Detect likely conflicting live BHM memories using deterministic heuristics.")
def bhm_detect_conflicts(project: str | None = None, limit: int = 20, include_archived: bool = False) -> dict[str, Any]:
    return _post(
        "/bhm/memory/detect-conflicts",
        {
            "project": project,
            "limit": limit,
            "include_archived": include_archived,
        },
    )


@mcp.tool(name="bhm_memory_lint", description="Lint a live BHM memory entry for compactness, scope, and likely secret leakage.")
def bhm_memory_lint(id: str, project: str | None = None) -> dict[str, Any]:
    return _post(
        "/bhm/memory/lint",
        {
            "id": id,
            "project": project,
        },
    )


@mcp.tool(name="bhm_delete_memory", description="Delete a live BHM memory entry by id from the canonical live store.")
def bhm_delete_memory(id: str, project: str | None = None) -> dict[str, Any]:
    return _delete_json("/bhm/memory", {"id": id, "project": project})


@mcp.tool(name="bhm_get_memories_by_concept", description="List live BHM memories that contain a required concept/tag.")
def bhm_get_memories_by_concept(concept: str, project: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    params: dict[str, Any] = {"concept": concept, "limit": limit, "offset": offset}
    if project:
        params["project"] = project
    return _get("/bhm/memories/by-concept", params)


@mcp.tool(name="bhm_get_memories_by_type", description="List live BHM memories by memory type.")
def bhm_get_memories_by_type(memory_type: str, project: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    params: dict[str, Any] = {"memory_type": memory_type, "limit": limit, "offset": offset}
    if project:
        params["project"] = project
    return _get("/bhm/memories/by-type", params)


@mcp.tool(name="bhm_set_memory_confidence", description="Set an explicit confidence score on a live BHM memory.")
def bhm_set_memory_confidence(id: str, confidence: float, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/memory/confidence", {"id": id, "project": project, "confidence": confidence})


@mcp.tool(name="bhm_pin_memory", description="Pin a live BHM memory for quick project recall.")
def bhm_pin_memory(id: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/memory/pin", {"id": id, "project": project, "pinned": True})


@mcp.tool(name="bhm_vote_memory_quality", description="Add a lightweight 1-5 quality vote to a live BHM memory.")
def bhm_vote_memory_quality(id: str, vote: int, project: str | None = None, voter: str = "agent") -> dict[str, Any]:
    return _post("/bhm/memory/vote-quality", {"id": id, "project": project, "vote": vote, "voter": voter})


@mcp.tool(name="bhm_unpin_memory", description="Remove the pinned flag from a live BHM memory.")
def bhm_unpin_memory(id: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/memory/pin", {"id": id, "project": project, "pinned": False})


@mcp.tool(name="bhm_list_pinned_memories", description="List pinned live BHM memories for a project.")
def bhm_list_pinned_memories(project: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if project:
        params["project"] = project
    return _get("/bhm/memories/pinned", params)


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
    return _post(
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
    return _get("/bhm/adrs", params)


@mcp.tool(name="bhm_adr_supersede", description="Mark one ADR as superseded by another and create a supersedes relation.")
def bhm_adr_supersede(project: str, old_id: str, new_id: str) -> dict[str, Any]:
    return _post("/bhm/adr/supersede", {"project": project, "old_id": old_id, "new_id": new_id})


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
    return _post(
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
    return _get("/bhm/handoffs", params)


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
    return _post(
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
    return _get("/bhm/session-records", params)


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
    return _post(
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
    return _post(
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
    return _get("/bhm/task", params)


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
    return _get("/bhm/tasks", params)


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
    return _post(
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
    return _get("/bhm/task-context", {"project": project})


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
    return _post(
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
    return _get("/bhm/risk-register", {"project": project})


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
    return _post(
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
    return _get("/bhm/validation-snapshot", {"project": project})


@mcp.tool(name="bhm_source_refs_attach", description="Attach canonical source references to a live BHM memory.")
def bhm_source_refs_attach(id: str, refs_csv: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/memory/source-refs", {"id": id, "project": project, "refs": _parse_csv(refs_csv) or []})


@mcp.tool(name="bhm_source_refs_get", description="Get canonical source references for a live BHM memory.")
def bhm_source_refs_get(id: str, project: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"id": id}
    if project:
        params["project"] = project
    return _get("/bhm/memory/source-refs", params)


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
    return _post(
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


@mcp.tool(name="bhm_query_suggestions", description="Suggest useful memory queries for a project based on current live-memory contents.")
def bhm_query_suggestions(project: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if project:
        params["project"] = project
    return _get("/bhm/query-suggestions", params)


@mcp.tool(name="bhm_delete_memory_hard", description="Hard-delete a live BHM memory and remove dependent canonical artifact references.")
def bhm_delete_memory_hard(id: str, project: str | None = None) -> dict[str, Any]:
    return _delete_json("/bhm/memory/hard", {"id": id, "project": project})


@mcp.tool(name="bhm_source_refs_detach", description="Detach selected canonical source references from a live BHM memory.")
def bhm_source_refs_detach(id: str, refs_csv: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/memory/source-refs/detach", {"id": id, "project": project, "refs": _parse_csv(refs_csv) or []})


@mcp.tool(name="bhm_source_refs_replace", description="Replace canonical source references on a live BHM memory.")
def bhm_source_refs_replace(id: str, refs_csv: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/memory/source-refs/replace", {"id": id, "project": project, "refs": _parse_csv(refs_csv) or []})


@mcp.tool(name="bhm_memory_restore_from_archive", description="Restore an archived live BHM memory back to active state.")
def bhm_memory_restore_from_archive(id: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/memory/restore", {"id": id, "project": project})


@mcp.tool(name="bhm_batch_upsert", description=f"Batch upsert multiple live BHM memories with typed item objects. {TAXONOMY_METADATA_HINT}")
def bhm_batch_upsert(items: list[BhmBatchUpsertItem], project: str | None = None) -> dict[str, Any]:
    return _post(
        "/bhm/memories/batch-upsert",
        {"project": project, "items": [_batch_upsert_item_payload(item) for item in items]},
    )


@mcp.tool(name="bhm_batch_link", description=f"Batch create explicit memory links with typed item objects. {TAXONOMY_METADATA_HINT}")
def bhm_batch_link(items: list[BhmBatchLinkItem], project: str | None = None) -> dict[str, Any]:
    return _post(
        "/bhm/memories/batch-link",
        {"project": project, "items": [_batch_link_item_payload(item) for item in items]},
    )


@mcp.tool(name="bhm_batch_upsert_memories", description="Compatibility JSON wrapper for bhm_batch_upsert.")
def bhm_batch_upsert_memories(items_json: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/memories/batch-upsert", {"project": project, "items": _jsonable_or_text(items_json) or []})


@mcp.tool(name="bhm_batch_attach_source_refs", description="Batch attach canonical source references to multiple live BHM memories.")
def bhm_batch_attach_source_refs(items_json: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/memory/source-refs/batch", {"project": project, "items": _jsonable_or_text(items_json) or []})


@mcp.tool(name="bhm_batch_archive_memories", description="Batch archive multiple live BHM memories.")
def bhm_batch_archive_memories(items_json: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/memories/batch-archive", {"project": project, "items": _jsonable_or_text(items_json) or []})


@mcp.tool(name="bhm_batch_delete_memories", description="Batch delete multiple live BHM memories from the live store.")
def bhm_batch_delete_memories(items_json: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/memories/batch-delete", {"project": project, "items": _jsonable_or_text(items_json) or []})


@mcp.tool(name="bhm_batch_link_memories", description="Compatibility JSON wrapper for bhm_batch_link.")
def bhm_batch_link_memories(items_json: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/memories/batch-link", {"project": project, "items": _jsonable_or_text(items_json) or []})


@mcp.tool(name="bhm_batch_unlink_memories", description="Batch remove explicit memory links.")
def bhm_batch_unlink_memories(items_json: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/memories/batch-unlink", {"project": project, "items": _jsonable_or_text(items_json) or []})


@mcp.tool(name="bhm_repair_live_indexes", description="Repair scoped canonical live-memory links and optional orphan artifacts.")
def bhm_repair_live_indexes(project: str | None = None, aggregate: bool = False, remove_orphan_links: bool = True, remove_orphan_artifacts: bool = False) -> dict[str, Any]:
    return _post("/bhm/repair-live-indexes", {"project": project, "aggregate": aggregate, "remove_orphan_links": remove_orphan_links, "remove_orphan_artifacts": remove_orphan_artifacts})


@mcp.tool(name="bhm_integrity_audit", description="Read-only integrity audit helper. Prefer bhm_integrity_repair_strict for operator repair passes and gate-level maintenance.")
def bhm_integrity_audit(project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/integrity-audit", {"project": project})


@mcp.tool(name="bhm_memory_diff", description="Diff two live BHM memories by content lines.")
def bhm_memory_diff(left_id: str, right_id: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/memory/diff", {"left_id": left_id, "right_id": right_id, "project": project})


@mcp.tool(name="bhm_rebuild_project_summary", description="Rebuild a compact project summary from project map, checkpoint, task context, risks, and validation snapshot.")
def bhm_rebuild_project_summary(project: str, upsert_key: str | None = None) -> dict[str, Any]:
    return _post("/bhm/project-summary/rebuild", {"project": project, "upsert_key": upsert_key})


@mcp.tool(name="bhm_project_summary_get", description="Get the current canonical project summary memory for a project.")
def bhm_project_summary_get(project: str) -> dict[str, Any]:
    return _get("/bhm/project-summary", {"project": project})


@mcp.tool(name="bhm_project_summary_pin", description="Pin the canonical project summary memory for a project.")
def bhm_project_summary_pin(project: str) -> dict[str, Any]:
    return _post("/bhm/project-summary/pin", {"project": project})


@mcp.tool(name="bhm_project_summary_list", description="List canonical project summary memories.")
def bhm_project_summary_list(project: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    return _post("/bhm/project-summary/list", {"project": project, "limit": limit, "offset": offset})


@mcp.tool(name="bhm_artifact_integrity_audit", description="Audit artifact stores for orphaned memory references.")
def bhm_artifact_integrity_audit(project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/artifact-integrity-audit", {"project": project})


@mcp.tool(name="bhm_entity_extract", description="Extract simple entities such as files, endpoints, env vars, and concepts from a live BHM memory.")
def bhm_entity_extract(id: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/entity-extract", {"id": id, "project": project})


@mcp.tool(name="bhm_relation_suggest", description="Read-only relation suggestion helper. Prefer bhm_relation_apply_suggestions when operators want deterministic apply behavior.")
def bhm_relation_suggest(project: str | None = None, limit: int = 20) -> dict[str, Any]:
    return _post("/bhm/relation-suggest", {"project": project, "limit": limit})


@mcp.tool(name="bhm_memory_compact", description="Replace a live BHM memory body with a compact summary while retaining provenance.")
def bhm_memory_compact(id: str, summary: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/memory/compact", {"id": id, "project": project, "summary": summary})


@mcp.tool(name="bhm_link_graph_stats", description="Get lightweight stats for the explicit memory-link graph.")
def bhm_link_graph_stats(project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/link-graph-stats", {"project": project})


@mcp.tool(name="bhm_reindex_memory_metadata", description="Rebuild derived metadata such as raw titles for live memories.")
def bhm_reindex_memory_metadata(project: str | None = None, aggregate: bool = False) -> dict[str, Any]:
    return _post("/bhm/reindex-memory-metadata", {"project": project, "aggregate": aggregate})


@mcp.tool(name="bhm_memory_schema_validate", description="Lightweight single-record schema validation helper. Prefer bhm_schema_validate_strict for operator gates and broader audits.")
def bhm_memory_schema_validate(id: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/memory/schema-validate", {"id": id, "project": project})


@mcp.tool(name="bhm_memory_type_migrate", description="Migrate a live BHM memory to a new memory type.")
def bhm_memory_type_migrate(id: str, new_type: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/memory/type-migrate", {"id": id, "project": project, "new_type": new_type})


@mcp.tool(name="bhm_search_hybrid", description="Run hybrid search that combines structured advanced search with query suggestions.")
def bhm_search_hybrid(
    query: str,
    project: str | None = None,
    memory_class: MemoryClass | None = None,
    event_role: MemoryEventRole | None = None,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    include_archived: bool = False,
    include_logs: bool = False,
    limit: int = 10,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "query": query,
        "include_archived": include_archived,
        "include_logs": include_logs,
        "limit": limit,
        **({"include_temporal_unknown": True} if include_temporal_unknown else {}),
    }
    if project:
        body["project"] = project
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
    if domain:
        body["domain"] = domain
    if semantic_type:
        body["semantic_type"] = semantic_type
    if priority:
        body["priority"] = priority
    return _post("/bhm/search/hybrid", body)


@mcp.tool(name="bhm_search_by_source_ref", description="Search live BHM memories by canonical source reference or file.")
def bhm_search_by_source_ref(ref: str, project: str | None = None, limit: int = 20) -> dict[str, Any]:
    return _post("/bhm/search/by-source-ref", {"ref": ref, "project": project, "limit": limit})


@mcp.tool(name="bhm_search_by_upsert_key", description="Search live BHM memories by exact upsert key.")
def bhm_search_by_upsert_key(upsert_key: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/search/by-upsert-key", {"upsert_key": upsert_key, "project": project})


@mcp.tool(name="bhm_list_archived_memories", description="List archived live BHM memories for a project.")
def bhm_list_archived_memories(project: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if project:
        params["project"] = project
    return _get("/bhm/memories/archived", params)


@mcp.tool(name="bhm_memory_restore_batch", description="Restore multiple archived live BHM memories in one call.")
def bhm_memory_restore_batch(items_json: str) -> dict[str, Any]:
    return _post("/bhm/memory/restore-batch", {"items": _jsonable_or_text(items_json) or []})


@mcp.tool(name="bhm_artifact_restore", description="Restore a first-class artifact by reviving or recreating its backing live memory.")
def bhm_artifact_restore(artifact_type: str, artifact_id: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/artifact/restore", {"artifact_type": artifact_type, "artifact_id": artifact_id, "project": project})


@mcp.tool(name="bhm_orphan_artifact_relink", description="Relink an orphan first-class artifact to a target live memory id.")
def bhm_orphan_artifact_relink(artifact_type: str, artifact_id: str, target_memory_id: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/artifact/relink", {"artifact_type": artifact_type, "artifact_id": artifact_id, "target_memory_id": target_memory_id, "project": project})


@mcp.tool(name="bhm_memory_staleness_report", description="Report stale live memories older than a chosen age threshold.")
def bhm_memory_staleness_report(project: str | None = None, days: int = 30, limit: int = 20) -> dict[str, Any]:
    return _post("/bhm/memory/staleness-report", {"project": project, "days": days, "limit": limit})


@mcp.tool(name="bhm_memory_review_queue", description="Build a deterministic review queue for low-quality memories and contradictions.")
def bhm_memory_review_queue(project: str | None = None, limit: int = 20, include_conflicts: bool = True, include_closed: bool = False) -> dict[str, Any]:
    return _post(
        "/bhm/memory/review-queue",
        {"project": project, "limit": limit, "include_conflicts": include_conflicts, "include_closed": include_closed},
    )


@mcp.tool(name="bhm_memory_triage_queue", description="Build a deterministic triage queue of duplicates, contradictions, and relation suggestions.")
def bhm_memory_triage_queue(project: str | None = None, limit: int = 20, include_closed: bool = False) -> dict[str, Any]:
    return _post("/bhm/memory/triage-queue", {"project": project, "limit": limit, "include_closed": include_closed})


@mcp.tool(name="bhm_project_summary_refresh_all", description="Bulk operator helper for refreshing many project summaries. Prefer bhm_rebuild_project_summary for normal single-project flows.")
def bhm_project_summary_refresh_all(projects_csv: str | None = None, project: str | None = None, aggregate: bool = False) -> dict[str, Any]:
    return _post("/bhm/project-summary/refresh-all", {"projects": _parse_csv(projects_csv), "project": project, "aggregate": aggregate})


@mcp.tool(name="bhm_relation_apply_suggestions", description="Apply suggested relations above a confidence threshold.")
def bhm_relation_apply_suggestions(project: str | None = None, aggregate: bool = False, min_score: float = 0.65, limit: int = 20, include_relates_to: bool = False) -> dict[str, Any]:
    return _post("/bhm/relation/apply-suggestions", {"project": project, "aggregate": aggregate, "min_score": min_score, "limit": limit, "include_relates_to": include_relates_to})


@mcp.tool(name="bhm_memory_merge_preview", description="Preview a merge of two live memories without modifying either record.")
def bhm_memory_merge_preview(project: str, source_id: str, target_id: str) -> dict[str, Any]:
    return _post("/bhm/memory/merge-preview", {"project": project, "source_id": source_id, "target_id": target_id})


@mcp.tool(name="bhm_ontology_quarantine_list", description="List bounded content-free ontology relation worklist items for one project. It never admits or modifies a link.")
def bhm_ontology_quarantine_list(project: str, limit: int = 100) -> dict[str, Any]:
    return _get("/bhm/ontology/quarantine", {"project": project, "limit": limit})


@mcp.tool(name="bhm_schema_upgrade_all", description="Apply a lightweight schema upgrade pass to all live memories.")
def bhm_schema_upgrade_all(project: str | None = None, aggregate: bool = False) -> dict[str, Any]:
    return _post("/bhm/schema/upgrade-all", {"project": project, "aggregate": aggregate})


@mcp.tool(name="bhm_memory_redact", description="Redact secret-like substrings from a live memory body.")
def bhm_memory_redact(
    id: str,
    project: str | None = None,
    patterns_csv: str | None = None,
    replacement: Annotated[str, Field(max_length=120)] = "[REDACTED]",
) -> dict[str, Any]:
    return _post("/bhm/memory/redact", {"id": id, "project": project, "patterns": _parse_csv(patterns_csv), "replacement": replacement})


@mcp.tool(name="bhm_secret_scan_existing_memories", description="Scan existing live memories for secret-like content.")
def bhm_secret_scan_existing_memories(project: str | None = None, limit: int = 50) -> dict[str, Any]:
    return _post("/bhm/memory/secret-scan", {"project": project, "limit": limit})


@mcp.tool(name="bhm_agent_activity_rollup", description="Get compact activity rollups across checkpoints, handoffs, sessions, and observations.")
def bhm_agent_activity_rollup(project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/agent-activity-rollup", {"project": project})


@mcp.tool(name="bhm_project_memory_heatmap", description="Get type, tag, and age-bucket distributions for project live memories.")
def bhm_project_memory_heatmap(project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/project-memory-heatmap", {"project": project})


@mcp.tool(name="bhm_relation_confidence_set", description="Set an explicit confidence score on a memory relation link.")
def bhm_relation_confidence_set(source_id: str, target_id: str, relation: str, project: str, confidence: float) -> dict[str, Any]:
    return _post("/bhm/relation/confidence", {"source_id": source_id, "target_id": target_id, "relation": relation, "project": project, "confidence": confidence})


@mcp.tool(name="bhm_relation_vote_quality", description="Vote on the quality of a memory relation link.")
def bhm_relation_vote_quality(source_id: str, target_id: str, relation: str, project: str, vote: int, voter: str = "agent") -> dict[str, Any]:
    return _post("/bhm/relation/vote-quality", {"source_id": source_id, "target_id": target_id, "relation": relation, "project": project, "vote": vote, "voter": voter})


@mcp.tool(name="bhm_memory_alias_add", description="Add an alias to a live memory for future resolution.")
def bhm_memory_alias_add(id: str, alias: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/memory/alias/add", {"id": id, "alias": alias, "project": project})


@mcp.tool(name="bhm_memory_alias_remove", description="Remove an alias from a live memory.")
def bhm_memory_alias_remove(id: str, alias: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/memory/alias/remove", {"id": id, "alias": alias, "project": project})


@mcp.tool(name="bhm_alias_resolve", description="Resolve an alias to matching live memories.")
def bhm_alias_resolve(alias: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/memory/alias/resolve", {"alias": alias, "project": project})


@mcp.tool(name="bhm_entity_catalog_get", description="Get the current derived entity catalog for a project.")
def bhm_entity_catalog_get(project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/entity-catalog/get", {"project": project})


@mcp.tool(name="bhm_entity_catalog_rebuild", description="Rebuild the derived entity catalog for a project.")
def bhm_entity_catalog_rebuild(project: str | None = None, aggregate: bool = False) -> dict[str, Any]:
    return _post("/bhm/entity-catalog/rebuild", {"project": project, "aggregate": aggregate})


@mcp.tool(name="bhm_project_summary_compare", description="Compare canonical project summaries between two projects.")
def bhm_project_summary_compare(left_project: str, right_project: str) -> dict[str, Any]:
    return _post("/bhm/project-summary/compare", {"left_project": left_project, "right_project": right_project})


@mcp.tool(name="bhm_memory_usage_stats", description="Get compact memory usage stats for a project.")
def bhm_memory_usage_stats(project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/memory/usage-stats", {"project": project})


@mcp.tool(name="bhm_recent_failures_feed", description="Get a compact feed of recent failure-like validation or handoff artifacts.")
def bhm_recent_failures_feed(project: str | None = None, limit: int = 20) -> dict[str, Any]:
    return _post("/bhm/recent-failures-feed", {"project": project, "limit": limit})


@mcp.tool(name="bhm_memory_restore_hard_deleted_preview", description="Preview whether a live memory could be reconstructed after a hard delete.")
def bhm_memory_restore_hard_deleted_preview(id: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/memory/restore-hard-deleted-preview", {"id": id, "project": project})


@mcp.tool(name="bhm_artifact_delete", description="Delete a first-class artifact, optionally deleting its backing memory too.")
def bhm_artifact_delete(artifact_type: str, artifact_id: str, project: str | None = None, delete_backing_memory: bool = False) -> dict[str, Any]:
    return _post("/bhm/artifact/delete", {"artifact_type": artifact_type, "artifact_id": artifact_id, "project": project, "delete_backing_memory": delete_backing_memory})


@mcp.tool(name="bhm_artifact_list_by_type", description="List first-class artifacts by canonical artifact type.")
def bhm_artifact_list_by_type(artifact_type: str, project: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    return _post("/bhm/artifact/list-by-type", {"artifact_type": artifact_type, "project": project, "limit": limit, "offset": offset})


@mcp.tool(name="bhm_artifact_usage_stats", description="Get usage stats across canonical artifact stores.")
def bhm_artifact_usage_stats(project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/artifact/usage-stats", {"project": project})


@mcp.tool(name="bhm_memory_gc_candidates", description="Report live memories that look like good garbage-collection candidates.")
def bhm_memory_gc_candidates(project: str | None = None, stale_days: int = 90, limit: int = 20) -> dict[str, Any]:
    return _post("/bhm/memory/gc-candidates", {"project": project, "stale_days": stale_days, "limit": limit})


@mcp.tool(name="bhm_memory_compaction_report", description="Report oversized or log-shaped memories that should be compacted.")
def bhm_memory_compaction_report(project: str | None = None, min_chars: int = 1200, min_lines: int = 25, limit: int = 20) -> dict[str, Any]:
    return _post("/bhm/memory/compaction-report", {"project": project, "min_chars": min_chars, "min_lines": min_lines, "limit": limit})


@mcp.tool(name="bhm_link_cycle_detect", description="Detect cycles in the explicit memory-link graph.")
def bhm_link_cycle_detect(project: str | None = None, limit: int = 20) -> dict[str, Any]:
    return _post("/bhm/link/cycle-detect", {"project": project, "limit": limit})


@mcp.tool(name="bhm_link_orphan_scan", description="Scan the explicit memory-link graph for orphan links.")
def bhm_link_orphan_scan(project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/link/orphan-scan", {"project": project})


@mcp.tool(name="bhm_project_map_compare", description="Compare canonical project maps between two projects.")
def bhm_project_map_compare(left_project: str, right_project: str) -> dict[str, Any]:
    return _post("/bhm/project-map/compare", {"left_project": left_project, "right_project": right_project})


@mcp.tool(name="bhm_validation_trend_report", description="Get a compact trend report across validation snapshots for a project.")
def bhm_validation_trend_report(project: str, limit: int = 20) -> dict[str, Any]:
    return _post("/bhm/validation/trend-report", {"project": project, "limit": limit})


@mcp.tool(name="bhm_entity_search", description="Search the derived entity catalog for a project.")
def bhm_entity_search(query: str, project: str | None = None, limit: int = 20) -> dict[str, Any]:
    return _post("/bhm/entity/search", {"query": query, "project": project, "limit": limit})


@mcp.tool(name="bhm_entity_link_memories", description="Create explicit links between memories that share a chosen entity.")
def bhm_entity_link_memories(entity: str, project: str, relation: str = "relates_to", limit: int = 20) -> dict[str, Any]:
    return _post("/bhm/entity/link-memories", {"entity": entity, "project": project, "relation": relation, "limit": limit})


@mcp.tool(name="bhm_alias_stats", description="Get alias usage and duplication stats.")
def bhm_alias_stats(project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/alias/stats", {"project": project})


@mcp.tool(name="bhm_relation_prune_low_quality", description="Prune low-confidence or low-quality explicit relations.")
def bhm_relation_prune_low_quality(project: str | None = None, aggregate: bool = False, max_confidence: float = 0.5, max_quality_score: float = 2.5, remove_unscored: bool = False) -> dict[str, Any]:
    return _post("/bhm/relation/prune-low-quality", {"project": project, "aggregate": aggregate, "max_confidence": max_confidence, "max_quality_score": max_quality_score, "remove_unscored": remove_unscored})


@mcp.tool(name="bhm_project_similarity_report", description="Find projects with the most overlap in concepts and files.")
def bhm_project_similarity_report(project: str, limit: int = 10) -> dict[str, Any]:
    return _post("/bhm/project-similarity-report", {"project": project, "limit": limit})


@mcp.tool(name="bhm_memory_changelog", description="Read the explicit and inferred changelog for a live memory.")
def bhm_memory_changelog(id: str, project: str | None = None, limit: int = 50) -> dict[str, Any]:
    return _post("/bhm/memory/changelog", {"id": id, "project": project, "limit": limit})


@mcp.tool(name="bhm_review_queue_apply", description="Apply deterministic review actions to selected quality or contradiction queue items.")
def bhm_review_queue_apply(
    project: str | None = None,
    limit: int = 20,
    mark_needs_review: bool = True,
    auto_redact_secrets: bool = True,
    queue_ids_csv: str | None = None,
    status: Literal["needs_review", "resolved", "dismissed"] = "needs_review",
) -> dict[str, Any]:
    return _post(
        "/bhm/review-queue/apply",
        {
            "project": project,
            "limit": limit,
            "mark_needs_review": mark_needs_review,
            "auto_redact_secrets": auto_redact_secrets,
            "queue_ids": _parse_csv(queue_ids_csv) or [],
            "status": status,
        },
    )


@mcp.tool(name="bhm_triage_queue_apply", description="Apply deterministic triage actions from the current triage queue.")
def bhm_triage_queue_apply(project: str | None = None, limit: int = 20, min_score: float = 0.75, include_relates_to: bool = False) -> dict[str, Any]:
    return _post("/bhm/triage-queue/apply", {"project": project, "limit": limit, "min_score": min_score, "include_relates_to": include_relates_to})


@mcp.tool(name="bhm_artifact_batch_delete", description="Delete multiple first-class artifacts in one call.")
def bhm_artifact_batch_delete(artifact_type: str, artifact_ids_csv: str, project: str | None = None, delete_backing_memory: bool = False) -> dict[str, Any]:
    return _post("/bhm/artifact/batch-delete", {"artifact_type": artifact_type, "artifact_ids": _parse_csv(artifact_ids_csv) or [], "project": project, "delete_backing_memory": delete_backing_memory})


@mcp.tool(name="bhm_artifact_batch_relink", description="Relink multiple first-class artifacts to target memories in one call.")
def bhm_artifact_batch_relink(artifact_type: str, items_json: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/artifact/batch-relink", {"artifact_type": artifact_type, "items": _jsonable_or_text(items_json) or [], "project": project})


@mcp.tool(name="bhm_artifact_batch_restore", description="Restore multiple first-class artifacts in one call.")
def bhm_artifact_batch_restore(artifact_type: str, artifact_ids_csv: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/artifact/batch-restore", {"artifact_type": artifact_type, "artifact_ids": _parse_csv(artifact_ids_csv) or [], "project": project})


@mcp.tool(name="bhm_schema_validate_strict", description="Run strict schema validation across live memories and artifact references.")
def bhm_schema_validate_strict(project: str | None = None, include_archived: bool = True) -> dict[str, Any]:
    return _post("/bhm/schema/validate-strict", {"project": project, "include_archived": include_archived})


@mcp.tool(name="bhm_integrity_repair_strict", description="Run a stricter integrity repair pass with orphan cleanup and metadata normalization.")
def bhm_integrity_repair_strict(project: str | None = None, remove_orphan_links: bool = True, remove_orphan_artifacts: bool = True, normalize_metadata: bool = True) -> dict[str, Any]:
    return _post("/bhm/integrity/repair-strict", {"project": project, "remove_orphan_links": remove_orphan_links, "remove_orphan_artifacts": remove_orphan_artifacts, "normalize_metadata": normalize_metadata})


@mcp.tool(name="bhm_memory_normalize_metadata", description="Normalize live-memory metadata fields such as raw_title, files, source_refs, aliases, and changelog.")
def bhm_memory_normalize_metadata(project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/memory/normalize-metadata", {"project": project})


@mcp.tool(name="bhm_admin_export", description="Export canonical live-memory, links, and artifacts to a JSON admin snapshot.")
def bhm_admin_export(project: str | None = None, include_archived: bool = True, include_artifacts: bool = True, export_name: str | None = None) -> dict[str, Any]:
    return _post("/bhm/admin/export", {"project": project, "include_archived": include_archived, "include_artifacts": include_artifacts, "export_name": export_name})


@mcp.tool(name="bhm_admin_import_preview", description="Preview a JSON admin snapshot before import.")
def bhm_admin_import_preview(path: str, project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/admin/import-preview", {"path": path, "project": project})


@mcp.tool(name="bhm_admin_import_apply", description="Apply a JSON admin snapshot import in upsert or replace mode.")
def bhm_admin_import_apply(path: str, merge_mode: str = "upsert", project: str | None = None) -> dict[str, Any]:
    return _post("/bhm/admin/import-apply", {"path": path, "merge_mode": merge_mode, "project": project})


@mcp.tool(name="bhm_policy_profile_get", description="Get the current canonical BHM policy profile.")
def bhm_policy_profile_get() -> dict[str, Any]:
    return _get("/bhm/policy/profile")


@mcp.tool(name="bhm_policy_profile_set", description="Set the canonical BHM policy profile.")
def bhm_policy_profile_set(max_content_chars: int = 8000, max_lines: int = 120, require_project: bool = True, require_memory_type: bool = False, block_secret_like: bool = True, block_raw_logs: bool = False) -> dict[str, Any]:
    return _post("/bhm/policy/profile", {"max_content_chars": max_content_chars, "max_lines": max_lines, "require_project": require_project, "require_memory_type": require_memory_type, "block_secret_like": block_secret_like, "block_raw_logs": block_raw_logs})


@mcp.tool(name="bhm_policy_enforce_memory", description="Enforce the current policy profile against a specific live memory.")
def bhm_policy_enforce_memory(id: str, project: str | None = None, auto_redact: bool = False) -> dict[str, Any]:
    return _post("/bhm/policy/enforce-memory", {"id": id, "project": project, "auto_redact": auto_redact})


@mcp.tool(name="bhm_overlap_report", description="Report likely overlapping or redundant live memories.")
def bhm_overlap_report(project: str | None = None, limit: int = 20) -> dict[str, Any]:
    return _post("/bhm/overlap/report", {"project": project, "limit": limit})


@mcp.tool(name="bhm_overlap_cleanup_apply", description="Apply deterministic overlap cleanup by merging duplicate candidates.")
def bhm_overlap_cleanup_apply(project: str | None = None, aggregate: bool = False, limit: int = 20, archive_sources: bool = True) -> dict[str, Any]:
    return _post("/bhm/overlap/cleanup-apply", {"project": project, "aggregate": aggregate, "limit": limit, "archive_sources": archive_sources})


@mcp.tool(name="bhm_policy_guard", description="Guard a candidate memory payload for secrets, raw logs, oversize content, and missing scope.")
def bhm_policy_guard(content: str, project: str | None = None, memory_type: str | None = None) -> dict[str, Any]:
    return _post("/bhm/policy-guard", {"content": content, "project": project, "memory_type": memory_type})


@mcp.tool(name="bhm_shared_memory_policy_preflight", description="Evaluate governed shared-memory policy and append a content-free SQLite audit receipt. This tool never reads or writes shared memory.")
def bhm_shared_memory_policy_preflight(
    request_id: Annotated[str, Field(min_length=1, max_length=256)],
    operation: Literal["read", "write", "update", "transition", "delete"],
    visibility: Literal["private/agent", "session", "project", "team", "org/tenant"],
    owner_id: Annotated[str, Field(min_length=1, max_length=256)],
    at: Annotated[str, Field(min_length=20, max_length=64)],
    project: Annotated[str, Field(min_length=1, max_length=256)],
    memory_id: Annotated[str, Field(min_length=1, max_length=256)] | None = None,
    sensitivity: Literal["public", "internal", "restricted"] = "internal",
    expected_revision: Annotated[str, Field(min_length=1, max_length=256)] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "request_id": request_id,
        "operation": operation,
        "visibility": visibility,
        "owner_id": owner_id,
        "at": at,
        "project": project,
        "sensitivity": sensitivity,
    }
    if memory_id is not None:
        body["memory_id"] = memory_id
    if expected_revision is not None:
        body["expected_revision"] = expected_revision
    return _post("/bhm/shared-memory/policy/evaluate", body)


@mcp.tool(name="bhm_shared_memory_read", description="Read one active project memory only through the feature-gated governed shared-memory policy. It never writes shared memory, Qdrant, Mem0, or lifecycle state.")
def bhm_shared_memory_read(
    request_id: Annotated[str, Field(min_length=1, max_length=256)],
    visibility: Literal["private/agent", "session", "project", "team", "org/tenant"],
    owner_id: Annotated[str, Field(min_length=1, max_length=256)],
    memory_id: Annotated[str, Field(min_length=1, max_length=256)],
    at: Annotated[str, Field(min_length=20, max_length=64)],
    project: Annotated[str, Field(min_length=1, max_length=256)],
    sensitivity: Literal["public", "internal", "restricted"] = "internal",
    expected_revision: Annotated[str, Field(min_length=1, max_length=256)] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "request_id": request_id,
        "operation": "read",
        "visibility": visibility,
        "owner_id": owner_id,
        "memory_id": memory_id,
        "at": at,
        "project": project,
        "sensitivity": sensitivity,
    }
    if expected_revision is not None:
        body["expected_revision"] = expected_revision
    return _post("/bhm/shared-memory/read", body)


@mcp.tool(name="bhm_utility_feedback_record", description="Append one caller-bound, immutable utility signal for an existing project memory. This never changes lifecycle or projections.")
def bhm_utility_feedback_record(
    event_id: Annotated[str, Field(min_length=1, max_length=160)],
    memory_id: Annotated[str, Field(min_length=1, max_length=160)],
    event_type: Literal["retrieved", "used", "accepted", "dismissed", "corrected", "contradicted"],
    observed_at: Annotated[str, Field(min_length=20, max_length=64)],
    request_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    project: Annotated[str, Field(min_length=1, max_length=160)],
    confidence: Annotated[float | None, Field(ge=0.0, le=1.0)] = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "event_id": event_id,
        "memory_id": memory_id,
        "event_type": event_type,
        "observed_at": observed_at,
        "request_digest": request_digest,
        "project": project,
    }
    if confidence is not None:
        body["confidence"] = confidence
    return _post("/bhm/utility-feedback/event", body)


@mcp.tool(name="bhm_utility_feedback_report", description="Return a deterministic read-only utility report for one project; low scores never trigger lifecycle changes.")
def bhm_utility_feedback_report(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    as_of: Annotated[str, Field(min_length=20, max_length=64)],
    half_life_days: Annotated[float, Field(ge=0.25, le=3_650)] = 30.0,
    min_samples: Annotated[int, Field(ge=1, le=10_000)] = 3,
) -> dict[str, Any]:
    return _get(
        "/bhm/utility-feedback/report",
        {
            "project": project,
            "as_of": as_of,
            "half_life_days": half_life_days,
            "min_samples": min_samples,
        },
    )


@mcp.tool(name="bhm_utility_feedback_consolidation_preview", description="Return a bounded, content-free operator review worklist from immutable utility feedback. It never applies merge, archive, tombstone, or ranking changes.")
def bhm_utility_feedback_consolidation_preview(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    as_of: Annotated[str, Field(min_length=20, max_length=64)],
    half_life_days: Annotated[float, Field(ge=0.25, le=3_650)] = 30.0,
    min_samples: Annotated[int, Field(ge=1, le=10_000)] = 3,
    max_proposals: Annotated[int, Field(ge=1, le=256)] = 64,
) -> dict[str, Any]:
    return _get(
        "/bhm/utility-feedback/consolidation-preview",
        {
            "project": project,
            "as_of": as_of,
            "half_life_days": half_life_days,
            "min_samples": min_samples,
            "max_proposals": max_proposals,
        },
    )


@mcp.tool(name="bhm_consolidation_change_set_preview", description="Build a bounded, snapshot-bound, content-free consolidation change-set preview. Operator approval and typed dry-run are still required; this tool never applies changes.")
def bhm_consolidation_change_set_preview(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    as_of: Annotated[str, Field(min_length=20, max_length=64)],
    candidates: Annotated[list[dict[str, Any]], Field(min_length=1, max_length=128)],
    max_actions: Annotated[int, Field(ge=1, le=128)] = 64,
) -> dict[str, Any]:
    return _post(
        "/bhm/consolidation/change-set/preview",
        {
            "project": project,
            "as_of": as_of,
            "candidates": candidates,
            "max_actions": max_actions,
        },
    )


@mcp.tool(name="bhm_consolidation_change_set_review", description="Record an admin-gated, append-only decision over a freshly regenerated consolidation change-set. Approval never applies a lifecycle, SQLite, Qdrant, Mem0, model, or ranker change.")
def bhm_consolidation_change_set_review(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    as_of: Annotated[str, Field(min_length=20, max_length=64)],
    candidates: Annotated[list[dict[str, Any]], Field(min_length=1, max_length=128)],
    change_set: dict[str, Any],
    review_id: Annotated[str, Field(min_length=1, max_length=96)],
    decision: Literal["approved_no_apply", "rejected", "deferred"],
    action_ids: Annotated[list[str], Field(min_length=1, max_length=128)],
    reviewed_at: Annotated[str, Field(min_length=20, max_length=64)],
    rationale_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    max_actions: Annotated[int, Field(ge=1, le=128)] = 64,
) -> dict[str, Any]:
    return _post(
        "/bhm/consolidation/change-set/review",
        {
            "project": project,
            "as_of": as_of,
            "candidates": candidates,
            "change_set": change_set,
            "review_id": review_id,
            "decision": decision,
            "action_ids": action_ids,
            "reviewed_at": reviewed_at,
            "rationale_digest": rationale_digest,
            "max_actions": max_actions,
        },
    )


@mcp.tool(name="bhm_governed_consolidation_status", description="Report the disabled, proposal-only, approval-gated, policy-auto-reviewed, or degraded governed consolidation state. It never writes SQLite, Mem0, or Qdrant.")
def bhm_governed_consolidation_status() -> dict[str, Any]:
    return _get("/bhm/governed-consolidation/status")


@mcp.tool(name="bhm_governed_consolidation_create", description="Create or replay a bounded same-project consolidation proposal. It never applies a memory lifecycle mutation or writes Qdrant/Mem0.")
def bhm_governed_consolidation_create(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    memory_ids: Annotated[list[str], Field(min_length=1, max_length=32)],
    operation: Literal["no_op", "create", "revise", "supersede", "archive", "link"] = "create",
    candidate: dict[str, Any] | None = None,
    reason: Annotated[str | None, Field(max_length=480)] = None,
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.75,
) -> dict[str, Any]:
    return _post(
        "/bhm/governed-consolidation/proposals",
        {"project": project, "memory_ids": memory_ids, "operation": operation, "candidate": candidate, "reason": reason, "confidence": confidence},
    )


@mcp.tool(name="bhm_governed_semantic_proposal", description="Retrieve up to 20 semantic candidates, re-read their canonical SQLite revisions, and return or explicitly store one local-model proposal. Preview never mutates; an explicitly stored proposal may enter the separately feature-gated deterministic auto-review/apply path. It never writes Qdrant/Mem0 directly.")
def bhm_governed_semantic_proposal(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    query: Annotated[str, Field(min_length=1, max_length=480)],
    limit: Annotated[int, Field(ge=1, le=20)] = 12,
    store_proposal: bool = False,
    include_historical: bool = False,
) -> dict[str, Any]:
    return _post(
        "/bhm/governed-consolidation/semantic-proposals",
        {
            "project": project,
            "query": query,
            "limit": limit,
            "store_proposal": store_proposal,
            "include_historical": include_historical,
        },
    )


@mcp.tool(name="bhm_governed_semantic_shadow_metrics", description="Return content-free shadow metrics for stored local semantic editor proposals. It is read-only and never evaluates quality from model self-claims.")
def bhm_governed_semantic_shadow_metrics(
    project: Annotated[str, Field(min_length=1, max_length=160)],
) -> dict[str, Any]:
    return _get("/bhm/governed-consolidation/semantic-shadow-metrics", {"project": project})


@mcp.tool(name="bhm_governed_consolidation_list", description="List bounded project-scoped governed consolidation proposals. Read-only; proposal contents remain operator-scoped.")
def bhm_governed_consolidation_list(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    status: Annotated[str | None, Field(max_length=16)] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 100,
) -> dict[str, Any]:
    return _get("/bhm/governed-consolidation/proposals", {"project": project, "status": status, "limit": limit})


@mcp.tool(name="bhm_governed_consolidation_inspect", description="Inspect one project-scoped governed consolidation proposal and its redacted approval state. Read-only.")
def bhm_governed_consolidation_inspect(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    proposal_id: Annotated[str, Field(min_length=8, max_length=96)],
) -> dict[str, Any]:
    return _get(f"/bhm/governed-consolidation/proposals/{proposal_id}", {"project": project})


@mcp.tool(name="bhm_governed_consolidation_validate", description="Revalidate an existing proposal's project, current revision and content digests against SQLite authority. It never writes.")
def bhm_governed_consolidation_validate(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    proposal_id: Annotated[str, Field(min_length=8, max_length=96)],
) -> dict[str, Any]:
    return _get(f"/bhm/governed-consolidation/proposals/{proposal_id}/validate", {"project": project})


@mcp.tool(name="bhm_governed_consolidation_dry_run", description="Return the exact apply prerequisites and current stale result for a proposal. It never writes.")
def bhm_governed_consolidation_dry_run(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    proposal_id: Annotated[str, Field(min_length=8, max_length=96)],
) -> dict[str, Any]:
    return _post("/bhm/governed-consolidation/proposals/dry-run", {"project": project, "proposal_id": proposal_id})


@mcp.tool(name="bhm_governed_consolidation_decide", description="Admin-only: record one approve or reject decision for a project-scoped proposal. Approval alone never applies memory state.")
def bhm_governed_consolidation_decide(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    proposal_id: Annotated[str, Field(min_length=8, max_length=96)],
    decision: Literal["approve", "reject"],
) -> dict[str, Any]:
    return _post("/bhm/governed-consolidation/proposals/decision", {"project": project, "proposal_id": proposal_id, "decision": decision})


@mcp.tool(name="bhm_governed_consolidation_apply", description="Admin-only: apply exactly one approved proposal after explicit apply=true and matching proposal confirmation. Revalidates SQLite then uses the outbox; never writes Qdrant/Mem0 directly.")
def bhm_governed_consolidation_apply(
    project: Annotated[str, Field(min_length=1, max_length=160)],
    proposal_id: Annotated[str, Field(min_length=8, max_length=96)],
    confirmation: Annotated[str, Field(min_length=8, max_length=96)],
    apply: bool = False,
) -> dict[str, Any]:
    return _post("/bhm/governed-consolidation/proposals/apply", {"project": project, "proposal_id": proposal_id, "confirmation": confirmation, "apply": apply})


@mcp.tool(name="bhm_remember", description=f"Save a durable memory entry into BHM. {TAXONOMY_METADATA_HINT}")
def bhm_remember(
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
    concepts: list[str] | None = None,
    files: list[str] | None = None,
    metadata: MemoryMetadata | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "content": content,
        "project": project,
        "type": memory_type,
        "memory_class": memory_class.value if memory_class else None,
        "event_role": event_role.value if event_role else None,
        "concepts": concepts or [],
        "files": files or [],
    }
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
    if metadata is not None:
        body["metadata"] = _metadata_payload(metadata)
    return _post(
        "/bhm/remember",
        body,
    )


@mcp.tool(name="bhm_profile", description="Get compact BHM profile stats for a project.")
def bhm_profile(project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    return _get("/bhm/profile", {"project": project})


@mcp.tool(name="bhm_insights", description="Get compact BHM insight/checkpoint summary for a project.")
def bhm_insights(project: str = DEFAULT_PROJECT, limit: int = 5) -> dict[str, Any]:
    return _get("/bhm/insights", {"project": project, "limit": limit})


@mcp.tool(name="bhm_lessons_create", description="Create a lesson entry in BHM.")
def bhm_lessons_create(
    content: str,
    project: str = DEFAULT_PROJECT,
    context: str = "",
    confidence: float = 0.7,
    tags_csv: str | None = None,
) -> dict[str, Any]:
    return _post(
        "/bhm/lessons",
        {
            "content": content,
            "project": project,
            "context": context,
            "confidence": confidence,
            "tags": _parse_csv(tags_csv) or [],
        },
    )


@mcp.tool(name="bhm_lessons_search", description="Search BHM lessons for a project.")
def bhm_lessons_search(query: str, project: str = DEFAULT_PROJECT, limit: int = 5) -> dict[str, Any]:
    return _post("/bhm/lessons/search", {"query": query, "project": project, "limit": limit})


@mcp.tool(name="bhm_observe", description="Send a compact observe event into BHM.")
def bhm_observe(
    hook_type: Annotated[str, Field(min_length=1)],
    session_id: Annotated[str, Field(min_length=1)],
    cwd: str = "",
    project: str = DEFAULT_PROJECT,
    data_json: str | None = None,
    timestamp: str | None = None,
    parent_event_id: str | None = None,
) -> dict[str, Any]:
    return _post(
        "/bhm/observe",
        {
            "hookType": hook_type,
            "sessionId": session_id,
            "project": project,
            "cwd": cwd,
            "timestamp": timestamp,
            "parentEventId": parent_event_id,
            "data": _jsonable_or_text(data_json),
        },
    )


@mcp.tool(name="bhm_slot_list", description="List BHM slots for a project.")
def bhm_slot_list(project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    return _get("/bhm/slots", {"project": project})


@mcp.tool(name="bhm_slot_get", description="Get a single BHM slot by label.")
def bhm_slot_get(label: str, project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    return _get("/bhm/slot", {"project": project, "label": label})


@mcp.tool(name="bhm_slot_set", description="Create or overwrite a BHM slot.")
def bhm_slot_set(
    label: str,
    content: str,
    project: str = DEFAULT_PROJECT,
    size_limit: int = 2000,
    description: str = "",
    pinned: bool = True,
    scope: str = "project",
) -> dict[str, Any]:
    return _post(
        "/bhm/slot",
        {
            "label": label,
            "content": content,
            "sizeLimit": size_limit,
            "description": description,
            "pinned": pinned,
            "scope": scope,
            "project": project,
        },
    )


@mcp.tool(name="bhm_slot_append", description="Append text to a BHM slot.")
def bhm_slot_append(label: str, text: str, project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    return _post("/bhm/slot/append", {"label": label, "text": text, "project": project})


@mcp.tool(name="bhm_slot_replace", description="Replace BHM slot content.")
def bhm_slot_replace(label: str, content: str, project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    return _post("/bhm/slot/replace", {"label": label, "content": content, "project": project})


@mcp.tool(name="bhm_slot_delete", description="Delete a BHM slot.")
def bhm_slot_delete(label: str, project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    return _delete("/bhm/slot", {"label": label, "project": project})


@mcp.tool(name="bhm_slot_reflect", description="Reflect a BHM slot.")
def bhm_slot_reflect(label: str, project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    return _post("/bhm/slot/reflect", {"label": label, "project": project})


def _public_code_tool(
    operation: str,
    *,
    project: str = DEFAULT_PROJECT,
    root: str = ".",
    apply: bool = False,
    build_graph: bool = True,
    defer_graph: bool = False,
    graph_only: bool = False,
    force_refresh: bool = False,
    max_files_per_run: int = BHM_INDEX_MAX_FILES_PER_RUN,
    expected_job_id: str | None = None,
    expected_state_digest: str | None = None,
    query: str = "",
    graph_operation: str = "symbol",
    depth: int = 2,
    limit: int = 32,
    offset: int = 0,
    max_tokens: int = 4_096,
    time_budget_ms: float = 250.0,
    snapshot_id: str | None = None,
    expected_graph_digest: str | None = None,
    changed_paths: list[str] | None = None,
    base_revision: str | None = None,
    include_git_history: bool = True,
    semantic_fusion: bool = False,
    semantic_weight: float = 0.35,
    semantic_query: list[str] | None = None,
    semantic_min_score: float = 0.0,
    search_mode: str = "text",
    include_snippets: bool = False,
    snippet_max_chars: int = 280,
    path: str | None = None,
    line: int = 1,
    context: int = 2,
    artifact_path: str | None = None,
    detached_signature_b64: str | None = None,
    detached_public_key_b64: str | None = None,
    adoption_receipt_digest: str | None = None,
    rollback_anchor_snapshot_id: str | None = None,
    rollback_anchor_digest: str | None = None,
    cycles: int = 1,
    interval_seconds: float = 0.0,
) -> dict[str, Any]:
    return _post(
        "/bhm/code-tools",
        {
            "operation": operation,
            "project": project,
            "root": root,
            "apply": apply,
            "build_graph": build_graph,
            "defer_graph": defer_graph,
            "graph_only": graph_only,
            "force_refresh": force_refresh,
            "max_files_per_run": max_files_per_run,
            "expected_job_id": expected_job_id,
            "expected_state_digest": expected_state_digest,
            "query": query,
            "graph_operation": graph_operation,
            "depth": depth,
            "limit": limit,
            "offset": offset,
            "max_tokens": max_tokens,
            "time_budget_ms": time_budget_ms,
            "snapshot_id": snapshot_id,
            "expected_graph_digest": expected_graph_digest,
            "changed_paths": changed_paths or [],
            "base_revision": base_revision,
            "include_git_history": include_git_history,
            "semantic_fusion": semantic_fusion,
            "semantic_weight": semantic_weight,
            "semantic_query": semantic_query,
            "semantic_min_score": semantic_min_score,
            "search_mode": search_mode,
            "include_snippets": include_snippets,
            "snippet_max_chars": snippet_max_chars,
            "path": path,
            "line": line,
            "context": context,
            "artifact_path": artifact_path,
            "detached_signature_b64": detached_signature_b64,
            "detached_public_key_b64": detached_public_key_b64,
            "adoption_receipt_digest": adoption_receipt_digest,
            "rollback_anchor_snapshot_id": rollback_anchor_snapshot_id,
            "rollback_anchor_digest": rollback_anchor_digest,
            "cycles": cycles,
            "interval_seconds": interval_seconds,
        },
    )


@mcp.tool(name="bhm_index_repository", description="Run a bounded resumable repository-index slice. Read-only plan by default; set apply=true explicitly. Graph construction is deferred by default and may be completed with graph_only=true plus the completed snapshot_id.")
def bhm_index_repository(
    project: str = DEFAULT_PROJECT,
    root: str = ".",
    apply: bool = False,
    build_graph: bool = True,
    force_refresh: bool = False,
    max_files_per_run: int = BHM_INDEX_MAX_FILES_PER_RUN,
    expected_job_id: str | None = None,
    expected_state_digest: str | None = None,
    defer_graph: bool = True,
    graph_only: bool = False,
    snapshot_id: str | None = None,
) -> dict[str, Any]:
    return _public_code_tool(
        "index",
        project=project,
        root=root,
        apply=apply,
        build_graph=build_graph,
        defer_graph=defer_graph,
        graph_only=graph_only,
        force_refresh=force_refresh,
        max_files_per_run=max_files_per_run,
        expected_job_id=expected_job_id,
        expected_state_digest=expected_state_digest,
        snapshot_id=snapshot_id,
    )


@mcp.tool(name="bhm_index_status", description="Get authoritative repository-index and code-graph freshness for an allowlisted repository.")
def bhm_index_status(project: str = DEFAULT_PROJECT, root: str = ".") -> dict[str, Any]:
    return _public_code_tool("status", project=project, root=root)


@mcp.tool(name="bhm_list_projects", description="List repository projects currently published in the BHM SQLite index.")
def bhm_list_projects() -> dict[str, Any]:
    return _public_code_tool("projects")


@mcp.tool(name="bhm_watch_repository", description="Run an explicit bounded repository watcher/index cycle; no background daemon is started and apply must be explicit. Graph construction is deferred by default.")
def bhm_watch_repository(project: str = DEFAULT_PROJECT, root: str = ".", apply: bool = False, cycles: int = 1, interval_seconds: float = 0.0, build_graph: bool = True, defer_graph: bool = True) -> dict[str, Any]:
    return _public_code_tool("watch", project=project, root=root, apply=apply, cycles=cycles, interval_seconds=interval_seconds, build_graph=build_graph, defer_graph=defer_graph)


@mcp.tool(name="bhm_search_graph", description="Search the bounded BHM code graph by symbol/path/name without returning raw source.")
def bhm_search_graph(query: str = "", project: str = DEFAULT_PROJECT, root: str = ".", limit: int = 32) -> dict[str, Any]:
    return _public_code_tool("search", project=project, root=root, query=query, limit=limit)


@mcp.tool(name="bhm_search_code", description="Search indexed repository code with bounded lexical matching, optional deterministic graph-metadata semantic_query results, and optional feature-flagged Qdrant metadata fusion; source is not persisted and snippets are redacted and opt-in. Semantic token/time budgets are bounded.")
def bhm_search_code(query: str = "", project: str = DEFAULT_PROJECT, root: str = ".", mode: str = "text", limit: int = 32, offset: int = 0, include_snippets: bool = False, semantic_fusion: bool = False, semantic_weight: float = 0.35, semantic_query: list[str] | None = None, semantic_min_score: float = 0.0, max_tokens: int = 4_096, time_budget_ms: float = 250.0) -> dict[str, Any]:
    return _public_code_tool("code_search", project=project, root=root, query=query, search_mode=mode, limit=limit, offset=offset, include_snippets=include_snippets, semantic_fusion=semantic_fusion, semantic_weight=semantic_weight, semantic_query=semantic_query, semantic_min_score=semantic_min_score, max_tokens=max_tokens, time_budget_ms=time_budget_ms)


@mcp.tool(name="bhm_get_code_snippet", description="Return a small redacted line-numbered snippet from an indexed file; never persists source and never returns unredacted raw source.")
def bhm_get_code_snippet(path: str, line: int = 1, context: int = 2, project: str = DEFAULT_PROJECT, root: str = ".") -> dict[str, Any]:
    return _public_code_tool("code_snippet", project=project, root=root, path=path, line=line, context=context)


@mcp.tool(name="bhm_export_graph_artifact", description="Export the current SQLite-authoritative code graph as a bounded gzip+SHA256 non-authoritative sharing artifact; set apply=true explicitly.")
def bhm_export_graph_artifact(project: str = DEFAULT_PROJECT, root: str = ".", apply: bool = False) -> dict[str, Any]:
    return _public_code_tool("graph_artifact_export", project=project, root=root, apply=apply)


@mcp.tool(name="bhm_verify_graph_artifact", description="Verify a BHM shared graph artifact checksum, provenance and schema without importing or changing SQLite.")
def bhm_verify_graph_artifact(path: str, project: str = DEFAULT_PROJECT, root: str = ".") -> dict[str, Any]:
    return _public_code_tool("graph_artifact_verify", project=project, root=root, artifact_path=path)


@mcp.tool(name="bhm_plan_graph_artifact_promotion", description="Build a source-free, human-gated compatibility, detached-signature and rollback plan for a verified graph artifact; never imports or applies it.")
def bhm_plan_graph_artifact_promotion(
    path: str,
    project: str = DEFAULT_PROJECT,
    root: str = ".",
    detached_signature_b64: str | None = None,
    detached_public_key_b64: str | None = None,
    adoption_receipt_digest: str | None = None,
    rollback_anchor_snapshot_id: str | None = None,
    rollback_anchor_digest: str | None = None,
) -> dict[str, Any]:
    return _public_code_tool(
        "graph_artifact_promotion_plan",
        project=project,
        root=root,
        artifact_path=path,
        detached_signature_b64=detached_signature_b64,
        detached_public_key_b64=detached_public_key_b64,
        adoption_receipt_digest=adoption_receipt_digest,
        rollback_anchor_snapshot_id=rollback_anchor_snapshot_id,
        rollback_anchor_digest=rollback_anchor_digest,
    )


@mcp.tool(name="bhm_query_graph", description="Run one allowlisted bounded graph operation with metadata-only pagination: symbol, resolve, callers, callees, imports, importers, routes, tests, impact, or neighborhood.")
def bhm_query_graph(query: str = "", operation: str = "symbol", project: str = DEFAULT_PROJECT, root: str = ".", depth: int = 2, limit: int = 32, offset: int = 0) -> dict[str, Any]:
    return _public_code_tool("graph", project=project, root=root, query=query, graph_operation=operation, depth=depth, limit=limit, offset=offset)


@mcp.tool(name="bhm_query_graph_dsl", description="Run the bounded read-only Cypher-like graph pattern subset; only metadata rows are returned and writes/arbitrary SQL are rejected.")
def bhm_query_graph_dsl(query: str, project: str = DEFAULT_PROJECT, root: str = ".", limit: int = 32, offset: int = 0, time_budget_ms: float = 250.0) -> dict[str, Any]:
    return _public_code_tool("graph_query", project=project, root=root, query=query, limit=limit, offset=offset, time_budget_ms=time_budget_ms)


@mcp.tool(name="bhm_get_graph_schema", description="Return the versioned BHM graph schema, parser registry digest, and allowlisted operations.")
def bhm_get_graph_schema(project: str = DEFAULT_PROJECT, root: str = ".") -> dict[str, Any]:
    return _public_code_tool("schema", project=project, root=root)


@mcp.tool(name="bhm_check_index_coverage", description="Report repository-index freshness and parser coverage with fail-closed completeness status.")
def bhm_check_index_coverage(project: str = DEFAULT_PROJECT, root: str = ".") -> dict[str, Any]:
    return _public_code_tool("coverage", project=project, root=root)


@mcp.tool(name="bhm_get_architecture", description="Return bounded architecture summary derived from the authoritative code graph; no raw source is returned.")
def bhm_get_architecture(project: str = DEFAULT_PROJECT, root: str = ".") -> dict[str, Any]:
    return _public_code_tool("architecture", project=project, root=root)


@mcp.tool(name="bhm_resolve_packages", description="Resolve bounded package/module identities from allowlisted manifests; versions, URLs, secrets and raw manifest text never leave BHM.")
def bhm_resolve_packages(project: str = DEFAULT_PROJECT, root: str = ".", limit: int = 32) -> dict[str, Any]:
    return _public_code_tool("package_resolution", project=project, root=root, limit=limit)


@mcp.tool(name="bhm_dependency_provenance", description="Inventory recognized local lockfiles as bounded metadata-only dependency provenance; versions, URLs, credentials, raw lockfile text, network and package managers are never used.")
def bhm_dependency_provenance(project: str = DEFAULT_PROJECT, root: str = ".", limit: int = 32) -> dict[str, Any]:
    return _public_code_tool("dependency_provenance", project=project, root=root, limit=limit)


@mcp.tool(name="bhm_type_references", description="Return bounded proposal-only inheritance, implements, type-alias and import-reference metadata from the authoritative code graph; unresolved relationships remain visible and no source is returned.")
def bhm_type_references(project: str = DEFAULT_PROJECT, root: str = ".", limit: int = 64) -> dict[str, Any]:
    return _public_code_tool("type_references", project=project, root=root, limit=limit)


@mcp.tool(name="bhm_bicep_module_resolution", description="Return bounded proposal-only resolution of literal Bicep module targets from the authoritative graph; unresolved and ambiguous targets remain visible and no compiler or source access is used.")
def bhm_bicep_module_resolution(project: str = DEFAULT_PROJECT, root: str = ".", limit: int = 64) -> dict[str, Any]:
    return _public_code_tool("bicep_module_resolution", project=project, root=root, limit=limit)


@mcp.tool(name="bhm_trace_path", description="Trace bounded callers/callees paths with graph provenance and explicit depth/token budgets.")
def bhm_trace_path(query: str, project: str = DEFAULT_PROJECT, root: str = ".", operation: str = "callers", depth: int = 2, limit: int = 32) -> dict[str, Any]:
    return _public_code_tool("trace", project=project, root=root, query=query, graph_operation=operation, depth=depth, limit=limit)


@mcp.tool(name="bhm_trace_graph", description="Build a bounded evidence-only cross-service trace graph. Runtime observations remain untrusted corroboration and are never promoted to graph authority.")
def bhm_trace_graph(project: str = DEFAULT_PROJECT, limit: int = 64) -> dict[str, Any]:
    return _public_code_tool("trace_evidence", project=project, limit=limit)


@mcp.tool(name="bhm_cross_repo_links", description="Return bounded proposal-only CROSS_* links between published repository graphs; never promotes edges or returns source.")
def bhm_cross_repo_links(project: str = DEFAULT_PROJECT, limit: int = 64) -> dict[str, Any]:
    return _public_code_tool("cross_repo", project=project, limit=limit)


@mcp.tool(name="bhm_change_impact", description="Return bounded graph impact analysis for a symbol/path; read-only and provenance-bearing.")
def bhm_change_impact(query: str, project: str = DEFAULT_PROJECT, root: str = ".", depth: int = 2, limit: int = 32) -> dict[str, Any]:
    return _public_code_tool("impact", project=project, root=root, query=query, graph_operation="impact", depth=depth, limit=limit)


@mcp.tool(name="bhm_change_impact_preview", description="Return a bounded proposal-only git/change impact preview with base/head revisions, graph digest and test impact; never writes the worktree.")
def bhm_change_impact_preview(project: str = DEFAULT_PROJECT, root: str = ".", changed_paths: list[str] | None = None, base_revision: str | None = None, expected_graph_digest: str | None = None, include_git_history: bool = True) -> dict[str, Any]:
    return _public_code_tool("impact_preview", project=project, root=root, changed_paths=changed_paths or [], base_revision=base_revision, expected_graph_digest=expected_graph_digest, include_git_history=include_git_history)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
