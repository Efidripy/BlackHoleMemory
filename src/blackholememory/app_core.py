"""Domain module app_core: moved out of app.py (FIX-017). Cross-module calls go through module attributes."""

from __future__ import annotations

import time
import asyncio
import copy
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from fastapi import HTTPException
from pydantic import BaseModel
from . import app_core_fallback
from . import app_core_hit
from . import app_runtime as rt
from .app_runtime import ResponseTimeout, _env_float, _env_int, _run_bounded_read, _safe_exception_text
from .api_models import SearchRequest, RememberRequest, MemoryUpdateRequest, MemoryUpsertRequest, MemoryCrystallizeRequest, MemoryDetectRequest
from .capability import configured_admin_capability
from .capability import extract_mcp_capability
from .capability import MCP_OPERATOR_SESSION
from .capability import is_admin_capability_valid
from .mcp_protocol_contract import validate_bhm_remember_arguments
from .ingress_admission import IngressKind
from .ingress_admission import admit_ingress
from .mem0_adapter import StorageNotReady
from .mem0_adapter import ensure_decay_metadata
from .mem0_adapter import global_collection_name
from .mem0_adapter import initial_decay_metadata
from .mem0_adapter import local_collection_name
from .mcp_surfaces import filter_tools
from .mcp_surfaces import is_tool_allowed
from .mcp_surfaces import requires_admin_capability
from .mcp_surfaces import resolve_mcp_surface
from .mcp_protocol_contract import initialize_capabilities
from .mcp_protocol_contract import negotiate_protocol_version
from .mcp_protocol_contract import ProtocolContractError
from .mcp_streamable_http import BhmStreamableHttpGateway
from .semantic_readiness import SemanticReadinessCache
from .semantic_readiness import build_readiness_key
from .semantic_readiness import evaluate_semantic_readiness
from .semantic_readiness import project_warmup_state
from .memory_service import MemoryServiceNotReady
from .typed_memory_contract import TypedMemoryContractUnavailable
from .typed_memory_contract import classify_new_memory
from .typed_memory_contract import typed_memory_capability_available
from . import memory_contracts as _memory_contracts
from .vector_routing import route_vector_targets
from .version_manifest import BROKER_VERSION
from .observation_security import PayloadSanitizer
from .observation_security import redact_secret_text
from .usage_telemetry import UsageTelemetry
from .usage_telemetry import monotonic_elapsed_ms
from .usage_telemetry import normalize_operation
from .code_graph import PARSER_REGISTRY_DIGEST
from .error_taxonomy import classify_jsonrpc_error
from .temporal_contract import normalize_temporal_fields
from .temporal_contract import temporal_contract_enabled


MemoryMetadata = _memory_contracts.MemoryMetadata


MemoryClassSource = _memory_contracts.MemoryClassSource


EVENT_ROLE_SCHEMA_VERSION = _memory_contracts.EVENT_ROLE_SCHEMA_VERSION


_USAGE_TELEMETRY = UsageTelemetry()


_SEMANTIC_READINESS_CACHE = SemanticReadinessCache(
    ttl_seconds=_env_float("BHM_SEMANTIC_READINESS_CACHE_TTL_SECONDS", 30.0, 1.0)
)


def _mcp_model_dump(value: Any) -> Any:
    if hasattr(value, "to_mcp_tool"):
        value = value.to_mcp_tool()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {key: _mcp_model_dump(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_mcp_model_dump(item) for item in value]
    return value


def _compact_mcp_input_schema_descriptions(value: Any) -> Any:
    """Drop only verbose nested schema prose from the bounded core catalog."""

    if isinstance(value, dict):
        return {
            key: _compact_mcp_input_schema_descriptions(item)
            for key, item in value.items()
            if key != "description" or not isinstance(item, str) or len(item) <= 120
        }
    if isinstance(value, list):
        return [_compact_mcp_input_schema_descriptions(item) for item in value]
    return value


_MCP_CORE_SCHEMA_PROPERTY_LEVELS = 2


def _collapse_nested_mcp_schema_properties(value: Any, level: int = 0) -> Any:
    """Replace property subschemas nested deeper than the core budget with ``{}``.

    ``{}`` is a valid "any value" JSON Schema, so strict clients keep the tool;
    the server-side pydantic models still validate the full argument shape.
    """

    if isinstance(value, list):
        return [_collapse_nested_mcp_schema_properties(item, level) for item in value]
    if not isinstance(value, dict):
        return value
    collapsed: dict[str, Any] = {}
    for key, item in value.items():
        if key == "properties" and isinstance(item, dict):
            collapsed[key] = {
                name: {} if level >= _MCP_CORE_SCHEMA_PROPERTY_LEVELS else _collapse_nested_mcp_schema_properties(sub, level + 1)
                for name, sub in item.items()
            }
        else:
            collapsed[key] = _collapse_nested_mcp_schema_properties(item, level)
    return collapsed


def _mcp_catalog_tool_dump(value: Any, *, compact_schema: bool = False) -> dict[str, Any]:
    """Publish a compact core catalog without FastMCP implementation extras."""

    dumped = _mcp_model_dump(value)
    if not isinstance(dumped, dict):
        return {}
    # ``outputSchema`` and ``meta.fastmcp`` are optional implementation
    # metadata, not part of BHM's stable tool admission contract.  Omitting
    # them keeps the default attach catalog below the bounded frame budget.
    dumped.pop("outputSchema", None)
    dumped.pop("meta", None)
    description = dumped.get("description")
    if isinstance(description, str) and len(description) > 240:
        dumped["description"] = description[:237].rstrip() + "..."
    if compact_schema and isinstance(dumped.get("inputSchema"), dict):
        dumped["inputSchema"] = _collapse_nested_mcp_schema_properties(
            _compact_mcp_input_schema_descriptions(dumped["inputSchema"])
        )
    return dumped


_MCP_CATALOG_MAX_DEPTH = 64


def _jsonrpc_success(request_id: Any, result: dict[str, Any], *, max_depth: int | None = None) -> dict[str, Any]:
    """Return a bounded JSON-RPC result without leaking nested sensitive fields."""

    # MCP catalogue and bounded query responses legitimately contain more
    # than the observation-ingress default of 128 members.  Keep their
    # established response contract while still redacting recursively.
    if max_depth is None:
        sanitized = PayloadSanitizer(max_collection_items=2_000).sanitize(result)
    else:
        sanitized = PayloadSanitizer(max_collection_items=2_000, max_depth=max_depth).sanitize(result)
    return {"jsonrpc": "2.0", "id": request_id, "result": sanitized}


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    safe_message = redact_secret_text(str(message)).value[:2_000]
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": safe_message,
            "data": {
                "bhm_error_code": classify_jsonrpc_error(code),
                "schema_version": "bhm.error-taxonomy.v1",
            },
        },
    }


