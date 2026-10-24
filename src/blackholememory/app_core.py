"""Domain module app_core: moved out of app.py (FIX-017). Cross-module calls go through module attributes."""

from __future__ import annotations

import time
import asyncio
import hashlib
import json
from typing import Any, Mapping
from fastapi import HTTPException
from . import app_core_fallback
from . import app_runtime as rt
from .app_runtime import ResponseTimeout, _env_float, _env_int, _run_bounded_read, _safe_exception_text
from .api_models import RememberRequest
from .capability import configured_admin_capability
from .capability import extract_mcp_capability
from .capability import MCP_OPERATOR_SESSION
from .capability import is_admin_capability_valid
from .mcp_protocol_contract import validate_bhm_remember_arguments
from .ingress_admission import IngressKind
from .ingress_admission import admit_ingress
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
from . import memory_contracts as _memory_contracts
from .version_manifest import BROKER_VERSION
from .observation_security import PayloadSanitizer
from .observation_security import redact_secret_text
from .usage_telemetry import UsageTelemetry
from .usage_telemetry import monotonic_elapsed_ms
from .usage_telemetry import normalize_operation
from .code_graph import PARSER_REGISTRY_DIGEST
from .error_taxonomy import classify_jsonrpc_error
from .app_core_memory_base import _append_memory_changelog, _build_memory_title, _effective_event_role, _effective_memory_class, _find_live_memory_by_upsert_key, _is_archived_memory, _mem0_metadata_for_record, _memory_lifecycle, _metadata_matches_taxonomy_filters, _metadata_to_dict, _replace_live_memory, _resolve_live_memory_for_vector_item, _save_live_memories, _serialize_memory_record, _user_memory_metadata, _validate_procedure_contract_binding, _vector_id_list, _vector_metadata_for_record, _vector_targets_for_record
from .app_core_memory_projection import _sync_mem0_record, _update_mem0_record
from .app_core_memory_query import _vector_item_matches_search_request
from .app_core_memory_write import _apply_temporal_update, _crystallize_memories, _detect_duplicates, _merge_memory_records, _project_summary_get, _remember_live_memory, _temporal_request_fields, _update_live_memory, _upsert_live_memory

__all__ = (
    '_append_memory_changelog', '_build_memory_title', '_effective_event_role', '_effective_memory_class',
    '_find_live_memory_by_upsert_key', '_is_archived_memory', '_mem0_metadata_for_record', '_memory_lifecycle',
    '_metadata_matches_taxonomy_filters', '_metadata_to_dict', '_replace_live_memory', '_resolve_live_memory_for_vector_item',
    '_save_live_memories', '_serialize_memory_record', '_user_memory_metadata', '_validate_procedure_contract_binding',
    '_vector_id_list', '_vector_metadata_for_record', '_vector_targets_for_record', '_sync_mem0_record', '_update_mem0_record',
    '_vector_item_matches_search_request', '_apply_temporal_update', '_crystallize_memories', '_detect_duplicates',
    '_merge_memory_records', '_project_summary_get', '_remember_live_memory', '_temporal_request_fields', '_update_live_memory',
    '_upsert_live_memory', 'MemoryMetadata', 'MemoryClassSource', 'EVENT_ROLE_SCHEMA_VERSION',
)


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