def _validate_bhm_remember_mcp_arguments(arguments: dict[str, Any]) -> str | None:
    return validate_bhm_remember_arguments(arguments)


async def _handle_mcp_gateway_jsonrpc_core(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = str(message.get("method") or "")
    params = message.get("params") or {}
    is_notification = "id" not in message

    if not isinstance(params, dict):
        return _jsonrpc_error(request_id, -32600, "JSON-RPC params must be an object")
    if method in {"notifications/initialized", "notifications/cancelled", "exit"}:
        if is_notification:
            return None
        return _jsonrpc_error(request_id, -32600, f"{method} must be sent as a notification")
    if is_notification:
        return None
    if method == "initialize":
        try:
            requested_version = negotiate_protocol_version(params.get("protocolVersion"))
        except ProtocolContractError as exc:
            return _jsonrpc_error(request_id, exc.code, _safe_exception_text(exc))
        surface = resolve_mcp_surface()
        return _jsonrpc_success(
            request_id,
            {
                "protocolVersion": requested_version,
                "capabilities": initialize_capabilities(),
                "serverInfo": {
                    "name": "bhm",
                    "version": BROKER_VERSION,
                    "surface": surface.value,
                    "adminCapabilityRequired": surface.value == "admin",
                    "adminCapabilityConfigured": bool(configured_admin_capability()),
                },
            },
        )
    if method == "shutdown":
        return _jsonrpc_success(request_id, {})
    if method == "ping":
        return _jsonrpc_success(request_id, {})
    if method in {"resources/list", "resources/templates/list", "prompts/list"}:
        key = {
            "resources/list": "resources",
            "resources/templates/list": "resourceTemplates",
            "prompts/list": "prompts",
        }[method]
        return _jsonrpc_success(request_id, {key: []})
    if method == "tools/list":
        from . import bhm_mcp

        surface = resolve_mcp_surface()
        requested_tools = surface
        if surface.value == "admin" and not is_admin_capability_valid(extract_mcp_capability(params)):
            requested_tools = resolve_mcp_surface("core")
        tools = filter_tools(await bhm_mcp.mcp.list_tools(), requested_tools)
        return _jsonrpc_success(
            request_id,
            {
                "tools": [
                    _mcp_catalog_tool_dump(tool, compact_schema=requested_tools.value == "core")
                    for tool in tools
                ]
            },
            # Server-generated input schemas nest deeper than the observation default;
            # truncating them leaves strings where subschemas belong and strict clients drop the tool.
            max_depth=_MCP_CATALOG_MAX_DEPTH,
        )
    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if not name:
            return _jsonrpc_error(request_id, -32600, "tools/call requires params.name")
        if not isinstance(arguments, dict):
            return _jsonrpc_error(request_id, -32600, "tools/call params.arguments must be an object")
        surface = resolve_mcp_surface()
        if not is_tool_allowed(name, surface):
            return _jsonrpc_error(
                request_id,
                -32601,
                f"MCP tool '{name}' is not available on '{surface.value}' surface",
            )
        if requires_admin_capability(name, surface) and not is_admin_capability_valid(extract_mcp_capability(params)):
            return _jsonrpc_error(request_id, -32003, "BHM admin capability is required for this tool")
        if name == "bhm_remember":
            validation_error = _validate_bhm_remember_mcp_arguments(arguments)
            if validation_error:
                return _jsonrpc_error(request_id, -32600, validation_error)
        if name == "bhm_remember" and MCP_OPERATOR_SESSION.get():
            try:
                remember_request = _remember_request_from_mcp_arguments(arguments)
                remember_result = await _remember_authoritative(
                    remember_request,
                    actor="mcp-http-operator",
                )
            except HTTPException as exc:
                detail = exc.detail
                if isinstance(detail, dict) and detail.get("code") == "ingress_admission_rejected":
                    rejection = {"success": False, **detail}
                    return _jsonrpc_success(
                        request_id,
                        {
                            "content": [{"type": "text", "text": json.dumps(rejection, ensure_ascii=False)}],
                            "isError": True,
                            "structuredContent": rejection,
                        },
                    )
                return _jsonrpc_error(request_id, -32603, _safe_exception_text(exc))
            except Exception as exc:  # broad: MCP operator boundary must return JSON-RPC errors
                app_core_fallback._LOGGER.warning("MCP remember operation failed (%s)", type(exc).__name__)
                return _jsonrpc_error(request_id, -32603, f"{name} failed: {redact_secret_text(str(exc)).value}")
            return _jsonrpc_success(
                request_id,
                {
                    "content": [{"type": "text", "text": json.dumps(remember_result, ensure_ascii=False)}],
                    "isError": False,
                    "structuredContent": remember_result,
                },
            )
        try:
            from . import bhm_mcp

            content = await bhm_mcp.mcp.call_tool(name, arguments)
        except Exception as exc:  # broad: MCP tool boundary must return JSON-RPC errors
            app_core_fallback._LOGGER.warning("MCP tool operation failed (%s)", type(exc).__name__)
            return _jsonrpc_error(request_id, -32603, f"{name} failed: {redact_secret_text(str(exc)).value}")
        dumped = _mcp_model_dump(content)
        if isinstance(dumped, dict):
            result: dict[str, Any] = {
                "content": dumped.get("content") or [],
                "isError": bool(dumped.get("isError", dumped.get("is_error", False))),
            }
            structured_content = dumped.get("structuredContent", dumped.get("structured_content"))
            if structured_content is not None:
                result["structuredContent"] = structured_content
            meta = dumped.get("_meta", dumped.get("meta"))
            if meta is not None:
                result["_meta"] = meta
        else:
            result = {"content": dumped if isinstance(dumped, list) else [], "isError": False}
        return _jsonrpc_success(request_id, result)
    return _jsonrpc_error(request_id, -32601, f"Unsupported MCP method: {method}")


def _mcp_usage_operation(message: dict[str, Any]) -> str:
    if not isinstance(message, dict):
        return "invalid"
    method = normalize_operation(message.get("method"), fallback="unknown")
    if method != "tools/call":
        return method
    params = message.get("params")
    if not isinstance(params, dict):
        return method
    tool_name = normalize_operation(params.get("name"), fallback="other")
    return normalize_operation(f"{method}:{tool_name}", fallback=method)


def _mcp_usage_outcome(response: dict[str, Any] | None) -> tuple[str, bool]:
    if response is None:
        return "notification", False
    error = response.get("error")
    if not isinstance(error, dict):
        return "success", False
    try:
        error_code = int(error.get("code"))
    except (TypeError, ValueError):
        error_code = 0
    if error_code == -32004:
        return "timeout", True
    return "error", False


async def _handle_mcp_gateway_jsonrpc_async(message: dict[str, Any]) -> dict[str, Any] | None:
    started_at = time.perf_counter()
    response: dict[str, Any] | None = None
    status = "success"
    timed_out = False
    try:
        response = await _handle_mcp_gateway_jsonrpc_core(message)
        status, timed_out = _mcp_usage_outcome(response)
        return response
    except (asyncio.TimeoutError, TimeoutError, ResponseTimeout):
        status = "timeout"
        timed_out = True
        raise
    except Exception as exc:  # broad: telemetry records status before propagating failures
        app_core_fallback._LOGGER.warning("MCP gateway operation failed (%s)", type(exc).__name__)
        status = "exception"
        raise
    finally:
        _USAGE_TELEMETRY.record(
            surface="mcp",
            operation=_mcp_usage_operation(message),
            status=status,
            duration_ms=monotonic_elapsed_ms(started_at),
            timeout=timed_out,
        )


_MCP_STREAMABLE_HTTP = BhmStreamableHttpGateway(
    _handle_mcp_gateway_jsonrpc_async,
    server_version=BROKER_VERSION,
)


def _admit_new_content(*, ingress: IngressKind, project: str, actor: str, payload: Any, requires_authority: bool) -> dict[str, Any]:
    """Fail before SQLite, queue, projection, or file mutation on hostile ingress."""

    admission = admit_ingress(ingress=ingress, project=project, actor=actor, payload=payload)
    if not admission.admitted or (requires_authority and not admission.authority_eligible):
        raise HTTPException(status_code=422, detail={"code": "ingress_admission_rejected", "admission": admission.receipt()})
    return admission.receipt()


def _remember_response(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": True,
        "memory": {
            "id": record["source_id"],
            "title": record["metadata"]["raw_title"],
            "project": record["project"],
            "type": record["memory_type"],
            "memoryClass": _effective_memory_class(record),
            "createdAt": record["created_at"],
        },
    }


async def _remember_authoritative(request: RememberRequest, *, actor: str) -> dict[str, Any]:
    """Run the single operator-authority write path shared by REST and HTTP MCP."""

    _admit_new_content(
        ingress=IngressKind.OPERATOR,
        project=request.project,
        actor=actor,
        payload=request.model_dump(mode="json"),
        requires_authority=True,
    )
    record = await rt._run_bounded_write("bhm.remember", _remember_live_memory, request)
    return _remember_response(record)


def _remember_request_from_mcp_arguments(arguments: dict[str, Any]) -> RememberRequest:
    payload = dict(arguments)
    payload["type"] = payload.pop("memory_type", "workflow")
    payload["concepts"] = payload.get("concepts") or []
    payload["files"] = payload.get("files") or []
    return RememberRequest.model_validate(payload)


def _semantic_projected_code_metadata_count(
    *,
    project: str,
    graph_snapshot_id: str,
    graph_digest: str,
) -> int:
    """Count active SQLite projection candidates bound to one graph epoch.

    This is intentionally authoritative/local only.  It does not instantiate
    Mem0, query an embedding provider, contact Qdrant, or return source text.
    The outbox state below separately proves whether these rows have drained to
    the projection layer.
    """

    accepted_projects = rt._project_aliases(project)
    prefix = f"code-metadata:{rt._canonical_project(project)}:"
    service = rt._memory_service()
    counter = getattr(service, "count_projected_code_metadata", None)
    if callable(counter):
        return int(
            counter(
                projects=accepted_projects,
                upsert_key_prefix=prefix,
                graph_snapshot_id=graph_snapshot_id,
                graph_digest=graph_digest,
            )
        )

    # Compatibility fallback for narrow test doubles and older disposable
    # runtimes.  The live service always takes the bounded SQL path above.
    count = 0
    for record in rt._load_live_memories():
        if accepted_projects and record.get("project") not in accepted_projects:
            continue
        if _memory_lifecycle(record) != "active":
            continue
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        if str(metadata.get("source_kind") or "") != "code-graph-metadata":
            continue
        if not str(metadata.get("upsert_key") or "").startswith(prefix):
            continue
        if str(metadata.get("graph_snapshot_id") or "") != str(graph_snapshot_id or ""):
            continue
        if str(metadata.get("graph_digest") or "") != str(graph_digest or ""):
            continue
        count += 1
    return count


async def _semantic_readiness_receipt(
    *,
    project: str,
    current_graph: Mapping[str, Any],
    repository_snapshot: Mapping[str, Any],
    embedding_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Build/read the bounded per-project readiness receipt."""

    graph_snapshot_id = str(current_graph.get("graph_snapshot_id") or "")
    graph_digest = str(current_graph.get("graph_digest") or "")
    repository_snapshot_id = str(repository_snapshot.get("snapshot_id") or "")
    graph_repository_snapshot_id = str(current_graph.get("repository_snapshot_id") or "")
    repository_digest = str(repository_snapshot.get("snapshot_digest") or "")
    source_count = len(list(repository_snapshot.get("files") or []))
    max_files = _env_int("BHM_SEMANTIC_READINESS_MAX_FILES", 128, 1)
    selected_count = min(source_count, max_files)
    projected_count = await asyncio.to_thread(
        _semantic_projected_code_metadata_count,
        project=project,
        graph_snapshot_id=graph_snapshot_id,
        graph_digest=graph_digest,
    )
    try:
        outbox = await _run_bounded_read("semantic.readiness.outbox", rt._memory_service().outbox_status)
    except MemoryServiceNotReady:
        outbox = {"pending": 1, "failed": 1}
    projection_pending = max(int(outbox.get("pending") or 0), 0)
    projection_failed = max(int(outbox.get("failed") or 0), 0)
    warmup_status = rt._get_provider_warmup_status()
    provider_ready = bool(warmup_status.get("ready"))
    project_warmup_enabled, project_warmup_ready, project_warmup_phase = project_warmup_state(
        project, warmup_status
    )
    runtime_slo_status = (
        "healthy"
        if rt._memory_store_is_authoritative() and projection_pending == 0 and projection_failed == 0
        else "breached"
    )
    embedding_contract_digest = hashlib.sha256(
        json.dumps(dict(embedding_contract), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    key = build_readiness_key(
        project=project,
        graph_snapshot_id=graph_snapshot_id,
        graph_digest=graph_digest,
        repository_snapshot_digest=repository_digest,
        parser_registry_digest=PARSER_REGISTRY_DIGEST,
        embedding_contract_digest=embedding_contract_digest,
        source_row_count=source_count,
        selected_count=selected_count,
        projected_count=projected_count,
        projection_pending=projection_pending,
        projection_failed=projection_failed,
        provider_ready=provider_ready,
        project_warmup_enabled=project_warmup_enabled,
        project_warmup_ready=project_warmup_ready,
    )
    cached = _SEMANTIC_READINESS_CACHE.get(key)
    if cached is not None:
        return cached
    receipt = evaluate_semantic_readiness(
        project=project,
        graph_snapshot_id=graph_snapshot_id,
        graph_digest=graph_digest,
        current_graph_snapshot_id=graph_snapshot_id,
        graph_repository_snapshot_id=graph_repository_snapshot_id,
        current_repository_snapshot_id=repository_snapshot_id,
        repository_snapshot_digest=repository_digest,
        parser_registry_digest=PARSER_REGISTRY_DIGEST,
        embedding_contract_digest=embedding_contract_digest,
        provider_ready=provider_ready,
        runtime_slo_status=runtime_slo_status,
        source_row_count=source_count,
        selected_count=selected_count,
        projected_count=projected_count,
        projection_pending=projection_pending,
        projection_failed=projection_failed,
        skipped_count=max(source_count - selected_count, 0),
        project_warmup_enabled=project_warmup_enabled,
        project_warmup_ready=project_warmup_ready,
    )
    receipt["cache_key"] = key
    receipt["repository_snapshot_id"] = repository_snapshot_id
    receipt["provider"]["project_warmup_phase"] = project_warmup_phase
    return _SEMANTIC_READINESS_CACHE.put(key, receipt)


def _save_live_memories(items: list[dict]) -> Path:
    try:
        return rt._memory_service().upsert_records(items)
    except MemoryServiceNotReady as exc:
        raise StorageNotReady(str(exc)) from exc


def _replace_live_memory(record: dict) -> None:
    source_id = str(record.get("source_id") or "").strip()
    if not source_id:
        raise HTTPException(status_code=422, detail="memory source_id is required")
    try:
        project = rt._canonical_project(record.get("project")) if record.get("project") else None
        if rt._memory_service().get_record(source_id, project=project) is not None:
            rt._memory_service().upsert_records([record])
            return
    except MemoryServiceNotReady:
        pass
    live_records = rt._load_live_memories()
    for index, item in enumerate(live_records):
        if item.get("source_id") != record.get("source_id"):
            continue
        live_records[index] = record
        _save_live_memories(live_records)
        return
    raise HTTPException(status_code=404, detail="memory not found in live store")


def _append_memory_changelog(record: dict, action: str, details: dict | None = None) -> dict:
    metadata = record.setdefault("metadata", {})
    changelog = metadata.setdefault("changelog", [])
    changelog.append(
        {
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "action": action,
            "details": details or {},
        }
    )
    metadata["changelog"] = changelog[-200:]
    return record


def _serialize_memory_record(record: dict) -> dict:
    metadata = dict(record.get("metadata") or {})
    memory_class = str(
        record.get("memory_class")
        or metadata.get("memory_class")
        or app_core_hit.MemoryClass.UNCLASSIFIED.value
    )
    return {
        "id": record.get("source_id"),
        "title": metadata.get("raw_title") or _build_memory_title(record.get("content") or ""),
        "project": record.get("project"),
        "type": record.get("memory_type"),
        "memory_class": memory_class,
        "memory_class_source": (
            record.get("memory_class_source")
            or metadata.get("memory_class_source")
            or MemoryClassSource.LEGACY_DEFAULT.value
        ),
        "memory_class_confidence": (
            record.get("memory_class_confidence")
            if record.get("memory_class_confidence") is not None
            else metadata.get("memory_class_confidence")
        ),
        "event_role": _effective_event_role(record),
        "event_role_version": (
            record.get("event_role_version")
            or metadata.get("event_role_version")
            or EVENT_ROLE_SCHEMA_VERSION
        ),
        "observed_at": record.get("observed_at") or metadata.get("observed_at"),
        "observed_at_source": record.get("observed_at_source") or metadata.get("observed_at_source") or "legacy-unknown",
        "valid_from": record.get("valid_from") or metadata.get("valid_from"),
        "valid_to": record.get("valid_to") or metadata.get("valid_to"),
        "open_interval": (
            record.get("open_interval")
            if record.get("open_interval") is not None
            else metadata.get("open_interval", True)
        ),
        "supersedes_revision_id": record.get("supersedes_revision_id") or metadata.get("supersedes_revision_id"),
        "source_episode_id": record.get("source_episode_id") or metadata.get("source_episode_id"),
        "source_uri": record.get("source_uri") or metadata.get("source_uri"),
        "source_digest": record.get("source_digest") or metadata.get("source_digest"),
        "content": record.get("content"),
        "concepts": record.get("tags") or [],
        "files": metadata.get("files") or [],
        "source_system": record.get("source_system"),
        "agent_id": record.get("agent_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "lifecycle": _memory_lifecycle(record),
        "archived_at": metadata.get("archived_at"),
        "archive_reason": metadata.get("archive_reason"),
        "upsert_key": metadata.get("upsert_key"),
        "session_refs": record.get("session_refs") or [],
        "metadata": metadata,
    }


def _is_archived_memory(record: dict) -> bool:
    metadata = record.get("metadata") or {}
    lifecycle = str(metadata.get("lifecycle") or "").lower()
    return bool(metadata.get("archived_at") or record.get("archived_at") or lifecycle in {"archived", "deprecated"})


def _memory_lifecycle(record: dict) -> str:
    metadata = record.get("metadata") or {}
    lifecycle = str(record.get("lifecycle") or metadata.get("lifecycle") or "active").strip().lower()
    if lifecycle in {"tombstone", "tombstoned", "purged", "deleted"}:
        return "tombstoned"
    if lifecycle in {"archived", "archive", "deprecated"} or _is_archived_memory(record):
        return "archived"
    return "active"


def _effective_memory_class(record: Mapping[str, Any]) -> str:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    return str(
        record.get("memory_class")
        or metadata.get("memory_class")
        or app_core_hit.MemoryClass.UNCLASSIFIED.value
    ).strip().lower()


def _effective_event_role(record: Mapping[str, Any]) -> str:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    return str(
        record.get("event_role")
        or metadata.get("event_role")
        or app_core_hit.MemoryEventRole.UNCLASSIFIED.value
    ).strip().lower()


def _metadata_matches_taxonomy_filters(
    metadata: dict[str, Any],
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    include_archived: bool = False,
    include_logs: bool = True,
) -> bool:
    lifecycle = str(metadata.get("lifecycle") or "").lower()
    if lifecycle in {"tombstone", "tombstoned", "purged", "deleted"}:
        return False
    if not include_archived and (metadata.get("archived_at") or lifecycle in {"archived", "deprecated"}):
        return False

    record_semantic_type = str(metadata.get("semantic_type") or "").lower()
    if not include_logs and record_semantic_type in {"log", "error"}:
        return False

    if domain and metadata.get("domain") != domain:
        return False
    if semantic_type and metadata.get("semantic_type") != semantic_type:
        return False
    if priority and metadata.get("priority") != priority:
        return False

    return True


def _find_live_memory_by_upsert_key(project: str, upsert_key: str) -> dict | None:
    try:
        record = rt._memory_service().get_record_by_upsert_key(
            rt._canonical_project(project),
            upsert_key,
        )
        if record is not None:
            return record
    except MemoryServiceNotReady:
        pass
    accepted_projects = rt._project_aliases(project)
    for item in rt._load_live_memories():
        metadata = item.get("metadata") or {}
        # Tombstoned/archived records are historical state, not a live
        # idempotency target. A new guarded upsert must create an active
        # record; silently updating a tombstone would make a successful
        # projection invisible to retrieval and leave rollback ambiguous.
        lifecycle = str(item.get("lifecycle") or metadata.get("lifecycle") or "active")
        if (
            item.get("project") in accepted_projects
            and lifecycle == "active"
            and metadata.get("upsert_key") == upsert_key
        ):
            return item
    return None


def _vector_item_matches_search_request(item: dict, request: SearchRequest) -> bool:
    metadata = item.get("metadata") or {}
    metadata_source_id = metadata.get("source_id")
    project = app_core_fallback._effective_search_project(request.project)
    live_record = _resolve_live_memory_for_vector_item(item, project)
    if live_record is not None:
        item["metadata"] = _mem0_metadata_for_record(live_record)
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
    if request.memory_class and _effective_memory_class(item) != request.memory_class.value:
        return False
    if request.event_role and _effective_event_role(item) != request.event_role.value:
        return False
    return _metadata_matches_taxonomy_filters(
        metadata,
        domain=request.domain,
        semantic_type=request.semantic_type,
        priority=request.priority,
        include_archived=request.include_archived,
        include_logs=request.include_logs,
    )


def _resolve_live_memory_for_vector_item(item: dict, project: str | None = None) -> dict | None:
    metadata = item.get("metadata") or {}
    candidate_ids = [
        metadata.get("source_id"),
        metadata.get("id"),
        item.get("source_id"),
        item.get("id"),
        item.get("hash"),
    ]
    for candidate_id in candidate_ids:
        if not candidate_id:
            continue
        record = rt._find_live_memory(str(candidate_id), project)
        if record is not None:
            return record

    return None


def _build_memory_title(content: str) -> str:
    first_line = (content or "").strip().splitlines()[0] if (content or "").strip() else "memory"
    return first_line[:80]


_PROTECTED_MEMORY_METADATA_KEYS = frozenset(
    {
        "project",
        "source_system",
        "source_id",
        "memory_type",
        "memory_class",
        "memory_class_source",
        "memory_class_confidence",
        "event_role",
        "event_role_version",
        "tags",
        "session_refs",
        "created_at",
        "updated_at",
        "raw_title",
        "confidence",
        "files",
        "upsert_key",
        "archived_at",
        "archive_reason",
        "changelog",
        "mem0_ids",
        "access_count",
        "last_accessed_at",
    }
)


def _metadata_to_dict(metadata: MemoryMetadata | dict[str, Any] | None) -> dict[str, Any]:
    if metadata is None:
        return {}
    if isinstance(metadata, BaseModel):
        if hasattr(metadata, "model_dump"):
            return metadata.model_dump(mode="json", exclude_none=True)
        return metadata.dict(exclude_none=True)
    return dict(metadata)


def _user_memory_metadata(metadata: MemoryMetadata | dict[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    metadata_payload = _metadata_to_dict(metadata)
    return {
        key: value
        for key, value in metadata_payload.items()
        if key not in _PROTECTED_MEMORY_METADATA_KEYS
    }


def _validate_procedure_contract_binding(memory_class: str, metadata: Mapping[str, Any]) -> None:
    has_contract = isinstance(metadata.get("procedure_contract"), Mapping)
    is_procedural = str(memory_class or "").strip().casefold() == app_core_hit.MemoryClass.PROCEDURAL.value
    if is_procedural and not has_contract:
        raise HTTPException(
            status_code=422,
            detail={"code": "procedural_memory_requires_procedure_contract"},
        )
    if has_contract and not is_procedural:
        raise HTTPException(
            status_code=422,
            detail={"code": "procedure_contract_requires_procedural_memory_class"},
        )


def _mem0_metadata_for_record(record: dict) -> dict[str, Any]:
    metadata = record.get("metadata") or {}
    return {
        **metadata,
        "project": record["project"],
        "source_system": record["source_system"],
        "source_id": record["source_id"],
        "memory_type": record["memory_type"],
        "memory_class": record.get("memory_class") or metadata.get("memory_class") or app_core_hit.MemoryClass.UNCLASSIFIED.value,
        "memory_class_source": record.get("memory_class_source") or metadata.get("memory_class_source") or MemoryClassSource.LEGACY_DEFAULT.value,
        "memory_class_confidence": record.get("memory_class_confidence") if record.get("memory_class_confidence") is not None else metadata.get("memory_class_confidence"),
        "event_role": record.get("event_role") or metadata.get("event_role") or app_core_hit.MemoryEventRole.UNCLASSIFIED.value,
        "event_role_version": record.get("event_role_version") or metadata.get("event_role_version") or EVENT_ROLE_SCHEMA_VERSION,
        "tags": record["tags"],
        "session_refs": record["session_refs"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    }


def _vector_targets_for_record(record: dict) -> list[str]:
    return list(route_vector_targets(record).targets)


def _vector_id_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = []
    return [str(item) for item in items if str(item).strip()]


def _vector_metadata_for_record(
    record: dict,
    *,
    collection_name: str,
    context_origin: str,
    vector_targets: list[str],
) -> dict[str, Any]:
    metadata = _mem0_metadata_for_record(record)
    metadata["context_origin"] = context_origin
    metadata["context_origins"] = [context_origin]
    metadata["vector_collection"] = collection_name
    metadata["vector_targets"] = vector_targets
    metadata["vector_scope"] = "local+global" if "global" in vector_targets else "local"
    return metadata


def _sync_mem0_record(record: dict) -> dict[str, list[str]]:
    metadata = ensure_decay_metadata(
        record.setdefault("metadata", {}),
        fallback_at=record.get("created_at") or record.get("updated_at") or rt._utc_now_iso(),
    )
    record["metadata"] = metadata
    vector_targets = _vector_targets_for_record(record)
    global_ids_existing = _vector_id_list(metadata.get("global_mem0_ids"))
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
            "local": _vector_id_list(metadata.get("mem0_ids")),
            "global": global_ids_existing if "global" in vector_targets else [],
        }

    local_ids = app_core_hit._write_vector_record(
        record=record,
        collection_name=local_collection,
        context_origin=app_core_hit._VECTOR_CONTEXT_LOCAL,
        vector_targets=vector_targets,
        existing_ids=_vector_id_list(metadata.get("mem0_ids")),
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


def _remember_live_memory(request: RememberRequest) -> dict:
    source_id = f"mem_bhm_{uuid.uuid4().hex[:16]}"
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    project = rt._canonical_project(request.project)
    temporal_request_fields = {
        key: getattr(request, key)
        for key in (
            "observed_at",
            "valid_from",
            "valid_to",
            "open_interval",
            "supersedes_revision_id",
            "source_episode_id",
            "source_uri",
            "source_digest",
        )
        if getattr(request, key) is not None
    }
    if temporal_request_fields or temporal_contract_enabled():
        app_core_fallback._require_temporal_memory_boundary(write_fields=temporal_request_fields)
    temporal_fields = {}
    if temporal_contract_enabled():
        temporal_fields = normalize_temporal_fields(
            {
                "metadata": {
                    **temporal_request_fields,
                    "observed_at": request.observed_at or now,
                    "observed_at_source": "explicit" if request.observed_at else "transaction-clock",
                    "open_interval": request.open_interval if request.open_interval is not None else True,
                }
            },
            require_observed=True,
        )
    user_metadata = _user_memory_metadata(request.metadata)
    procedure_contract = request.metadata.procedure_contract if request.metadata is not None else None
    capability_available = (
        typed_memory_capability_available(rt._memory_service().repository.path)
        if rt._memory_store_is_authoritative()
        else True
    )
    try:
        classification = classify_new_memory(
            explicit_class=request.memory_class,
            memory_type=request.type,
            event_role=request.event_role,
            procedure_contract=procedure_contract,
            capability_available=capability_available,
        )
    except TypedMemoryContractUnavailable as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
    memory_class = classification.memory_class
    memory_class_source = classification.source
    event_role = request.event_role or app_core_hit.MemoryEventRole.UNCLASSIFIED
    _validate_procedure_contract_binding(memory_class.value, user_metadata)
    metadata = initial_decay_metadata(
        {
            "raw_title": _build_memory_title(request.content),
            "confidence": None,
            "files": request.files or [],
            "upsert_key": request.upsert_key,
            "memory_class": memory_class.value,
            "memory_class_source": memory_class_source.value,
            "memory_class_confidence": classification.confidence,
            "memory_class_rule_id": classification.rule_id,
            "memory_class_rule_version": classification.rule_version,
            "event_role": event_role.value,
            "event_role_version": EVENT_ROLE_SCHEMA_VERSION,
            **user_metadata,
        },
        created_at=now,
    )
    record = {
        "source_system": "bhm",
        "source_id": source_id,
        "project": project,
        "agent_id": "workspace",
        "memory_type": request.type,
        "memory_class": memory_class.value,
        "memory_class_source": memory_class_source.value,
        "memory_class_confidence": classification.confidence,
        "event_role": event_role.value,
        "event_role_version": EVENT_ROLE_SCHEMA_VERSION,
        **temporal_fields,
        "content": request.content,
        "summary": None,
        "tags": request.concepts or [],
        "session_refs": [],
        "created_at": now,
        "updated_at": now,
        "metadata": metadata,
    }

    if rt._memory_store_is_authoritative():
        _sync_mem0_record(record)
        _save_live_memories([record])
        return record

    live_records = rt._load_live_memories()
    live_records.append(record)
    _sync_mem0_record(record)
    live_records[-1] = record
    _save_live_memories(live_records)
    return record


def _temporal_request_fields(request: Any) -> dict[str, Any]:
    return {
        key: getattr(request, key)
        for key in (
            "observed_at",
            "valid_from",
            "valid_to",
            "open_interval",
            "supersedes_revision_id",
            "source_episode_id",
            "source_uri",
            "source_digest",
        )
        if getattr(request, key, None) is not None
    }


def _apply_temporal_update(item: dict[str, Any], request: Any, *, now: str) -> None:
    request_fields = _temporal_request_fields(request)
    if not request_fields and not temporal_contract_enabled():
        return
    if temporal_contract_enabled():
        app_core_fallback._require_temporal_memory_boundary(write_fields=request_fields)
        existing = normalize_temporal_fields(item)
        merged = {**existing, **request_fields}
        if "observed_at" not in request_fields:
            merged["observed_at"] = now
            merged["observed_at_source"] = "transaction-clock"
        normalized = normalize_temporal_fields({"metadata": merged}, require_observed=True)
        item.update(normalized)


def _update_live_memory(request: MemoryUpdateRequest) -> dict:
    app_core_fallback._require_typed_memory_boundary(
        memory_class=request.memory_class,
        event_role=request.event_role,
    )
    if rt._memory_store_is_authoritative():
        item = rt._find_live_memory(request.id, request.project)
        if item is None:
            raise HTTPException(status_code=404, detail="memory not found in live store")
        item = copy.deepcopy(item)
        now = rt._utc_now_iso()
        _apply_temporal_update(item, request, now=now)
        canonical_project = rt._canonical_project(request.project) if request.project else None
        if canonical_project:
            item["project"] = canonical_project
        if request.type:
            item["memory_type"] = request.type
        if request.memory_class is not None:
            item["memory_class"] = request.memory_class.value
            item["memory_class_source"] = MemoryClassSource.CALLER_EXPLICIT.value
            item.setdefault("metadata", {})["memory_class"] = request.memory_class.value
            item["metadata"]["memory_class_source"] = MemoryClassSource.CALLER_EXPLICIT.value
        if request.event_role is not None:
            item["event_role"] = request.event_role.value
            item["event_role_version"] = EVENT_ROLE_SCHEMA_VERSION
            item.setdefault("metadata", {})["event_role"] = request.event_role.value
            item["metadata"]["event_role_version"] = EVENT_ROLE_SCHEMA_VERSION
        if request.content is not None:
            item["content"] = request.content
            item.setdefault("metadata", {})["raw_title"] = _build_memory_title(request.content)
        if request.concepts is not None:
            item["tags"] = request.concepts
        if request.files is not None:
            item.setdefault("metadata", {})["files"] = request.files
        if request.metadata_patch is not None:
            item.setdefault("metadata", {}).update(_user_memory_metadata(request.metadata_patch))
        _validate_procedure_contract_binding(_effective_memory_class(item), item.setdefault("metadata", {}))
        item["updated_at"] = now
        item["metadata"] = ensure_decay_metadata(
            item.setdefault("metadata", {}),
            fallback_at=item.get("created_at") or now,
        )
        _append_memory_changelog(
            item,
            "update",
            {
                "type": request.type,
                "memory_class": request.memory_class.value if request.memory_class else None,
                "event_role": request.event_role.value if request.event_role else None,
                "content_changed": request.content is not None,
                "metadata_changed": request.metadata_patch is not None,
            },
        )
        _update_mem0_record(item)
        _save_live_memories([item])
        return item

    live_records = rt._load_live_memories()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    canonical_project = rt._canonical_project(request.project) if request.project else None
    accepted_projects = rt._project_aliases(request.project)

    for item in live_records:
        if item.get("source_id") != request.id:
            continue
        if request.project and item.get("project") not in accepted_projects:
            continue

        _apply_temporal_update(item, request, now=now)

        if canonical_project:
            item["project"] = canonical_project
        if request.type:
            item["memory_type"] = request.type
        if request.memory_class is not None:
            item["memory_class"] = request.memory_class.value
            item["memory_class_source"] = MemoryClassSource.CALLER_EXPLICIT.value
            item.setdefault("metadata", {})["memory_class"] = request.memory_class.value
            item["metadata"]["memory_class_source"] = MemoryClassSource.CALLER_EXPLICIT.value
        if request.event_role is not None:
            item["event_role"] = request.event_role.value
            item["event_role_version"] = EVENT_ROLE_SCHEMA_VERSION
            item.setdefault("metadata", {})["event_role"] = request.event_role.value
            item["metadata"]["event_role_version"] = EVENT_ROLE_SCHEMA_VERSION
        if request.content is not None:
            item["content"] = request.content
            item.setdefault("metadata", {})["raw_title"] = _build_memory_title(request.content)
        if request.concepts is not None:
            item["tags"] = request.concepts
        if request.files is not None:
            item.setdefault("metadata", {})["files"] = request.files
        if request.metadata_patch is not None:
            item.setdefault("metadata", {}).update(_user_memory_metadata(request.metadata_patch))
        _validate_procedure_contract_binding(_effective_memory_class(item), item.setdefault("metadata", {}))

        item["updated_at"] = now
        item["metadata"] = ensure_decay_metadata(
            item.setdefault("metadata", {}),
            fallback_at=item.get("created_at") or now,
        )
        _append_memory_changelog(
            item,
            "update",
            {
                "type": request.type,
                "memory_class": request.memory_class.value if request.memory_class else None,
                "event_role": request.event_role.value if request.event_role else None,
                "content_changed": request.content is not None,
                "metadata_changed": request.metadata_patch is not None,
            },
        )
        _update_mem0_record(item)
        _save_live_memories(live_records)
        return item

    raise HTTPException(status_code=404, detail="memory not found in live store")


def _upsert_live_memory(request: MemoryUpsertRequest) -> tuple[str, dict]:
    canonical_project = rt._canonical_project(request.project)
    existing = _find_live_memory_by_upsert_key(canonical_project, request.upsert_key)
    if existing is None:
        record = _remember_live_memory(
            RememberRequest(
                project=canonical_project,
                type=request.type,
                memory_class=request.memory_class,
                event_role=request.event_role,
                observed_at=request.observed_at,
                valid_from=request.valid_from,
                valid_to=request.valid_to,
                open_interval=request.open_interval,
                supersedes_revision_id=request.supersedes_revision_id,
                source_episode_id=request.source_episode_id,
                source_uri=request.source_uri,
                source_digest=request.source_digest,
                content=request.content,
                concepts=request.concepts,
                files=request.files,
                upsert_key=request.upsert_key,
                metadata=request.metadata,
            )
        )
        return "created", record

    metadata_patch = _user_memory_metadata(request.metadata) if request.metadata is not None else None
    updated = _update_live_memory(
        MemoryUpdateRequest(
            id=existing["source_id"],
            project=canonical_project,
            type=request.type,
            memory_class=request.memory_class,
            event_role=request.event_role,
            observed_at=request.observed_at,
            valid_from=request.valid_from,
            valid_to=request.valid_to,
            open_interval=request.open_interval,
            supersedes_revision_id=request.supersedes_revision_id,
            source_episode_id=request.source_episode_id,
            source_uri=request.source_uri,
            source_digest=request.source_digest,
            content=request.content,
            concepts=request.concepts,
            files=request.files,
            metadata_patch=metadata_patch,
        )
    )
    updated.setdefault("metadata", {})["upsert_key"] = request.upsert_key
    if rt._memory_store_is_authoritative():
        _save_live_memories([updated])
        if metadata_patch is not None:
            updated.setdefault("metadata", {}).update(metadata_patch)
        updated["metadata"] = ensure_decay_metadata(
            updated["metadata"],
            fallback_at=updated.get("created_at") or updated.get("updated_at"),
        )
        return "updated", updated
    live_records = rt._load_live_memories()
    for item in live_records:
        if item.get("source_id") == updated.get("source_id"):
            item_metadata = item.setdefault("metadata", {})
            if metadata_patch is not None:
                item_metadata.update(metadata_patch)
            item_metadata["upsert_key"] = request.upsert_key
            item["metadata"] = ensure_decay_metadata(item_metadata, fallback_at=item.get("created_at") or item.get("updated_at"))
            break
    _save_live_memories(live_records)
    if metadata_patch is not None:
        updated.setdefault("metadata", {}).update(metadata_patch)
    updated["metadata"]["upsert_key"] = request.upsert_key
    updated["metadata"] = ensure_decay_metadata(
        updated["metadata"],
        fallback_at=updated.get("created_at") or updated.get("updated_at"),
    )
    return "updated", updated


def _crystallize_memories(request: MemoryCrystallizeRequest) -> tuple[str, dict]:
    if len(request.source_ids) < 1:
        raise HTTPException(status_code=400, detail="source_ids must not be empty")

    source_records = []
    for memory_id in request.source_ids:
        record = rt._find_live_memory(memory_id, request.project)
        if record is None:
            raise HTTPException(status_code=404, detail=f"source memory not found: {memory_id}")
        source_records.append(record)

    source_titles = [
        (record.get("metadata") or {}).get("raw_title") or _build_memory_title(record.get("content") or "")
        for record in source_records
    ]
    source_files = sorted(
        {
            file_path
            for record in source_records
            for file_path in ((record.get("metadata") or {}).get("files") or [])
        }
    )
    files = request.files if request.files is not None else source_files

    lines = [
        f"{request.title}",
        f"summary: {request.summary}",
        "source_ids:",
    ]
    for source_id in request.source_ids:
        lines.append(f"- {source_id}")
    if source_titles:
        lines.append("source_titles:")
        for title in source_titles:
            lines.append(f"- {title}")
    content = "\n".join(lines)

    if request.upsert_key:
        action, record = _upsert_live_memory(
            MemoryUpsertRequest(
                upsert_key=request.upsert_key,
                project=request.project,
                type=request.target_type,
                content=content,
                concepts=request.concepts,
                files=files,
            )
        )
    else:
        record = _remember_live_memory(
            RememberRequest(
                project=request.project,
                type=request.target_type,
                content=content,
                concepts=request.concepts,
                files=files,
            )
        )
        action = "created"

    metadata = record.setdefault("metadata", {})
    metadata["crystallized_from"] = request.source_ids
    metadata["crystallized_summary"] = request.summary
    metadata["crystallized_title"] = request.title
    if request.upsert_key:
        metadata["upsert_key"] = request.upsert_key

    live_records = rt._load_live_memories()
    for item in live_records:
        if item.get("source_id") == record.get("source_id"):
            item.setdefault("metadata", {}).update(metadata)
            break
    _save_live_memories(live_records)
    for source_id in request.source_ids:
        rt._emit_memory_pulse(source_id, request.project)
    rt._emit_memory_pulse(record.get("source_id"), request.project)
    return action, record


def _merge_memory_records(
    source: dict,
    target: dict,
    *,
    archive_source: bool,
    target_id: str,
    now: str | None = None,
) -> tuple[dict, dict]:
    source = copy.deepcopy(source)
    target = copy.deepcopy(target)
    source_metadata = dict(source.get("metadata") or {})
    target_metadata = target.setdefault("metadata", {})
    source_files = set(source_metadata.get("files") or [])
    target_files = set(target_metadata.get("files") or [])
    source_tags = set(source.get("tags") or [])
    target_tags = set(target.get("tags") or [])
    merged_lines = [
        target.get("content") or "",
        "",
        "merged_memory:",
        f"- source_id: {source.get('source_id')}",
        f"- source_title: {source_metadata.get('raw_title') or _build_memory_title(source.get('content') or '')}",
        "source_content:",
        source.get("content") or "",
    ]
    target["content"] = "\n".join(line for line in merged_lines if line is not None).strip()
    target["tags"] = sorted(target_tags | source_tags)
    target_metadata["files"] = sorted(target_files | source_files)
    target_metadata["raw_title"] = _build_memory_title(target.get("content") or "")
    target_metadata["merged_from"] = sorted(
        set(target_metadata.get("merged_from") or []) | {source.get("source_id")}
    )
    timestamp = now or rt._utc_now_iso()
    target["updated_at"] = timestamp

    if archive_source:
        source_metadata = source.setdefault("metadata", {})
        source_metadata["archived_at"] = timestamp
        source_metadata["archive_reason"] = f"merged_into:{target_id}"
        source_metadata["merged_into"] = target_id
        source["updated_at"] = timestamp
        _append_memory_changelog(source, "archive", {"reason": f"merged_into:{target_id}"})

    return target, source


def _detect_duplicates(request: MemoryDetectRequest) -> list[dict]:
    limit = max(min(request.limit, 200), 1)
    records = sorted(
        [
        item for item in rt._load_live_memories()
        if app_core_hit._memory_matches_filters(item, project=request.project, include_archived=request.include_archived)
        ],
        key=lambda item: (str(item.get("project") or ""), str(item.get("source_id") or "")),
    )
    derived: dict[str, dict[str, Any]] = {}
    exact_content: dict[tuple[str, str], list[dict]] = {}
    title_and_files: dict[tuple[str, str, tuple[str, ...]], list[dict]] = {}
    titles: dict[tuple[str, str], list[dict]] = {}
    files_and_prefix: dict[tuple[str, tuple[str, ...], str], list[dict]] = {}
    for item in records:
        memory_id = str(item.get("source_id") or "")
        project = str(item.get("project") or "")
        metadata = item.get("metadata") or {}
        title = str(metadata.get("raw_title") or "").strip()
        normalized_title = title.casefold()
        text = app_core_hit._normalized_text(item.get("content"))
        files = tuple(sorted(str(value) for value in metadata.get("files") or []))
        derived[memory_id] = {
            "record": item,
            "title": title,
            "display_title": title or _build_memory_title(item.get("content") or ""),
        }
        if text:
            exact_content.setdefault((project, text), []).append(item)
        if normalized_title:
            titles.setdefault((project, normalized_title), []).append(item)
            if files:
                title_and_files.setdefault((project, normalized_title, files), []).append(item)
        if files and text[:120]:
            files_and_prefix.setdefault((project, files, text[:120]), []).append(item)

    candidates: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    candidate_budget = max(limit * 4, 200)

    def add_bucket_pairs(buckets: Mapping[Any, list[dict]], score: float, reason: str) -> None:
        for key in sorted(buckets, key=repr):
            bucket = sorted(buckets[key], key=lambda item: str(item.get("source_id") or ""))
            for left_index, left in enumerate(bucket):
                left_id = str(left.get("source_id") or "")
                for right in bucket[left_index + 1:]:
                    right_id = str(right.get("source_id") or "")
                    pair = (left_id, right_id)
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    candidates.append(
                        {
                            "left_id": left_id,
                            "right_id": right_id,
                            "project": left.get("project"),
                            "score": score,
                            "reason": reason,
                            "left_title": derived[left_id]["display_title"],
                            "right_title": derived[right_id]["display_title"],
                        }
                    )
                    if len(candidates) >= candidate_budget:
                        return

    add_bucket_pairs(exact_content, 1.0, "identical_content")
    if len(candidates) < candidate_budget:
        add_bucket_pairs(title_and_files, 0.92, "same_title_same_files")
    if len(candidates) < candidate_budget:
        add_bucket_pairs(titles, 0.82, "same_title")
    if len(candidates) < candidate_budget:
        add_bucket_pairs(files_and_prefix, 0.78, "same_files_similar_prefix")
    candidates.sort(key=lambda item: (item.get("score") or 0.0, item.get("left_title") or ""), reverse=True)
    return candidates[:limit]


def _project_summary_get(project: str) -> dict:
    canonical_project = rt._canonical_project(project)
    record = _find_live_memory_by_upsert_key(canonical_project, f"project-summary:{canonical_project}")
    if record is None:
        raise HTTPException(status_code=404, detail="project summary not found")
    return _serialize_memory_record(record)


